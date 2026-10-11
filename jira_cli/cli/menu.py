"""Interactive terminal menu for jira-cli.

The menu is built for a manager who works by hand in a terminal: every screen is
numbered, ``q`` quits from the main menu, and ``b`` (or ``!back``) backs out of any
prompt.  Every write shows a preview first and asks ``y/N``.

All I/O goes through :class:`MenuIO`, so the whole menu is testable offline by
passing a scripted implementation.  Service modules are imported lazily inside the
handlers, which keeps the import graph shallow and lets tests inject fakes through
``sys.modules``.

Server-controlled text (summaries, comments, names, error messages) is passed
through :mod:`jira_cli.textsafe` before it is shown, so an escape sequence in an
issue cannot rewrite a preview or reach the terminal.  Tables rendered by
``cli.output`` are already sanitised there and are shown as they are.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..redact import redact
from ..textsafe import terminal_safe, terminal_safe_line

_ROOT = (__package__ or "jira_cli.cli").rsplit(".", 1)[0]

try:  # pragma: no cover - exercised implicitly in both branches
    from ..errors import JiraError
except ImportError:  # pragma: no cover - core module not installed yet

    class JiraError(Exception):  # type: ignore[no-redef]
        """Fallback used only when ``jira_cli.errors`` cannot be imported."""


#: Answers that mean "go back one level" at any prompt.
BACK_WORDS = frozenset({"b", "!back"})
#: Answers that quit from the main menu.
QUIT_WORDS = frozenset({"q", "quit", "exit"})

_EXPORT_FORMATS = ("md", "csv", "json")
#: Answer to "New value for labels" that removes every label on purpose.
_CLEAR_LABELS = "-"


class _Back(Exception):
    """Internal signal: the operator asked to go back one level."""


# --------------------------------------------------------------------------- IO


class MenuIO:
    """Terminal I/O for the menu.  Subclass or replace it to script the menu."""

    def ask(self, prompt: str, default: str | None = None) -> str:
        """Read one line.  Returns the raw answer (``""`` when the user hit Enter)."""
        suffix = f" [{default}]" if default is not None else ""
        return input(f"{prompt}{suffix}: ")

    def choose(self, title: str, options: Sequence[str]) -> int | None:
        """Show a numbered list and return the chosen index, or ``None`` to go back."""
        print(f"\n{title}")
        for number, option in enumerate(options, start=1):
            print(f"  {number:>2}. {option}")
        print("   b. back")
        while True:
            raw = input("Choice: ").strip()
            if raw.casefold() in BACK_WORDS or not raw:
                return None
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            print("invalid choice")

    def confirm(self, prompt: str) -> bool:
        """Ask a yes/no question.  Anything but an explicit yes is ``False``."""
        return input(f"{prompt} [y/N]: ").strip().casefold() in ("y", "yes")

    def show(self, text: str) -> None:
        """Write a block of text to the operator."""
        print(text)


def _ask(
    io: MenuIO,
    prompt: str,
    default: str | None = None,
    *,
    required: bool = False,
) -> str:
    """Ask for a line of text, translating back-words and Ctrl-C into :class:`_Back`."""
    while True:
        try:
            raw = io.ask(prompt, default)
        except (KeyboardInterrupt, EOFError):
            raise _Back from None
        text = (raw or "").strip()
        if not text and default is not None:
            text = str(default)
        if text.casefold() in BACK_WORDS:
            raise _Back
        if not text and required:
            io.show("a value is required (b to go back)")
            continue
        return text


def _choose(io: MenuIO, title: str, options: Sequence[str]) -> int:
    """Pick one option; going back or an out-of-range answer raises :class:`_Back`."""
    if not options:
        io.show(terminal_safe_line(f"{title}: nothing to choose from"))
        raise _Back
    try:
        index = io.choose(
            terminal_safe_line(title), [terminal_safe_line(option) for option in options]
        )
    except (KeyboardInterrupt, EOFError):
        raise _Back from None
    if not isinstance(index, int) or isinstance(index, bool):
        raise _Back
    if not 0 <= index < len(options):
        raise _Back
    return index


def _confirm(io: MenuIO, prompt: str) -> bool:
    """Ask for confirmation; Ctrl-C at a confirmation means "no"."""
    try:
        return bool(io.confirm(prompt))
    except (KeyboardInterrupt, EOFError):
        return False


# ------------------------------------------------------------------- utilities


def _svc(name: str) -> Any:
    """Import a service module lazily (so fakes installed in ``sys.modules`` win)."""
    return importlib.import_module(f"{_ROOT}.services.{name}")


def _make_report(
    title: str,
    columns: list[str],
    rows: list[dict],
    *,
    summary: dict | None = None,
    meta: dict | None = None,
) -> dict:
    """Build the shared report shape, preferring ``jira_cli.report.make_report``."""
    try:
        report_mod = importlib.import_module(f"{_ROOT}.report")
        return report_mod.make_report(
            title, columns, rows, summary=summary, meta=meta
        )
    except (ImportError, AttributeError):
        # The core helper only normalises this dict; a local build keeps the menu
        # usable when jira_cli.report is unavailable.
        return {
            "title": title,
            "columns": columns,
            "rows": rows,
            "summary": summary or {},
            "meta": meta or {},
        }


def _plain_report(report: dict) -> str:
    """Minimal plain-text rendering, used when ``cli.output`` is unavailable."""
    columns = list(report.get("columns") or [])
    lines = [terminal_safe_line(report.get("title") or "report")]
    if columns:
        lines.append(" | ".join(terminal_safe_line(c) for c in columns))
    for row in report.get("rows") or []:
        lines.append(" | ".join(terminal_safe_line(row.get(c)) for c in columns))
    summary = report.get("summary") or {}
    if summary:
        lines.append(
            "summary: "
            + ", ".join(terminal_safe_line(f"{k}={v}") for k, v in summary.items())
        )
    if not report.get("rows"):
        lines.append("(no rows)")
    return "\n".join(lines)


def _render_report(report: dict) -> str:
    """Render a report through ``cli.output``, falling back to plain text."""
    try:
        output = importlib.import_module(f"{_ROOT}.cli.output")
        return output.render_report(report, "table")
    except (ImportError, AttributeError):
        return _plain_report(report)


def _render_rows(columns: list[str], rows: list[dict]) -> str:
    """Render a bare table through ``cli.output``, falling back to plain text."""
    try:
        output = importlib.import_module(f"{_ROOT}.cli.output")
        return output.render_table(columns, rows)
    except (ImportError, AttributeError):
        return _plain_report({"title": "", "columns": columns, "rows": rows})


_ISSUE_COLUMNS = ["key", "type", "status", "assignee", "priority", "due", "summary"]


def _issues_report(title: str, issues: list[dict], *, jql: str | None = None) -> dict:
    """Turn a list of normalised issues into the shared report shape."""
    rows = [{column: issue.get(column) for column in _ISSUE_COLUMNS} for issue in issues]
    meta = {"jql": [jql]} if jql else None
    return _make_report(
        title, _ISSUE_COLUMNS, rows, summary={"issues": len(rows)}, meta=meta
    )


def _issue_text(issue: dict) -> str:
    """Human-readable detail block for a single normalised issue.

    Every value comes from the server, so one-line fields are folded with
    :func:`terminal_safe_line` and multi-line bodies keep only their newlines.
    """
    line = terminal_safe_line
    lines = [
        f"{line(issue.get('key'))}  {line(issue.get('summary'))}",
        f"  url        {line(issue.get('url'))}",
    ]
    for label in ("type", "status", "priority", "assignee", "reporter", "due", "sprint", "epic", "parent"):
        value = issue.get(label)
        if value:
            lines.append(f"  {label:<10} {line(value)}")
    labels = issue.get("labels") or []
    if labels:
        lines.append("  labels     " + ", ".join(line(item) for item in labels))
    if issue.get("description"):
        lines.append("")
        lines.append(terminal_safe(issue["description"]))
    for comment in issue.get("comments") or []:
        lines.append(
            f"\n  -- {line(comment.get('author'))} ({line(comment.get('created'))})"
            f"\n  {terminal_safe(comment.get('body'))}"
        )
    return "\n".join(lines)


def _as_path(value: Any, fallback: str) -> Path:
    """Coerce a settings path (which may be missing) into a :class:`Path`."""
    return Path(str(value)) if value else Path(fallback)


def _split_list(text: str) -> list[str] | None:
    """Parse ``"A, B"`` into ``["A", "B"]``; empty input means "no filter"."""
    items = [part.strip() for part in text.split(",") if part.strip()]
    return items or None


# ------------------------------------------------------------------ session


@dataclass
class _Session:
    """Everything a handler needs, plus the little state the menu remembers."""

    client: Any
    settings: Any
    io: MenuIO
    last_report: dict | None = None
    last_key: str | None = None
    last_board: int | None = None
    recent_projects: list[str] = field(default_factory=list)

    def say(self, text: object) -> None:
        """Show text that may carry server data, with control characters removed."""
        self.io.show(terminal_safe(text))


# ------------------------------------------------------------- shared prompts


def _preview_and_confirm(session: _Session, title: str, lines: Sequence[str]) -> bool:
    """Show exactly what is about to be written and ask ``y/N``.

    Each line is folded to one terminal-safe line, so a value containing a
    newline or an escape sequence cannot fake or hide a preview line.
    """
    body = "\n".join(
        [terminal_safe_line(title), *(f"  {terminal_safe_line(line)}" for line in lines)]
    )
    session.io.show(body)
    if _confirm(session.io, "Proceed?"):
        return True
    session.io.show("cancelled; nothing was written")
    return False


def _ask_people(session: _Session, prompt: str = "Users (alice,bob or team:backend)") -> list[str]:
    """Resolve a people expression through ``services.people.resolve_people``."""
    spec = _ask(session.io, prompt, required=True)
    people = _svc("people")
    teams = people.load_teams(_as_path(getattr(session.settings, "teams_file", None), "teams.yaml"))
    resolved = people.resolve_people(spec, teams)
    if not resolved:
        session.io.show("error: that expression matched nobody")
        raise _Back
    return list(resolved)


def _ask_projects(session: _Session, prompt: str = "Projects (blank = all)") -> list[str] | None:
    default = getattr(session.settings, "default_project", None)
    return _split_list(_ask(session.io, prompt, default=default or ""))


def _pick_project(session: _Session) -> str:
    """Choose a project key from the server's project list."""
    projects = _svc("projects").list_projects(session.client)
    if not projects:
        return _ask(session.io, "Project key", required=True).upper()
    default = getattr(session.settings, "default_project", None)
    ordered = sorted(projects, key=lambda p: (p.get("key") != default, str(p.get("key"))))
    options = [f"{p.get('key')} - {p.get('name')}" for p in ordered]
    return str(ordered[_choose(session.io, "Project", options)].get("key"))


