"""Menu item 7 - Find issue.

A numbered choice comes first, so nothing has to be typed from memory:

1. an issue key or a pasted browse URL (``https://host/browse/PROJ-12``),
2. a JQL search,
3. a saved filter, picked from one numbered list of the local filters file and
   the operator's server favourites (a name typed instead is matched
   case-insensitively),
4. an issue used earlier in this session.

Search results are paged: the table says ``showing 1-50 of 109`` and offers the
next page instead of silently stopping at the first 50.  Typing a row number
opens that issue straight in the Issue actions list.
"""

from __future__ import annotations

from typing import Any

from jira_cli.cli import menu_actions
from jira_cli.cli.menu import (
    _ISSUE_COLUMNS,
    _as_path,
    _ask,
    _Back,
    _choose,
    _error_line,
    _export_report,
    _issue_text,
    _make_report,
    _render_report,
    _render_rows,
    _Session,
    _svc,
)
from jira_cli.errors import JiraError

#: Issues per result page.
PAGE_SIZE = 50

_RESULT_COLUMNS = ["#", *_ISSUE_COLUMNS]


def _is_bad_query(exc: JiraError) -> bool:
    """Jira answers 400 to a JQL it cannot parse - an input mistake, not an outage."""
    return getattr(exc, "status", None) == 400


# ----------------------------------------------------------------- results


def fetch_page(session: _Session, jql: str, page: int) -> dict:
    """One page of ``jql``: ``{"jql", "page", "start", "total", "issues"}``.

    ``total`` is the server's match count, so the menu can say how much it is
    not showing.
    """
    start = page * PAGE_SIZE
    total = int(session.client.count(jql))
    hits = _svc("issues").search_issues(session.client, jql, limit=start + PAGE_SIZE)
    return {"jql": jql, "page": page, "start": start, "total": total, "issues": hits[start:]}


def showing_text(data: dict) -> str:
    """``showing 1-50 of 109`` (or ``no matches``)."""
    count = len(data["issues"])
    if not count:
        return "no matches" if not data["total"] else f"no rows on this page (of {data['total']})"
    first, last = data["start"] + 1, data["start"] + count
    return f"showing {first}-{last} of {max(data['total'], last)}"


def _has_next(data: dict) -> bool:
    return data["start"] + len(data["issues"]) < data["total"]


def page_report(title: str, data: dict) -> dict:
    """The page as the shared report shape, with a ``#`` column to pick rows by."""
    rows = []
    for number, issue in enumerate(data["issues"], start=data["start"] + 1):
        row = {column: issue.get(column) for column in _ISSUE_COLUMNS}
        rows.append({"#": number, **row})
    summary = {"showing": showing_text(data), "total": data["total"]}
    return _make_report(title, _RESULT_COLUMNS, rows, summary=summary, meta={"jql": [data["jql"]]})


def _results_prompt(data: dict) -> str:
    parts = []
    if data["issues"]:
        parts.append("row # to open")
    if _has_next(data):
        parts.append("n)ext page")
    if data["page"] > 0:
        parts.append("p)revious page")
    parts += ["e)xport this page", "b)ack"]
    return ", ".join(parts)


def _open_row(session: _Session, data: dict, number: int) -> bool:
    """Open row ``number`` in Issue actions; ``False`` when it is not on this page."""
    first = data["start"] + 1
    last = data["start"] + len(data["issues"])
    if not first <= number <= last:
        where = f"rows {first}-{last}" if data["issues"] else "no rows"
        session.say(f"row {number} is not on this page ({where})")
        return False
    key = str(data["issues"][number - first].get("key"))
    try:
        menu_actions.actions_for(session, key)
    except _Back:
        pass
    return True


def browse_results(session: _Session, title: str, data: dict) -> None:
    """Show ``data`` and let the operator page, open a row, or export."""
    redraw = True
    while True:
        report = page_report(title, data)
        session.last_report = report
        if redraw:
            session.io.show(_render_report(report))
            session.say(showing_text(data))
        redraw = False
        try:
            answer = _ask(session.io, _results_prompt(data), default="b").casefold()
        except _Back:
            return
        if answer.isdigit():
            redraw = _open_row(session, data, int(answer))
        elif answer in ("n", "next") and _has_next(data):
            data, redraw = fetch_page(session, data["jql"], data["page"] + 1), True
        elif answer in ("p", "prev", "previous") and data["page"] > 0:
            data, redraw = fetch_page(session, data["jql"], data["page"] - 1), True
        elif answer in ("e", "export"):
            _export_report(session, report)
        elif answer in ("n", "next"):
            session.say("this is the last page")
        elif answer in ("p", "prev", "previous"):
            session.say("this is the first page")
        else:
            session.say(f"type {_results_prompt(data)}")


