"""Menu section: project members (who is in a project, and through which role).

On Jira Server a person is "in" a project when they are an actor of one of its
roles, either directly or through a group that is an actor.  This section shows
everyone in one project as a single numbered table and adds or removes direct
role members.

People are always picked from the people directory through ``menu_people``, so
only real users can be added (nobody who is already in the role is offered) and
only current direct members can be removed.  A typed name that is not a real
person can therefore never reach a preview.

Role membership is re-read from the server (``services.members.list_roles``)
every time it is shown or changed; the cached directory supplies display names,
the active flag, admin rights and memberships that come through a group.
"""

from __future__ import annotations

import importlib
from typing import Any

from jira_cli.cli.menu import (
    JiraError,
    _Back,
    _choose,
    _preview_and_confirm,
    _render_rows,
    _Session,
    _svc,
)

_ACTIONS = ["Show members", "Add people", "Remove people"]
_FIRST_ROLE = "Developers"
_VIA_GROUP = " (via group "
_TABLE_COLUMNS = ["#", "Name", "Username", "Roles", "Admin", "Active"]
_UNTOUCHED = "their issues, comments and worklogs are not touched"


def _people_ui() -> Any:
    """The shared people prompts, imported lazily so tests can install a fake."""
    return importlib.import_module("jira_cli.cli.menu_people")


def run(session: _Session) -> None:
    """Main-menu handler: pick a project, then show / add / remove until ``b``."""
    while True:
        project_key = _ask_project(session)
        try:
            _project_loop(session, project_key)
        except _Back:
            continue


def _ask_project(session: _Session) -> str:
    """One project key; ``b`` raises :class:`_Back` to the main menu."""
    while True:
        keys = _people_ui().ask_projects(session, "Project", allow_all=False) or []
        if len(keys) == 1:
            return str(keys[0])
        session.say("pick exactly one project")


def _project_loop(session: _Session, project_key: str) -> None:
    """Stay inside one project until ``b`` (which raises :class:`_Back`)."""
    handlers = (_show_members, _add_people, _remove_people)
    while True:
        action = _choose(session.io, f"Members of {project_key}", _ACTIONS)
        try:
            handlers[action](session, project_key)
        except _Back:
            continue


# ------------------------------------------------------------------ reading


def _read_roles(session: _Session, project_key: str) -> list[dict]:
    return list(_svc("members").list_roles(session.client, project_key) or [])


def _project_people(session: _Session, project_key: str) -> list[dict]:
    """Directory rows of people who hold any role in ``project_key``."""
    rows = _people_ui().people(session, project_key) or []
    return [p for p in rows if _labels_in(p, project_key)]


def _labels_in(person: dict, project_key: str) -> list[str]:
    for entry in person.get("projects") or []:
        if str(entry.get("key")) == project_key:
            return [str(label) for label in entry.get("roles") or []]
    return []


def _is_group_label(label: str) -> bool:
    return _VIA_GROUP in label


def _role_of_label(label: str) -> str:
    return label.split(_VIA_GROUP, 1)[0]


def _direct_roles(roles: list[dict]) -> dict[str, list[str]]:
    """Case-folded username -> names of the roles the user holds directly."""
    held: dict[str, list[str]] = {}
    for role in roles:
        for user in role.get("users") or []:
            name = str(user.get("username") or "")
            if name:
                held.setdefault(name.casefold(), []).append(str(role.get("name")))
    return held


def _admin_text(person: dict, project_key: str) -> str:
    parts = []
    if project_key in (person.get("project_admin") or []):
        parts.append("project admin")
    jira_admin = person.get("jira_admin")
    if jira_admin is True:
        parts.append("Jira admin")
    if parts:
        return ", ".join(parts)
    return "?" if jira_admin is None else "-"