def _pick_board(session: _Session, project_key: str) -> dict | None:
    """Reuse the project's board when there is one, otherwise offer to create it."""
    boards = _svc("boards")
    existing = boards.board_for_project(session.client, project_key)
    if existing:
        session.say(f"board: {existing.get('name')} (id {existing.get('id')}) - reused")
        session.last_board = existing.get("id")
        return existing
    if not _preview_and_confirm(
        session, "No board for this project. Create one?", [f"ensure kanban board for {project_key}"]
    ):
        return None
    result = boards.ensure_board(session.client, project_key)
    board = result.get("board") if isinstance(result, dict) else None
    if board:
        session.last_board = board.get("id")
        session.say(f"board ready: {board.get('name')} (id {board.get('id')})")
    return board


def _pick_assignee(session: _Session, project_key: str) -> str | None:
    """Pick an assignee from the project's assignable users."""
    users = _svc("people").assignable_users(session.client, project_key)
    options = ["(unassigned)"] + [
        f"{u.get('username')} - {u.get('display_name')}" for u in users
    ]
    index = _choose(session.io, "Assignee", options)
    return None if index == 0 else str(users[index - 1].get("username"))


def _pick_priority(session: _Session) -> str | None:
    priorities = _svc("projects").list_priorities(session.client)
    options = ["(server default)"] + [str(p) for p in priorities]
    index = _choose(session.io, "Priority", options)
    return None if index == 0 else str(priorities[index - 1])