def search_and_browse(session: _Session, title: str, jql: str) -> bool:
    """Run ``jql`` and browse it.  ``False`` (after saying why) when Jira rejects the JQL."""
    try:
        data = fetch_page(session, jql, 0)
    except JiraError as exc:
        if not _is_bad_query(exc):
            raise
        session.say(_error_line(exc))
        return False
    browse_results(session, title, data)
    return True


# ------------------------------------------------------------------- modes


def _show_and_offer_actions(session: _Session, key: str) -> None:
    session.io.show(_issue_text(_svc("issues").get_issue(session.client, key)))
    try:
        answer = _ask(session.io, f"a)ctions on {key}, b)ack", default="b").casefold()
    except _Back:
        return
    if answer in ("a", "actions"):
        menu_actions.actions_for(session, key)


def find_by_key(session: _Session) -> None:
    key, _issue = menu_actions.ask_issue(session, "Issue key or browse URL")
    _show_and_offer_actions(session, key)


def find_by_jql(session: _Session) -> None:
    jql = getattr(session, "last_jql", None)
    while True:
        jql = _ask(session.io, "JQL", default=jql, required=True)
        session.last_jql = jql
        if search_and_browse(session, f"Search: {jql}", jql):
            return


def filter_entries(session: _Session) -> list[dict]:
    """Local filters then server favourites: ``[{"name", "jql", "source"}]``.

    A broken filters file is reported with its real error (and the server
    favourites are still offered) instead of being mistaken for JQL.
    """
    filters = _svc("filters")
    path = _as_path(getattr(session.settings, "filters_file", None), "filters.yaml")
    entries: list[dict] = []
    try:
        local = filters.list_filters(path) or {}
    except JiraError as exc:
        session.say(_error_line(exc))
        local = {}
    for name, value in sorted(local.items(), key=lambda item: str(item[0]).casefold()):
        jql = value.get("jql") if isinstance(value, dict) else value
        entries.append({"name": str(name), "jql": str(jql or ""), "source": "local"})
    try:
        server = filters.server_filters(session.client) or []
    except JiraError as exc:
        reason = _error_line(exc).removeprefix("error: ")
        session.say(f"warning: server favourites unavailable: {reason}")
        server = []
    for item in sorted(server, key=lambda f: str(f.get("name") or "").casefold()):
        if item.get("jql"):
            entries.append({"name": str(item.get("name")), "jql": str(item["jql"]), "source": "server"})
    return [entry for entry in entries if entry["jql"].strip()]


def match_filter(entries: list[dict], text: str) -> dict | str:
    """A row number or a case-insensitive name -> the entry, else a reason string."""
    raw = (text or "").strip()
    if raw.isdigit():
        number = int(raw)
        if 1 <= number <= len(entries):
            return entries[number - 1]
        return f"pick a number from 1 to {len(entries)}"
    matches = [e for e in entries if e["name"].casefold() == raw.casefold()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return f"{len(matches)} filters are named {raw!r}; pick one by number"
    return f"no saved filter named {raw!r}; pick a number from the list"


def find_by_filter(session: _Session) -> None:
    entries = filter_entries(session)
    if not entries:
        path = _as_path(getattr(session.settings, "filters_file", None), "filters.yaml")
        session.say(f"no saved filters in {path} and no server favourites")
        return
    rows = [{"#": n, **entry} for n, entry in enumerate(entries, start=1)]
    while True:
        session.io.show(_render_rows(["#", "name", "source", "jql"], rows))
        answer = _ask(session.io, "Filter (number or name)", required=True)
        chosen: Any = match_filter(entries, answer)
        if isinstance(chosen, str):
            session.say(f"error: {chosen}")
            continue
        if search_and_browse(session, f"Filter: {chosen['name']}", chosen["jql"]):
            return


def find_recent(session: _Session) -> None:
    keys = menu_actions.recent_keys(session)
    if not keys:
        session.say("no issues used in this session yet")
        return
    key = keys[_choose(session.io, "Recent issues", keys)]
    if menu_actions.fetch_issue(session, key) is None:
        return
    menu_actions.remember_key(session, key)
    _show_and_offer_actions(session, key)


_MODES = [
    ("Issue key or browse URL", find_by_key),
    ("JQL search", find_by_jql),
    ("Saved filter (local or server favourite)", find_by_filter),
    ("Recent issue", find_recent),
]


def run(session: _Session) -> None:
    """Main-menu handler for item 7.  ``b`` at the mode list returns to the main menu."""
    while True:
        recent = menu_actions.recent_keys(session)
        labels = [label for label, _ in _MODES]
        labels[3] += f" ({', '.join(recent[:3])})" if recent else " (none yet)"
        index = _choose(session.io, "Find issue", labels)
        try:
            _MODES[index][1](session)
        except _Back:
            continue
