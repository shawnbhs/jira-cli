"""Tests for the shared menu pickers (``jira_cli.cli.menu_people``).

Fully offline: a scripted IO replaces the terminal and the directory, people
and projects services are fakes installed in ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from jira_cli.cli import menu, menu_people
from jira_cli.errors import JiraError, JiraPermissionError


class ScriptedIO:
    """Answers ``ask`` from a list; Ctrl-C/EOF are not needed here."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def ask(self, prompt, default=None):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"script exhausted at prompt {prompt!r}")
        return self.answers.pop(0)

    def show(self, text):
        self.shown.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.shown)


def _person(username, display, *projects):
    return {
        "username": username,
        "display_name": display,
        "active": True,
        "projects": [{"key": key, "roles": ["Developers"]} for key in projects],
        "project_admin": [],
        "jira_admin": False,
    }


DIRECTORY = [
    _person("alice", "Alice Archer", "PROJ"),
    _person("bob", "Bob Baker", "PROJ", "OPS"),
    _person("carol", "Carol Cook", "OPS"),
]

PROJECTS = [
    {"key": "PROJ", "name": "Project"},
    {"key": "OPS", "name": "Operations"},
    {"key": "WEB", "name": "Website"},
]


class Services:
    """Recording fakes for the three service modules the pickers use."""

    def __init__(self, monkeypatch, *, directory=DIRECTORY, directory_error=None, teams=None):
        self.directory_calls = 0
        self.project_calls = 0
        self.assignable_calls: list[str] = []
        self.search_calls: list[str] = []
        self.teams = teams or {}

        def build_directory(client, projects=None, **_):
            self.directory_calls += 1
            if directory_error is not None:
                raise directory_error
            return [dict(person) for person in directory]

        def suggest(people, token, limit=3):
            return [p for p in people if p["username"].startswith(token[:2])][:limit]

        def assignable_users(client, project_key, query=""):
            self.assignable_calls.append(project_key)
            return [{"username": "dave", "display_name": "Dave Diver", "active": True}]

        def search_users(client, query, limit=50):
            self.search_calls.append(query)
            return [{"username": "erin", "display_name": "Erin Eve", "active": True}]

        def load_teams(path):
            return self.teams

        def resolve_people(spec, teams):
            name = spec.split(":", 1)[1]
            if name not in teams:
                raise menu.JiraError(f"unknown team {name!r}")
            return list(teams[name])

        def list_projects(client):
            self.project_calls += 1
            return [dict(p) for p in PROJECTS]

        def validate_project_key(key):
            return key

        self._install(monkeypatch, "directory", build_directory=build_directory, suggest=suggest)
        self._install(
            monkeypatch,
            "people",
            assignable_users=assignable_users,
            search_users=search_users,
            load_teams=load_teams,
            resolve_people=resolve_people,
        )
        self._install(
            monkeypatch, "projects", list_projects=list_projects, validate_project_key=validate_project_key
        )

    @staticmethod
    def _install(monkeypatch, name, **attrs):
        module = types.ModuleType(f"jira_cli.services.{name}")
        for attr, value in attrs.items():
            setattr(module, attr, value)
        monkeypatch.setitem(sys.modules, f"jira_cli.services.{name}", module)


def _session(answers=(), default_project=None):
    settings = types.SimpleNamespace(default_project=default_project, teams_file=Path("teams.yaml"))
    return menu._Session(client=object(), settings=settings, io=ScriptedIO(answers))


def _names(rows):
    return [row["username"] for row in rows]


# --------------------------------------------------------------- people()


def test_directory_is_built_once_per_session(monkeypatch):
    services = Services(monkeypatch)
    session = _session()

    menu_people.people(session)
    menu_people.people(session, "OPS")

    assert services.directory_calls == 1
    assert "loading people from Jira..." in session.io.text


def test_project_filter_keeps_only_people_with_a_role_there(monkeypatch):
    Services(monkeypatch)

    assert _names(menu_people.people(_session(), "ops")) == ["bob", "carol"]


def test_project_nobody_belongs_to_returns_everyone(monkeypatch):
    Services(monkeypatch)

    assert _names(menu_people.people(_session(), "WEB")) == ["alice", "bob", "carol"]


def test_directory_error_falls_back_to_assignable_users_and_says_why_once(monkeypatch):
    services = Services(monkeypatch, directory_error=JiraPermissionError("not allowed", status=403))
    session = _session()

    first = menu_people.people(session, "PROJ")
    second = menu_people.people(session, "PROJ")

    assert (
        first
        == second
        == [
            {
                "username": "dave",
                "display_name": "Dave Diver",
                "active": True,
                "projects": [],
                "project_admin": [],
                "jira_admin": None,
            }
        ]
    )
    assert services.directory_calls == 1
    assert services.assignable_calls == ["PROJ"]
    assert session.io.text.count("people directory unavailable") == 1
    assert "not allowed" in session.io.text