def _pick_sprint(session: _Session, board_id: int | None) -> int | None:
    """Pick a sprint on ``board_id``; ``None`` means "leave it in the backlog"."""
    if board_id is None:
        return None
    sprints = _svc("boards").list_sprints(session.client, board_id)
    if not sprints:
        return None
    options = ["(backlog / no sprint)"] + [
        f"{s.get('name')} [{s.get('state')}]" for s in sprints
    ]
    index = _choose(session.io, "Sprint", options)
    return None if index == 0 else sprints[index - 1].get("id")


def _ask_board_id(session: _Session) -> int:
    raw = _ask(
        session.io,
        "Board id",
        default=str(session.last_board) if session.last_board else None,
        required=True,
    )
    if not raw.isdigit():
        session.io.show("error: board id must be a number")
        raise _Back
    session.last_board = int(raw)
    return int(raw)


def _ask_issue_key(session: _Session) -> str:
    key = _ask(session.io, "Issue key", default=session.last_key, required=True).upper()
    session.last_key = key
    return key


# ------------------------------------------------------- reports and exporting


def _present_report(
    session: _Session, report: dict, refresh: Callable[[], dict] | None = None
) -> None:
    """Show a report, then offer export / refresh / back until the operator leaves."""
    while True:
        session.last_report = report
        session.io.show(_render_report(report))
        try:
            answer = _ask(
                session.io, "e)xport md/csv/json, r)efresh, b)ack", default="b"
            ).casefold()
        except _Back:
            return
        if answer in ("e", "export"):
            _export_report(session, report)
        elif answer in ("r", "refresh"):
            if refresh is None:
                session.io.show("this report cannot be refreshed")
            else:
                report = refresh()
        else:
            session.io.show("type e, r or b")


def _export_report(session: _Session, report: dict) -> None:
    """Write a report to disk after a preview and a confirmation."""
    try:
        fmt = _ask(session.io, "Format (md/csv/json)", default="md").casefold()
        if fmt not in _EXPORT_FORMATS:
            session.say(f"error: unknown format {fmt!r}")
            return
        stem = _svc("export").safe_filename(str(report.get("title") or "report").lower())
        path = _ask(session.io, "Output file", default=f"{stem}.{fmt}")
    except _Back:
        return
    if not _preview_and_confirm(
        session,
        "Export report",
        [
            f"title  {report.get('title')}",
            f"rows   {len(report.get('rows') or [])}",
            f"file   {path}",
            f"format {fmt}",
        ],
    ):
        return
    written = _svc("export").write_report(report, Path(path), fmt)
    session.say(f"written: {written}")


# ------------------------------------------------------------------- handlers


def _menu_my_issues(session: _Session) -> None:
    include_done = _confirm(session.io, "Include resolved issues?")

    def build() -> dict:
        issues = _svc("issues").my_issues(session.client, include_done=include_done)
        return _issues_report("My issues", issues)

    _present_report(session, build(), refresh=build)


def _menu_team_dashboard(session: _Session) -> None:
    usernames = _ask_people(session)
    projects = _ask_projects(session)
    since = _ask(session.io, "Since", default="7d")

    def build() -> dict:
        return _svc("reports").team_dashboard(
            session.client, usernames, projects=projects, since=since
        )

    _present_report(session, build(), refresh=build)


