"""Agile board, sprint and backlog operations for Jira Software **Server** 10.3.

Server specifics that shape this module:

* The board list response on this server carries **no** ``location.projectKey``,
  so a project's boards cannot be found by filtering a full board list.  The
  only reliable lookup is ``GET /rest/agile/1.0/board?projectKeyOrId=<KEY>``,
  which this module always uses.
* Creating a board is a two-step operation: a saved filter must exist first
  (``POST /rest/api/2/filter``), then the board is created on that filter id
  (``POST /rest/agile/1.0/board``).
* On this server the agile sprint list (``/rest/agile/1.0/board/{id}/sprint``)
  was observed to return an EMPTY ``values`` array for every scrum board, even
  boards with an active sprint.  :func:`list_sprints` therefore falls back to
  GreenHopper (``/rest/greenhopper/1.0/sprintquery/{boardId}``, then
  ``/rest/greenhopper/1.0/sprint/picker``) when agile comes back empty; see
  :func:`find_sprints`, which also reports which source answered.
* Sprints only exist on ``scrum`` boards.  Asking a kanban board for a new
  sprint raises :class:`~jira_cli.errors.InputError` rather than letting the
  server answer with an opaque 400.

Nothing here prints; failures are raised as ``jira_cli.errors`` exceptions and
every write goes through the client, which refuses to mutate when the settings
say read-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import ids
from ..errors import InputError, JiraError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing requests
    from ..client import JiraClient

__all__ = [
    "AGILE",
    "API",
    "BOARD_TYPES",
    "list_boards",
    "get_board",
    "board_for_project",
    "ensure_board",
    "list_sprints",
    "find_sprints",
    "greenhopper_sprints",
    "active_sprint",
    "create_sprint",
    "add_to_sprint",
    "move_to_backlog",
    "board_issues",
]

API = "/rest/api/2"
AGILE = "/rest/agile/1.0"

#: Board types Jira Server accepts on ``POST /rest/agile/1.0/board``.
BOARD_TYPES = ("kanban", "scrum")

#: ``state`` values accepted by the board sprint endpoint.
_SPRINT_STATES = ("active", "future", "closed")

GREENHOPPER = "/rest/greenhopper/1.0"

#: Values of ``find_sprints(...)["method"]``.
SPRINTS_VIA_AGILE = "agile"
SPRINTS_VIA_SPRINTQUERY = "greenhopper-sprintquery"
SPRINTS_VIA_PICKER = "greenhopper-picker"
SPRINTS_VIA_NONE = "none"

#: GreenHopper rows carry no dates; at most this many are enriched from
#: ``/rest/agile/1.0/sprint/{id}`` so a big board cannot cause a request storm.
_ENRICH_LIMIT = 25


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _project_key(key: str) -> str:
    """Return ``key`` validated and upper-cased (it is used in URL paths)."""
    return ids.validate_project_key(key)


def _board_id(board_id: Any) -> int:
    """Return ``board_id`` as a positive int, or raise :class:`InputError`."""
    return ids.validate_int_id(board_id, what="board id")


def _sprint_id(sprint_id: Any) -> int:
    """Return ``sprint_id`` as a positive int, or raise :class:`InputError`."""
    return ids.validate_int_id(sprint_id, what="sprint id")


def _board_type(board_type: str) -> str:
    """Validate a board type against :data:`BOARD_TYPES`."""
    normalised = (board_type or "").strip().lower()
    if normalised not in BOARD_TYPES:
        raise InputError(
            f"unknown board type {board_type!r}: choose one of {', '.join(BOARD_TYPES)}"
        )
    return normalised


def _issue_keys(keys: Any) -> list[str]:
    """Normalise a list of issue keys, rejecting an empty or non-string list."""
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, (list, tuple)):
        raise InputError("issue keys must be given as a list of strings")
    cleaned: list[str] = []
    for key in keys:
        if not isinstance(key, str) or not key.strip():
            raise InputError(f"invalid issue key {key!r}: expected a non-empty string")
        text = ids.validate_issue_key(key)
        if text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise InputError("at least one issue key is required")
    return cleaned


def _board_project(raw: dict, fallback: str | None = None) -> str | None:
    """Best-effort project key of a board.

    This server omits ``location`` from the board list response, so ``fallback``
    (the key the caller asked about) is used whenever the payload is silent.
    """
    location = raw.get("location")
    if isinstance(location, dict):
        key = location.get("projectKey") or location.get("key")
        if key:
            return str(key)
    return fallback


def _board_row(raw: dict, project_key: str | None = None) -> dict:
    """Normalise one board payload to ``{"id", "name", "type", "project"}``."""
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("type"),
        "project": _board_project(raw, project_key),
    }


def _sprint_row(raw: dict) -> dict:
    """Normalise one sprint payload to the contract's sprint shape."""
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "state": raw.get("state"),
        "start": raw.get("startDate"),
        "end": raw.get("endDate"),
        "goal": raw.get("goal"),
    }


