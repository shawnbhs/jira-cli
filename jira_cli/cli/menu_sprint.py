"""Menu section: sprint report and velocity.

Nothing here is typed from memory.  The operator reaches a board through a
numbered list (the boards of one project, every scrum board on the server, or
the board used last), and a sprint through a numbered list of that board's
sprints.  Only scrum boards have sprints, so picking a kanban board says so and
returns to the board list instead of running an empty or failing report.

Every prompt accepts ``b`` to go back exactly one step; a bad answer is
re-asked at the same prompt with a short reason, so nothing already chosen is
lost.  The reports themselves come from ``services.sprint_report`` exactly as
before (``sprint_report`` and ``velocity``).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from typing import Any, TypeVar

from ..textsafe import terminal_safe_line
from .menu import _ask, _Back, _choose, _pick_project, _present_report, _Session, _svc

__all__ = ["run", "pick_scrum_board", "pick_sprint", "ask_sprint_count", "order_sprints"]

T = TypeVar("T")

#: Closed sprints offered in the sprint list (newest first).
CLOSED_SPRINTS_SHOWN = 10
#: Bounds of the velocity "how many sprints" answer.
MIN_SPRINTS = 1
MAX_SPRINTS = 20
DEFAULT_SPRINTS = 5

_REPORT_KINDS = ["Sprint report", "Velocity"]
_SOURCE_PROJECT = "Boards of one project"
_SOURCE_ALL_SCRUM = "All scrum boards"
_ISO_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}")
#: ASCII digits only: ``str.isdigit`` also accepts characters ``int`` rejects.
_WHOLE_NUMBER = re.compile(r"[0-9]+")


# --------------------------------------------------------------- entry point


def run(session: _Session) -> None:
    """Main-menu handler: choose a report, a board and a sprint, then show it.

    Raises:
        _Back: when the operator backs out of the first choice.
    """
    while True:
        kind = _choose(session.io, "Sprint report and velocity", _REPORT_KINDS)
        then = _sprint_report_step if kind == 0 else _velocity_step
        try:
            board, build = pick_scrum_board(session, then)
        except _Back:
            continue
        session.last_board = board.get("id")
        _present_report(session, build(), refresh=build)
        return


def _sprint_report_step(session: _Session, board: dict) -> Callable[[], dict]:
    sprint = pick_sprint(session, board)
    board_id, sprint_id = int(board["id"]), int(sprint["id"])
    return lambda: _svc("sprint_report").sprint_report(session.client, board_id, sprint_id)


def _velocity_step(session: _Session, board: dict) -> Callable[[], dict]:
    last = ask_sprint_count(session)
    board_id = int(board["id"])
    return lambda: _svc("sprint_report").velocity(session.client, board_id, last=last)


# --------------------------------------------------------------- board pick


def pick_scrum_board(session: _Session, then: Callable[[_Session, dict], T]) -> tuple[dict, T]:
    """Walk source -> (project) -> board list, then run ``then(session, board)``.

    ``then`` is the next step (sprint pick, sprint count).  When it raises
    :class:`_Back` the operator lands on the board list again, so ``b`` always
    goes back exactly one prompt.

    Returns:
        ``(board, then's result)`` where ``board`` is ``{"id", "name", "type", ...}``
        and is always a scrum board.

    Raises:
        _Back: when the operator backs out of the first (source) choice.
    """
    while True:
        options = [_SOURCE_PROJECT, _SOURCE_ALL_SCRUM]
        if session.last_board:
            options.append(f"Last board used (id {session.last_board})")
        source = options[_choose(session.io, "Which board?", options)]
        try:
            if source == _SOURCE_PROJECT:
                return _board_of_project(session, then)
            if source == _SOURCE_ALL_SCRUM:
                boards = _svc("boards").list_boards(session.client, board_type="scrum")
                return _board_from_list(session, boards, "Scrum boards", then)
            return _last_board(session, then)
        except _Back:
            continue


def _board_of_project(session: _Session, then: Callable[[_Session, dict], T]) -> tuple[dict, T]:
    while True:
        project = _ask_one_project(session)
        boards = _svc("boards").list_boards(session.client, project)
        if not boards:
            session.say(f"project {project} has no boards; pick another project")
            continue
        try:
            return _board_from_list(session, boards, f"Boards of {project}", then)
        except _Back:
            continue


def _board_from_list(
    session: _Session, boards: list[dict], title: str, then: Callable[[_Session, dict], T]
) -> tuple[dict, T]:
    if not boards:
        session.say(f"{title}: no boards found")
        raise _Back
    while True:
        board = boards[_choose(session.io, title, [_board_label(b) for b in boards])]
        if not _has_sprints(session, board):
            continue
        try:
            return board, then(session, board)
        except _Back:
            continue


def _last_board(session: _Session, then: Callable[[_Session, dict], T]) -> tuple[dict, T]:
    board = _svc("boards").get_board(session.client, session.last_board)
    if not _has_sprints(session, board):
        raise _Back
    return board, then(session, board)


def _has_sprints(session: _Session, board: dict) -> bool:
    """True for a scrum board; otherwise explain why there is nothing to report."""
    kind = _board_kind(session, board)
    if kind == "scrum":
        return True
    session.say(
        f"board {terminal_safe_line(board.get('name'))} (id {board.get('id')}) "
        f"is {terminal_safe_line(kind) or 'of unknown type'}: "
        "no sprints; use Boards overview instead"
    )
    return False


def _board_kind(session: _Session, board: dict) -> str:
    kind = str(board.get("type") or "").strip().lower()
    if not kind and board.get("id") is not None:
        # Some payloads omit the type; the board itself always carries it.
        kind = str(_svc("boards").get_board(session.client, board["id"]).get("type") or "")
    return kind.strip().lower()


def _board_label(board: dict) -> str:
    return f"{board.get('name')}  [{board.get('type') or '?'}]  (id {board.get('id')})"


def _ask_one_project(session: _Session) -> str:
    """One project key from the shared numbered project picker.

    Falls back to ``menu._pick_project`` when the shared people module is not
    installed.  Raises :class:`_Back` when the operator backs out.
    """
    people_menu = _menu_people()
    if people_menu is None:
        return _pick_project(session)
    while True:
        keys = people_menu.ask_projects(session, "Project", allow_all=False)
        if keys and len(keys) == 1:
            return str(keys[0])
        session.say("pick exactly one project (b to go back)")


def _menu_people() -> Any | None:
    name = f"{__package__}.menu_people"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None


# --------------------------------------------------------------- sprint pick


def order_sprints(sprints: list[dict]) -> list[dict]:
    """Active sprints, then future ones, then the newest closed sprints.

    Server order is kept inside the active and future groups.  Closed sprints are
    ordered by id, highest first: ids grow with creation and, unlike dates, are
    present on every row (GreenHopper rows are only partly enriched with dates).
    At most :data:`CLOSED_SPRINTS_SHOWN` closed sprints are kept.
    """
    rows = [s for s in sprints if isinstance(s, dict) and s.get("id") is not None]

    def in_state(state: str) -> list[dict]:
        return [s for s in rows if str(s.get("state") or "").lower() == state]

    closed = sorted(in_state("closed"), key=lambda s: int(s["id"]), reverse=True)
    return in_state("active") + in_state("future") + closed[:CLOSED_SPRINTS_SHOWN]


def pick_sprint(session: _Session, board: dict) -> dict:
    """Numbered pick of one sprint on ``board``; Enter takes the active sprint.

    With several active sprints there is no default and the operator must choose.

    Raises:
        _Back: on ``b``, or when the board has no sprints at all.
    """
    sprints = order_sprints(_svc("boards").list_sprints(session.client, int(board["id"]), state=""))
    if not sprints:
        session.say(
            f"board {terminal_safe_line(board.get('name'))} (id {board.get('id')}) has no sprints yet"
        )
        raise _Back
    active = [i for i, s in enumerate(sprints) if str(s.get("state") or "").lower() == "active"]
    lines = [f"Sprints on {terminal_safe_line(board.get('name'))} (id {board.get('id')}):"]
    lines += [f"  {n:>2}. {_sprint_label(s)}" for n, s in enumerate(sprints, start=1)]
    session.say("\n".join(lines))
    if len(active) == 1:
        prompt, default = "Sprint number (Enter = active sprint)", str(active[0] + 1)
    elif active:
        session.say(f"{len(active)} sprints are active; choose one")
        prompt, default = "Sprint number", None
    else:
        session.say("no sprint is active; choose one")
        prompt, default = "Sprint number", None
    return sprints[_ask_number(session, prompt, len(sprints), default) - 1]


def _sprint_label(sprint: dict) -> str:
    start, end = _day(sprint.get("start")), _day(sprint.get("end"))
    dates = f"  {start} -> {end}" if start or end else ""
    return terminal_safe_line(
        f"{sprint.get('name')}  [{sprint.get('state') or '?'}]{dates}  (id {sprint.get('id')})"
    )


def _day(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if _ISO_DAY.match(text) else text


# ------------------------------------------------------------ number prompts


def ask_sprint_count(session: _Session) -> int:
    """How many recent sprints velocity covers: a whole number 1..20, re-asked."""
    return _ask_number(
        session,
        f"How many sprints ({MIN_SPRINTS}-{MAX_SPRINTS})",
        MAX_SPRINTS,
        str(DEFAULT_SPRINTS),
        what="how many sprints",
    )


def _ask_number(
    session: _Session, prompt: str, highest: int, default: str | None, *, what: str = "choice"
) -> int:
    """Ask until the answer is a whole number from 1 to ``highest``; ``b`` raises _Back."""
    while True:
        text = _ask(session.io, prompt, default=default, required=True)
        if _WHOLE_NUMBER.fullmatch(text) and 1 <= int(text) <= highest:
            return int(text)
        session.say(f"{what} must be a whole number from 1 to {highest} (b to go back)")
