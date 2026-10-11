"""Tests for :mod:`jira_cli.services.reports` (CONTRACT section 3).

Offline and deterministic: no network, no real Jira.  A :class:`FakeClient`
records every JQL string and every REST path it is asked for and replays canned
raw Jira payloads, including raw ``changelog`` histories.

The real core modules (``report``, ``durations``, ``errors``) and the real
``services.worklog`` are used.  Nothing is injected into :data:`sys.modules`;
the only replacements are ``monkeypatch.setattr`` calls, which pytest undoes
after each test, so this file cannot leak state into other test files.

``TestRealFixtureShapes`` replays the sanitized Jira Server 10.3 payloads under
``tests/fixtures`` (see ``SHAPES.md`` SURPRISES) through every report.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from jira_cli import durations, errors
from jira_cli.services import reports
from jira_cli.services import worklog as worklog_mod

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str):
    """Parse one sanitized live-shape fixture (a fresh copy on every call)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #

class FakeClient:
    """A recording stand-in for ``JiraClient``.

    Args:
        counts: ``jql -> total`` for :meth:`count`; unknown JQL yields 0.
        issues: ``jql -> list[raw issue]`` for :meth:`search`; a ``None`` key
            acts as the catch-all. Unknown JQL yields ``[]``.
        gets: ``path -> payload`` for :meth:`get`.
    """

    def __init__(self, counts=None, issues=None, gets=None):
        self._counts = dict(counts or {})
        self._issues = dict(issues or {})
        self._gets = dict(gets or {})
        self.counted: list[str] = []
        self.searched: list[dict] = []
        self.got: list[tuple[str, dict | None]] = []

    def count(self, jql: str) -> int:
        self.counted.append(jql)
        return int(self._counts.get(jql, 0))

    def search(self, jql, fields=None, expand=None, limit=None):
        self.searched.append(
            {"jql": jql, "fields": fields, "expand": expand, "limit": limit}
        )
        if jql in self._issues:
            return list(self._issues[jql])
        return list(self._issues.get(None, []))

    def get(self, path, params=None):
        self.got.append((path, params))
        return self._gets.get(path)

    @property
    def search_jqls(self) -> list[str]:
        return [c["jql"] for c in self.searched]


def issue(
    key="PROJ-1",
    *,
    summary="Summary",
    assignee="alice",
    assignee_display=None,
    status="To Do",
    status_category="To Do",
    updated="2026-10-01T09:00:00.000+0000",
    created="2026-09-01T09:00:00.000+0000",
    duedate=None,
    priority="Medium",
    timeestimate=None,
    timeoriginalestimate=None,
    description="Some description",
    reporter="alice",
    comments=None,
    comments_total=None,
    worklogs=None,
    worklogs_total=None,
    changelog=None,
) -> dict:
    """Build one raw Jira issue dict the way /rest/api/2/search returns it."""
    fields: dict = {
        "summary": summary,
        "created": created,
        "updated": updated,
        "duedate": duedate,
        "description": description,
        "status": {
            "name": status,
            "statusCategory": {"name": status_category},
        },
        "assignee": user(assignee, assignee_display) if assignee else None,
        "reporter": user(reporter) if reporter else None,
        "priority": {"name": priority} if priority else None,
    }
    if timeestimate is not None:
        fields["timeestimate"] = timeestimate
    if timeoriginalestimate is not None:
        fields["timeoriginalestimate"] = timeoriginalestimate
    if comments is not None:
        fields["comment"] = {
            "comments": list(comments),
            "total": comments_total if comments_total is not None else len(comments),
        }
    if worklogs is not None:
        fields["worklog"] = {
            "worklogs": list(worklogs),
            "total": worklogs_total if worklogs_total is not None else len(worklogs),
        }
    raw: dict = {"key": key, "fields": fields}
    if changelog is not None:
        raw["changelog"] = {"histories": list(changelog)}
    return raw


def user(name: str, display: str | None = None) -> dict:
    return {"name": name, "displayName": display or name.title()}


def history(author: str, created: str, items: list[dict]) -> dict:
    """A raw changelog history entry as Jira's ?expand=changelog returns it."""
    return {"author": user(author), "created": created, "items": list(items)}


def item(field: str, from_s: str | None, to_s: str | None) -> dict:
    return {
        "field": field,
        "fromString": from_s,
        "from": from_s,
        "toString": to_s,
        "to": to_s,
    }


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    """Pin 'today' so day arithmetic and relative dates are deterministic."""
    monkeypatch.setattr(reports, "_today", lambda: date(2026, 10, 11))
    monkeypatch.setattr(
        reports, "_resolve_date", lambda text, today=None: durations.resolve_date(
            text, today or date(2026, 10, 11)
        )
    )


@pytest.fixture(autouse=True)
def _neutral_worklogs(monkeypatch):
    """Default every test to "nothing logged".

    ``team_dashboard`` delegates ``logged_since`` to the real
    ``services.worklog``, which would issue its own searches against the fake
    client.  Tests that care about worklogs override this themselves.
    """
    monkeypatch.setattr(worklog_mod, "user_worklogs", lambda *a, **k: [])


def assert_report_shape(report: dict) -> None:
    """Every report must satisfy the shared shape and record its JQL + method."""
    assert set(report) >= {"title", "columns", "rows", "summary", "meta"}
    assert isinstance(report["title"], str) and report["title"]
    assert isinstance(report["columns"], list) and report["columns"]
    assert isinstance(report["rows"], list)
    assert isinstance(report["summary"], dict)
    meta = report["meta"]
    assert isinstance(meta.get("jql"), list) and meta["jql"], "meta['jql'] required"
    assert all(isinstance(j, str) and j for j in meta["jql"])
    assert isinstance(meta.get("method"), str) and meta["method"].strip()
    assert isinstance(meta.get("limitations"), str) and meta["limitations"].strip()
    for row in report["rows"]:
        for column in report["columns"]:
            assert column in row, f"row missing column {column!r}"
        for value in row.values():
            assert value is None or isinstance(value, (str, int, float, bool))


# --------------------------------------------------------------------------- #
# jql_quote
# --------------------------------------------------------------------------- #

