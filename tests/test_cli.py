"""Tests for the non-interactive CLI (``jira_cli.cli.main`` and ``cli.output``).

Everything is offline and self-contained.  The real CLI, core and service
modules are imported normally; nothing is ever written into
:data:`sys.modules` at import time.  Each test replaces what it needs (settings
loader, client, field map, service modules, the lazily imported menu and MCP
server) with recording fakes through ``monkeypatch``, which restores the real
objects afterwards, so this file is safe to run in the full suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jira_cli.cli import main as cli
from jira_cli.cli import output


def _module(name: str, **attrs) -> types.ModuleType:
    """Build a throwaway module object carrying *attrs* (never registered)."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


_CONFIG_DIR = Path(tempfile.gettempdir()) / "jira-cli-test-config"


@dataclass(frozen=True)
class StubSettings:
    """Mirror of ``config.Settings`` (every field, so the real
    ``describe_settings`` can read it) that registers no secret."""

    base_url: str = "https://jira.example.com"
    auth_mode: str = "pat"
    username: str = "alice"
    secret: str = "placeholder"  # noqa: S105 - a fixture value, not a credential
    default_project: str | None = "PROJ"
    verify_tls: bool | str = True
    timeout: float = 20.0
    read_only: bool = False
    hours_per_day: float = 8.0
    days_per_week: float = 5.0
    config_dir: Path = _CONFIG_DIR
    teams_file: Path = _CONFIG_DIR / "teams.yaml"
    filters_file: Path = _CONFIG_DIR / "filters.yaml"
    field_overrides: dict = field(default_factory=dict)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    """Never read the user's real ``~/.config/jira-cli`` or ``~/.jira-cli``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JIRA_CLI_ENV_FILE", str(tmp_path / "absent.env"))
    for name in (
        "JIRA_BASE_URL",
        "JIRA_PAT",
        "JIRA_USERNAME",
        "JIRA_PASSWORD",
        "JIRA_API_TOKEN",
        "JIRA_READ_ONLY",
        "JIRA_CLI_DEBUG",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------- fakes


@dataclass
class Recorder:
    """Collects ``(name, args, kwargs)`` for every fake call the CLI makes."""

    calls: list[tuple] = field(default_factory=list)

    def fn(self, name: str, result=None):
        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return result() if callable(result) else result

        return _call

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def one(self, name: str) -> tuple:
        matches = [call for call in self.calls if call[0] == name]
        assert matches, f"{name} was never called; calls: {self.names()}"
        return matches[0]


PROJECT_ROW = {"key": "PROJ", "name": "Project", "lead": "alice", "type": "software"}
BOARD = {"id": 12, "name": "PROJ board", "type": "scrum"}
SPRINT = {"id": 5, "name": "Sprint 5", "state": "active"}
USER = {"username": "alice", "display_name": "Alice", "email": "a@example.com", "active": True}
TEAM = {"members": ["alice", "bob"], "projects": ["PROJ"], "boards": [12]}
WORKLOG_ROW = {"id": "1", "author": "alice", "started": "x", "seconds": 3600, "comment": ""}
SERVER_FILTER = {"id": "1", "name": "fav", "jql": "x", "owner": "alice"}
APPLY_RESULT = {
    "created": [{"ref": "t1", "key": "PROJ-9", "url": "u"}],
    "skipped": [],
    "failed": [],
    "dry_run": True,
}
COMMENT_ROW = {"author": "alice", "created": "x", "body": "hi"}

ISSUE = {
    "key": "PROJ-1",
    "url": "https://jira.example.com/browse/PROJ-1",
    "summary": "Fix the login screen",
    "type": "Task",
    "status": "To Do",
    "assignee": "alice",
    "priority": "Medium",
    "due": None,
}


def make_report(title: str = "Team dashboard") -> dict:
    return {
        "title": title,
        "columns": ["user", "open"],
        "rows": [{"user": "alice", "open": 3}, {"user": "bob", "open": 1}],
        "summary": {"users": 2},
        "meta": {"generated_at": "2026-10-11T00:00:00Z", "jql": []},
    }


class FakeClient:
    """Stands in for ``JiraClient``; only what ``doctor`` and ``me`` need."""

    def __init__(self, server=None, me=None, fail=None):
        self._server = server or {"version": "10.3.0", "deploymentType": "Server"}
        self._me = me or {"name": "alice", "displayName": "Alice Example"}
        self._fail = fail

    def get(self, path, params=None):
        if self._fail == "server":
            raise cli.JiraError("serverInfo exploded")
        return self._server

    def myself(self):
        if self._fail == "myself":
            raise cli.AuthError("401 unauthorized")
        return self._me


class FakeFieldMap:
    def __init__(self, client, sprint="customfield_10109", epic_link=None, points="cf_1"):
        self.sprint = sprint
        self.epic_link = epic_link
        self.story_points = points


@pytest.fixture(autouse=True)
def _reset_color():
    """Colour is process state; never let one test's ``--no-color`` leak."""
    output.set_color(None)
    yield
    output.set_color(None)


