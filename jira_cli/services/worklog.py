"""Worklogs and time estimates.

Jira Software **Server** 10.3 exposes worklogs per issue
(``/rest/api/2/issue/{key}/worklog``) and offers no endpoint that lists the
worklogs of a *person*.  A per-user report therefore needs two steps:

1. narrow the issue set with JQL (``worklogAuthor`` / ``worklogDate``), which is
   indexed server-side and cheap;
2. fetch each candidate issue's worklogs and filter them **client-side** by
   author and date, because the JQL predicates match the *issue*, not the
   individual worklog entry — an issue that Alice logged on last week is still
   returned when the window only covers today.

Dates are compared on the ``YYYY-MM-DD`` prefix of ``started`` as Jira returned
it, i.e. in the offset recorded on the entry.  Re-interpreting that offset would
silently move work across midnight, so the calendar date the author saw is the
date used here.

All durations in reports are **hours** (float, 2 decimal places); raw entries
keep ``seconds`` so callers can aggregate without rounding twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from ..durations import resolve_date, to_jira
from ..errors import InputError
from ..ids import validate_issue_key
from ..report import make_report

__all__ = [
    "issue_worklogs",
    "user_worklogs",
    "worklog_report",
    "add_worklog",
    "set_estimate",
]

#: Jira Server accepts ``started`` only in this exact shape (milliseconds and a
#: four-digit numeric offset are both mandatory).
_STARTED_FORMAT = "%Y-%m-%dT%H:%M:%S.000+0000"

#: Time of day used when the caller gave a bare ``YYYY-MM-DD``.  Midday keeps the
#: entry on the intended calendar date in every real time zone, which an
#: assumed midnight would not.
_DEFAULT_TIME = "12:00:00"


# --------------------------------------------------------------------------- #
# JQL building blocks
# --------------------------------------------------------------------------- #


def _q(value: Any) -> str:
    """Return ``value`` as a safely quoted JQL string literal.

    Wrapping in double quotes and escaping backslashes and quotes stops a
    username or project key from terminating the literal and appending JQL of
    its own.
    """
    text = "" if value is None else str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _in_clause(field: str, values: Sequence[str]) -> str:
    """Build ``field in ("a", "b")``."""
    return f"{field} in ({', '.join(_q(v) for v in values)})"


def _clean_list(values: Any, label: str) -> list[str]:
    """Coerce ``values`` into a list of non-empty stripped strings."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise InputError(f"{label} must be a list of strings")
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise InputError(f"{label} must contain only non-empty strings")
        text = item.strip()
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _require_key(key: Any) -> str:
    """Return a validated, upper-cased issue key or raise :class:`InputError`.

    The key is interpolated into a URL path, so anything not shaped like
    ``PROJ-123`` (notably ``PROJ-1/../..`` traversal) is refused here.
    """
    return validate_issue_key(key)


def _worklog_jql(
    usernames: Sequence[str],
    since: str,
    until: str | None,
    projects: Sequence[str] | None,
) -> str:
    """Build the candidate-issue JQL for a per-user worklog window."""
    clauses = [
        _in_clause("worklogAuthor", list(usernames)),
        f"worklogDate >= {_q(since)}",
    ]
    if until:
        clauses.append(f"worklogDate <= {_q(until)}")
    if projects:
        clauses.append(_in_clause("project", list(projects)))
    return " AND ".join(clauses)


# --------------------------------------------------------------------------- #
# Reading worklogs
# --------------------------------------------------------------------------- #


def _author_name(entry: Mapping[str, Any]) -> str | None:
    """Username of a worklog's author (``name``, not the Cloud ``accountId``)."""
    author = entry.get("author")
    if isinstance(author, Mapping):
        return author.get("name")
    return None


def _normalize_worklog(entry: Mapping[str, Any]) -> dict:
    """Reduce a raw worklog object to the fields callers use."""
    return {
        "id": entry.get("id"),
        "author": _author_name(entry),
        "started": entry.get("started"),
        "seconds": int(entry.get("timeSpentSeconds") or 0),
        "comment": entry.get("comment"),
    }


