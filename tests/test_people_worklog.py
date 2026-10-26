"""Tests for ``jira_cli.services.people`` and ``jira_cli.services.worklog``.

Offline only: a small fake client records every request and returns canned
payloads.  The real core modules (``errors``, ``durations``, ``report``) are
imported directly; nothing is injected into ``sys.modules``, so this file is
safe to run together with every other test file.
"""

from __future__ import annotations

import os
import re
import stat
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from jira_cli.errors import InputError
from jira_cli.services import people, worklog

# --------------------------------------------------------------------------- #
# Fake client
# --------------------------------------------------------------------------- #


class FakeClient:
    """Records requests and replays canned responses."""

    def __init__(self, gets: dict | None = None, searches: Any = None):
        self.gets = gets or {}
        self.searches = searches if searches is not None else []
        self.calls: list[tuple] = []

    def get(self, path: str, params: dict | None = None) -> Any:
        self.calls.append(("GET", path, params))
        return self.gets.get(path)

    def post(self, path: str, json: Any = None, params: dict | None = None) -> Any:
        self.calls.append(("POST", path, json))
        return {"id": "9001", "timeSpentSeconds": 7200}

    def put(self, path: str, json: Any = None, params: dict | None = None) -> Any:
        self.calls.append(("PUT", path, json))
        return None

    def search(self, jql: str, fields=None, expand=None, limit=None) -> list[dict]:
        self.calls.append(("SEARCH", jql, fields))
        return list(self.searches)

    def last(self, verb: str) -> tuple:
        for call in reversed(self.calls):
            if call[0] == verb:
                return call
        raise AssertionError(f"no {verb} call recorded")


def user_payload(name: str, display: str, email: str, active: bool = True) -> dict:
    return {
        "name": name,
        "displayName": display,
        "emailAddress": email,
        "active": active,
        "self": "https://jira.example.com/rest/api/2/user?username=" + name,
    }


# --------------------------------------------------------------------------- #
# people: Jira user endpoints
# --------------------------------------------------------------------------- #


def test_search_users_calls_server_endpoint_and_normalizes():
    client = FakeClient(
        {"/rest/api/2/user/search": [user_payload("alice", "Alice A", "alice@example.com")]}
    )

    result = people.search_users(client, "ali", limit=10)

    assert client.last("GET") == (
        "GET",
        "/rest/api/2/user/search",
        {"username": "ali", "maxResults": 10},
    )
    assert result == [
        {
            "username": "alice",
            "display_name": "Alice A",
            "email": "alice@example.com",
            "active": True,
        }
    ]


def test_search_users_rejects_blank_query():
    with pytest.raises(InputError):
        people.search_users(FakeClient(), "   ")


def test_search_users_rejects_non_positive_limit():
    with pytest.raises(InputError):
        people.search_users(FakeClient(), "ali", limit=0)


def test_search_users_tolerates_unexpected_body():
    client = FakeClient({"/rest/api/2/user/search": {"errorMessages": []}})
    assert people.search_users(client, "ali") == []


def test_get_user_uses_username_parameter():
    client = FakeClient(
        {"/rest/api/2/user": user_payload("bob", "Bob B", "bob@example.com", False)}
    )

    result = people.get_user(client, "bob")

    assert client.last("GET") == ("GET", "/rest/api/2/user", {"username": "bob"})
    assert result == {
        "username": "bob",
        "display_name": "Bob B",
        "email": "bob@example.com",
        "active": False,
    }


def test_get_user_rejects_blank_username():
    with pytest.raises(InputError):
        people.get_user(FakeClient(), "")


def test_assignable_users_passes_project_and_query():
    client = FakeClient(
        {
            "/rest/api/2/user/assignable/search": [
                user_payload("alice", "Alice A", "alice@example.com")
            ]
        }
    )

    result = people.assignable_users(client, "PROJ", "al")

    assert client.last("GET") == (
        "GET",
        "/rest/api/2/user/assignable/search",
        {"project": "PROJ", "username": "al"},
    )
    assert [u["username"] for u in result] == ["alice"]