class TestJqlQuote:
    def test_plain_value_is_double_quoted(self):
        assert reports.jql_quote("alice") == '"alice"'

    def test_embedded_double_quote_is_escaped(self):
        assert reports.jql_quote('say "hi"') == '"say \\"hi\\""'

    def test_backslash_is_escaped_before_quotes(self):
        assert reports.jql_quote("a\\b") == '"a\\\\b"'

    def test_trailing_backslash_cannot_escape_the_closing_quote(self):
        # Naive quoting would emit "a\" and leave the literal open.
        assert reports.jql_quote("a\\") == '"a\\\\"'

    def test_injection_attempt_stays_inside_the_literal(self):
        quoted = reports.jql_quote('a" OR project = X')
        assert quoted == '"a\\" OR project = X"'
        # The only unescaped quotes are the first and the last character.
        inner = quoted[1:-1]
        assert '"' not in inner.replace('\\"', "")

    def test_backslash_quote_injection_stays_inside_the_literal(self):
        quoted = reports.jql_quote('a\\" OR project = X')
        assert quoted == '"a\\\\\\" OR project = X"'

    def test_none_becomes_an_empty_literal(self):
        assert reports.jql_quote(None) == '""'

    def test_non_string_values_are_stringified(self):
        assert reports.jql_quote(12) == '"12"'

    def test_newline_is_kept_inside_the_literal(self):
        assert reports.jql_quote("a\nb") == '"a\nb"'

    def test_quoted_value_used_in_a_report_jql(self):
        client = FakeClient()
        report = reports.unassigned_issues(client, ['PROJ" OR project = X'])
        jql = report["meta"]["jql"][0]
        assert 'project = "PROJ\\" OR project = X"' in jql


# --------------------------------------------------------------------------- #
# team_dashboard
# --------------------------------------------------------------------------- #

class TestTeamDashboard:
    def _counts(self, user_="alice", project='project in ("PROJ", "OPS")'):
        base = f'assignee = "{user_}" AND {project}'
        return {
            f'{base} AND statusCategory = "To Do"': 4,
            f'{base} AND statusCategory = "In Progress"': 2,
            f'{base} AND statusCategory = Done AND resolved >= "2026-10-04"': 7,
            f"{base} AND duedate < now() AND statusCategory != Done": 1,
            f"{base} AND updated <= -5d AND statusCategory != Done": 3,
        }

    def test_counts_come_from_the_five_contract_queries(self):
        client = FakeClient(counts=self._counts())
        report = reports.team_dashboard(
            client, ["alice"], ["PROJ", "OPS"], since="7d"
        )
        row = report["rows"][0]
        assert row["user"] == "alice"
        assert row["open_todo"] == 4
        assert row["in_progress"] == 2
        assert row["done_since"] == 7
        assert row["overdue"] == 1
        assert row["stale"] == 3
        assert_report_shape(report)

    def test_every_counted_jql_is_recorded_in_meta(self):
        client = FakeClient(counts=self._counts())
        report = reports.team_dashboard(client, ["alice"], ["PROJ", "OPS"])
        for jql in client.counted:
            assert jql in report["meta"]["jql"]

    def test_status_category_clauses_match_the_contract(self):
        client = FakeClient()
        reports.team_dashboard(client, ["alice"], ["PROJ"])
        joined = "\n".join(client.counted)
        assert 'statusCategory = "To Do"' in joined
        assert 'statusCategory = "In Progress"' in joined
        assert 'statusCategory = Done AND resolved >= "2026-10-04"' in joined
        assert "duedate < now() AND statusCategory != Done" in joined
        assert "updated <= -5d AND statusCategory != Done" in joined

    def test_remaining_estimate_uses_search_with_estimate_fields_only(self):
        client = FakeClient(
            issues={
                None: [
                    issue("PROJ-1", timeestimate=3600),
                    issue("PROJ-2", timeestimate=1800),
                ]
            }
        )
        report = reports.team_dashboard(client, ["alice"], ["PROJ"])
        assert report["rows"][0]["total_estimate_open"] == 1.5
        call = client.searched[0]
        assert call["fields"] == [
            "timetracking",
            "timeestimate",
            "timeoriginalestimate",
        ]
        assert "statusCategory != Done" in call["jql"]

    def test_remaining_estimate_falls_back_to_the_original_estimate(self):
        client = FakeClient(issues={None: [issue("PROJ-1", timeoriginalestimate=7200)]})
        report = reports.team_dashboard(client, ["alice"], ["PROJ"])
        assert report["rows"][0]["total_estimate_open"] == 2.0

    def test_explicit_zero_remaining_does_not_fall_back_to_original(self):
        client = FakeClient(
            issues={
                None: [issue("PROJ-1", timeestimate=0, timeoriginalestimate=36000)]
            }
        )
        report = reports.team_dashboard(client, ["alice"], ["PROJ"])
        assert report["rows"][0]["total_estimate_open"] == 0.0

    def test_missing_estimates_count_as_zero_hours(self):
        client = FakeClient(issues={None: [issue("PROJ-1")]})
        report = reports.team_dashboard(client, ["alice"], ["PROJ"])
        assert report["rows"][0]["total_estimate_open"] == 0.0

    def test_logged_since_comes_from_worklog_user_worklogs_in_hours(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            worklog_mod,
            "user_worklogs",
            lambda client, usernames, since, until=None, projects=None: [
                e
                for e in (
                    {"author": "alice", "seconds": 3600},
                    {"author": "alice", "seconds": 5400},
                    {"author": "bob", "seconds": 9999},
                )
                if e["author"] in usernames
            ],
        )
        client = FakeClient()
        report = reports.team_dashboard(client, ["alice"], ["PROJ"], since="7d")
        assert report["rows"][0]["logged_since"] == 2.5

    def test_logged_since_is_zero_when_nothing_was_logged(self, monkeypatch):
        monkeypatch.setattr(
            worklog_mod,
            "user_worklogs",
            lambda *a, **k: [],
        )
        report = reports.team_dashboard(FakeClient(), ["alice"], ["PROJ"])
        assert report["rows"][0]["logged_since"] == 0.0

    def test_a_failing_worklog_query_is_not_swallowed(self, monkeypatch):
        def boom(*args, **kwargs):
            raise errors.JiraError("worklog search failed")

        monkeypatch.setattr(worklog_mod, "user_worklogs", boom)
        with pytest.raises(errors.JiraError):
            reports.team_dashboard(FakeClient(), ["alice"], ["PROJ"])

    def test_worklog_is_queried_with_the_resolved_since_date(self, monkeypatch):
        calls: list[tuple] = []

        def record(client, usernames, since, until=None, projects=None):
            calls.append((list(usernames), since, until, projects))
            return []

        monkeypatch.setattr(worklog_mod, "user_worklogs", record)
        reports.team_dashboard(FakeClient(), ["alice"], ["PROJ"], since="7d")
        usernames, since, until, projects = calls[0]
        assert usernames == ["alice"]
        assert since == "2026-10-04"
        assert until is None
        assert projects == ["PROJ"]

    def test_no_projects_means_no_project_clause(self):
        client = FakeClient()
        reports.team_dashboard(client, ["alice"], None)
        assert all("project" not in jql for jql in client.counted)
        assert client.counted[0] == 'assignee = "alice" AND statusCategory = "To Do"'

    def test_single_project_uses_equality_not_in(self):
        client = FakeClient()
        reports.team_dashboard(client, ["alice"], ["PROJ"])
        assert 'project = "PROJ"' in client.counted[0]
        assert "project in (" not in client.counted[0]

    def test_multiple_projects_use_an_in_clause(self):
        client = FakeClient()
        reports.team_dashboard(client, ["alice"], ["PROJ", "OPS"])
        assert 'project in ("PROJ", "OPS")' in client.counted[0]

    def test_user_order_is_preserved_and_one_row_per_user(self):
        client = FakeClient()
        report = reports.team_dashboard(client, ["bob", "alice"], ["PROJ"])
        assert [r["user"] for r in report["rows"]] == ["bob", "alice"]

    def test_summary_totals_the_numeric_columns(self):
        client = FakeClient(
            counts={
                **self._counts("alice", 'project = "PROJ"'),
                **self._counts("bob", 'project = "PROJ"'),
            }
        )
        report = reports.team_dashboard(client, ["alice", "bob"], ["PROJ"])
        assert report["summary"]["open_todo"] == 8
        assert report["summary"]["users"] == 2
        assert report["summary"]["since"] == "2026-10-04"

    def test_empty_user_list_yields_an_empty_report(self):
        client = FakeClient()
        report = reports.team_dashboard(client, [], ["PROJ"])
        assert report["rows"] == []
        assert report["summary"]["users"] == 0
        assert client.counted == []

    def test_absolute_since_date_is_used_verbatim(self):
        client = FakeClient()
        reports.team_dashboard(client, ["alice"], ["PROJ"], since="2026-01-31")
        assert any('resolved >= "2026-01-31"' in j for j in client.counted)

    def test_username_is_quoted_safely(self):
        client = FakeClient()
        reports.team_dashboard(client, ['a" OR project = X'], ["PROJ"])
        assert 'assignee = "a\\" OR project = X"' in client.counted[0]

    def test_meta_states_method_and_limitations(self):
        report = reports.team_dashboard(FakeClient(), ["alice"], ["PROJ"])
        assert "maxResults=0" in report["meta"]["method"]
        assert "hours" in report["meta"]["method"]
        assert "reassigned" in report["meta"]["limitations"]
        assert report["meta"]["projects"] == ["PROJ"]