def _project_name(client: JiraClient, project_key: str) -> str | None:
    """Display name of a project, or ``None`` when it cannot be read.

    Used only to break a tie between several boards, so a failure here must not
    turn a board lookup into an error.
    """
    try:
        raw = client.get(f"{API}/project/{project_key}") or {}
    except JiraError:
        return None
    name = raw.get("name")
    return str(name) if name else None


# --------------------------------------------------------------------------- #
# boards
# --------------------------------------------------------------------------- #


def list_boards(
    client: JiraClient,
    project_key: str | None = None,
    board_type: str | None = None,
) -> list[dict]:
    """List agile boards, optionally restricted to a project and/or a type.

    Args:
        client: Configured Jira client.
        project_key: Restrict to the boards of this project. The server-side
            ``projectKeyOrId`` filter is used because board payloads on this
            server carry no project key of their own.
        board_type: ``"kanban"`` or ``"scrum"``.

    Returns:
        ``[{"id", "name", "type", "project"}]`` in server order.

    Raises:
        InputError: If ``board_type`` is not a known board type.
    """
    params: dict[str, Any] = {}
    key = _project_key(project_key) if project_key else None
    if key:
        params["projectKeyOrId"] = key
    if board_type:
        params["type"] = _board_type(board_type)
    raw = client.paginate(f"{AGILE}/board", params or None, items_key="values")
    return [_board_row(b, key) for b in raw if isinstance(b, dict)]


def get_board(client: JiraClient, board_id: int) -> dict:
    """Fetch one board.

    Args:
        client: Configured Jira client.
        board_id: Numeric board id.

    Returns:
        ``{"id", "name", "type", "project", "filter_id"}``. ``filter_id`` is
        ``None`` when the server does not report the board's saved filter.

    Raises:
        InputError: If ``board_id`` is not a positive integer.
        NotFoundError: If the board does not exist (raised by the client).
    """
    identifier = _board_id(board_id)
    raw = client.get(f"{AGILE}/board/{identifier}") or {}
    row = _board_row(raw)
    row["id"] = row["id"] if row["id"] is not None else identifier
    filter_id = (raw.get("filter") or {}).get("id") if isinstance(raw.get("filter"), dict) else None
    row["filter_id"] = filter_id
    return row


def board_for_project(client: JiraClient, project_key: str) -> dict | None:
    """Pick the one board that best represents ``project_key``.

    Selection rule (documented because the server gives us nothing better):

    1. Ask the server for the project's boards with ``projectKeyOrId``; this is
       the only reliable project-to-board link on Jira Server 10.3.
    2. If exactly one board comes back, use it.
    3. Otherwise prefer the first board whose **name contains the project key**
       (case-insensitive) -- the convention Jira itself follows when it
       auto-creates a board for a new project.
    4. Then prefer the first board whose name contains the **project display
       name** (case-insensitive). The project is only fetched for this tie-break,
       and a failure to fetch it is ignored.
    5. Otherwise fall back to the **first** board in server order.

    Args:
        client: Configured Jira client.
        project_key: Project key (case-insensitive).

    Returns:
        The board dict, or ``None`` when the project has no board at all.

    Raises:
        InputError: If ``project_key`` is blank.
    """
    key = _project_key(project_key)
    candidates = list_boards(client, key)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    needle = key.lower()
    for board in candidates:
        if needle in str(board.get("name") or "").lower():
            return board

    name = _project_name(client, key)
    if name:
        name_needle = name.strip().lower()
        if name_needle:
            for board in candidates:
                if name_needle in str(board.get("name") or "").lower():
                    return board

    return candidates[0]


def _create_board_filter(client: JiraClient, project_key: str, board_name: str) -> int:
    """Create the saved filter a new board needs and return its id."""
    jql = f'project = "{project_key}" ORDER BY Rank ASC'
    created = (
        client.post(
            f"{API}/filter",
            json={
                "name": f"{board_name} filter",
                "description": f"Filter backing the {board_name} board (created by jira-cli).",
                "jql": jql,
            },
        )
        or {}
    )
    filter_id = created.get("id")
    if filter_id is None:
        raise JiraError(f"Jira did not return a filter id for project {project_key}")
    return int(filter_id)


