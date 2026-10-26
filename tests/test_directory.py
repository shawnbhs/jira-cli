"""Offline tests for ``jira_cli.services.directory`` (the people directory).

Only the HTTP client is faked: GET paths map to canned payloads and an
unmapped path raises, so an unexpected request fails loudly.
"""

from __future__ import annotations

from typing import Any

import pytest

from jira_cli.errors import InputError, JiraPermissionError, NotFoundError
from jira_cli.services import directory

API = "/rest/api/2"


def _user(name: str, display: str | None = None) -> dict:
    shown = name.title() if display is None else display
    return {"type": "atlassian-user-role-actor", "name": name, "displayName": shown}


def _group(name: str) -> dict:
    return {"type": "atlassian-group-role-actor", "name": name, "displayName": name}


def _member(name: str, display: str | None = None) -> dict:
    return {"name": name, "displayName": display or name.title(), "active": True}


class FakeClient:
    """Maps GET path (+ groupname for group/member) to payloads; anything else raises."""

    def __init__(self) -> None:
        self.projects: list[dict] = []
        self.roles: dict[str, dict[str, list[dict]]] = {}
        self.groups: dict[str, list[dict]] = {
            "jira-administrators": [],
            "jira-system-administrators": [],
        }
        self.failures: dict[str, Exception] = {}
        self.page_size = 50
        self.calls: list[tuple[str, Any]] = []

    def add_project(self, key: str, roles: dict[str, list[dict]]) -> None:
        self.projects.append({"key": key, "name": key.title()})
        self.roles[key] = roles

    def get(self, path: str, params: dict | None = None) -> Any:
        self.calls.append((path, params))
        if path in self.failures:
            raise self.failures[path]
        if path == f"{API}/project":
            return list(self.projects)
        if path == f"{API}/group/member":
            return self._group_page(params or {})
        parts = path.split("/")
        if path.startswith(f"{API}/project/") and parts[6] == "role":
            key = parts[5]
            names = list(self.roles[key])
            if len(parts) == 7:
                return {
                    n: f"https://jira.example.com{API}/project/{key}/role/{10000 + i}"
                    for i, n in enumerate(names)
                }
            name = names[int(parts[7]) - 10000]
            return {"id": int(parts[7]), "name": name, "actors": self.roles[key][name]}
        raise AssertionError(f"unexpected GET {path} {params}")

    def _group_page(self, params: dict) -> dict:
        group = params["groupname"]
        assert params["includeInactiveUsers"] == "false"
        failure = self.failures.get(f"group:{group}")
        if failure is not None:
            raise failure
        if group not in self.groups:
            raise NotFoundError("group not found", status=404)
        start = params["startAt"]
        size = min(params["maxResults"], self.page_size)
        values = self.groups[group][start : start + size]
        return {"values": values, "startAt": start, "isLast": start + size >= len(self.groups[group])}


def _person(people: list[dict], username: str) -> dict:
    return next(p for p in people if p["username"] == username)


def test_direct_role_members_become_people_with_their_roles() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice"), _user("bob")], "Users": [_user("alice")]})

    people = directory.build_directory(client)

    assert _person(people, "alice") == {
        "username": "alice",
        "display_name": "Alice",
        "active": None,
        "projects": [{"key": "PROJ", "roles": ["Developers", "Users"]}],
        "project_admin": [],
        "jira_admin": False,
    }
    assert [p["username"] for p in people] == ["alice", "bob"]


def test_group_in_role_is_expanded_and_labelled_via_group() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_group("devs")]})
    client.groups["devs"] = [_member("carol")]

    carol = _person(directory.build_directory(client), "carol")

    assert carol["projects"] == [{"key": "PROJ", "roles": ["Developers (via group devs)"]}]
    assert carol["active"] is True


def test_group_members_are_read_across_pages() -> None:
    client = FakeClient()
    client.page_size = 2
    client.add_project("PROJ", {"Developers": [_group("devs")]})
    client.groups["devs"] = [_member(n) for n in ("alice", "bob", "carol", "dave", "erin")]

    people = directory.build_directory(client)

    assert [p["username"] for p in people] == ["alice", "bob", "carol", "dave", "erin"]


def test_group_is_fetched_once_even_when_used_by_many_projects() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_group("devs")]})
    client.add_project("OPS", {"Developers": [_group("devs")]})
    client.groups["devs"] = [_member("carol")]

    carol = _person(directory.build_directory(client), "carol")

    group_calls = [c for c in client.calls if c[1] and c[1].get("groupname") == "devs"]
    assert len(group_calls) == 1
    assert [p["key"] for p in carol["projects"]] == ["OPS", "PROJ"]


def test_expand_groups_false_lists_only_direct_members() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice"), _group("devs")]})
    client.groups["devs"] = [_member("carol")]

    people = directory.build_directory(client, expand_groups=False)

    assert [p["username"] for p in people] == ["alice"]


def test_project_admin_lists_projects_where_person_holds_administrators_role() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Administrators": [_user("alice")], "Developers": [_user("bob")]})
    client.add_project("OPS", {"admin": [_group("ops-leads")]})
    client.groups["ops-leads"] = [_member("alice"), _member("bob")]

    people = directory.build_directory(client)

    assert _person(people, "alice")["project_admin"] == ["OPS", "PROJ"]
    assert _person(people, "bob")["project_admin"] == ["OPS"]


