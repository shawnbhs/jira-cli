"""Menu section 11 - teams: list, show, create, edit and remove roster entries.

A team is ``{"members": [...], "projects": [...], "boards": [...]}`` in the
roster file named by ``settings.teams_file``.  Nothing here is typed from memory:

* members are picked with the shared people picker (names and roles shown);
* projects with the shared project picker, boards from a numbered list of the
  boards of those projects - an answer that is not a listed number is refused
  and asked again, never silently dropped;
* when editing, every field offers *keep*, *change* and (projects/boards) an
  explicit *none* that clears it; the preview prints ``(none)`` for an empty
  list so "cleared" can never be mistaken for a value;
* the team name is checked against the roster's name rule before anything else.

The roster is only written after a preview and an explicit ``y``.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from .menu import _ask, _choose, _preview_and_confirm, _render_rows, _Session, _svc

_ACTIONS = ("List", "Show", "Create", "Edit", "Remove")
NONE_TEXT = "(none)"
_NAME_RULE = "1-64 letters, digits, '_', '.' or '-'"
#: Same rule as ``services.people.TEAM_NAME_RE``; used only if the service lacks it.
_FALLBACK_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# What the operator wants to do with one field of a team.
_KEEP, _CHANGE, _CLEAR = "keep", "change", "clear"


def run(session: _Session) -> None:
    """Main-menu handler for *Teams*."""
    people = _svc("people")
    path = _teams_path(session)
    index = _choose(session.io, "Teams", list(_ACTIONS))
    teams = people.load_teams(path)
    if index == 0:
        _list(session, teams)
        return
    if index == 2:
        _create(session, people, path, teams)
        return
    if not teams:
        session.io.show("no teams yet")
        return
    names = sorted(teams)
    name = names[_choose(session.io, "Team", names)]
    if index == 1:
        _show(session, name, teams[name])
    elif index == 3:
        _edit(session, people, path, name, teams[name])
    else:
        _remove(session, people, path, name)


def _teams_path(session: _Session) -> Path:
    value = getattr(session.settings, "teams_file", None)
    return Path(str(value)) if value else Path("teams.yaml")


def _people_ui() -> Any:
    """The shared people/project pickers (imported lazily so tests can swap them)."""
    return importlib.import_module(".menu_people", __package__)


def _fmt(values: Any) -> str:
    """Comma-joined values, or ``(none)`` for an empty list."""
    return ", ".join(str(value) for value in values or []) or NONE_TEXT


# ------------------------------------------------------------------ read-only views


def _list(session: _Session, teams: dict) -> None:
    if not teams:
        session.io.show("no teams yet")
        return
    rows = [
        {
            "team": name,
            "members": _fmt(data.get("members")),
            "projects": _fmt(data.get("projects")),
            "boards": _fmt(data.get("boards")),
        }
        for name, data in sorted(teams.items())
    ]
    session.io.show(_render_rows(["team", "members", "projects", "boards"], rows))


def _show(session: _Session, name: str, data: dict) -> None:
    session.say(
        f"{name}\n  members  {_fmt(data.get('members'))}"
        f"\n  projects {_fmt(data.get('projects'))}"
        f"\n  boards   {_fmt(data.get('boards'))}"
    )


def _remove(session: _Session, people: Any, path: Path, name: str) -> None:
    if not _preview_and_confirm(session, "Remove team", [name]):
        return
    people.remove_team(path, name)
    session.say(f"team {name} removed")


# ------------------------------------------------------------------ create / edit


def _ask_new_name(session: _Session, people: Any, teams: dict) -> str:
    """Ask for an unused, valid team name; a bad one is explained and asked again."""
    pattern = getattr(people, "TEAM_NAME_RE", _FALLBACK_NAME_RE)
    while True:
        name = _ask(session.io, f"New team name ({_NAME_RULE})", required=True)
        if not pattern.match(name):
            session.say(f"team name must be {_NAME_RULE}; try again (b to go back)")
        elif name in teams:
            session.say(f"team {name} already exists; try another name (b to go back)")
        else:
            return name


def _field_action(session: _Session, title: str, current: list | None, *, clearable: bool) -> str:
    """Ask what to do with one field: keep it, change it, or (if allowed) clear it.

    ``current is None`` means a new team: there is nothing to keep.
    """
    actions = [] if current is None else [_KEEP]
    labels = [] if current is None else [f"Keep: {_fmt(current)}"]
    actions.append(_CHANGE)
    labels.append("Choose" if current is None else "Change")
    if clearable:
        actions.append(_CLEAR)
        labels.append("None" if current is None else "None (clear)")
    return actions[_choose(session.io, title, labels)]


def _ask_members(session: _Session, name: str, current: list | None) -> list[str]:
    if current and _field_action(session, f"Members of {name}", current, clearable=False) == _KEEP:
        return list(current)
    return list(_people_ui().ask_people(session, f"Members of {name}"))


def _ask_projects(session: _Session, name: str, current: list | None) -> list[str]:
    action = _field_action(session, f"Projects of {name}", current, clearable=True)
    if action == _KEEP:
        return list(current or [])
    if action == _CLEAR:
        return []
    return list(_people_ui().ask_projects(session, f"Projects of {name}", allow_all=False) or [])


def _ask_boards(session: _Session, name: str, projects: list[str], current: list | None) -> list[dict]:
    """Return the chosen boards as ``{"id", "name", ...}`` rows."""
    action = _field_action(session, f"Boards of {name}", current, clearable=True)
    if action == _KEEP:
        return [{"id": board_id} for board_id in current or []]
    if action == _CLEAR:
        return []
    return _pick_boards(session, projects)


def _boards_for(session: _Session, projects: list[str]) -> list[dict]:
    """Boards of ``projects`` (all boards when no project is set), de-duplicated by id."""
    boards_svc = _svc("boards")
    found: dict[Any, dict] = {}
    for key in projects or [None]:
        for board in boards_svc.list_boards(session.client, key):
            found.setdefault(board.get("id"), board)
    return list(found.values())


def _pick_boards(session: _Session, projects: list[str]) -> list[dict]:
    boards = _boards_for(session, projects)
    if not boards:
        session.say(f"no boards found for {_fmt(projects)}; the team gets no boards")
        return []
    rows = [
        {
            "#": number,
            "id": board.get("id"),
            "name": board.get("name"),
            "type": board.get("type"),
            "project": board.get("project"),
        }
        for number, board in enumerate(boards, start=1)
    ]
    session.io.show(_render_rows(["#", "id", "name", "type", "project"], rows))
    while True:
        answer = _ask(session.io, "Boards (numbers like 1,3 or 2-4, all, none)", required=True)
        try:
            return [boards[index] for index in parse_numbers(answer, len(boards))]
        except ValueError as exc:
            session.say(f"{exc}; try again (b to go back)")


def parse_numbers(text: str, count: int) -> list[int]:
    """Parse ``"1,3"``, ``"2-4"``, ``"all"`` or ``"none"`` into zero-based indexes.

    Raises ``ValueError`` naming the first token that is not a listed number, so
    nothing the operator typed is silently dropped.
    """
    answer = text.strip().casefold()
    if answer in ("all", "*"):
        return list(range(count))
    if answer in ("none", "-"):
        return []
    chosen: list[int] = []
    for token in re.split(r"[,\s]+", answer):
        if not token:
            continue
        first, dash, last = token.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise ValueError(f"{token!r} is not a number from 1 to {count}")
        low, high = int(first), int(last) if dash else int(first)
        if not 1 <= low <= high <= count:
            raise ValueError(f"{token!r} is outside 1 to {count}")
        chosen.extend(index for index in range(low - 1, high) if index not in chosen)
    if not chosen:
        raise ValueError("no board numbers given")
    return chosen


def _board_text(board: dict) -> str:
    return f"{board.get('id')} {board.get('name')}" if board.get("name") else str(board.get("id"))


def _field_line(label: str, before: str | None, after: str) -> str:
    if before is None or before == after:
        return f"{label:<9}{after}"
    return f"{label:<9}{before} -> {after}"


def _save(
    session: _Session,
    people: Any,
    path: Path,
    name: str,
    before: dict | None,
    team: tuple[list[str], list[str], list[dict]],
) -> None:
    members, projects, boards = team

    def was(field: str) -> str | None:
        return None if before is None else _fmt(before.get(field))

    lines = [
        _field_line("members", was("members"), _fmt(members)),
        _field_line("projects", was("projects"), _fmt(projects)),
        _field_line("boards", was("boards"), ", ".join(_board_text(b) for b in boards) or NONE_TEXT),
    ]
    if not _preview_and_confirm(session, f"Save team {name}", lines):
        return
    board_ids = [int(board["id"]) for board in boards]
    people.save_team(path, name, members, projects, board_ids)
    session.say(f"team {name} saved")


def _create(session: _Session, people: Any, path: Path, teams: dict) -> None:
    name = _ask_new_name(session, people, teams)
    members = _ask_members(session, name, None)
    projects = _ask_projects(session, name, None)
    boards = _ask_boards(session, name, projects, None)
    _save(session, people, path, name, None, (members, projects, boards))


def _edit(session: _Session, people: Any, path: Path, name: str, current: dict) -> None:
    members = _ask_members(session, name, list(current.get("members") or []))
    projects = _ask_projects(session, name, list(current.get("projects") or []))
    boards = _ask_boards(session, name, projects, list(current.get("boards") or []))
    _save(session, people, path, name, current, (members, projects, boards))