# --------------------------------------------------------------------------- #
# person_activity
# --------------------------------------------------------------------------- #

class TestPersonActivityJql:
    def test_candidate_jql_matches_the_contract(self):
        client = FakeClient()
        reports.person_activity(client, "alice", since="7d")
        jql = client.search_jqls[0]
        assert jql == (
            '(assignee was "alice" OR assignee = "alice" '
            'OR reporter = "alice" OR worklogAuthor = "alice") '
            'AND updated >= "2026-10-04"'
        )

    def test_changelog_is_expanded_and_the_contract_fields_requested(self):
        client = FakeClient()
        reports.person_activity(client, "alice")
        call = client.searched[0]
        assert call["expand"] == ["changelog"]
        assert call["fields"] == [
            "summary",
            "created",
            "reporter",
            "comment",
            "worklog",
        ]

    def test_project_filter_is_appended(self):
        client = FakeClient()
        reports.person_activity(client, "alice", projects=["PROJ", "OPS"])
        assert client.search_jqls[0].endswith('project in ("PROJ", "OPS")')

    def test_no_projects_means_no_project_clause(self):
        client = FakeClient()
        reports.person_activity(client, "alice", projects=None)
        assert "project" not in client.search_jqls[0]

    def test_username_is_quoted_safely_in_every_predicate(self):
        client = FakeClient()
        reports.person_activity(client, 'a" OR project = X')
        jql = client.search_jqls[0]
        assert jql.count('"a\\" OR project = X"') == 4

    def test_jql_is_recorded_in_meta(self):
        client = FakeClient()
        report = reports.person_activity(client, "alice")
        assert report["meta"]["jql"] == [client.search_jqls[0]]