def test_assignable_users_defaults_query_to_empty_string():
    client = FakeClient({"/rest/api/2/user/assignable/search": []})
    people.assignable_users(client, "PROJ")
    assert client.last("GET")[2] == {"project": "PROJ", "username": ""}


def test_assignable_users_rejects_blank_project():
    with pytest.raises(InputError):
        people.assignable_users(FakeClient(), " ")


# --------------------------------------------------------------------------- #
# people: teams.yaml
# --------------------------------------------------------------------------- #


def test_load_teams_returns_empty_dict_when_file_is_missing(tmp_path: Path):
    assert people.load_teams(tmp_path / "teams.yaml") == {}


def test_save_team_then_load_teams_roundtrips(tmp_path: Path):
    path = tmp_path / "cfg" / "teams.yaml"

    saved = people.save_team(path, "backend", ["alice", "bob"], ["PROJ"], [12])

    assert saved == {
        "name": "backend",
        "members": ["alice", "bob"],
        "projects": ["PROJ"],
        "boards": [12],
    }
    assert people.load_teams(path) == {
        "backend": {"members": ["alice", "bob"], "projects": ["PROJ"], "boards": [12]}
    }


def test_save_team_writes_plain_yaml_a_human_can_edit(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    people.save_team(path, "backend", ["alice"], ["PROJ"], [12])

    text = path.read_text(encoding="utf-8")

    assert "backend:" in text
    assert "- alice" in text
    assert "{" not in text  # block style, not flow style


def test_save_team_keeps_other_teams(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    people.save_team(path, "backend", ["alice"])
    people.save_team(path, "frontend", ["carol"])

    assert sorted(people.load_teams(path)) == ["backend", "frontend"]


def test_save_team_deduplicates_members_and_keeps_order(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    saved = people.save_team(path, "backend", ["bob", "alice", "bob"])
    assert saved["members"] == ["bob", "alice"]


def test_save_team_leaves_no_temporary_file_behind(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    people.save_team(path, "backend", ["alice"])
    assert [p.name for p in tmp_path.iterdir()] == ["teams.yaml"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_save_team_creates_private_file_and_directory(tmp_path: Path):
    path = tmp_path / "cfg" / "teams.yaml"

    people.save_team(path, "backend", ["alice"])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("name", ["", "has space", "a" * 65, "semi;colon", "slash/es"])
def test_save_team_rejects_invalid_names(tmp_path: Path, name: str):
    with pytest.raises(InputError):
        people.save_team(tmp_path / "teams.yaml", name, ["alice"])


@pytest.mark.parametrize("name", ["backend", "team.one", "team_2", "a-b", "A"])
def test_save_team_accepts_valid_names(tmp_path: Path, name: str):
    people.save_team(tmp_path / "teams.yaml", name, ["alice"])
    assert name in people.load_teams(tmp_path / "teams.yaml")


def test_save_team_rejects_non_string_members(tmp_path: Path):
    with pytest.raises(InputError):
        people.save_team(tmp_path / "teams.yaml", "backend", ["alice", 7])


def test_save_team_rejects_non_integer_boards(tmp_path: Path):
    with pytest.raises(InputError):
        people.save_team(tmp_path / "teams.yaml", "backend", ["alice"], None, ["abc"])


def test_remove_team_reports_whether_it_existed(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    people.save_team(path, "backend", ["alice"])

    assert people.remove_team(path, "backend") is True
    assert people.remove_team(path, "backend") is False
    assert people.load_teams(path) == {}


def test_load_teams_accepts_hand_written_member_list_shorthand(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    path.write_text("backend:\n  - alice\n  - bob\n", encoding="utf-8")

    assert people.load_teams(path) == {
        "backend": {"members": ["alice", "bob"], "projects": [], "boards": []}
    }


def test_load_teams_fills_missing_sections(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    path.write_text("backend:\n  members: [alice]\n", encoding="utf-8")

    assert people.load_teams(path)["backend"] == {
        "members": ["alice"],
        "projects": [],
        "boards": [],
    }


def test_load_teams_treats_empty_file_as_no_teams(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    path.write_text("", encoding="utf-8")
    assert people.load_teams(path) == {}


def test_load_teams_rejects_invalid_yaml(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    path.write_text("backend: [alice\n", encoding="utf-8")
    with pytest.raises(InputError):
        people.load_teams(path)


def test_load_teams_rejects_a_top_level_list(tmp_path: Path):
    path = tmp_path / "teams.yaml"
    path.write_text("- alice\n- bob\n", encoding="utf-8")
    with pytest.raises(InputError):
        people.load_teams(path)


def test_load_teams_does_not_execute_python_tags(tmp_path: Path):
    """``yaml.safe_load`` must refuse an object tag rather than construct it."""
    path = tmp_path / "teams.yaml"
    path.write_text("backend: !!python/object/apply:os.system ['echo hi']\n", encoding="utf-8")
    with pytest.raises(InputError):
        people.load_teams(path)


# --------------------------------------------------------------------------- #
# people: resolve_people
# --------------------------------------------------------------------------- #


TEAMS = {
    "backend": {"members": ["alice", "bob"], "projects": ["PROJ"], "boards": []},
    "frontend": {"members": ["carol"], "projects": [], "boards": []},
}


def test_resolve_people_expands_team_prefix():
    assert people.resolve_people("team:backend", TEAMS) == ["alice", "bob"]


def test_resolve_people_expands_at_prefix():
    assert people.resolve_people("@backend", TEAMS) == ["alice", "bob"]


def test_resolve_people_is_case_insensitive_on_the_team_keyword():
    assert people.resolve_people("TEAM:backend", TEAMS) == ["alice", "bob"]


def test_resolve_people_drops_case_variants_keeping_the_first_spelling():
    assert people.resolve_people("alice,ALICE, Alice ,bob", TEAMS) == ["alice", "bob"]


def test_resolve_people_dedupes_case_variants_across_teams_and_literals():
    assert people.resolve_people(["BOB", "@backend"], TEAMS) == ["BOB", "alice"]


def test_resolve_people_splits_comma_lists():
    assert people.resolve_people("alice,bob", TEAMS) == ["alice", "bob"]


def test_resolve_people_ignores_surrounding_whitespace():
    assert people.resolve_people(" alice , bob ", TEAMS) == ["alice", "bob"]


def test_resolve_people_accepts_a_list_and_mixes_forms():
    assert people.resolve_people(["@backend", "carol,dave"], TEAMS) == [
        "alice",
        "bob",
        "carol",
        "dave",
    ]


def test_resolve_people_dedupes_keeping_first_occurrence():
    assert people.resolve_people("bob,@backend,bob", TEAMS) == ["bob", "alice"]


def test_resolve_people_unknown_team_names_the_known_teams():
    with pytest.raises(InputError) as excinfo:
        people.resolve_people("team:nope", TEAMS)

    message = str(excinfo.value)
    assert "nope" in message
    assert "backend" in message and "frontend" in message


def test_resolve_people_unknown_team_with_no_teams_at_all():
    with pytest.raises(InputError):
        people.resolve_people("@backend", {})


def test_resolve_people_rejects_an_empty_spec():
    with pytest.raises(InputError):
        people.resolve_people("  ,  ", TEAMS)


def test_resolve_people_rejects_a_wrong_type():
    with pytest.raises(InputError):
        people.resolve_people(42, TEAMS)


def test_resolve_people_rejects_a_bare_team_marker():
    with pytest.raises(InputError):
        people.resolve_people("team:", TEAMS)


# --------------------------------------------------------------------------- #
# worklog: reading
# --------------------------------------------------------------------------- #


def worklog_entry(wid: str, author: str, started: str, seconds: int, comment=None) -> dict:
    return {
        "id": wid,
        "author": {"name": author, "displayName": author.title()},
        "started": started,
        "timeSpentSeconds": seconds,
        "comment": comment,
    }


def test_issue_worklogs_normalizes_entries():
    client = FakeClient(
        {
            "/rest/api/2/issue/PROJ-1/worklog": {
                "total": 1,
                "worklogs": [
                    worklog_entry("1", "alice", "2026-10-05T09:00:00.000+0330", 3600, "fix")
                ],
            }
        }
    )

    assert worklog.issue_worklogs(client, "PROJ-1") == [
        {
            "id": "1",
            "author": "alice",
            "started": "2026-10-05T09:00:00.000+0330",
            "seconds": 3600,
            "comment": "fix",
        }
    ]
    assert client.last("GET")[1] == "/rest/api/2/issue/PROJ-1/worklog"


def test_issue_worklogs_returns_empty_list_without_worklogs_key():
    client = FakeClient({"/rest/api/2/issue/PROJ-1/worklog": {"total": 0}})
    assert worklog.issue_worklogs(client, "PROJ-1") == []


def test_issue_worklogs_rejects_blank_key():
    with pytest.raises(InputError):
        worklog.issue_worklogs(FakeClient(), "")


def test_issue_worklogs_upper_cases_a_lower_case_key():
    client = FakeClient({"/rest/api/2/issue/PROJ-1/worklog": {"worklogs": []}})

    worklog.issue_worklogs(client, " proj-1 ")

    assert client.last("GET")[1] == "/rest/api/2/issue/PROJ-1/worklog"


#: Keys that would rewrite the request path if interpolated raw.
_TRAVERSAL_KEYS = [
    "PROJ-1/../../project/PROJ",
    "PROJ-1/../../../serverInfo",
    "../PROJ-1",
    "PROJ-1?expand=x",
    "PROJ-1%2F..",
    "PROJ",
]


@pytest.mark.parametrize("key", _TRAVERSAL_KEYS)
@pytest.mark.parametrize(
    "call",
    [
        lambda c, k: worklog.issue_worklogs(c, k),
        lambda c, k: worklog.add_worklog(c, k, "1h"),
        lambda c, k: worklog.set_estimate(c, k, original="1h"),
    ],
    ids=["issue_worklogs", "add_worklog", "set_estimate"],
)
def test_malformed_issue_key_is_rejected_before_any_request(call, key):
    client = FakeClient()

    with pytest.raises(InputError):
        call(client, key)

    assert client.calls == []


def test_user_worklogs_never_requests_a_path_built_from_a_malformed_search_key():
    # Keys returned by search are server data; a hostile one is refused, not followed.
    client = FakeClient(searches=[{"key": "PROJ-1/../../project/PROJ", "fields": {}}])

    with pytest.raises(InputError):
        worklog.user_worklogs(client, ["alice"], "2026-10-01")

    assert [c for c in client.calls if c[0] != "SEARCH"] == []


def _window_client() -> FakeClient:
    client = FakeClient(
        searches=[
            {"key": "PROJ-1", "fields": {"summary": "First"}},
            {"key": "PROJ-2", "fields": {"summary": "Second"}},
        ]
    )
    client.gets = {
        "/rest/api/2/issue/PROJ-1/worklog": {
            "worklogs": [
                worklog_entry("1", "alice", "2026-10-05T09:00:00.000+0000", 3600, "a"),
                worklog_entry("2", "alice", "2026-09-30T09:00:00.000+0000", 1800, "too early"),
                worklog_entry("3", "carol", "2026-10-05T09:00:00.000+0000", 900, "other user"),
            ]
        },
        "/rest/api/2/issue/PROJ-2/worklog": {
            "worklogs": [
                worklog_entry("4", "bob", "2026-10-07T17:00:00.000+0000", 7200, "b"),
                worklog_entry("5", "bob", "2026-10-09T17:00:00.000+0000", 3600, "too late"),
            ]
        },
    }
    return client


def test_user_worklogs_builds_the_expected_jql():
    client = _window_client()

    worklog.user_worklogs(
        client, ["alice", "bob"], "2026-10-01", "2026-10-07", ["PROJ"]
    )

    verb, jql, fields = client.last("SEARCH")
    assert jql == (
        'worklogAuthor in ("alice", "bob") AND worklogDate >= "2026-10-01" '
        'AND worklogDate <= "2026-10-07" AND project in ("PROJ")'
    )
    assert fields == ["summary"]


def test_user_worklogs_omits_optional_clauses():
    client = FakeClient(searches=[])
    worklog.user_worklogs(client, ["alice"], "2026-10-01")
    assert client.last("SEARCH")[1] == (
        'worklogAuthor in ("alice") AND worklogDate >= "2026-10-01"'
    )


def test_user_worklogs_filters_by_author_and_by_date_client_side():
    client = _window_client()

    rows = worklog.user_worklogs(
        client, ["alice", "bob"], "2026-10-01", "2026-10-07", ["PROJ"]
    )

    assert [(r["key"], r["author"], r["seconds"]) for r in rows] == [
        ("PROJ-1", "alice", 3600),
        ("PROJ-2", "bob", 7200),
    ]
    assert rows[0]["summary"] == "First"
    assert rows[0]["comment"] == "a"


def test_user_worklogs_without_until_keeps_later_entries():
    client = _window_client()

    rows = worklog.user_worklogs(client, ["bob"], "2026-10-01")

    assert [r["seconds"] for r in rows] == [7200, 3600]


def test_user_worklogs_quotes_a_username_that_tries_to_inject_jql():
    client = FakeClient(searches=[])
    worklog.user_worklogs(client, ['a" OR project = X'], "2026-10-01")
    assert client.last("SEARCH")[1].startswith(
        'worklogAuthor in ("a\\" OR project = X")'
    )


def test_user_worklogs_requires_at_least_one_username():
    with pytest.raises(InputError):
        worklog.user_worklogs(FakeClient(), [], "2026-10-01")


def test_user_worklogs_rejects_an_unparsable_date():
    with pytest.raises(InputError):
        worklog.user_worklogs(FakeClient(), ["alice"], "last tuesday")


def test_user_worklogs_resolves_relative_dates():
    client = FakeClient(searches=[])
    worklog.user_worklogs(client, ["alice"], "today")
    assert f'worklogDate >= "{date.today().isoformat()}"' in client.last("SEARCH")[1]


def test_user_worklogs_skips_issues_without_a_key():
    client = FakeClient(searches=[{"fields": {"summary": "no key"}}])
    assert worklog.user_worklogs(client, ["alice"], "2026-10-01") == []


# --------------------------------------------------------------------------- #
# worklog: report
# --------------------------------------------------------------------------- #


def test_worklog_report_aggregates_per_user_and_issue():
    client = _window_client()
    client.gets["/rest/api/2/issue/PROJ-1/worklog"]["worklogs"].append(
        worklog_entry("6", "alice", "2026-10-06T09:00:00.000+0000", 1800, "more")
    )

    report = worklog.worklog_report(
        client, ["alice", "bob"], "2026-10-01", "2026-10-07", ["PROJ"]
    )

    assert report["columns"] == ["user", "key", "summary", "hours"]
    assert report["rows"] == [
        {"user": "alice", "key": "PROJ-1", "summary": "First", "hours": 1.5},
        {"user": "bob", "key": "PROJ-2", "summary": "Second", "hours": 2.0},
    ]
    assert report["summary"] == {"alice": 1.5, "bob": 2.0, "total": 3.5}


def test_worklog_report_records_its_jql_and_window_in_meta():
    client = _window_client()

    report = worklog.worklog_report(client, ["alice"], "2026-10-01", "2026-10-07")

    assert report["meta"]["since"] == "2026-10-01"
    assert report["meta"]["until"] == "2026-10-07"
    assert report["meta"]["jql"] == [
        'worklogAuthor in ("alice") AND worklogDate >= "2026-10-01" '
        'AND worklogDate <= "2026-10-07"'
    ]
    assert report["meta"]["method"]
    assert report["meta"]["limitations"]


def test_worklog_report_is_empty_but_well_formed_without_entries():
    report = worklog.worklog_report(FakeClient(searches=[]), ["alice"], "2026-10-01")

    assert report["rows"] == []
    assert report["summary"] == {"total": 0.0}
    assert report["title"] == "Worklog report"


def test_worklog_report_rows_are_json_serialisable_scalars():
    report = worklog.worklog_report(_window_client(), ["alice", "bob"], "2026-10-01")
    for row in report["rows"]:
        for value in row.values():
            assert isinstance(value, (str, int, float, bool, type(None)))


# --------------------------------------------------------------------------- #
# worklog: writing
# --------------------------------------------------------------------------- #

_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000\+0000$")


def test_add_worklog_posts_normalized_duration_and_comment():
    client = FakeClient()

    result = worklog.add_worklog(client, "PROJ-1", "2H 30M", "2026-10-05", "done")

    verb, path, body = client.last("POST")
    assert path == "/rest/api/2/issue/PROJ-1/worklog"
    assert body == {
        "timeSpent": "2h 30m",
        "started": "2026-10-05T12:00:00.000+0000",
        "comment": "done",
    }
    assert result["key"] == "PROJ-1"
    assert result["id"] == "9001"
    assert result["seconds"] == 7200
    assert result["time_spent"] == "2h 30m"


def test_add_worklog_defaults_started_to_now_in_jira_format():
    client = FakeClient()
    worklog.add_worklog(client, "PROJ-1", "1h")

    started = client.last("POST")[2]["started"]
    assert _STAMP_RE.match(started)


def test_add_worklog_omits_an_empty_comment():
    client = FakeClient()
    worklog.add_worklog(client, "PROJ-1", "1h")
    assert "comment" not in client.last("POST")[2]


def test_add_worklog_resolves_a_relative_started_date():
    client = FakeClient()
    worklog.add_worklog(client, "PROJ-1", "1h", "yesterday")

    expected = (date.today() - timedelta(days=1)).isoformat()
    assert client.last("POST")[2]["started"] == f"{expected}T12:00:00.000+0000"


def test_add_worklog_passes_through_a_full_timestamp():
    client = FakeClient()
    worklog.add_worklog(client, "PROJ-1", "1h", "2026-10-05T08:15:00.000+0000")
    assert client.last("POST")[2]["started"] == "2026-10-05T08:15:00.000+0000"


def test_add_worklog_rejects_a_bad_duration():
    with pytest.raises(InputError):
        worklog.add_worklog(FakeClient(), "PROJ-1", "soon")


def test_add_worklog_rejects_a_blank_key():
    with pytest.raises(InputError):
        worklog.add_worklog(FakeClient(), " ", "1h")


def test_set_estimate_puts_both_timetracking_fields():
    client = FakeClient()

    result = worklog.set_estimate(client, "PROJ-1", "3h", "1h 30m")

    verb, path, body = client.last("PUT")
    assert path == "/rest/api/2/issue/PROJ-1"
    assert body == {
        "fields": {
            "timetracking": {"originalEstimate": "3h", "remainingEstimate": "1h 30m"}
        }
    }
    assert result == {"key": "PROJ-1", "original": "3h", "remaining": "1h 30m"}


def test_set_estimate_sends_only_the_field_given():
    client = FakeClient()
    worklog.set_estimate(client, "PROJ-1", remaining="2h")
    assert client.last("PUT")[2]["fields"]["timetracking"] == {"remainingEstimate": "2h"}


def test_set_estimate_requires_at_least_one_value():
    with pytest.raises(InputError):
        worklog.set_estimate(FakeClient(), "PROJ-1")


def test_set_estimate_rejects_a_bad_duration():
    with pytest.raises(InputError):
        worklog.set_estimate(FakeClient(), "PROJ-1", "three hours")


# --------------------------------------------------------------------------- #
# Names the sibling consumers (reports.py, cli/menu.py) rely on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["search_users", "get_user", "assignable_users", "load_teams", "save_team",
     "remove_team", "resolve_people"],
)
def test_people_exports_contract_names(name: str):
    assert callable(getattr(people, name))


@pytest.mark.parametrize(
    "name",
    ["issue_worklogs", "user_worklogs", "worklog_report", "add_worklog", "set_estimate"],
)
def test_worklog_exports_contract_names(name: str):
    assert callable(getattr(worklog, name))


def test_user_worklogs_accepts_the_positional_call_reports_py_makes():
    """``reports._logged_hours`` calls ``user_worklogs(c, [u], since, None, projects)``."""
    client = FakeClient(searches=[])
    assert worklog.user_worklogs(client, ["alice"], "7d", None, ["PROJ"]) == []


def test_worklog_report_accepts_the_keyword_call_menu_py_makes():
    client = FakeClient(searches=[])
    report = worklog.worklog_report(
        client, ["alice"], since="2026-10-01", until=None, projects=None
    )
    assert report["title"] == "Worklog report"
