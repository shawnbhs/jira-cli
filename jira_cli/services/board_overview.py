"""Per-board overview: how many issues sit in each column, and which ones.

Two entry points:

* :func:`list_board_summaries` -- one cheap row per board (counts only).
* :func:`board_overview` -- one board, column by column, with issue rows
  (title, assignee, reporter, ...).

How a board is read on Jira Software **Server** (shapes captured live):

* ``GET /rest/agile/1.0/board/{id}/configuration`` lists the columns in board
  order; each column carries status **ids only** (``{"id", "self"}``).  Names
  come from ``GET /rest/api/2/status``.
* Counting never fetches issues: ``GET /board/{id}/issue?maxResults=0&jql=...``
  answers with the server-side ``total`` (the server clamps ``maxResults`` to 1,
  which is harmless).  The board's own filter is ANDed in by the server.
* **Scrum**: columns count the issues of the ACTIVE sprint(s) only
  (``sprint in (<ids>)``); the backlog is ``GET /board/{id}/backlog``.  With no
  active sprint every column is 0 and no column query is sent.
* **Kanban**: columns count every board issue.  ``columns[0]`` of a kanban
  configuration is the *Kanban backlog* slot -- on this server it is present on
  every kanban board, named "Backlog" and status-less while the kanban backlog
  is disabled.  Whether it is enabled is only exposed by GreenHopper
  (``rapidviewconfig/editmodel.json`` -> ``isKanPlanEnabled``).  When enabled,
  that column is reported ONLY as ``backlog`` (never also in ``columns``); when
  disabled, an empty slot is dropped and ``backlog`` is ``None``.
  ``/board/{id}/backlog`` answers HTTP 400 for kanban boards and is not used.
* Issues whose status is mapped to no column are reported as ``unmapped`` so the
  per-column numbers always add up to ``total``.
* A board's **project**: the board list carries no ``location`` on this server,
  so an unfiltered summary asks ``GET /board/{id}/project`` (live: answered for
  29 of 44 boards, empty for the rest) and falls back to the project named by
  the board's saved-filter JQL (``GET /rest/api/2/filter/{id}`` -> ``project =
  KEY ...``; the filter id comes from the configuration already fetched).

Nothing here prints or writes; failures surface as ``jira_cli.errors`` types.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import ids
from ..errors import InputError, JiraError
from . import boards as boards_service

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import JiraClient

__all__ = ["ISSUE_FIELDS", "board_overview", "list_board_summaries"]

API = "/rest/api/2"
AGILE = "/rest/agile/1.0"
GREENHOPPER = "/rest/greenhopper/1.0"

#: The only fields fetched per issue; enough for an overview row.
ISSUE_FIELDS = "summary,status,issuetype,priority,assignee,reporter,updated"

#: Field asked for on count-only queries, to keep the clamped 1-issue page tiny.
_COUNT_FIELDS = "status"


@dataclass(frozen=True)
class _Column:
    name: str
    status_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Layout:
    """A board's columns, with the kanban backlog column split out."""

    columns: tuple[_Column, ...]
    backlog_column: _Column | None
    filter_id: str | None = None

    @property
    def mapped_ids(self) -> tuple[str, ...]:
        every = list(self.columns)
        if self.backlog_column is not None:
            every.append(self.backlog_column)
        return tuple(sid for column in every for sid in column.status_ids)


# --------------------------------------------------------------------------- #
# validation and JQL
# --------------------------------------------------------------------------- #


