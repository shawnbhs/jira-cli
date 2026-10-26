"""Menu section: Create issue (guided).

The flow is a short list of steps run by a tiny state machine.  ``b`` at any
prompt goes back exactly one step and every answer already given is kept and
offered again as the default, so a typo late in the flow never costs the
operator what they typed earlier.  Every typed value is checked at its own
prompt (estimate, due date, labels, epic and parent keys) and re-asked with a
short reason, so the final preview only ever shows values Jira will accept.

Sprints are asked only on a board that has them: kanban boards (most boards on
this server) answer the agile sprint endpoint with HTTP 400, and any Jira error
while looking up a board or its sprints simply means "no sprint" plus a note.
Nothing is written before the final ``y``.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..durations import to_jira
from ..errors import InputError, JiraError, NotFoundError
from ..ids import validate_issue_key
from ..textsafe import terminal_safe_line
from .menu import _ask, _Back, _pick_project, _Session, _svc

_ROOT = (__package__ or "jira_cli.cli").rsplit(".", 1)[0]

#: Answer to an optional prompt that clears a previously given value.
CLEAR = "-"
_YES = frozenset({"y", "yes"})
_RELATIVE_DUE_RE = re.compile(r"(?i)^\+?(\d{1,4})\s*([dwm])$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DAYS_PER_UNIT = {"d": 1, "w": 7, "m": 30}
_SUBTASK_NAMES = frozenset({"sub-task", "subtask"})


def _today() -> date:
    """Today's date; a seam so tests can pin the clock."""
    return date.today()


def _people_menu() -> Any:
    """The shared people pickers, imported lazily (tests swap ``sys.modules``)."""
    return importlib.import_module(f"{_ROOT}.cli.menu_people")


# ------------------------------------------------------------------ parsing


def parse_due(text: str, today: date) -> str:
    """Resolve a due date to ``YYYY-MM-DD``.

    Accepts ``YYYY-MM-DD``, ``today``, ``tomorrow`` or a *forward* offset such
    as ``7d``, ``2w`` or ``1m`` (calendar days from ``today``; a month is 30
    days).  ``durations.resolve_date`` is deliberately not used: it counts
    ``7d`` backwards, which is right for "since" windows and wrong for a due date.
    """
    raw = text.strip().lower()
    if raw == "today":
        return today.isoformat()
    if raw == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    relative = _RELATIVE_DUE_RE.match(raw)
    if relative:
        days = int(relative.group(1)) * _DAYS_PER_UNIT[relative.group(2)]
        return (today + timedelta(days=days)).isoformat()
    explicit = _ISO_DATE_RE.match(raw)
    if not explicit:
        raise InputError(f"cannot read due date {text!r}; use YYYY-MM-DD or 7d / 2w from today")
    try:
        return date(*(int(part) for part in explicit.groups())).isoformat()
    except ValueError as exc:
        raise InputError(f"{text!r} is not a real date: {exc}") from None


def parse_estimate(text: str) -> str:
    """Validate a Jira duration (``3h``, ``1d 2h``, ``30m``) and normalise it."""
    return to_jira(text)


def parse_labels(text: str) -> list[str]:
    """Split ``"a, b"`` into labels; a label may not contain whitespace."""
    labels = [part.strip() for part in text.split(",") if part.strip()]
    for label in labels:
        if re.search(r"\s", label):
            raise InputError(f"label {label!r} must not contain spaces (separate labels with commas)")
    return labels


def parse_numbers(text: str, count: int) -> list[int]:
    """Parse ``"1,3"`` into zero-based indexes, each within ``1..count``."""
    picked: list[int] = []
    for part in text.split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit() or not 1 <= int(token) <= count:
            raise InputError(f"{token!r} is not a number from 1 to {count}")
        if int(token) - 1 not in picked:
            picked.append(int(token) - 1)
    return picked


# ------------------------------------------------------------------ prompts


def _ask_optional(session: _Session, prompt: str, parse: Any, previous: Any, shown: str | None) -> Any:
    """Ask an optional value, re-asking until ``parse`` accepts it.

    Blank means "none" (or "keep" when there is a previous answer, shown as the
    default); ``-`` clears a previous answer.  ``parse`` raises
    :class:`InputError` with the reason to show.
    """
    hint = f"{prompt} (blank = keep, {CLEAR} = none)" if shown else f"{prompt} (blank = none)"
    while True:
        text = _ask(session.io, hint, default=shown)
        if not text or text == CLEAR:
            return None
        if shown is not None and text == shown:
            return previous
        try:
            return parse(text)
        except InputError as exc:
            session.say(f"  {exc.message} - try again (b to go back)")


