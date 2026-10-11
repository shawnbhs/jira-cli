"""User lookup and local team-roster management.

Jira Software **Server** 10.3 identifies users by their ``name`` (username), not by
the Cloud-only ``accountId``.  Every endpoint used here therefore takes a
``username`` query parameter and returns ``name`` / ``displayName`` /
``emailAddress`` / ``active``.

Teams are a purely local concept: a small YAML roster (``teams.yaml``) that maps a
team name to its members, projects and boards.  The file is written atomically and
kept private (mode ``0600`` in a ``0700`` directory) because it names real people.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from ..errors import InputError

__all__ = [
    "search_users",
    "get_user",
    "assignable_users",
    "load_teams",
    "save_team",
    "remove_team",
    "resolve_people",
]

#: A team name is used as a YAML key, a CLI token and part of ``team:<name>``,
#: so it is deliberately restricted to characters that need no quoting anywhere.
TEAM_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_DIR_MODE = 0o700
_FILE_MODE = 0o600

_TEAM_LIST_FIELDS = ("members", "projects", "boards")


# --------------------------------------------------------------------------- #
# Jira user endpoints
# --------------------------------------------------------------------------- #


def _normalize_user(raw: Mapping[str, Any]) -> dict:
    """Reduce a Jira Server user object to the four fields callers care about."""
    return {
        "username": raw.get("name"),
        "display_name": raw.get("displayName"),
        "email": raw.get("emailAddress"),
        "active": bool(raw.get("active", True)),
    }


def _normalize_users(payload: Any) -> list[dict]:
    """Normalize a user-search response, tolerating a non-list body."""
    if not isinstance(payload, list):
        return []
    return [_normalize_user(item) for item in payload if isinstance(item, Mapping)]


def _require_text(value: Any, label: str) -> str:
    """Return ``value`` stripped, or raise :class:`InputError` when it is blank."""
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{label} is required")
    return value.strip()


def search_users(client, query: str, limit: int = 50) -> list[dict]:
    """Search all visible users by username, display name or e-mail fragment.

    Args:
        client: A ``JiraClient``.
        query: Substring to match; must not be empty (the Server endpoint
            requires a ``username`` parameter).
        limit: Maximum number of users to return.

    Returns:
        A list of ``{"username", "display_name", "email", "active"}`` dicts.

    Raises:
        InputError: If ``query`` is blank or ``limit`` is not positive.
    """
    text = _require_text(query, "search query")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise InputError("limit must be a positive integer")
    payload = client.get(
        "/rest/api/2/user/search",
        params={"username": text, "maxResults": limit},
    )
    return _normalize_users(payload)


def get_user(client, username: str) -> dict:
    """Fetch a single user by username.

    Raises:
        InputError: If ``username`` is blank.
        NotFoundError: Propagated from the client when no such user exists.
    """
    name = _require_text(username, "username")
    payload = client.get("/rest/api/2/user", params={"username": name})
    if not isinstance(payload, Mapping):
        raise InputError(f"unexpected response for user {name!r}")
    return _normalize_user(payload)


def assignable_users(client, project_key: str, query: str = "") -> list[dict]:
    """List users who can be assigned issues in a project.

    Args:
        client: A ``JiraClient``.
        project_key: Project key, e.g. ``"PROJ"``.
        query: Optional username substring; an empty string lists everyone
            assignable.

    Raises:
        InputError: If ``project_key`` is blank.
    """
    key = _require_text(project_key, "project key")
    payload = client.get(
        "/rest/api/2/user/assignable/search",
        params={"project": key, "username": query or ""},
    )
    return _normalize_users(payload)


# --------------------------------------------------------------------------- #
# teams.yaml
# --------------------------------------------------------------------------- #


def _validate_team_name(name: Any) -> str:
    """Validate a team name against :data:`TEAM_NAME_RE`."""
    if not isinstance(name, str) or not TEAM_NAME_RE.match(name):
        raise InputError(
            "team name must be 1-64 characters of letters, digits, '_', '.' or '-'"
        )
    return name


def _clean_names(values: Any, label: str) -> list[str]:
    """Coerce ``values`` into a deduplicated list of non-empty strings."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        raise InputError(f"{label} must be a list of strings")
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise InputError(f"{label} must contain only non-empty strings")
        text = item.strip()
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_board_ids(values: Any) -> list[int]:
    """Coerce ``values`` into a deduplicated list of integer board ids."""
    if values is None:
        return []
    if isinstance(values, (int, str)):
        values = [values]
    if not isinstance(values, Iterable):
        raise InputError("boards must be a list of integers")
    cleaned: list[int] = []
    for item in values:
        if isinstance(item, bool):
            raise InputError("boards must contain only integers")
        try:
            board_id = int(item)
        except (TypeError, ValueError) as exc:
            raise InputError("boards must contain only integers") from exc
        if board_id not in cleaned:
            cleaned.append(board_id)
    return cleaned


