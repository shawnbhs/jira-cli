"""Supervision reports: what a manager needs to see about a team's Jira work.

Every function in this module returns the shared report shape built by
:func:`jira_cli.report.make_report` and records, in ``meta``, the exact JQL it
ran plus a plain-English ``method`` and ``limitations`` note.  Correctness is
preferred over cleverness: counts come from ``client.count()`` (server-side
``total``) rather than from client-side length of a truncated page.

Design notes
------------
* Core modules (``report``, ``durations``, ``errors``) and sibling services
  (``worklog``) are imported lazily inside functions so this module can be
  loaded and unit-tested in isolation.
* Dates are compared on their ``YYYY-MM-DD`` prefix.  Jira returns timestamps
  with per-user time zone offsets; truncating to the calendar date is stable
  and avoids silently re-interpreting an offset we did not pick.
* All estimate/worklog values exposed in rows are **hours** (float, 2dp);
  the unit is also stated in ``meta["method"]``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any

__all__ = [
    "jql_quote",
    "team_dashboard",
    "person_activity",
    "stale_issues",
    "overdue_issues",
    "incomplete_issues",
    "unassigned_issues",
    "workload",
]

#: Jira status category names, as used in JQL.
CATEGORY_TODO = '"To Do"'
CATEGORY_IN_PROGRESS = '"In Progress"'
CATEGORY_DONE = "Done"

#: Default number of days without an update after which an issue counts as stale
#: in :func:`team_dashboard`.
DASHBOARD_STALE_DAYS = 5

#: Comment/worklog bodies are truncated to this many characters in activity rows.
DETAIL_CHARS = 160

#: English status category labels mapped to the stable API keys.
_CATEGORY_NAME_TO_KEY = {
    "to do": "new",
    "in progress": "indeterminate",
    "done": "done",
}

#: Fields every estimate reader needs; ``timetracking`` is preferred, the
#: top-level siblings are the fallback (see tests/fixtures/SHAPES.md #4).
_ESTIMATE_FIELDS = ["timetracking", "timeestimate", "timeoriginalestimate"]

_INCOMPLETE_CHECKS = ("description", "priority", "estimate", "assignee", "due")


# --------------------------------------------------------------------------- #
# JQL building blocks
# --------------------------------------------------------------------------- #

def jql_quote(value: Any) -> str:
    """Return ``value`` as a safely quoted JQL string literal.

    Wraps the value in double quotes and escapes backslashes and double quotes,
    so that user-supplied text cannot terminate the literal and inject extra
    JQL clauses::

        >>> jql_quote('a" OR project = X')
        '"a\\\\" OR project = X"'

    Args:
        value: Any value; it is stringified first.

    Returns:
        A double-quoted JQL literal.
    """
    text = "" if value is None else str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _in_clause(field: str, values: Sequence[str]) -> str:
    """Build ``field = "a"`` for one value or ``field in ("a", "b")`` for many."""
    quoted = [jql_quote(v) for v in values]
    if len(quoted) == 1:
        return f"{field} = {quoted[0]}"
    return f"{field} in ({', '.join(quoted)})"


def _project_clause(projects: Sequence[str] | None) -> str | None:
    """Return a project filter clause, or ``None`` for "all projects"."""
    if not projects:
        return None
    return _in_clause("project", list(projects))


def _assignee_clause(usernames: Sequence[str] | None) -> str | None:
    """Return an assignee filter clause, or ``None`` for "any assignee"."""
    if not usernames:
        return None
    return _in_clause("assignee", list(usernames))


def _and(*clauses: str | None) -> str:
    """Join the non-empty clauses with ``AND``, parenthesising nothing extra."""
    return " AND ".join(c for c in clauses if c)


def _order(jql: str, by: str) -> str:
    """Append an ``ORDER BY`` tail to ``jql``."""
    return f"{jql} ORDER BY {by}"


# --------------------------------------------------------------------------- #
# Lazy imports of core / sibling modules
# --------------------------------------------------------------------------- #

def _make_report(
    title: str,
    columns: list[str],
    rows: list[dict],
    *,
    summary: dict | None = None,
    meta: dict | None = None,
) -> dict:
    """Build the shared report shape (lazy import of :mod:`jira_cli.report`)."""
    from jira_cli.report import make_report

    return make_report(title, columns, rows, summary=summary, meta=meta)


def _resolve_date(text: str, today: date | None = None) -> str:
    """Resolve a date expression to ``YYYY-MM-DD`` (lazy import of durations)."""
    from jira_cli.durations import resolve_date

    return resolve_date(text, today)


def _input_error(message: str) -> Exception:
    """Build an :class:`jira_cli.errors.InputError` (lazy import)."""
    from jira_cli.errors import InputError

    return InputError(message)


def _today() -> date:
    """Today's date in UTC. Separate function so tests can pin it."""
    return datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- #