@pytest.fixture
def env(monkeypatch):
    """Patch the CLI's settings/client plus every service with a recorder."""
    rec = Recorder()
    settings = StubSettings()
    client = FakeClient()

    monkeypatch.setattr(cli.config_module, "load_settings", lambda **kw: settings, raising=False)
    monkeypatch.setattr(cli, "JiraClient", lambda s: client)
    monkeypatch.setattr(cli, "FieldMap", FakeFieldMap)

    monkeypatch.setattr(
        cli,
        "issues",
        _module(
            "issues",
            IssueSpec=FakeIssueSpec,
            get_issue=rec.fn("get_issue", lambda: dict(ISSUE)),
            search_issues=rec.fn("search_issues", lambda: [dict(ISSUE)]),
            my_issues=rec.fn("my_issues", lambda: [dict(ISSUE)]),
            create_issue=rec.fn(
                "create_issue", lambda: {"key": "PROJ-9", "url": "u", "warnings": []}
            ),
            update_fields=rec.fn("update_fields", dict),
            assign=rec.fn("assign", dict),
            list_transitions=rec.fn(
                "list_transitions", lambda: [{"id": "11", "name": "Start", "to": "In Progress"}]
            ),
            transition=rec.fn("transition", dict),
            add_comment=rec.fn("add_comment", dict),
            list_comments=rec.fn("list_comments", lambda: [dict(COMMENT_ROW)]),
            link_issues=rec.fn("link_issues", dict),
            list_link_types=rec.fn("list_link_types", lambda: [{"name": "Blocks"}]),
            delete_issue=rec.fn("delete_issue", dict),
        ),
    )
    monkeypatch.setattr(
        cli,
        "projects",
        _module(
            "projects",
            list_projects=rec.fn("list_projects", lambda: [dict(PROJECT_ROW)]),
            get_project=rec.fn(
                "get_project", lambda: dict(PROJECT_ROW, issue_types=["Task"])
            ),
            create_project=rec.fn("create_project", lambda: {"key": "NEW", "url": "u"}),
            create_component=rec.fn("create_component", lambda: {"name": "api"}),
            create_version=rec.fn("create_version", lambda: {"name": "1.0"}),
            list_issue_types=rec.fn("list_issue_types", lambda: ["Task", "Story"]),
            list_priorities=rec.fn("list_priorities", lambda: ["High", "Medium"]),
            list_statuses=rec.fn("list_statuses", lambda: ["To Do", "Done"]),
        ),
    )
    monkeypatch.setattr(
        cli,
        "boards",
        _module(
            "boards",
            list_boards=rec.fn("list_boards", lambda: [dict(BOARD, project="PROJ")]),
            get_board=rec.fn("get_board", lambda: dict(BOARD)),
            ensure_board=rec.fn(
                "ensure_board", lambda: {"board": dict(BOARD), "created": False}
            ),
            board_issues=rec.fn("board_issues", lambda: [dict(ISSUE)]),
            list_sprints=rec.fn("list_sprints", lambda: [dict(SPRINT)]),
            active_sprint=rec.fn("active_sprint", lambda: dict(SPRINT)),
            create_sprint=rec.fn("create_sprint", lambda: {"id": 6, "name": "Sprint 6"}),
            add_to_sprint=rec.fn("add_to_sprint", dict),
            move_to_backlog=rec.fn("move_to_backlog", dict),
        ),
    )
    monkeypatch.setattr(
        cli,
        "people",
        _module(
            "people",
            search_users=rec.fn("search_users", lambda: [dict(USER)]),
            assignable_users=rec.fn(
                "assignable_users",
                lambda: [dict(USER, username="bob", display_name="Bob")],
            ),
            load_teams=rec.fn("load_teams", lambda: {"backend": dict(TEAM)}),
            save_team=rec.fn("save_team", lambda: {"members": ["alice"]}),
            remove_team=rec.fn("remove_team", lambda: True),
            resolve_people=lambda spec, teams: (
                list(teams.get(spec.split(":", 1)[1], {}).get("members") or [])
                if str(spec).startswith("team:")
                else [p.strip() for p in str(spec).split(",") if p.strip()]
            ),
        ),
    )
    monkeypatch.setattr(
        cli,
        "reports",
        _module(
            "reports",
            team_dashboard=rec.fn("team_dashboard", make_report),
            person_activity=rec.fn("person_activity", make_report),
            stale_issues=rec.fn("stale_issues", make_report),
            overdue_issues=rec.fn("overdue_issues", make_report),
            incomplete_issues=rec.fn("incomplete_issues", make_report),
            unassigned_issues=rec.fn("unassigned_issues", make_report),
            workload=rec.fn("workload", make_report),
        ),
    )
    monkeypatch.setattr(
        cli,
        "worklog",
        _module(
            "worklog",
            worklog_report=rec.fn("worklog_report", make_report),
            issue_worklogs=rec.fn("issue_worklogs", lambda: [dict(WORKLOG_ROW)]),
            add_worklog=rec.fn("add_worklog", dict),
            set_estimate=rec.fn("set_estimate", dict),
        ),
    )
    monkeypatch.setattr(
        cli,
        "sprint_report",
        _module(
            "sprint_report",
            sprint_report=rec.fn("sprint_report", make_report),
            velocity=rec.fn("velocity", make_report),
        ),
    )
    monkeypatch.setattr(
        cli,
        "filters",
        _module(
            "filters",
            list_filters=rec.fn("list_filters", lambda: {"mine": {"jql": "assignee = alice"}}),
            save_filter=rec.fn("save_filter", dict),
            get_filter=rec.fn("get_filter", lambda: {"jql": "assignee = alice"}),
            delete_filter=rec.fn("delete_filter", lambda: True),
            server_filters=rec.fn("server_filters", lambda: [dict(SERVER_FILTER)]),
        ),
    )
    monkeypatch.setattr(
        cli,
        "export",
        _module("export", write_report=rec.fn("write_report", lambda: Path("out.md"))),
    )
    monkeypatch.setattr(
        cli,
        "plan",
        _module(
            "plan",
            new_plan_template=rec.fn("new_plan_template", lambda: "# plan template\nissues: []\n"),
            load_plan=rec.fn("load_plan", lambda: {"issues": []}),
            validate_plan=rec.fn(
                "validate_plan", lambda: {"ok": True, "errors": [], "warnings": []}
            ),
            preview_plan=rec.fn("preview_plan", make_report),
            apply_plan=rec.fn("apply_plan", lambda: dict(APPLY_RESULT)),
        ),
    )
    rec.client = client  # type: ignore[attr-defined]
    rec.settings = settings  # type: ignore[attr-defined]
    return rec


@dataclass
class FakeIssueSpec:
    """Mirror of ``services.issues.IssueSpec``."""

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
    fix_versions: list[str] = field(default_factory=list)
    story_points: float | None = None
    sprint_id: int | None = None


def yes(*argv: str) -> list[str]:
    """Prefix a command with ``--yes`` so a write runs without a prompt."""
    return ["--yes", *argv]


# =========================================================== parser coverage

