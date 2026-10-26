"""Tests for the project-members menu section (``jira_cli.cli.menu_members``).

Fully offline: the terminal is a scripted IO, ``jira_cli.cli.menu_people`` and
``jira_cli.services.members`` are fakes installed in ``sys.modules``.
"""

from __future__ import annotations

import sys
import types

import pytest

from jira_cli.cli import menu, menu_members

BACK = object()


class ScriptedIO:
    """A ``MenuIO`` whose ``choose``/``confirm`` answers come from a script."""

    def __init__(self, script):
        self.script = list(script)
        self.shown: list[str] = []
        self.asked: list[tuple] = []

    def _answer(self, kind):
        if not self.script:
            raise AssertionError(f"script exhausted; section asked for a {kind}")
        expected, value = self.script.pop(0)
        assert expected == kind, f"script expected a {expected}, section asked for a {kind}"
        return value

    def ask(self, prompt, default=None):
        self.asked.append(("ask", prompt))
        return self._answer("ask")

    def choose(self, title, options):
        self.asked.append(("choose", title, list(options)))
        return self._answer("choose")

    def confirm(self, prompt):
        self.asked.append(("confirm", prompt))
        return self._answer("confirm")

    def show(self, text):
        self.shown.append(str(text))

    @property
    def text(self):
        return "\n".join(self.shown)

    def options(self, title_fragment):
        return [e[2] for e in self.asked if e[0] == "choose" and title_fragment in e[1]][0]


ROLES = [
    {
        "id": 1,
        "name": "Administrators",
        "users": [{"username": "alice", "display_name": "Alice Admin"}],
        "groups": ["jira-administrators"],
    },
    {
        "id": 2,
        "name": "Developers",
        "users": [
            {"username": "bob", "display_name": "Bob Builder"},
            {"username": "carol", "display_name": "Carol Coder"},
        ],
        "groups": [],
    },
    {"id": 3, "name": "Viewers", "users": [], "groups": []},
]

DIRECTORY = [
    {
        "username": "alice",
        "display_name": "Alice Admin",
        "active": True,
        "projects": [{"key": "PROJ", "roles": ["Administrators"]}],
        "project_admin": ["PROJ"],
        "jira_admin": True,
    },
    {
        "username": "bob",
        "display_name": "Bob Builder",
        "active": True,
        "projects": [{"key": "PROJ", "roles": ["Developers"]}],
        "project_admin": [],
        "jira_admin": False,
    },
    {
        "username": "carol",
        "display_name": "Carol Coder",
        "active": False,
        "projects": [{"key": "PROJ", "roles": ["Developers"]}],
        "project_admin": [],
        "jira_admin": False,
    },
    {
        "username": "dave",
        "display_name": "Dave Group",
        "active": True,
        "projects": [{"key": "PROJ", "roles": ["Administrators (via group jira-administrators)"]}],
        "project_admin": ["PROJ"],
        "jira_admin": True,
    },
    {
        "username": "erin",
        "display_name": "Erin Elsewhere",
        "active": True,
        "projects": [{"key": "OTHER", "roles": ["Developers"]}],
        "project_admin": [],
        "jira_admin": None,
    },
]


class FakePeopleUI(types.ModuleType):
    """Stands in for ``jira_cli.cli.menu_people`` (contract from WAVE2)."""

    def __init__(self, projects, picks):
        super().__init__("jira_cli.cli.menu_people")
        self.project_answers = list(projects)
        self.pick_answers = list(picks)
        self.people_calls: list[dict] = []

    def ask_projects(self, session, title="Projects", *, allow_all=True):
        assert allow_all is False
        if not self.project_answers:
            raise menu._Back
        return self.project_answers.pop(0)

    def ask_people(self, session, title, *, project=None, only=None, exclude=None):
        self.people_calls.append({"title": title, "project": project, "only": only, "exclude": exclude})
        answer = self.pick_answers.pop(0)
        if answer is BACK:
            raise menu._Back
        return answer

    def people(self, session, project=None):
        return [dict(p) for p in DIRECTORY]


class FakeMembers(types.ModuleType):
    def __init__(self):
        super().__init__("jira_cli.services.members")
        self.roles = [dict(r, users=list(r["users"])) for r in ROLES]
        self.writes: list[tuple] = []

    def list_roles(self, client, project):
        assert project == "PROJ"
        return [dict(r, users=list(r["users"])) for r in self.roles]

    def _role(self, name):
        return next(r for r in self.roles if r["name"] == name)

    def add_members(self, client, project, role, users=None, groups=None):
        self.writes.append(("add", project, role, list(users)))
        self._role(role)["users"] += [{"username": u, "display_name": u} for u in users]
        return {"added_users": list(users)}

    def remove_members(self, client, project, role, users=None, groups=None):
        self.writes.append(("remove", project, role, list(users)))
        r = self._role(role)
        r["users"] = [u for u in r["users"] if u["username"] not in users]
        return {"removed_users": list(users)}


class FakeClient:
    def __init__(self, me="alice"):
        self.me = me

    def myself(self):
        return {"name": self.me}


@pytest.fixture
def world(monkeypatch):
    def make(script, *, projects=(["PROJ"],), picks=(), me="alice"):
        ui = FakePeopleUI(projects, picks)
        members = FakeMembers()
        monkeypatch.setitem(sys.modules, "jira_cli.cli.menu_people", ui)
        monkeypatch.setitem(sys.modules, "jira_cli.services.members", members)
        io = ScriptedIO(script)
        session = menu._Session(client=FakeClient(me), settings=types.SimpleNamespace(), io=io)
        return session, io, ui, members

    return make


def _run(session):
    with pytest.raises(menu._Back):
        menu_members.run(session)


# ------------------------------------------------------------------- show


