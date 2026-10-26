"""Numbered people picker for the interactive menu.

Nobody remembers Jira usernames, so every "who?" prompt in the menu shows a
numbered table of the people it knows about -- display name, username, the
projects and roles they hold, and whether they administer anything -- and the
operator answers with numbers (``1,3``, ``2-5``, ``all``), an exact username or
display name, or ``team:NAME``.  ``/text`` narrows the table.

The person dicts are produced elsewhere and taken as plain data, already
sorted; this module never re-sorts them, so the numbers shown are stable::

    {"username": str, "display_name": str, "active": bool | None,
     "projects": [{"key": "PROJ", "roles": ["Developers", ...]}],
     "project_admin": ["PROJ"], "jira_admin": bool | None}

Every piece of server text goes through :func:`terminal_safe_line` before it
is measured or printed, so a display name cannot drive the terminal.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..textsafe import terminal_safe_line
from .output import COLUMN_GAP, MIN_COLUMN_WIDTH, display_width, render_table, terminal_width
from .output import truncate as truncate_cells

__all__ = ["BACK", "parse_selection", "pick_people", "render_people"]

#: Returned by :func:`pick_people` when the operator backs out.
BACK = None

Person = Mapping[str, Any]
TeamsResolver = Callable[[str], Sequence[str] | None]
Suggester = Callable[[Sequence[Person], str], Sequence[Person]]

COLUMNS = ("#", "Name", "Username", "Projects / roles", "Admin")
PROJECTS_COLUMN = "Projects / roles"
MAX_PROJECTS_SHOWN = 3
MAX_SUGGESTIONS = 5

BACK_ANSWERS = ("b", "!back")
TEAM_PREFIX = "team:"
MULTI_PROMPT = "Pick numbers (e.g. 1,3 or 2-5), /text to filter, b to go back"
SINGLE_PROMPT = "Pick a number, /text to filter, b to go back"
MULTI_HINT = "number(s), 1-3 ranges, all, /text to filter, username, b = back"
SINGLE_HINT = "one number or username, /text to filter, b = back"
FREE_TEXT_PROMPT = "Username (b to go back)"
ONLY_ONE = "pick exactly one"

_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")
_TOKEN_SPLIT_RE = re.compile(r"\s+")


# ------------------------------------------------------------------ rendering


def _safe(value: Any) -> str:
    return terminal_safe_line("" if value is None else str(value)).replace("\r", "").strip()


def _name_cell(person: Person) -> str:
    name = _safe(person.get("display_name")) or _safe(person.get("username"))
    return f"{name} (inactive)" if person.get("active") is False else name


def _projects_cell(person: Person, focus_project: str | None) -> str:
    projects = list(person.get("projects") or [])
    if focus_project:
        focus = focus_project.casefold()
        projects.sort(key=lambda project: str(project.get("key") or "").casefold() != focus)
    parts = []
    for project in projects[:MAX_PROJECTS_SHOWN]:
        key = _safe(project.get("key"))
        roles = ", ".join(_safe(role) for role in project.get("roles") or [])
        parts.append(f"{key}: {roles}" if roles else key)
    hidden = len(projects) - MAX_PROJECTS_SHOWN
    if hidden > 0:
        parts.append(f"+{hidden} more")
    return "; ".join(parts) or "-"


def _admin_cell(person: Person) -> str:
    jira_admin = person.get("jira_admin")
    if jira_admin is True:
        return "Jira admin"
    project_admin = [_safe(key) for key in person.get("project_admin") or []]
    if project_admin:
        return "admin: " + ",".join(project_admin)
    return "?" if jira_admin is None else "-"


def render_people(people: list[dict], *, focus_project: str | None = None, width: int | None = None) -> str:
    """Numbered table of ``people`` that fits in ``width`` terminal cells.

    The projects cell absorbs the squeeze (truncated with an ellipsis) so a
    row never wraps; numbering is 1-based in the order given.
    """
    limit = width if width and width > 0 else terminal_width()
    rows = [
        {
            "#": str(number),
            "Name": _name_cell(person),
            "Username": _safe(person.get("username")),
            PROJECTS_COLUMN: _projects_cell(person, focus_project),
            "Admin": _admin_cell(person),
        }
        for number, person in enumerate(people, start=1)
    ]
    fixed = sum(
        max([display_width(column)] + [display_width(row[column]) for row in rows])
        for column in COLUMNS
        if column != PROJECTS_COLUMN
    )
    budget = max(MIN_COLUMN_WIDTH, limit - fixed - len(COLUMN_GAP) * (len(COLUMNS) - 1))
    for row in rows:
        row[PROJECTS_COLUMN] = truncate_cells(row[PROJECTS_COLUMN], budget)
    return render_table(COLUMNS, rows, max_width=limit)


# ------------------------------------------------------------------- parsing


def _comma_parts(text: str) -> list[str]:
    """Non-empty, stripped comma-separated parts of ``text``."""
    return [part.strip() for part in text.split(",") if part.strip()]


def _by_name(people: Sequence[Person]) -> dict[str, str]:
    """Casefolded username and display name -> username (usernames win)."""
    index: dict[str, str] = {}
    for person in people:
        username = str(person.get("username") or "")
        display = str(person.get("display_name") or "").strip()
        if display:
            index.setdefault(display.casefold(), username)
    for person in people:
        username = str(person.get("username") or "")
        if username:
            index[username.casefold()] = username
    return index


def _resolve_word(
    token: str,
    numbered: Sequence[Person],
    names: Mapping[str, str],
    *,
    multi: bool,
    teams_resolver: TeamsResolver | None,
) -> list[str] | None:
    """Usernames one token stands for, or ``None`` when it matches nothing."""
    lowered = token.casefold()
    usernames = [str(person.get("username") or "") for person in numbered]
    if token.isdigit():
        index = int(token)
        return [usernames[index - 1]] if 1 <= index <= len(usernames) else None
    match = _RANGE_RE.match(token)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        if not multi or not 1 <= low <= high <= len(usernames):
            return None
        return usernames[low - 1 : high]
    if lowered == "all":
        return list(usernames) if multi and usernames else None
    if lowered.startswith(TEAM_PREFIX):
        team = token[len(TEAM_PREFIX) :].strip()
        if teams_resolver is None or not team:
            return None
        try:
            members = teams_resolver(team)
        except (KeyError, LookupError, ValueError):
            return None
        return [str(member) for member in members] if members else None
    if lowered in names:
        return [names[lowered]]
    return None


def _resolve(
    text: str,
    numbered: Sequence[Person],
    named: Sequence[Person],
    *,
    multi: bool,
    teams_resolver: TeamsResolver | None,
) -> tuple[list[str], list[str]]:
    names = _by_name(named)
    picked: list[str] = []
    unknown: list[str] = []

    def take(token: str) -> bool:
        found = _resolve_word(token, numbered, names, multi=multi, teams_resolver=teams_resolver)
        if found is None:
            return False
        picked.extend(found)
        return True

    # A comma part that is an exact display name stays whole, so
    # "Alice Smith, bob" works although tokens are whitespace-separated.
    for part in _comma_parts(text):
        if part.casefold() in names and take(part):
            continue
        for token in _TOKEN_SPLIT_RE.split(part):
            if token and not take(token):
                unknown.append(token)

    usernames = list(dict.fromkeys(name for name in picked if name))
    unknown = list(dict.fromkeys(unknown))
    if not multi and len(usernames) > 1:
        raise ValueError(ONLY_ONE)
    return usernames, unknown


def parse_selection(
    text: str,
    people: list[dict],
    *,
    multi: bool,
    teams_resolver: TeamsResolver | None = None,
) -> tuple[list[str], list[str]]:
    """Turn an answer into ``(usernames, unknown_tokens)``.

    Tokens are split on commas and whitespace.  Accepted: ``3``; ``2-5`` and
    ``all`` (multi only); an exact username or display name (case-insensitive);
    ``team:NAME`` -- ``teams_resolver`` is called with ``NAME`` (the part after
    ``team:``) and returns usernames.  Both lists are de-duplicated in order of
    first appearance.  In single mode more than one username raises
    ``ValueError('pick exactly one')``.
    """
    return _resolve(text, people, people, multi=multi, teams_resolver=teams_resolver)


# ------------------------------------------------------------------- picking


def _matches(person: Person, needle: str) -> bool:
    haystacks = (person.get("username"), person.get("display_name"))
    return any(needle in str(value or "").casefold() for value in haystacks)


def _suggestion_line(people: Sequence[Person], token: str, suggest: Suggester | None) -> str | None:
    if suggest is None:
        return None
    try:
        candidates = list(suggest(people, token) or [])[:MAX_SUGGESTIONS]
    except (KeyError, LookupError, ValueError, TypeError):
        return None
    labels = [
        f"{_safe(person.get('username'))} ({_safe(person.get('display_name'))})" for person in candidates
    ]
    return "did you mean: " + ", ".join(labels) if labels else None


def _ask(io: Any, prompt: str) -> str | None:
    """One answer, or ``None`` on Ctrl-C/EOF."""
    try:
        return str(io.ask(prompt, None) or "").strip()
    except (KeyboardInterrupt, EOFError):
        return None


def _with_none(prompt: str, none_label: str | None) -> str:
    """``prompt`` plus the Enter-means-nobody note when "nobody" is allowed."""
    return f"{prompt}, Enter = {_safe(none_label)}" if none_label is not None else prompt


def _people_count(shown: int, total: int) -> str:
    noun = "person" if total == 1 else "people"
    return f"{shown} {noun}" if shown == total else f"{shown} of {total} {noun}"


def _choose_none(io: Any, none_label: str) -> list[str]:
    io.show(f"selected: {_safe(none_label)}")
    return []


def _ask_free_text(io: Any, *, multi: bool, none_label: str | None) -> list[str] | None:
    """Typed usernames when there is no table; ``none_label`` set = Enter picks nobody."""
    io.show("no people to pick from")
    prompt = (
        f"Username (Enter = {_safe(none_label)}, b to go back)"
        if none_label is not None
        else FREE_TEXT_PROMPT
    )
    while True:
        answer = _ask(io, prompt)
        if answer is None or answer.casefold() in BACK_ANSWERS:
            return BACK
        names = list(dict.fromkeys(part.strip() for part in answer.split(",") if part.strip()))
        if not names:
            if none_label is not None:
                return _choose_none(io, none_label)
            io.show("a choice is required")
            continue
        if not multi and len(names) > 1:
            io.show(ONLY_ONE)
            continue
        return names


def pick_people(
    io: Any,
    people: list[dict],
    *,
    title: str = "People",
    multi: bool = True,
    allow_none: bool = False,
    none_label: str = "(nobody / unassigned)",
    focus_project: str | None = None,
    teams_resolver: TeamsResolver | None = None,
    suggest: Suggester | None = None,
) -> list[str] | None:
    """Ask the operator to pick people; return their usernames.

    Returns :data:`BACK` (``None``) on ``b``/``!back``/Ctrl-C/EOF.  When
    ``allow_none``, ``0`` or an empty answer (Enter) returns ``[]``;
    otherwise Enter re-prompts.  Nothing is returned until every token in the
    answer resolves.  ``io`` needs ``ask(prompt, default)`` and ``show``.
    """
    nobody = none_label if allow_none else None
    if not people:
        return _ask_free_text(io, multi=multi, none_label=nobody)
    prompt = _with_none(MULTI_PROMPT if multi else SINGLE_PROMPT, nobody)
    hint = MULTI_HINT if multi else SINGLE_HINT
    view: list[dict] = list(people)
    redraw = True
    while True:
        if redraw:
            table = render_people(view, focus_project=focus_project)
            lines = [f"{_safe(title)} ({_people_count(len(view), len(people))})", hint, table]
            if allow_none:
                lines.append(f"0. {_safe(none_label)}")
            io.show("\n".join(lines))
            redraw = False

        answer = _ask(io, prompt)
        if answer is None or answer.casefold() in BACK_ANSWERS:
            return BACK
        if allow_none and answer in ("", "0"):
            return _choose_none(io, none_label)
        if not answer:
            io.show("a choice is required")
            continue
        if answer.startswith("/"):
            needle = answer[1:].strip().casefold()
            matches = [person for person in people if _matches(person, needle)]
            if not matches:
                io.show(f"no matches for {_safe(answer[1:].strip())!r}")
                continue
            view, redraw = matches, True
            continue

        try:
            usernames, unknown = _resolve(answer, view, people, multi=multi, teams_resolver=teams_resolver)
        except ValueError as error:
            io.show(str(error))
            continue
        if unknown:
            for token in unknown:
                io.show(f"not found: {_safe(token)}")
                hint = _suggestion_line(people, token, suggest)
                if hint:
                    io.show(hint)
            continue
        if not usernames:
            io.show("a choice is required")
            continue
        io.show("selected: " + ", ".join(_safe(name) for name in usernames))
        return usernames
