"""Server-side caps on unbounded report and directory fetches.

Report tools and the people directory are reachable from the MCP server, so a
single call with a broad query must not page through every issue, project or
group member on the Jira server.  Only the HTTP client is faked here; no test
touches the network.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from jira_cli import errors
from jira_cli.client import JiraClient
from jira_cli.config import AUTH_PAT, Settings
from jira_cli.services import directory, reports, worklog

API = "/rest/api/2"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def _issue(n: int) -> dict:
    return {
        "key": f"PROJ-{n}",
        "fields": {
            "summary": f"Issue {n}",
            "assignee": {"name": "alice", "displayName": "Alice"},
            "status": {"name": "Open", "statusCategory": {"key": "new"}},
            "updated": "2026-10-01T09:00:00.000+0000",
            "created": "2026-10-01T09:00:00.000+0000",
            "duedate": "2026-10-02",
            "reporter": {"name": "bob"},
        },
    }


class SearchClient:
    """Honours ``limit`` like ``JiraClient.paginate`` does; records every call."""

    def __init__(self, matching: int) -> None:
        self.matching = matching
        self.searches: list[dict] = []
        self.gets: list[str] = []

    def search(self, jql, fields=None, expand=None, limit=None):
        self.searches.append({"jql": jql, "limit": limit})
        total = self.matching if limit is None else min(self.matching, limit)
        return [_issue(n) for n in range(1, total + 1)]

    def count(self, jql: str) -> int:
        return 0

    def get(self, path: str, params: dict | None = None) -> Any:
        self.gets.append(path)
        if path == f"{API}/user":
            return {"name": (params or {}).get("username"), "active": True}
        if path.endswith("/worklog"):
            return {"worklogs": []}
        raise AssertionError(f"unexpected GET {path}")


@pytest.fixture
def cap3(monkeypatch):
    monkeypatch.setattr(reports, "MAX_FETCH", 3)
    return 3


# --------------------------------------------------------------------------- #
# capped_search
# --------------------------------------------------------------------------- #


def test_capped_search_asks_for_one_more_than_the_cap(cap3):
    client = SearchClient(matching=2)
    notes: list[str] = []
    reports.capped_search(client, "project = PROJ", notes=notes)
    assert client.searches[0]["limit"] == 4


def test_capped_search_at_exactly_the_cap_is_not_truncated(cap3):
    notes: list[str] = []
    issues = reports.capped_search(SearchClient(matching=3), "project = PROJ", notes=notes)
    assert len(issues) == 3
    assert notes == []


def test_capped_search_one_past_the_cap_is_truncated_with_a_note(cap3):
    notes: list[str] = []
    issues = reports.capped_search(SearchClient(matching=4), "project = PROJ", notes=notes)
    assert [i["key"] for i in issues] == ["PROJ-1", "PROJ-2", "PROJ-3"]
    assert len(notes) == 1
    assert notes[0].startswith("truncated: more than 3 issues matched")


@pytest.mark.parametrize("bad", [0, -1])
def test_capped_search_refuses_a_cap_below_one_instead_of_unbounded(monkeypatch, bad):
    monkeypatch.setattr(reports, "MAX_FETCH", bad)
    with pytest.raises(errors.InputError):
        reports.capped_search(SearchClient(matching=1), "project = PROJ", notes=[])


def test_default_cap_is_a_finite_positive_bound():
    assert 1 <= reports.MAX_FETCH <= 10000


def test_real_client_stops_paging_at_the_cap_even_when_server_total_is_huge(cap3, monkeypatch):
    settings = Settings(
        base_url="https://jira.example.com",
        auth_mode=AUTH_PAT,
        username="alice",
        secret="example-token-value",  # noqa: S106 - fake credential, never sent
    )
    client = JiraClient(settings, session=requests.Session())
    pages: list[dict] = []

    def fake_get(path, params=None):
        pages.append(dict(params or {}))
        start, size = params["startAt"], params["maxResults"]
        return {
            "total": 100000,
            "issues": [_issue(n) for n in range(start + 1, start + size + 1)],
        }

    monkeypatch.setattr(client, "get", fake_get)  # no request can reach the network
    notes: list[str] = []
    issues = reports.capped_search(client, "project = PROJ", notes=notes)
    assert len(issues) == 3
    assert sum(p["maxResults"] for p in pages) == 4
    assert notes


# --------------------------------------------------------------------------- #
# Every report marks truncation
# --------------------------------------------------------------------------- #

REPORTS = {
    "stale": lambda c: reports.stale_issues(c, days=0),
    "overdue": lambda c: reports.overdue_issues(c),
    "incomplete": lambda c: reports.incomplete_issues(c),
    "unassigned": lambda c: reports.unassigned_issues(c),
    "workload": lambda c: reports.workload(c, ["alice"]),
    "person_activity": lambda c: reports.person_activity(c, "alice", since="2026-01-01"),
}


@pytest.fixture
def no_worklogs(monkeypatch):
    monkeypatch.setattr(worklog, "user_worklogs", lambda *a, **k: [])


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_report_over_the_cap_is_marked_truncated(cap3, name):
    client = SearchClient(matching=50)
    report = REPORTS[name](client)
    assert report["meta"]["truncated"] is True
    assert report["summary"]["truncated"].startswith("yes")
    assert any(n.startswith("truncated:") for n in report["meta"]["notes"])
    assert all(s["limit"] == 4 for s in client.searches)


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_report_under_the_cap_keeps_its_summary_shape(cap3, name):
    report = REPORTS[name](SearchClient(matching=2))
    assert report["meta"]["truncated"] is False
    assert "truncated" not in report["summary"]


def test_capped_report_rows_never_exceed_the_cap(cap3):
    report = reports.stale_issues(SearchClient(matching=50), days=0)
    assert len(report["rows"]) == 3


def test_dashboard_open_issue_search_is_capped_and_marked(cap3, no_worklogs):
    client = SearchClient(matching=50)
    report = reports.team_dashboard(client, ["alice"], None)
    assert client.searches[0]["limit"] == 4
    assert report["meta"]["truncated"] is True


def test_dashboard_is_marked_when_only_the_worklog_search_was_capped(cap3):
    client = SearchClient(matching=0)
    original = client.search

    def search(jql, fields=None, expand=None, limit=None):
        if "worklogAuthor" in jql:
            client.matching = 50
        try:
            return original(jql, fields=fields, expand=expand, limit=limit)
        finally:
            client.matching = 0

    client.search = search  # type: ignore[method-assign]
    report = reports.team_dashboard(client, ["alice"], None, since="2026-01-01")
    assert report["meta"]["truncated"] is True


def test_person_activity_keeps_the_http_400_fallback_alongside_the_cap(cap3):
    class Rejecting(SearchClient):
        def search(self, jql, fields=None, expand=None, limit=None):
            if "assignee was" in jql:
                raise errors.JiraError(
                    "The value 'alice' does not exist for the field 'assignee'.",
                    status=400,
                    url="https://jira.example.com/rest/api/2/search",
                )
            return super().search(jql, fields, expand, limit)

    report = reports.person_activity(Rejecting(matching=50), "alice", since="2026-01-01")
    notes = report["meta"]["notes"]
    assert any(n.startswith("dropped 'assignee was") for n in notes)
    assert any(n.startswith("truncated:") for n in notes)
    assert report["meta"]["truncated"] is True


# --------------------------------------------------------------------------- #
# Worklog
# --------------------------------------------------------------------------- #


def test_user_worklogs_reads_worklogs_only_for_capped_issues(cap3):
    client = SearchClient(matching=50)
    notes: list[str] = []
    worklog.user_worklogs(client, ["alice"], "2026-01-01", notes=notes)
    assert client.searches[0]["limit"] == 4
    assert len([g for g in client.gets if g.endswith("/worklog")]) == 3
    assert notes and notes[0].startswith("truncated:")


def test_user_worklogs_without_notes_still_caps(cap3):
    client = SearchClient(matching=50)
    worklog.user_worklogs(client, ["alice"], "2026-01-01")
    assert len([g for g in client.gets if g.endswith("/worklog")]) == 3


def test_worklog_report_over_the_cap_is_marked_truncated(cap3):
    report = worklog.worklog_report(SearchClient(matching=50), ["alice"], "1900-01-01")
    assert report["meta"]["truncated"] is True
    assert report["summary"]["truncated"].startswith("yes")


def test_worklog_report_under_the_cap_is_not_marked(cap3):
    report = worklog.worklog_report(SearchClient(matching=1), ["alice"], "2026-01-01")
    assert report["meta"]["truncated"] is False
    assert "truncated" not in report["summary"]


# --------------------------------------------------------------------------- #
# People directory
# --------------------------------------------------------------------------- #


def _member(name: str) -> dict:
    return {"name": name, "displayName": name.title(), "active": True}


class DirectoryClient:
    """Projects with one Developers role held by one group each."""

    def __init__(
        self,
        groups: dict[str, int],
        projects: list[str] | None = None,
        direct_users: list[str] | None = None,
    ) -> None:
        self.groups = {g: [_member(f"{g}-u{i}") for i in range(n)] for g, n in groups.items()}
        self.groups.setdefault("jira-administrators", [])
        self.groups.setdefault("jira-system-administrators", [])
        role_groups = [g for g in groups if not g.startswith("jira-")]
        self.projects = projects or ["PROJ"]
        self.role_groups = role_groups
        self.direct_users = list(direct_users or [])
        self.member_requests: list[dict] = []
        self.project_role_reads: list[str] = []

    def get(self, path: str, params: dict | None = None) -> Any:
        if path == f"{API}/project":
            return [{"key": k, "name": k} for k in self.projects]
        if path == f"{API}/group/member":
            params = params or {}
            self.member_requests.append(dict(params))
            listed = self.groups[params["groupname"]]
            start, size = params["startAt"], min(params["maxResults"], 50)
            return {"values": listed[start : start + size], "isLast": start + size >= len(listed)}
        parts = path.split("/")
        if path.startswith(f"{API}/project/") and parts[6] == "role":
            key = parts[5]
            if len(parts) == 7:
                self.project_role_reads.append(key)
                return {"Developers": f"https://jira.example.com{API}/project/{key}/role/10000"}
            actors = [
                {"type": "atlassian-group-role-actor", "name": g, "displayName": g} for g in self.role_groups
            ] + [
                {"type": "atlassian-user-role-actor", "name": u, "displayName": u.title()}
                for u in self.direct_users
            ]
            return {"id": 10000, "name": "Developers", "actors": actors}
        raise AssertionError(f"unexpected GET {path} {params}")


def test_directory_member_budget_is_shared_across_all_groups(monkeypatch):
    monkeypatch.setattr(directory, "MAX_GROUP_MEMBERS", 120)
    client = DirectoryClient({"g1": 100, "g2": 100, "g3": 100})
    report = directory.build_directory_report(client, max_workers=1)
    assert len(report["people"]) == 120
    # two (empty) admin-group pages + three pages of g1; g2/g3 never requested
    assert len(client.member_requests) == 5
    assert report["truncated"] is True
    assert any("group expansion stopped at 120" in n for n in report["notes"])


def test_directory_stops_requesting_once_the_budget_is_spent(monkeypatch):
    monkeypatch.setattr(directory, "MAX_GROUP_MEMBERS", 50)
    client = DirectoryClient({f"g{i}": 50 for i in range(30)})
    directory.build_directory_report(client, max_workers=1)
    assert len(client.member_requests) <= 3


def test_directory_under_the_budget_is_not_truncated():
    client = DirectoryClient({"g1": 3})
    report = directory.build_directory_report(client)
    assert report["truncated"] is False
    assert report["notes"] == []
    assert len(report["people"]) == 3


def test_truncated_admin_group_gives_jira_admin_none_not_false(monkeypatch):
    monkeypatch.setattr(directory, "MAX_GROUP_MEMBERS", 5)
    client = DirectoryClient({"jira-administrators": 10}, direct_users=["carol"])
    report = directory.build_directory_report(client, max_workers=1)
    carol = next(p for p in report["people"] if p["username"] == "carol")
    # carol is beyond the members that fit the budget, so "not an admin" is
    # unknowable and must not be reported as False.
    assert carol["jira_admin"] is None
    assert report["truncated"] is True
    assert "jira-administrators" in report["notes"][0]


def test_complete_admin_groups_still_give_jira_admin_false():
    client = DirectoryClient({}, direct_users=["carol"])
    carol = directory.build_directory(client)[0]
    assert carol["jira_admin"] is False


def test_directory_with_projects_none_reads_at_most_max_projects(monkeypatch):
    monkeypatch.setattr(directory, "MAX_PROJECTS", 2)
    client = DirectoryClient({"g1": 1}, projects=["CCC", "AAA", "BBB"])
    report = directory.build_directory_report(client)
    assert sorted(client.project_role_reads) == ["AAA", "BBB"]
    assert report["truncated"] is True
    assert any("3 projects are visible" in n for n in report["notes"])


def test_build_directory_with_projects_none_still_returns_people(monkeypatch):
    monkeypatch.setattr(directory, "MAX_PROJECTS", 1)
    client = DirectoryClient({"g1": 2}, projects=["AAA", "BBB"])
    people = directory.build_directory(client)
    assert {p["username"] for p in people} == {"g1-u0", "g1-u1"}


def test_too_many_explicit_projects_is_an_input_error(monkeypatch):
    monkeypatch.setattr(directory, "MAX_PROJECTS", 2)
    with pytest.raises(errors.InputError):
        directory.build_directory_report(DirectoryClient({}), ["AAA", "BBB", "CCC"])


def test_default_directory_caps_are_finite():
    assert 1 <= directory.MAX_GROUP_MEMBERS <= 10000
    assert 1 <= directory.MAX_PROJECTS <= 10000