# Small field readers (raw Jira issue dicts)
# --------------------------------------------------------------------------- #

def _fields(raw: dict) -> dict:
    return raw.get("fields") or {}


def _user_name(user: Any) -> str | None:
    """Username of a Jira user object, or ``None``."""
    if not isinstance(user, dict):
        return None
    return user.get("name") or user.get("key") or user.get("accountId")


def _display_name(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return user.get("displayName") or _user_name(user)


def _is_user(user: Any, username: str) -> bool:
    """True when the Jira user object identifies ``username``.

    Jira Server identifies users by ``name`` (the username) and ``key``.  The
    display name is NOT an identifier (it is free text, may be non-ASCII and
    may collide with another user's username), so it is consulted only when
    the user object carries neither ``name`` nor ``key`` at all.
    """
    if not isinstance(user, dict):
        return False
    wanted = username.strip().lower()
    identifiers = [user.get("name"), user.get("key")]
    if not any(identifiers):
        identifiers = [user.get("displayName")]
    for candidate in identifiers:
        if candidate and str(candidate).strip().lower() == wanted:
            return True
    return False


def _status_category_key(fields: dict) -> str | None:
    """Stable status category key: ``new`` | ``indeterminate`` | ``done``.

    Jira Server returns ``status.statusCategory.{key, name}``; ``name`` is a
    localised label, so the key is preferred.  When only a name is present
    (older payloads, hand-built fakes) the English label is mapped back.
    Returns ``None`` when no category is known, and ``"undefined"`` for the
    real "No Category" bucket.
    """
    status = fields.get("status")
    if not isinstance(status, dict):
        return None
    category = status.get("statusCategory")
    if not isinstance(category, dict):
        return None
    key = category.get("key")
    if key:
        return str(key).strip().lower()
    return _CATEGORY_NAME_TO_KEY.get(str(category.get("name") or "").strip().lower())


def _day(timestamp: Any) -> str | None:
    """Calendar-date prefix (``YYYY-MM-DD``) of a Jira timestamp."""
    if not timestamp:
        return None
    text = str(timestamp)
    return text[:10] if len(text) >= 10 else None


def _in_window(timestamp: Any, since: str, until: str | None) -> bool:
    """True when ``timestamp`` falls in the inclusive ``since``..``until`` range."""
    day = _day(timestamp)
    if day is None:
        return False
    if day < since:
        return False
    if until is not None and day > until:
        return False
    return True


def _days_between(day: str | None, today: date) -> int | None:
    """Whole days from ``day`` (``YYYY-MM-DD``) up to ``today``."""
    if not day:
        return None
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return None
    return (today - parsed).days


def _hours(seconds: Any) -> float:
    """Seconds to hours, rounded to 2 decimals; ``None`` becomes ``0.0``."""
    if not seconds:
        return 0.0
    try:
        return round(float(seconds) / 3600.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _seconds_or_none(value: Any) -> int | None:
    """``int(value)`` or ``None`` when absent/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _estimate_seconds(fields: dict, kind: str) -> int | None:
    """Original or remaining estimate in seconds, ``None`` when not reported.

    ``kind`` is ``"original"`` or ``"remaining"``.  The ``timetracking``
    object (``originalEstimateSeconds``/``remainingEstimateSeconds``) wins;
    the top-level ``timeoriginalestimate``/``timeestimate`` fields are the
    fallback.  ``timetracking`` is ``{}`` when nothing is tracked and any key
    may be missing when ``fields=`` restricted the response.
    """
    tracking_key, flat_key = {
        "original": ("originalEstimateSeconds", "timeoriginalestimate"),
        "remaining": ("remainingEstimateSeconds", "timeestimate"),
    }[kind]
    tracking = fields.get("timetracking")
    if isinstance(tracking, dict):
        value = _seconds_or_none(tracking.get(tracking_key))
        if value is not None:
            return value
    return _seconds_or_none(fields.get(flat_key))


def _remaining_seconds(fields: dict) -> int:
    """Remaining estimate in seconds, falling back to the original estimate.

    The remaining estimate is used whenever Jira reported it at all,
    including an explicit ``0``: an issue whose remaining estimate was zeroed
    has no work left, and falling through to the original estimate there
    would silently re-add the whole original estimate to a workload total.
    """
    remaining = _estimate_seconds(fields, "remaining")
    if remaining is not None:
        return remaining
    return _estimate_seconds(fields, "original") or 0


def _has_estimate(fields: dict) -> bool:
    """True when someone actually estimated the issue.

    An original estimate counts whenever it is present (an explicit ``0`` is
    a deliberate estimate).  A remaining estimate alone counts only when it
    is positive: Jira Server auto-sets ``timeestimate = 0`` as soon as work
    is logged on an issue that never had an estimate (see
    ``tests/fixtures/search_changelog.json``), so a bare zero is not
    evidence that anybody estimated it.
    """
    if _estimate_seconds(fields, "original") is not None:
        return True
    return (_estimate_seconds(fields, "remaining") or 0) > 0


def _truncate(text: Any, limit: int = DETAIL_CHARS) -> str:
    """One-line, length-capped rendering of free text."""
    if text is None:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit]


# --------------------------------------------------------------------------- #
# team_dashboard
# --------------------------------------------------------------------------- #

def team_dashboard(
    client: Any,
    usernames: list[str],
    projects: list[str] | None = None,
    since: str = "7d",
) -> dict:
    """One row per team member with the numbers a supervisor asks for first.

    Per user: issues in the To Do and In Progress status categories, issues
    resolved since ``since``, overdue issues, issues untouched for more than
    :data:`DASHBOARD_STALE_DAYS` days, the total remaining estimate of their
    open issues, and the hours they logged since ``since``.

    Counts come from ``client.count()`` so they reflect the server's ``total``
    and are not capped by a page size.

    Args:
        client: A ``JiraClient``.
        usernames: Usernames to report on (order is preserved).
        projects: Optional project keys to restrict every query to.
            ``None`` means no project filter (all projects the user can see).
        since: Date expression (``YYYY-MM-DD``, ``7d``, ``2w``, ``today``, ...).

    Returns:
        The report shape; ``meta["jql"]`` lists every query that was run.
    """
    since_day = _resolve_date(since)
    project = _project_clause(projects)
    jqls: list[str] = []
    rows: list[dict] = []

    for username in usernames:
        mine = _and(_in_clause("assignee", [username]), project)
        queries = {
            "open_todo": _and(mine, f"statusCategory = {CATEGORY_TODO}"),
            "in_progress": _and(mine, f"statusCategory = {CATEGORY_IN_PROGRESS}"),
            "done_since": _and(
                mine,
                f"statusCategory = {CATEGORY_DONE}",
                f"resolved >= {jql_quote(since_day)}",
            ),
            "overdue": _and(
                mine, "duedate < now()", f"statusCategory != {CATEGORY_DONE}"
            ),
            "stale": _and(
                mine,
                f"updated <= -{DASHBOARD_STALE_DAYS}d",
                f"statusCategory != {CATEGORY_DONE}",
            ),
        }
        row: dict[str, Any] = {"user": username}
        for name, jql in queries.items():
            jqls.append(jql)
            row[name] = int(client.count(jql))

        open_jql = _and(mine, f"statusCategory != {CATEGORY_DONE}")
        jqls.append(open_jql)
        open_issues = client.search(open_jql, fields=list(_ESTIMATE_FIELDS))
        remaining = sum(_remaining_seconds(_fields(i)) for i in open_issues or [])
        row["total_estimate_open"] = _hours(remaining)
        row["logged_since"] = _logged_hours(client, username, since_day, projects)
        rows.append(row)

    columns = [
        "user",
        "open_todo",
        "in_progress",
        "done_since",
        "overdue",
        "stale",
        "total_estimate_open",
        "logged_since",
    ]
    summary: dict[str, Any] = {"users": len(rows), "since": since_day}
    for name in ("open_todo", "in_progress", "done_since", "overdue", "stale"):
        summary[name] = sum(int(r[name]) for r in rows)
    for name in ("total_estimate_open", "logged_since"):
        summary[name] = round(sum(float(r[name]) for r in rows), 2)
    meta = {
        "jql": jqls,
        "since": since_day,
        "projects": list(projects) if projects else None,
        "method": (
            "Counts are server-side totals from /rest/api/2/search with "
            "maxResults=0, one query per metric per user. "
            "'total_estimate_open' sums the remaining estimate (timetracking, "
            "else timeestimate; falling back to the original estimate) over "
            "the user's non-Done issues and is "
            "reported in hours. 'logged_since' sums worklog seconds attributed "
            "to the user via services.worklog.user_worklogs, in hours. "
            f"'stale' means no update for more than {DASHBOARD_STALE_DAYS} days."
        ),
        "limitations": (
            "Only issues currently assigned to the user are counted, so work "
            "they did on issues later reassigned is invisible here. Issues the "
            "authenticated account cannot browse are excluded by Jira itself. "
            "Estimates are only as good as what people entered; missing "
            "estimates count as zero. 'overdue' ignores issues with no due date. "
            "Issues whose status is in Jira's 'No Category' bucket are in "
            "neither open_todo nor in_progress."
        ),
    }
    return _make_report(
        "Team dashboard", columns, rows, summary=summary, meta=meta
    )


def _logged_hours(
    client: Any, username: str, since_day: str, projects: list[str] | None
) -> float:
    """Hours ``username`` logged on or after ``since_day`` (``YYYY-MM-DD``).

    Delegates to :func:`jira_cli.services.worklog.user_worklogs`, imported
    lazily so this module stays loadable on its own.  Errors are **not**
    swallowed: a failing worklog query is a wrong dashboard, not a zero.
    """
    from jira_cli.services import worklog

    entries = worklog.user_worklogs(client, [username], since_day, None, projects)
    return _hours(sum(int(e.get("seconds") or 0) for e in entries or []))


# --------------------------------------------------------------------------- #
# person_activity
# --------------------------------------------------------------------------- #

def person_activity(
    client: Any,
    username: str,
    since: str = "7d",
    until: str | None = None,
    projects: list[str] | None = None,
) -> dict:
    """Timeline of what one person actually did, newest-last.

    Candidate issues are those the person is or was involved with
    (``assignee was``/``assignee =``/``reporter =``/``worklogAuthor =``) and
    that changed in the window.  Events are then filtered down to the ones
    *authored by that person*: issues they reported, changelog entries they
    made, comments they wrote and worklogs they logged.

    Args:
        client: A ``JiraClient``.
        username: The username to report on.
        since: Start of the window (inclusive), date expression.
        until: End of the window (inclusive), date expression; ``None`` = open.
        projects: Optional project keys to restrict the search to.

    Returns:
        The report shape with columns ``when, key, summary, action, detail``.
    """
    since_day = _resolve_date(since)
    until_day = _resolve_date(until) if until else None

    involved = (
        f"(assignee was {jql_quote(username)} "
        f"OR assignee = {jql_quote(username)} "
        f"OR reporter = {jql_quote(username)} "
        f"OR worklogAuthor = {jql_quote(username)})"
    )
    jql = _and(
        involved,
        f"updated >= {jql_quote(since_day)}",
        _project_clause(projects),
    )
    jqls = [jql]
    requests_made: list[str] = []
    issues = client.search(
        jql,
        fields=["summary", "created", "reporter", "comment", "worklog"],
        expand=["changelog"],
    ) or []

    events: list[dict] = []
    for raw in issues:
        key = raw.get("key") or ""
        fields = _fields(raw)
        summary = _truncate(fields.get("summary"))
        events.extend(_created_event(raw, key, summary, username, since_day, until_day))
        events.extend(
            _changelog_events(
                client, raw, key, summary, username, since_day, until_day,
                requests_made,
            )
        )
        events.extend(
            _comment_events(
                client, raw, key, summary, username, since_day, until_day,
                requests_made,
            )
        )
        events.extend(
            _worklog_events(
                client, raw, key, summary, username, since_day, until_day,
                requests_made,
            )
        )

    events.sort(key=lambda e: (e["when"] or "", e["key"], e["action"]))

    by_action: dict[str, int] = {}
    for event in events:
        by_action[event["action"]] = by_action.get(event["action"], 0) + 1
    summary_block = {
        "user": username,
        "events": len(events),
        "issues_touched": len({e["key"] for e in events}),
        "since": since_day,
        "until": until_day,
        **{f"{name}_events": count for name, count in sorted(by_action.items())},
    }
    meta = {
        "jql": jqls,
        "requests": requests_made,
        "since": since_day,
        "until": until_day,
        "projects": list(projects) if projects else None,
        "method": (
            "One search for issues the user is or was involved with "
            "(assignee was/is, reporter, worklogAuthor) and that were updated "
            "in the window, expanded with the changelog. Events are kept only "
            "when the user authored them: issue created (they are the "
            "reporter), changelog items under their name, comments they wrote, "
            "worklogs they logged. Comment and worklog lists embedded in the "
            "search response, and the changelog, are re-fetched from "
            "/rest/api/2/issue/{key}/comment, /worklog and "
            "/issue/{key}?expand=changelog when the reported total exceeds "
            "the embedded page. Events are then filtered to the "
            "since/until window and sorted by time."
        ),
        "limitations": (
            "Comments on issues the user was never assigned to, never "
            "reported and never logged work on are NOT discovered, because the "
            "candidate set comes from assignee/reporter/worklogAuthor only "
            "(JQL has no 'commented by' predicate on this server). Issues "
            "whose last update predates 'since' are excluded even if the user "
            "acted on them earlier in the window. Changelog entries are "
            "attributed by the author Jira recorded, so bulk changes or "
            "automation running as another account appear under that account. "
            "Deleted comments and worklogs leave no trace. Events are matched "
            "to the window by calendar date of the Jira timestamp."
        ),
    }
    return _make_report(
        f"Activity: {username}",
        ["when", "key", "summary", "action", "detail"],
        events,
        summary=summary_block,
        meta=meta,
    )


def _event(when: Any, key: str, summary: str, action: str, detail: str) -> dict:
    return {
        "when": str(when) if when else None,
        "key": key,
        "summary": summary,
        "action": action,
        "detail": detail,
    }


def _created_event(
    raw: dict,
    key: str,
    summary: str,
    username: str,
    since: str,
    until: str | None,
) -> list[dict]:
    """The "created this issue" event, when the user reported it in-window."""
    fields = _fields(raw)
    created = fields.get("created")
    if not _is_user(fields.get("reporter"), username):
        return []
    if not _in_window(created, since, until):
        return []
    return [_event(created, key, summary, "created", "reported this issue")]


def _histories(
    client: Any, raw: dict, key: str, requests_made: list[str]
) -> list[dict]:
    """Changelog histories, re-fetching the issue when the page is truncated.

    ``expand=changelog`` on search yields
    ``changelog.{startAt, maxResults, total, histories[]}``; when ``total``
    exceeds the embedded page the full history is read from
    ``/rest/api/2/issue/{key}?expand=changelog``.
    """
    block = raw.get("changelog") or {}
    histories = list(block.get("histories") or [])
    total = block.get("total")
    if isinstance(total, int) and total > len(histories) and key:
        path = f"/rest/api/2/issue/{key}"
        requests_made.append(f"GET {path}?expand=changelog")
        fetched = client.get(path, params={"expand": "changelog", "fields": "summary"}) or {}
        histories = list(((fetched.get("changelog") or {}).get("histories")) or histories)
    return histories


def _changelog_events(
    client: Any,
    raw: dict,
    key: str,
    summary: str,
    username: str,
    since: str,
    until: str | None,
    requests_made: list[str],
) -> list[dict]:
    """Field changes recorded in the changelog under the user's name."""
    histories = _histories(client, raw, key, requests_made)
    events: list[dict] = []
    for history in histories:
        if not _is_user(history.get("author"), username):
            continue
        when = history.get("created")
        if not _in_window(when, since, until):
            continue
        for item in history.get("items") or []:
            field = str(item.get("field") or "field")
            before = item.get("fromString") or item.get("from") or "none"
            after = item.get("toString") or item.get("to") or "none"
            lowered = field.lower()
            if lowered == "status":
                action = "status"
            elif lowered == "assignee":
                action = "assignee"
            else:
                action = "field"
            events.append(
                _event(
                    when,
                    key,
                    summary,
                    action,
                    f"{field}: {_truncate(before, 60)} -> {_truncate(after, 60)}",
                )
            )
    return events


def _comment_events(
    client: Any,
    raw: dict,
    key: str,
    summary: str,
    username: str,
    since: str,
    until: str | None,
    requests_made: list[str],
) -> list[dict]:
    """Comments written by the user, re-fetching the full list when truncated."""
    block = _fields(raw).get("comment") or {}
    comments = list(block.get("comments") or [])
    total = block.get("total")
    if isinstance(total, int) and total > len(comments) and key:
        path = f"/rest/api/2/issue/{key}/comment"
        requests_made.append(f"GET {path}")
        fetched = client.get(path, params={"maxResults": total}) or {}
        comments = list(fetched.get("comments") or comments)

    events: list[dict] = []
    for comment in comments:
        if not _is_user(comment.get("author"), username):
            continue
        when = comment.get("created")
        if not _in_window(when, since, until):
            continue
        events.append(
            _event(when, key, summary, "comment", _truncate(comment.get("body")))
        )
    return events


def _worklog_events(
    client: Any,
    raw: dict,
    key: str,
    summary: str,
    username: str,
    since: str,
    until: str | None,
    requests_made: list[str],
) -> list[dict]:
    """Worklogs logged by the user, re-fetching the full list when truncated."""
    block = _fields(raw).get("worklog") or {}
    worklogs = list(block.get("worklogs") or [])
    total = block.get("total")
    if isinstance(total, int) and total > len(worklogs) and key:
        path = f"/rest/api/2/issue/{key}/worklog"
        requests_made.append(f"GET {path}")
        fetched = client.get(path, params={"maxResults": total}) or {}
        worklogs = list(fetched.get("worklogs") or worklogs)

    events: list[dict] = []
    for entry in worklogs:
        if not _is_user(entry.get("author"), username):
            continue
        when = entry.get("started") or entry.get("created")
        if not _in_window(when, since, until):
            continue
        hours = _hours(entry.get("timeSpentSeconds"))
        note = _truncate(entry.get("comment"), DETAIL_CHARS)
        detail = f"logged {hours}h" + (f": {note}" if note else "")
        events.append(_event(when, key, summary, "worklog", detail))
    return events


# --------------------------------------------------------------------------- #
# Issue-list reports
# --------------------------------------------------------------------------- #

_LIST_FIELDS = [
    "summary",
    "status",
    "assignee",
    "updated",
    "duedate",
    "priority",
    *_ESTIMATE_FIELDS,
]


def _base_row(raw: dict) -> dict:
    """Common columns for every per-issue row.

    ``assignee`` is the username (the identifier Jira Server wants back) and
    ``assignee_name`` the display name, mirroring the CONTRACT's normalised
    issue dict.  ``report.make_report`` prunes rows down to each report's
    ``columns``, so a report without an assignee column simply drops both.
    """
    fields = _fields(raw)
    status = fields.get("status") or {}
    assignee = fields.get("assignee")
    return {
        "key": raw.get("key"),
        "summary": _truncate(fields.get("summary")),
        "assignee": _user_name(assignee),
        "assignee_name": _display_name(assignee),
        "status": status.get("name") if isinstance(status, dict) else None,
    }


def stale_issues(
    client: Any,
    days: int = 7,
    projects: list[str] | None = None,
    usernames: list[str] | None = None,
) -> dict:
    """Open issues nobody has touched for ``days`` days or more.

    Args:
        client: A ``JiraClient``.
        days: Minimum number of days without an update.
        projects: Optional project keys to restrict the search to.
        usernames: Optional assignees to restrict the search to.

    Returns:
        The report shape, newest-update last.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise _input_error("days must be an integer") from None
    if days < 0:
        raise _input_error("days must be >= 0")

    jql = _order(
        _and(
            f"updated <= -{days}d",
            f"statusCategory != {CATEGORY_DONE}",
            _project_clause(projects),
            _assignee_clause(usernames),
        ),
        "updated ASC",
    )
    issues = client.search(jql, fields=_LIST_FIELDS) or []
    today = _today()

    rows = []
    for raw in issues:
        row = _base_row(raw)
        updated = _fields(raw).get("updated")
        row["updated"] = updated
        row["days_since_update"] = _days_between(_day(updated), today)
        rows.append(row)
    rows.sort(key=lambda r: -(r["days_since_update"] or 0))

    columns = [
        "key",
        "summary",
        "assignee",
        "assignee_name",
        "status",
        "updated",
        "days_since_update",
    ]
    meta = {
        "jql": [jql],
        "projects": list(projects) if projects else None,
        "usernames": list(usernames) if usernames else None,
        "method": (
            f"JQL 'updated <= -{days}d AND statusCategory != Done'. "
            "days_since_update is whole days between the calendar date of the "
            "issue's updated timestamp and today (UTC)."
        ),
        "limitations": (
            "'updated' changes on any field edit, including automated ones, so "
            "a bot touch makes an abandoned issue look alive. Comments and "
            "worklogs do bump 'updated'; a parent issue is not bumped by work "
            "on its sub-tasks. Issues the authenticated account cannot browse "
            "are invisible."
        ),
    }
    return _make_report(
        f"Stale issues (no update for {days}d+)",
        columns,
        rows,
        summary={"issues": len(rows), "days": days},
        meta=meta,
    )


def overdue_issues(
    client: Any,
    projects: list[str] | None = None,
    usernames: list[str] | None = None,
) -> dict:
    """Open issues whose due date has passed, most overdue first."""
    jql = _order(
        _and(
            "duedate < now()",
            f"statusCategory != {CATEGORY_DONE}",
            _project_clause(projects),
            _assignee_clause(usernames),
        ),
        "duedate ASC",
    )
    issues = client.search(jql, fields=_LIST_FIELDS) or []
    today = _today()

    rows = []
    for raw in issues:
        row = _base_row(raw)
        due = _fields(raw).get("duedate")
        row["due"] = due
        row["days_overdue"] = _days_between(_day(due), today)
        row["updated"] = _fields(raw).get("updated")
        rows.append(row)
    rows.sort(key=lambda r: -(r["days_overdue"] or 0))

    columns = [
        "key",
        "summary",
        "assignee",
        "assignee_name",
        "status",
        "due",
        "days_overdue",
        "updated",
    ]
    meta = {
        "jql": [jql],
        "projects": list(projects) if projects else None,
        "usernames": list(usernames) if usernames else None,
        "method": (
            "JQL 'duedate < now() AND statusCategory != Done'. days_overdue is "
            "whole days between the due date and today (UTC)."
        ),
        "limitations": (
            "Issues with no due date can never appear, so an empty report does "
            "not mean the team is on schedule - it may mean nobody sets due "
            "dates. Due dates are plain dates with no time zone; 'now()' is "
            "evaluated by the Jira server in its own time zone."
        ),
    }
    return _make_report(
        "Overdue issues",
        columns,
        rows,
        summary={"issues": len(rows)},
        meta=meta,
    )


def incomplete_issues(
    client: Any,
    projects: list[str] | None = None,
    usernames: list[str] | None = None,
    checks: tuple = _INCOMPLETE_CHECKS,
) -> dict:
    """Open issues missing the fields a team needs to actually plan work.

    Args:
        client: A ``JiraClient``.
        projects: Optional project keys to restrict the search to.
        usernames: Optional assignees to restrict the search to.
        checks: Which fields to require. Supported values are
            ``description``, ``priority``, ``estimate``, ``assignee``, ``due``.

    Raises:
        jira_cli.errors.InputError: If ``checks`` contains an unknown name or
            is empty.

    Returns:
        The report shape; only issues missing at least one checked field appear.
    """
    selected = tuple(str(c).strip().lower() for c in checks or ())
    if not selected:
        raise _input_error("checks must not be empty")
    unknown = [c for c in selected if c not in _INCOMPLETE_CHECKS]
    if unknown:
        raise _input_error(
            f"unknown checks: {', '.join(unknown)}; "
            f"allowed: {', '.join(_INCOMPLETE_CHECKS)}"
        )

    jql = _order(
        _and(
            f"statusCategory != {CATEGORY_DONE}",
            _project_clause(projects),
            _assignee_clause(usernames),
        ),
        "created DESC",
    )
    fields = list(_LIST_FIELDS) + ["description"]
    issues = client.search(jql, fields=fields) or []

    rows = []
    missing_totals = {name: 0 for name in selected}
    for raw in issues:
        missing = _missing_fields(_fields(raw), selected)
        if not missing:
            continue
        row = _base_row(raw)
        row["missing"] = ", ".join(missing)
        row["missing_count"] = len(missing)
        row["updated"] = _fields(raw).get("updated")
        rows.append(row)
        for name in missing:
            missing_totals[name] += 1
    rows.sort(key=lambda r: (-r["missing_count"], r["key"] or ""))

    columns = [
        "key",
        "summary",
        "assignee",
        "assignee_name",
        "status",
        "missing",
        "missing_count",
        "updated",
    ]
    summary = {"issues": len(rows), "checks": ", ".join(selected)}
    summary.update({f"missing_{name}": count for name, count in missing_totals.items()})
    meta = {
        "jql": [jql],
        "projects": list(projects) if projects else None,
        "usernames": list(usernames) if usernames else None,
        "checks": list(selected),
        "method": (
            "All non-Done issues are fetched and checked locally: description "
            "non-empty, priority set, an estimate present (an original "
            "estimate of any value incl. 0, or a positive remaining estimate; "
            "timetracking preferred over the top-level time fields), an "
            "assignee set, a due date set. Only issues missing "
            "at least one checked field are listed."
        ),
        "limitations": (
            "'priority' is reported as missing only when Jira returns no "
            "priority at all; a project whose default priority is always "
            "applied will never flag it. A description containing only "
            "whitespace or a placeholder counts as present. Required custom "
            "fields and story points are not checked. Every open issue is "
            "fetched, so on large projects pass 'projects' to keep the query "
            "bounded."
        ),
    }
    return _make_report(
        "Incomplete issues", columns, rows, summary=summary, meta=meta
    )


def _missing_fields(fields: dict, checks: Sequence[str]) -> list[str]:
    """Names of the checked fields that are not filled in on this issue."""
    missing: list[str] = []
    for name in checks:
        if name == "description" and not str(fields.get("description") or "").strip():
            missing.append(name)
        elif name == "priority" and not (fields.get("priority") or None):
            missing.append(name)
        elif name == "estimate" and not _has_estimate(fields):
            missing.append(name)
        elif name == "assignee" and not _user_name(fields.get("assignee")):
            missing.append(name)
        elif name == "due" and not fields.get("duedate"):
            missing.append(name)
    return missing


def unassigned_issues(client: Any, projects: list[str] | None = None) -> dict:
    """Open issues with nobody on the hook for them.

    Args:
        client: A ``JiraClient``.
        projects: Project keys to restrict the search to; ``None`` means no
            project filter.
    """
    jql = _order(
        _and(
            "assignee is EMPTY",
            f"statusCategory != {CATEGORY_DONE}",
            _project_clause(projects),
        ),
        "created ASC",
    )
    issues = client.search(jql, fields=_LIST_FIELDS) or []
    today = _today()

    rows = []
    for raw in issues:
        row = _base_row(raw)
        fields = _fields(raw)
        row["updated"] = fields.get("updated")
        row["due"] = fields.get("duedate")
        row["days_since_update"] = _days_between(_day(fields.get("updated")), today)
        rows.append(row)

    columns = ["key", "summary", "status", "due", "updated", "days_since_update"]
    meta = {
        "jql": [jql],
        "projects": list(projects) if projects else None,
        "method": (
            "JQL 'assignee is EMPTY AND statusCategory != Done'. Rows carry the "
            "age of the last update so the oldest neglected issues stand out."
        ),
        "limitations": (
            "An issue assigned to a shared or placeholder account is not "
            "unassigned and will not appear. Sub-tasks are included and may "
            "duplicate work already owned through their parent."
        ),
    }
    return _make_report(
        "Unassigned issues",
        columns,
        rows,
        summary={"issues": len(rows)},
        meta=meta,
    )


def workload(
    client: Any,
    usernames: list[str],
    projects: list[str] | None = None,
) -> dict:
    """Open issue count and remaining estimate per person.

    Args:
        client: A ``JiraClient``.
        usernames: Usernames to report on (order is preserved).
        projects: Optional project keys to restrict every query to.
    """
    project = _project_clause(projects)
    jqls: list[str] = []
    rows: list[dict] = []

    for username in usernames:
        jql = _order(
            _and(
                _in_clause("assignee", [username]),
                f"statusCategory != {CATEGORY_DONE}",
                project,
            ),
            "duedate ASC",
        )
        jqls.append(jql)
        issues = client.search(jql, fields=_LIST_FIELDS) or []

        remaining = 0
        in_progress = 0
        unestimated = 0
        for raw in issues:
            fields = _fields(raw)
            seconds = _remaining_seconds(fields)
            remaining += seconds
            if not seconds:
                unestimated += 1
            if _status_category_key(fields) == "indeterminate":
                in_progress += 1

        rows.append(
            {
                "user": username,
                "open_issues": len(issues),
                "in_progress": in_progress,
                "remaining_hours": _hours(remaining),
                "unestimated": unestimated,
            }
        )

    columns = ["user", "open_issues", "in_progress", "remaining_hours", "unestimated"]
    summary = {
        "users": len(rows),
        "open_issues": sum(r["open_issues"] for r in rows),
        "in_progress": sum(r["in_progress"] for r in rows),
        "remaining_hours": round(sum(r["remaining_hours"] for r in rows), 2),
        "unestimated": sum(r["unestimated"] for r in rows),
    }
    meta = {
        "jql": jqls,
        "projects": list(projects) if projects else None,
        "method": (
            "One search per user over their non-Done issues. remaining_hours "
            "sums the remaining estimate (timetracking, else timeestimate), "
            "falling back to the original estimate, in hours. 'unestimated' "
            "counts open issues with no remaining work recorded. in_progress "
            "counts statusCategory.key 'indeterminate' (the stable key, not "
            "the localised name)."
        ),
        "limitations": (
            "Unestimated issues contribute zero hours, so a light-looking "
            "workload may just be an unestimated one - read remaining_hours "
            "together with unestimated. Sub-tasks and their parents are both "
            "counted, which can double-count effort. Issues assigned to nobody "
            "appear in no row; use unassigned_issues for those."
        ),
    }
    return _make_report("Workload", columns, rows, summary=summary, meta=meta)
