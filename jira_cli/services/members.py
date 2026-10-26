"""Project-role membership on Jira Software **Server/DC** (REST v2).

On Jira Server a person "is in a project" when they are an actor of one of the
project's roles (``Developers``, ``Administrators``, ...). The permission
scheme then grants roles the right to browse, be assigned issues and so on.
This module reads and changes those role actors:

* ``GET    /rest/api/2/project/{key}/role``            role name -> role URL
* ``GET    /rest/api/2/project/{key}/role/{id}``       one role and its actors
* ``POST   /rest/api/2/project/{key}/role/{id}``       add actors
  (body ``{"user": [...]}`` or ``{"group": [...]}``)
* ``DELETE /rest/api/2/project/{key}/role/{id}?user=X`` (or ``?group=X``)

Users are identified by **username** (``name``), never ``accountId`` (Cloud).
Every write first reads the role, so adding someone who is already a member
and removing someone who is not are reported and skipped, never sent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .. import ids
from ..errors import InputError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import JiraClient

__all__ = [
    "USERNAME_RE",
    "GROUP_RE",
    "clean_usernames",
    "clean_groups",
    "list_roles",
    "get_role",
    "resolve_role_id",
    "add_members",
    "remove_members",
]

#: Jira Server usernames: letters, digits and ``. _ - @ +`` (no spaces, no slashes).
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,255}$")
#: Group names may also contain single spaces between words.
GROUP_RE = re.compile(r"^[A-Za-z0-9._@+-]+(?: [A-Za-z0-9._@+-]+)*$")
#: Role names as shown by Jira (``Developers``, ``Service Desk Team``).
ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,100}$")

_USER_ACTOR = "atlassian-user-role-actor"
_GROUP_ACTOR = "atlassian-group-role-actor"


def _split(values: str | Iterable[str] | None) -> list[str]:
    """Accept ``"a,b"`` or ``["a", "b"]``; strip blanks; keep first-seen order."""
    if values is None:
        return []
    raw = values.split(",") if isinstance(values, str) else list(values)
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise InputError(f"expected a name, got {item!r}")
        text = item.strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def clean_usernames(values: str | Iterable[str] | None) -> list[str]:
    """Validate and de-duplicate usernames.

    Raises:
        InputError: when a username carries characters Jira usernames never have.
    """
    names = _split(values)
    for name in names:
        if not USERNAME_RE.match(name):
            raise InputError(f"invalid username {name!r}: letters, digits and . _ - @ + only")
    return names


def clean_groups(values: str | Iterable[str] | None) -> list[str]:
    """Validate and de-duplicate group names.

    Raises:
        InputError: when a group name is malformed.
    """
    names = _split(values)
    for name in names:
        if len(name) > 255 or not GROUP_RE.match(name):
            raise InputError(f"invalid group name {name!r}")
    return names


def _role_id_from_url(url: Any) -> int | None:
    """``.../project/PROJ/role/10001`` -> ``10001``; ``None`` when unparsable."""
    tail = str(url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _normalize_role(raw: dict[str, Any], fallback_name: str = "") -> dict:
    """Role payload -> ``{"id", "name", "users": [...], "groups": [...]}``."""
    users: list[dict] = []
    groups: list[str] = []
    for actor in raw.get("actors") or []:
        if not isinstance(actor, dict):
            continue
        kind = actor.get("type")
        if kind == _USER_ACTOR:
            users.append(
                {
                    "username": actor.get("name"),
                    "display_name": actor.get("displayName"),
                }
            )
        elif kind == _GROUP_ACTOR:
            groups.append(str(actor.get("name") or actor.get("displayName") or ""))
    users.sort(key=lambda u: str(u.get("username") or "").lower())
    groups.sort(key=str.lower)
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or fallback_name,
        "users": users,
        "groups": [g for g in groups if g],
    }


def _role_index(client: JiraClient, key: str) -> dict[str, int]:
    """Role name -> role id for one project."""
    raw = client.get(f"/rest/api/2/project/{key}/role") or {}
    index: dict[str, int] = {}
    if isinstance(raw, dict):
        for name, url in raw.items():
            rid = _role_id_from_url(url)
            if rid is not None:
                index[str(name)] = rid
    return index


def resolve_role_id(client: JiraClient, project: str, role: str | int) -> tuple[str, int]:
    """Resolve a role name (case-insensitive) or numeric id for a project.

    Returns:
        ``(project_key, role_id)``.

    Raises:
        InputError: for a malformed project key / role, or a role the project
            does not have (the message lists the roles it does have).
    """
    key = ids.validate_project_key(project)
    text = str(role).strip() if role is not None else ""
    if not text:
        raise InputError("a project role is required, for example Developers")
    if text.isdigit():
        return key, ids.validate_int_id(text, what="role id")
    if not ROLE_NAME_RE.match(text):
        raise InputError(f"invalid role name {text!r}")
    index = _role_index(client, key)
    for name, rid in index.items():
        if name.lower() == text.lower():
            return key, rid
    available = ", ".join(sorted(index)) or "none"
    raise InputError(f"project {key} has no role {text!r}; roles: {available}")


def get_role(client: JiraClient, project: str, role: str | int) -> dict:
    """One role of a project with its user and group members.

    Returns:
        ``{"project", "id", "name", "users": [{"username", "display_name"}],
        "groups": [str]}``.
    """
    key, rid = resolve_role_id(client, project, role)
    raw = client.get(f"/rest/api/2/project/{key}/role/{rid}") or {}
    out = _normalize_role(raw if isinstance(raw, dict) else {})
    out["id"] = out["id"] or rid
    out["project"] = key
    return out


def list_roles(client: JiraClient, project: str) -> list[dict]:
    """Every role of a project with its members, sorted by role name."""
    key = ids.validate_project_key(project)
    roles = []
    for name, rid in _role_index(client, key).items():
        raw = client.get(f"/rest/api/2/project/{key}/role/{rid}") or {}
        role = _normalize_role(raw if isinstance(raw, dict) else {}, fallback_name=name)
        role["id"] = role["id"] or rid
        roles.append(role)
    roles.sort(key=lambda r: str(r.get("name") or "").lower())
    return roles


def _require_some(users: list[str], groups: list[str]) -> None:
    if not users and not groups:
        raise InputError("name at least one user or group")


def add_members(
    client: JiraClient,
    project: str,
    role: str | int,
    users: str | Iterable[str] | None = None,
    groups: str | Iterable[str] | None = None,
) -> dict:
    """Add users and/or groups to a project role. Existing members are skipped.

    Returns:
        ``{"project", "role", "role_id", "added_users", "added_groups",
        "already_members", "members_after": {"users": [...], "groups": [...]}}``.

    Raises:
        InputError: on malformed names or an unknown role.
        ReadOnlyError: if the client is read-only (raised by the client).
    """
    user_list = clean_usernames(users)
    group_list = clean_groups(groups)
    _require_some(user_list, group_list)
    client.ensure_writable()
    current = get_role(client, project, role)
    key, rid = current["project"], current["id"]
    have_users = {str(u.get("username") or "").lower() for u in current["users"]}
    have_groups = {g.lower() for g in current["groups"]}
    new_users = [u for u in user_list if u.lower() not in have_users]
    new_groups = [g for g in group_list if g.lower() not in have_groups]
    already = [u for u in user_list if u.lower() in have_users] + [
        g for g in group_list if g.lower() in have_groups
    ]
    path = f"/rest/api/2/project/{key}/role/{rid}"
    if new_users:
        client.post(path, json={"user": new_users})
    if new_groups:
        client.post(path, json={"group": new_groups})
    # Read back instead of trusting the POST body: the reported state is what
    # Jira actually holds, not what this call thinks it did.
    after = get_role(client, key, rid) if (new_users or new_groups) else current
    return {
        "project": key,
        "role": current["name"],
        "role_id": rid,
        "added_users": new_users,
        "added_groups": new_groups,
        "already_members": already,
        "members_after": {"users": after["users"], "groups": after["groups"]},
    }


def remove_members(
    client: JiraClient,
    project: str,
    role: str | int,
    users: str | Iterable[str] | None = None,
    groups: str | Iterable[str] | None = None,
) -> dict:
    """Remove users and/or groups from a project role. Non-members are skipped.

    Removing someone from a role does not touch their issues, comments or
    worklogs; it only drops what the role granted them in this project.

    Returns:
        ``{"project", "role", "role_id", "removed_users", "removed_groups",
        "not_members", "members_after": {"users": [...], "groups": [...]}}``.
    """
    user_list = clean_usernames(users)
    group_list = clean_groups(groups)
    _require_some(user_list, group_list)
    client.ensure_writable()
    current = get_role(client, project, role)
    key, rid = current["project"], current["id"]
    by_user = {str(u.get("username") or "").lower(): str(u.get("username")) for u in current["users"]}
    by_group = {g.lower(): g for g in current["groups"]}
    removed_users: list[str] = []
    removed_groups: list[str] = []
    not_members: list[str] = []
    path = f"/rest/api/2/project/{key}/role/{rid}"
    for name in user_list:
        actual = by_user.get(name.lower())
        if actual is None:
            not_members.append(name)
            continue
        client.delete(path, params={"user": actual})
        removed_users.append(actual)
    for name in group_list:
        actual = by_group.get(name.lower())
        if actual is None:
            not_members.append(name)
            continue
        client.delete(path, params={"group": actual})
        removed_groups.append(actual)
    after = get_role(client, key, rid) if (removed_users or removed_groups) else current
    return {
        "project": key,
        "role": current["name"],
        "role_id": rid,
        "removed_users": removed_users,
        "removed_groups": removed_groups,
        "not_members": not_members,
        "members_after": {"users": after["users"], "groups": after["groups"]},
    }