def issue_worklogs(client, key: str) -> list[dict]:
    """List every worklog entry on one issue.

    Args:
        client: A ``JiraClient``.
        key: Issue key, e.g. ``"PROJ-12"``.

    Returns:
        A list of ``{"id", "author", "started", "seconds", "comment"}`` dicts in
        the order Jira returned them.

    Raises:
        InputError: If ``key`` is blank.
        NotFoundError: Propagated from the client for an unknown issue.
    """
    issue_key = _require_key(key)
    payload = client.get(f"/rest/api/2/issue/{issue_key}/worklog")
    entries = payload.get("worklogs") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        return []
    return [_normalize_worklog(e) for e in entries if isinstance(e, Mapping)]


def _date_of(started: Any) -> str:
    """Calendar date of a ``started`` timestamp, in the entry's own offset."""
    return started[:10] if isinstance(started, str) else ""


def user_worklogs(
    client,
    usernames: list[str],
    since: str,
    until: str | None = None,
    projects: list[str] | None = None,
) -> list[dict]:
    """Worklog entries logged by given users inside a date window.

    Args:
        client: A ``JiraClient``.
        usernames: Usernames to include; must not be empty.
        since: ``YYYY-MM-DD`` or a relative form (``7d``, ``2w``, ``today``).
        until: Inclusive end of the window; ``None`` means "up to now".
        projects: Optional project keys to restrict the search to.

    Returns:
        A list of ``{"key", "summary", "author", "started", "seconds",
        "comment"}`` dicts, filtered client-side by author and date.

    Raises:
        InputError: If ``usernames`` is empty or a date cannot be parsed.
    """
    people = _clean_list(usernames, "usernames")
    if not people:
        raise InputError("at least one username is required")
    project_keys = _clean_list(projects, "projects")
    start = resolve_date(since)
    end = resolve_date(until) if until else None

    jql = _worklog_jql(people, start, end, project_keys)
    issues = client.search(jql, fields=["summary"]) or []

    wanted = set(people)
    rows: list[dict] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        key = issue.get("key")
        if not key:
            continue
        fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
        summary = fields.get("summary")
        for entry in issue_worklogs(client, key):
            if entry["author"] not in wanted:
                continue
            day = _date_of(entry["started"])
            if day < start or (end and day > end):
                continue
            rows.append(
                {
                    "key": key,
                    "summary": summary,
                    "author": entry["author"],
                    "started": entry["started"],
                    "seconds": entry["seconds"],
                    "comment": entry["comment"],
                }
            )
    return rows


def _hours(seconds: int) -> float:
    """Seconds as hours, rounded to 2 decimal places."""
    return round(seconds / 3600.0, 2)


def worklog_report(
    client,
    usernames: list[str],
    since: str,
    until: str | None = None,
    projects: list[str] | None = None,
) -> dict:
    """Per-user, per-issue hours logged inside a date window.

    Rows are one per ``(user, issue)`` pair, sorted by user then issue key.  The
    summary holds total hours per user plus a grand ``total``.

    Returns:
        The shared report shape from :func:`jira_cli.report.make_report`.
    """
    entries = user_worklogs(client, usernames, since, until, projects)

    totals: dict[tuple[str, str], int] = {}
    summaries: dict[str, Any] = {}
    for entry in entries:
        author = entry.get("author") or "(unknown)"
        key = entry.get("key") or "(unknown)"
        pair = (author, key)
        totals[pair] = totals.get(pair, 0) + int(entry.get("seconds") or 0)
        summaries.setdefault(key, entry.get("summary"))

    rows = [
        {
            "user": author,
            "key": key,
            "summary": summaries.get(key),
            "hours": _hours(seconds),
        }
        for (author, key), seconds in sorted(totals.items())
    ]

    per_user: dict[str, int] = {}
    for (author, _key), seconds in totals.items():
        per_user[author] = per_user.get(author, 0) + seconds
    summary = {name: _hours(seconds) for name, seconds in sorted(per_user.items())}
    summary["total"] = _hours(sum(per_user.values()))

    start = resolve_date(since)
    end = resolve_date(until) if until else None
    meta = {
        "jql": [_worklog_jql(_clean_list(usernames, "usernames"), start, end,
                             _clean_list(projects, "projects"))],
        "since": start,
        "until": end,
        "method": (
            "Candidate issues come from JQL on worklogAuthor/worklogDate; each "
            "issue's worklogs are then fetched and filtered client-side by "
            "author and by the calendar date of 'started' in its own offset. "
            "Hours are seconds/3600 rounded to 2 decimals."
        ),
        "limitations": (
            "Worklogs the caller cannot see are invisible to this report, and "
            "deleted worklogs leave no trace. Rounding each (user, issue) pair "
            "means row hours can differ from the user total by up to 0.01."
        ),
    }
    return make_report(
        "Worklog report",
        ["user", "key", "summary", "hours"],
        rows,
        summary=summary,
        meta=meta,
    )