def _menu_person_activity(session: _Session) -> None:
    username = _ask(session.io, "Username", required=True)
    since = _ask(session.io, "Since", default="7d")
    projects = _ask_projects(session)

    def build() -> dict:
        return _svc("reports").person_activity(
            session.client, username, since=since, projects=projects
        )

    _present_report(session, build(), refresh=build)


def _menu_problems(session: _Session) -> None:
    index = _choose(
        session.io,
        "Problems",
        ["Stale issues", "Overdue issues", "Incomplete issues", "Unassigned issues"],
    )
    reports = _svc("reports")
    if index == 0:
        days_text = _ask(session.io, "Not updated for how many days", default="7")
        if not days_text.isdigit():
            session.io.show("error: days must be a number")
            return
        projects = _ask_projects(session)

        def build() -> dict:
            return reports.stale_issues(session.client, days=int(days_text), projects=projects)

    elif index == 1:
        projects = _ask_projects(session)

        def build() -> dict:
            return reports.overdue_issues(session.client, projects=projects)

    elif index == 2:
        projects = _ask_projects(session)

        def build() -> dict:
            return reports.incomplete_issues(session.client, projects=projects)

    else:
        projects = _ask_projects(session, "Projects (required)")
        if not projects:
            session.io.show("error: unassigned needs at least one project")
            return

        def build() -> dict:
            return reports.unassigned_issues(session.client, projects)

    _present_report(session, build(), refresh=build)


def _menu_workload(session: _Session) -> None:
    index = _choose(session.io, "Workload and worklogs", ["Workload", "Worklog report"])
    usernames = _ask_people(session)
    if index == 0:
        projects = _ask_projects(session)

        def build() -> dict:
            return _svc("reports").workload(session.client, usernames, projects=projects)

    else:
        since = _ask(session.io, "Since", default="7d")
        until = _ask(session.io, "Until (blank = today)") or None
        projects = _ask_projects(session)

        def build() -> dict:
            return _svc("worklog").worklog_report(
                session.client, usernames, since=since, until=until, projects=projects
            )

    _present_report(session, build(), refresh=build)


def _menu_sprint_report(session: _Session) -> None:
    index = _choose(session.io, "Sprint", ["Sprint report", "Velocity"])
    board_id = _ask_board_id(session)
    sprint_report = _svc("sprint_report")
    if index == 0:
        sprint_text = _ask(session.io, "Sprint id (blank = active)")
        sprint_id = int(sprint_text) if sprint_text.isdigit() else None

        def build() -> dict:
            return sprint_report.sprint_report(session.client, board_id, sprint_id)

    else:
        last_text = _ask(session.io, "How many sprints", default="5")
        last = int(last_text) if last_text.isdigit() else 5

        def build() -> dict:
            return sprint_report.velocity(session.client, board_id, last=last)

    _present_report(session, build(), refresh=build)


def _menu_find_issue(session: _Session) -> None:
    query = _ask(session.io, "Issue key, JQL, or saved filter name", required=True)
    issues_mod = _svc("issues")
    if _looks_like_key(query):
        session.last_key = query.upper()
        session.io.show(_issue_text(issues_mod.get_issue(session.client, query.upper())))
        return
    jql = _resolve_jql(session, query)

    def build() -> dict:
        return _issues_report(f"Search: {jql}", issues_mod.search_issues(session.client, jql), jql=jql)

    _present_report(session, build(), refresh=build)


def _looks_like_key(text: str) -> bool:
    """``PROJ-12`` is a key; anything else is JQL or a saved filter name."""
    head, _, tail = text.strip().partition("-")
    return bool(head) and bool(tail) and head.replace("_", "").isalnum() and tail.isdigit()


def _resolve_jql(session: _Session, query: str) -> str:
    """A saved filter name resolves to its JQL; everything else is JQL already."""
    if " " in query:
        return query
    filters_path = _as_path(getattr(session.settings, "filters_file", None), "filters.yaml")
    try:
        saved = _svc("filters").list_filters(filters_path)
    except JiraError:
        return query
    entry = (saved or {}).get(query)
    if isinstance(entry, dict) and entry.get("jql"):
        return str(entry["jql"])
    if isinstance(entry, str):
        return entry
    return query


