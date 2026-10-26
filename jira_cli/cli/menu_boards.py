"""Menu section: projects and boards (main-menu item 12) and the boards overview.

Two entry points, both ``(session) -> None``:

* :func:`run` -- the "Projects and boards" submenu: boards overview, list
  projects, create project, ensure board, create sprint.
* :func:`run_overview` -- "Boards overview": how many issues sit on each board
  and in each column, then one board column by column with title, assignee and
  reporter for every issue.

Kanban and scrum read differently (see :mod:`jira_cli.services.board_overview`):
a kanban board counts every issue in its columns, a scrum board counts only the
active sprint in its columns and keeps the backlog separate.  The renderers in
:mod:`jira_cli.cli.board_view` already present both shapes, so this module only
drives the screens.

Input mistakes are caught at their prompt and re-asked with a short reason;
``b`` backs out one step; Jira errors from a write bubble to the menu runner.
"""

from __future__ import annotations

import importlib
import re
from datetime import date
from typing import Any

from ..errors import JiraError
from .board_view import column_choices, render_board_header, render_board_summaries, render_column
from .menu import _ask, _Back, _choose, _error_line, _preview_and_confirm, _render_rows, _Session, _svc
from .output import terminal_width

__all__ = ["run", "run_overview"]

#: Same rule as ``services.projects.PROJECT_KEY_RE``; checked here so a bad key
#: is re-asked at its prompt instead of failing after the preview.
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: A single column can hold hundreds of Done issues; cap what one screen prints.
_MAX_ROWS_ONE_COLUMN = 100
_MAX_ROWS_ALL_COLUMNS = 30

_UNASSIGNED = "(unassigned)"


def _people_ui() -> Any:
    """The shared people/project pickers, imported lazily (tests swap the module)."""
    return importlib.import_module("jira_cli.cli.menu_people")


def _width() -> int:
    return terminal_width()


# =========================================================== boards overview


def run_overview(session: _Session) -> None:
    """Boards overview: scope -> board list with counts -> one board's columns.

    ``b`` at the board list returns to the scope question; ``b`` at the scope
    question leaves the overview.
    """
    while True:
        projects = _ask_scope(session)
        try:
            _board_list_screen(session, projects)
        except _Back:
            continue


def _ask_scope(session: _Session) -> list[str] | None:
    """Project keys to show boards for; ``None`` means every board."""
    session.io.show("Which boards? Pick a project, or answer 'all' for every board.")
    return _people_ui().ask_projects(session, "Boards of project", allow_all=True)


def _load_summaries(session: _Session, projects: list[str] | None) -> list[dict]:
    session.io.show("loading boards... (all boards take a few seconds)")
    service = _svc("board_overview")
    if projects is None:
        return list(service.list_board_summaries(session.client))
    rows: list[dict] = []
    seen: set[Any] = set()
    for project in projects:
        for row in service.list_board_summaries(session.client, project):
            if row.get("id") not in seen:
                seen.add(row.get("id"))
                rows.append(row)
    return rows


def _board_list_screen(session: _Session, projects: list[str] | None) -> None:
    rows = _load_summaries(session, projects)
    while True:
        session.io.show(render_board_summaries(rows, width=_width()))
        if not rows:
            raise _Back
        index = _ask_board_number(session, rows)
        if index is None:
            rows = _load_summaries(session, projects)
            continue
        try:
            _board_screen(session, rows[index])
        except _Back:
            continue