def _active_text(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "?"


def member_rows(project_key: str, roles: list[dict], people: list[dict]) -> list[dict]:
    """One row per person in the project, sorted by name.

    Direct roles come from ``roles`` (fresh from the server); roles held
    through a group, names, admin rights and the active flag come from the
    directory ``people``.  A direct member the directory does not know yet is
    still listed.
    """
    direct = _direct_roles(roles)
    known = {str(p.get("username") or "").casefold(): p for p in people}
    names_from_roles = {
        str(u.get("username") or "").casefold(): u for r in roles for u in r.get("users") or []
    }
    rows = []
    for folded in sorted(set(known) | set(direct)):
        if not folded:
            continue
        person = known.get(folded) or {}
        user = names_from_roles.get(folded) or {}
        via_group = [label for label in _labels_in(person, project_key) if _is_group_label(label)]
        labels = sorted(direct.get(folded, []), key=str.casefold) + via_group
        if not labels:
            continue  # the directory is older than this change: no longer a member
        rows.append(
            {
                "Name": person.get("display_name") or user.get("display_name") or "",
                "Username": person.get("username") or user.get("username"),
                "Roles": ", ".join(labels),
                "Admin": _admin_text(person, project_key) if person else "?",
                "Active": _active_text(person.get("active")),
            }
        )
    rows.sort(key=lambda r: (str(r["Name"] or r["Username"]).casefold(), str(r["Username"])))
    for number, row in enumerate(rows, start=1):
        row["#"] = number
    return rows


def _group_lines(roles: list[dict]) -> list[str]:
    return [f"  {role.get('name')} <- group {group}" for role in roles for group in role.get("groups") or []]


def _show_members(session: _Session, project_key: str) -> None:
    roles = _read_roles(session, project_key)
    rows = member_rows(project_key, roles, _project_people(session, project_key))
    if not rows:
        session.say(f"project {project_key} has no members")
        return
    count = "1 person" if len(rows) == 1 else f"{len(rows)} people"
    session.say(f"Members of {project_key} ({count})")
    session.io.show(_render_rows(_TABLE_COLUMNS, rows))
    groups = _group_lines(roles)
    if groups:
        session.say("\n".join(["groups in roles:", *groups]))


# ------------------------------------------------------------------ writing


def _people_text(count: int) -> str:
    return "1 person" if count == 1 else f"{count} people"


def _pick_role(session: _Session, project_key: str, roles: list[dict]) -> dict:
    """Choose one role; ``Developers`` first.  ``b`` raises :class:`_Back`."""
    if not roles:
        session.say(f"project {project_key} has no roles you can see")
        raise _Back
    ordered = sorted(roles, key=lambda r: (str(r.get("name")) != _FIRST_ROLE, str(r.get("name")).casefold()))
    options = [f"{r.get('name')} ({_people_text(len(r.get('users') or []))})" for r in ordered]
    return ordered[_choose(session.io, f"Role in {project_key}", options)]


def _direct_members(role: dict) -> list[str]:
    return [str(u.get("username")) for u in role.get("users") or [] if u.get("username")]


def _group_members(session: _Session, project_key: str, role_name: str) -> list[str]:
    """Usernames that hold ``role_name`` only through a group (from the directory)."""
    return [
        str(person.get("username"))
        for person in _project_people(session, project_key)
        if any(
            _is_group_label(label) and _role_of_label(label) == role_name
            for label in _labels_in(person, project_key)
        )
    ]


def _keep(picked: list[str], allowed: list[str]) -> list[str]:
    """``picked`` restricted to ``allowed`` (case-insensitive), in pick order."""
    folded = {name.casefold() for name in allowed}
    return [name for name in picked if name.casefold() in folded]


def _drop(picked: list[str], excluded: list[str]) -> list[str]:
    """``picked`` without ``excluded`` (case-insensitive), in pick order."""
    folded = {name.casefold() for name in excluded}
    return [name for name in picked if name.casefold() not in folded]


def _current_username(session: _Session) -> str | None:
    """The signed-in user's username: ``/myself`` first, then the settings."""
    myself = getattr(session.client, "myself", None)
    if callable(myself):
        try:
            name = (myself() or {}).get("name")
        except JiraError:
            name = None  # fall back to the configured username below
        if name:
            return str(name)
    name = getattr(session.settings, "username", None)
    return str(name) if name else None


def _show_role_after(session: _Session, project_key: str, role_name: str) -> None:
    """Re-read the role from the server and list who holds it now."""
    for role in _read_roles(session, project_key):
        if str(role.get("name")) == role_name:
            users = _direct_members(role)
            session.say(
                f"{project_key} {role_name} now has {_people_text(len(users))}: "
                + (", ".join(users) or "nobody")
            )
            return
    session.say(f"{project_key} no longer shows a role named {role_name}")


def _picks_with_role(session: _Session, project_key: str) -> tuple[dict, str]:
    """Role pick for a write; ``b`` here goes back to the action list."""
    role = _pick_role(session, project_key, _read_roles(session, project_key))
    return role, str(role.get("name"))


def _add_people(session: _Session, project_key: str) -> None:
    while True:
        role, role_name = _picks_with_role(session, project_key)
        already = _direct_members(role) + _group_members(session, project_key, role_name)
        try:
            picked = _people_ui().ask_people(session, "Add who", exclude=already)
            picked = _drop(picked, already)
        except _Back:
            continue  # back to the role list
        if not picked:
            session.say("nobody picked; nothing to do")
            continue
        lines = [f"project {project_key}", f"role {role_name}", f"add {', '.join(picked)}"]
        if _preview_and_confirm(session, "Add to project", lines):
            result = _svc("members").add_members(session.client, project_key, role_name, users=picked)
            session.say(f"added {', '.join(result.get('added_users') or []) or 'nobody'}")
            _show_role_after(session, project_key, role_name)
        return


def _self_warning(picked: list[str], me: str | None, project_key: str) -> list[str]:
    """An extra preview line when the signed-in user is about to remove themselves."""
    if me and any(name.casefold() == me.casefold() for name in picked):
        return [f"WARNING: this removes YOU ({me}); you may lose access to {project_key}"]
    return []


def _remove_people(session: _Session, project_key: str) -> None:
    while True:
        role, role_name = _picks_with_role(session, project_key)
        members = _direct_members(role)
        if not members:
            note = " (members through a group leave by leaving the group)" if role.get("groups") else ""
            session.say(f"role {role_name} has nobody to remove{note}")
            continue
        try:
            picked = _people_ui().ask_people(session, "Remove who", project=project_key, only=members)
            picked = _keep(picked, members)
        except _Back:
            continue
        if not picked:
            session.say("nobody picked; nothing to do")
            continue
        lines = [
            f"project {project_key}",
            f"role {role_name}",
            f"remove {', '.join(picked)}",
            _UNTOUCHED,
            *_self_warning(picked, _current_username(session), project_key),
        ]
        if _preview_and_confirm(session, "Remove from project", lines):
            result = _svc("members").remove_members(session.client, project_key, role_name, users=picked)
            session.say(f"removed {', '.join(result.get('removed_users') or []) or 'nobody'}")
            _show_role_after(session, project_key, role_name)
        return