def _positive_int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise InputError(f"invalid {what} {value!r}: expected a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InputError(f"invalid {what} {value!r}: expected a positive integer") from None
    if number <= 0:
        raise InputError(f"invalid {what} {value!r}: must be positive")
    return number


def _jql_value(status_id: str) -> str:
    """A status id as a JQL literal; ids are numeric, anything else is quoted."""
    if status_id.isdigit():
        return status_id
    escaped = status_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _status_clause(status_ids: tuple[str, ...], *, exclude: bool = False) -> str:
    operator = "not in" if exclude else "in"
    return f"status {operator} ({','.join(_jql_value(s) for s in status_ids)})"


def _all_of(*clauses: str | None) -> str | None:
    present = [c for c in clauses if c]
    return " AND ".join(f"({c})" if len(present) > 1 else c for c in present) or None


# --------------------------------------------------------------------------- #
# reading the board
# --------------------------------------------------------------------------- #


def _parse_columns(config: Any) -> list[_Column]:
    column_config = (config or {}).get("columnConfig") if isinstance(config, dict) else None
    raw_columns = (column_config or {}).get("columns") or []
    columns: list[_Column] = []
    for raw in raw_columns:
        if not isinstance(raw, dict):
            continue
        status_ids = tuple(
            str(s["id"]) for s in raw.get("statuses") or [] if isinstance(s, dict) and s.get("id")
        )
        columns.append(_Column(name=str(raw.get("name") or ""), status_ids=status_ids))
    return columns


def _kanban_backlog_enabled(client: JiraClient, board_id: int) -> bool:
    """GreenHopper's ``isKanPlanEnabled``; ``False`` when it cannot be read.

    The endpoint is unversioned, so any failure means "no kanban backlog"
    rather than a failed overview.
    """
    try:
        payload = client.get(f"{GREENHOPPER}/rapidviewconfig/editmodel.json", {"rapidViewId": board_id})
    except JiraError:
        return False
    return isinstance(payload, dict) and payload.get("isKanPlanEnabled") is True


def _filter_id(config: Any) -> str | None:
    saved_filter = config.get("filter") if isinstance(config, dict) else None
    value = saved_filter.get("id") if isinstance(saved_filter, dict) else None
    return str(value) if value is not None and str(value).isdigit() else None


def _load_layout(client: JiraClient, board_id: int, board_type: str) -> _Layout:
    config = client.get(f"{AGILE}/board/{board_id}/configuration")
    columns = _parse_columns(config)
    filter_id = _filter_id(config)
    if board_type != "kanban" or not columns:
        return _Layout(tuple(columns), None, filter_id)
    if _kanban_backlog_enabled(client, board_id):
        return _Layout(tuple(columns[1:]), columns[0], filter_id)
    if not columns[0].status_ids:  # the disabled kanban-backlog slot
        columns = columns[1:]
    return _Layout(tuple(columns), None, filter_id)


def _active_sprints(client: JiraClient, board_id: int, board_type: str) -> list[dict]:
    if board_type != "scrum":
        return []
    rows = boards_service.list_sprints(client, board_id, state="active")
    return [
        {"id": r.get("id"), "name": r.get("name"), "state": r.get("state"), "end_date": r.get("end")}
        for r in rows
        if str(r.get("state") or "").lower() == "active" and r.get("id") is not None
    ]


def _status_names(client: JiraClient) -> dict[str, str]:
    """``{status id: name}``; empty when the list cannot be read (ids are shown)."""
    try:
        payload = client.get(f"{API}/status")
    except JiraError:
        return {}
    if not isinstance(payload, list):
        return {}
    return {
        str(s["id"]): str(s.get("name") or s["id"])
        for s in payload
        if isinstance(s, dict) and s.get("id") is not None
    }


def _total_of(payload: Any) -> int:
    total = payload.get("total") if isinstance(payload, dict) else None
    return int(total) if isinstance(total, int) else 0


# --------------------------------------------------------------------------- #
# which project a board belongs to
# --------------------------------------------------------------------------- #

#: A plain project key as it appears in JQL (ids and odd tokens are rejected).
_JQL_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

#: ``project = KEY`` or ``project in (A, B)`` at the very start of a filter.
_PROJECT_CLAUSE = re.compile(
    r"""^\s*project\s*(?:=\s*(?P<one>"[^"]*"|'[^']*'|[^\s()]+)|in\s*\((?P<many>[^)]*)\))""",
    re.IGNORECASE,
)
_ORDER_BY = re.compile(r"\border\s+by\b.*\Z", re.IGNORECASE | re.DOTALL)
_OR = re.compile(r"\bor\b", re.IGNORECASE)


def _project_keys_in_jql(jql: Any) -> list[str]:
    """Project keys a board filter is restricted to, or ``[]`` when unsure.

    Deliberately conservative: only a leading ``project = KEY`` /
    ``project in (KEY, ...)`` clause counts, any ``OR`` makes the restriction
    unknowable, and numeric project ids (no cheap way to name them) give up.
    """
    if not isinstance(jql, str):
        return []
    where = _ORDER_BY.sub("", jql)
    match = _PROJECT_CLAUSE.match(where)
    if match is None or _OR.search(where):
        return []
    raw = [match.group("one")] if match.group("one") else match.group("many").split(",")
    keys = [value.strip().strip("\"'") for value in raw]
    if not keys or not all(_JQL_KEY.fullmatch(key) for key in keys):
        return []
    return list(dict.fromkeys(key.upper() for key in keys))


def _agile_project_keys(client: JiraClient, board_id: int) -> list[str]:
    payload = client.get(f"{AGILE}/board/{board_id}/project")
    values = payload.get("values") if isinstance(payload, dict) else None
    keys = [str(v["key"]) for v in values or [] if isinstance(v, dict) and v.get("key")]
    return list(dict.fromkeys(keys))


def _filter_project_keys(client: JiraClient, filter_id: str | None) -> list[str]:
    if filter_id is None:
        return []
    payload = client.get(f"{API}/filter/{filter_id}")
    return _project_keys_in_jql(payload.get("jql") if isinstance(payload, dict) else None)


def _board_project(client: JiraClient, board_id: int, filter_id: str | None) -> str | None:
    """``"KEY"`` (or ``"A,B"``) for a board, ``None`` when it cannot be told.

    The agile ``/project`` answer is authoritative when present; the saved
    filter's JQL is the fallback for boards it leaves empty.  A failed lookup
    only costs the project cell, never the summary row.
    """
    for lookup in (
        lambda: _agile_project_keys(client, board_id),
        lambda: _filter_project_keys(client, filter_id),
    ):
        try:
            keys = lookup()
        except JiraError:
            continue
        if keys:
            return ",".join(keys)
    return None


# --------------------------------------------------------------------------- #
# issue rows
# --------------------------------------------------------------------------- #


def _named(value: Any) -> str | None:
    return str(value["name"]) if isinstance(value, dict) and value.get("name") else None


def _person(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    username = value.get("name") or value.get("key")
    display = value.get("displayName") or username
    return (str(display) if display else None), (str(username) if username else None)


def _issue_row(raw: Any) -> dict:
    fields = (raw.get("fields") if isinstance(raw, dict) else None) or {}
    assignee, assignee_username = _person(fields.get("assignee"))
    reporter, reporter_username = _person(fields.get("reporter"))
    return {
        "key": raw.get("key") if isinstance(raw, dict) else None,
        "summary": fields.get("summary"),
        "status": _named(fields.get("status")),
        "type": _named(fields.get("issuetype")),
        "priority": _named(fields.get("priority")),
        "assignee": assignee,
        "assignee_username": assignee_username,
        "reporter": reporter,
        "reporter_username": reporter_username,
        "updated": fields.get("updated"),
    }


# --------------------------------------------------------------------------- #
# counting / fetching one bucket
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Reader:
    """Counts and lists one board's issues for a given JQL bucket."""

    client: JiraClient
    board_id: int
    include_issues: bool
    limit: int

    def board_bucket(self, jql: str | None) -> dict:
        return self._bucket(f"{AGILE}/board/{self.board_id}/issue", jql)

    def backlog_bucket(self) -> dict:
        return self._bucket(f"{AGILE}/board/{self.board_id}/backlog", None)

    def _bucket(self, path: str, jql: str | None) -> dict:
        params: dict[str, Any] = {"jql": jql} if jql else {}
        count = _total_of(self.client.get(path, {**params, "maxResults": 0, "fields": _COUNT_FIELDS}))
        bucket: dict[str, Any] = {"count": count, "issues": []}
        if not self.include_issues or count == 0:
            return bucket
        raw = self.client.paginate(
            path, {**params, "fields": ISSUE_FIELDS}, items_key="issues", limit=self.limit
        )
        bucket["issues"] = [_issue_row(item) for item in raw]
        if count > len(bucket["issues"]):
            bucket["truncated"] = True
        return bucket


def _empty_bucket() -> dict:
    return {"count": 0, "issues": []}


def _build_overview(
    client: JiraClient,
    board: dict,
    *,
    include_issues: bool,
    limit: int,
    status_names: dict[str, str],
    layout: _Layout | None = None,
) -> dict:
    board_id = int(board["id"])
    board_type = str(board.get("type") or "").lower()
    if layout is None:
        layout = _load_layout(client, board_id, board_type)
    sprints = _active_sprints(client, board_id, board_type)
    reader = _Reader(client, board_id, include_issues, limit)

    # Scrum columns show only the active sprint(s); with none, nothing is on the board.
    has_scope = board_type != "scrum" or bool(sprints)
    scope = f"sprint in ({','.join(str(int(s['id'])) for s in sprints)})" if sprints else None

    def bucket_for(status_ids: tuple[str, ...]) -> dict:
        if not has_scope or not status_ids:
            return _empty_bucket()
        return reader.board_bucket(_all_of(scope, _status_clause(status_ids)))

    columns = [
        {
            "name": column.name,
            "statuses": [status_names.get(s, s) for s in column.status_ids],
            **bucket_for(column.status_ids),
        }
        for column in layout.columns
    ]

    if board_type == "scrum":
        backlog: dict | None = reader.backlog_bucket()
    elif layout.backlog_column is not None:
        backlog = bucket_for(layout.backlog_column.status_ids)
    else:
        backlog = None

    mapped = layout.mapped_ids
    if not has_scope:
        unmapped = _empty_bucket()
    else:
        unmapped = reader.board_bucket(
            _all_of(scope, _status_clause(mapped, exclude=True) if mapped else None)
        )

    total = sum(c["count"] for c in columns) + unmapped["count"]
    total += backlog["count"] if backlog else 0
    return {
        "board": {k: board.get(k) for k in ("id", "name", "type", "project")},
        "sprints": sprints,
        "columns": columns,
        "backlog": backlog,
        "unmapped": unmapped,
        "total": total,
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def board_overview(
    client: JiraClient, board_id: int, *, include_issues: bool = True, limit: int = 300
) -> dict:
    """One board, column by column.

    Args:
        client: Configured Jira client.
        board_id: Numeric board id.
        include_issues: When ``False`` only counts are filled; issue lists stay empty.
        limit: Maximum issues listed per column / backlog / unmapped bucket.
            A bucket holding more gets ``"truncated": True``.

    Returns:
        ``{"board": {"id", "name", "type", "project"},
        "sprints": [{"id", "name", "state", "end_date"}],
        "columns": [{"name", "statuses": [str], "count", "issues": [row]}],
        "backlog": {"count", "issues"} | None,
        "unmapped": {"count", "issues"}, "total": int}`` where a row is
        ``{"key", "summary", "status", "type", "priority", "assignee",
        "assignee_username", "reporter", "reporter_username", "updated"}``.
        ``sprints`` is ``[]`` for kanban.  A kanban backlog column appears only
        under ``backlog``, never also in ``columns``.

    Raises:
        InputError: invalid ``board_id`` or ``limit``.
        NotFoundError: the board does not exist or is not visible.
    """
    identifier = ids.validate_int_id(board_id, what="board id")
    page_limit = _positive_int(limit, "limit")
    board = boards_service.get_board(client, identifier)
    board["id"] = identifier
    return _build_overview(
        client,
        board,
        include_issues=include_issues,
        limit=page_limit,
        status_names=_status_names(client),
    )


def _summary_row(client: JiraClient, board: dict) -> dict:
    row: dict[str, Any] = {
        "id": board.get("id"),
        "name": board.get("name"),
        "type": board.get("type"),
        "project": board.get("project"),
        "total": 0,
        "backlog": None,
        "columns": [],
        "sprint": None,
        "error": None,
    }
    try:
        identifier = ids.validate_int_id(board.get("id"), what="board id")
        layout = _load_layout(client, identifier, str(board.get("type") or "").lower())
        overview = _build_overview(
            client,
            {**board, "id": identifier},
            include_issues=False,
            limit=1,
            status_names={},
            layout=layout,
        )
    except JiraError as exc:  # one broken board must not sink the whole list
        row["error"] = str(exc)
        return row
    backlog = overview["backlog"]
    row.update(
        total=overview["total"],
        backlog=backlog["count"] if backlog else None,
        columns=[{"name": c["name"], "count": c["count"]} for c in overview["columns"]],
        sprint=", ".join(str(s["name"]) for s in overview["sprints"]) or None,
    )
    if not row["project"]:
        row["project"] = _board_project(client, identifier, layout.filter_id)
    return row


def list_board_summaries(
    client: JiraClient, project: str | None = None, *, max_workers: int = 6
) -> list[dict]:
    """One count-only row per board, in server order.

    Args:
        client: Configured Jira client.
        project: Project key/id to restrict to; ``None`` lists every visible board.
        max_workers: Boards summarised concurrently (``1`` = sequential).

    Returns:
        ``[{"id", "name", "type", "project", "total", "backlog": int | None,
        "columns": [{"name", "count"}], "sprint": str | None,
        "error": str | None}]``.  A board that fails to load gets ``error`` set,
        zero counts and empty ``columns``; it never aborts the list.
        ``project`` is the asked-for key when ``project`` is given; otherwise
        it is looked up per board (``"KEY"``, ``"A,B"`` for a multi-project
        board, or ``None`` when neither the agile API nor the filter JQL tells).

    Raises:
        InputError: invalid ``project`` or ``max_workers``.
    """
    workers = _positive_int(max_workers, "max_workers")
    board_rows = boards_service.list_boards(client, project)
    if workers == 1 or len(board_rows) <= 1:
        return [_summary_row(client, b) for b in board_rows]
    with ThreadPoolExecutor(max_workers=min(workers, len(board_rows))) as pool:
        return list(pool.map(lambda b: _summary_row(client, b), board_rows))
