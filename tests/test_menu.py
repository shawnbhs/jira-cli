"""Tests for the interactive menu (``jira_cli.cli.menu``).

Everything is offline: a scripted :class:`ScriptedIO` replaces the terminal and every
service module is replaced by a recording fake installed in ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jira_cli.cli import menu
from jira_cli.services.export import safe_filename


class FakeJiraError(Exception):
    """Stands in for ``jira_cli.errors.JiraError`` so the test needs no core module."""


# --------------------------------------------------------------------- scripted IO

_SENTINEL_INTERRUPT = object()
_SENTINEL_EOF = object()


class ScriptedIO:
    """A :class:`menu.MenuIO` whose answers come from a script of ``(kind, value)``."""

    def __init__(self, script):
        self.script = list(script)
        self.shown: list[str] = []
        self.asked: list[tuple] = []

    def _answer(self, kind: str):
        if not self.script:
            raise AssertionError(f"script exhausted; menu asked for a {kind}")
        expected_kind, value = self.script.pop(0)
        if expected_kind != kind:
            raise AssertionError(f"script expected a {expected_kind}, menu asked for a {kind}")
        if value is _SENTINEL_INTERRUPT:
            raise KeyboardInterrupt
        if value is _SENTINEL_EOF:
            raise EOFError
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
    def text(self) -> str:
        return "\n".join(self.shown)

    def choose_options(self, title_fragment: str) -> list[str]:
        for entry in self.asked:
            if entry[0] == "choose" and title_fragment in entry[1]:
                return entry[2]
        raise AssertionError(f"no choose prompt mentioning {title_fragment!r}")


# ------------------------------------------------------------------------- fakes


@dataclass
class FakeIssueSpec:
    """Mirror of ``services.issues.IssueSpec`` (only the fields the menu sets)."""

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
    story_points: float | None = None
    sprint_id: int | None = None


@dataclass
class FakeSettings:
    base_url: str = "https://jira.example.com"
    default_project: str = "PROJ"
    teams_file: Path = Path("teams.yaml")
    filters_file: Path = Path("filters.yaml")


class FakeClient:
    def myself(self):
        return {"name": "alice", "displayName": "Alice Example"}


ISSUE = {
    "key": "PROJ-1",
    "url": "https://jira.example.com/browse/PROJ-1",
    "summary": "An issue",
    "type": "Task",
    "status": "To Do",
    "assignee": "alice",
    "project": "PROJ",
    "priority": "Medium",
    "due": None,
}


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def env(monkeypatch):
    """Install fake services and return ``(calls, client, settings)``."""
    calls: list[tuple] = []

    def record(name, result=None):
        def _fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result() if callable(result) else result

        return _fn

    def report(title="report", rows=None):
        return {
            "title": title,
            "columns": ["key", "summary"],
            "rows": rows if rows is not None else [{"key": "PROJ-1", "summary": "An issue"}],
            "summary": {},
            "meta": {},
        }

    fakes = {
        "jira_cli.report": _module(
            "jira_cli.report",
            make_report=lambda title, columns, rows, summary=None, meta=None: {
                "title": title,
                "columns": columns,
                "rows": rows,
                "summary": summary or {},
                "meta": meta or {},
            },
        ),
        "jira_cli.config": _module(
            "jira_cli.config",
            describe_settings=lambda settings: {"base_url": settings.base_url},
        ),
        "jira_cli.cli.output": _module(
            "jira_cli.cli.output",
            render_report=lambda rep, fmt: f"[report {rep['title']} rows={len(rep['rows'])}]",
            render_table=lambda columns, rows: f"[table {len(rows)} rows]",
        ),
        "jira_cli.services.issues": _module(
            "jira_cli.services.issues",
            IssueSpec=FakeIssueSpec,
            my_issues=record("my_issues", lambda: [ISSUE]),
            search_issues=record("search_issues", lambda: [ISSUE]),
            get_issue=record("get_issue", lambda: dict(ISSUE)),
            create_issue=record(
                "create_issue", lambda: {"key": "PROJ-9", "url": "u", "warnings": []}
            ),
            assign=record("assign", dict),
            list_transitions=record(
                "list_transitions", lambda: [{"id": "11", "name": "Start", "to": "In Progress"}]
            ),
            transition=record("transition", dict),
            add_comment=record("add_comment", dict),
            update_fields=record("update_fields", dict),
            link_issues=record("link_issues", dict),
            list_link_types=record("list_link_types", lambda: [{"name": "Blocks"}]),
        ),
        "jira_cli.services.projects": _module(
            "jira_cli.services.projects",
            list_projects=record(
                "list_projects",
                lambda: [{"key": "PROJ", "name": "Project", "lead": "alice", "type": "software"}],
            ),
            list_issue_types=record("list_issue_types", lambda: ["Task", "Story"]),
            list_priorities=record("list_priorities", lambda: ["High", "Medium"]),
            create_project=record("create_project", lambda: {"key": "NEW", "url": "u"}),
        ),
        "jira_cli.services.boards": _module(
            "jira_cli.services.boards",
            board_for_project=record(
                "board_for_project", lambda: {"id": 12, "name": "PROJ board", "type": "scrum"}
            ),
            ensure_board=record(
                "ensure_board", lambda: {"board": {"id": 12, "name": "PROJ board"}, "created": False}
            ),
            list_sprints=record(
                "list_sprints", lambda: [{"id": 5, "name": "Sprint 5", "state": "active"}]
            ),
            create_sprint=record("create_sprint", lambda: {"id": 6, "name": "Sprint 6"}),
        ),
        "jira_cli.services.people": _module(
            "jira_cli.services.people",
            assignable_users=record(
                "assignable_users",
                lambda: [
                    {"username": "alice", "display_name": "Alice"},
                    {"username": "bob", "display_name": "Bob"},
                ],
            ),
            load_teams=record("load_teams", lambda: {"backend": {"members": ["alice", "bob"]}}),
            save_team=record("save_team", dict),
            remove_team=record("remove_team", lambda: True),
            resolve_people=lambda spec, teams: (
                list(teams.get(spec.split(":", 1)[-1], {}).get("members", []))
                if spec.startswith(("team:", "@"))
                else [part.strip() for part in spec.split(",") if part.strip()]
            ),
        ),
        "jira_cli.services.worklog": _module(
            "jira_cli.services.worklog",
            worklog_report=record("worklog_report", lambda: report("Worklog")),
            add_worklog=record("add_worklog", dict),
            set_estimate=record("set_estimate", dict),
        ),
        "jira_cli.services.reports": _module(
            "jira_cli.services.reports",
            team_dashboard=record("team_dashboard", lambda: report("Team dashboard")),
            person_activity=record("person_activity", lambda: report("Person activity")),
            stale_issues=record("stale_issues", lambda: report("Stale")),
            overdue_issues=record("overdue_issues", lambda: report("Overdue")),
            incomplete_issues=record("incomplete_issues", lambda: report("Incomplete")),
            unassigned_issues=record("unassigned_issues", lambda: report("Unassigned")),
            workload=record("workload", lambda: report("Workload")),
        ),
        "jira_cli.services.sprint_report": _module(
            "jira_cli.services.sprint_report",
            sprint_report=record("sprint_report", lambda: report("Sprint report")),
            velocity=record("velocity", lambda: report("Velocity")),
        ),
        "jira_cli.services.export": _module(
            "jira_cli.services.export",
            write_report=record("write_report", lambda: Path("out.json")),
            safe_filename=safe_filename,
        ),
        "jira_cli.services.filters": _module(
            "jira_cli.services.filters",
            list_filters=record("list_filters", lambda: {"mine": {"jql": "assignee = alice"}}),
            save_filter=record("save_filter", dict),
            delete_filter=record("delete_filter", lambda: True),
            server_filters=record("server_filters", lambda: [{"name": "fav", "jql": "x"}]),
        ),
        "jira_cli.services.plan": _module(
            "jira_cli.services.plan",
            load_plan=record("load_plan", lambda: {"issues": []}),
            new_plan_template=record("new_plan_template", lambda: "issues: []\n"),
            validate_plan=record("validate_plan", lambda: {"ok": True, "errors": [], "warnings": []}),
            preview_plan=record("preview_plan", lambda: report("Plan preview")),
            apply_plan=record(
                "apply_plan",
                lambda: {
                    "created": [{"ref": "t1", "key": "PROJ-2", "url": "u"}],
                    "skipped": [],
                    "failed": [],
                },
            ),
        ),
    }
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(menu, "JiraError", FakeJiraError)
    return calls, FakeClient(), FakeSettings()


def names(calls) -> list[str]:
    return [call[0] for call in calls]


# ------------------------------------------------------------------ navigation

#: First answer(s) that back straight out of each main-menu item.
BACK_OUT = {
    "1": [("confirm", False), ("ask", "b")],
    "2": [("ask", "b")],
    "3": [("ask", "b")],
    "4": [("choose", None)],
    "5": [("choose", None)],
    "6": [("choose", None)],
    "7": [("ask", "b")],
    "8": [("choose", None)],
    "9": [("ask", "b")],
    "10": [("choose", None)],
    "11": [("choose", None)],
    "12": [("choose", None)],
    "13": [("choose", None)],
    "14": [],
    "15": [("confirm", False)],
}


@pytest.mark.parametrize("key", sorted(BACK_OUT, key=int))
def test_every_main_menu_item_is_reachable_and_backs_out(env, key):
    _calls, client, settings = env
    io = ScriptedIO([("ask", key), *BACK_OUT[key], ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert io.script == [], "the menu did not consume the whole script"
    assert "main menu" in io.text


def test_main_menu_lists_all_fifteen_items():
    text = menu.main_menu_text()
    for key in BACK_OUT:
        assert f"{key}." in text
    assert "q. quit" in text


def test_back_from_a_submenu_returns_to_the_main_menu(env):
    calls, client, settings = env
    io = ScriptedIO(
        [
            ("ask", "9"),            # issue actions
            ("ask", "PROJ-1"),       # issue key
            ("choose", None),        # back out of the action list
            ("ask", "q"),            # back at the main menu
        ]
    )

    assert menu.run(client, settings, io=io) == 0
    assert io.text.count("main menu") == 2
    assert names(calls) == []


def test_bang_back_word_also_goes_back(env):
    calls, client, settings = env
    io = ScriptedIO([("ask", "3"), ("ask", "!back"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert names(calls) == []


def test_invalid_main_menu_choice_is_rejected_without_quitting(env):
    _calls, client, settings = env
    io = ScriptedIO([("ask", "99"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert "invalid choice: 99" in io.text


def test_keyboard_interrupt_at_the_main_menu_exits_zero(env):
    _calls, client, settings = env
    io = ScriptedIO([("ask", _SENTINEL_INTERRUPT)])

    assert menu.run(client, settings, io=io) == 0


def test_keyboard_interrupt_at_a_prompt_goes_back_one_level(env):
    calls, client, settings = env
    io = ScriptedIO([("ask", "3"), ("ask", _SENTINEL_INTERRUPT), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert names(calls) == []
    assert io.text.count("main menu") == 2


# ---------------------------------------------------------------- create issue


def _create_issue_script(confirm: bool):
    return [
        ("ask", "8"),                 # main menu: create issue
        ("choose", 0),                # project PROJ
        ("choose", 1),                # issue type Story
        ("ask", "Ship the thing"),    # summary
        ("ask", "details"),           # description
        ("choose", 1),                # assignee alice
        ("choose", 1),                # priority High
        ("ask", "3h"),                # estimate
        ("ask", "2026-10-20"),        # due
        ("ask", ""),                  # epic
        ("ask", ""),                  # parent
        ("choose", 1),                # sprint 5
        ("confirm", confirm),
        ("ask", "q"),
    ]


def test_create_issue_happy_path_calls_create_issue_once_with_the_spec(env):
    calls, client, settings = env
    io = ScriptedIO(_create_issue_script(confirm=True))

    assert menu.run(client, settings, io=io) == 0
    assert io.script == []

    creates = [call for call in calls if call[0] == "create_issue"]
    assert len(creates) == 1
    spec = creates[0][1][1]
    assert spec == FakeIssueSpec(
        project="PROJ",
        summary="Ship the thing",
        issue_type="Story",
        description="details",
        assignee="alice",
        priority="High",
        due_date="2026-10-20",
        original_estimate="3h",
        epic_key=None,
        parent_key=None,
        sprint_id=5,
    )
    assert "created PROJ-9" in io.text


def test_create_issue_reuses_the_existing_board(env):
    calls, client, settings = env
    io = ScriptedIO(_create_issue_script(confirm=True))

    menu.run(client, settings, io=io)

    assert "board_for_project" in names(calls)
    assert "ensure_board" not in names(calls)
    assert "reused" in io.text


def test_declined_confirmation_creates_nothing(env):
    calls, client, settings = env
    io = ScriptedIO(_create_issue_script(confirm=False))

    assert menu.run(client, settings, io=io) == 0
    assert "create_issue" not in names(calls)
    assert "nothing was written" in io.text


def test_declined_confirmation_on_a_comment_writes_nothing(env):
    calls, client, settings = env
    io = ScriptedIO(
        [
            ("ask", "9"),
            ("ask", "PROJ-1"),
            ("choose", 3),            # Comment
            ("ask", "needs review"),
            ("confirm", False),
            ("ask", "q"),
        ]
    )

    assert menu.run(client, settings, io=io) == 0
    assert "add_comment" not in names(calls)
    assert "nothing was written" in io.text


def test_confirmed_comment_is_written(env):
    calls, client, settings = env
    io = ScriptedIO(
        [
            ("ask", "9"),
            ("ask", "PROJ-1"),
            ("choose", 3),
            ("ask", "needs review"),
            ("confirm", True),
            ("ask", "q"),
        ]
    )

    menu.run(client, settings, io=io)

    comments = [call for call in calls if call[0] == "add_comment"]
    assert len(comments) == 1
    assert comments[0][1][1:] == ("PROJ-1", "needs review")


# ----------------------------------------------------------------- resilience


def test_jira_error_inside_an_action_keeps_the_menu_alive(env):
    calls, client, settings = env
    reports = sys.modules["jira_cli.services.reports"]

    def boom(*args, **kwargs):
        raise FakeJiraError("permission denied")

    reports.team_dashboard = boom

    io = ScriptedIO(
        [
            ("ask", "2"),          # team dashboard
            ("ask", "alice"),      # users
            ("ask", ""),           # projects (default)
            ("ask", "7d"),         # since
            ("ask", "1"),          # the menu is still alive: my issues
            ("confirm", False),
            ("ask", "b"),
            ("ask", "q"),
        ]
    )

    assert menu.run(client, settings, io=io) == 0
    assert "error: permission denied" in io.text
    assert "my_issues" in names(calls)


def test_team_expression_is_resolved_through_teams_file(env):
    calls, client, settings = env
    io = ScriptedIO(
        [
            ("ask", "5"),
            ("choose", 0),         # workload
            ("ask", "team:backend"),
            ("ask", ""),           # projects
            ("ask", "b"),          # leave the report
            ("ask", "q"),
        ]
    )

    menu.run(client, settings, io=io)

    workloads = [call for call in calls if call[0] == "workload"]
    assert len(workloads) == 1
    assert workloads[0][1][1] == ["alice", "bob"]


# --------------------------------------------------------------------- reports


def test_report_can_be_exported_after_confirmation(env, tmp_path):
    calls, client, settings = env
    target = tmp_path / "mine.json"
    io = ScriptedIO(
        [
            ("ask", "1"),
            ("confirm", False),       # include done?
            ("ask", "e"),             # export
            ("ask", "json"),
            ("ask", str(target)),
            ("confirm", True),
            ("ask", "b"),
            ("ask", "q"),
        ]
    )

    assert menu.run(client, settings, io=io) == 0
    writes = [call for call in calls if call[0] == "write_report"]
    assert len(writes) == 1
    assert writes[0][1][1] == target
    assert writes[0][1][2] == "json"


def test_report_refresh_rebuilds_the_report(env):
    calls, client, settings = env
    io = ScriptedIO(
        [
            ("ask", "1"),
            ("confirm", True),
            ("ask", "r"),
            ("ask", "b"),
            ("ask", "q"),
        ]
    )

    menu.run(client, settings, io=io)

    assert names(calls).count("my_issues") == 2


def test_export_last_report_without_a_report_says_so(env):
    calls, client, settings = env
    io = ScriptedIO([("ask", "14"), ("ask", "q")])

    menu.run(client, settings, io=io)

    assert "no report to export yet" in io.text
    assert "write_report" not in names(calls)


def test_find_issue_by_key_shows_the_issue(env):
    calls, client, settings = env
    io = ScriptedIO([("ask", "7"), ("ask", "proj-1"), ("ask", "q")])

    menu.run(client, settings, io=io)

    gets = [call for call in calls if call[0] == "get_issue"]
    assert gets and gets[0][1][1] == "PROJ-1"
    assert "search_issues" not in names(calls)


def test_find_issue_by_saved_filter_name_runs_its_jql(env):
    calls, client, settings = env
    io = ScriptedIO([("ask", "7"), ("ask", "mine"), ("ask", "b"), ("ask", "q")])

    menu.run(client, settings, io=io)

    searches = [call for call in calls if call[0] == "search_issues"]
    assert searches and searches[0][1][1] == "assignee = alice"


def test_find_issue_by_jql_searches_directly(env):
    calls, client, settings = env
    io = ScriptedIO(
        [("ask", "7"), ("ask", "project = PROJ AND status = Done"), ("ask", "b"), ("ask", "q")]
    )

    menu.run(client, settings, io=io)

    searches = [call for call in calls if call[0] == "search_issues"]
    assert searches and searches[0][1][1] == "project = PROJ AND status = Done"


# ----------------------------------------------------------------- plan files


def test_plan_apply_does_a_dry_run_before_the_real_apply(env):
    calls, client, settings = env
    io = ScriptedIO(
        [
            ("ask", "10"),
            ("choose", 3),            # Apply
            ("ask", "plan.yaml"),
            ("confirm", True),
            ("ask", "q"),
        ]
    )

    menu.run(client, settings, io=io)

    applies = [call for call in calls if call[0] == "apply_plan"]
    assert [call[2]["dry_run"] for call in applies] == [True, False]


def test_plan_apply_declined_never_runs_for_real(env):
    calls, client, settings = env
    io = ScriptedIO(
        [
            ("ask", "10"),
            ("choose", 3),
            ("ask", "plan.yaml"),
            ("confirm", False),
            ("ask", "q"),
        ]
    )

    menu.run(client, settings, io=io)

    applies = [call for call in calls if call[0] == "apply_plan"]
    assert [call[2]["dry_run"] for call in applies] == [True]


# --------------------------------------------------------------------- default IO


def test_default_menu_io_reads_and_writes(monkeypatch, capsys):
    answers = iter(["", "2", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    io = menu.MenuIO()

    assert io.ask("Name", "alice") == ""
    assert io.choose("Pick", ["a", "b"]) == 1
    assert io.confirm("Proceed?") is True
    io.show("hello")
    assert "hello" in capsys.readouterr().out


# ------------------------------------------------- F1: server text is made safe

ESC_SUMMARY = "evil\x1b]52;c;cm0gLXJmIH4=\x07\x1b[2K\rfake"


def test_issue_detail_strips_escape_sequences_from_server_text(env):
    _calls, client, settings = env
    issues = sys.modules["jira_cli.services.issues"]
    issues.get_issue = lambda *a, **k: dict(
        ISSUE,
        summary=ESC_SUMMARY,
        description="line one\n\x1b[31mred\u202e",
        comments=[{"author": "bob\x9b", "created": "2026-10-01", "body": "hi\x1b]0;title\x07"}],
    )
    io = ScriptedIO([("ask", "7"), ("ask", "PROJ-1"), ("ask", "q")])

    menu.run(client, settings, io=io)

    assert "\x1b" not in io.text
    assert "\x9b" not in io.text
    assert "\u202e" not in io.text
    assert "\r" not in io.text
    assert "line one\n" in io.text, "description newlines are kept"


def test_preview_lines_and_choices_strip_escape_sequences(env):
    calls, client, settings = env
    issues = sys.modules["jira_cli.services.issues"]
    issues.list_transitions = lambda *a, **k: [
        {"name": "Start\x1b[2K", "to": "Done\nfake line"}
    ]
    io = ScriptedIO(
        [
            ("ask", "9"),
            ("ask", "PROJ-1"),
            ("choose", 2),            # Move status
            ("choose", 0),
            ("ask", ""),              # no comment
            ("confirm", False),
            ("ask", "q"),
        ]
    )

    menu.run(client, settings, io=io)

    assert "\x1b" not in io.text
    assert "Done fake line" in io.text, "a newline in a preview value is folded"
    assert all("\x1b" not in option for option in io.choose_options("Move PROJ-1"))


def test_error_message_from_server_strips_escape_sequences(env):
    _calls, client, settings = env

    def boom(*args, **kwargs):
        raise FakeJiraError("denied\x1b]52;c;eA==\x07")

    sys.modules["jira_cli.services.reports"].team_dashboard = boom
    io = ScriptedIO([("ask", "2"), ("ask", "alice"), ("ask", ""), ("ask", "7d"), ("ask", "q")])

    menu.run(client, settings, io=io)

    assert "error: denied" in io.text
    assert "\x1b" not in io.text


# ------------------------------------- F5: unexpected errors never end the session


def _export_script(target):
    return [
        ("ask", "1"),
        ("confirm", False),
        ("ask", "e"),
        ("ask", "json"),
        ("ask", str(target)),
        ("confirm", True),
        ("ask", "b"),
        ("ask", "q"),
    ]


def test_os_error_in_an_action_prints_one_error_line_and_keeps_the_menu(env, tmp_path):
    _calls, client, settings = env

    def unwritable(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(tmp_path / "out.json"))

    sys.modules["jira_cli.services.export"].write_report = unwritable
    io = ScriptedIO(
        [
            ("ask", "1"),
            ("confirm", False),
            ("ask", "e"),
            ("ask", "json"),
            ("ask", str(tmp_path / "out.json")),
            ("confirm", True),
            ("ask", "14"),            # back at the main menu; last report survived
            ("ask", "b"),             # back out of the export format prompt
            ("ask", "q"),
        ]
    )

    assert menu.run(client, settings, io=io) == 0
    assert io.script == []
    assert "error: Permission denied: " in io.text
    assert "Traceback" not in io.text
    assert "no report to export yet" not in io.text


def test_unexpected_exception_is_reported_redacted_and_the_menu_continues(env):
    calls, client, settings = env

    def bug(*args, **kwargs):
        raise ValueError("bad value token=QQQQLEAKME1234")

    sys.modules["jira_cli.services.reports"].team_dashboard = bug
    io = ScriptedIO(
        [
            ("ask", "2"), ("ask", "alice"), ("ask", ""), ("ask", "7d"),
            ("ask", "1"), ("confirm", False), ("ask", "b"),   # menu still alive
            ("ask", "q"),
        ]
    )

    assert menu.run(client, settings, io=io) == 0
    assert "error: unexpected ValueError" in io.text
    assert "QQQQLEAKME1234" not in io.text
    assert "my_issues" in names(calls)


def test_keyboard_interrupt_during_a_service_call_returns_to_the_menu(env):
    _calls, client, settings = env

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    sys.modules["jira_cli.services.reports"].team_dashboard = interrupted
    io = ScriptedIO([("ask", "2"), ("ask", "alice"), ("ask", ""), ("ask", "7d"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert "error" not in io.text


# --------------------------------------------- F6: new plan file never clobbers


def _new_plan_script(path, *answers):
    return [("ask", "10"), ("choose", 0), ("ask", str(path)), *answers, ("ask", "q")]


def test_new_plan_file_is_written_when_the_file_is_new(env, tmp_path):
    _calls, client, settings = env
    target = tmp_path / "plan.yaml"
    io = ScriptedIO(_new_plan_script(target, ("confirm", True)))

    menu.run(client, settings, io=io)

    assert target.read_text(encoding="utf-8") == "issues: []\n"
    assert "OVERWRITE" not in io.text


def test_new_plan_file_keeps_an_existing_file_by_default(env, tmp_path):
    _calls, client, settings = env
    target = tmp_path / "plan.yaml"
    target.write_text("MY HAND-WRITTEN PLAN\n", encoding="utf-8")
    io = ScriptedIO(_new_plan_script(target, ("confirm", False)))

    assert menu.run(client, settings, io=io) == 0
    assert io.script == []
    assert target.read_text(encoding="utf-8") == "MY HAND-WRITTEN PLAN\n"
    assert "already exists" in str(io.asked)
    assert "nothing was written" in io.text


def test_new_plan_file_overwrites_only_after_explicit_confirmation(env, tmp_path):
    _calls, client, settings = env
    target = tmp_path / "plan.yaml"
    target.write_text("MY HAND-WRITTEN PLAN\n", encoding="utf-8")
    io = ScriptedIO(_new_plan_script(target, ("confirm", True), ("confirm", True)))

    menu.run(client, settings, io=io)

    assert target.read_text(encoding="utf-8") == "issues: []\n"
    assert "OVERWRITE existing file" in io.text


def test_new_plan_file_declined_at_the_preview_keeps_the_existing_file(env, tmp_path):
    _calls, client, settings = env
    target = tmp_path / "plan.yaml"
    target.write_text("MY HAND-WRITTEN PLAN\n", encoding="utf-8")
    io = ScriptedIO(_new_plan_script(target, ("confirm", True), ("confirm", False)))

    menu.run(client, settings, io=io)

    assert target.read_text(encoding="utf-8") == "MY HAND-WRITTEN PLAN\n"


# ------------------------------------------- F7: labels are never cleared by accident


def _edit_labels_script(answer, *rest):
    labels_index = menu._EDITABLE_FIELDS.index("labels")
    return [
        ("ask", "9"),
        ("ask", "PROJ-1"),
        ("choose", 6),                # Edit fields
        ("choose", labels_index),
        ("ask", answer),
        *rest,
        ("ask", "q"),
    ]


@pytest.fixture
def labelled(env):
    calls, client, settings = env
    sys.modules["jira_cli.services.issues"].get_issue = lambda *a, **k: dict(
        ISSUE, labels=["a", "b"]
    )
    return calls, client, settings


@pytest.mark.parametrize("answer", [",", " , ", ",,"])
def test_label_input_that_parses_to_nothing_is_rejected(labelled, answer):
    calls, client, settings = labelled
    io = ScriptedIO(_edit_labels_script(answer))

    assert menu.run(client, settings, io=io) == 0
    assert io.script == []
    assert "update_fields" not in names(calls)
    assert "no labels parsed" in io.text


def test_dash_clears_labels_and_the_preview_says_so(labelled):
    calls, client, settings = labelled
    io = ScriptedIO(_edit_labels_script("-", ("confirm", True)))

    menu.run(client, settings, io=io)

    assert "labels: [a, b] -> (cleared)" in io.text
    updates = [call for call in calls if call[0] == "update_fields"]
    assert [call[1][2] for call in updates] == [{"labels": []}]


def test_new_labels_replace_the_old_ones_with_a_before_and_after_preview(labelled):
    calls, client, settings = labelled
    io = ScriptedIO(_edit_labels_script("x, y", ("confirm", True)))

    menu.run(client, settings, io=io)

    assert "labels: [a, b] -> [x, y]" in io.text
    updates = [call for call in calls if call[0] == "update_fields"]
    assert [call[1][2] for call in updates] == [{"labels": ["x", "y"]}]


# ----------------------------------------------- export: default file name is safe


def test_default_export_file_name_is_built_with_safe_filename(env):
    _calls, client, settings = env
    report = {"title": "../../etc/Sprint 5: done?", "columns": [], "rows": []}
    session = menu._Session(client=client, settings=settings, io=None)
    defaults = []

    class CaptureIO(ScriptedIO):
        def ask(self, prompt, default=None):
            defaults.append(default)
            return super().ask(prompt, default)

    session.io = CaptureIO([("ask", "md"), ("ask", "b")])

    menu._export_report(session, report)

    assert defaults[1] == safe_filename("../../etc/sprint 5: done?") + ".md"
    assert "/" not in defaults[1] and not defaults[1].startswith(".")