def _menu_create_issue(session: _Session) -> None:
    """Guided issue creation: project, board, type, text, people, dates, sprint."""
    issues_mod = _svc("issues")
    project_key = _pick_project(session)
    board = _pick_board(session, project_key)
    types = _svc("projects").list_issue_types(session.client, project_key)
    issue_type = str(types[_choose(session.io, "Issue type", [str(t) for t in types])])
    summary = _ask(session.io, "Summary", required=True)
    description = _ask(session.io, "Description (blank = none)") or None
    assignee = _pick_assignee(session, project_key)
    priority = _pick_priority(session)
    estimate = _ask(session.io, "Original estimate (e.g. 3h, blank = none)") or None
    due = _ask(session.io, "Due date (YYYY-MM-DD or 7d, blank = none)") or None
    epic_key = _ask(session.io, "Epic key (blank = none)") or None
    parent_key = _ask(session.io, "Parent key for a sub-task (blank = none)") or None
    sprint_id = _pick_sprint(session, board.get("id") if board else None)

    spec = issues_mod.IssueSpec(
        project=project_key,
        summary=summary,
        issue_type=issue_type,
        description=description,
        assignee=assignee,
        priority=priority,
        due_date=due,
        original_estimate=estimate,
        epic_key=epic_key,
        parent_key=parent_key,
        sprint_id=sprint_id,
    )
    preview = [
        f"project   {project_key}",
        f"type      {issue_type}",
        f"summary   {summary}",
        f"assignee  {assignee or '(unassigned)'}",
        f"priority  {priority or '(server default)'}",
        f"estimate  {estimate or '-'}",
        f"due       {due or '-'}",
        f"epic      {epic_key or '-'}",
        f"parent    {parent_key or '-'}",
        f"sprint    {sprint_id if sprint_id is not None else '-'}",
    ]
    if not _preview_and_confirm(session, "Create issue", preview):
        return
    created = issues_mod.create_issue(session.client, spec)
    session.last_key = created.get("key")
    session.say(f"created {created.get('key')}  {created.get('url')}")
    for warning in created.get("warnings") or []:
        session.say(f"warning: {warning}")


_ISSUE_ACTIONS = [
    "Show",
    "Assign",
    "Move status",
    "Comment",
    "Log work",
    "Set estimate",
    "Edit fields",
    "Link to another issue",
]


def _menu_issue_actions(session: _Session) -> None:
    key = _ask_issue_key(session)
    index = _choose(session.io, f"Actions for {key}", _ISSUE_ACTIONS)
    issues_mod = _svc("issues")
    if index == 0:
        session.io.show(_issue_text(issues_mod.get_issue(session.client, key)))
    elif index == 1:
        _action_assign(session, key)
    elif index == 2:
        _action_transition(session, key)
    elif index == 3:
        _action_comment(session, key)
    elif index == 4:
        _action_worklog(session, key)
    elif index == 5:
        _action_estimate(session, key)
    elif index == 6:
        _action_edit(session, key)
    else:
        _action_link(session, key)


def _action_assign(session: _Session, key: str) -> None:
    issue = _svc("issues").get_issue(session.client, key, comments=False)
    username = _pick_assignee(session, str(issue.get("project") or ""))
    if not _preview_and_confirm(
        session, f"Assign {key}", [f"assignee -> {username or '(unassigned)'}"]
    ):
        return
    _svc("issues").assign(session.client, key, username)
    session.say(f"{key} assigned to {username or '(unassigned)'}")


def _action_transition(session: _Session, key: str) -> None:
    issues_mod = _svc("issues")
    transitions = issues_mod.list_transitions(session.client, key)
    if not transitions:
        session.io.show("error: no transitions available")
        return
    options = [f"{t.get('name')} -> {t.get('to')}" for t in transitions]
    target = transitions[_choose(session.io, f"Move {key}", options)]
    comment = _ask(session.io, "Comment (blank = none)") or None
    lines = [
        f"transition {target.get('name')} -> {target.get('to')}",
        f"comment    {comment or '-'}",
    ]
    if not _preview_and_confirm(session, f"Move {key}", lines):
        return
    issues_mod.transition(session.client, key, str(target.get("name")), comment)
    session.say(f"{key} moved to {target.get('to')}")


def _action_comment(session: _Session, key: str) -> None:
    body = _ask(session.io, "Comment", required=True)
    if not _preview_and_confirm(session, f"Comment on {key}", [body]):
        return
    _svc("issues").add_comment(session.client, key, body)
    session.say(f"comment added to {key}")


def _action_worklog(session: _Session, key: str) -> None:
    time_spent = _ask(session.io, "Time spent (e.g. 2h 30m)", required=True)
    started = _ask(session.io, "Started (YYYY-MM-DD, blank = now)") or None
    comment = _ask(session.io, "Comment (blank = none)") or None
    lines = [
        f"time    {time_spent}",
        f"started {started or 'now'}",
        f"comment {comment or '-'}",
    ]
    if not _preview_and_confirm(session, f"Log work on {key}", lines):
        return
    _svc("worklog").add_worklog(session.client, key, time_spent, started, comment)
    session.say(f"{time_spent} logged on {key}")


def _action_estimate(session: _Session, key: str) -> None:
    original = _ask(session.io, "Original estimate (blank = leave)") or None
    remaining = _ask(session.io, "Remaining estimate (blank = leave)") or None
    if original is None and remaining is None:
        session.io.show("nothing to change")
        return
    lines = [
        f"original  {original or '(unchanged)'}",
        f"remaining {remaining or '(unchanged)'}",
    ]
    if not _preview_and_confirm(session, f"Set estimates on {key}", lines):
        return
    _svc("worklog").set_estimate(session.client, key, original, remaining)
    session.say(f"estimates updated on {key}")


_EDITABLE_FIELDS = [
    "summary",
    "description",
    "priority",
    "due_date",
    "original_estimate",
    "labels",
    "story_points",
]