def ensure_board(
    client: JiraClient,
    project_key: str,
    board_type: str = "kanban",
    name: str | None = None,
) -> dict:
    """Return the project's board, creating one only when none exists.

    An existing board is **always** reused, whatever its type: a project with a
    board does not need a second one, and silently adding boards to projects is
    the kind of mess nobody cleans up. Creation only happens when
    :func:`board_for_project` finds nothing.

    Args:
        client: Configured Jira client.
        project_key: Project key (case-insensitive).
        board_type: Type to use **if** a board has to be created.
        name: Board name to use if one has to be created; defaults to
            ``"<KEY> board"``.

    Returns:
        ``{"board": {"id", "name", "type", "project"}, "created": bool}``.

    Raises:
        InputError: If the project key is blank or the board type is unknown.
        ReadOnlyError: If a board would have to be created while the client is
            configured read-only.
    """
    key = _project_key(project_key)
    wanted_type = _board_type(board_type)

    existing = board_for_project(client, key)
    if existing:
        return {"board": existing, "created": False}

    board_name = (name or "").strip() or f"{key} board"
    filter_id = _create_board_filter(client, key, board_name)
    created = (
        client.post(
            f"{AGILE}/board",
            json={"name": board_name, "type": wanted_type, "filterId": filter_id},
        )
        or {}
    )
    board = _board_row(created, key)
    if not board.get("name"):
        board["name"] = board_name
    if not board.get("type"):
        board["type"] = wanted_type
    return {"board": board, "created": True}


# --------------------------------------------------------------------------- #
# sprints
# --------------------------------------------------------------------------- #


def _sprint_states(state: str) -> list[str]:
    """Validate a comma-separated state filter; ``[]`` means every state."""
    wanted = (state or "").strip().lower()
    if not wanted or wanted == "all":
        return []
    parts = [p.strip() for p in wanted.split(",") if p.strip()]
    unknown = [p for p in parts if p not in _SPRINT_STATES]
    if unknown:
        raise InputError(
            f"unknown sprint state(s) {', '.join(unknown)}: "
            f"choose from {', '.join(_SPRINT_STATES)}"
        )
    return parts


def _agile_sprints(client: JiraClient, board_id: int, states: list[str]) -> list[dict]:
    """Sprints from the public agile API (empty on some Server boards)."""
    params = {"state": ",".join(states)} if states else None
    raw = client.paginate(f"{AGILE}/board/{board_id}/sprint", params, items_key="values")
    return [_sprint_row(s) for s in raw if isinstance(s, dict)]


def _gh_row(raw: dict, state_key: str = "state") -> dict:
    """Normalise a GreenHopper sprint entry; states are lower-cased."""
    row = _sprint_row(raw)
    row["state"] = str(raw.get(state_key) or raw.get("state") or "").lower() or None
    return row


def _sprintquery(client: JiraClient, board_id: int) -> list[dict]:
    """``GET /rest/greenhopper/1.0/sprintquery/{boardId}`` -> sprint rows.

    Shape (not captured in the fixtures, documented by GreenHopper):
    ``{"rapidViewId", "sprints": [{"id", "name", "state", "sequence", "goal"}]}``.
    """
    payload = client.get(
        f"{GREENHOPPER}/sprintquery/{board_id}",
        {"includeHistoricSprints": "true", "includeFutureSprints": "true"},
    )
    sprints = (payload or {}).get("sprints") if isinstance(payload, dict) else None
    return [_gh_row(s) for s in sprints or [] if isinstance(s, dict) and s.get("id") is not None]


def _board_name(client: JiraClient, board_id: int) -> str | None:
    """Board display name, ``None`` when the board cannot be read."""
    try:
        return get_board(client, board_id).get("name") or None
    except JiraError:
        return None


def _picker(client: JiraClient, board_id: int) -> list[dict]:
    """``GET /rest/greenhopper/1.0/sprint/picker?rapidViewId=`` -> sprint rows.

    Captured shape: ``{"suggestions": [...], "allMatches": [{"id", "name",
    "stateKey", "boardName"?, "date"}]}``.  The picker also lists sprints of
    OTHER boards, so entries are kept only when their ``boardName`` equals this
    board's name (entries without a ``boardName`` are dropped as unattributable).
    """
    payload = client.get(f"{GREENHOPPER}/sprint/picker", {"rapidViewId": board_id})
    if not isinstance(payload, dict):
        return []
    name = _board_name(client, board_id)
    if not name:
        return []
    rows: list[dict] = []
    seen: set[Any] = set()
    for bucket in ("suggestions", "allMatches"):
        for item in payload.get(bucket) or []:
            if not isinstance(item, dict) or item.get("id") in seen:
                continue
            if str(item.get("boardName") or "") != name:
                continue
            seen.add(item.get("id"))
            rows.append(_gh_row(item, state_key="stateKey"))
    return rows


