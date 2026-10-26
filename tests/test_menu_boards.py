"""Tests for the projects-and-boards menu section (``jira_cli.cli.menu_boards``).

Offline: a scripted IO replaces the terminal; the board-overview, boards and
projects services and the shared people pickers are fakes installed in
``sys.modules``.  The board renderers (``cli.board_view``) are real, so the
assertions read what the operator would actually see.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from jira_cli.cli import menu, menu_boards
from jira_cli.errors import NotFoundError


class ScriptedIO:
    """A ``menu.MenuIO`` whose answers come from a script of ``(kind, value)``."""

    def __init__(self, script):
        self.script = list(script)
        self.shown: list[str] = []
        self.asked: list[tuple] = []

    def _answer(self, kind):
        if not self.script:
            raise AssertionError(f"script exhausted; menu asked for a {kind}: {self.asked[-1]}")
        expected, value = self.script.pop(0)
        if expected != kind:
            raise AssertionError(f"script expected a {expected}, menu asked for a {kind}: {self.asked[-1]}")
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


@dataclass
class FakeSettings:
    base_url: str = "https://jira.example.com"
    default_project: str = "PROJ"


def _issue(key, summary, status, assignee, reporter="Carol Reporter"):
    username = assignee.split()[0].lower() if assignee else None
    return {
        "key": key,
        "summary": summary,
        "status": status,
        "type": "Task",
        "priority": "Medium",
        "assignee": assignee,
        "assignee_username": username,
        "reporter": reporter,
        "reporter_username": reporter.split()[0].lower(),
        "updated": "2026-10-20T10:00:00.000+0000",
    }


def kanban_overview():
    """Kanban with the backlog disabled: 'Backlog' is a normal first column."""
    return {
        "board": {"id": 37, "name": "PROJ kanban", "type": "kanban", "project": "PROJ"},
        "sprints": [],
        "columns": [
            {
                "name": "Backlog",
                "statuses": ["Open"],
                "count": 2,
                "issues": [
                    _issue("PROJ-1", "Write onboarding guide", "Open", "Alice Able"),
                    _issue("PROJ-2", "Plan audit", "Open", None),
                ],
            },
            {
                "name": "Develop",
                "statuses": ["In Progress"],
                "count": 2,
                "issues": [
                    _issue("PROJ-3", "Fix login timeout", "In Progress", "Bob Baker"),
                    _issue("PROJ-4", "Add export button", "In Progress", "Alice Able"),
                ],
            },
            {"name": "Done", "statuses": ["Done"], "count": 0, "issues": []},
        ],
        "backlog": None,
        "unmapped": {"count": 0, "issues": []},
        "total": 4,
    }


def scrum_overview():
    """Scrum: columns hold the active sprint; the backlog is separate."""
    return {
        "board": {"id": 50, "name": "PROJ scrum", "type": "scrum", "project": "PROJ"},
        "sprints": [{"id": 5, "name": "Sprint 5", "state": "active", "end_date": "2026-11-01T10:00:00.000Z"}],
        "columns": [
            {
                "name": "To Do",
                "statuses": ["To Do"],
                "count": 1,
                "issues": [_issue("PROJ-10", "Sprint task", "To Do", "Bob Baker")],
            },
            {"name": "In Progress", "statuses": ["In Progress"], "count": 0, "issues": []},
            {"name": "Done", "statuses": ["Done"], "count": 0, "issues": []},
        ],
        "backlog": {"count": 1, "issues": [_issue("PROJ-11", "Groom later", "Open", "Alice Able")]},
        "unmapped": {"count": 0, "issues": []},
        "total": 2,
    }


SUMMARIES = [
    {
        "id": 37,
        "name": "PROJ kanban",
        "type": "kanban",
        "project": "PROJ",
        "total": 4,
        "backlog": None,
        "columns": [
            {"name": "Backlog", "count": 2},
            {"name": "Develop", "count": 2},
            {"name": "Done", "count": 0},
        ],
        "sprint": None,
        "error": None,
    },
    {
        "id": 50,
        "name": "PROJ scrum",
        "type": "scrum",
        "project": "PROJ",
        "total": 2,
        "backlog": 1,
        "columns": [
            {"name": "To Do", "count": 1},
            {"name": "In Progress", "count": 0},
            {"name": "Done", "count": 0},
        ],
        "sprint": "Sprint 5",
        "error": None,
    },
]


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def env(monkeypatch):
    """Install fakes; returns a namespace with ``calls`` and tunable answers."""
    state = types.SimpleNamespace(
        calls=[],
        projects_answer=["PROJ"],
        overviews={
            37: kanban_overview,
            50: scrum_overview,
        },
        scrum_boards=[{"id": 50, "name": "PROJ scrum", "type": "scrum", "project": "PROJ"}],
        existing_board=None,
        shown_at_summary_call=None,
        io=None,
    )

    def record(name, result):
        def fn(*args, **kwargs):
            state.calls.append((name, args, kwargs))
            return result(*args, **kwargs)

        return fn

    def summaries(client, project=None, **_):
        if state.io is not None:
            state.shown_at_summary_call = list(state.io.shown)
        return [row for row in SUMMARIES if project in (None, row["project"])]

    def overview(client, board_id, **_):
        if board_id not in state.overviews:
            raise NotFoundError(f"board {board_id} not found")
        return state.overviews[board_id]()

    fakes = {
        "jira_cli.services.board_overview": _module(
            "jira_cli.services.board_overview",
            list_board_summaries=record("list_board_summaries", summaries),
            board_overview=record("board_overview", overview),
        ),
        "jira_cli.services.boards": _module(
            "jira_cli.services.boards",
            list_boards=record("list_boards", lambda *a, **k: state.scrum_boards),
            board_for_project=record("board_for_project", lambda *a, **k: state.existing_board),
            ensure_board=record(
                "ensure_board", lambda *a, **k: {"board": {"id": 9, "name": "NEW"}, "created": True}
            ),
            create_sprint=record("create_sprint", lambda *a, **k: {"id": 6, "name": "Sprint 6"}),
        ),
        "jira_cli.services.projects": _module(
            "jira_cli.services.projects",
            list_projects=record(
                "list_projects",
                lambda *a, **k: [{"key": "PROJ", "name": "Project", "lead": "alice", "type": "software"}],
            ),
            create_project=record(
                "create_project", lambda *a, **k: {"key": "NEW", "url": "u", "warnings": []}
            ),
        ),
        "jira_cli.cli.menu_people": _module(
            "jira_cli.cli.menu_people",
            ask_projects=record("ask_projects", lambda *a, **k: state.projects_answer),
            ask_person=record("ask_person", lambda *a, **k: "bob"),
        ),
    }
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(menu_boards, "terminal_width", lambda: 120)
    return state


def _session(state, script):
    io = ScriptedIO(script)
    state.io = io
    return menu._Session(client=object(), settings=FakeSettings(), io=io)


def _called(state, name):
    return [call for call in state.calls if call[0] == name]


def _overview_until_back(session):
    with pytest.raises(menu._Back):
        menu_boards.run_overview(session)


class _ScopeBack:
    """Makes the fake ``ask_projects`` raise ``_Back`` on its N-th call (leave the overview)."""

    def __init__(self, state, answers):
        self.answers = list(answers)
        module = sys.modules["jira_cli.cli.menu_people"]
        original = module.ask_projects

        def ask_projects(*args, **kwargs):
            original(*args, **kwargs)
            if not self.answers:
                raise menu._Back
            return self.answers.pop(0)

        module.ask_projects = ask_projects


# ======================================================== boards overview


def test_board_list_shows_counts_per_board_after_a_loading_line(env):
    _ScopeBack(env, [["PROJ"]])
    session = _session(env, [("ask", "b")])

    _overview_until_back(session)

    assert any("loading" in line for line in env.shown_at_summary_call)
    assert "PROJ kanban" in session.io.text and "PROJ scrum" in session.io.text
    assert "Develop 2" in session.io.text
    assert "Sprint 5" in session.io.text
    assert _called(env, "list_board_summaries")[0][1][1:] == ("PROJ",)


def test_all_scope_lists_every_board_with_one_call(env):
    _ScopeBack(env, [None])
    session = _session(env, [("ask", "b")])

    _overview_until_back(session)

    calls = _called(env, "list_board_summaries")
    assert len(calls) == 1 and calls[0][1][1:] == ()


def test_several_projects_are_merged_without_duplicate_boards(env):
    _ScopeBack(env, [["PROJ", "PROJ"]])
    session = _session(env, [("ask", "b")])

    _overview_until_back(session)

    assert session.io.text.count("PROJ kanban") == 1


def test_kanban_board_shows_backlog_as_a_normal_column_with_title_assignee_reporter(env):
    _ScopeBack(env, [["PROJ"]])
    session = _session(env, [("ask", "1"), ("ask", "1"), ("ask", "b"), ("ask", "b")])

    _overview_until_back(session)

    text = session.io.text
    assert "PROJ kanban  ·  kanban  ·  project PROJ  ·  4 issues" in text
    assert "1. Backlog (2)" in text and "2. Develop (2)" in text and "5. All columns" not in text
    assert "4. All columns" in text
    assert "Write onboarding guide" in text and "Alice Able" in text and "Carol Reporter" in text
    assert "unassigned" in text
    assert "sprint:" not in text
    assert session.last_board == 37


def test_scrum_board_shows_active_sprint_and_separate_backlog(env):
    _ScopeBack(env, [["PROJ"]])
    session = _session(env, [("ask", "2"), ("ask", "1"), ("ask", "b"), ("ask", "b")])

    _overview_until_back(session)

    text = session.io.text
    assert "sprint: Sprint 5 (active, ends 2026-11-01)" in text
    assert "1. Backlog (1)" in text and "2. To Do (1)" in text
    assert "Groom later" in text
    assert "Sprint task" not in text  # only the picked column is listed


def test_all_columns_lists_every_section(env):
    _ScopeBack(env, [["PROJ"]])
    session = _session(env, [("ask", "2"), ("ask", "5"), ("ask", "b"), ("ask", "b")])

    _overview_until_back(session)

    assert "Groom later" in session.io.text and "Sprint task" in session.io.text


def test_bad_board_and_column_numbers_are_re_asked(env):
    _ScopeBack(env, [["PROJ"]])
    session = _session(
        env, [("ask", "9"), ("ask", "x"), ("ask", "1"), ("ask", "0"), ("ask", "b"), ("ask", "b")]
    )

    _overview_until_back(session)

    assert "type a board number from 1 to 2, r or b" in session.io.text
    assert "type a column number from 1 to 4, a, r or b" in session.io.text
    assert len(_called(env, "board_overview")) == 1


def test_refresh_reloads_board_and_board_list(env):
    _ScopeBack(env, [["PROJ"]])
    session = _session(env, [("ask", "r"), ("ask", "1"), ("ask", "r"), ("ask", "b"), ("ask", "b")])

    _overview_until_back(session)

    assert len(_called(env, "list_board_summaries")) == 2
    assert len(_called(env, "board_overview")) == 2


def test_back_from_board_list_returns_to_scope_question(env):
    _ScopeBack(env, [["PROJ"], None])
    session = _session(env, [("ask", "b"), ("ask", "b")])

    _overview_until_back(session)

    assert len(_called(env, "ask_projects")) == 3


def test_last_board_is_offered_as_default_board_number(env):
    _ScopeBack(env, [["PROJ"]])
    session = _session(env, [("ask", "b")])
    session.last_board = 50

    _overview_until_back(session)

    board_prompts = [entry for entry in session.io.asked if entry[0] == "ask" and "Board number" in entry[1]]
    assert board_prompts[0][2] == "2"


def test_assignee_filter_shows_only_that_persons_issues(env):
    _ScopeBack(env, [["PROJ"]])
    # board 1 -> a -> options: everyone, Alice (2), Bob (1), (unassigned) (1) -> pick Bob -> column 2
    session = _session(
        env, [("ask", "1"), ("ask", "a"), ("choose", 2), ("ask", "2"), ("ask", "b"), ("ask", "b")]
    )

    _overview_until_back(session)

    options = [entry for entry in session.io.asked if entry[0] == "choose"][0][2]
    assert options == ["(everyone - no filter)", "Alice Able (2)", "Bob Baker (1)", "(unassigned) (1)"]
    text = session.io.text
    assert "filter: assignee = Bob Baker" in text
    assert "2. Develop (1)" in text
    column_output = next(line for line in session.io.shown if line.startswith("Develop — "))
    assert "Fix login timeout" in column_output and "Add export button" not in column_output


def test_unreadable_board_shows_error_and_stays_on_board_list(env):
    env.overviews.pop(37)
    _ScopeBack(env, [["PROJ"]])
    session = _session(env, [("ask", "1"), ("ask", "b")])

    _overview_until_back(session)

    assert "error: board 37 not found" in session.io.text


# ================================================== projects and boards


def test_submenu_boards_overview_entry_runs_the_overview(env):
    _ScopeBack(env, [])
    session = _session(env, [("choose", 0), ("choose", None)])

    with pytest.raises(menu._Back):
        menu_boards.run(session)

    options = [entry for entry in session.io.asked if entry[0] == "choose"][0][2]
    assert options[0] == "Boards overview"
    assert len(_called(env, "ask_projects")) == 1


def test_list_projects_shows_lead_column(env):
    session = _session(env, [("choose", 1), ("choose", None)])

    with pytest.raises(menu._Back):
        menu_boards.run(session)

    assert "lead" in session.io.text and "alice" in session.io.text


def test_create_project_re_asks_bad_and_taken_keys_and_picks_lead(env):
    session = _session(
        env,
        [
            ("choose", 2),
            ("ask", "x"),
            ("ask", "proj"),
            ("ask", "new"),
            ("ask", "New project"),
            ("choose", 0),
            ("confirm", False),
            ("choose", None),
        ],
    )

    with pytest.raises(menu._Back):
        menu_boards.run(session)

    text = session.io.text
    assert "a key is 2-10 characters" in text
    assert "project PROJ already exists" in text
    assert "lead     bob" in text
    assert _called(env, "ask_person")
    assert not _called(env, "create_project")


def test_ensure_board_reports_existing_board_without_asking_type(env):
    env.existing_board = {"id": 37, "name": "PROJ kanban", "type": "kanban"}
    session = _session(env, [("choose", 3), ("choose", None)])

    with pytest.raises(menu._Back):
        menu_boards.run(session)

    assert "PROJ already has a board: PROJ kanban (id 37, kanban); nothing to do" in session.io.text
    assert not [entry for entry in session.io.asked if entry[0] == "choose" and "Board type" in entry[1]]
    assert not _called(env, "ensure_board")
    assert session.last_board == 37


def test_ensure_board_without_board_previews_and_cancels(env):
    session = _session(env, [("choose", 3), ("choose", 1), ("confirm", False), ("choose", None)])

    with pytest.raises(menu._Back):
        menu_boards.run(session)

    assert "type    scrum" in session.io.text
    assert "cancelled; nothing was written" in session.io.text
    assert not _called(env, "ensure_board")


def test_create_sprint_lists_scrum_boards_and_validates_dates(env):
    session = _session(
        env,
        [
            ("choose", 3 + 1),
            ("choose", 0),
            ("ask", "Sprint 6"),
            ("ask", "11/02/2026"),
            ("ask", "2026-02-30"),
            ("ask", "2026-11-02"),
            ("ask", "2026-11-01"),
            ("ask", "2026-11-16"),
            ("ask", ""),
            ("confirm", False),
            ("choose", None),
        ],
    )

    with pytest.raises(menu._Back):
        menu_boards.run(session)

    assert _called(env, "list_boards")[0][2] == {"board_type": "scrum"}
    text = session.io.text
    assert "'11/02/2026' is not YYYY-MM-DD" in text
    assert "2026-02-30 is not a real calendar date" in text
    assert "must not be before the start date 2026-11-02" in text
    assert "start 2026-11-02" in text and "end   2026-11-16" in text
    assert any("YYYY-MM-DD" in entry[1] for entry in session.io.asked if entry[0] == "ask")
    assert not _called(env, "create_sprint")


def test_create_sprint_confirmed_passes_board_and_dates(env):
    session = _session(
        env,
        [
            ("choose", 4),
            ("choose", 0),
            ("ask", "Sprint 6"),
            ("ask", ""),
            ("ask", ""),
            ("ask", "Ship it"),
            ("confirm", True),
            ("choose", None),
        ],
    )

    with pytest.raises(menu._Back):
        menu_boards.run(session)

    assert _called(env, "create_sprint")[0][1][1:] == (50, "Sprint 6", None, None, "Ship it")
    assert "created sprint Sprint 6 (id 6)" in session.io.text


def test_create_sprint_with_no_scrum_boards_explains_kanban(env):
    env.scrum_boards = []
    session = _session(env, [("choose", 4), ("choose", None)])

    with pytest.raises(menu._Back):
        menu_boards.run(session)

    assert "no scrum boards; kanban boards have no sprints" in session.io.text