def _action_edit(session: _Session, key: str) -> None:
    field_name = _EDITABLE_FIELDS[_choose(session.io, f"Edit {key}", _EDITABLE_FIELDS)]
    if field_name == "labels":
        _edit_labels(session, key)
        return
    raw = _ask(session.io, f"New value for {field_name}", required=True)
    value: Any = raw
    if field_name == "story_points":
        try:
            value = float(raw)
        except ValueError:
            session.say("error: story points must be a number")
            return
    if not _preview_and_confirm(session, f"Edit {key}", [f"{field_name} -> {value}"]):
        return
    _svc("issues").update_fields(session.client, key, {field_name: value})
    session.say(f"{key} updated")


def _format_labels(labels: Sequence[object]) -> str:
    """``["a", "b"]`` -> ``[a, b]``; an empty list -> ``(none)``."""
    return f"[{', '.join(str(item) for item in labels)}]" if labels else "(none)"


def _edit_labels(session: _Session, key: str) -> None:
    """Replace the labels of ``key``; clearing them needs the explicit ``-`` answer.

    An answer like ``,`` parses to no labels at all.  It is rejected rather than
    treated as "clear", because silently wiping every label is too easy to do
    by accident.
    """
    raw = _ask(session.io, f"New labels (a,b; {_CLEAR_LABELS} = clear all)", required=True)
    if raw == _CLEAR_LABELS:
        new_labels: list[str] = []
    else:
        new_labels = _split_list(raw) or []
        if not new_labels:
            session.say(f"error: no labels parsed; answer {_CLEAR_LABELS} to clear all labels")
            return
    issues_mod = _svc("issues")
    current = issues_mod.get_issue(session.client, key, comments=False).get("labels") or []
    after = _format_labels(new_labels) if new_labels else "(cleared)"
    line = f"labels: {_format_labels(current)} -> {after}"
    if not _preview_and_confirm(session, f"Edit {key}", [line]):
        return
    issues_mod.update_fields(session.client, key, {"labels": new_labels})
    session.say(f"{key} updated")


def _action_link(session: _Session, key: str) -> None:
    issues_mod = _svc("issues")
    link_types = issues_mod.list_link_types(session.client)
    names = [str(t.get("name") if isinstance(t, dict) else t) for t in link_types] or ["Relates"]
    link_type = names[_choose(session.io, "Link type", names)]
    other = _ask(session.io, "Other issue key", required=True).upper()
    if not _preview_and_confirm(session, "Link issues", [f"{key} {link_type} {other}"]):
        return
    issues_mod.link_issues(session.client, key, other, link_type)
    session.say(f"{key} {link_type} {other}")


def _menu_plan_files(session: _Session) -> None:
    index = _choose(
        session.io, "Plan files", ["New plan file", "Validate", "Preview", "Apply (dry run first)"]
    )
    plan_mod = _svc("plan")
    path = Path(_ask(session.io, "Plan file", default="plan.yaml", required=True))
    if index == 0:
        _write_plan_template(session, plan_mod, path)
        return

    plan = plan_mod.load_plan(path)
    if index == 1:
        result = plan_mod.validate_plan(session.client, plan)
        session.io.show("plan is valid" if result.get("ok") else "plan has errors")
        for problem in list(result.get("errors") or []) + list(result.get("warnings") or []):
            session.say(f"  {problem.get('ref')}: {problem.get('field')} {problem.get('message')}")
        return
    if index == 2:
        _present_report(session, plan_mod.preview_plan(plan, session.settings))
        return

    dry = plan_mod.apply_plan(session.client, plan, dry_run=True)
    session.io.show(_render_rows(["ref", "key", "url"], list(dry.get("created") or [])))
    lines = [
        f"would create {len(dry.get('created') or [])}",
        f"would skip   {len(dry.get('skipped') or [])}",
        f"failed       {len(dry.get('failed') or [])}",
    ]
    if not _preview_and_confirm(session, f"Apply plan {path} for real", lines):
        return
    result = plan_mod.apply_plan(session.client, plan, dry_run=False)
    session.say(
        f"created {len(result.get('created') or [])}, "
        f"skipped {len(result.get('skipped') or [])}, "
        f"failed {len(result.get('failed') or [])}"
    )


def _write_plan_template(session: _Session, plan_mod: Any, path: Path) -> None:
    """Write a new plan template; an existing file is kept unless the operator says so.

    The CLI refuses to overwrite outright.  The menu asks instead, defaulting to
    No, and the preview names the overwrite so it cannot be missed.
    """
    overwrite = path.exists()
    if overwrite and not _confirm(
        session.io, f"{terminal_safe_line(path)} already exists. Overwrite it?"
    ):
        session.say(f"kept existing {path}; nothing was written")
        return
    template = plan_mod.new_plan_template()
    lines = [f"file {path}"] + (["OVERWRITE existing file"] if overwrite else [])
    if not _preview_and_confirm(session, "Write plan template", lines):
        return
    # "x" refuses a file that appeared since the check; "w" only after an explicit yes.
    with path.open("w" if overwrite else "x", encoding="utf-8") as handle:
        handle.write(template)
    session.say(f"written: {path}")


