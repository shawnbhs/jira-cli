"""Shared "who?" and "which projects?" prompts for every menu section.

Nobody remembers Jira usernames or project keys, so every section asks through
this module: people come from the role directory of *all* projects (built once
per menu session and cached on the session object), projects from a numbered
list of the server's projects (also cached).

* :func:`people` -- the cached person rows, optionally narrowed to a project.
* :func:`ask_person` / :func:`ask_people` -- numbered people picker
  (``1,3``, ``2-5``, ``all``, exact names, ``team:NAME``, ``/text`` filter).
* :func:`ask_projects` -- numbered project pick; ``all`` means every project.

Every function raises :class:`_Back` when the operator backs out.  Service
modules are reached through ``_svc`` so tests can swap them in ``sys.modules``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from jira_cli.cli import people_picker
from jira_cli.cli.menu import JiraError, _as_path, _ask, _Back, _Session, _svc
from jira_cli.textsafe import terminal_safe_line

__all__ = ["ask_people", "ask_person", "ask_projects", "people"]

#: Session attributes holding the per-session caches.
PEOPLE_CACHE = "_people_cache"
FALLBACK_CACHE = "_people_fallback"
PROJECTS_CACHE = "_projects_cache"

#: Jira Server's user search needs a non-empty username; "." matches everyone.
_EVERYONE_QUERY = "."
_EVERYONE_LIMIT = 1000
_ALL_WORDS = frozenset({"all", "*"})
_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")
_TOKEN_SPLIT_RE = re.compile(r"[\s,]+")
#: Cache value meaning "the directory was tried and is not available".
_UNAVAILABLE: list[dict] = []


# ------------------------------------------------------------------ people


def _person_row(user: dict) -> dict:
    """A plain user (from the fallback endpoints) in directory person shape."""
    username = str(user.get("username") or "")
    return {
        "username": username,
        "display_name": user.get("display_name") or username,
        "active": user.get("active"),
        "projects": [],
        "project_admin": [],
        "jira_admin": None,
    }


def _load_directory(session: _Session) -> list[dict]:
    """The all-projects directory, built on first use; ``_UNAVAILABLE`` on failure."""
    cached = getattr(session, PEOPLE_CACHE, None)
    if cached is not None:
        return cached
    session.io.show("loading people from Jira...")
    started = time.monotonic()
    try:
        rows = list(_svc("directory").build_directory(session.client) or [])
        reason = "it came back empty" if not rows else ""
    except JiraError as exc:
        rows, reason = [], str(exc)
    if rows:
        seconds = time.monotonic() - started
        session.io.show(f"loaded {len(rows)} people in {seconds:.1f}s")
    else:
        session.io.show(
            terminal_safe_line(f"people directory unavailable ({reason}); showing assignable users instead")
        )
        rows = _UNAVAILABLE
    setattr(session, PEOPLE_CACHE, rows)
    return rows


def _fallback_people(session: _Session, project: str | None) -> list[dict]:
    """Assignable users of ``project`` (or a user search), cached per project."""
    cache: dict[str, list[dict]] = getattr(session, FALLBACK_CACHE, None) or {}
    key = (project or "").upper()
    if key not in cache:
        people_svc = _svc("people")
        try:
            if key:
                users = people_svc.assignable_users(session.client, key)
            else:
                users = people_svc.search_users(session.client, _EVERYONE_QUERY, _EVERYONE_LIMIT)
        except JiraError as exc:
            session.io.show(terminal_safe_line(f"could not list users: {exc}"))
            users = []
        cache[key] = [_person_row(user) for user in users or [] if user.get("username")]
        setattr(session, FALLBACK_CACHE, cache)
    return list(cache[key])


def _has_role_in(person: dict, project: str) -> bool:
    wanted = project.upper()
    return any(str(entry.get("key") or "").upper() == wanted for entry in person.get("projects") or [])


def people(session: _Session, project: str | None = None) -> list[dict]:
    """Person rows for the pickers, in directory order.

    With ``project`` only the people holding a role there are returned; when
    nobody does, everyone is returned so the operator is never left empty-handed.
    """
    directory = _load_directory(session)
    if directory is _UNAVAILABLE:
        return _fallback_people(session, project)
    if project:
        in_project = [person for person in directory if _has_role_in(person, project)]
        if in_project:
            return in_project
    return list(directory)


def _candidates(
    session: _Session, project: str | None, only: list[str] | None, exclude: list[str] | None
) -> list[dict]:
    if only is None:
        rows = people(session, project)
    else:
        known = {str(p.get("username") or "").casefold(): p for p in people(session, None)}
        wanted = list(dict.fromkeys(name for name in only if name))
        wanted_folded = {name.casefold() for name in wanted}
        listed = [person for folded, person in known.items() if folded in wanted_folded]
        missing = [_person_row({"username": name}) for name in wanted if name.casefold() not in known]
        rows = listed + missing
    if exclude:
        dropped = {name.casefold() for name in exclude}
        rows = [p for p in rows if str(p.get("username") or "").casefold() not in dropped]
    return rows


def _teams_resolver(session: _Session) -> Callable[[str], list[str] | None]:
    """``team:NAME`` lookup against the configured teams file (read when used)."""
    people_svc = _svc("people")
    path = _as_path(getattr(session.settings, "teams_file", None), "teams.yaml")

    def resolve(name: str) -> list[str] | None:
        try:
            return list(people_svc.resolve_people(f"team:{name}", people_svc.load_teams(path)))
        except JiraError as exc:
            session.io.show(terminal_safe_line(str(exc)))
            return None

    return resolve


def _pick(
    session: _Session,
    title: str,
    *,
    project: str | None,
    multi: bool,
    allow_none: bool = False,
    none_label: str = "(nobody / unassigned)",
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[str]:
    picked = people_picker.pick_people(
        session.io,
        _candidates(session, project, only, exclude),
        title=title,
        multi=multi,
        allow_none=allow_none,
        none_label=none_label,
        focus_project=project,
        teams_resolver=_teams_resolver(session),
        suggest=getattr(_svc("directory"), "suggest", None),
    )
    if picked is None:
        raise _Back
    return list(picked)


def ask_person(
    session: _Session,
    title: str,
    *,
    project: str | None = None,
    allow_none: bool = False,
    none_label: str = "(nobody / unassigned)",
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> str | None:
    """One username; ``None`` when ``allow_none`` and "nobody" was picked."""
    picked = _pick(
        session,
        title,
        project=project,
        multi=False,
        allow_none=allow_none,
        none_label=none_label,
        only=only,
        exclude=exclude,
    )
    return picked[0] if picked else None


def ask_people(
    session: _Session,
    title: str,
    *,
    project: str | None = None,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[str]:
    """One or more usernames (numbers, ranges, ``all``, names, ``team:NAME``)."""
    return _pick(session, title, project=project, multi=True, only=only, exclude=exclude)


# ---------------------------------------------------------------- projects


def _project_list(session: _Session) -> list[dict]:
    cached = getattr(session, PROJECTS_CACHE, None)
    if cached is None:
        listed = _svc("projects").list_projects(session.client) or []
        cached = [p for p in listed if p.get("key")]
        setattr(session, PROJECTS_CACHE, cached)
    return cached


def _default_projects(session: _Session) -> list[str]:
    raw = getattr(session.settings, "default_project", None) or ""
    return [part.strip().upper() for part in str(raw).split(",") if part.strip()]


def _projects_prompt(default: list[str], allow_all: bool) -> str:
    parts = ["Pick numbers or keys"]
    if allow_all:
        parts.append("all = every project")
    if default:
        parts.append(f"Enter = [{','.join(default)}]")
    elif allow_all:
        parts.append("Enter = all")
    parts.append("b = back")
    return ", ".join(parts)


def _resolve_project_token(token: str, keys: list[str]) -> list[str] | None:
    if token.isdigit():
        index = int(token)
        return [keys[index - 1]] if 1 <= index <= len(keys) else None
    match = _RANGE_RE.match(token)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return keys[low - 1 : high] if 1 <= low <= high <= len(keys) else None
    by_key = {key.casefold(): key for key in keys}
    found = by_key.get(token.casefold())
    if found is None and not keys:  # no list to check against: take a well-formed key
        try:
            found = _svc("projects").validate_project_key(token.upper())
        except JiraError:
            found = None
    return [found] if found else None


def _parse_projects(answer: str, keys: list[str]) -> tuple[list[str], list[str]]:
    """``(picked keys, unknown tokens)`` for a typed answer."""
    picked: list[str] = []
    unknown: list[str] = []
    for token in _TOKEN_SPLIT_RE.split(answer):
        if not token:
            continue
        found = _resolve_project_token(token, keys)
        if found is None:
            unknown.append(token)
        else:
            picked.extend(found)
    return list(dict.fromkeys(picked)), unknown


def ask_projects(session: _Session, title: str = "Projects", *, allow_all: bool = True) -> list[str] | None:
    """Pick projects by number, range or key; ``None`` means every project.

    Enter takes the configured default project(s) (or all projects when there
    is no default and ``allow_all``); ``b`` raises :class:`_Back`.
    """
    projects = _project_list(session)
    keys = [str(p.get("key")) for p in projects]
    lines = [terminal_safe_line(title)]
    lines += [
        f"  {number:>2}. {terminal_safe_line(p.get('key'))} - {terminal_safe_line(p.get('name'))}"
        for number, p in enumerate(projects, start=1)
    ]
    session.io.show("\n".join(lines))
    default = _default_projects(session)
    prompt = _projects_prompt(default, allow_all)
    while True:
        answer = _ask(session.io, prompt)
        if not answer:
            if default:
                return list(default)
            if allow_all:
                return None
            session.io.show("pick at least one project (b to go back)")
            continue
        if answer.casefold() in _ALL_WORDS:
            if allow_all:
                return None
            session.io.show("pick specific projects here (all is not allowed)")
            continue
        picked, unknown = _parse_projects(answer, keys)
        if unknown:
            for token in unknown:
                session.io.show(terminal_safe_line(f"unknown project: {token}"))
            continue
        if picked:
            return picked
        session.io.show("pick at least one project (b to go back)")