def _pick_index(session: _Session, title: str, options: list[str], default: int | None) -> int:
    """Numbered pick where Enter takes ``default`` and ``b`` goes back.

    ``MenuIO.choose`` treats Enter as "back", which is wrong for optional
    pickers (B9), so this asks through ``io.ask`` instead.
    """
    lines = [terminal_safe_line(title)]
    lines += [f"  {n:>2}. {terminal_safe_line(option)}" for n, option in enumerate(options, 1)]
    lines.append("   b. back")
    session.io.show("\n".join(lines))
    shown = str(default + 1) if default is not None else None
    while True:
        text = _ask(session.io, "Choice", default=shown, required=True)
        if text.isdigit() and 1 <= int(text) <= len(options):
            return int(text) - 1
        session.say(f"  pick a number from 1 to {len(options)} (b to go back)")


def _first_line(text: str | None) -> str:
    if not text:
        return "-"
    lines = text.splitlines()
    more = f"  (+{len(lines) - 1} more lines)" if len(lines) > 1 else ""
    return f"{lines[0]}{more}"


# -------------------------------------------------------------------- draft


@dataclass
class _Draft:
    """Every answer so far.  Steps read their previous answer from here."""

    project: str | None = None
    issue_type: str | None = None  # as picked; see _CreateFlow.effective_type
    subtask_type: str | None = None  # used instead when a parent is given
    summary: str | None = None
    description: str | None = None
    assignee: str | None = None
    priority: str | None = None
    estimate: str | None = None
    due: str | None = None
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    parent: str | None = None
    epic: str | None = None
    board: dict | None = None
    sprint: dict | None = None

    def forget_project_answers(self) -> None:
        """Drop answers that only make sense inside the previous project."""
        self.issue_type = None
        self.subtask_type = None
        self.assignee = None
        self.components = []
        self.parent = None
        self.epic = None
        self.board = None
        self.sprint = None


