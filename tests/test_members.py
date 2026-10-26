"""Offline tests for ``jira_cli.services.members`` (project-role membership).

Only the HTTP client is faked. Every request is recorded, and an unmapped
request raises, so a test that makes an unexpected call fails loudly.
"""

from __future__ import annotations

from typing import Any

import pytest

from jira_cli.errors import InputError, ReadOnlyError
from jira_cli.services import members

BASE = "https://jira.example.com/rest/api/2/project/PROJ/role"
ROLES = {"Developers": f"{BASE}/10001", "Administrators": f"{BASE}/10002"}


def _actor(name: str, kind: str = "user") -> dict:
    return {
        "type": f"atlassian-{kind}-role-actor",
        "name": name,
        "displayName": name.title(),
    }


class FakeClient:
    """Holds role membership in memory and mimics the role REST endpoints."""

    def __init__(self, read_only: bool = False) -> None:
        self.read_only = read_only
        self.state: dict[int, dict[str, Any]] = {
            10001: {"name": "Developers", "users": ["alice", "carol"], "groups": []},
            10002: {"name": "Administrators", "users": ["admin"], "groups": ["jira-administrators"]},
        }
        self.calls: list[tuple[str, str, Any]] = []

    def ensure_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyError("read-only")

    def _role(self, rid: int) -> dict:
        role = self.state[rid]
        return {
            "id": rid,
            "name": role["name"],
            "actors": [_actor(u) for u in role["users"]] + [_actor(g, "group") for g in role["groups"]],
        }

    def get(self, path: str, params: dict | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if path == "/rest/api/2/project/PROJ/role":
            return dict(ROLES)
        prefix = "/rest/api/2/project/PROJ/role/"
        if path.startswith(prefix) and path[len(prefix) :].isdigit():
            return self._role(int(path[len(prefix) :]))
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, json: Any = None, params: dict | None = None) -> Any:
        self.ensure_writable()
        self.calls.append(("POST", path, json))
        rid = int(path.rsplit("/", 1)[-1])
        self.state[rid]["users"] += list(json.get("user") or [])
        self.state[rid]["groups"] += list(json.get("group") or [])
        return self._role(rid)

    def delete(self, path: str, params: dict | None = None) -> Any:
        self.ensure_writable()
        self.calls.append(("DELETE", path, params))
        rid = int(path.rsplit("/", 1)[-1])
        if "user" in params:
            self.state[rid]["users"].remove(params["user"])
        if "group" in params:
            self.state[rid]["groups"].remove(params["group"])
        return None

    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]


# ------------------------------------------------------------------ reading


def test_list_roles_returns_every_role_sorted_with_users_and_groups():
    roles = members.list_roles(FakeClient(), "proj")

    assert [r["name"] for r in roles] == ["Administrators", "Developers"]
    assert roles[0]["groups"] == ["jira-administrators"]
    assert [u["username"] for u in roles[1]["users"]] == ["alice", "carol"]


def test_get_role_matches_the_role_name_case_insensitively():
    role = members.get_role(FakeClient(), "PROJ", "developers")

    assert role["id"] == 10001
    assert role["project"] == "PROJ"


def test_get_role_accepts_a_numeric_role_id_without_listing_roles():
    client = FakeClient()

    role = members.get_role(client, "PROJ", "10002")

    assert role["name"] == "Administrators"
    assert ("GET", "/rest/api/2/project/PROJ/role", None) not in client.calls


def test_an_unknown_role_names_the_roles_the_project_has():
    with pytest.raises(InputError, match="Administrators, Developers"):
        members.get_role(FakeClient(), "PROJ", "Testers")


def test_a_malformed_project_key_never_reaches_the_url():
    client = FakeClient()

    with pytest.raises(InputError):
        members.list_roles(client, "PROJ/../../admin")
    assert client.calls == []


# ------------------------------------------------------------------- adding


def test_add_members_posts_only_the_people_not_already_in_the_role():
    client = FakeClient()

    result = members.add_members(client, "PROJ", "Developers", users="alice, bob,dave")

    assert client.writes() == [("POST", "/rest/api/2/project/PROJ/role/10001", {"user": ["bob", "dave"]})]
    assert result["added_users"] == ["bob", "dave"]
    assert result["already_members"] == ["alice"]
    assert [u["username"] for u in result["members_after"]["users"]] == [
        "alice",
        "bob",
        "carol",
        "dave",
    ]


def test_add_members_sends_nothing_when_everyone_is_already_a_member():
    client = FakeClient()

    result = members.add_members(client, "PROJ", "Developers", users=["ALICE", "carol"])

    assert client.writes() == []
    assert result["added_users"] == []
    assert result["already_members"] == ["ALICE", "carol"]


def test_add_members_can_add_a_group():
    client = FakeClient()

    result = members.add_members(client, "PROJ", "Developers", groups=["dev team"])

    assert client.writes() == [("POST", "/rest/api/2/project/PROJ/role/10001", {"group": ["dev team"]})]
    assert result["added_groups"] == ["dev team"]


def test_add_members_deduplicates_the_input():
    client = FakeClient()

    members.add_members(client, "PROJ", "Developers", users=["bob", "bob", " bob "])

    assert client.writes()[0][2] == {"user": ["bob"]}


@pytest.mark.parametrize(
    "bad",
    ["bob/../admin", "bob smith", "bob?x=1", "bob#", "", "a" * 256],
)
def test_add_members_rejects_a_malformed_username_before_any_request(bad):
    client = FakeClient()

    with pytest.raises(InputError):
        members.add_members(client, "PROJ", "Developers", users=[bad] if bad else None)
    assert client.calls == []


def test_add_members_is_refused_in_read_only_mode_before_any_request():
    client = FakeClient(read_only=True)

    with pytest.raises(ReadOnlyError):
        members.add_members(client, "PROJ", "Developers", users="bob")
    assert client.calls == []


# ----------------------------------------------------------------- removing


def test_remove_members_deletes_only_actual_members_by_username():
    client = FakeClient()

    result = members.remove_members(client, "PROJ", "Developers", users="Carol,zed")

    assert client.writes() == [("DELETE", "/rest/api/2/project/PROJ/role/10001", {"user": "carol"})]
    assert result["removed_users"] == ["carol"]
    assert result["not_members"] == ["zed"]
    assert [u["username"] for u in result["members_after"]["users"]] == ["alice"]


def test_remove_members_can_remove_a_group():
    client = FakeClient()

    result = members.remove_members(client, "PROJ", "Administrators", groups="jira-administrators")

    assert client.writes() == [
        ("DELETE", "/rest/api/2/project/PROJ/role/10002", {"group": "jira-administrators"})
    ]
    assert result["members_after"]["groups"] == []


def test_remove_members_sends_nothing_for_non_members():
    client = FakeClient()

    result = members.remove_members(client, "PROJ", "Developers", users=["zed"])

    assert client.writes() == []
    assert result["removed_users"] == []


def test_remove_members_needs_at_least_one_name():
    with pytest.raises(InputError, match="at least one"):
        members.remove_members(FakeClient(), "PROJ", "Developers")


def test_remove_members_is_refused_in_read_only_mode_before_any_request():
    client = FakeClient(read_only=True)

    with pytest.raises(ReadOnlyError):
        members.remove_members(client, "PROJ", "Developers", users="carol")
    assert client.calls == []
