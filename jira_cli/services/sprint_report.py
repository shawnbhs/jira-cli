"""Sprint report and velocity services.

Two data sources exist for sprint analytics on Jira Server:

* the Greenhopper "rapid chart" endpoints, which are the only ones that know
  which issues were *removed from* or *added to* a sprint after it started, and
* the public Agile API, which only lists a sprint's current issues.

Both are implemented. The Greenhopper endpoint is tried first and the Agile API
is the fallback; the report always records which one produced it in
``meta["method"]`` so a caller can tell a complete report from an approximate
one.  ``meta["sprint_discovery"]`` records how the sprint id was found
(``explicit``, ``agile`` or a GreenHopper fallback -- on Jira Server 10.3 the
agile board sprint list is often empty, see ``services.boards.find_sprints``).

GreenHopper's ``sprint`` block carries localised *display* dates
(``07/Sep/25 6:16 PM``); the ISO dates are read from
``/rest/agile/1.0/sprint/{id}`` when that answers, and
``meta["sprint_dates"]`` says which kind the report holds.
"""

from __future__ import annotations

from typing import Any

from jira_cli.errors import InputError, JiraError
from jira_cli.fields import FieldMap
from jira_cli.report import make_report

GREENHOPPER_SPRINT_REPORT = "/rest/greenhopper/1.0/rapid/charts/sprintreport"
GREENHOPPER_VELOCITY = "/rest/greenhopper/1.0/rapid/charts/velocity"

#: Produced by the Greenhopper sprint report endpoint.
METHOD_GREENHOPPER = "greenhopper"
#: Produced by the public Agile API; removals and additions are not knowable.
METHOD_AGILE = "agile"

SPRINT_COLUMNS = [
    "key",
    "summary",
    "type",
    "status",
    "assignee",
    "story_points",
    "category",
    "added_after_start",
]
VELOCITY_COLUMNS = ["sprint_id", "sprint", "state", "committed", "completed"]

#: ``meta["sprint_discovery"]`` when the caller passed the sprint id.
DISCOVERY_EXPLICIT = "explicit"
DISCOVERY_AGILE = "agile"

CATEGORY_COMPLETED = "completed"
CATEGORY_NOT_COMPLETED = "not_completed"
CATEGORY_REMOVED = "removed"


def sprint_report(client, board_id: int, sprint_id: int | None = None) -> dict:
    """Report what a sprint committed to and what it actually finished.

    Args:
        client: a ``JiraClient``.
        board_id: the board (rapid view) the sprint belongs to.
        sprint_id: the sprint to report on; defaults to the board's active
            sprint.

    Returns:
        A report dict (see ``jira_cli.report.make_report``). ``meta["method"]``
        is ``"greenhopper"`` for a full report or ``"agile"`` when the fallback
        was used, in which case removals and late additions read as zero.

    Raises:
        InputError: ``sprint_id`` was omitted and the board has no active
            sprint.
        JiraError: both data sources failed.
    """
    if sprint_id is not None:
        resolved_id, discovery = sprint_id, DISCOVERY_EXPLICIT
    else:
        resolved_id, discovery = _active_sprint_id(client, board_id)
    try:
        report = _greenhopper_sprint_report(client, board_id, resolved_id)
    except JiraError as exc:
        report = _agile_sprint_report(client, board_id, resolved_id, fallback_reason=str(exc))
    report["meta"]["sprint_discovery"] = discovery
    return report


def velocity(client, board_id: int, last: int = 5) -> dict:
    """Report committed vs completed work for a board's most recent sprints.

    Args:
        client: a ``JiraClient``.
        board_id: the board (rapid view) to measure.
        last: how many of the most recent sprints to include.

    Returns:
        A report dict. With ``meta["method"] == "greenhopper"`` the values are
        the board's estimation statistic (story points on most boards); with
        ``"agile"`` they are issue counts, because the estimate history is not
        available from the public API.

    Raises:
        InputError: ``last`` is not a positive integer.
    """
    if not isinstance(last, int) or isinstance(last, bool) or last < 1:
        raise InputError("last must be a positive integer")
    try:
        return _greenhopper_velocity(client, board_id, last)
    except JiraError as exc:
        return _agile_velocity(client, board_id, last, fallback_reason=str(exc))


# --------------------------------------------------------------------------- #
# sprint report
# --------------------------------------------------------------------------- #


