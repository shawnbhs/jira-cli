"""Project-level operations against Jira Software **Server** 10.3 (REST v2).

Server specifics that shape this module:

* Users are identified by ``name`` (username), never ``accountId`` (that is Cloud),
  so a project lead is sent as a plain username string.
* Project creation uses ``POST /rest/api/2/project`` with ``projectTypeKey`` and
  ``projectTemplateKey``; the agile templates make Jira auto-create a board, which
  :func:`create_project` then discovers (and reuses) through
  :func:`jira_cli.services.boards.ensure_board`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .. import ids
from ..errors import InputError, JiraError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing requests at runtime
    from ..client import JiraClient

__all__ = [
    "PROJECT_KEY_RE",
    "TEMPLATE_KEYS",
    "validate_project_key",
    "list_projects",
    "get_project",
    "list_issue_types",
    "list_priorities",
    "list_statuses",
    "project_url",
    "create_project",
    "create_component",
    "create_version",
]

#: Jira project keys: an uppercase letter followed by 1-9 uppercase letters/digits.
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")

#: Short template name -> Jira Server project template key.
TEMPLATE_KEYS: dict[str, str] = {
    "kanban": "com.pyxis.greenhopper.jira:gh-kanban-template",
    "scrum": "com.pyxis.greenhopper.jira:gh-scrum-template",
    "basic": "com.pyxis.greenhopper.jira:basic-software-development-template",
}

#: Templates for which Jira creates an agile board of a matching type.
_BOARD_TYPE_FOR_TEMPLATE: dict[str, str] = {
    "kanban": "kanban",
    "scrum": "scrum",
    "basic": "kanban",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_project_key(key: str) -> str:
    """Return ``key`` upper-cased and validated as a Jira project key.

    Args:
        key: Candidate project key, e.g. ``"proj"``.

    Returns:
        The normalised (upper-case, stripped) key.

    Raises:
        InputError: If the key does not match ``^[A-Z][A-Z0-9]{1,9}$``.
    """
    normalised = key.strip().upper() if isinstance(key, str) else ""
    if not PROJECT_KEY_RE.match(normalised):
        raise InputError(
            f"Invalid project key {key!r}: must be 2-10 characters, start with a "
            "letter and contain only letters and digits (pattern ^[A-Z][A-Z0-9]{1,9}$)."
        )
    # The shared validator is the path-traversal gate every URL-bound key must
    # pass; the local pattern above is stricter and only adds a clearer message.
    return ids.validate_project_key(normalised)


def _lead_username(raw: dict[str, Any]) -> str | None:
    """Extract the lead's username from a project/component payload."""
    lead = raw.get("lead")
    if isinstance(lead, dict):
        return lead.get("name") or lead.get("key") or None
    if isinstance(lead, str) and lead:
        return lead
    return None


def _lead_display_name(raw: dict[str, Any]) -> str | None:
    """Extract the lead's display name from a project/component payload."""
    lead = raw.get("lead")
    if isinstance(lead, dict):
        return lead.get("displayName") or None
    return None


def _names(items: Any) -> list[str]:
    """Return the ``name`` of every dict in ``items``, skipping blanks."""
    if not isinstance(items, list):
        return []
    return [str(i["name"]) for i in items if isinstance(i, dict) and i.get("name")]


def list_projects(client: JiraClient) -> list[dict]:
    """List every project visible to the authenticated user.

    Args:
        client: Configured Jira client.

    Returns:
        A list of ``{"key", "name", "lead", "lead_name", "type"}`` dicts. ``lead``
        is the lead's username and ``lead_name`` their display name; either is
        ``None`` when the server does not report it.

    Note:
        Jira Server leaves ``lead`` out of the project *list* unless it is asked
        for with ``expand=lead``, so the request always carries it.
    """
    raw = client.get("/rest/api/2/project", {"expand": "lead"}) or []
    return [
        {
            "key": p.get("key"),
            "name": p.get("name"),
            "lead": _lead_username(p),
            "lead_name": _lead_display_name(p),
            "type": p.get("projectTypeKey"),
        }
        for p in raw
        if isinstance(p, dict)
    ]