class TestPersonActivityEvents:
    def test_created_event_only_when_the_user_reported_it(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="alice",
                        created="2026-10-05T10:00:00.000+0000",
                    ),
                    issue(
                        "PROJ-2",
                        reporter="bob",
                        created="2026-10-06T10:00:00.000+0000",
                    ),
                ]
            }
        )
        report = reports.person_activity(client, "alice")
        created = [r for r in report["rows"] if r["action"] == "created"]
        assert [r["key"] for r in created] == ["PROJ-1"]

    def test_created_event_outside_the_window_is_dropped(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="alice",
                        created="2026-01-01T10:00:00.000+0000",
                    )
                ]
            }
        )
        report = reports.person_activity(client, "alice", since="7d")
        assert [r for r in report["rows"] if r["action"] == "created"] == []

    def test_status_change_from_raw_changelog(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        changelog=[
                            history(
                                "alice",
                                "2026-10-07T11:00:00.000+0000",
                                [item("status", "To Do", "In Progress")],
                            )
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert len(rows) == 1
        assert rows[0]["action"] == "status"
        assert rows[0]["detail"] == "status: To Do -> In Progress"

    def test_assignee_change_from_raw_changelog(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        changelog=[
                            history(
                                "alice",
                                "2026-10-07T11:00:00.000+0000",
                                [item("assignee", "bob", "carol")],
                            )
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert rows[0]["action"] == "assignee"
        assert rows[0]["detail"] == "assignee: bob -> carol"

    def test_other_field_change_is_labelled_field(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        changelog=[
                            history(
                                "alice",
                                "2026-10-07T11:00:00.000+0000",
                                [item("priority", "Low", "High")],
                            )
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert rows[0]["action"] == "field"
        assert rows[0]["detail"] == "priority: Low -> High"

    def test_missing_from_and_to_render_as_none(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        changelog=[
                            history(
                                "alice",
                                "2026-10-07T11:00:00.000+0000",
                                [item("duedate", None, None)],
                            )
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert rows[0]["detail"] == "duedate: none -> none"

    def test_changelog_history_by_another_author_is_ignored(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        changelog=[
                            history(
                                "bob",
                                "2026-10-07T11:00:00.000+0000",
                                [item("status", "To Do", "Done")],
                            )
                        ],
                    )
                ]
            }
        )
        assert reports.person_activity(client, "alice")["rows"] == []

    def test_changelog_history_outside_the_window_is_ignored(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        changelog=[
                            history(
                                "alice",
                                "2026-01-01T11:00:00.000+0000",
                                [item("status", "To Do", "Done")],
                            )
                        ],
                    )
                ]
            }
        )
        assert reports.person_activity(client, "alice")["rows"] == []

    def test_all_items_of_one_history_become_separate_events(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        changelog=[
                            history(
                                "alice",
                                "2026-10-07T11:00:00.000+0000",
                                [
                                    item("status", "To Do", "In Progress"),
                                    item("assignee", None, "alice"),
                                ],
                            )
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert {r["action"] for r in rows} == {"status", "assignee"}

    def test_comment_by_the_user_becomes_an_event(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        comments=[
                            {
                                "author": user("alice"),
                                "created": "2026-10-08T12:00:00.000+0000",
                                "body": "Looks good to me",
                            },
                            {
                                "author": user("bob"),
                                "created": "2026-10-08T13:00:00.000+0000",
                                "body": "Not mine",
                            },
                        ],
                    )
                ]
            }
        )
        rows = [
            r for r in reports.person_activity(client, "alice")["rows"]
            if r["action"] == "comment"
        ]
        assert len(rows) == 1
        assert rows[0]["detail"] == "Looks good to me"

    def test_comment_body_is_truncated_to_160_characters(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        comments=[
                            {
                                "author": user("alice"),
                                "created": "2026-10-08T12:00:00.000+0000",
                                "body": "x" * 500,
                            }
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert rows[0]["detail"] == "x" * 160

    def test_comments_are_refetched_when_total_exceeds_the_embedded_page(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        comments=[
                            {
                                "author": user("bob"),
                                "created": "2026-10-08T12:00:00.000+0000",
                                "body": "embedded only",
                            }
                        ],
                        comments_total=3,
                    )
                ]
            },
            gets={
                "/rest/api/2/issue/PROJ-1/comment": {
                    "comments": [
                        {
                            "author": user("bob"),
                            "created": "2026-10-08T12:00:00.000+0000",
                            "body": "embedded only",
                        },
                        {
                            "author": user("alice"),
                            "created": "2026-10-09T12:00:00.000+0000",
                            "body": "hidden on page two",
                        },
                        {
                            "author": user("carol"),
                            "created": "2026-10-09T13:00:00.000+0000",
                            "body": "also hidden",
                        },
                    ]
                }
            },
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert [r["detail"] for r in rows] == ["hidden on page two"]
        assert client.got == [
            ("/rest/api/2/issue/PROJ-1/comment", {"maxResults": 3})
        ]

    def test_comments_are_not_refetched_when_the_page_is_complete(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        comments=[
                            {
                                "author": user("alice"),
                                "created": "2026-10-08T12:00:00.000+0000",
                                "body": "all of them",
                            }
                        ],
                    )
                ]
            }
        )
        reports.person_activity(client, "alice")
        assert client.got == []

    def test_worklog_by_the_user_becomes_an_event_with_hours(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        worklogs=[
                            {
                                "author": user("alice"),
                                "started": "2026-10-08T09:00:00.000+0000",
                                "timeSpentSeconds": 5400,
                                "comment": "pairing",
                            },
                            {
                                "author": user("bob"),
                                "started": "2026-10-08T09:00:00.000+0000",
                                "timeSpentSeconds": 3600,
                            },
                        ],
                    )
                ]
            }
        )
        rows = [
            r for r in reports.person_activity(client, "alice")["rows"]
            if r["action"] == "worklog"
        ]
        assert len(rows) == 1
        assert rows[0]["detail"] == "logged 1.5h: pairing"

    def test_worklog_without_a_comment_omits_the_note(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        worklogs=[
                            {
                                "author": user("alice"),
                                "started": "2026-10-08T09:00:00.000+0000",
                                "timeSpentSeconds": 3600,
                            }
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert rows[0]["detail"] == "logged 1.0h"

    def test_worklogs_are_refetched_when_total_exceeds_the_embedded_page(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        worklogs=[
                            {
                                "author": user("bob"),
                                "started": "2026-10-08T09:00:00.000+0000",
                                "timeSpentSeconds": 60,
                            }
                        ],
                        worklogs_total=2,
                    )
                ]
            },
            gets={
                "/rest/api/2/issue/PROJ-1/worklog": {
                    "worklogs": [
                        {
                            "author": user("bob"),
                            "started": "2026-10-08T09:00:00.000+0000",
                            "timeSpentSeconds": 60,
                        },
                        {
                            "author": user("alice"),
                            "started": "2026-10-09T09:00:00.000+0000",
                            "timeSpentSeconds": 7200,
                        },
                    ]
                }
            },
        )
        rows = reports.person_activity(client, "alice")["rows"]
        assert [r["detail"] for r in rows] == ["logged 2.0h"]
        assert client.got == [
            ("/rest/api/2/issue/PROJ-1/worklog", {"maxResults": 2})
        ]

    def test_refetch_paths_are_recorded_outside_meta_jql(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        comments=[],
                        comments_total=2,
                    )
                ]
            },
            gets={"/rest/api/2/issue/PROJ-1/comment": {"comments": []}},
        )
        meta = reports.person_activity(client, "alice")["meta"]
        assert meta["requests"] == ["GET /rest/api/2/issue/PROJ-1/comment"]
        assert all(j.startswith("(assignee was") for j in meta["jql"])

    def test_events_outside_the_until_bound_are_dropped(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        comments=[
                            {
                                "author": user("alice"),
                                "created": "2026-10-05T12:00:00.000+0000",
                                "body": "inside",
                            },
                            {
                                "author": user("alice"),
                                "created": "2026-10-10T12:00:00.000+0000",
                                "body": "after until",
                            },
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(
            client, "alice", since="2026-10-01", until="2026-10-06"
        )["rows"]
        assert [r["detail"] for r in rows] == ["inside"]

    def test_until_bound_is_inclusive_on_the_calendar_date(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        comments=[
                            {
                                "author": user("alice"),
                                "created": "2026-10-06T23:30:00.000+0000",
                                "body": "last moment",
                            }
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(
            client, "alice", since="2026-10-01", until="2026-10-06"
        )["rows"]
        assert [r["detail"] for r in rows] == ["last moment"]

    def test_events_are_sorted_by_time_oldest_first(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="alice",
                        created="2026-10-09T08:00:00.000+0000",
                        comments=[
                            {
                                "author": user("alice"),
                                "created": "2026-10-05T12:00:00.000+0000",
                                "body": "earliest",
                            }
                        ],
                        changelog=[
                            history(
                                "alice",
                                "2026-10-07T11:00:00.000+0000",
                                [item("status", "To Do", "Done")],
                            )
                        ],
                    )
                ]
            }
        )
        rows = reports.person_activity(client, "alice", since="2026-10-01")["rows"]
        whens = [r["when"] for r in rows]
        assert whens == sorted(whens)
        assert rows[0]["detail"] == "earliest"

    def test_summary_counts_events_and_issues_touched(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="alice",
                        created="2026-10-05T08:00:00.000+0000",
                    ),
                    issue(
                        "PROJ-2",
                        reporter="alice",
                        created="2026-10-06T08:00:00.000+0000",
                    ),
                ]
            }
        )
        report = reports.person_activity(client, "alice", since="2026-10-01")
        assert report["summary"]["events"] == 2
        assert report["summary"]["issues_touched"] == 2
        assert report["summary"]["created_events"] == 2
        assert report["summary"]["user"] == "alice"

    def test_report_shape_and_columns(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="alice",
                        created="2026-10-05T08:00:00.000+0000",
                    )
                ]
            }
        )
        report = reports.person_activity(client, "alice", since="2026-10-01")
        assert report["columns"] == ["when", "key", "summary", "action", "detail"]
        assert report["title"] == "Activity: alice"
        assert_report_shape(report)

    def test_meta_limitations_name_the_undiscovered_comments(self):
        meta = reports.person_activity(FakeClient(), "alice")["meta"]
        limits = meta["limitations"].lower()
        assert "comments on issues" in limits
        assert "never assigned" in limits
        assert "never" in limits and "reported" in limits
        assert "logged work" in limits
        assert "not discovered" in limits

    def test_no_issues_yields_an_empty_but_well_formed_report(self):
        report = reports.person_activity(FakeClient(), "alice")
        assert report["rows"] == []
        assert report["summary"]["events"] == 0
        assert_report_shape(report)

    def test_user_matched_by_display_name_in_the_changelog(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        reporter="bob",
                        changelog=[
                            {
                                "author": {"displayName": "Alice"},
                                "created": "2026-10-07T11:00:00.000+0000",
                                "items": [item("status", "To Do", "Done")],
                            }
                        ],
                    )
                ]
            }
        )
        assert len(reports.person_activity(client, "alice")["rows"]) == 1


