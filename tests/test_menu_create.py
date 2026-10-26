"""Tests for the guided create section (``jira_cli.cli.menu_create``).

Fully offline: a scripted IO replaces the terminal and every service module is
a fake installed in ``sys.modules``, exactly how ``menu._svc`` resolves them.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import date

import pytest

from jira_cli.cli import menu, menu_create
from jira_cli.errors import InputError, JiraError, NotFoundError

TODAY = date(2026, 10, 26)


class ScriptedIO:
    """Answers come from a list of ``(kind, value)``; every prompt is recorded."""

    def __init__(self, script):
        self.script = list(script)
        self.shown: list[str] = []
        self.asked: list[tuple] = []

    def _answer(self, kind):
        if not self.script:
            raise AssertionError(f"script exhausted; flow asked for a {kind}: {self.asked[-1]}")
        expected, value = self.script.pop(0)
        assert expected == kind, f"script expected {expected}, flow asked {kind}: {self.asked[-1]}"
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

    def defaults_for(self, fragment):
        return [entry[2] for entry in self.asked if entry[0] == "ask" and fragment in entry[1]]


@dataclass
class FakeIssueSpec:
    project: str
    summary: str
    issue_type: str = "Task"
    description: str | None = None
    assignee: str | None = None
    priority: str | None = None
    labels: list[str] = field(default_factory=list)
    due_date: str | None = None
    original_estimate: str | None = None
    epic_key: str | None = None
    parent_key: str | None = None
    components: list[str] = field(default_factory=list)
    sprint_id: int | None = None


@dataclass
class FakeSettings:
    base_url: str = "https://jira.example.com"
    default_project: str = "PROJ"


ISSUES_BY_KEY = {
    "PROJ-2": {"key": "PROJ-2", "type": "Task"},
    "PROJ-3": {"key": "PROJ-3", "type": "Epic"},
    "PROJ-5": {"key": "PROJ-5", "type": "Story"},
    "PROJ-6": {"key": "PROJ-6", "type": "Sub-task"},
}


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class World:
    """Fake Jira state the services read; tests tweak it before running."""

    def __init__(self):
        self.board = {"id": 37, "name": "PROJ board", "type": "kanban", "project": "PROJ"}
        self.sprints = [{"id": 7, "name": "Sprint 7", "state": "active"}]
        self.sprint_error: Exception | None = None
        self.components = ["api", "ui"]
        self.created: list[FakeIssueSpec] = []
        self.sprint_calls = 0


@pytest.fixture
def world(monkeypatch):
    w = World()

    def list_sprints(client, board_id, state="active,future"):
        w.sprint_calls += 1
        if w.sprint_error:
            raise w.sprint_error
        return w.sprints

    def get_issue(client, key, comments=True):
        if key not in ISSUES_BY_KEY:
            raise NotFoundError("Issue Does Not Exist", status=404)
        return ISSUES_BY_KEY[key]

    def create_issue(client, spec):
        w.created.append(spec)
        return {"key": "PROJ-99", "url": "https://jira.example.com/browse/PROJ-99"}

    def ask_person(session, title, *, project=None, allow_none=False, **_kw):
        answer = session.io.ask(f"{title} (person)", None)
        if answer == "b":
            raise menu._Back
        return answer or None

    issue_types = [
        {"name": "Epic", "subtask": False},
        {"name": "Task", "subtask": False},
        {"name": "Sub-task", "subtask": True},
    ]
    fakes = {
        "jira_cli.services.projects": _module(
            "projects",
            list_projects=lambda client: [
                {"key": "OTHER", "name": "Other"},
                {"key": "PROJ", "name": "Project"},
            ],
            list_issue_types=lambda client, key: [t["name"] for t in issue_types],
            get_project=lambda client, key: {"key": key, "components": w.components},
            list_priorities=lambda client: ["Highest", "Medium"],
        ),
        "jira_cli.services.issues": _module(
            "issues",
            createmeta_issue_types=lambda client, key: issue_types,
            get_issue=get_issue,
            IssueSpec=FakeIssueSpec,
            create_issue=create_issue,
        ),
        "jira_cli.services.boards": _module(
            "boards",
            board_for_project=lambda client, key: w.board,
            list_sprints=list_sprints,
        ),
        "jira_cli.cli.menu_people": _module("menu_people", ask_person=ask_person),
    }
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(menu_create, "_today", lambda: TODAY)
    return w


def answers(**overrides):
    """A full walk to the confirmation; a list value means several answers (re-asks)."""
    order = {
        "project": ("choose", 0),  # PROJ is the default project, sorted first
        "type": "",  # Enter = Task
        "summary": "Fix login",
        "description": "Line one\nline two",
        "assignee": "alice",
        "priority": "",  # Enter = server default
        "estimate": "3h",
        "due": "",
        "labels": "",
        "components": "",
        "parent": "",
        "epic": "",
        "sprint": None,  # omitted: kanban board asks nothing
        "confirm": "n",
    }
    order.update(overrides)
    script = []
    for value in order.values():
        if value is None:
            continue
        if isinstance(value, tuple):
            script.append(value)
            continue
        for item in value if isinstance(value, list) else [value]:
            script.append(("ask", item))
    return script


def _run(script):
    io = ScriptedIO(script)
    session = menu._Session(client=object(), settings=FakeSettings(), io=io)
    menu_create.run(session)
    assert not io.script, f"unused answers: {io.script}"
    return io, session


def _preview(io):
    return next(block for block in io.shown if block.startswith("Create issue"))


# ------------------------------------------------------------ B1: kanban sprint


def test_kanban_board_skips_sprint_step_and_reaches_preview(world):
    io, _ = _run(answers())

    assert world.sprint_calls == 0
    assert "kanban; no sprint" in io.text
    assert "sprint       -" in _preview(io)
    assert "cancelled; nothing was written" in io.text
    assert world.created == []


def test_sprint_lookup_error_means_no_sprint_not_abort(world):
    world.board["type"] = "scrum"
    world.sprint_error = JiraError("The board doesn't support sprints.", status=400)

    io, _ = _run(answers())

    assert "could not list sprints" in io.text
    assert "sprint       -" in _preview(io)


def test_board_lookup_error_means_no_sprint(world, monkeypatch):
    def broken(client, key):
        raise JiraError("You must have the browse project permission", status=400)

    monkeypatch.setattr(sys.modules["jira_cli.services.boards"], "board_for_project", broken)
    io, _ = _run(answers())

    assert "could not look up a board" in io.text
    assert "cancelled; nothing was written" in io.text


def test_scrum_board_enter_at_sprint_means_no_sprint(world):
    world.board["type"] = "scrum"

    io, _ = _run(answers(sprint="", confirm="y"))

    assert world.created[0].sprint_id is None


def test_scrum_board_sprint_can_be_picked(world):
    world.board["type"] = "scrum"

    _run(answers(sprint="2", confirm="y"))

    assert world.created[0].sprint_id == 7


# --------------------------------------------------- validation at the prompt


def test_yes_creates_with_normalised_values(world):
    io, session = _run(
        answers(
            estimate="1d 2h",
            due="7d",
            labels="backend, urgent",
            components="2",
            epic="proj-3",
            priority="2",
            confirm="y",
        )
    )

    spec = world.created[0]
    assert spec.due_date == "2026-11-02"
    assert spec.original_estimate == "1d 2h"
    assert spec.labels == ["backend", "urgent"]
    assert spec.components == ["ui"]
    assert spec.epic_key == "PROJ-3"
    assert spec.priority == "Highest"
    assert spec.assignee == "alice"
    assert spec.issue_type == "Task"
    assert session.last_key == "PROJ-99"
    assert "created PROJ-99" in io.text


def test_bad_values_are_reasked_until_valid(world):
    io, _ = _run(
        answers(
            estimate=["abc", "30m"],
            due=["next tuesday", "2026-02-30", "2w"],
            labels=["a b", "a,b"],
            components=["9", "1"],
            epic=["not a key", "PROJ-404", "PROJ-2", "PROJ-3"],
            confirm="y",
        )
    )

    spec = world.created[0]
    assert (spec.original_estimate, spec.due_date) == ("30m", "2026-11-09")
    assert (spec.labels, spec.components, spec.epic_key) == (["a", "b"], ["api"], "PROJ-3")
    assert "PROJ-404 does not exist" in io.text
    assert "PROJ-2 is a Task, not an Epic" in io.text
    assert "not a real date" in io.text


def test_preview_shows_every_field_with_description_first_line(world):
    io, _ = _run(answers(due="2026-12-01", labels="x"))

    preview = _preview(io)
    for expected in (
        "project      PROJ",
        "board        PROJ board (id 37, kanban)",
        "type         Task",
        "summary      Fix login",
        "description  Line one  (+1 more lines)",
        "assignee     alice",
        "priority     (server default)",
        "estimate     3h",
        "due          2026-12-01",
        "labels       x",
        "components   -",
        "parent       -",
        "epic         -",
    ):
        assert expected in preview


# ---------------------------------------------------------- B8: parent => sub-task


def test_parent_turns_preview_type_into_subtask(world):
    io, _ = _run(answers(parent="PROJ-5", epic=None, confirm="y"))

    assert "type         Sub-task" in _preview(io)
    assert world.created[0].issue_type == "Sub-task"
    assert world.created[0].parent_key == "PROJ-5"


def test_parent_must_exist_be_in_project_and_not_a_subtask(world):
    io, _ = _run(answers(parent=["OTHER-1", "PROJ-404", "PROJ-6", "PROJ-5"], epic=None))

    assert "must be in PROJ" in io.text
    assert "PROJ-404 does not exist" in io.text
    assert "PROJ-6 is itself a sub-task" in io.text


def test_subtask_type_requires_a_parent(world):
    io, _ = _run(answers(type="3", parent=["", "PROJ-5"], epic=None, confirm="y"))

    assert "needs a parent" in io.text
    assert world.created[0].issue_type == "Sub-task"


# ------------------------------------------------------- B9 and one-step back


def test_enter_at_pickers_takes_the_default(world):
    _run(answers(type="", assignee="", priority="", confirm="y"))

    spec = world.created[0]
    assert (spec.issue_type, spec.assignee, spec.priority) == ("Task", None, None)


def test_back_goes_one_step_and_keeps_every_answer(world):
    script = answers(
        due=["b", "", "5d"],  # b at Due -> Estimate again; Enter keeps 3h; then Due
        confirm="y",
    )
    io, _ = _run(script)

    assert io.defaults_for("Original estimate") == [None, "3h"]
    spec = world.created[0]
    assert (spec.summary, spec.original_estimate, spec.due_date) == ("Fix login", "3h", "2026-10-31")


def test_back_at_confirm_reopens_last_step_and_dash_clears_it(world):
    # b at the confirmation re-asks Epic (the kanban sprint step asked nothing),
    # offering the previous answer; "-" clears it.
    io, _ = _run(answers(epic="PROJ-3", confirm=["b", "-", "y"]))

    assert io.defaults_for("Epic key") == [None, "PROJ-3"]
    assert world.created[0].epic_key is None


def test_back_at_first_step_leaves_the_section(world):
    io = ScriptedIO([("choose", None)])
    session = menu._Session(client=object(), settings=FakeSettings(), io=io)

    with pytest.raises(menu._Back):
        menu_create.run(session)


def test_no_and_eof_at_confirm_write_nothing(world):
    io, _ = _run(answers(confirm=""))

    assert "cancelled; nothing was written" in io.text
    assert world.created == []


# ------------------------------------------------------------------- parsers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7d", "2026-11-02"),
        ("2w", "2026-11-09"),
        ("1m", "2026-11-25"),
        ("tomorrow", "2026-10-27"),
        ("2026-12-01", "2026-12-01"),
    ],
)
def test_parse_due_resolves_forward(text, expected):
    assert menu_create.parse_due(text, TODAY) == expected


@pytest.mark.parametrize("text", ["-3d", "next tuesday", "2026-13-01", "7"])
def test_parse_due_rejects_unreadable(text):
    with pytest.raises(InputError):
        menu_create.parse_due(text, TODAY)