def get_project(client: JiraClient, key: str) -> dict:
    """Fetch one project with its issue types, components and versions.

    Args:
        client: Configured Jira client.
        key: Project key (case-insensitive).

    Returns:
        ``{"key", "name", "id", "type", "lead", "lead_name", "description", "url",
        "issue_types": [str], "components": [str],
        "versions": [{"name", "released", "release_date"}]}``.

    Raises:
        InputError: If ``key`` is not a valid project key.
        NotFoundError: If the project does not exist (raised by the client).
    """
    project_key = validate_project_key(key)
    raw = client.get(f"/rest/api/2/project/{project_key}") or {}
    versions = [
        {
            "name": v.get("name"),
            "released": bool(v.get("released")),
            "release_date": v.get("releaseDate"),
        }
        for v in raw.get("versions") or []
        if isinstance(v, dict)
    ]
    return {
        "key": raw.get("key", project_key),
        "name": raw.get("name"),
        "id": raw.get("id"),
        "type": raw.get("projectTypeKey"),
        "lead": _lead_username(raw),
        "lead_name": _lead_display_name(raw),
        "description": raw.get("description"),
        "url": project_url(client, raw.get("key", project_key)),
        "issue_types": _names(raw.get("issueTypes")),
        "components": _names(raw.get("components")),
        "versions": versions,
    }


def project_url(client: JiraClient, key: str) -> str:
    """Return the human browse URL of a project's summary page."""
    base = str(client.settings.base_url).rstrip("/")
    return f"{base}/projects/{key}"


def list_issue_types(client: JiraClient, project_key: str) -> list[str]:
    """Return the issue type names a user can create in ``project_key``.

    Source order: the Jira 9+/10 createmeta endpoint
    ``/rest/api/2/issue/createmeta/{key}/issuetypes`` (the legacy single-shot
    ``/issue/createmeta`` is gone on Jira 10.3 and answers 404), then the
    ``issueTypes`` of ``/rest/api/2/project/{key}`` when createmeta fails or is
    empty.  Server order is kept.
    """
    key = validate_project_key(project_key)
    # Lazy import keeps the two service modules independent at import time.
    from . import issues

    try:
        names = _names(issues.createmeta_issue_types(client, key))
    except JiraError:
        names = []
    if names:
        return names
    return get_project(client, key)["issue_types"]


def list_priorities(client: JiraClient) -> list[str]:
    """Return every priority name configured on the instance, server order kept."""
    return _names(client.get("/rest/api/2/priority") or [])


def list_statuses(client: JiraClient, project_key: str) -> list[str]:
    """Return the distinct status names usable in ``project_key``.

    The server reports statuses grouped per issue type, so the same status appears
    many times; duplicates are dropped and the first-seen order is preserved.

    Args:
        client: Configured Jira client.
        project_key: Project key (case-insensitive).

    Returns:
        Status names in first-seen order, e.g. ``["To Do", "In Progress", "Done"]``.
    """
    key = validate_project_key(project_key)
    groups = client.get(f"/rest/api/2/project/{key}/statuses") or []
    ordered: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        for status in group.get("statuses") or []:
            name = status.get("name") if isinstance(status, dict) else None
            if name and name not in seen:
                seen.add(name)
                ordered.append(str(name))
    return ordered