def _normalize_team(name: str, raw: Any) -> dict:
    """Normalize one roster entry so every team has all three list fields."""
    if raw is None:
        raw = {}
    if isinstance(raw, list):  # shorthand: "backend: [alice, bob]"
        raw = {"members": raw}
    if not isinstance(raw, Mapping):
        raise InputError(f"team {name!r} must be a mapping of members/projects/boards")
    return {
        "members": _clean_names(raw.get("members"), f"team {name!r} members"),
        "projects": _clean_names(raw.get("projects"), f"team {name!r} projects"),
        "boards": _clean_board_ids(raw.get("boards")),
    }


def load_teams(path: Path) -> dict:
    """Read the team roster from ``path``.

    A missing file is not an error and yields ``{}``.

    Returns:
        ``{"<team>": {"members": [...], "projects": [...], "boards": [...]}}``

    Raises:
        InputError: If the file is not a YAML mapping, or an entry is malformed.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except IsADirectoryError as exc:
        raise InputError(f"teams file {file_path} is a directory") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InputError(f"teams file {file_path} is not valid YAML") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise InputError(f"teams file {file_path} must contain a mapping of teams")
    teams: dict[str, dict] = {}
    for name, entry in data.items():
        team_name = _validate_team_name(str(name))
        teams[team_name] = _normalize_team(team_name, entry)
    return teams


def _write_teams(path: Path, teams: Mapping[str, Any]) -> None:
    """Write the roster atomically with private permissions.

    The temporary file is created in the destination directory so that
    :func:`os.replace` is a same-filesystem rename, and it is chmod-ed before the
    rename so the roster is never world-readable, not even briefly.
    """
    file_path = Path(path)
    parent = file_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, _DIR_MODE)
    except OSError:  # e.g. a filesystem without POSIX modes
        pass

    payload = yaml.safe_dump(dict(teams), default_flow_style=False, sort_keys=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix=file_path.name + ".", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.chmod(tmp_name, _FILE_MODE)
        os.replace(tmp_name, file_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    try:
        os.chmod(file_path, _FILE_MODE)
    except OSError:
        pass


def save_team(
    path: Path,
    name: str,
    members: list[str],
    projects: list[str] | None = None,
    boards: list[int] | None = None,
) -> dict:
    """Create or replace one team in the roster at ``path``.

    Returns:
        ``{"name", "members", "projects", "boards"}`` as stored.

    Raises:
        InputError: If the name is invalid or a list contains bad values.
    """
    team_name = _validate_team_name(name)
    entry = {
        "members": _clean_names(members, "members"),
        "projects": _clean_names(projects, "projects"),
        "boards": _clean_board_ids(boards),
    }
    teams = load_teams(path)
    teams[team_name] = entry
    _write_teams(path, teams)
    return {"name": team_name, **entry}


def remove_team(path: Path, name: str) -> bool:
    """Delete a team from the roster.

    Returns:
        ``True`` if the team existed and was removed, ``False`` otherwise.
    """
    team_name = _validate_team_name(name)
    teams = load_teams(path)
    if team_name not in teams:
        return False
    del teams[team_name]
    _write_teams(path, teams)
    return True


# --------------------------------------------------------------------------- #
# Resolving a people specification
# --------------------------------------------------------------------------- #


def _expand_team(token: str, teams: Mapping[str, Any]) -> list[str]:
    """Expand a ``team:NAME`` / ``@NAME`` token into its member usernames."""
    team_name = token[5:] if token.lower().startswith("team:") else token[1:]
    team_name = team_name.strip()
    if not team_name:
        raise InputError("team reference is missing a name")
    entry = teams.get(team_name)
    if entry is None:
        known = ", ".join(sorted(str(key) for key in teams)) or "(none)"
        raise InputError(f"unknown team {team_name!r}; known teams: {known}")
    if isinstance(entry, Mapping):
        return _clean_names(entry.get("members"), f"team {team_name!r} members")
    return _clean_names(entry, f"team {team_name!r} members")


def _tokens(spec: str | list[str]) -> list[str]:
    """Flatten a spec into comma-separated, whitespace-stripped tokens."""
    if isinstance(spec, str):
        raw_items: Iterable[Any] = [spec]
    elif isinstance(spec, (list, tuple)):
        raw_items = spec
    else:
        raise InputError("people specification must be a string or a list of strings")
    tokens: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            raise InputError("people specification must contain only strings")
        tokens.extend(part.strip() for part in item.split(","))
    return [token for token in tokens if token]


def resolve_people(spec: str | list[str], teams: dict) -> list[str]:
    """Resolve a people specification into a list of usernames.

    Accepted forms, mixable and comma-separated:

    * ``"team:backend"`` or ``"@backend"`` — expands to the team's members
    * ``"alice,bob"`` — literal usernames
    * ``["@backend", "carol"]`` — a list of any of the above

    Order is preserved and duplicates are dropped.

    Raises:
        InputError: If the spec is empty, of the wrong type, or names an unknown
            team (the message lists the known team names).
    """
    teams = teams or {}
    tokens = _tokens(spec)
    if not tokens:
        raise InputError("no people specified")
    people: list[str] = []
    for token in tokens:
        if token.lower().startswith("team:") or token.startswith("@"):
            candidates = _expand_team(token, teams)
        else:
            candidates = [token]
        for username in candidates:
            if username not in people:
                people.append(username)
    if not people:
        raise InputError("people specification resolved to nobody")
    return people