def test_jira_admin_true_for_members_of_admin_groups_false_for_others() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice"), _user("bob"), _user("carol")]})
    client.groups["jira-administrators"] = [_member("alice")]
    client.groups["jira-system-administrators"] = [_member("bob")]

    people = directory.build_directory(client)

    assert [p["jira_admin"] for p in people] == [True, True, False]


def test_jira_admin_false_when_an_admin_group_does_not_exist() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice")]})
    del client.groups["jira-system-administrators"]

    report = directory.build_directory_report(client)

    assert report["people"][0]["jira_admin"] is False
    assert report["unexpanded_groups"] == []


def test_jira_admin_unknown_when_admin_group_cannot_be_read() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice"), _user("bob")]})
    client.groups["jira-system-administrators"] = [_member("bob")]
    client.failures["group:jira-administrators"] = JiraPermissionError("forbidden", status=403)

    people = directory.build_directory(client)

    assert _person(people, "alice")["jira_admin"] is None
    assert _person(people, "bob")["jira_admin"] is True


def test_unreadable_role_group_is_reported_and_directory_still_built() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice"), _group("secret")]})
    client.groups["secret"] = [_member("bob")]
    client.failures["group:secret"] = JiraPermissionError("forbidden", status=403)

    report = directory.build_directory_report(client)

    assert [p["username"] for p in report["people"]] == ["alice"]
    assert len(report["unexpanded_groups"]) == 1
    assert report["unexpanded_groups"][0]["group"] == "secret"
    assert "403" in report["unexpanded_groups"][0]["reason"]


def test_failing_project_is_reported_and_other_projects_still_returned() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice")]})
    client.add_project("OPS", {"Developers": [_user("bob")]})
    client.failures[f"{API}/project/OPS/role"] = JiraPermissionError("forbidden", status=403)

    report = directory.build_directory_report(client)

    assert report["projects"] == ["PROJ"]
    assert [p["username"] for p in report["people"]] == ["alice"]
    assert [e["project"] for e in report["errors"]] == ["OPS"]
    assert "403" in report["errors"][0]["error"]


def test_explicit_projects_are_validated_and_only_those_are_read() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice")]})
    client.add_project("OPS", {"Developers": [_user("bob")]})

    report = directory.build_directory_report(client, ["proj"])

    assert report["projects"] == ["PROJ"]
    assert [p["username"] for p in report["people"]] == ["alice"]
    assert (f"{API}/project", None) not in client.calls


def test_malformed_project_key_raises_input_error() -> None:
    with pytest.raises(InputError):
        directory.build_directory(FakeClient(), ["../x"])


def test_people_sorted_by_display_name_then_username_case_insensitively() -> None:
    client = FakeClient()
    client.add_project(
        "PROJ",
        {
            "Developers": [
                _user("zed", "bob"),
                _user("carol", "Carol"),
                _user("amy", "Bob"),
                _user("dave", ""),
            ]
        },
    )

    people = directory.build_directory(client)

    assert [(p["display_name"], p["username"]) for p in people] == [
        ("Bob", "amy"),
        ("bob", "zed"),
        ("Carol", "carol"),
        ("dave", "dave"),
    ]


def test_direct_and_group_membership_of_same_role_are_both_listed() -> None:
    client = FakeClient()
    client.add_project("PROJ", {"Developers": [_user("alice"), _group("devs")]})
    client.groups["devs"] = [_member("alice")]

    alice = _person(directory.build_directory(client), "alice")

    assert alice["projects"] == [{"key": "PROJ", "roles": ["Developers", "Developers (via group devs)"]}]


def test_directory_meta_counts_people_and_projects() -> None:
    people = [
        {"username": "alice", "projects": [{"key": "PROJ", "roles": []}]},
        {"username": "bob", "projects": [{"key": "OPS", "roles": []}, {"key": "PROJ", "roles": []}]},
    ]

    assert directory.directory_meta(people) == {
        "people": 2,
        "projects": ["OPS", "PROJ"],
        "unexpanded_groups": [],
    }


PEOPLE = [
    {"username": "alice", "display_name": "Alice Smith"},
    {"username": "bob", "display_name": "Bob Jones"},
    {"username": "al.white", "display_name": "Al White"},
    {"username": "carol", "display_name": "Carol Alison"},
]


def test_find_people_matches_username_or_display_name_case_insensitively() -> None:
    assert [p["username"] for p in directory.find_people(PEOPLE, "JON")] == ["bob"]
    assert [p["username"] for p in directory.find_people(PEOPLE, "al")] == ["alice", "al.white", "carol"]


def test_find_people_with_no_match_returns_empty() -> None:
    assert directory.find_people(PEOPLE, "nobody") == []


def test_suggest_ranks_whole_word_match_first() -> None:
    names = [p["username"] for p in directory.suggest(PEOPLE, "al")]

    assert names[0] == "al.white"
    assert len(names) == 3


def test_suggest_falls_back_to_close_matches_for_typos() -> None:
    assert [p["username"] for p in directory.suggest(PEOPLE, "crol", limit=1)] == ["carol"]


def test_suggest_returns_nothing_for_blank_token() -> None:
    assert directory.suggest(PEOPLE, "  ") == []