# --------------------------------------------------------------------------- #
# stale_issues
# --------------------------------------------------------------------------- #

class TestStaleIssues:
    def test_jql_and_ordering(self):
        client = FakeClient()
        report = reports.stale_issues(client, days=7)
        jql = client.search_jqls[0]
        assert jql == (
            "updated <= -7d AND statusCategory != Done ORDER BY updated ASC"
        )
        assert report["meta"]["jql"] == [jql]

    def test_project_and_assignee_filters_are_applied(self):
        client = FakeClient()
        reports.stale_issues(
            client, days=3, projects=["PROJ", "OPS"], usernames=["alice", "bob"]
        )
        jql = client.search_jqls[0]
        assert 'project in ("PROJ", "OPS")' in jql
        assert 'assignee in ("alice", "bob")' in jql

    def test_no_projects_and_no_usernames_means_no_such_clauses(self):
        client = FakeClient()
        reports.stale_issues(client, days=7, projects=None, usernames=None)
        jql = client.search_jqls[0]
        assert "project" not in jql
        assert "assignee" not in jql

    def test_days_since_update_is_computed_from_today(self):
        client = FakeClient(
            issues={None: [issue("PROJ-1", updated="2026-10-01T09:00:00.000+0000")]}
        )
        row = reports.stale_issues(client, days=7)["rows"][0]
        assert row["days_since_update"] == 10
        assert row["key"] == "PROJ-1"

    def test_rows_are_sorted_stalest_first(self):
        client = FakeClient(
            issues={
                None: [
                    issue("PROJ-1", updated="2026-10-05T09:00:00.000+0000"),
                    issue("PROJ-2", updated="2026-09-11T09:00:00.000+0000"),
                    issue("PROJ-3", updated="2026-10-01T09:00:00.000+0000"),
                ]
            }
        )
        rows = reports.stale_issues(client, days=1)["rows"]
        assert [r["key"] for r in rows] == ["PROJ-2", "PROJ-3", "PROJ-1"]

    def test_assignee_is_reported_as_the_username(self):
        client = FakeClient(
            issues={
                None: [issue("PROJ-1", assignee="alice", assignee_display="Alice A")]
            }
        )
        row = reports.stale_issues(client)["rows"][0]
        assert row["assignee"] == "alice"

    def test_assignee_username_and_display_name_are_both_reported(self):
        client = FakeClient(
            issues={
                None: [issue("PROJ-1", assignee="alice", assignee_display="Alice A")]
            }
        )
        row = reports.stale_issues(client)["rows"][0]
        assert row["assignee"] == "alice"
        assert row["assignee_name"] == "Alice A"

    def test_unassigned_issue_reports_none(self):
        client = FakeClient(issues={None: [issue("PROJ-1", assignee=None)]})
        row = reports.stale_issues(client)["rows"][0]
        assert row["assignee"] is None
        assert row["assignee_name"] is None

    def test_days_zero_is_allowed(self):
        client = FakeClient()
        reports.stale_issues(client, days=0)
        assert "updated <= -0d" in client.search_jqls[0]

    def test_negative_days_is_an_input_error(self):
        with pytest.raises(errors.InputError):
            reports.stale_issues(FakeClient(), days=-1)

    def test_non_numeric_days_is_an_input_error(self):
        with pytest.raises(errors.InputError):
            reports.stale_issues(FakeClient(), days="many")

    def test_report_shape(self):
        client = FakeClient(issues={None: [issue("PROJ-1")]})
        report = reports.stale_issues(client, days=7)
        assert report["columns"] == [
            "key",
            "summary",
            "assignee",
            "assignee_name",
            "status",
            "updated",
            "days_since_update",
        ]
        assert report["summary"] == {"issues": 1, "days": 7}
        assert_report_shape(report)


# --------------------------------------------------------------------------- #
# overdue_issues
# --------------------------------------------------------------------------- #

class TestOverdueIssues:
    def test_jql_and_ordering(self):
        client = FakeClient()
        report = reports.overdue_issues(client)
        jql = client.search_jqls[0]
        assert jql == (
            "duedate < now() AND statusCategory != Done ORDER BY duedate ASC"
        )
        assert report["meta"]["jql"] == [jql]

    def test_filters_are_applied(self):
        client = FakeClient()
        reports.overdue_issues(client, projects=["PROJ"], usernames=["alice"])
        jql = client.search_jqls[0]
        assert 'project = "PROJ"' in jql
        assert 'assignee = "alice"' in jql

    def test_days_overdue_is_computed_from_today(self):
        client = FakeClient(issues={None: [issue("PROJ-1", duedate="2026-10-04")]})
        row = reports.overdue_issues(client)["rows"][0]
        assert row["due"] == "2026-10-04"
        assert row["days_overdue"] == 7

    def test_rows_are_sorted_most_overdue_first(self):
        client = FakeClient(
            issues={
                None: [
                    issue("PROJ-1", duedate="2026-10-09"),
                    issue("PROJ-2", duedate="2026-08-01"),
                ]
            }
        )
        rows = reports.overdue_issues(client)["rows"]
        assert [r["key"] for r in rows] == ["PROJ-2", "PROJ-1"]

    def test_missing_due_date_yields_none_days_overdue(self):
        client = FakeClient(issues={None: [issue("PROJ-1", duedate=None)]})
        row = reports.overdue_issues(client)["rows"][0]
        assert row["days_overdue"] is None

    def test_meta_limitations_warn_about_issues_with_no_due_date(self):
        meta = reports.overdue_issues(FakeClient())["meta"]
        assert "no due date" in meta["limitations"]

    def test_report_shape(self):
        client = FakeClient(issues={None: [issue("PROJ-1", duedate="2026-10-01")]})
        report = reports.overdue_issues(client)
        assert report["columns"] == [
            "key",
            "summary",
            "assignee",
            "assignee_name",
            "status",
            "due",
            "days_overdue",
            "updated",
        ]
        assert_report_shape(report)


