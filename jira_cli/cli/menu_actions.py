"""Menu item 9 - Issue actions.

The issue key is asked once, checked against the server (a typo is re-asked,
never carried into the action list), and the operator then works through a
numbered action list until they go back.  Every value is validated at its own
prompt - durations, dates, story points, labels, link targets - so a preview
only ever shows something the server will accept.  Every write still goes
through :func:`_preview_and_confirm`.

:func:`actions_for` is public so other sections (Find issue) can jump straight
into the action list for a key they already have.
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from jira_cli import durations
from jira_cli.cli.menu import (
    _ask,
    _Back,
    _choose,
    _issue_text,
    _pick_priority,
    _preview_and_confirm,
    _Session,
    _split_list,
    _svc,
)
from jira_cli.errors import InputError, JiraError
from jira_cli.ids import validate_issue_key
from jira_cli.textsafe import terminal_safe_line

#: How many issue keys the session remembers for "Recent issue".
RECENT_LIMIT = 10

_BROWSE_RE = re.compile(r"/browse/([A-Za-z][A-Za-z0-9_]*-\d+)(?=[/?#]|$)")
_FORWARD_RE = re.compile(r"(?i)^\+?(\d+)\s*([dw])$")
_CLEAR = "-"
_TITLE_SUMMARY_WIDTH = 60


# --------------------------------------------------------------- keys / recent


def parse_issue_ref(text: str) -> str:
    """``proj-12``, `` PROJ-12 `` or ``https://host/browse/PROJ-12?x`` -> ``PROJ-12``.

    Raises:
        InputError: nothing in ``text`` is shaped like an issue key.
    """
    raw = (text or "").strip()
    match = _BROWSE_RE.search(raw)
    if match:
        return validate_issue_key(match.group(1))
    if "://" in raw or raw.startswith("/"):
        raise InputError("no issue key in that link; expected .../browse/PROJ-123")
    return validate_issue_key(raw)


def remember_key(session: _Session, key: str) -> None:
    """Make ``key`` the default issue key and the newest "recent" entry."""
    session.last_key = key
    recent = [k for k in (getattr(session, "recent_keys", None) or []) if k != key]
    session.recent_keys = [key, *recent][:RECENT_LIMIT]


def recent_keys(session: _Session) -> list[str]:
    """Issue keys used in this session, newest first."""
    recent = list(getattr(session, "recent_keys", None) or [])
    if session.last_key and session.last_key not in recent:
        recent.insert(0, session.last_key)
    return recent


def fetch_issue(session: _Session, key: str) -> dict | None:
    """GET ``key``; a 404 is reported and returns ``None``, other errors bubble."""
    try:
        return _svc("issues").get_issue(session.client, key, comments=False)
    except JiraError as exc:
        if getattr(exc, "status", None) != 404:
            raise
    session.say(f"error: {key} does not exist, or you cannot see it")
    return None


def ask_issue(session: _Session, prompt: str = "Issue key or browse URL") -> tuple[str, dict]:
    """Ask for an issue until it parses *and* exists; returns ``(key, issue)``."""
    while True:
        raw = _ask(session.io, prompt, default=session.last_key, required=True)
        try:
            key = parse_issue_ref(raw)
        except InputError as exc:
            session.say(f"error: {exc}")
            continue
        issue = fetch_issue(session, key)
        if issue is not None:
            remember_key(session, key)
            return key, issue


# ------------------------------------------------------------- value parsers


def _ask_parsed(
    session: _Session,
    prompt: str,
    parse: Callable[[str], Any],
    *,
    required: bool = False,
) -> Any:
    """Ask until ``parse`` accepts the answer.  Blank (when optional) -> ``None``."""
    while True:
        text = _ask(session.io, prompt, required=required)
        if not text:
            return None
        try:
            return parse(text)
        except InputError as exc:
            session.say(f"error: {exc}")


def parse_started(text: str, today: date | None = None) -> str:
    """A worklog start date: ``YYYY-MM-DD``, ``today``, ``yesterday``, ``3d`` ago."""
    base = today or date.today()
    resolved = durations.resolve_date(text, today=base)
    if resolved > base.isoformat():
        raise InputError(f"started {resolved} is in the future")
    return resolved


def parse_due_date(text: str, today: date | None = None) -> str:
    """A due date counted *forwards*: ``YYYY-MM-DD``, ``today``, ``tomorrow``, ``3d``, ``2w``.

    ``-`` returns ``""``, meaning "clear the due date".
    """
    raw = (text or "").strip().lower()
    base = today or date.today()
    if raw == _CLEAR:
        return ""
    if raw == "today":
        return base.isoformat()
    if raw == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    forward = _FORWARD_RE.match(raw)
    if forward:
        amount = int(forward.group(1)) * (7 if forward.group(2).lower() == "w" else 1)
        return (base + timedelta(days=amount)).isoformat()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return durations.resolve_date(raw, today=base)
    raise InputError(f"cannot read due date {text!r}; use YYYY-MM-DD, today, tomorrow, 3d or 2w")


def parse_story_points(text: str) -> float:
    """A finite, non-negative number (``nan``/``inf`` would break the JSON body)."""
    try:
        value = float(text)
    except ValueError:
        raise InputError("story points must be a number") from None
    if not math.isfinite(value) or value < 0:
        raise InputError("story points must be a finite number of 0 or more")
    return value


def parse_labels(text: str) -> list[str]:
    """``"a, b"`` -> ``["a", "b"]``; labels cannot contain spaces."""
    labels: list[str] = []
    for label in _split_list(text) or []:
        if re.search(r"\s", label):
            raise InputError(f"label {label!r} must not contain spaces")
        if label not in labels:
            labels.append(label)
    if not labels:
        raise InputError("no labels given; type them like a,b")
    return labels


def parse_numbers(text: str, count: int) -> list[int]:
    """``"1,3"``, ``"2-4"`` or ``"all"`` -> zero-based indexes into ``count`` items."""
    raw = (text or "").strip().lower()
    if raw in ("all", "*"):
        return list(range(count))
    picked: list[int] = []
    for part in (p.strip() for p in raw.split(",")):
        bounds = part.split("-", 1)
        if not part or not all(b.strip().isdigit() for b in bounds):
            raise InputError(f"{part!r} is not a number or a range like 2-4")
        low, high = int(bounds[0]), int(bounds[-1])
        if not 1 <= low <= high <= count:
            raise InputError(f"{part!r} is out of range 1-{count}")
        picked.extend(i - 1 for i in range(low, high + 1) if i - 1 not in picked)
    return picked


def _fmt_labels(labels: list[str]) -> str:
    return f"[{', '.join(labels)}]" if labels else "(none)"


# ----------------------------------------------------------------- actions


@dataclass
class IssueTarget:
    """The issue an action works on, as last read from the server."""

    key: str
    issue: dict
    board: dict | None


def _show(session: _Session, target: IssueTarget) -> None:
    session.io.show(_issue_text(_svc("issues").get_issue(session.client, target.key)))


def _assign(session: _Session, target: IssueTarget) -> None:
    people = importlib.import_module("jira_cli.cli.menu_people")
    current = target.issue.get("assignee")
    username = people.ask_person(
        session, "Assignee", project=target.issue.get("project") or None, allow_none=True
    )
    if username == current:
        session.say(f"{target.key} is already assigned to {current or '(nobody)'}; nothing to change")
        return
    line = f"assignee {current or '(unassigned)'} -> {username or '(unassigned)'}"
    if not _preview_and_confirm(session, f"Assign {target.key}", [line]):
        return
    _svc("issues").assign(session.client, target.key, username)
    session.say(f"{target.key} assigned to {username or '(unassigned)'}")


def _transition(session: _Session, target: IssueTarget) -> None:
    issues = _svc("issues")
    status = target.issue.get("status")
    transitions = issues.list_transitions(session.client, target.key)
    if not transitions:
        session.say(f"no transitions are available on {target.key} (status {status})")
        return
    options = [
        f"{t.get('name')} -> {t.get('to')}" + ("  (stays in current status)" if t.get("to") == status else "")
        for t in transitions
    ]
    chosen = transitions[_choose(session.io, f"Move {target.key} (now: {status})", options)]
    comment = _ask(session.io, "Comment (blank = none)") or None
    lines = [
        f"status     {status} -> {chosen.get('to')}",
        f"transition {chosen.get('name')}",
        f"comment    {comment or '-'}",
    ]
    if not _preview_and_confirm(session, f"Move {target.key}", lines):
        return
    # The id is unambiguous even when two transitions share a name.
    issues.transition(session.client, target.key, str(chosen.get("id")), comment)
    session.say(f"{target.key} moved to {chosen.get('to')}")


def _comment(session: _Session, target: IssueTarget) -> None:
    body = _ask(session.io, "Comment", required=True)
    if not _preview_and_confirm(session, f"Comment on {target.key}", [body]):
        return
    _svc("issues").add_comment(session.client, target.key, body)
    session.say(f"comment added to {target.key}")


def _worklog(session: _Session, target: IssueTarget) -> None:
    spent = _ask_parsed(session, "Time spent (e.g. 2h 30m)", durations.to_jira, required=True)
    started = _ask_parsed(
        session, "Started (YYYY-MM-DD, today, yesterday, 3d = 3 days ago; blank = now)", parse_started
    )
    comment = _ask(session.io, "Comment (blank = none)") or None
    lines = [f"time    {spent}", f"started {started or 'now'}", f"comment {comment or '-'}"]
    if not _preview_and_confirm(session, f"Log work on {target.key}", lines):
        return
    _svc("worklog").add_worklog(session.client, target.key, spent, started, comment)
    session.say(f"{spent} logged on {target.key}")


def _estimate(session: _Session, target: IssueTarget) -> None:
    now_original = durations.format_duration(target.issue.get("estimate_seconds"))
    now_remaining = durations.format_duration(target.issue.get("remaining_seconds"))
    original = _ask_parsed(
        session, f"Original estimate (now {now_original}; e.g. 3h, blank = leave)", durations.to_jira
    )
    remaining = _ask_parsed(
        session, f"Remaining estimate (now {now_remaining}; e.g. 1d, blank = leave)", durations.to_jira
    )
    if original is None and remaining is None:
        session.say("nothing to change")
        return
    lines = [
        f"original  {now_original} -> {original or '(unchanged)'}",
        f"remaining {now_remaining} -> {remaining or '(unchanged)'}",
    ]
    if not _preview_and_confirm(session, f"Set estimates on {target.key}", lines):
        return
    _svc("worklog").set_estimate(session.client, target.key, original, remaining)
    session.say(f"estimates updated on {target.key}")


def _ask_new_priority(session: _Session, current: Any) -> tuple[bool, Any]:
    priority = _pick_priority(session)
    if priority is None:
        session.say("(server default) cannot be set on an existing issue; nothing changed")
        return False, None
    return priority != current, priority


def _edit_value(session: _Session, field: str, current: Any) -> tuple[bool, Any]:
    """Ask for the new value of ``field``; returns ``(changed, value)``."""
    if field == "priority":
        return _ask_new_priority(session, current)
    if field == "summary":
        value: Any = _ask(session.io, "New summary", required=True)
    elif field == "description":
        text = _ask(session.io, f"New description (one line; {_CLEAR} = clear)", required=True)
        value = None if text == _CLEAR else text
    elif field == "due_date":
        parsed = _ask_parsed(
            session,
            f"Due date (YYYY-MM-DD, today, tomorrow, 3d / 2w from today; {_CLEAR} = clear)",
            parse_due_date,
            required=True,
        )
        value = parsed or None
    elif field == "original_estimate":
        value = _ask_parsed(session, "Original estimate (e.g. 3h)", durations.to_jira, required=True)
    else:
        value = _ask_parsed(session, "Story points (e.g. 3 or 0.5)", parse_story_points, required=True)
    return value != current, value


_EDITABLE = [
    ("summary", "summary"),
    ("description", "description"),
    ("priority", "priority"),
    ("due_date", "due"),
    ("original_estimate", "estimate_seconds"),
    ("story_points", "story_points"),
]


def _edit(session: _Session, target: IssueTarget) -> None:
    def shown(issue_field: str) -> str:
        value = target.issue.get(issue_field)
        if issue_field == "estimate_seconds":
            return durations.format_duration(value)
        text = "-" if value in (None, "") else str(value)
        return text if len(text) <= 40 else text[:39] + "…"

    options = [f"{name:<18} ({shown(issue_field)})" for name, issue_field in _EDITABLE]
    name, issue_field = _EDITABLE[_choose(session.io, f"Edit {target.key}", options)]
    current = target.issue.get(issue_field)
    changed, value = _edit_value(session, name, current)
    if not changed:
        session.say(f"{name} is already {shown(issue_field)}; nothing to change")
        return
    after = "(cleared)" if value is None else value
    if not _preview_and_confirm(session, f"Edit {target.key}", [f"{name}: {shown(issue_field)} -> {after}"]):
        return
    _svc("issues").update_fields(session.client, target.key, {name: value})
    session.say(f"{target.key} updated")


def _labels_after(session: _Session, current: list[str]) -> list[str]:
    mode = _choose(
        session.io,
        f"Labels (now {_fmt_labels(current)})",
        ["Add labels", "Remove labels", "Replace all labels"],
    )
    if mode == 0:
        added = _ask_parsed(session, "Labels to add (a,b)", parse_labels, required=True)
        return current + [label for label in added if label not in current]
    if mode == 1:
        if not current:
            session.say("this issue has no labels to remove")
            raise _Back
        session.io.show(
            "\n".join(f"  {n:>2}. {terminal_safe_line(label)}" for n, label in enumerate(current, 1))
        )
        gone = _ask_parsed(
            session,
            "Labels to remove (numbers like 1,3 or 1-2; all)",
            lambda text: parse_numbers(text, len(current)),
            required=True,
        )
        return [label for i, label in enumerate(current) if i not in gone]
    text = _ask(session.io, f"New labels (a,b; {_CLEAR} = clear all)", required=True)
    return [] if text == _CLEAR else _replace_labels(session, text)


def _replace_labels(session: _Session, text: str) -> list[str]:
    while True:
        try:
            return parse_labels(text)
        except InputError as exc:
            session.say(f"error: {exc}")
        text = _ask(session.io, f"New labels (a,b; {_CLEAR} = clear all)", required=True)
        if text == _CLEAR:
            return []


def _labels(session: _Session, target: IssueTarget) -> None:
    current = list(target.issue.get("labels") or [])
    after = _labels_after(session, current)
    if after == current:
        session.say(f"labels are already {_fmt_labels(current)}; nothing to change")
        return
    line = f"labels: {_fmt_labels(current)} -> {_fmt_labels(after) if after else '(cleared)'}"
    if not _preview_and_confirm(session, f"Edit {target.key}", [line]):
        return
    _svc("issues").update_fields(session.client, target.key, {"labels": after})
    session.say(f"{target.key} labels now {_fmt_labels(after)}")


def _ask_link_target(session: _Session, key: str) -> str:
    while True:
        raw = _ask(session.io, "Other issue key or browse URL", required=True)
        try:
            other = parse_issue_ref(raw)
        except InputError as exc:
            session.say(f"error: {exc}")
            continue
        if other == key:
            session.say("error: an issue cannot be linked to itself")
            continue
        if fetch_issue(session, other) is not None:
            return other


def _link(session: _Session, target: IssueTarget) -> None:
    issues = _svc("issues")
    link_types = [t for t in issues.list_link_types(session.client) if t.get("name")]
    if not link_types:
        session.say("this Jira has no issue link types")
        return
    options = [f"{t['name']}  ({t.get('outward') or '-'} / {t.get('inward') or '-'})" for t in link_types]
    chosen = link_types[_choose(session.io, "Link type", options)]
    other = _ask_link_target(session, target.key)
    lines = [f"{target.key} {chosen.get('outward') or chosen['name']} {other}", f"type {chosen['name']}"]
    if not _preview_and_confirm(session, "Link issues", lines):
        return
    issues.link_issues(session.client, target.key, other, str(chosen["name"]))
    session.say(f"{target.key} {chosen.get('outward') or chosen['name']} {other}")


def _sprint(session: _Session, target: IssueTarget) -> None:
    board = target.board or {}
    current = target.issue.get("sprint")
    mode = _choose(
        session.io,
        f"Sprint (now {current or 'backlog'}; board {board.get('name')})",
        ["Add to a sprint", "Remove from sprint (move to backlog)"],
    )
    boards = _svc("boards")
    if mode == 1:
        if not current:
            session.say(f"{target.key} is not in a sprint")
            return
        if not _preview_and_confirm(session, f"Sprint for {target.key}", [f"sprint: {current} -> (backlog)"]):
            return
        boards.move_to_backlog(session.client, [target.key])
        session.say(f"{target.key} moved to the backlog")
        return
    sprints = boards.list_sprints(session.client, board.get("id"), "active,future")
    if not sprints:
        session.say(f"board {board.get('name')} has no active or future sprint")
        return
    options = [f"{s.get('name')} [{s.get('state')}]" for s in sprints]
    sprint = sprints[_choose(session.io, "Pick a sprint", options)]
    if sprint.get("name") == current:
        session.say(f"{target.key} is already in {current}; nothing to change")
        return
    line = f"sprint: {current or '(backlog)'} -> {sprint.get('name')} (id {sprint.get('id')})"
    if not _preview_and_confirm(session, f"Sprint for {target.key}", [line]):
        return
    boards.add_to_sprint(session.client, sprint.get("id"), [target.key])
    session.say(f"{target.key} added to {sprint.get('name')}")


_ACTIONS: list[tuple[str, Callable[[_Session, IssueTarget], None]]] = [
    ("Show", _show),
    ("Assign", _assign),
    ("Move status", _transition),
    ("Comment", _comment),
    ("Log work", _worklog),
    ("Set estimate", _estimate),
    ("Edit fields", _edit),
    ("Labels (add / remove / replace)", _labels),
    ("Link to another issue", _link),
]
_SPRINT_ACTION = ("Sprint (add / remove)", _sprint)


def _project_board(session: _Session, project: str | None) -> dict | None:
    """The project's board, or ``None`` when there is none or it cannot be read."""
    if not project:
        return None
    try:
        return _svc("boards").board_for_project(session.client, project)
    except JiraError:
        # Without a board the sprint action is simply not offered.
        return None