def test_show_lists_everyone_in_the_project_with_roles_admin_and_active(world, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")  # wide enough that no cell is truncated
    session, io, _, _ = world([("choose", 0), ("choose", None)])
    _run(session)
    text = io.text
    assert "Members of PROJ (4 people)" in text
    assert "Administrators (via group jira-administrators)" in text
    assert "Dave Group" in text
    assert "erin" not in text  # member of another project only
    assert "project admin, Jira admin" in text
    assert "group jira-administrators" in text


def test_member_rows_are_numbered_and_carry_active_and_admin():
    rows = menu_members.member_rows("PROJ", ROLES, DIRECTORY)
    by_user = {r["Username"]: r for r in rows}
    assert [r["#"] for r in rows] == [1, 2, 3, 4]
    assert by_user["carol"]["Active"] == "no"
    assert by_user["bob"]["Admin"] == "-"
    assert by_user["bob"]["Roles"] == "Developers"


def test_member_rows_list_a_direct_member_unknown_to_the_directory():
    roles = [{"name": "Developers", "users": [{"username": "zed", "display_name": "Zed"}]}]
    rows = menu_members.member_rows("PROJ", roles, [])
    assert rows == [
        {"#": 1, "Name": "Zed", "Username": "zed", "Roles": "Developers", "Admin": "?", "Active": "?"}
    ]


def test_member_rows_drop_a_stale_directory_row_whose_direct_role_is_gone():
    rows = menu_members.member_rows("PROJ", [], [DIRECTORY[1]])
    assert rows == []


# -------------------------------------------------------------------- add


def test_add_offers_developers_first_and_excludes_current_role_members(world):
    session, io, ui, members = world(
        [("choose", 1), ("choose", 0), ("confirm", False), ("choose", None)],
        picks=[["erin"]],
    )
    _run(session)
    assert io.options("Role in PROJ")[0] == "Developers (2 people)"
    assert io.options("Role in PROJ")[1] == "Administrators (1 person)"
    assert ui.people_calls[0]["exclude"] == ["bob", "carol"]
    assert "add erin" in io.text
    assert "cancelled; nothing was written" in io.text
    assert members.writes == []


def test_add_excludes_people_who_hold_the_role_through_a_group(world):
    session, _, ui, _ = world(
        [("choose", 1), ("choose", 1), ("confirm", False), ("choose", None)],
        picks=[["erin"]],
    )
    _run(session)
    assert sorted(ui.people_calls[0]["exclude"]) == ["alice", "dave"]


def test_confirmed_add_writes_then_shows_the_role_re_read(world):
    session, io, _, members = world(
        [("choose", 1), ("choose", 0), ("confirm", True), ("choose", None)],
        picks=[["erin"]],
    )
    _run(session)
    assert members.writes == [("add", "PROJ", "Developers", ["erin"])]
    assert "PROJ Developers now has 3 people: bob, carol, erin" in io.text


def test_add_never_previews_someone_who_is_already_a_member(world):
    session, io, _, members = world(
        [("choose", 1), ("choose", 0), ("choose", None), ("choose", None)],
        picks=[["BOB"]],
    )
    _run(session)
    assert "nobody picked; nothing to do" in io.text
    assert "Add to project" not in io.text
    assert members.writes == []


def test_back_at_add_who_returns_to_the_role_list(world):
    session, io, _, _ = world(
        [("choose", 1), ("choose", 0), ("choose", None), ("choose", None)],
        picks=[BACK],
    )
    _run(session)
    role_prompts = [e for e in io.asked if e[0] == "choose" and "Role in" in e[1]]
    assert len(role_prompts) == 2


# ----------------------------------------------------------------- remove


def test_remove_offers_only_direct_members_and_says_work_is_untouched(world):
    session, io, ui, members = world(
        [("choose", 2), ("choose", 0), ("confirm", False), ("choose", None)],
        picks=[["bob"]],
    )
    _run(session)
    assert ui.people_calls[0]["only"] == ["bob", "carol"]
    assert "remove bob" in io.text
    assert "their issues, comments and worklogs are not touched" in io.text
    assert "WARNING" not in io.text
    assert members.writes == []


def test_removing_yourself_adds_a_warning_line_to_the_preview(world):
    session, io, _, _ = world(
        [("choose", 2), ("choose", 1), ("confirm", False), ("choose", None)],
        picks=[["alice"]],
        me="alice",
    )
    _run(session)
    assert "WARNING: this removes YOU (alice); you may lose access to PROJ" in io.text


def test_confirmed_remove_writes_then_shows_the_role_re_read(world):
    session, io, _, members = world(
        [("choose", 2), ("choose", 0), ("confirm", True), ("choose", None)],
        picks=[["carol"]],
    )
    _run(session)
    assert members.writes == [("remove", "PROJ", "Developers", ["carol"])]
    assert "PROJ Developers now has 1 person: bob" in io.text


def test_remove_from_a_role_with_no_direct_members_re_asks_the_role(world):
    session, io, ui, _ = world([("choose", 2), ("choose", 2), ("choose", None), ("choose", None)])
    _run(session)
    assert "role Viewers has nobody to remove" in io.text
    assert ui.people_calls == []


# ------------------------------------------------------------- navigation


def test_more_than_one_project_is_re_asked(world):
    session, io, _, _ = world([("choose", None)], projects=[["PROJ", "OTHER"], ["PROJ"]])
    _run(session)
    assert "pick exactly one project" in io.text


def test_back_at_the_action_list_returns_to_the_project_pick(world):
    session, io, _, _ = world([("choose", None), ("choose", None)], projects=[["PROJ"], ["PROJ"]])
    _run(session)
    assert len([e for e in io.asked if e[0] == "choose"]) == 2