def _enrich(client: JiraClient, rows: list[dict]) -> list[dict]:
    """Fill dates/goal from ``/rest/agile/1.0/sprint/{id}``, best effort.

    That endpoint returns proper ISO dates on this server even when the board
    sprint list is empty.  A failure leaves the row as it was.
    """
    for row in rows[:_ENRICH_LIMIT]:
        if row.get("start") and row.get("end"):
            continue
        try:
            sprint_id = ids.validate_int_id(row.get("id"), what="sprint id")
            detail = client.get(f"{AGILE}/sprint/{sprint_id}") or {}
        except JiraError:  # InputError included: a malformed id is skipped
            continue
        if not isinstance(detail, dict):
            continue
        for key, value in _sprint_row(detail).items():
            if row.get(key) in (None, "") and value not in (None, ""):
                row[key] = value
    return rows


def greenhopper_sprints(
    client: JiraClient, board_id: int, state: str = "active,future"
) -> dict:
    """Discover a board's sprints through GreenHopper only.

    Tries ``sprintquery/{boardId}`` first (board-scoped, every state), then the
    sprint picker filtered to this board's name.  Every GreenHopper failure is
    swallowed: these endpoints are unversioned and unsupported.

    Returns:
        ``{"sprints": [sprint rows], "method": "greenhopper-sprintquery" |
        "greenhopper-picker" | "none"}``.
    """
    identifier = _board_id(board_id)
    states = _sprint_states(state)
    for method, source in ((SPRINTS_VIA_SPRINTQUERY, _sprintquery), (SPRINTS_VIA_PICKER, _picker)):
        try:
            rows = source(client, identifier)
        except JiraError:
            continue
        if not rows:
            continue
        if states:
            rows = [r for r in rows if r.get("state") in states]
        return {"sprints": _enrich(client, rows), "method": method}
    return {"sprints": [], "method": SPRINTS_VIA_NONE}


def find_sprints(client: JiraClient, board_id: int, state: str = "active,future") -> dict:
    """Like :func:`list_sprints`, but also says which source answered.

    Agile first; when it returns nothing the GreenHopper fallback of
    :func:`greenhopper_sprints` runs.

    Returns:
        ``{"sprints": [...], "method": "agile" | "greenhopper-sprintquery" |
        "greenhopper-picker" | "none"}``.

    Raises:
        InputError: If the board id is invalid or ``state`` names an unknown
            sprint state.
    """
    identifier = _board_id(board_id)
    states = _sprint_states(state)
    rows = _agile_sprints(client, identifier, states)
    if rows:
        return {"sprints": rows, "method": SPRINTS_VIA_AGILE}
    return greenhopper_sprints(client, identifier, ",".join(states))


def list_sprints(
    client: JiraClient, board_id: int, state: str = "active,future"
) -> list[dict]:
    """List a board's sprints.

    Args:
        client: Configured Jira client.
        board_id: Numeric board id.
        state: Comma-separated subset of ``active``, ``future``, ``closed``.
            An empty string (or ``"all"``) asks the server for every sprint.

    Returns:
        ``[{"id", "name", "state", "start", "end", "goal"}]`` in server order.
        When the agile list is empty the rows come from GreenHopper (see
        :func:`find_sprints`, which also reports the source).

    Raises:
        InputError: If the board id is invalid or ``state`` names an unknown
            sprint state.
    """
    return find_sprints(client, board_id, state)["sprints"]


def active_sprint(client: JiraClient, board_id: int) -> dict | None:
    """Return the board's active sprint, or ``None``.

    A board can in theory report several active sprints (parallel sprints); the
    first one in server order wins.  Uses the GreenHopper fallback of
    :func:`list_sprints` when the agile list is empty.
    """
    for sprint in list_sprints(client, board_id, state="active"):
        if str(sprint.get("state") or "").lower() == "active":
            return sprint
    return None