#: One invocation per leaf command in CONTRACT section 4.
EVERY_COMMAND: list[tuple[str, list[str]]] = [
    ("me", ["me"]),
    ("doctor", ["doctor"]),
    ("config.show", ["config", "show"]),
    ("projects.list", ["projects", "list"]),
    ("projects.show", ["projects", "show", "PROJ"]),
    ("projects.create", ["projects", "create", "NEW", "New project", "--lead", "alice"]),
    ("projects.component", ["projects", "component", "PROJ", "api"]),
    ("projects.version", ["projects", "version", "PROJ", "1.0"]),
    ("projects.types", ["projects", "types", "PROJ"]),
    ("projects.priorities", ["projects", "priorities"]),
    ("projects.statuses", ["projects", "statuses", "PROJ"]),
    ("boards.list", ["boards", "list", "--project", "PROJ"]),
    ("boards.show", ["boards", "show", "12"]),
    ("boards.ensure", ["boards", "ensure", "PROJ", "--type", "scrum"]),
    ("boards.issues", ["boards", "issues", "12"]),
    ("sprints", ["sprints", "12", "--state", "active"]),
    ("sprint.active", ["sprint", "active", "12"]),
    ("sprint.create", ["sprint", "create", "12", "Sprint 6", "--goal", "ship"]),
    ("sprint.add", ["sprint", "add", "5", "PROJ-1", "PROJ-2"]),
    ("sprint.backlog", ["sprint", "backlog", "PROJ-1"]),
    ("issue.show", ["issue", "show", "PROJ-1"]),
    ("issue.create", ["issue", "create", "--project", "PROJ", "--summary", "S"]),
    ("issue.edit", ["issue", "edit", "PROJ-1", "--summary", "S2"]),
    ("issue.assign", ["issue", "assign", "PROJ-1", "alice"]),
    ("issue.move", ["issue", "move", "PROJ-1", "Done", "--comment", "c"]),
    ("issue.transitions", ["issue", "transitions", "PROJ-1"]),
    ("issue.comment", ["issue", "comment", "PROJ-1", "hello"]),
    ("issue.comments", ["issue", "comments", "PROJ-1"]),
    ("issue.link", ["issue", "link", "PROJ-1", "PROJ-2", "--type", "Blocks"]),
    ("issue.linktypes", ["issue", "linktypes"]),
    ("issue.delete", ["issue", "delete", "PROJ-1"]),
    ("issue.worklog", ["issue", "worklog", "PROJ-1", "2h", "--comment", "c"]),
    ("issue.worklogs", ["issue", "worklogs", "PROJ-1"]),
    ("issue.estimate", ["issue", "estimate", "PROJ-1", "--original", "3h"]),
    ("search", ["search", "project = PROJ", "--limit", "10"]),
    ("mine", ["mine", "--all"]),
    ("users.search", ["users", "search", "alice"]),
    ("users.assignable", ["users", "assignable", "PROJ", "al"]),
    ("team.list", ["team", "list"]),
    ("team.show", ["team", "show", "backend"]),
    ("team.set", ["team", "set", "backend", "--members", "alice,bob", "--projects", "PROJ"]),
    ("team.remove", ["team", "remove", "backend"]),
    ("report", ["report", "team", "--team", "backend"]),
    ("export", ["export", "report.json", "--format", "md"]),
    ("filter.list", ["filter", "list"]),
    ("filter.save", ["filter", "save", "mine", "assignee = alice"]),
    ("filter.run", ["filter", "run", "mine"]),
    ("filter.delete", ["filter", "delete", "mine"]),
    ("filter.server", ["filter", "server"]),
    ("plan.new", ["plan", "new", "plan.yaml"]),
    ("plan.validate", ["plan", "validate", "plan.yaml"]),
    ("plan.preview", ["plan", "preview", "plan.yaml"]),
    ("plan.apply", ["plan", "apply", "plan.yaml", "--dry-run"]),
    ("mcp", ["mcp"]),
]


@pytest.mark.parametrize(("key", "argv"), EVERY_COMMAND, ids=[c[0] for c in EVERY_COMMAND])
def test_every_contract_command_parses_to_a_handler(key, argv):
    args = cli.build_parser().parse_args(argv)
    assert cli._handler_key(args) == key
    assert key in cli.HANDLERS


def test_every_handler_key_is_reachable_from_the_parser():
    """No handler is dead code: every key in HANDLERS has a sample invocation."""
    assert set(cli.HANDLERS) == {key for key, _argv in EVERY_COMMAND}


@pytest.mark.parametrize("kind", cli.REPORT_KINDS)
def test_report_accepts_every_kind(kind):
    args = cli.build_parser().parse_args(["report", kind])
    assert args.kind == kind


def test_global_flags_are_parsed_before_the_command():
    args = cli.build_parser().parse_args(
        ["--env-file", ".env", "--json", "--no-color", "--yes", "--debug", "me"]
    )
    assert (args.env_file, args.json, args.no_color, args.yes, args.debug) == (
        ".env",
        True,
        True,
        True,
        True,
    )


def test_unknown_command_exits_two_from_argparse():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["nonsense-command"])
    assert excinfo.value.code == 2


def test_group_without_subcommand_reports_an_incomplete_command(capsys, env):
    assert cli.main(["issue"]) == cli.EXIT_ERROR
    assert "error: incomplete command: issue" in capsys.readouterr().err


# ================================================================ exit codes


def _raise(exc):
    def _fn(*args, **kwargs):
        raise exc

    return _fn


def test_input_error_exits_one_with_a_stderr_line(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "search_issues", _raise(cli.InputError("bad jql")))
    assert cli.main(["search", "nonsense"]) == 1
    assert capsys.readouterr().err.splitlines()[0] == "error: bad jql"


def test_config_error_exits_two(capsys, env, monkeypatch):
    monkeypatch.setattr(
        cli.config_module, "load_settings", _raise(cli.ConfigError("JIRA_BASE_URL is not set"))
    )
    assert cli.main(["me"]) == 2
    assert "error: JIRA_BASE_URL is not set" in capsys.readouterr().err


def test_auth_error_exits_three_and_points_at_doctor(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise(cli.AuthError("401 unauthorized")))
    assert cli.main(["mine"]) == 3
    err = capsys.readouterr().err
    assert "error: 401 unauthorized" in err
    assert "run: jira doctor" in err