def _active_sprint_id(client, board_id: int) -> tuple[int, str]:
    """Return ``(active sprint id, discovery method)`` or raise ``InputError``.

    The agile board sprint list is asked first; when it is empty (as observed
    for every scrum board on Jira Server 10.3) GreenHopper is asked through
    ``services.boards.greenhopper_sprints``.
    """
    payload = client.get(f"/rest/agile/1.0/board/{board_id}/sprint", {"state": "active"})
    values = (payload or {}).get("values") if isinstance(payload, dict) else None
    for value in values or []:
        if isinstance(value, dict) and value.get("id") is not None:
            return value.get("id"), DISCOVERY_AGILE

    from jira_cli.services import boards

    found = boards.greenhopper_sprints(client, board_id, "active")
    for sprint in found["sprints"]:
        if sprint.get("id") is not None:
            return sprint["id"], found["method"]
    raise InputError(
        f"board {board_id} has no active sprint (asked agile and GreenHopper); "
        "pass a sprint id explicitly"
    )


def _greenhopper_sprint_report(client, board_id: int, sprint_id: int) -> dict:
    """Build the report from the Greenhopper sprint report endpoint."""
    payload = client.get(
        GREENHOPPER_SPRINT_REPORT,
        {"rapidViewId": board_id, "sprintId": sprint_id},
    ) or {}
    contents = payload.get("contents") if isinstance(payload, dict) else None
    if not isinstance(contents, dict):
        # A 200 without the chart body (HTML login page, plugin disabled) must
        # not read as an empty sprint; let the agile fallback answer instead.
        raise JiraError("GreenHopper sprint report returned no contents")
    added_raw = contents.get("issueKeysAddedDuringSprint")
    # A dict keyed by issue key on this server; tolerate a list just in case.
    added_keys = set(added_raw.keys() if isinstance(added_raw, dict) else added_raw or [])

    rows: list[dict] = []
    for category, source_key in (
        (CATEGORY_COMPLETED, "completedIssues"),
        (CATEGORY_NOT_COMPLETED, "issuesNotCompletedInCurrentSprint"),
        (CATEGORY_REMOVED, "puntedIssues"),
    ):
        for raw in contents.get(source_key) or []:
            if isinstance(raw, dict):
                rows.append(_greenhopper_row(raw, category, added_keys))

    sprint, dates = _greenhopper_sprint_info(client, payload.get("sprint"), sprint_id)
    return make_report(
        _sprint_title(sprint, sprint_id),
        list(SPRINT_COLUMNS),
        rows,
        summary=_summarize(rows),
        meta={
            "method": METHOD_GREENHOPPER,
            "board_id": board_id,
            "sprint_id": sprint_id,
            "sprint": sprint,
            "sprint_dates": dates,
        },
    )


def _greenhopper_sprint_info(client, raw: Any, sprint_id: int) -> tuple[dict, str]:
    """Sprint info for a GreenHopper report, preferring agile ISO dates.

    Returns ``(sprint info, "iso" | "display")``.  The agile lookup is best
    effort; the GreenHopper block is kept for anything agile does not say.
    """
    shown = _sprint_info(raw if isinstance(raw, dict) else {})
    try:
        detail = client.get(f"/rest/agile/1.0/sprint/{sprint_id}")
    except JiraError:
        detail = None
    if not isinstance(detail, dict) or not detail:
        return shown, "display"
    iso = _sprint_info(detail)
    merged = {key: iso.get(key) if iso.get(key) not in (None, "") else shown.get(key) for key in shown}
    return merged, "iso"


def _greenhopper_row(raw: dict, category: str, added_keys: set[str]) -> dict:
    """Normalise one Greenhopper issue entry into a report row."""
    key = raw.get("key")
    return {
        "key": key,
        "summary": raw.get("summary"),
        "type": raw.get("typeName"),
        "status": raw.get("statusName"),
        "assignee": raw.get("assignee") or raw.get("assigneeName"),
        "story_points": _stat_value(raw),
        "category": category,
        "added_after_start": key in added_keys,
    }


