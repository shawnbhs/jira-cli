"""Tests for menu section 11, teams (``jira_cli.cli.menu_teams``).

Offline: the real roster service writes to a ``tmp_path`` teams file; the shared
people/project pickers and the board listing are recorded fakes.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from jira_cli.cli import menu_teams
from jira_cli.cli.menu import _Back, _Session
from jira_cli.services import people as real_people

BOARDS = {
    "PROJ": [
        {"id": 12, "name": "PROJ board", "type": "scrum", "project": "PROJ"},
        {"id": 13, "name": "PROJ support", "type": "kanban", "project": "PROJ"},
    ],
    "OPS": [{"id": 12, "name": "PROJ board", "type": "scrum", "project": "OPS"}],
}


class ScriptedIO:
    """Answers come from a list of ``(kind, value)``; asking out of order fails."""

    def __init__(self, script):
        self.script = list(script)
        self.shown: list[str] = []
        self.asked: list[tuple] = []

    def _answer(self, kind):
        if not self.script:
            raise AssertionError(f"script exhausted; menu asked for a {kind}")
        expected, value = self.script.pop(0)
        if expected != kind:
            raise AssertionError(f"script expected a {expected}, menu asked for a {kind}")
        return value

    def ask(self, prompt, default=None):
        self.asked.append(("ask", prompt, default))
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
    def text(self) -> str:
        return "\n".join(self.shown)

    def options(self, title):
        return next(entry[2] for entry in self.asked if entry[0] == "choose" and entry[1] == title)


@dataclass
class FakeSettings:
    teams_file: Path
    default_project: str = "PROJ"


class Pickers:
    """Fake ``menu_people`` and ``services.boards``; records how they were called."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.members = ["alice", "bob"]
        self.projects = ["PROJ"]
        self.people_ui = types.ModuleType("jira_cli.cli.menu_people")
        self.people_ui.ask_people = self.ask_people
        self.people_ui.ask_projects = self.ask_projects
        self.boards = types.ModuleType("jira_cli.services.boards")
        self.boards.list_boards = self.list_boards

    def ask_people(self, session, title, **kwargs):
        self.calls.append(("ask_people", title, kwargs))
        return list(self.members)

    def ask_projects(self, session, title="Projects", **kwargs):
        self.calls.append(("ask_projects", title, kwargs))
        return list(self.projects)

    def list_boards(self, client, project_key=None, board_type=None):
        self.calls.append(("list_boards", project_key))
        return list(BOARDS.get(project_key, []))


@pytest.fixture
def pickers(monkeypatch):
    fake = Pickers()
    monkeypatch.setitem(sys.modules, "jira_cli.cli.menu_people", fake.people_ui)
    monkeypatch.setitem(sys.modules, "jira_cli.services.boards", fake.boards)
    monkeypatch.setitem(sys.modules, "jira_cli.services.people", real_people)
    return fake


@pytest.fixture
def roster(tmp_path) -> Path:
    path = tmp_path / "teams.yaml"
    real_people.save_team(path, "backend", ["alice", "bob"], ["PROJ"], [12])
    return path


def _session(script, teams_file: Path) -> _Session:
    return _Session(client=object(), settings=FakeSettings(teams_file), io=ScriptedIO(script))


