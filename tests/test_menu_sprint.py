"""Tests for the sprint report / velocity menu section (``jira_cli.cli.menu_sprint``).

Fully offline: a scripted IO replaces the terminal, and the boards, sprint_report
and output modules plus the shared people menu are fakes in ``sys.modules``.
"""

from __future__ import annotations

import sys
import types

import pytest

from jira_cli.cli import menu, menu_sprint

# ----------------------------------------------------------------- scripted IO


class ScriptedIO:
    """A ``menu.MenuIO`` whose answers come from a script of ``(kind, value)``."""

    def __init__(self, script):
        self.script = list(script)
        self.shown: list[str] = []
        self.asked: list[tuple] = []

    def _answer(self, kind: str):
        if not self.script:
            raise AssertionError(f"script exhausted; menu asked for a {kind}: {self.asked[-1]}")
        expected_kind, value = self.script.pop(0)
        if expected_kind != kind:
            raise AssertionError(f"script expected a {expected_kind}, menu asked for a {kind}")
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

    def titles(self) -> list[str]:
        return [entry[1] for entry in self.asked if entry[0] == "choose"]

    def prompts(self) -> list[tuple]:
        return [entry for entry in self.asked if entry[0] == "ask"]


# ------------------------------------------------------------------------ fakes

PROJ_BOARDS = [
    {"id": 37, "name": "PROJ kanban", "type": "kanban", "project": "PROJ"},
    {"id": 50, "name": "PROJ scrum", "type": "scrum", "project": "PROJ"},
]
SCRUM_BOARDS = [
    {"id": 50, "name": "PROJ scrum", "type": "scrum", "project": None},
    {"id": 51, "name": "OTHER scrum", "type": "scrum", "project": None},
]


def _sprint(sprint_id, state, start=None, end=None):
    return {
        "id": sprint_id,
        "name": f"Sprint {sprint_id}",
        "state": state,
        "start": start,
        "end": end,
        "goal": None,
    }


ONE_ACTIVE = [
    _sprint(3, "closed", "2026-09-01T09:00:00.000+03:30", "2026-09-14T17:00:00.000+03:30"),
    _sprint(16, "active", "2026-10-20T09:00:00.000+03:30", "2026-11-03T17:00:00.000+03:30"),
    _sprint(17, "future"),
]