# --------------------------------------------------------------------------- #
# Writing worklogs and estimates
# --------------------------------------------------------------------------- #


def _started_stamp(started: str | None) -> str:
    """Render ``started`` in the exact format Jira Server requires.

    ``None`` means now (UTC).  A bare ``YYYY-MM-DD`` (or a relative form such as
    ``yesterday``) is resolved and placed at midday; a value that already
    carries a time is used as given once it looks like an ISO timestamp.
    """
    if started is None:
        return datetime.now(timezone.utc).strftime(_STARTED_FORMAT)
    if not isinstance(started, str) or not started.strip():
        raise InputError("started must be a date or timestamp string")
    text = started.strip()
    if "T" in text:
        return text
    return f"{resolve_date(text)}T{_DEFAULT_TIME}.000+0000"


def add_worklog(
    client,
    key: str,
    time_spent: str,
    started: str | None = None,
    comment: str | None = None,
) -> dict:
    """Log work against an issue.

    Args:
        client: A ``JiraClient``.
        key: Issue key.
        time_spent: Jira duration text, e.g. ``"2h 30m"``; normalised through
            ``durations.to_jira`` so garbage is rejected before the request.
        started: ``None`` for now, else ``YYYY-MM-DD``/relative/ISO timestamp.
        comment: Optional worklog comment.

    Returns:
        ``{"key", "id", "time_spent", "started", "seconds"}``.

    Raises:
        InputError: If the key, duration or date is invalid.
        ReadOnlyError: Propagated from the client in read-only mode.
    """
    issue_key = _require_key(key)
    duration = to_jira(time_spent)
    body: dict[str, Any] = {
        "timeSpent": duration,
        "started": _started_stamp(started),
    }
    if comment:
        body["comment"] = comment
    payload = client.post(f"/rest/api/2/issue/{issue_key}/worklog", json=body)
    result = payload if isinstance(payload, Mapping) else {}
    return {
        "key": issue_key,
        "id": result.get("id"),
        "time_spent": duration,
        "started": body["started"],
        "seconds": int(result.get("timeSpentSeconds") or 0),
    }


def set_estimate(
    client,
    key: str,
    original: str | None = None,
    remaining: str | None = None,
) -> dict:
    """Set an issue's original and/or remaining estimate.

    Both values are Jira duration text (``"3h"``, ``"2d"``).  Passing neither is
    an error rather than a silent no-op.

    Returns:
        ``{"key", "original", "remaining"}`` with the normalised values actually
        sent (``None`` for a field left untouched).

    Raises:
        InputError: If the key is blank, both estimates are ``None``, or a
            duration cannot be parsed.
        ReadOnlyError: Propagated from the client in read-only mode.
    """
    issue_key = _require_key(key)
    if original is None and remaining is None:
        raise InputError("set at least one of original or remaining estimate")
    timetracking: dict[str, str] = {}
    normalized_original = to_jira(original) if original is not None else None
    normalized_remaining = to_jira(remaining) if remaining is not None else None
    if normalized_original is not None:
        timetracking["originalEstimate"] = normalized_original
    if normalized_remaining is not None:
        timetracking["remainingEstimate"] = normalized_remaining
    client.put(
        f"/rest/api/2/issue/{issue_key}",
        json={"fields": {"timetracking": timetracking}},
    )
    return {
        "key": issue_key,
        "original": normalized_original,
        "remaining": normalized_remaining,
    }