def _menu_teams(session: _Session) -> None:
    people = _svc("people")
    teams_path = _as_path(getattr(session.settings, "teams_file", None), "teams.yaml")
    index = _choose(
        session.io, "Teams", ["List", "Show", "Create", "Edit members", "Remove"]
    )
    teams = people.load_teams(teams_path)
    if index == 0:
        rows = [
            {"team": name, "members": ", ".join(data.get("members") or [])}
            for name, data in sorted(teams.items())
        ]
        session.io.show(_render_rows(["team", "members"], rows) if rows else "no teams yet")
        return
    if index in (1, 3, 4):
        if not teams:
            session.io.show("no teams yet")
            return
        names = sorted(teams)
        name = names[_choose(session.io, "Team", names)]
    else:
        name = _ask(session.io, "New team name", required=True)
        if name in teams:
            session.say(f"error: team {name} already exists")
            return

    if index == 1:
        data = teams[name]
        session.say(
            f"{name}\n  members  {', '.join(data.get('members') or []) or '-'}"
            f"\n  projects {', '.join(data.get('projects') or []) or '-'}"
            f"\n  boards   {', '.join(str(b) for b in data.get('boards') or []) or '-'}"
        )
        return
    if index == 4:
        if not _preview_and_confirm(session, "Remove team", [name]):
            return
        people.remove_team(teams_path, name)
        session.say(f"team {name} removed")
        return

    current = teams.get(name, {})
    default_members = ",".join(current.get("members") or []) or None
    default_projects = ",".join(current.get("projects") or [])
    default_boards = ",".join(str(b) for b in current.get("boards") or [])
    members = _split_list(
        _ask(session.io, "Members (alice,bob)", default=default_members, required=True)
    ) or []
    projects = _split_list(_ask(session.io, "Projects (blank = none)", default=default_projects))
    boards_text = _split_list(_ask(session.io, "Board ids (blank = none)", default=default_boards))
    boards = [int(b) for b in boards_text if b.isdigit()] if boards_text else None
    lines = [
        f"members  {', '.join(members)}",
        f"projects {', '.join(projects or []) or '-'}",
        f"boards   {boards or '-'}",
    ]
    if not _preview_and_confirm(session, f"Save team {name}", lines):
        return
    people.save_team(teams_path, name, members, projects, boards)
    session.say(f"team {name} saved")


def _menu_projects_boards(session: _Session) -> None:
    index = _choose(
        session.io,
        "Projects and boards",
        ["List projects", "Create project", "Ensure board", "Create sprint"],
    )
    if index == 0:
        projects = _svc("projects").list_projects(session.client)
        session.io.show(_render_rows(["key", "name", "lead", "type"], projects))
        return
    if index == 1:
        key = _ask(session.io, "Project key", required=True).upper()
        name = _ask(session.io, "Project name", required=True)
        lead = _ask(session.io, "Project lead username", required=True)
        template_options = ["kanban", "scrum", "basic"]
        template = template_options[_choose(session.io, "Template", template_options)]
        if not _preview_and_confirm(
            session, "Create project", [f"key {key}", f"name {name}", f"lead {lead}", f"template {template}"]
        ):
            return
        created = _svc("projects").create_project(session.client, key, name, lead, template)
        session.say(f"created project {created.get('key')}  {created.get('url')}")
        return
    if index == 2:
        project_key = _pick_project(session)
        board_types = ["kanban", "scrum"]
        board_type = board_types[_choose(session.io, "Board type", board_types)]
        lines = [f"project {project_key}", f"type {board_type}", "an existing board is reused"]
        if not _preview_and_confirm(session, "Ensure board", lines):
            return
        result = _svc("boards").ensure_board(session.client, project_key, board_type)
        board = result.get("board") or {}
        session.last_board = board.get("id")
        session.say(
            f"board {board.get('name')} (id {board.get('id')}) "
            f"{'created' if result.get('created') else 'reused'}"
        )
        return

    board_id = _ask_board_id(session)
    name = _ask(session.io, "Sprint name", required=True)
    start = _ask(session.io, "Start (blank = none)") or None
    end = _ask(session.io, "End (blank = none)") or None
    goal = _ask(session.io, "Goal (blank = none)") or None
    lines = [f"board {board_id}", f"name {name}", f"start {start or '-'}", f"end {end or '-'}"]
    if not _preview_and_confirm(session, "Create sprint", lines):
        return
    sprint = _svc("boards").create_sprint(session.client, board_id, name, start, end, goal)
    session.say(f"created sprint {sprint.get('name')} (id {sprint.get('id')})")