def _stat_value(raw: dict) -> float | None:
    """Pull the board's estimation statistic off a Greenhopper issue entry."""
    for name in ("estimateStatistic", "currentEstimateStatistic"):
        value = ((raw.get(name) or {}).get("statFieldValue") or {}).get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _agile_sprint_report(
    client, board_id: int, sprint_id: int, *, fallback_reason: str
) -> dict:
    """Build the report from the public Agile API (no removal/addition data)."""
    points_field = _story_points_field(client)
    wanted = ["summary", "issuetype", "status", "assignee"]
    if points_field:
        # The field must be requested explicitly or Jira omits it and every
        # story point reads as None.
        wanted.append(points_field)
    issues = client.paginate(
        f"/rest/agile/1.0/sprint/{sprint_id}/issue",
        {"fields": ",".join(wanted)},
        items_key="issues",
    )
    rows = [_agile_row(raw, points_field) for raw in issues]

    try:
        detail = client.get(f"/rest/agile/1.0/sprint/{sprint_id}") or {}
    except JiraError:
        detail = {}
    sprint = _sprint_info(detail if isinstance(detail, dict) else {})
    return make_report(
        _sprint_title(sprint, sprint_id),
        list(SPRINT_COLUMNS),
        rows,
        summary=_summarize(rows),
        meta={
            "method": METHOD_AGILE,
            "board_id": board_id,
            "sprint_id": sprint_id,
            "sprint": sprint,
            "sprint_dates": "iso",
            "fallback_reason": fallback_reason,
        },
    )


def _agile_row(raw: dict, points_field: str | None) -> dict:
    """Normalise one Agile API issue into a report row."""
    fields = raw.get("fields") or {}
    assignee = fields.get("assignee") or {}
    return {
        "key": raw.get("key"),
        "summary": fields.get("summary"),
        "type": (fields.get("issuetype") or {}).get("name"),
        "status": (fields.get("status") or {}).get("name"),
        "assignee": assignee.get("name") or assignee.get("displayName"),
        "story_points": _points_from_fields(fields, points_field),
        "category": CATEGORY_COMPLETED if _is_done(fields) else CATEGORY_NOT_COMPLETED,
        # The public API cannot say whether an issue joined after sprint start.
        "added_after_start": False,
    }


def _is_done(fields: dict) -> bool:
    """True when the issue's status sits in the Done status category."""
    category = (fields.get("status") or {}).get("statusCategory") or {}
    return str(category.get("key") or category.get("name") or "").lower() == "done"


def _points_from_fields(fields: dict, points_field: str | None) -> float | None:
    """Read the story points custom field off a raw issue's fields."""
    if not points_field:
        return None
    value = fields.get(points_field)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _story_points_field(client) -> str | None:
    """Discover the story points field id, tolerating a failed field lookup."""
    try:
        return FieldMap(client).story_points
    except JiraError:
        return None


def _sprint_info(raw: dict) -> dict:
    """Reduce a sprint payload to the few fields a report shows.

    Empty strings (GreenHopper's ``completeDate: ""``) and Java's ``<null>``
    read as ``None``.
    """

    def clean(value: Any) -> Any:
        return None if value in ("", "<null>") else value

    return {
        "id": raw.get("id"),
        "name": clean(raw.get("name")),
        "state": clean(raw.get("state")),
        "start": clean(raw.get("startDate")),
        "end": clean(raw.get("endDate")),
        "complete": clean(raw.get("completeDate")),
        "goal": clean(raw.get("goal")),
    }


def _sprint_title(sprint: dict, sprint_id: int) -> str:
    """Human title for the report heading."""
    return f"Sprint report: {sprint.get('name') or sprint_id}"


def _summarize(rows: list[dict]) -> dict:
    """Count and, where available, sum story points per sprint outcome.

    ``committed`` is every issue in the sprint minus those added after it
    started, so ``completion_pct`` measures the sprint against its own plan
    rather than against work injected mid-flight.
    """
    by_category = {
        category: [r for r in rows if r["category"] == category]
        for category in (CATEGORY_COMPLETED, CATEGORY_NOT_COMPLETED, CATEGORY_REMOVED)
    }
    added = [r for r in rows if r["added_after_start"]]
    committed = len(rows) - len(added)
    completed = len(by_category[CATEGORY_COMPLETED])

    summary = {
        "committed": committed,
        "completed": completed,
        "not_completed": len(by_category[CATEGORY_NOT_COMPLETED]),
        "removed": len(by_category[CATEGORY_REMOVED]),
        "added_after_start": len(added),
        "completion_pct": _pct(completed, committed),
    }

    if any(r["story_points"] is not None for r in rows):
        points_committed = _sum_points(r for r in rows if not r["added_after_start"])
        points_completed = _sum_points(by_category[CATEGORY_COMPLETED])
        summary.update(
            points_committed=points_committed,
            points_completed=points_completed,
            points_not_completed=_sum_points(by_category[CATEGORY_NOT_COMPLETED]),
            points_removed=_sum_points(by_category[CATEGORY_REMOVED]),
            points_added_after_start=_sum_points(added),
            points_completion_pct=_pct(points_completed, points_committed),
        )
    return summary