class _CreateFlow:
    """Guided create as an ordered list of steps with one-step back."""

    def __init__(self, session: _Session) -> None:
        self.session = session
        self.draft = _Draft()
        self._types: list[dict] = []
        self._components: list[str] = []
        self._sprints: list[dict] = []
        self._board_checked_for: str | None = None
        self.steps = [
            self.step_project,
            self.step_type,
            self.step_summary,
            self.step_description,
            self.step_assignee,
            self.step_priority,
            self.step_estimate,
            self.step_due,
            self.step_labels,
            self.step_components,
            self.step_parent,
            self.step_epic,
            self.step_sprint,
            self.step_confirm,
        ]

    # ----------------------------------------------------------- runner

    def run(self) -> None:
        """Run the steps; ``_Back`` re-opens the last step that asked something."""
        asked: list[int] = []
        index = 0
        while index < len(self.steps):
            try:
                prompted = self.steps[index]()
            except _Back:
                if not asked:
                    raise
                index = asked.pop()
                continue
            if prompted:
                asked.append(index)
            index += 1

    # --------------------------------------------------------- lookups

    @property
    def client(self) -> Any:
        return self.session.client

    def _load_project(self, key: str) -> None:
        """Fetch what the later steps need about ``key`` (types, components)."""
        try:
            raw = _svc("issues").createmeta_issue_types(self.client, key)
            types = [
                {"name": str(t.get("name")), "subtask": bool(t.get("subtask"))} for t in raw if t.get("name")
            ]
        except JiraError:
            types = []
        if not types:
            names = _svc("projects").list_issue_types(self.client, key)
            types = [{"name": n, "subtask": n.casefold() in _SUBTASK_NAMES} for n in names]
        self._types = types
        try:
            self._components = list(_svc("projects").get_project(self.client, key).get("components") or [])
        except JiraError as exc:
            self._components = []
            self.session.say(f"note: components unavailable ({exc}); skipping components")

    def _subtask_types(self) -> list[str]:
        return [t["name"] for t in self._types if t["subtask"]]

    def _is_subtask(self, name: str | None) -> bool:
        return bool(name) and name in self._subtask_types()

    def effective_type(self) -> str | None:
        """B8: the type Jira will really get -- a sub-task type once a parent is set."""
        draft = self.draft
        if draft.parent and not self._is_subtask(draft.issue_type):
            return draft.subtask_type
        return draft.issue_type

    # ----------------------------------------------------------- steps
    # Each step returns True when it prompted the operator (so ``b`` from
    # the next step can return to it) and False when it had nothing to ask.

    def step_project(self) -> bool:
        key = _pick_project(self.session)
        if key != self.draft.project:
            self.draft.forget_project_answers()
            self.draft.project = key
            self._load_project(key)
        return True

    def step_type(self) -> bool:
        if not self._types:
            raise InputError(f"Jira lists no issue types you can create in {self.draft.project}")
        names = [t["name"] for t in self._types]
        options = [
            f"{t['name']}  (sub-task: needs a parent)" if t["subtask"] else t["name"] for t in self._types
        ]
        current = self.draft.issue_type
        if current in names:
            default = names.index(current)
        else:
            default = next((i for i, t in enumerate(self._types) if t["name"] == "Task"), None)
        self.draft.issue_type = names[_pick_index(self.session, "Issue type", options, default)]
        return True

    def step_summary(self) -> bool:
        self.draft.summary = _ask(self.session.io, "Summary", default=self.draft.summary, required=True)
        return True

    def step_description(self) -> bool:
        previous = self.draft.description
        self.draft.description = _ask_optional(
            self.session, "Description", lambda text: text, previous, previous
        )
        return True

    def step_assignee(self) -> bool:
        if self.draft.assignee:
            self.session.say(f"current assignee: {self.draft.assignee}")
        self.draft.assignee = _people_menu().ask_person(
            self.session, "Assignee", project=self.draft.project, allow_none=True
        )
        return True

    def step_priority(self) -> bool:
        try:
            priorities = [str(p) for p in _svc("projects").list_priorities(self.client)]
        except JiraError as exc:
            self.session.say(f"note: priorities unavailable ({exc}); using the server default")
            self.draft.priority = None
            return False
        options = ["(server default)", *priorities]
        default = priorities.index(self.draft.priority) + 1 if self.draft.priority in priorities else 0
        index = _pick_index(self.session, "Priority (Enter = default)", options, default)
        self.draft.priority = None if index == 0 else priorities[index - 1]
        return True

    def step_estimate(self) -> bool:
        previous = self.draft.estimate
        self.draft.estimate = _ask_optional(
            self.session, "Original estimate (e.g. 3h, 1d 2h, 30m)", parse_estimate, previous, previous
        )
        return True

    def step_due(self) -> bool:
        previous = self.draft.due
        today = _today()
        self.draft.due = _ask_optional(
            self.session,
            "Due date (YYYY-MM-DD, or 7d / 2w from today)",
            lambda text: parse_due(text, today),
            previous,
            previous,
        )
        return True

    def step_labels(self) -> bool:
        previous = self.draft.labels
        shown = ", ".join(previous) if previous else None
        self.draft.labels = (
            _ask_optional(self.session, "Labels (comma separated)", parse_labels, previous, shown) or []
        )
        return True

    def step_components(self) -> bool:
        if not self._components:
            self.draft.components = []
            return False
        names = self._components
        lines = ["Components (numbers, comma separated)"]
        lines += [f"  {n:>2}. {terminal_safe_line(name)}" for n, name in enumerate(names, 1)]
        self.session.io.show("\n".join(lines))
        previous = self.draft.components
        shown = ",".join(str(names.index(c) + 1) for c in previous if c in names) or None

        def parse(text: str) -> list[str]:
            return [names[i] for i in parse_numbers(text, len(names))]

        self.draft.components = _ask_optional(self.session, "Components", parse, previous, shown) or []
        return True

    def step_parent(self) -> bool:
        draft = self.draft
        needs_parent = self._is_subtask(draft.issue_type)
        if needs_parent:
            prompt = "Parent issue key (required for a sub-task)"
        else:
            prompt = "Parent key (makes it a sub-task)"
        while True:
            draft.parent = _ask_optional(self.session, prompt, self._check_parent, draft.parent, draft.parent)
            if draft.parent or not needs_parent:
                break
            self.session.say(f"  {draft.issue_type} is a sub-task type and needs a parent")
        draft.subtask_type = None
        if draft.parent and not needs_parent:
            draft.subtask_type = self._choose_subtask_type()
        return True

    def _choose_subtask_type(self) -> str:
        """B8: a parent turns the issue into a sub-task; show which type."""
        subtasks = self._subtask_types()
        if not subtasks:
            self.session.say("note: Jira lists no sub-task type here; using 'Sub-task'")
            return "Sub-task"
        if len(subtasks) == 1:
            self.session.say(f"note: with a parent this becomes a {subtasks[0]}")
            return subtasks[0]
        return subtasks[_pick_index(self.session, "Sub-task type", subtasks, 0)]

    def _fetch_issue(self, key: str) -> dict:
        try:
            return _svc("issues").get_issue(self.client, key, comments=False)
        except NotFoundError:
            raise InputError(f"{key} does not exist (or you cannot see it)") from None
        except JiraError as exc:
            raise InputError(f"could not check {key}: {exc}") from None

    def _check_parent(self, text: str) -> str:
        key = validate_issue_key(text, what="parent key")
        if key.rsplit("-", 1)[0] != self.draft.project:
            raise InputError(f"a sub-task's parent must be in {self.draft.project}, not {key}")
        issue = self._fetch_issue(key)
        if self._is_subtask(issue.get("type")):
            raise InputError(f"{key} is itself a sub-task ({issue.get('type')}); pick its parent")
        return key

    def _check_epic(self, text: str) -> str:
        key = validate_issue_key(text, what="epic key")
        issue = self._fetch_issue(key)
        kind = str(issue.get("type") or "")
        if kind.casefold() != "epic":
            raise InputError(f"{key} is a {kind or 'non-epic issue'}, not an Epic")
        return key

    def step_epic(self) -> bool:
        draft = self.draft
        if draft.parent or str(self.effective_type()).casefold() == "epic":
            # A sub-task takes its parent's epic, and an epic cannot sit in an epic.
            draft.epic = None
            return False
        draft.epic = _ask_optional(self.session, "Epic key", self._check_epic, draft.epic, draft.epic)
        return True

    def _find_board(self) -> dict | None:
        try:
            return _svc("boards").board_for_project(self.client, self.draft.project)
        except JiraError as exc:
            self.session.say(f"note: could not look up a board ({exc}); no sprint")
            return None

    def step_sprint(self) -> bool:
        draft = self.draft
        draft.sprint = None
        if self._board_checked_for != draft.project:
            self._board_checked_for = draft.project
            draft.board = self._find_board()
            self._sprints = []
            board = draft.board
            if board is None:
                self.session.say(f"note: {draft.project} has no board; no sprint")
            elif str(board.get("type") or "").casefold() == "kanban":
                self.session.say(f"note: board {board.get('name')} is kanban; no sprint")
            else:
                self._sprints = self._load_sprints(board)
        if not self._sprints:
            return False
        options = ["(no sprint)", *(f"{s.get('name')} [{s.get('state')}]" for s in self._sprints)]
        index = _pick_index(self.session, "Sprint (Enter = no sprint)", options, 0)
        draft.sprint = None if index == 0 else self._sprints[index - 1]
        return True

    def _load_sprints(self, board: dict) -> list[dict]:
        try:
            sprints = _svc("boards").list_sprints(self.client, board.get("id"))
        except JiraError as exc:
            self.session.say(f"note: could not list sprints ({exc}); no sprint")
            return []
        if not sprints:
            self.session.say(f"note: board {board.get('name')} has no active or future sprint; no sprint")
        return list(sprints)

    # --------------------------------------------------------- preview

    def preview_lines(self) -> list[str]:
        d = self.draft
        board = f"{d.board.get('name')} (id {d.board.get('id')}, {d.board.get('type')})" if d.board else "-"
        sprint = f"{d.sprint.get('name')} (id {d.sprint.get('id')})" if d.sprint else "-"
        return [
            f"project      {d.project}",
            f"board        {board}",
            f"type         {self.effective_type()}",
            f"summary      {d.summary}",
            f"description  {_first_line(d.description)}",
            f"assignee     {d.assignee or '(unassigned)'}",
            f"priority     {d.priority or '(server default)'}",
            f"estimate     {d.estimate or '-'}",
            f"due          {d.due or '-'}",
            f"labels       {', '.join(d.labels) or '-'}",
            f"components   {', '.join(d.components) or '-'}",
            f"parent       {d.parent or '-'}",
            f"epic         {d.epic or '-'}",
            f"sprint       {sprint}",
        ]

    def step_confirm(self) -> bool:
        body = ["Create issue", *(f"  {terminal_safe_line(line)}" for line in self.preview_lines())]
        self.session.io.show("\n".join(body))
        try:
            answer = self.session.io.ask("Create this issue? y/N (b = back)", None)
        except (KeyboardInterrupt, EOFError):
            answer = ""
        answer = (answer or "").strip().casefold()
        if answer in ("b", "!back"):
            raise _Back
        if answer not in _YES:
            self.session.io.show("cancelled; nothing was written")
            return False
        self._create()
        return False

    def _create(self) -> None:
        d = self.draft
        issues = _svc("issues")
        spec = issues.IssueSpec(
            project=d.project,
            summary=d.summary,
            issue_type=self.effective_type(),
            description=d.description,
            assignee=d.assignee,
            priority=d.priority,
            labels=list(d.labels),
            due_date=d.due,
            original_estimate=d.estimate,
            epic_key=d.epic,
            parent_key=d.parent,
            components=list(d.components),
            sprint_id=d.sprint.get("id") if d.sprint else None,
        )
        created = issues.create_issue(self.client, spec)
        self.session.last_key = created.get("key")
        self.session.say(f"created {created.get('key')}  {created.get('url')}")
        for warning in created.get("warnings") or []:
            self.session.say(f"warning: {warning}")


def run(session: _Session) -> None:
    """Main-menu handler for "Create issue (guided)"."""
    _CreateFlow(session).run()