class Fakes:
    """Recording fake services; tests adjust the return values they care about."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.project_boards = list(PROJ_BOARDS)
        self.scrum_boards = list(SCRUM_BOARDS)
        self.sprints = list(ONE_ACTIVE)
        self.boards_by_id = {b["id"]: b for b in PROJ_BOARDS + SCRUM_BOARDS}

    def list_boards(self, client, project_key=None, board_type=None):
        self.calls.append(("list_boards", project_key, board_type))
        return self.scrum_boards if board_type == "scrum" else self.project_boards

    def get_board(self, client, board_id):
        self.calls.append(("get_board", board_id))
        return dict(self.boards_by_id[board_id])

    def list_sprints(self, client, board_id, state="active,future"):
        self.calls.append(("list_sprints", board_id, state))
        return [dict(s) for s in self.sprints]

    def sprint_report(self, client, board_id, sprint_id=None):
        self.calls.append(("sprint_report", board_id, sprint_id))
        return {"title": "Sprint report", "columns": [], "rows": [], "summary": {}, "meta": {}}

    def velocity(self, client, board_id, last=5):
        self.calls.append(("velocity", board_id, last))
        return {"title": "Velocity", "columns": [], "rows": [], "summary": {}, "meta": {}}

    def ask_projects(self, session, title="Projects", *, allow_all=True):
        self.calls.append(("ask_projects", allow_all))
        answer = menu._ask(session.io, title, required=True)
        return [part.strip().upper() for part in answer.split(",") if part.strip()]

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    modules = {
        "jira_cli.services.boards": _module(
            "jira_cli.services.boards",
            list_boards=f.list_boards,
            get_board=f.get_board,
            list_sprints=f.list_sprints,
        ),
        "jira_cli.services.sprint_report": _module(
            "jira_cli.services.sprint_report",
            sprint_report=f.sprint_report,
            velocity=f.velocity,
        ),
        "jira_cli.cli.output": _module(
            "jira_cli.cli.output",
            render_report=lambda report, fmt: f"[report {report['title']}]",
        ),
        "jira_cli.cli.menu_people": _module("jira_cli.cli.menu_people", ask_projects=f.ask_projects),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return f


def _session(script, last_board=None):
    return menu._Session(client=object(), settings=None, io=ScriptedIO(script), last_board=last_board)


# Script fragments.  ``choose`` answers are 0-based indexes.
SPRINT_REPORT = ("choose", 0)
VELOCITY = ("choose", 1)
FROM_PROJECT = ("choose", 0)
ALL_SCRUM = ("choose", 1)
LAST_BOARD = ("choose", 2)
LEAVE_REPORT = ("ask", "b")


# ------------------------------------------------------------- sprint report


def test_enter_at_sprint_prompt_reports_the_active_sprint(fakes):
    session = _session(
        [SPRINT_REPORT, FROM_PROJECT, ("ask", "PROJ"), ("choose", 1), ("ask", ""), LEAVE_REPORT]
    )

    menu_sprint.run(session)

    assert fakes.named("sprint_report") == [("sprint_report", 50, 16)]
    assert session.last_board == 50


def test_project_picker_is_asked_for_a_single_project(fakes):
    session = _session(
        [SPRINT_REPORT, FROM_PROJECT, ("ask", "PROJ"), ("choose", 1), ("ask", ""), LEAVE_REPORT]
    )

    menu_sprint.run(session)

    assert fakes.named("ask_projects") == [("ask_projects", False)]
    assert ("list_boards", "PROJ", None) in fakes.calls


def test_board_list_shows_name_type_and_id(fakes):
    session = _session(
        [SPRINT_REPORT, FROM_PROJECT, ("ask", "PROJ"), ("choose", 1), ("ask", ""), LEAVE_REPORT]
    )

    menu_sprint.run(session)

    board_prompt = next(e for e in session.io.asked if e[0] == "choose" and e[1] == "Boards of PROJ")
    assert board_prompt[2] == ["PROJ kanban  [kanban]  (id 37)", "PROJ scrum  [scrum]  (id 50)"]


def test_two_projects_are_rejected_and_reasked(fakes):
    session = _session(
        [
            SPRINT_REPORT,
            FROM_PROJECT,
            ("ask", "PROJ, OTHER"),
            ("ask", "PROJ"),
            ("choose", 1),
            ("ask", ""),
            LEAVE_REPORT,
        ]
    )

    menu_sprint.run(session)

    assert "pick exactly one project (b to go back)" in session.io.text
    assert fakes.named("sprint_report") == [("sprint_report", 50, 16)]


def test_kanban_board_explains_and_returns_to_the_board_list(fakes):
    session = _session(
        [
            SPRINT_REPORT,
            FROM_PROJECT,
            ("ask", "PROJ"),
            ("choose", 0),  # kanban board 37
            ("choose", 1),  # scrum board 50
            ("ask", ""),
            LEAVE_REPORT,
        ]
    )

    menu_sprint.run(session)

    assert "board PROJ kanban (id 37) is kanban: no sprints; use Boards overview instead" in (session.io.text)
    assert session.io.titles().count("Boards of PROJ") == 2
    assert ("list_sprints", 37, "") not in fakes.calls
    assert fakes.named("sprint_report") == [("sprint_report", 50, 16)]


def test_board_without_a_type_is_checked_with_get_board(fakes):
    fakes.project_boards = [{"id": 37, "name": "PROJ kanban", "type": None, "project": "PROJ"}]
    # pick the untyped board, then back out: board list -> project -> source -> kind
    session = _session(
        [
            SPRINT_REPORT,
            FROM_PROJECT,
            ("ask", "PROJ"),
            ("choose", 0),
            ("choose", None),
            ("ask", "b"),
            ("choose", None),
            ("choose", None),
        ]
    )

    with pytest.raises(menu._Back):
        menu_sprint.run(session)

    assert ("get_board", 37) in fakes.calls
    assert "is kanban: no sprints" in session.io.text


def test_all_scrum_boards_lists_only_scrum_boards(fakes):
    session = _session([SPRINT_REPORT, ALL_SCRUM, ("choose", 1), ("ask", ""), LEAVE_REPORT])

    menu_sprint.run(session)

    assert fakes.named("list_boards") == [("list_boards", None, "scrum")]
    assert fakes.named("sprint_report") == [("sprint_report", 51, 16)]


def test_last_board_option_appears_only_after_a_board_was_used(fakes):
    session = _session([SPRINT_REPORT, LAST_BOARD, ("ask", ""), LEAVE_REPORT], last_board=50)

    menu_sprint.run(session)

    assert session.io.asked[1][2][-1] == "Last board used (id 50)"
    assert fakes.named("sprint_report") == [("sprint_report", 50, 16)]


def test_first_visit_offers_no_last_board_option(fakes):
    session = _session([SPRINT_REPORT, ALL_SCRUM, ("choose", 0), ("ask", ""), LEAVE_REPORT])

    menu_sprint.run(session)

    assert session.io.asked[1][2] == ["Boards of one project", "All scrum boards"]


def test_sprints_are_listed_active_then_future_then_ten_newest_closed(fakes):
    closed = [_sprint(i, "closed") for i in range(1, 13)]
    fakes.sprints = [*closed[:6], _sprint(20, "future"), _sprint(16, "active"), *closed[6:]]
    session = _session([SPRINT_REPORT, ALL_SCRUM, ("choose", 0), ("ask", ""), LEAVE_REPORT])

    menu_sprint.run(session)

    listing = next(text for text in session.io.shown if text.startswith("Sprints on"))
    ids = [line.rsplit("(id ", 1)[1].rstrip(")") for line in listing.splitlines()[1:]]
    assert ids == ["16", "20", "12", "11", "10", "9", "8", "7", "6", "5", "4", "3"]
    assert ("list_sprints", 50, "") in fakes.calls


def test_sprint_line_shows_state_and_dates(fakes):
    session = _session([SPRINT_REPORT, ALL_SCRUM, ("choose", 0), ("ask", ""), LEAVE_REPORT])

    menu_sprint.run(session)

    assert "   1. Sprint 16  [active]  2026-10-20 -> 2026-11-03  (id 16)" in session.io.text
    assert "   2. Sprint 17  [future]  (id 17)" in session.io.text


def test_sprint_prompt_defaults_to_the_active_sprint_number(fakes):
    session = _session([SPRINT_REPORT, ALL_SCRUM, ("choose", 0), ("ask", ""), LEAVE_REPORT])

    menu_sprint.run(session)

    assert ("ask", "Sprint number (Enter = active sprint)", "1") in session.io.asked


def test_several_active_sprints_have_no_default_and_must_be_chosen(fakes):
    fakes.sprints = [_sprint(6, "active"), _sprint(15, "active")]
    session = _session([SPRINT_REPORT, ALL_SCRUM, ("choose", 0), ("ask", ""), ("ask", "2"), LEAVE_REPORT])

    menu_sprint.run(session)

    assert "2 sprints are active; choose one" in session.io.text
    assert ("ask", "Sprint number", None) in session.io.asked
    assert "a value is required (b to go back)" in session.io.text
    assert fakes.named("sprint_report") == [("sprint_report", 50, 15)]


@pytest.mark.parametrize("bad", ["abc", "0", "4", "-1", "²"])
def test_bad_sprint_number_is_reasked_not_replaced(fakes, bad):
    session = _session([SPRINT_REPORT, ALL_SCRUM, ("choose", 0), ("ask", bad), ("ask", "2"), LEAVE_REPORT])

    menu_sprint.run(session)

    assert "choice must be a whole number from 1 to 3 (b to go back)" in session.io.text
    assert fakes.named("sprint_report") == [("sprint_report", 50, 17)]


def test_board_without_sprints_says_so_and_returns_to_the_board_list(fakes):
    fakes.sprints = []
    session = _session(
        [SPRINT_REPORT, ALL_SCRUM, ("choose", 0), ("choose", None), ("choose", None), ("choose", None)]
    )

    with pytest.raises(menu._Back):
        menu_sprint.run(session)

    assert "board PROJ scrum (id 50) has no sprints yet" in session.io.text
    assert session.io.titles().count("Scrum boards") == 2
    assert fakes.named("sprint_report") == []


# ------------------------------------------------------------------ velocity


def test_velocity_uses_five_sprints_on_enter(fakes):
    session = _session([VELOCITY, ALL_SCRUM, ("choose", 0), ("ask", ""), LEAVE_REPORT])

    menu_sprint.run(session)

    assert fakes.named("velocity") == [("velocity", 50, 5)]
    assert ("ask", "How many sprints (1-20)", "5") in session.io.asked


@pytest.mark.parametrize("bad", ["abc", "0", "21", "-3", "1.5"])
def test_velocity_count_outside_1_to_20_is_reasked(fakes, bad):
    session = _session([VELOCITY, ALL_SCRUM, ("choose", 0), ("ask", bad), ("ask", "20"), LEAVE_REPORT])

    menu_sprint.run(session)

    assert "how many sprints must be a whole number from 1 to 20 (b to go back)" in session.io.text
    assert fakes.named("velocity") == [("velocity", 50, 20)]


def test_velocity_on_kanban_board_explains_and_returns_to_the_board_list(fakes):
    session = _session(
        [VELOCITY, FROM_PROJECT, ("ask", "PROJ"), ("choose", 0), ("choose", 1), ("ask", "3"), LEAVE_REPORT]
    )

    menu_sprint.run(session)

    assert "is kanban: no sprints; use Boards overview instead" in session.io.text
    assert fakes.named("velocity") == [("velocity", 50, 3)]


def test_last_board_that_is_kanban_goes_back_to_the_source_choice(fakes):
    session = _session(
        [VELOCITY, LAST_BOARD, ALL_SCRUM, ("choose", 0), ("ask", "2"), LEAVE_REPORT], last_board=37
    )

    menu_sprint.run(session)

    assert "is kanban: no sprints" in session.io.text
    assert fakes.named("velocity") == [("velocity", 50, 2)]


# --------------------------------------------------------------- going back


def test_b_at_sprint_prompt_returns_to_the_board_list(fakes):
    session = _session(
        [SPRINT_REPORT, ALL_SCRUM, ("choose", 0), ("ask", "b"), ("choose", 1), ("ask", ""), LEAVE_REPORT]
    )

    menu_sprint.run(session)

    assert session.io.titles().count("Scrum boards") == 2
    assert fakes.named("sprint_report") == [("sprint_report", 51, 16)]


def test_b_at_count_prompt_returns_to_the_board_list(fakes):
    session = _session(
        [VELOCITY, ALL_SCRUM, ("choose", 0), ("ask", "b"), ("choose", 1), ("ask", "4"), LEAVE_REPORT]
    )

    menu_sprint.run(session)

    assert fakes.named("velocity") == [("velocity", 51, 4)]


def test_b_at_board_list_returns_to_the_project_prompt(fakes):
    session = _session(
        [
            SPRINT_REPORT,
            FROM_PROJECT,
            ("ask", "PROJ"),
            ("choose", None),
            ("ask", "PROJ"),
            ("choose", 1),
            ("ask", ""),
            LEAVE_REPORT,
        ]
    )

    menu_sprint.run(session)

    assert len(fakes.named("ask_projects")) == 2


def test_b_at_project_prompt_returns_to_the_source_choice(fakes):
    session = _session(
        [SPRINT_REPORT, FROM_PROJECT, ("ask", "b"), ALL_SCRUM, ("choose", 0), ("ask", ""), LEAVE_REPORT]
    )

    menu_sprint.run(session)

    assert session.io.titles().count("Which board?") == 2


def test_b_at_source_returns_to_report_kind_and_b_there_leaves(fakes):
    session = _session([SPRINT_REPORT, ("choose", None), ("choose", None)])

    with pytest.raises(menu._Back):
        menu_sprint.run(session)

    assert session.io.titles() == [
        "Sprint report and velocity",
        "Which board?",
        "Sprint report and velocity",
    ]
    assert fakes.named("sprint_report") == []


# ----------------------------------------------------------- pure ordering


def test_order_sprints_drops_rows_without_id():
    rows = [{"id": None, "state": "active"}, _sprint(2, "active")]

    assert [s["id"] for s in menu_sprint.order_sprints(rows)] == [2]


def test_project_picker_falls_back_to_pick_project_without_menu_people(fakes, monkeypatch):
    monkeypatch.setitem(sys.modules, "jira_cli.cli.menu_people", None)
    monkeypatch.setitem(
        sys.modules,
        "jira_cli.services.projects",
        _module("jira_cli.services.projects", list_projects=lambda client: [{"key": "PROJ", "name": "P"}]),
    )
    session = _session([SPRINT_REPORT, FROM_PROJECT, ("choose", 0), ("choose", 1), ("ask", ""), LEAVE_REPORT])

    menu_sprint.run(session)

    assert fakes.named("sprint_report") == [("sprint_report", 50, 16)]