def _sum_points(rows) -> float:
    """Sum story points over rows, treating a missing estimate as zero."""
    return round(sum(r["story_points"] or 0.0 for r in rows), 2)


def _pct(part: float, whole: float) -> float:
    """Percentage of ``part`` in ``whole``, 0.0 when ``whole`` is zero."""
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 1)


# --------------------------------------------------------------------------- #
# velocity
# --------------------------------------------------------------------------- #


def _greenhopper_velocity(client, board_id: int, last: int) -> dict:
    """Build velocity rows from the Greenhopper velocity chart."""
    payload = client.get(GREENHOPPER_VELOCITY, {"rapidViewId": board_id}) or {}
    entries = payload.get("velocityStatEntries") if isinstance(payload, dict) else None
    entries = entries if isinstance(entries, dict) else {}
    sprints = [s for s in (payload.get("sprints") or []) if isinstance(s, dict)]
    # ``sequence`` is the authoritative order; server order is the tie-break.
    sprints.sort(key=lambda s: s.get("sequence") if isinstance(s.get("sequence"), int) else 0)

    rows = []
    for sprint in sprints[-last:]:
        # Keys are the sprint id as a STRING while ``sprints[].id`` is an int.
        entry = entries.get(str(sprint.get("id"))) or {}
        rows.append(
            {
                "sprint_id": sprint.get("id"),
                "sprint": sprint.get("name"),
                "state": sprint.get("state"),
                "committed": _entry_value(entry, "estimated"),
                "completed": _entry_value(entry, "completed"),
            }
        )
    return _velocity_report(board_id, last, rows, METHOD_GREENHOPPER)


def _entry_value(entry: dict, name: str) -> float:
    """Read one side of a velocity stat entry, defaulting to 0.0."""
    value = (entry.get(name) or {}).get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _agile_velocity(client, board_id: int, last: int, *, fallback_reason: str) -> dict:
    """Approximate velocity by counting issues in each closed sprint."""
    sprints = client.paginate(
        f"/rest/agile/1.0/board/{board_id}/sprint", {"state": "closed"}
    )
    rows = []
    for sprint in sprints[-last:]:
        sprint_id = sprint.get("id")
        issues = client.paginate(
            f"/rest/agile/1.0/sprint/{sprint_id}/issue",
            {"fields": "status"},
            items_key="issues",
        )
        rows.append(
            {
                "sprint_id": sprint_id,
                "sprint": sprint.get("name"),
                "state": sprint.get("state"),
                "committed": len(issues),
                "completed": sum(1 for raw in issues if _is_done(raw.get("fields") or {})),
            }
        )
    return _velocity_report(
        board_id, last, rows, METHOD_AGILE, fallback_reason=fallback_reason
    )


def _velocity_report(
    board_id: int,
    last: int,
    rows: list[dict],
    method: str,
    *,
    fallback_reason: str | None = None,
) -> dict:
    """Wrap velocity rows in the report shape with averages."""
    count = len(rows)
    summary = {
        "sprints": count,
        "total_committed": round(sum(r["committed"] for r in rows), 2),
        "total_completed": round(sum(r["completed"] for r in rows), 2),
        "avg_committed": round(sum(r["committed"] for r in rows) / count, 2) if count else 0.0,
        "avg_completed": round(sum(r["completed"] for r in rows) / count, 2) if count else 0.0,
    }
    meta: dict[str, Any] = {"method": method, "board_id": board_id, "last": last}
    if fallback_reason is not None:
        meta["fallback_reason"] = fallback_reason
    return make_report(
        f"Velocity: board {board_id}",
        list(VELOCITY_COLUMNS),
        rows,
        summary=summary,
        meta=meta,
    )