def test_other_jira_error_exits_one(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise(cli.JiraError("gateway timeout")))
    assert cli.main(["mine"]) == 1
    assert "error: gateway timeout" in capsys.readouterr().err


def test_keyboard_interrupt_exits_130(env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise(KeyboardInterrupt()))
    assert cli.main(["mine"]) == 130


def test_debug_flag_prints_a_traceback(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise(cli.JiraError("boom")))
    assert cli.main(["--debug", "mine"]) == 1
    assert "Traceback (most recent call last)" in capsys.readouterr().err


def test_debug_env_var_prints_a_traceback(capsys, env, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_DEBUG", "1")
    monkeypatch.setattr(cli.issues, "my_issues", _raise(cli.JiraError("boom")))
    assert cli.main(["mine"]) == 1
    assert "Traceback (most recent call last)" in capsys.readouterr().err


def test_no_traceback_without_debug(capsys, env, monkeypatch):
    monkeypatch.delenv("JIRA_CLI_DEBUG", raising=False)
    monkeypatch.setattr(cli.issues, "my_issues", _raise(cli.JiraError("boom")))
    assert cli.main(["mine"]) == 1
    assert "Traceback" not in capsys.readouterr().err


# ---------------------------------------------- F4: --debug traceback is redacted

_LEAKED = "QQQQLEAKME1234"


def _raise_chained(outer):
    """Raise ``outer`` while handling an inner error that carries a secret."""

    def _fn(*args, **kwargs):
        try:
            raise ValueError(
                f"upstream said token={_LEAKED} for https://alice:{_LEAKED}@jira.example.com"
            )
        except ValueError as inner:
            raise outer from inner

    return _fn


def test_debug_traceback_includes_the_chained_cause(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise_chained(cli.JiraError("boom")))
    assert cli.main(["--debug", "mine"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "direct cause" in err


def test_debug_traceback_redacts_secrets_in_chained_causes(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise_chained(cli.JiraError("boom")))
    assert cli.main(["--debug", "mine"]) == cli.EXIT_ERROR
    assert _LEAKED not in capsys.readouterr().err


def test_debug_traceback_strips_terminal_escapes(capsys, env, monkeypatch):
    monkeypatch.setattr(
        cli.issues, "my_issues", _raise(RuntimeError("\x1b]52;c;cm0gLXJmIH4=\x07evil"))
    )
    assert cli.main(["--debug", "mine"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "\x1b" not in err
    assert "\x07" not in err


# ------------------------------------------------ F5: non-Jira exceptions in main()


def test_os_error_exits_with_the_io_code_and_one_error_line(capsys, env, monkeypatch):
    monkeypatch.setattr(
        cli.export,
        "write_report",
        _raise(PermissionError(13, "Permission denied", "/srv/out.md")),
    )
    assert cli.main(["report", "team", "--team", "backend", "--out", "/srv/out.md"]) == cli.EXIT_IO
    err = capsys.readouterr().err
    assert err.splitlines() == ["error: Permission denied: /srv/out.md"]


def test_plan_new_into_a_missing_directory_is_a_clean_io_error(capsys, env, tmp_path):
    target = tmp_path / "missing" / "plan.yaml"
    assert cli.main(yes("plan", "new", str(target))) == cli.EXIT_IO
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "Traceback" not in err
    assert str(target) in err


def test_io_exit_code_is_distinct_from_every_other_code():
    others = {cli.EXIT_OK, cli.EXIT_ERROR, cli.EXIT_CONFIG, cli.EXIT_AUTH, cli.EXIT_CANCELLED}
    assert cli.EXIT_IO == 5
    assert cli.EXIT_IO not in others


def test_os_error_message_is_redacted(capsys, env, monkeypatch):
    monkeypatch.setattr(
        cli.export,
        "write_report",
        _raise(OSError(5, "I/O error", f"reports/out.md?token={_LEAKED}")),
    )
    assert cli.main(["report", "team", "--team", "backend", "--out", "x.md"]) == cli.EXIT_IO
    assert _LEAKED not in capsys.readouterr().err


def test_os_error_prints_a_traceback_only_with_debug(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.export, "write_report", _raise(FileNotFoundError(2, "No such file", "x")))
    assert cli.main(["--debug", "report", "team", "--team", "backend", "--out", "x"]) == cli.EXIT_IO
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert err.rstrip().splitlines()[-1] == "error: No such file: x"


def test_unexpected_exception_is_one_line_without_its_message(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise(TypeError(f"secret {_LEAKED}")))
    assert cli.main(["mine"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "error: unexpected TypeError",
        "rerun with --debug for details",
    ]


def test_unexpected_exception_with_debug_shows_a_redacted_traceback(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise(TypeError(f"password={_LEAKED}")))
    assert cli.main(["--debug", "mine"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "TypeError" in err
    assert _LEAKED not in err


# ------------------------------------------- F1: server text never reaches raw

_OSC52 = "\x1b]52;c;cm0gLXJmIH4=\x07"


def test_error_line_strips_escape_sequences_from_server_text(capsys, env, monkeypatch):
    monkeypatch.setattr(cli.issues, "my_issues", _raise(cli.JiraError(f"bad{_OSC52}\x1b[2K\rx")))
    assert cli.main(["mine"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "\r" not in err
    assert err.splitlines()[0].startswith("error: bad")


def test_error_line_is_redacted(capsys):
    # _fail redacts on its own, whatever the exception's __str__ already did.
    assert cli._fail(f"server echoed JIRA_PAT={_LEAKED}", cli.EXIT_ERROR) == cli.EXIT_ERROR
    assert _LEAKED not in capsys.readouterr().err


def test_text_emit_strips_escapes_from_server_values(capsys, env):
    env.client._me = {"name": f"alice{_OSC52}", "displayName": "Alice\x9b31m"}
    assert cli.main(["me"]) == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "\x1b" not in out
    assert "\x9b" not in out


def test_json_emit_escapes_c1_controls(capsys, env):
    env.client._me = {"name": "alice\x9b31m", "displayName": "A\u202eB"}
    assert cli.main(["--json", "me"]) == 0
    raw = capsys.readouterr().out
    assert "\x9b" not in raw
    assert "\u202e" not in raw
    assert json.loads(raw) == {"name": "alice\x9b31m", "displayName": "A\u202eB"}


def test_confirmation_preview_cannot_be_rewritten_by_its_values(capsys, env):
    assert cli.main(yes("issue", "comment", "PROJ-1", f"ok{_OSC52}\x1b[2K\rfake\nline")) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\r" not in out
    assert "  ok" in out and "fake line" in out  # newline folded into one preview line


def test_doctor_details_strip_escapes(capsys, env, monkeypatch):
    env.client._server = {"version": f"10{_OSC52}", "deploymentType": "Server"}
    cli.main(["doctor"])
    assert "\x1b" not in capsys.readouterr().out


def test_issue_create_warnings_strip_escapes(capsys, env, monkeypatch):
    monkeypatch.setattr(
        cli.issues,
        "create_issue",
        lambda client, spec: {"key": "PROJ-9", "url": "u", "warnings": [f"w{_OSC52}"]},
    )
    assert cli.main(yes("issue", "create", "--project", "PROJ", "--summary", "s")) == 0
    assert "\x1b" not in capsys.readouterr().err


# ------------------------------------------------ F8: --json keeps stdout pure


def test_json_yes_write_prints_only_json_on_stdout(capsys, env):
    assert cli.main(["--json", "--yes", "issue", "delete", "PROJ-9"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # raises if any preview/prompt text leaked in
    assert "Delete PROJ-9" in captured.err
    assert "Proceed? [y/N]: y" in captured.err


def test_json_declined_write_leaves_stdout_empty(capsys, env, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["--json", "issue", "delete", "PROJ-9"]) == cli.EXIT_CANCELLED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Delete PROJ-9" in captured.err
    assert "Aborted, nothing changed." in captured.err


def test_json_prompt_is_written_to_stderr(capsys, env, monkeypatch):
    def fake_input(prompt=""):
        print(prompt, end="")  # like input(): the prompt goes to sys.stdout
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    assert cli.main(["--json", "issue", "comment", "PROJ-1", "hi"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "Proceed? [y/N]: " in captured.err


def test_json_report_out_prints_json_not_a_notice(capsys, env, tmp_path):
    out_file = tmp_path / "r.md"
    assert cli.main(["--json", "report", "team", "--team", "backend", "--out", str(out_file)]) == 0
    assert json.loads(capsys.readouterr().out) == {"written": "out.md"}


def test_text_mode_preview_stays_on_stdout(capsys, env):
    assert cli.main(yes("issue", "delete", "PROJ-9")) == 0
    out = capsys.readouterr().out
    assert "Delete PROJ-9" in out
    assert "Proceed? [y/N]: y" in out


# ====================================================================== reads


def test_me_prints_the_username(capsys, env):
    assert cli.main(["me"]) == 0
    assert "alice" in capsys.readouterr().out


def test_search_renders_a_table_of_issues(capsys, env):
    assert cli.main(["search", "project = PROJ", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "PROJ-1" in out
    assert "key" in out.splitlines()[0]
    assert env.one("search_issues")[1][1:] == ("project = PROJ", 5)


def test_json_flag_prints_parseable_json_and_no_table(capsys, env):
    assert cli.main(["--json", "search", "project = PROJ"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["key"] == "PROJ-1"


def test_json_flag_on_issue_show_prints_the_whole_issue(capsys, env):
    assert cli.main(["--json", "issue", "show", "proj-1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == "Fix the login screen"
    assert env.one("get_issue")[1][1] == "PROJ-1"  # key is upper-cased


def test_issue_show_without_json_prints_a_detail_block(capsys, env):
    assert cli.main(["issue", "show", "PROJ-1"]) == 0
    out = capsys.readouterr().out
    assert "PROJ-1  Fix the login screen" in out
    assert "status" in out


def test_mine_passes_include_done(env):
    assert cli.main(["mine", "--all"]) == 0
    assert env.one("my_issues")[2]["include_done"] is True


def test_team_list_reads_the_teams_file_from_settings(env):
    assert cli.main(["team", "list"]) == 0
    assert env.one("load_teams")[1][0] == Path(env.settings.teams_file)


def test_team_show_of_an_unknown_team_is_an_input_error(capsys, env):
    assert cli.main(["team", "show", "nope"]) == 1
    assert "no such team: nope" in capsys.readouterr().err


# ===================================================================== writes


def test_declined_confirmation_performs_no_write(capsys, env, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["issue", "comment", "PROJ-1", "hello"]) == cli.EXIT_CANCELLED
    assert "add_comment" not in env.names()
    captured = capsys.readouterr()
    assert captured.err.strip().splitlines()[-1] == "Aborted, nothing changed."
    assert "hello" in captured.out  # the preview was shown before the question


def test_eof_at_the_prompt_declines_the_write(capsys, env, monkeypatch):
    monkeypatch.setattr("builtins.input", _raise(EOFError()))
    assert cli.main(["issue", "delete", "PROJ-1"]) == cli.EXIT_CANCELLED
    assert "delete_issue" not in env.names()
    assert "Aborted, nothing changed." in capsys.readouterr().err


def test_cancelled_exit_code_is_distinct_from_every_other_code():
    others = {cli.EXIT_OK, cli.EXIT_ERROR, cli.EXIT_CONFIG, cli.EXIT_AUTH, cli.EXIT_IO}
    assert cli.EXIT_CANCELLED == 4
    assert cli.EXIT_CANCELLED not in others


@pytest.mark.parametrize(
    "argv",
    [
        ["projects", "create", "NEW", "New", "--lead", "alice"],
        ["sprint", "create", "12", "Sprint 6"],
        ["issue", "edit", "PROJ-1", "--summary", "x"],
        ["issue", "move", "PROJ-1", "Done"],
        ["team", "set", "backend", "--members", "alice"],
        ["filter", "delete", "mine"],
    ],
)
def test_every_kind_of_write_exits_cancelled_when_declined(capsys, env, monkeypatch, argv):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert cli.main(argv) == cli.EXIT_CANCELLED
    assert "Aborted, nothing changed." in capsys.readouterr().err


def test_accepted_confirmation_performs_the_write(env, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert cli.main(["issue", "comment", "PROJ-1", "hello"]) == 0
    assert env.one("add_comment")[1][1:] == ("PROJ-1", "hello")


def test_yes_flag_skips_the_prompt(env, monkeypatch):
    monkeypatch.setattr("builtins.input", _raise(AssertionError("must not prompt")))
    assert cli.main(yes("issue", "comment", "PROJ-1", "hi")) == 0
    assert env.one("add_comment")[1][1:] == ("PROJ-1", "hi")


def test_issue_create_builds_the_spec_from_flags(env):
    code = cli.main(
        yes(
            "issue", "create",
            "--project", "proj",
            "--summary", "Fix login",
            "--type", "Story",
            "--assignee", "alice",
            "--estimate", "3h",
            "--due", "2026-10-20",
            "--label", "backend",
            "--label", "ai",
            "--story-points", "3",
            "--sprint", "5",
        )
    )
    assert code == 0
    spec = env.one("create_issue")[1][1]
    assert (spec.project, spec.summary, spec.issue_type) == ("PROJ", "Fix login", "Story")
    assert spec.labels == ["backend", "ai"]
    assert (spec.original_estimate, spec.due_date, spec.story_points, spec.sprint_id) == (
        "3h",
        "2026-10-20",
        3.0,
        5,
    )


def test_issue_assign_none_unassigns(env):
    assert cli.main(yes("issue", "assign", "PROJ-1", "none")) == 0
    assert env.one("assign")[1][1:] == ("PROJ-1", None)


def test_issue_edit_without_a_field_flag_is_an_input_error(capsys, env):
    assert cli.main(yes("issue", "edit", "PROJ-1")) == 1
    assert "nothing to change" in capsys.readouterr().err


def test_issue_edit_maps_flags_to_contract_field_names(env):
    assert cli.main(yes("issue", "edit", "PROJ-1", "--due", "2026-01-01", "--remaining", "2h")) == 0
    assert env.one("update_fields")[1][2] == {"due_date": "2026-01-01", "remaining_estimate": "2h"}


def test_issue_estimate_needs_at_least_one_value(capsys, env):
    assert cli.main(yes("issue", "estimate", "PROJ-1")) == 1
    assert "--original" in capsys.readouterr().err


def test_issue_worklog_logs_time(env):
    assert cli.main(yes("issue", "worklog", "PROJ-1", "2h", "--started", "2026-10-01")) == 0
    assert env.one("add_worklog")[1][1:] == ("PROJ-1", "2h", "2026-10-01", None)


def test_sprint_add_upper_cases_keys(env):
    assert cli.main(yes("sprint", "add", "5", "proj-1", "proj-2")) == 0
    assert env.one("add_to_sprint")[1][1:] == (5, ["PROJ-1", "PROJ-2"])


def test_team_set_parses_members_projects_and_boards(env):
    assert cli.main(
        yes("team", "set", "backend", "--members", "alice,bob", "--projects", "PROJ,OPS", "--boards", "12,13")
    ) == 0
    args = env.one("save_team")[1]
    assert args[2:] == (["alice", "bob"], ["PROJ", "OPS"], [12, 13])


def test_team_set_rejects_a_non_numeric_board_id(capsys, env):
    assert cli.main(yes("team", "set", "backend", "--members", "alice", "--boards", "twelve")) == 1
    assert "not a number: twelve" in capsys.readouterr().err


def test_plan_new_refuses_to_overwrite(capsys, env, tmp_path):
    target = tmp_path / "plan.yaml"
    target.write_text("existing", encoding="utf-8")
    assert cli.main(yes("plan", "new", str(target))) == 1
    assert "already exists" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "existing"


def test_plan_new_writes_the_template(env, tmp_path):
    target = tmp_path / "plan.yaml"
    assert cli.main(yes("plan", "new", str(target))) == 0
    assert "plan template" in target.read_text(encoding="utf-8")


def test_plan_validate_exits_one_when_the_plan_has_errors(capsys, env, monkeypatch):
    monkeypatch.setattr(
        cli.plan,
        "validate_plan",
        lambda client, plan: {
            "ok": False,
            "errors": [{"ref": "t1", "field": "assignee", "message": "unknown"}],
            "warnings": [],
        },
    )
    assert cli.main(["plan", "validate", "plan.yaml"]) == 1
    assert "t1: assignee unknown" in capsys.readouterr().out


def test_plan_apply_dry_run_never_applies_for_real(env):
    assert cli.main(["plan", "apply", "plan.yaml", "--dry-run"]) == 0
    dry_runs = [call[2].get("dry_run") for call in env.calls if call[0] == "apply_plan"]
    assert dry_runs == [True]


def test_plan_apply_declined_does_not_apply_for_real(env, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["plan", "apply", "plan.yaml"]) == cli.EXIT_CANCELLED
    dry_runs = [call[2].get("dry_run") for call in env.calls if call[0] == "apply_plan"]
    assert dry_runs == [True]  # only the preview run happened


def test_plan_apply_confirmed_applies_once_for_real(env):
    assert cli.main(yes("plan", "apply", "plan.yaml")) == 0
    dry_runs = [call[2].get("dry_run") for call in env.calls if call[0] == "apply_plan"]
    assert dry_runs == [True, False]


# ==================================================================== reports


def test_report_team_resolves_a_team_to_its_members(env):
    assert cli.main(["report", "team", "--team", "backend", "--since", "14d"]) == 0
    args = env.one("team_dashboard")[1]
    assert args[1] == ["alice", "bob"]
    assert args[3] == "14d"


def test_report_team_uses_the_teams_projects_when_none_given(env):
    assert cli.main(["report", "team", "--team", "backend"]) == 0
    assert env.one("team_dashboard")[1][2] == ["PROJ"]


def test_explicit_projects_beat_the_teams_projects(env):
    assert cli.main(["report", "team", "--team", "backend", "--projects", "OPS,WEB"]) == 0
    assert env.one("team_dashboard")[1][2] == ["OPS", "WEB"]


def test_report_users_flag_takes_a_comma_list(env):
    assert cli.main(["report", "workload", "--users", "alice,bob"]) == 0
    assert env.one("workload")[1][1] == ["alice", "bob"]


def test_report_user_flag_takes_one_username(env):
    assert cli.main(["report", "person", "--user", "alice"]) == 0
    assert env.one("person_activity")[1][1] == "alice"


def test_report_that_needs_people_fails_without_them(capsys, env):
    assert cli.main(["report", "team"]) == 1
    assert "--team" in capsys.readouterr().err


def test_report_unassigned_needs_projects(capsys, env):
    assert cli.main(["report", "unassigned"]) == 1
    assert "--projects" in capsys.readouterr().err


def test_report_sprint_needs_a_board(capsys, env):
    assert cli.main(["report", "sprint"]) == 1
    assert "--board" in capsys.readouterr().err


def test_report_velocity_passes_board_and_last(env):
    assert cli.main(["report", "velocity", "--board", "12", "--last", "3"]) == 0
    assert env.one("velocity")[1][1] == 12
    assert env.one("velocity")[2]["last"] == 3


def test_report_stale_passes_days(env):
    assert cli.main(["report", "stale", "--days", "21"]) == 0
    assert env.one("stale_issues")[2]["days"] == 21


def test_report_format_table_is_the_default(capsys, env):
    assert cli.main(["report", "team", "--team", "backend"]) == 0
    out = capsys.readouterr().out
    assert "Team dashboard" in out
    assert "alice" in out
    assert "{" not in out


def test_report_format_csv_prints_csv(capsys, env):
    assert cli.main(["report", "team", "--team", "backend", "--format", "csv"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "user,open"
    assert lines[1] == "alice,3"


def test_report_format_json_prints_the_report_document(capsys, env):
    assert cli.main(["report", "team", "--team", "backend", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["title"] == "Team dashboard"
    assert payload["summary"] == {"users": 2}


def test_report_format_md_prints_a_markdown_table(capsys, env):
    assert cli.main(["report", "team", "--team", "backend", "--format", "md"]) == 0
    out = capsys.readouterr().out
    assert "# Team dashboard" in out
    assert "| user | open |" in out


def test_json_flag_makes_a_report_json(capsys, env):
    assert cli.main(["--json", "report", "team", "--team", "backend"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Team dashboard"


def test_report_out_routes_to_export_write_report(capsys, env, tmp_path):
    target = tmp_path / "team.md"
    assert cli.main(["report", "team", "--team", "backend", "--format", "md", "--out", str(target)]) == 0
    name, args, _kwargs = env.one("write_report")
    assert args[0]["title"] == "Team dashboard"
    assert args[1] == target
    assert args[2] == "md"
    assert "written:" in capsys.readouterr().out


def test_report_out_without_format_lets_export_infer_from_the_suffix(env, tmp_path):
    assert cli.main(["report", "team", "--team", "backend", "--out", str(tmp_path / "t.csv")]) == 0
    assert env.one("write_report")[1][2] is None


def test_export_rerenders_a_saved_json_report(capsys, env, tmp_path):
    source = tmp_path / "report.json"
    source.write_text(json.dumps(make_report()), encoding="utf-8")
    assert cli.main(["export", str(source), "--format", "csv"]) == 0
    assert "user,open" in capsys.readouterr().out


def test_export_rejects_a_file_that_is_not_a_report(capsys, env, tmp_path):
    source = tmp_path / "x.json"
    source.write_text('{"hello": 1}', encoding="utf-8")
    assert cli.main(["export", str(source)]) == 1
    assert "does not look like a report" in capsys.readouterr().err


def test_export_reports_a_missing_file_as_an_input_error(capsys, env, tmp_path):
    assert cli.main(["export", str(tmp_path / "missing.json")]) == 1
    assert "cannot read" in capsys.readouterr().err


# ===================================================================== doctor


def test_doctor_passes_everything_on_a_healthy_server(capsys, env):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "PASS   serverInfo" in out
    assert "10.3.0" in out
    assert "PASS   myself" in out
    assert "FAIL" not in out


def test_doctor_warns_about_a_missing_field_id(capsys, env):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "WARN   field.epic_link" in out
    assert "JIRA_FIELD_EPIC_LINK" in out


def test_doctor_warns_in_read_only_mode(capsys, env, monkeypatch):
    monkeypatch.setattr(
        cli.config_module, "load_settings", lambda **kw: StubSettings(read_only=True)
    )
    assert cli.main(["doctor"]) == 0
    assert "WARN   read_only" in capsys.readouterr().out


def test_doctor_fails_and_exits_nonzero_when_auth_is_broken(capsys, env, monkeypatch):
    monkeypatch.setattr(cli, "JiraClient", lambda s: FakeClient(fail="myself"))
    assert cli.main(["doctor"]) != 0
    out = capsys.readouterr().out
    assert "FAIL   myself" in out
    assert "1 failed" in out


def test_doctor_fails_when_server_info_is_unreachable(capsys, env, monkeypatch):
    monkeypatch.setattr(cli, "JiraClient", lambda s: FakeClient(fail="server"))
    assert cli.main(["doctor"]) == 1
    assert "FAIL   serverInfo" in capsys.readouterr().out


def test_doctor_checks_the_env_file_permission(capsys, env, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("JIRA_BASE_URL=https://jira.example.com\n", encoding="utf-8")
    env_file.chmod(0o644)
    assert cli.main(["--env-file", str(env_file), "doctor"]) == 0
    out = capsys.readouterr().out
    assert str(env_file) in out
    assert "chmod 600" in out


def test_doctor_json_lists_every_check(capsys, env):
    assert cli.main(["--json", "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    checks = {item["check"] for item in payload["checks"]}
    assert {"settings", "serverInfo", "myself", "read_only", "field.sprint"} <= checks


# =============================================== menu, mcp and lazy importing


def test_no_subcommand_runs_the_interactive_menu(env, monkeypatch):
    """Routing only: the real menu's ``run`` is replaced, so stdin is never read."""
    from jira_cli.cli import menu

    seen: list[tuple] = []
    monkeypatch.setattr(
        menu, "run", lambda client, settings: seen.append((client, settings)) or 0
    )
    monkeypatch.setattr("builtins.input", _raise(AssertionError("must not read stdin")))
    assert cli.main([]) == 0
    assert seen and seen[0][1] is env.settings


def test_mcp_subcommand_runs_the_mcp_server(env, monkeypatch):
    """Routing only: the real server's ``main`` is replaced, so no stdio server starts."""
    from jira_cli import mcp_server

    calls: list[int] = []
    monkeypatch.setattr(mcp_server, "main", lambda: calls.append(1) or 0)
    assert cli.main(["mcp"]) == 0
    assert calls == [1]


def test_importing_the_cli_package_pulls_in_neither_menu_nor_mcp():
    """``jira_cli.cli`` must stay side-effect free (CONTRACT section 4).

    Checked in a fresh interpreter, so this process's module cache is untouched.
    """
    probe = (
        "import sys, jira_cli.cli; "
        "print(sorted(n for n in ('jira_cli.cli.menu', 'jira_cli.mcp_server') "
        "if n in sys.modules))"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=60,
    )
    assert result.stdout.strip() == "[]"


def test_there_is_no_dunder_main_module_owned_by_the_cli():
    """``jira_cli/cli`` never adds a ``__main__``: the console script is the entry."""
    assert not (Path(cli.__file__).resolve().parent / "__main__.py").exists()


# ===================================================== output: colour and width


def test_no_color_flag_strips_ansi(capsys, env):
    assert cli.main(["--no-color", "doctor"]) == 0
    assert "\033[" not in capsys.readouterr().out


def test_color_is_off_when_stdout_is_not_a_tty():
    output.set_color(None)
    assert output.colors_enabled() is False
    assert output.color("PASS", "green") == "PASS"


def test_no_color_env_var_wins_over_a_tty(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    output.set_color(None)
    assert output.colors_enabled() is False


def test_color_is_on_for_a_tty_without_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    output.set_color(None)
    assert output.color("PASS", "green") == "\033[32mPASS\033[0m"


def test_forced_color_wraps_the_text():
    output.set_color(True)
    assert output.color("x", "red") == "\033[31mx\033[0m"


@pytest.mark.parametrize(
    ("text", "width"),
    [
        ("abc", 3),
        ("", 0),
        ("\u4f60\u597d", 4),           # two wide CJK ideographs
        ("\u0633\u0644\u0627\u0645", 4),  # Persian/Arabic letters are single-width
        ("\u0633\u064e\u0644\u0627\u0645", 4),  # a combining fatha adds no cell
    ],
)
def test_display_width_counts_cells_not_characters(text, width):
    assert output.display_width(text) == width


def _cell_offset(line: str, marker: str) -> int:
    """Terminal-cell offset at which ``marker`` starts in ``line``.

    Splitting on the column gap cannot be used: padding runs contain the gap
    string, so the offset is measured from the real prefix instead.
    """
    index = line.rindex(marker)
    return output.display_width(line[:index])


def test_render_table_aligns_rows_of_equal_display_width():
    """A wide CJK cell must not push the next column out of line."""
    rows = [
        {"name": "alice", "note": "\u4f60\u597d", "tail": "x"},
        {"name": "bob", "note": "hi", "tail": "y"},
    ]
    lines = output.render_table(["name", "note", "tail"], rows, max_width=40).splitlines()
    first, second = lines[2], lines[3]
    assert output.display_width(first) == output.display_width(second)
    assert _cell_offset(first, "x") == _cell_offset(second, "y")


def test_render_table_keeps_persian_rows_aligned():
    rows = [
        {"key": "PROJ-1", "summary": "\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627", "tail": "a"},
        {"key": "PROJ-22", "summary": "plain ascii", "tail": "b"},
    ]
    body = output.render_table(["key", "summary", "tail"], rows, max_width=60).splitlines()[2:]
    assert len({output.display_width(line) for line in body}) == 1
    assert _cell_offset(body[0], "a") == _cell_offset(body[1], "b")


def test_render_table_truncates_with_an_ellipsis_to_fit_the_width():
    rows = [{"summary": "a very long summary that will never fit"}]
    rendered = output.render_table(["summary"], rows, max_width=12)
    cell = rendered.splitlines()[2]
    assert output.display_width(cell) <= 12
    assert cell.endswith(output.ELLIPSIS)


def test_render_table_marks_an_empty_result():
    assert "(no rows)" in output.render_table(["key"], [])


def test_render_table_renders_none_as_a_dash():
    assert "-" in output.render_table(["due"], [{"due": None}]).splitlines()[2]


def test_render_table_flattens_lists_and_newlines():
    rendered = output.render_table(
        ["labels", "summary"], [{"labels": ["a", "b"], "summary": "one\ntwo"}], max_width=60
    )
    assert "a, b" in rendered
    assert "one two" in rendered


def test_truncate_of_a_wide_string_never_exceeds_the_budget():
    assert output.display_width(output.truncate("\u4f60\u597d\u4e16\u754c", 5)) <= 5


def test_render_report_rejects_an_unknown_format():
    with pytest.raises(ValueError, match="unknown report format"):
        output.render_report(make_report(), "xml")


def test_render_report_table_includes_the_summary():
    rendered = output.render_report(make_report(), "table")
    assert "Team dashboard" in rendered
    assert "users" in rendered


def test_print_issue_writes_the_key_summary_and_url(capsys):
    output.set_color(False)
    output.print_issue(dict(ISSUE, comments=[{"author": "bob", "created": "t", "body": "ok"}]))
    out = capsys.readouterr().out
    assert "PROJ-1  Fix the login screen" in out
    assert "https://jira.example.com/browse/PROJ-1" in out
    assert "bob" in out


def test_confirm_with_assume_yes_never_reads_stdin(capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", _raise(AssertionError("must not prompt")))
    assert output.confirm("Proceed?", True) is True
    assert "y" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("y", True), ("Y", True), ("yes", True), ("n", False), ("", False), ("nope", False)],
)
def test_confirm_only_accepts_an_explicit_yes(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert output.confirm("Proceed?", False) is expected


def test_confirm_treats_an_interrupt_as_no(monkeypatch):
    monkeypatch.setattr("builtins.input", _raise(KeyboardInterrupt()))
    assert output.confirm("Proceed?", False) is False