# --------------------------------------------------------------------------- #
# incomplete_issues
# --------------------------------------------------------------------------- #

class TestIncompleteIssues:
    def test_jql_covers_open_issues_only(self):
        client = FakeClient()
        reports.incomplete_issues(client)
        assert client.search_jqls[0] == (
            "statusCategory != Done ORDER BY created DESC"
        )

    def test_description_is_requested_alongside_the_list_fields(self):
        client = FakeClient()
        reports.incomplete_issues(client)
        assert "description" in client.searched[0]["fields"]

    def test_complete_issues_are_omitted(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        description="full",
                        priority="High",
                        timeoriginalestimate=3600,
                        assignee="alice",
                        duedate="2026-12-01",
                    )
                ]
            }
        )
        assert reports.incomplete_issues(client)["rows"] == []

    def test_each_missing_field_is_named(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        description="",
                        priority=None,
                        assignee=None,
                        duedate=None,
                    )
                ]
            }
        )
        row = reports.incomplete_issues(client)["rows"][0]
        assert row["missing"] == "description, priority, estimate, assignee, due"
        assert row["missing_count"] == 5

    def test_whitespace_only_description_counts_as_missing(self):
        client = FakeClient(
            issues={None: [issue("PROJ-1", description="   \n  ")]}
        )
        row = reports.incomplete_issues(client, checks=("description",))["rows"][0]
        assert row["missing"] == "description"

    def test_zero_original_estimate_counts_as_estimated(self):
        client = FakeClient(
            issues={None: [issue("PROJ-1", timeoriginalestimate=0)]}
        )
        assert reports.incomplete_issues(client, checks=("estimate",))["rows"] == []

    def test_positive_remaining_estimate_alone_counts_as_estimated(self):
        client = FakeClient(issues={None: [issue("PROJ-1", timeestimate=1800)]})
        assert reports.incomplete_issues(client, checks=("estimate",))["rows"] == []

    def test_bare_zero_remaining_is_not_an_estimate(self):
        # Jira Server sets timeestimate=0 once work is logged on an issue
        # nobody estimated; that must still be flagged.
        client = FakeClient(issues={None: [issue("PROJ-1", timeestimate=0)]})
        rows = reports.incomplete_issues(client, checks=("estimate",))["rows"]
        assert rows[0]["missing"] == "estimate"

    def test_original_estimate_alone_satisfies_the_estimate_check(self):
        client = FakeClient(
            issues={None: [issue("PROJ-1", timeoriginalestimate=7200)]}
        )
        assert reports.incomplete_issues(client, checks=("estimate",))["rows"] == []

    def test_absent_estimate_fields_count_as_missing(self):
        client = FakeClient(issues={None: [issue("PROJ-1")]})
        rows = reports.incomplete_issues(client, checks=("estimate",))["rows"]
        assert rows[0]["missing"] == "estimate"

    def test_only_the_selected_checks_are_applied(self):
        client = FakeClient(
            issues={None: [issue("PROJ-1", description="", priority=None)]}
        )
        rows = reports.incomplete_issues(client, checks=("priority",))["rows"]
        assert rows[0]["missing"] == "priority"

    def test_rows_are_sorted_by_missing_count_then_key(self):
        client = FakeClient(
            issues={
                None: [
                    issue("PROJ-2", description="ok", priority=None),
                    issue("PROJ-1", description="", priority=None),
                    issue("PROJ-3", description="ok", priority=None),
                ]
            }
        )
        rows = reports.incomplete_issues(
            client, checks=("description", "priority")
        )["rows"]
        assert [r["key"] for r in rows] == ["PROJ-1", "PROJ-2", "PROJ-3"]

    def test_summary_counts_each_missing_field(self):
        client = FakeClient(
            issues={
                None: [
                    issue("PROJ-1", description="", priority=None),
                    issue("PROJ-2", description="ok", priority=None),
                ]
            }
        )
        summary = reports.incomplete_issues(
            client, checks=("description", "priority")
        )["summary"]
        assert summary["issues"] == 2
        assert summary["missing_description"] == 1
        assert summary["missing_priority"] == 2

    def test_unknown_check_is_an_input_error_naming_the_allowed_values(self):
        with pytest.raises(errors.InputError) as excinfo:
            reports.incomplete_issues(FakeClient(), checks=("bogus",))
        assert "bogus" in str(excinfo.value)

    def test_empty_checks_is_an_input_error(self):
        with pytest.raises(errors.InputError):
            reports.incomplete_issues(FakeClient(), checks=())

    def test_checks_are_case_insensitive(self):
        client = FakeClient(issues={None: [issue("PROJ-1", priority=None)]})
        rows = reports.incomplete_issues(client, checks=("PRIORITY",))["rows"]
        assert rows[0]["missing"] == "priority"

    def test_filters_are_applied(self):
        client = FakeClient()
        reports.incomplete_issues(
            client, projects=["PROJ"], usernames=["alice"]
        )
        jql = client.search_jqls[0]
        assert 'project = "PROJ"' in jql
        assert 'assignee = "alice"' in jql

    def test_report_shape(self):
        client = FakeClient(issues={None: [issue("PROJ-1", priority=None)]})
        report = reports.incomplete_issues(client)
        assert report["columns"] == [
            "key",
            "summary",
            "assignee",
            "assignee_name",
            "status",
            "missing",
            "missing_count",
            "updated",
        ]
        assert_report_shape(report)


# --------------------------------------------------------------------------- #
# unassigned_issues
# --------------------------------------------------------------------------- #

class TestUnassignedIssues:
    def test_jql_and_ordering(self):
        client = FakeClient()
        report = reports.unassigned_issues(client, ["PROJ"])
        assert client.search_jqls[0] == (
            'assignee is EMPTY AND statusCategory != Done AND project = "PROJ" '
            "ORDER BY created ASC"
        )
        assert report["meta"]["jql"] == client.search_jqls

    def test_multiple_projects_use_an_in_clause(self):
        client = FakeClient()
        reports.unassigned_issues(client, ["PROJ", "OPS"])
        assert 'project in ("PROJ", "OPS")' in client.search_jqls[0]

    def test_no_projects_means_no_project_clause(self):
        client = FakeClient()
        reports.unassigned_issues(client, None)
        assert client.search_jqls[0] == (
            "assignee is EMPTY AND statusCategory != Done ORDER BY created ASC"
        )

    def test_rows_carry_the_age_of_the_last_update(self):
        client = FakeClient(
            issues={
                None: [
                    issue(
                        "PROJ-1",
                        assignee=None,
                        updated="2026-10-04T09:00:00.000+0000",
                        duedate="2026-11-01",
                    )
                ]
            }
        )
        row = reports.unassigned_issues(client, ["PROJ"])["rows"][0]
        assert row["days_since_update"] == 7
        assert row["due"] == "2026-11-01"

    def test_report_shape(self):
        client = FakeClient(issues={None: [issue("PROJ-1", assignee=None)]})
        report = reports.unassigned_issues(client, ["PROJ"])
        assert report["columns"] == [
            "key",
            "summary",
            "status",
            "due",
            "updated",
            "days_since_update",
        ]
        assert report["summary"] == {"issues": 1}
        assert_report_shape(report)


