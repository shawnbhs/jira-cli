"""Tests for menu item 9, Issue actions (``jira_cli.cli.menu_actions``).

Fully offline: a scripted IO replaces the terminal, and every service module
(plus the shared people picker) is a fake installed in ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from jira_cli.cli import menu, menu_actions
from jira_cli.errors import InputError, JiraError, NotFoundError


class ScriptedIO:
    """Answers come from a script of ``(kind, value)``; prompts and defaults are recorded."""

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
    def text(self):
        return "\n".join(self.shown)

    def options(self, title_fragment):
        for entry in self.asked:
            if entry[0] == "choose" and title_fragment in entry[1]:
                return entry[2]
        raise AssertionError(f"no choose prompt mentioning {title_fragment!r}")


@dataclass
class FakeSettings:
    base_url: str = "https://jira.example.com"
    default_project: str = "PROJ"
    filters_file: Path = Path("filters.yaml")


class FakeClient:
    pass


def _issue(key, **extra):
    base = {
        "key": key,
        "url": f"https://jira.example.com/browse/{key}",
        "summary": f"Summary of {key}",
        "type": "Task",
        "status": "To Do",
        "priority": "Medium",
        "assignee": "alice",
        "project": key.split("-")[0],
        "labels": [],
        "due": None,
        "estimate_seconds": None,
        "remaining_seconds": None,
        "story_points": None,
        "sprint": None,
        "description": None,
        "comments": [],
    }
    base.update(extra)
    return base


class Fakes:
    """Recording fakes for issues / worklog / boards / projects / menu_people."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.issues = {"PROJ-1": _issue("PROJ-1"), "PROJ-2": _issue("PROJ-2")}
        self.board: dict | None = {"id": 7, "name": "PROJ board", "type": "kanban"}
        self.board_error: Exception | None = None
        self.sprints = [
            {"id": 11, "name": "Sprint 11", "state": "active"},
            {"id": 12, "name": "Sprint 12", "state": "future"},
        ]
        self.person = "bob"

    def record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def names(self):
        return [call[0] for call in self.calls]

    def call(self, name):
        matches = [c for c in self.calls if c[0] == name]
        assert matches, f"{name} was never called; calls: {self.names()}"
        return matches[-1]

    # issues
    def get_issue(self, client, key, comments=True, changelog=False):
        self.record("get_issue", key, comments=comments)
        if key not in self.issues:
            raise NotFoundError("Issue Does Not Exist", status=404)
        return dict(self.issues[key])

    def list_transitions(self, client, key):
        return [
            {"id": "11", "name": "Start", "to": "In Progress"},
            {"id": "21", "name": "Reopen", "to": "To Do"},
        ]

    def list_link_types(self, client):
        return [
            {"id": "1", "name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
            {"id": "2", "name": "Relates", "outward": "relates to", "inward": "relates to"},
        ]

    def board_for_project(self, client, project):
        if self.board_error:
            raise self.board_error
        return self.board

    def list_sprints(self, client, board_id, state="active,future"):
        self.record("list_sprints", board_id, state)
        return self.sprints

    def ask_person(self, session, title, **kwargs):
        self.record("ask_person", title, **kwargs)
        return self.person

    def writer(self, name):
        return lambda *args, **kwargs: self.record(name, *args[1:], **kwargs)


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    modules = {
        "jira_cli.services.issues": _module(
            "jira_cli.services.issues",
            get_issue=f.get_issue,
            list_transitions=f.list_transitions,
            list_link_types=f.list_link_types,
            assign=f.writer("assign"),
            transition=f.writer("transition"),
            add_comment=f.writer("add_comment"),
            update_fields=f.writer("update_fields"),
            link_issues=f.writer("link_issues"),
        ),
        "jira_cli.services.worklog": _module(
            "jira_cli.services.worklog",
            add_worklog=f.writer("add_worklog"),
            set_estimate=f.writer("set_estimate"),
        ),
        "jira_cli.services.boards": _module(
            "jira_cli.services.boards",
            board_for_project=f.board_for_project,
            list_sprints=f.list_sprints,
            add_to_sprint=f.writer("add_to_sprint"),
            move_to_backlog=f.writer("move_to_backlog"),
        ),
        "jira_cli.services.projects": _module(
            "jira_cli.services.projects", list_priorities=lambda client: ["High", "Medium", "Low"]
        ),
        "jira_cli.cli.menu_people": _module("jira_cli.cli.menu_people", ask_person=f.ask_person),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return f


def _session(script):
    return menu._Session(client=FakeClient(), settings=FakeSettings(), io=ScriptedIO(script))


def _run(script):
    session = _session(script)
    menu._run_action(session, menu_actions.run)
    assert not session.io.script, f"unused answers: {session.io.script}"
    return session


BACK = ("choose", None)


def _action(label):
    labels = [name for name, _ in menu_actions._ACTIONS] + [menu_actions._SPRINT_ACTION[0]]
    return ("choose", next(i for i, name in enumerate(labels) if name.startswith(label)))


# ------------------------------------------------------------------ parsing


def test_parse_issue_ref_accepts_key_lowercase_and_browse_url():
    assert menu_actions.parse_issue_ref(" proj-12 ") == "PROJ-12"
    assert menu_actions.parse_issue_ref("https://jira.example.com/browse/PROJ-12") == "PROJ-12"
    assert menu_actions.parse_issue_ref("https://jira.example.com/browse/PROJ-12?focusedId=3") == "PROJ-12"


@pytest.mark.parametrize("text", ["not a key", "1-2", "https://jira.example.com/projects/PROJ", ""])
def test_parse_issue_ref_rejects_text_without_a_key(text):
    with pytest.raises(InputError):
        menu_actions.parse_issue_ref(text)


def test_parse_due_date_counts_forward_and_dash_clears():
    today = date(2030, 1, 10)
    assert menu_actions.parse_due_date("7d", today) == "2030-01-17"
    assert menu_actions.parse_due_date("2w", today) == "2030-01-24"
    assert menu_actions.parse_due_date("tomorrow", today) == "2030-01-11"
    assert menu_actions.parse_due_date("2030-02-01", today) == "2030-02-01"
    assert menu_actions.parse_due_date("-", today) == ""


@pytest.mark.parametrize("text", ["next tuesday", "notadate", "2030-13-45"])
def test_parse_due_date_rejects_garbage(text):
    with pytest.raises(InputError):
        menu_actions.parse_due_date(text, date(2030, 1, 10))


def test_parse_started_rejects_future_dates():
    with pytest.raises(InputError, match="future"):
        menu_actions.parse_started("2030-01-11", date(2030, 1, 10))
    assert menu_actions.parse_started("3d", date(2030, 1, 10)) == "2030-01-07"


@pytest.mark.parametrize("text", ["nan", "inf", "-1", "abc"])
def test_parse_story_points_rejects_non_finite_or_negative(text):
    with pytest.raises(InputError):
        menu_actions.parse_story_points(text)


def test_parse_numbers_reads_lists_ranges_and_all():
    assert menu_actions.parse_numbers("1,3", 3) == [0, 2]
    assert menu_actions.parse_numbers("2-3", 3) == [1, 2]
    assert menu_actions.parse_numbers("all", 2) == [0, 1]
    with pytest.raises(InputError):
        menu_actions.parse_numbers("4", 3)
    with pytest.raises(InputError):
        menu_actions.parse_numbers("x", 3)


# -------------------------------------------------------------- issue key


def test_invalid_and_missing_keys_are_re_asked_and_never_remembered(fakes):
    session = _run([("ask", "not a key"), ("ask", "PROJ-999"), ("ask", "proj-1"), BACK])
    assert "invalid issue key" in session.io.text
    assert "PROJ-999 does not exist" in session.io.text
    assert session.last_key == "PROJ-1"
    assert any(e[0] == "choose" and e[1].startswith("Actions for PROJ-1") for e in session.io.asked)


def test_issue_key_prompt_accepts_a_browse_url(fakes):
    session = _run([("ask", "https://jira.example.com/browse/PROJ-2"), BACK])
    assert session.last_key == "PROJ-2"


# ----------------------------------------------------------------- actions


def test_assign_picks_from_the_issue_project_and_cancel_writes_nothing(fakes):
    session = _run([("ask", "PROJ-1"), _action("Assign"), ("confirm", False), BACK])
    assert fakes.call("ask_person")[2] == {"project": "PROJ", "allow_none": True}
    assert "assignee alice -> bob" in session.io.text
    assert "cancelled; nothing was written" in session.io.text
    assert "assign" not in fakes.names()


def test_assign_confirmed_assigns_the_picked_user(fakes):
    _run([("ask", "PROJ-1"), _action("Assign"), ("confirm", True), BACK])
    assert fakes.call("assign")[1] == ("PROJ-1", "bob")


def test_assign_to_nobody_previews_unassigned(fakes):
    fakes.person = None
    session = _run([("ask", "PROJ-1"), _action("Assign"), ("confirm", False), BACK])
    assert "assignee alice -> (unassigned)" in session.io.text


def test_transition_lists_numbered_targets_and_sends_the_transition_id(fakes):
    session = _run([("ask", "PROJ-1"), _action("Move"), ("choose", 0), ("ask", ""), ("confirm", True), BACK])
    options = session.io.options("Move PROJ-1")
    assert options[0] == "Start -> In Progress"
    assert "stays in current status" in options[1]
    assert fakes.call("transition")[1] == ("PROJ-1", "11", None)


def test_worklog_re_asks_bad_durations_and_dates_before_the_preview(fakes):
    session = _run(
        [
            ("ask", "PROJ-1"),
            _action("Log work"),
            ("ask", "banana"),
            ("ask", "-3h"),
            ("ask", "2h 30m"),
            ("ask", "2026-13-45"),
            ("ask", "2020-01-02"),
            ("ask", ""),
            ("confirm", False),
            BACK,
        ]
    )
    assert session.io.text.count("error: cannot read duration") == 2
    assert "not a real date" in session.io.text
    assert "time    2h 30m" in session.io.text
    assert "started 2020-01-02" in session.io.text
    assert "add_worklog" not in fakes.names()


def test_estimate_rejects_garbage_and_sends_normalised_values(fakes):
    _run(
        [
            ("ask", "PROJ-1"),
            _action("Set estimate"),
            ("ask", "xyz"),
            ("ask", "90m"),
            ("ask", ""),
            ("confirm", True),
            BACK,
        ]
    )
    assert fakes.call("set_estimate")[1] == ("PROJ-1", "1h 30m", None)


def test_estimate_with_both_blank_changes_nothing(fakes):
    session = _run([("ask", "PROJ-1"), _action("Set estimate"), ("ask", ""), ("ask", ""), BACK])
    assert "nothing to change" in session.io.text


def test_edit_priority_uses_the_priority_picker_not_free_text(fakes):
    session = _run(
        [("ask", "PROJ-1"), _action("Edit"), ("choose", 2), ("choose", 1), ("confirm", True), BACK]
    )
    assert session.io.options("Priority") == ["(server default)", "High", "Medium", "Low"]
    assert "priority: Medium -> High" in session.io.text
    assert fakes.call("update_fields")[1] == ("PROJ-1", {"priority": "High"})


def test_edit_story_points_rejects_nan_then_accepts_a_number(fakes):
    session = _run(
        [
            ("ask", "PROJ-1"),
            _action("Edit"),
            ("choose", 5),
            ("ask", "nan"),
            ("ask", "5"),
            ("confirm", True),
            BACK,
        ]
    )
    assert "finite number" in session.io.text
    assert fakes.call("update_fields")[1] == ("PROJ-1", {"story_points": 5.0})


def test_edit_due_date_rejects_words_and_accepts_a_date(fakes):
    _run(
        [
            ("ask", "PROJ-1"),
            _action("Edit"),
            ("choose", 3),
            ("ask", "next tuesday"),
            ("ask", "2030-01-31"),
            ("confirm", True),
            BACK,
        ]
    )
    assert fakes.call("update_fields")[1] == ("PROJ-1", {"due_date": "2030-01-31"})


def test_labels_add_keeps_existing_labels(fakes):
    fakes.issues["PROJ-1"]["labels"] = ["a"]
    session = _run(
        [("ask", "PROJ-1"), _action("Labels"), ("choose", 0), ("ask", "b, a"), ("confirm", True), BACK]
    )
    assert "labels: [a] -> [a, b]" in session.io.text
    assert fakes.call("update_fields")[1] == ("PROJ-1", {"labels": ["a", "b"]})


def test_labels_remove_by_number(fakes):
    fakes.issues["PROJ-1"]["labels"] = ["a", "b", "c"]
    _run(
        [
            ("ask", "PROJ-1"),
            _action("Labels"),
            ("choose", 1),
            ("ask", "9"),
            ("ask", "1,3"),
            ("confirm", True),
            BACK,
        ]
    )
    assert fakes.call("update_fields")[1] == ("PROJ-1", {"labels": ["b"]})


def test_labels_replace_rejects_spaces_and_dash_clears(fakes):
    fakes.issues["PROJ-1"]["labels"] = ["a"]
    session = _run(
        [
            ("ask", "PROJ-1"),
            _action("Labels"),
            ("choose", 2),
            ("ask", "two words"),
            ("ask", "-"),
            ("confirm", False),
            BACK,
        ]
    )
    assert "must not contain spaces" in session.io.text
    assert "labels: [a] -> (cleared)" in session.io.text


def test_link_re_asks_self_garbage_and_missing_targets(fakes):
    session = _run(
        [
            ("ask", "PROJ-1"),
            _action("Link"),
            ("choose", 0),
            ("ask", "PROJ-1"),
            ("ask", "foo bar"),
            ("ask", "PROJ-404"),
            ("ask", "proj-2"),
            ("confirm", True),
            BACK,
        ]
    )
    assert session.io.options("Link type")[0] == "Blocks  (blocks / is blocked by)"
    assert "cannot be linked to itself" in session.io.text
    assert "invalid issue key" in session.io.text
    assert "PROJ-404 does not exist" in session.io.text
    assert "PROJ-1 blocks PROJ-2" in session.io.text
    assert fakes.call("link_issues")[1] == ("PROJ-1", "PROJ-2", "Blocks")


def test_sprint_action_is_hidden_on_kanban_boards(fakes):
    session = _run([("ask", "PROJ-1"), BACK])
    assert not any("Sprint" in option for option in session.io.options("Actions for"))


def test_sprint_action_is_hidden_when_the_board_cannot_be_read(fakes):
    fakes.board_error = JiraError("no permission", status=400)
    session = _run([("ask", "PROJ-1"), BACK])
    assert not any("Sprint" in option for option in session.io.options("Actions for"))


def test_scrum_board_offers_numbered_sprint_add(fakes):
    fakes.board = {"id": 9, "name": "Scrum board", "type": "scrum"}
    session = _run(
        [("ask", "PROJ-1"), _action("Sprint"), ("choose", 0), ("choose", 1), ("confirm", True), BACK]
    )
    assert session.io.options("Pick a sprint") == ["Sprint 11 [active]", "Sprint 12 [future]"]
    assert fakes.call("list_sprints")[1] == (9, "active,future")
    assert fakes.call("add_to_sprint")[1] == (12, ["PROJ-1"])


def test_scrum_board_sprint_remove_moves_to_backlog(fakes):
    fakes.board = {"id": 9, "name": "Scrum board", "type": "scrum"}
    fakes.issues["PROJ-1"]["sprint"] = "Sprint 11"
    session = _run([("ask", "PROJ-1"), _action("Sprint"), ("choose", 1), ("confirm", True), BACK])
    assert "sprint: Sprint 11 -> (backlog)" in session.io.text
    assert fakes.call("move_to_backlog")[1] == (["PROJ-1"],)


def test_back_inside_an_action_returns_to_the_action_list(fakes):
    session = _run([("ask", "PROJ-1"), _action("Comment"), ("ask", "b"), BACK])
    titles = [e[1] for e in session.io.asked if e[0] == "choose"]
    assert len(titles) == 2 and all(t.startswith("Actions for PROJ-1") for t in titles)
    assert "add_comment" not in fakes.names()


def test_server_errors_inside_an_action_bubble_to_the_menu_runner(fakes, monkeypatch):
    def boom(*args, **kwargs):
        raise JiraError("server exploded", status=500)

    monkeypatch.setattr(sys.modules["jira_cli.services.issues"], "list_transitions", boom)
    session = _run([("ask", "PROJ-1"), _action("Move")])
    assert "error: server exploded" in session.io.text


def test_actions_for_records_the_key_as_recent(fakes):
    session = _session([BACK])
    menu_actions.actions_for(session, "PROJ-2")
    assert menu_actions.recent_keys(session) == ["PROJ-2"]


def test_scrum_sprint_add_to_the_current_sprint_changes_nothing(fakes):
    fakes.board = {"id": 9, "name": "Scrum board", "type": "scrum"}
    fakes.issues["PROJ-1"]["sprint"] = "Sprint 11"
    session = _run([("ask", "PROJ-1"), _action("Sprint"), ("choose", 0), ("choose", 0), BACK])
    assert "already in Sprint 11" in session.io.text
    assert "add_to_sprint" not in fakes.names()