def create_sprint(
    client: JiraClient,
    board_id: int,
    name: str,
    start: str | None = None,
    end: str | None = None,
    goal: str | None = None,
) -> dict:
    """Create a sprint on a scrum board.

    Args:
        client: Configured Jira client.
        board_id: Origin board id; must be a **scrum** board.
        name: Sprint name (required, non-blank).
        start: Optional start date/time accepted by Jira (``YYYY-MM-DD`` works).
        end: Optional end date/time.
        goal: Optional sprint goal.

    Returns:
        The normalised sprint dict, plus ``"board_id"``.

    Raises:
        InputError: On a blank name, an invalid board id, or a kanban board --
            kanban boards have no sprints at all, they have a backlog and a
            continuous flow, so use :func:`move_to_backlog` instead.
        ReadOnlyError: If the client is configured read-only.
    """
    identifier = _board_id(board_id)
    sprint_name = (name or "").strip()
    if not sprint_name:
        raise InputError("a sprint name is required")

    board = get_board(client, identifier)
    kind = str(board.get("type") or "").lower()
    if kind and kind != "scrum":
        raise InputError(
            f"board {identifier} ({board.get('name')}) is a {kind} board and has no sprints: "
            "only scrum boards support sprints -- use the backlog instead"
        )

    payload: dict[str, Any] = {"name": sprint_name, "originBoardId": identifier}
    if start:
        payload["startDate"] = start
    if end:
        payload["endDate"] = end
    if goal:
        payload["goal"] = goal
    created = client.post(f"{AGILE}/sprint", json=payload) or {}
    row = _sprint_row(created)
    if not row.get("name"):
        row["name"] = sprint_name
    row["board_id"] = identifier
    return row


def add_to_sprint(client: JiraClient, sprint_id: int, keys: list[str]) -> dict:
    """Move issues into a sprint.

    Args:
        client: Configured Jira client.
        sprint_id: Target sprint id.
        keys: Issue keys; duplicates are dropped, order is kept.

    Returns:
        ``{"sprint_id", "issues": [keys], "moved": int}``.

    Raises:
        InputError: On an invalid sprint id or an empty/invalid key list.
        ReadOnlyError: If the client is configured read-only.
    """
    identifier = _sprint_id(sprint_id)
    issue_keys = _issue_keys(keys)
    client.post(f"{AGILE}/sprint/{identifier}/issue", json={"issues": issue_keys})
    return {"sprint_id": identifier, "issues": issue_keys, "moved": len(issue_keys)}


def move_to_backlog(client: JiraClient, keys: list[str]) -> dict:
    """Move issues out of any sprint and back to the backlog.

    Args:
        client: Configured Jira client.
        keys: Issue keys; duplicates are dropped, order is kept.

    Returns:
        ``{"issues": [keys], "moved": int}``.

    Raises:
        InputError: On an empty or invalid key list.
        ReadOnlyError: If the client is configured read-only.
    """
    issue_keys = _issue_keys(keys)
    client.post(f"{AGILE}/backlog/issue", json={"issues": issue_keys})
    return {"issues": issue_keys, "moved": len(issue_keys)}


# --------------------------------------------------------------------------- #
# board issues
# --------------------------------------------------------------------------- #


def board_issues(
    client: JiraClient, board_id: int, jql: str | None = None, limit: int = 200
) -> list[dict]:
    """List a board's issues, normalised like every other issue in this CLI.

    Args:
        client: Configured Jira client.
        board_id: Numeric board id.
        jql: Optional extra JQL, ANDed by the server with the board's filter.
        limit: Maximum number of issues to collect.

    Returns:
        Normalised issue dicts (see ``services.issues.normalize_issue``).

    Raises:
        InputError: If the board id is invalid or ``limit`` is not positive.
    """
    identifier = _board_id(board_id)
    try:
        page_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise InputError(f"invalid limit {limit!r}: expected an integer") from exc
    if page_limit <= 0:
        raise InputError(f"invalid limit {limit!r}: must be positive")

    params: dict[str, Any] = {}
    if jql and jql.strip():
        params["jql"] = jql.strip()
    raw = client.paginate(
        f"{AGILE}/board/{identifier}/issue",
        params or None,
        items_key="issues",
        limit=page_limit,
    )

    # Lazy import: services.issues pulls in the field map, which this module
    # does not otherwise need, and a module-level import would also make the
    # import order between the two service modules matter.
    from ..fields import FieldMap
    from . import issues as issues_service

    # One field map for the whole page: normalize_issue would otherwise build
    # (and re-fetch) one per issue.
    field_map = FieldMap(client)
    return [issues_service.normalize_issue(raw_issue, client, field_map) for raw_issue in raw]