# --------------------------------------------------------------------------- #
# workload
# --------------------------------------------------------------------------- #

class TestWorkload:
    def test_one_search_per_user_over_open_issues(self):
        client = FakeClient()
        report = reports.workload(client, ["alice", "bob"], ["PROJ"])
        assert client.search_jqls == [
            'assignee = "alice" AND statusCategory != Done AND project = "PROJ" '
            "ORDER BY duedate ASC",
            'assignee = "bob" AND statusCategory != Done AND project = "PROJ" '
            "ORDER BY duedate ASC",
        ]
        assert report["meta"]["jql"] == client.search_jqls

    def test_no_projects_means_no_project_clause(self):
        client = FakeClient()
        reports.workload(client, ["alice"], None)
        assert client.search_jqls == [
            'assignee = "alice" AND statusCategory != Done ORDER BY duedate ASC'
        ]

    def test_remaining_hours_sum_the_remaining_estimate(self):
        client = FakeClient(
            issues={
                None: [
                    issue("PROJ-1", timeestimate=3600),
                    issue("PROJ-2", timeestimate=5400),
                ]
            }
        )
        row = reports.workload(client, ["alice"])["rows"][0]
        assert row["remaining_hours"] == 2.5
        assert row["open_issues"] == 2

    def test_remaining_hours_fall_back_to_the_original_estimate(self):
        client = FakeClient(
            issues={None: [issue("PROJ-1", timeoriginalestimate=3600)]}
        )
        assert reports.workload(client, ["alice"])["rows"][0]["remaining_hours"] == 1.0

    def test_zero_remaining_does_not_fall_back_to_original(self):
        client = FakeClient(
            issues={
                None: [issue("PROJ-1", timeestimate=0, timeoriginalestimate=36000)]
            }
        )
        row = reports.workload(client, ["alice"])["rows"][0]
        assert row["remaining_hours"] == 0.0
        assert row["unestimated"] == 1

    def test_unestimated_counts_issues_with_no_remaining_work(self):
        client = FakeClient(
            issues={
                None: [issue("PROJ-1"), issue("PROJ-2", timeestimate=3600)]
            }
        )
        assert reports.workload(client, ["alice"])["rows"][0]["unestimated"] == 1

    def test_in_progress_counts_the_in_progress_status_category(self):
        client = FakeClient(
            issues={
                None: [
                    issue("PROJ-1", status_category="In Progress"),
                    issue("PROJ-2", status_category="To Do"),
                ]
            }
        )
        assert reports.workload(client, ["alice"])["rows"][0]["in_progress"] == 1

    def test_user_order_is_preserved(self):
        client = FakeClient()
        report = reports.workload(client, ["bob", "alice"])
        assert [r["user"] for r in report["rows"]] == ["bob", "alice"]

    def test_summary_totals_across_users(self):
        client = FakeClient(issues={None: [issue("PROJ-1", timeestimate=3600)]})
        summary = reports.workload(client, ["alice", "bob"])["summary"]
        assert summary["users"] == 2
        assert summary["open_issues"] == 2
        assert summary["remaining_hours"] == 2.0

    def test_empty_user_list_yields_an_empty_report(self):
        client = FakeClient()
        report = reports.workload(client, [])
        assert report["rows"] == []
        assert report["meta"]["jql"] == []
        assert client.searched == []

    def test_username_is_quoted_safely(self):
        client = FakeClient()
        reports.workload(client, ['a" OR project = X'])
        assert 'assignee = "a\\" OR project = X"' in client.search_jqls[0]

    def test_report_shape_and_limitations(self):
        client = FakeClient(issues={None: [issue("PROJ-1", timeestimate=3600)]})
        report = reports.workload(client, ["alice"])
        assert report["columns"] == [
            "user",
            "open_issues",
            "in_progress",
            "remaining_hours",
            "unestimated",
        ]
        assert "unestimated" in report["meta"]["limitations"]
        assert_report_shape(report)


# --------------------------------------------------------------------------- #
# Real Jira Server shapes (tests/fixtures, see SHAPES.md SURPRISES)
# --------------------------------------------------------------------------- #

def _search_issues() -> list[dict]:
    """The five raw issues of ``search_changelog.json`` (expand=changelog)."""
    return load_fixture("search_changelog.json")["issues"]


def _sprint_issues() -> list[dict]:
    """The raw issues of ``sprint_issues.json`` (Sprint field is a blob)."""
    return load_fixture("sprint_issues.json")["issues"]