def create_project(
    client: JiraClient,
    key: str,
    name: str,
    lead: str,
    template: str = "kanban",
    description: str | None = None,
) -> dict:
    """Create a software project and make sure it has an agile board.

    Jira Server auto-creates a board for the agile templates, so the board is not
    created blindly: :func:`jira_cli.services.boards.ensure_board` looks for an
    existing board first and only creates one when none is found.

    Args:
        client: Configured Jira client.
        key: Desired project key, validated against ``^[A-Z][A-Z0-9]{1,9}$``.
        name: Project display name (required, non-blank).
        lead: Project lead **username** (Server identifies users by username).
        template: One of ``"kanban"``, ``"scrum"``, ``"basic"``.
        description: Optional project description.

    Returns:
        ``{"key", "id", "url", "board": <board dict or None>, "board_created": bool,
        "warnings": [str]}``. ``board`` is ``None`` when board discovery/creation
        failed; the reason is then in ``warnings`` (the project itself was still
        created).

    Raises:
        InputError: On an invalid key, a blank name/lead, or an unknown template.
        ReadOnlyError: If the client is configured read-only.
    """
    project_key = validate_project_key(key)
    if not (name or "").strip():
        raise InputError("Project name must not be empty.")
    if not (lead or "").strip():
        raise InputError("Project lead username must not be empty.")
    template_name = (template or "").strip().lower()
    if template_name not in TEMPLATE_KEYS:
        raise InputError(
            f"Unknown project template {template!r}: choose one of "
            f"{', '.join(sorted(TEMPLATE_KEYS))}."
        )

    payload: dict[str, Any] = {
        "key": project_key,
        "name": name.strip(),
        "lead": lead.strip(),
        "projectTypeKey": "software",
        "projectTemplateKey": TEMPLATE_KEYS[template_name],
    }
    if description:
        payload["description"] = description

    created = client.post("/rest/api/2/project", json=payload) or {}

    warnings: list[str] = []
    board: dict | None = None
    board_created = False
    try:
        # Lazy import so a fake boards module installed in ``sys.modules`` (tests,
        # the menu's ``_svc`` indirection) is honoured, and so neither service
        # module needs the other at import time.
        from . import boards

        ensured = boards.ensure_board(
            client,
            project_key,
            board_type=_BOARD_TYPE_FOR_TEMPLATE[template_name],
        )
        board = ensured.get("board")
        board_created = bool(ensured.get("created"))
    except JiraError as exc:  # the project exists; the board is best-effort
        warnings.append(f"Project created but board setup failed: {exc}")

    return {
        "key": created.get("key", project_key),
        "id": created.get("id"),
        "url": project_url(client, created.get("key", project_key)),
        "board": board,
        "board_created": board_created,
        "warnings": warnings,
    }


def create_component(
    client: JiraClient,
    project_key: str,
    name: str,
    lead: str | None = None,
) -> dict:
    """Create a project component.

    Args:
        client: Configured Jira client.
        project_key: Owning project key.
        name: Component name (required, non-blank).
        lead: Optional component lead **username**.

    Returns:
        ``{"id", "name", "project", "lead"}``.

    Raises:
        InputError: On an invalid project key or a blank name.
        ReadOnlyError: If the client is configured read-only.
    """
    key = validate_project_key(project_key)
    if not (name or "").strip():
        raise InputError("Component name must not be empty.")
    payload: dict[str, Any] = {"name": name.strip(), "project": key}
    if lead:
        # Server takes the lead's username under leadUserName, not a lead object.
        payload["leadUserName"] = lead.strip()
    created = client.post("/rest/api/2/component", json=payload) or {}
    return {
        "id": created.get("id"),
        "name": created.get("name", name.strip()),
        "project": created.get("project", key),
        "lead": _lead_username(created) or (lead.strip() if lead else None),
    }


def create_version(
    client: JiraClient,
    project_key: str,
    name: str,
    release_date: str | None = None,
) -> dict:
    """Create a project version (fix version).

    Args:
        client: Configured Jira client.
        project_key: Owning project key.
        name: Version name (required, non-blank).
        release_date: Optional planned release date, ``YYYY-MM-DD``.

    Returns:
        ``{"id", "name", "project", "release_date", "released"}``.

    Raises:
        InputError: On an invalid project key, a blank name, or a malformed date.
        ReadOnlyError: If the client is configured read-only.
    """
    key = validate_project_key(project_key)
    if not (name or "").strip():
        raise InputError("Version name must not be empty.")
    payload: dict[str, Any] = {"name": name.strip(), "project": key}
    if release_date:
        if not _DATE_RE.match(release_date.strip()):
            raise InputError(
                f"Invalid release date {release_date!r}: expected YYYY-MM-DD."
            )
        payload["releaseDate"] = release_date.strip()
    created = client.post("/rest/api/2/version", json=payload) or {}
    return {
        "id": created.get("id"),
        "name": created.get("name", name.strip()),
        "project": created.get("project", key),
        "release_date": created.get("releaseDate", payload.get("releaseDate")),
        "released": bool(created.get("released")),
    }