def _menu_filters(session: _Session) -> None:
    filters_mod = _svc("filters")
    path = _as_path(getattr(session.settings, "filters_file", None), "filters.yaml")
    index = _choose(
        session.io, "Saved filters", ["List", "Run", "Save", "Delete", "Server favourites"]
    )
    if index == 0:
        saved = filters_mod.list_filters(path)
        rows = [
            {"name": name, "jql": (data.get("jql") if isinstance(data, dict) else data)}
            for name, data in sorted((saved or {}).items())
        ]
        session.io.show(_render_rows(["name", "jql"], rows) if rows else "no saved filters")
        return
    if index == 1:
        saved = filters_mod.list_filters(path)
        names = sorted(saved or {})
        if not names:
            session.io.show("no saved filters")
            return
        name = names[_choose(session.io, "Filter", names)]
        jql = _resolve_jql(session, name)

        def build() -> dict:
            return _issues_report(
                f"Filter: {name}", _svc("issues").search_issues(session.client, jql), jql=jql
            )

        _present_report(session, build(), refresh=build)
        return
    if index == 2:
        name = _ask(session.io, "Filter name", required=True)
        jql = _ask(session.io, "JQL", required=True)
        description = _ask(session.io, "Description (blank = none)") or None
        if not _preview_and_confirm(session, "Save filter", [f"name {name}", f"jql  {jql}"]):
            return
        filters_mod.save_filter(path, name, jql, description)
        session.say(f"filter {name} saved")
        return
    if index == 3:
        saved = filters_mod.list_filters(path)
        names = sorted(saved or {})
        if not names:
            session.io.show("no saved filters")
            return
        name = names[_choose(session.io, "Filter", names)]
        if not _preview_and_confirm(session, "Delete filter", [name]):
            return
        filters_mod.delete_filter(path, name)
        session.say(f"filter {name} deleted")
        return

    server = filters_mod.server_filters(session.client)
    session.io.show(_render_rows(["name", "jql"], server) if server else "no server favourites")


def _menu_export_last(session: _Session) -> None:
    if not session.last_report:
        session.io.show("no report to export yet; run a report first")
        return
    _export_report(session, session.last_report)


def _menu_settings(session: _Session) -> None:
    try:
        config = importlib.import_module(f"{_ROOT}.config")
        described = config.describe_settings(session.settings)
    except (ImportError, AttributeError):
        described = {"base_url": getattr(session.settings, "base_url", "?")}
    session.say(
        "\n".join(f"  {key:<18} {value}" for key, value in sorted(described.items()))
    )
    if not _confirm(session.io, "Check the connection now?"):
        return
    me = session.client.myself()
    session.say(f"connected as {me.get('name')} ({me.get('displayName')})")


# ------------------------------------------------------------------ main menu

_MAIN_MENU: list[tuple[str, str, Callable[[_Session], None]]] = [
    ("1", "My issues", _menu_my_issues),
    ("2", "Team dashboard", _menu_team_dashboard),
    ("3", "Person activity", _menu_person_activity),
    ("4", "Problems (stale / overdue / incomplete / unassigned)", _menu_problems),
    ("5", "Workload and worklogs", _menu_workload),
    ("6", "Sprint report and velocity", _menu_sprint_report),
    ("7", "Find issue (key, JQL or saved filter)", _menu_find_issue),
    ("8", "Create issue (guided)", _menu_create_issue),
    ("9", "Issue actions", _menu_issue_actions),
    ("10", "Plan files", _menu_plan_files),
    ("11", "Teams", _menu_teams),
    ("12", "Projects and boards", _menu_projects_boards),
    ("13", "Saved filters", _menu_filters),
    ("14", "Export last report", _menu_export_last),
    ("15", "Settings / doctor", _menu_settings),
]

_MAIN_ACTIONS = {key: handler for key, _label, handler in _MAIN_MENU}


def main_menu_text() -> str:
    """The main menu as it is shown to the operator."""
    lines = ["", "jira-cli - main menu", ""]
    lines += [f"  {key:>2}. {label}" for key, label, _handler in _MAIN_MENU]
    lines += ["", "   q. quit"]
    return "\n".join(lines)


def _main_choice(io: MenuIO) -> str:
    """Show the main menu and read one choice; Ctrl-C here means quit."""
    io.show(main_menu_text())
    try:
        raw = io.ask("Choice", None)
    except (KeyboardInterrupt, EOFError):
        return "q"
    return (raw or "").strip().casefold()


def _error_line(exc: Exception) -> str:
    """One redacted line describing ``exc`` for an operator, never a traceback."""
    if isinstance(exc, JiraError):
        return f"error: {redact(exc)}"
    if isinstance(exc, OSError):
        reason = exc.strerror or type(exc).__name__
        where = f": {exc.filename}" if exc.filename is not None else ""
        return f"error: {redact(f'{reason}{where}')}"
    return f"error: unexpected {type(exc).__name__}: {redact(exc)}"


def _run_action(session: _Session, handler: Callable[[_Session], None]) -> None:
    """Run one menu action.  Any error ends the action, never the session.

    A failing action (a Jira error, an unwritable file, a bug in a service)
    prints one redacted ``error:`` line and returns to the main menu, so the
    operator keeps the session and the last report.  Ctrl-C is not an error:
    it is a ``BaseException`` and only backs out one level.
    """
    try:
        handler(session)
    except _Back:
        return
    except KeyboardInterrupt:
        # Ctrl-C outside a prompt (during a service call) also means "one level up".
        session.io.show("")
    except Exception as exc:  # noqa: BLE001 - the menu must survive any action failure
        session.say(_error_line(exc))


def run(client: Any, settings: Any, io: MenuIO | None = None) -> int:
    """Run the interactive menu until the operator quits.  Returns the exit code."""
    session = _Session(client=client, settings=settings, io=io or MenuIO())
    while True:
        choice = _main_choice(session.io)
        if choice in QUIT_WORDS:
            return 0
        if not choice or choice in BACK_WORDS:
            continue
        handler = _MAIN_ACTIONS.get(choice)
        if handler is None:
            session.say(f"invalid choice: {choice}")
            continue
        _run_action(session, handler)