class TestRealFixtureShapes:
    """Every report must cope with the payloads the real server sends."""

    def test_fixture_carries_the_surprising_shapes_under_test(self):
        # Guard: if the fixtures are regenerated without these traits, the
        # tests below would silently stop exercising them.
        sprint = _sprint_issues()[0]["fields"]
        assert sprint["timetracking"] == {}
        assert isinstance(sprint["customfield_10109"], list)
        assert sprint["customfield_10109"][0].startswith(
            "com.atlassian.greenhopper.service.sprint.Sprint@"
        )
        categories = {
            i["fields"]["status"]["statusCategory"]["key"] for i in _search_issues()
        }
        assert categories == {"done", "undefined"}
        changelog = _search_issues()[0]["changelog"]
        assert set(changelog) >= {"startAt", "maxResults", "total", "histories"}

    def test_stale_rows_from_real_issues_are_scalar_and_identify_the_user(self):
        client = FakeClient(issues={None: _search_issues()})
        report = reports.stale_issues(client, days=0)
        assert_report_shape(report)
        by_key = {r["key"]: r for r in report["rows"]}
        assert by_key["PRJ21-567"]["assignee"] == "bob"
        assert by_key["PRJ21-567"]["assignee_name"] == "Bob"
        assert by_key["PRJ21-567"]["status"] == "Done"
        # updated carries a +0330 offset; the calendar date is used as-is.
        assert by_key["PRJ21-567"]["days_since_update"] == 0

    def test_sprint_blob_never_reaches_a_report_cell(self):
        client = FakeClient(issues={None: _sprint_issues()})
        produced = [
            reports.stale_issues(client, days=0),
            reports.overdue_issues(client),
            reports.incomplete_issues(client),
            reports.unassigned_issues(client, ["PROJ"]),
            reports.workload(client, ["alice"]),
        ]
        for report in produced:
            assert_report_shape(report)
            for row in report["rows"]:
                assert not any(
                    "greenhopper" in str(value) for value in row.values()
                )

    def test_workload_counts_in_progress_by_category_key_not_name(self):
        issues = _sprint_issues()
        category = issues[0]["fields"]["status"]["statusCategory"]
        # A localised label must not matter; only the stable key does.
        category.update({"key": "indeterminate", "id": 4, "name": "Sample text"})
        client = FakeClient(issues={None: issues})
        row = reports.workload(client, ["alice"])["rows"][0]
        assert row["open_issues"] == len(issues)
        assert row["in_progress"] == 1

    def test_no_category_status_is_not_in_progress(self):
        client = FakeClient(issues={None: _search_issues()})
        row = reports.workload(client, ["erin"])["rows"][0]
        # PRJ21-554 is in Jira's "No Category" bucket (key 'undefined').
        assert row["in_progress"] == 0

    def test_empty_timetracking_falls_back_to_top_level_fields(self):
        issues = _sprint_issues()
        issues[0]["fields"]["timeestimate"] = 5400
        client = FakeClient(issues={None: issues[:1]})
        row = reports.workload(client, ["alice"])["rows"][0]
        assert row["remaining_hours"] == 1.5

    def test_timetracking_seconds_win_over_top_level_fields(self):
        issues = _sprint_issues()
        fields = issues[0]["fields"]
        fields["timetracking"] = {
            "originalEstimate": "1d",
            "remainingEstimate": "2h",
            "originalEstimateSeconds": 28800,
            "remainingEstimateSeconds": 7200,
        }
        fields["timeestimate"] = 99999  # hidden/stale sibling must lose
        client = FakeClient(issues={None: issues[:1]})
        row = reports.workload(client, ["alice"])["rows"][0]
        assert row["remaining_hours"] == 2.0
        incomplete = reports.incomplete_issues(client, checks=("estimate",))
        assert incomplete["rows"] == []

    def test_timetracking_original_only_is_used_when_remaining_absent(self):
        issues = _sprint_issues()
        fields = issues[0]["fields"]
        fields["timetracking"] = {"originalEstimateSeconds": 3600}
        fields.pop("timeestimate", None)
        fields.pop("timeoriginalestimate", None)
        client = FakeClient(issues={None: issues[:1]})
        assert reports.workload(client, ["alice"])["rows"][0]["remaining_hours"] == 1.0

    def test_auto_zeroed_remaining_estimate_is_flagged_incomplete(self):
        # Every search_changelog issue has timeestimate=0 and no original
        # estimate: Jira zeroed it when work was logged, nobody estimated.
        client = FakeClient(issues={None: _search_issues()})
        report = reports.incomplete_issues(client, checks=("estimate",))
        assert {r["key"] for r in report["rows"]} == {
            i["key"] for i in _search_issues()
        }

    def test_incomplete_names_every_gap_on_a_real_issue(self):
        client = FakeClient(issues={None: _search_issues()})
        rows = {r["key"]: r for r in reports.incomplete_issues(client)["rows"]}
        assert rows["PRJ21-554"]["missing"] == "description, estimate, due"
        assert rows["PRJ21-554"]["assignee"] == "erin"

    def test_person_activity_reads_the_real_changelog_layout(self):
        client = FakeClient(issues={None: _search_issues()})
        report = reports.person_activity(client, "bob", since="2026-10-11")
        assert_report_shape(report)
        rows = report["rows"]
        assert {r["key"] for r in rows} == {"PRJ21-567", "PRJ21-568"}
        assert report["summary"]["status_events"] == 2
        assert report["summary"]["assignee_events"] == 2
        assert report["summary"]["field_events"] == 10
        status_rows = [r for r in rows if r["action"] == "status"]
        assert all(r["detail"] == "status: Open -> Done" for r in status_rows)
        # Embedded changelog was complete (total == len), so no re-fetch.
        assert client.got == []

    def test_changelog_attribution_ignores_other_authors(self):
        client = FakeClient(issues={None: _search_issues()})
        rows = reports.person_activity(client, "grace", since="2026-10-10")["rows"]
        assert {r["key"] for r in rows} == {"PRJ21-113", "PRJ21-553"}
        assert {r["action"] for r in rows} == {"assignee"}

    def test_truncated_changelog_is_refetched_from_the_issue(self):
        issues = _search_issues()[:1]
        full = copy.deepcopy(issues[0]["changelog"])
        issues[0]["changelog"]["histories"] = full["histories"][:1]
        issues[0]["changelog"]["maxResults"] = 1
        path = "/rest/api/2/issue/PRJ21-567"
        client = FakeClient(
            issues={None: issues}, gets={path: {"key": "PRJ21-567", "changelog": full}}
        )
        report = reports.person_activity(client, "bob", since="2026-10-11")
        assert client.got == [(path, {"expand": "changelog", "fields": "summary"})]
        assert f"GET {path}?expand=changelog" in report["meta"]["requests"]
        assert report["summary"]["status_events"] == 1
        assert report["summary"]["assignee_events"] == 1

    def test_display_name_does_not_impersonate_a_username(self):
        # A user whose display name equals another user's username must not
        # have their changelog entries attributed to that other user.
        issues = _search_issues()[:1]
        for history in issues[0]["changelog"]["histories"]:
            history["author"]["displayName"] = "alice"
        client = FakeClient(issues={None: issues})
        rows = reports.person_activity(client, "alice", since="2026-10-11")["rows"]
        assert rows == []


# --------------------------------------------------------------------------- #
# Cross-report invariants
# --------------------------------------------------------------------------- #

def test_every_report_records_its_jql_and_states_its_method():
    """CONTRACT: every report records its JQL in meta['jql'] and states method."""
    client = FakeClient(issues={None: [issue("PROJ-1", timeestimate=3600)]})
    produced = [
        reports.team_dashboard(client, ["alice"], ["PROJ"]),
        reports.person_activity(client, "alice"),
        reports.stale_issues(client, 7, ["PROJ"]),
        reports.overdue_issues(client, ["PROJ"]),
        reports.incomplete_issues(client, ["PROJ"]),
        reports.unassigned_issues(client, ["PROJ"]),
        reports.workload(client, ["alice"], ["PROJ"]),
    ]
    for report in produced:
        assert_report_shape(report)


def test_public_api_matches_the_contract():
    assert set(reports.__all__) == {
        "jql_quote",
        "team_dashboard",
        "person_activity",
        "stale_issues",
        "overdue_issues",
        "incomplete_issues",
        "unassigned_issues",
        "workload",
    }
    for name in reports.__all__:
        assert callable(getattr(reports, name))