def _actions_for_board(board: dict | None) -> list[tuple[str, Callable[[_Session, IssueTarget], None]]]:
    """Sprints exist only on scrum boards; kanban issues never see the sprint action."""
    is_scrum = str((board or {}).get("type") or "").lower() == "scrum"
    return [*_ACTIONS, _SPRINT_ACTION] if is_scrum else list(_ACTIONS)


def _title(key: str, issue: dict) -> str:
    summary = str(issue.get("summary") or "")
    if len(summary) > _TITLE_SUMMARY_WIDTH:
        summary = summary[: _TITLE_SUMMARY_WIDTH - 1] + "…"
    return f"Actions for {key} [{issue.get('status') or '?'}] {summary}".rstrip()


def actions_for(session: _Session, key: str, *, issue: dict | None = None) -> None:
    """Run the numbered action list for ``key`` until the operator goes back."""
    if issue is None:
        issue = fetch_issue(session, key)
        if issue is None:
            return
    remember_key(session, key)
    board = _project_board(session, issue.get("project"))
    actions = _actions_for_board(board)
    while True:
        try:
            index = _choose(session.io, _title(key, issue), [label for label, _ in actions])
        except _Back:
            return
        try:
            actions[index][1](session, IssueTarget(key=key, issue=issue, board=board))
        except _Back:
            pass  # backing out of one action returns to the action list
        issue = fetch_issue(session, key) or issue


def run(session: _Session) -> None:
    """Main-menu handler for item 9."""
    key, issue = ask_issue(session)
    actions_for(session, key, issue=issue)