def _saved(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ create


def test_create_saves_members_projects_and_picked_boards(pickers, tmp_path):
    path = tmp_path / "teams.yaml"
    script = [
        ("choose", 2),  # Create
        ("ask", "frontend"),
        ("choose", 0),  # projects: Choose
        ("choose", 0),  # boards: Choose
        ("ask", "2"),
        ("confirm", True),
    ]
    session = _session(script, path)

    menu_teams.run(session)

    assert _saved(path)["frontend"] == {
        "members": ["alice", "bob"],
        "projects": ["PROJ"],
        "boards": [13],
    }
    assert ("ask_projects", "Projects of frontend", {"allow_all": False}) in pickers.calls


def test_invalid_team_name_is_refused_before_anything_else(pickers, roster):
    script = [("choose", 2), ("ask", "bad name!"), ("ask", "backend"), ("ask", "b")]
    session = _session(script, roster)

    with pytest.raises(_Back):
        menu_teams.run(session)

    assert "team name must be 1-64 letters" in session.io.text
    assert "team backend already exists" in session.io.text
    assert not any(call[0] == "ask_people" for call in pickers.calls)


def test_board_answer_with_an_unlisted_token_is_refused_not_dropped(pickers, tmp_path):
    path = tmp_path / "teams.yaml"
    script = [
        ("choose", 2),
        ("ask", "frontend"),
        ("choose", 0),
        ("choose", 0),
        ("ask", "1,x"),
        ("ask", "1-2"),
        ("confirm", False),
    ]
    session = _session(script, path)

    menu_teams.run(session)

    assert "'x' is not a number from 1 to 2" in session.io.text
    assert "boards   12 PROJ board, 13 PROJ support" in session.io.text
    assert not path.exists()


def test_boards_of_several_projects_are_listed_once_each(pickers, tmp_path):
    pickers.projects = ["PROJ", "OPS"]
    script = [
        ("choose", 2),
        ("ask", "frontend"),
        ("choose", 0),
        ("choose", 0),
        ("ask", "all"),
        ("confirm", False),
    ]
    session = _session(script, tmp_path / "teams.yaml")

    menu_teams.run(session)

    assert "boards   12 PROJ board, 13 PROJ support" in session.io.text


def test_project_without_boards_says_so_and_saves_none(pickers, tmp_path):
    pickers.projects = ["EMPTY"]
    script = [("choose", 2), ("ask", "frontend"), ("choose", 0), ("choose", 0), ("confirm", False)]
    session = _session(script, tmp_path / "teams.yaml")

    menu_teams.run(session)

    assert "no boards found for EMPTY" in session.io.text
    assert "boards   (none)" in session.io.text


# ------------------------------------------------------------------ edit


def test_edit_can_clear_projects_and_boards_explicitly(pickers, roster):
    script = [
        ("choose", 3),  # Edit
        ("choose", 0),  # backend
        ("choose", 0),  # members: keep
        ("choose", 2),  # projects: None (clear)
        ("choose", 2),  # boards: None (clear)
        ("confirm", True),
    ]
    session = _session(script, roster)

    menu_teams.run(session)

    assert "projects PROJ -> (none)" in session.io.text
    assert "boards   12 -> (none)" in session.io.text
    assert _saved(roster)["backend"] == {"members": ["alice", "bob"], "projects": [], "boards": []}


def test_edit_keeping_everything_previews_without_changes(pickers, roster):
    script = [("choose", 3), ("choose", 0), ("choose", 0), ("choose", 0), ("choose", 0)]
    script.append(("confirm", False))
    session = _session(script, roster)

    menu_teams.run(session)

    assert "members  alice, bob" in session.io.text
    assert "projects PROJ" in session.io.text
    assert "->" not in session.io.text
    assert not any(call[0] == "ask_people" for call in pickers.calls)


def test_edit_offers_keep_with_the_current_value(pickers, roster):
    script = [("choose", 3), ("choose", 0), ("choose", None)]
    session = _session(script, roster)

    with pytest.raises(_Back):
        menu_teams.run(session)

    assert session.io.options("Members of backend") == ["Keep: alice, bob", "Change"]


def test_declined_edit_leaves_the_roster_file_untouched(pickers, roster):
    before = roster.read_bytes()
    script = [("choose", 3), ("choose", 0), ("choose", 1), ("choose", 2), ("choose", 2)]
    script.append(("confirm", False))
    session = _session(script, roster)

    menu_teams.run(session)

    assert roster.read_bytes() == before
    assert "cancelled; nothing was written" in session.io.text


# ------------------------------------------------------------------ list / show / remove


def test_list_marks_empty_fields_as_none(pickers, tmp_path):
    path = tmp_path / "teams.yaml"
    real_people.save_team(path, "solo", ["carol"])
    session = _session([("choose", 0)], path)

    menu_teams.run(session)

    assert "(none)" in session.io.text and "carol" in session.io.text


def test_show_prints_every_field(pickers, roster):
    session = _session([("choose", 1), ("choose", 0)], roster)

    menu_teams.run(session)

    assert "members  alice, bob" in session.io.text
    assert "boards   12" in session.io.text


def test_remove_after_confirmation_deletes_the_team(pickers, roster):
    session = _session([("choose", 4), ("choose", 0), ("confirm", True)], roster)

    menu_teams.run(session)

    assert _saved(roster) == {}


def test_no_teams_yet_for_show(pickers, tmp_path):
    session = _session([("choose", 1)], tmp_path / "teams.yaml")

    menu_teams.run(session)

    assert session.io.shown == ["no teams yet"]


# ------------------------------------------------------------------ parse_numbers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", [0]),
        ("1,3", [0, 2]),
        ("2-3", [1, 2]),
        ("3, 1 1", [2, 0]),
        ("all", [0, 1, 2]),
        ("none", []),
        ("-", []),
    ],
)
def test_parse_numbers_accepts_lists_ranges_all_and_none(text, expected):
    assert menu_teams.parse_numbers(text, 3) == expected


@pytest.mark.parametrize("text", ["0", "4", "x", "1,x", "3-1", "2-", ",", "1.5"])
def test_parse_numbers_rejects_anything_not_listed(text):
    with pytest.raises(ValueError):
        menu_teams.parse_numbers(text, 3)