def _ask_board_number(session: _Session, rows: list[dict]) -> int | None:
    """Index into ``rows`` of the picked board, or ``None`` for refresh."""
    ids = [row.get("id") for row in rows]
    default = str(ids.index(session.last_board) + 1) if session.last_board in ids else None
    while True:
        answer = _ask(session.io, f"Board number 1-{len(rows)}, r)efresh, b)ack", default=default)
        if answer.casefold() in ("r", "refresh"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(rows):
            return int(answer) - 1
        session.io.show(f"type a board number from 1 to {len(rows)}, r or b")


# ------------------------------------------------------------- one board


def _load_board(session: _Session, board_id: int) -> dict:
    session.io.show("loading board...")
    return _svc("board_overview").board_overview(session.client, board_id)


def _board_screen(session: _Session, row: dict) -> None:
    """One board: header, numbered columns, the picked column's issues."""
    board_id = int(row["id"])
    session.last_board = board_id
    try:
        overview = _load_board(session, board_id)
    except JiraError as exc:  # one unreadable board must not end the overview
        session.say(_error_line(exc))
        raise _Back from None
    assignee: str | None = None  # username, _UNASSIGNED, or None for everyone
    show_header = True
    while True:
        view = _filter_by_assignee(overview, assignee)
        if show_header:
            session.say(_header_text(view, overview, assignee))
            show_header = False
        choices = column_choices(view)
        session.say(_choices_text(choices))
        action = _ask_board_action(session, len(choices))
        if action == "r":
            overview = _load_board(session, board_id)
            show_header = True
        elif action == "a":
            assignee = _ask_assignee_filter(session, overview, assignee)
            show_header = True
        else:
            max_rows = _MAX_ROWS_ALL_COLUMNS if action == len(choices) - 1 else _MAX_ROWS_ONE_COLUMN
            session.io.show(render_column(view, int(action), width=_width(), max_rows=max_rows))


def _header_text(view: dict, overview: dict, assignee: str | None) -> str:
    lines = [render_board_header(view, width=_width())]
    if assignee is not None:
        name = _UNASSIGNED if assignee == _UNASSIGNED else _display_name(overview, assignee)
        lines.append(f"filter: assignee = {name}  (a to change)")
        if _any_truncated(overview):
            lines.append("note: the filter covers only the issues loaded for each column")
    return "\n".join(lines)


def _choices_text(choices: list[str]) -> str:
    lines = ["", "Columns"]
    lines += [f"  {number:>2}. {label}" for number, label in enumerate(choices, start=1)]
    return "\n".join(lines)


def _ask_board_action(session: _Session, count: int) -> int | str:
    """A column index (0-based), ``"r"`` or ``"a"``; ``b`` raises :class:`_Back`."""
    while True:
        answer = _ask(session.io, f"Column 1-{count}, a)ssignee filter, r)efresh, b)ack to boards").casefold()
        if answer in ("r", "refresh"):
            return "r"
        if answer in ("a", "assignee"):
            return "a"
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        session.io.show(f"type a column number from 1 to {count}, a, r or b")


# ------------------------------------------------------- assignee filter


def _sections(overview: dict) -> list[dict]:
    """Every issue bucket of an overview: backlog, columns, unmapped."""
    found = [overview["backlog"]] if overview.get("backlog") is not None else []
    found += list(overview.get("columns") or [])
    if overview.get("unmapped") is not None:
        found.append(overview["unmapped"])
    return found


def _issues(overview: dict) -> list[dict]:
    return [issue for section in _sections(overview) for issue in section.get("issues") or []]


def _any_truncated(overview: dict) -> bool:
    return any(section.get("truncated") for section in _sections(overview))


def _assignee_key(issue: dict) -> str:
    return str(issue.get("assignee_username") or _UNASSIGNED)


def _display_name(overview: dict, username: str) -> str:
    for issue in _issues(overview):
        if issue.get("assignee_username") == username:
            return str(issue.get("assignee") or username)
    return username


def _ask_assignee_filter(session: _Session, overview: dict, current: str | None) -> str | None:
    """Numbered pick among the assignees on this board; first option clears it.

    ``b`` keeps ``current``.
    """
    counts: dict[str, int] = {}
    for issue in _issues(overview):
        key = _assignee_key(issue)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        session.io.show("no issues loaded on this board; nothing to filter")
        return current
    ordered = sorted(counts, key=lambda key: (-counts[key], key == _UNASSIGNED, key.casefold()))
    labels = [
        f"{_UNASSIGNED if key == _UNASSIGNED else _display_name(overview, key)} ({counts[key]})"
        for key in ordered
    ]
    try:
        index = _choose(session.io, "Show issues of", ["(everyone - no filter)", *labels])
    except _Back:
        return current
    return None if index == 0 else ordered[index - 1]


def _filter_section(section: dict, assignee: str) -> dict:
    issues = [issue for issue in section.get("issues") or [] if _assignee_key(issue) == assignee]
    return {**section, "issues": issues, "count": len(issues), "truncated": False}


def _filter_by_assignee(overview: dict, assignee: str | None) -> dict:
    """The overview restricted to one assignee; counts become loaded-issue counts."""
    if assignee is None:
        return overview
    columns = [_filter_section(column, assignee) for column in overview.get("columns") or []]
    backlog = overview.get("backlog")
    backlog = _filter_section(backlog, assignee) if backlog is not None else None
    unmapped = _filter_section(overview.get("unmapped") or {}, assignee)
    total = sum(c["count"] for c in columns) + unmapped["count"] + (backlog["count"] if backlog else 0)
    return {**overview, "columns": columns, "backlog": backlog, "unmapped": unmapped, "total": total}


# ===================================================== projects and boards


_ACTIONS = ["Boards overview", "List projects", "Create project", "Ensure board", "Create sprint"]


def run(session: _Session) -> None:
    """Item 12 submenu; stays open until ``b`` (which returns to the main menu)."""
    handlers = [run_overview, _list_projects, _create_project, _ensure_board, _create_sprint]
    while True:
        index = _choose(session.io, "Projects and boards", _ACTIONS)
        try:
            handlers[index](session)
        except _Back:
            continue


def _list_projects(session: _Session) -> None:
    projects = _svc("projects").list_projects(session.client)
    session.io.show(_render_rows(["key", "name", "lead", "type"], projects))


def _ask_one_project(session: _Session, title: str) -> str:
    while True:
        picked = _people_ui().ask_projects(session, title, allow_all=False) or []
        if len(picked) == 1:
            return str(picked[0])
        session.io.show("pick exactly one project")


def _ask_new_project_key(session: _Session) -> str:
    existing = {str(p.get("key")).upper() for p in _svc("projects").list_projects(session.client)}
    while True:
        key = _ask(session.io, "Project key (2-10 chars, A-Z then A-Z/0-9, e.g. PROJ)", required=True).upper()
        if not _PROJECT_KEY_RE.match(key):
            session.io.show("a key is 2-10 characters: a letter A-Z, then letters or digits")
        elif key in existing:
            session.io.show(f"project {key} already exists; choose another key")
        else:
            return key


def _create_project(session: _Session) -> None:
    key = _ask_new_project_key(session)
    name = _ask(session.io, "Project name", required=True)
    lead = _people_ui().ask_person(session, "Project lead")
    templates = ["kanban", "scrum", "basic"]
    template = templates[_choose(session.io, "Template", templates)]
    lines = [f"key      {key}", f"name     {name}", f"lead     {lead}", f"template {template}"]
    if not _preview_and_confirm(session, "Create project", lines):
        return
    created = _svc("projects").create_project(session.client, key, name, lead, template)
    session.say(f"created project {created.get('key')}  {created.get('url')}")
    for warning in created.get("warnings") or []:
        session.say(f"warning: {warning}")


def _ensure_board(session: _Session) -> None:
    project_key = _ask_one_project(session, "Board for project")
    boards = _svc("boards")
    existing = boards.board_for_project(session.client, project_key)
    if existing:
        session.last_board = existing.get("id")
        session.say(
            f"{project_key} already has a board: {existing.get('name')} "
            f"(id {existing.get('id')}, {existing.get('type') or 'unknown type'}); nothing to do"
        )
        return
    board_types = ["kanban", "scrum"]
    board_type = board_types[_choose(session.io, f"{project_key} has no board yet. Board type", board_types)]
    if not _preview_and_confirm(
        session, "Create board", [f"project {project_key}", f"type    {board_type}", "a new board is created"]
    ):
        return
    result = boards.ensure_board(session.client, project_key, board_type)
    board = result.get("board") or {}
    session.last_board = board.get("id")
    verb = "created" if result.get("created") else "reused"
    session.say(f"board {board.get('name')} (id {board.get('id')}) {verb}")


def _pick_scrum_board(session: _Session) -> dict:
    """Numbered pick among scrum boards only: kanban boards have no sprints."""
    session.io.show("loading scrum boards...")
    boards = _svc("boards").list_boards(session.client, board_type="scrum")
    if not boards:
        session.io.show("no scrum boards; kanban boards have no sprints")
        raise _Back
    boards = sorted(boards, key=lambda b: b.get("id") != session.last_board)
    options = [
        f"{b.get('name')} (id {b.get('id')}" + (f", project {b.get('project')})" if b.get("project") else ")")
        for b in boards
    ]
    board = boards[_choose(session.io, "Scrum board", options)]
    session.last_board = board.get("id")
    return board


def _ask_date(session: _Session, prompt: str, *, not_before: str | None = None) -> str | None:
    """``YYYY-MM-DD`` or ``None`` for blank; re-asks until the date is real."""
    while True:
        text = _ask(session.io, f"{prompt} (YYYY-MM-DD, blank = none)")
        if not text:
            return None
        if not _DATE_RE.match(text):
            session.io.show(f"{text!r} is not YYYY-MM-DD (example 2026-11-02)")
            continue
        try:
            date.fromisoformat(text)
        except ValueError:
            session.io.show(f"{text} is not a real calendar date")
            continue
        if not_before and text < not_before:
            session.io.show(f"the end date must not be before the start date {not_before}")
            continue
        return text


def _create_sprint(session: _Session) -> None:
    board = _pick_scrum_board(session)
    name = _ask(session.io, "Sprint name", required=True)
    start = _ask_date(session, "Start date")
    end = _ask_date(session, "End date", not_before=start)
    goal = _ask(session.io, "Goal (blank = none)") or None
    lines = [
        f"board {board.get('name')} (id {board.get('id')})",
        f"name  {name}",
        f"start {start or '-'}",
        f"end   {end or '-'}",
        f"goal  {goal or '-'}",
    ]
    if not _preview_and_confirm(session, "Create sprint", lines):
        return
    sprint = _svc("boards").create_sprint(session.client, int(board["id"]), name, start, end, goal)
    session.say(f"created sprint {sprint.get('name')} (id {sprint.get('id')})")