def test_directory_error_without_project_falls_back_to_user_search(monkeypatch):
    services = Services(monkeypatch, directory_error=JiraError("boom"))

    assert _names(menu_people.people(_session())) == ["erin"]
    assert services.search_calls == ["."]


def test_empty_directory_also_falls_back(monkeypatch):
    services = Services(monkeypatch, directory=[])

    assert _names(menu_people.people(_session(), "PROJ")) == ["dave"]
    assert services.assignable_calls == ["PROJ"]


# ------------------------------------------------- ask_person / ask_people


def test_ask_person_returns_the_numbered_username(monkeypatch):
    Services(monkeypatch)
    session = _session(["2"])

    assert menu_people.ask_person(session, "Assignee", project="OPS") == "carol"


def test_ask_person_filter_then_number(monkeypatch):
    Services(monkeypatch)
    session = _session(["/car", "1"])

    assert menu_people.ask_person(session, "Assignee") == "carol"


def test_ask_person_allow_none_returns_none_for_zero(monkeypatch):
    Services(monkeypatch)
    session = _session(["0"])

    assert menu_people.ask_person(session, "Assignee", allow_none=True, none_label="(unassigned)") is None
    assert "0. (unassigned)" in session.io.text


def test_ask_person_back_raises_back(monkeypatch):
    Services(monkeypatch)

    with pytest.raises(menu._Back):
        menu_people.ask_person(_session(["b"]), "Assignee")


def test_ask_people_back_raises_back(monkeypatch):
    Services(monkeypatch)

    with pytest.raises(menu._Back):
        menu_people.ask_people(_session(["b"]), "Watchers")


def test_ask_people_accepts_ranges(monkeypatch):
    Services(monkeypatch)

    assert menu_people.ask_people(_session(["1-2"]), "Who") == ["alice", "bob"]


def test_only_keeps_directory_order_and_shows_unknown_usernames(monkeypatch):
    Services(monkeypatch)
    session = _session(["all"])

    picked = menu_people.ask_people(session, "Who", only=["zed", "carol", "ALICE"])

    assert picked == ["alice", "carol", "zed"]


def test_exclude_removes_usernames_case_insensitively(monkeypatch):
    Services(monkeypatch)

    assert menu_people.ask_people(_session(["all"]), "Who", exclude=["BOB"]) == ["alice", "carol"]


def test_team_name_resolves_through_teams_file(monkeypatch):
    Services(monkeypatch, teams={"backend": ["bob", "carol"]})

    assert menu_people.ask_people(_session(["team:backend"]), "Who") == ["bob", "carol"]


def test_unknown_team_is_reported_and_re_asked(monkeypatch):
    Services(monkeypatch, teams={})
    session = _session(["team:nope", "1"])

    assert menu_people.ask_people(session, "Who") == ["alice"]
    assert "unknown team 'nope'" in session.io.text


# ------------------------------------------------------------ ask_projects


def test_ask_projects_enter_takes_the_default(monkeypatch):
    Services(monkeypatch)
    session = _session([""], default_project="PROJ")

    assert menu_people.ask_projects(session) == ["PROJ"]
    assert session.io.prompts == ["Pick numbers or keys, all = every project, Enter = [PROJ], b = back"]


@pytest.mark.parametrize("answer", ["all", "*", "ALL"])
def test_ask_projects_all_means_every_project(monkeypatch, answer):
    Services(monkeypatch)

    assert menu_people.ask_projects(_session([answer], default_project="PROJ")) is None


def test_ask_projects_enter_without_default_means_all(monkeypatch):
    Services(monkeypatch)

    assert menu_people.ask_projects(_session([""])) is None


def test_ask_projects_numbers_ranges_and_keys(monkeypatch):
    Services(monkeypatch)

    assert menu_people.ask_projects(_session(["1, web 2-3"])) == ["PROJ", "WEB", "OPS"]


def test_ask_projects_unknown_key_is_re_asked(monkeypatch):
    Services(monkeypatch)
    session = _session(["NOPE", "9", "ops"])

    assert menu_people.ask_projects(session) == ["OPS"]
    assert "unknown project: NOPE" in session.io.text
    assert "unknown project: 9" in session.io.text


def test_ask_projects_back_raises_back(monkeypatch):
    Services(monkeypatch)

    with pytest.raises(menu._Back):
        menu_people.ask_projects(_session(["b"]))


def test_ask_projects_without_allow_all_rejects_all(monkeypatch):
    Services(monkeypatch)
    session = _session(["all", "", "3"])

    assert menu_people.ask_projects(session, allow_all=False) == ["WEB"]
    assert session.io.prompts[0] == "Pick numbers or keys, b = back"


def test_project_list_is_cached_on_the_session(monkeypatch):
    services = Services(monkeypatch)
    session = _session(["1", "2"])

    menu_people.ask_projects(session)
    menu_people.ask_projects(session)

    assert services.project_calls == 1
