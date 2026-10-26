"""Tests for the report menu sections (``jira_cli.cli.menu_reports``).

Offline: a scripted IO replaces the terminal, ``jira_cli.cli.menu_people`` and the
report services are fakes installed in ``sys.modules``, and "today" is pinned.
"""

from __future__ import annotations

import sys
import types
from datetime import date

import pytest

from jira_cli.cli import menu_reports
from jira_cli.cli.menu import _Back
from jira_cli.errors import JiraError

TODAY = date(2026, 10, 26)


class ScriptedIO:
    """Answers come from a script of ``(kind, value)`` pairs, in order."""

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

    def prompts(self) -> list[str]:
        return [entry[1] for entry in self.asked if entry[0] == "ask"]


class Session:
    """Just the attributes the sections and ``_present_report`` use."""

    def __init__(self, io):
        self.client = object()
        self.settings = types.SimpleNamespace(default_project="PROJ")
        self.io = io
        self.last_report = None

    def say(self, text):
        self.io.show(str(text))


class FakePickers:
    """Fake ``menu_people``: each picker returns the next queued answer.

    A queued ``_Back`` is raised, as the real picker does when the user types b.
    """

    def __init__(self, projects=(), people=(), persons=()):
        self.queues = {"projects": list(projects), "people": list(people), "person": list(persons)}
        self.calls: list[tuple] = []

    def _next(self, name, kwargs):
        self.calls.append((name, kwargs))
        if not self.queues[name]:
            raise AssertionError(f"no queued answer for {name}")
        value = self.queues[name].pop(0)
        if value is _Back:
            raise _Back
        return value

    def module(self):
        return types.SimpleNamespace(
            ask_projects=lambda session, title="Projects", **kw: self._next("projects", kw),
            ask_people=lambda session, title, **kw: self._next("people", kw),
            ask_person=lambda session, title, **kw: self._next("person", kw),
        )

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)


def _report(title):
    return {"title": title, "columns": ["a"], "rows": [], "summary": {}, "meta": {}}


@pytest.fixture
def env(monkeypatch):
    """Install fake services; returns ``(calls, install_pickers, reports_module)``."""
    calls: list[tuple] = []

    def record(name):
        def _fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return _report(name)

        return _fn

    reports = types.ModuleType("jira_cli.services.reports")
    for name in (
        "team_dashboard",
        "person_activity",
        "stale_issues",
        "overdue_issues",
        "incomplete_issues",
        "unassigned_issues",
        "workload",
    ):
        setattr(reports, name, record(name))
    worklog = types.ModuleType("jira_cli.services.worklog")
    worklog.worklog_report = record("worklog_report")
    monkeypatch.setitem(sys.modules, "jira_cli.services.reports", reports)
    monkeypatch.setitem(sys.modules, "jira_cli.services.worklog", worklog)
    monkeypatch.setattr(menu_reports, "_today", lambda: TODAY)

    def install(pickers: FakePickers) -> FakePickers:
        monkeypatch.setitem(sys.modules, "jira_cli.cli.menu_people", pickers.module())
        return pickers

    return calls, install, reports


LEAVE_REPORT = ("ask", "b")


def _only_call(calls, name):
    matching = [call for call in calls if call[0] == name]
    assert len(matching) == 1, calls
    return matching[0]


# ------------------------------------------------------------- team dashboard


def test_team_dashboard_dedupes_people_case_insensitively(env):
    calls, install, _ = env
    install(FakePickers(projects=[["PROJ"]], people=[["alice", "ALICE", "bob"]]))
    io = ScriptedIO([("ask", ""), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    _, args, kwargs = _only_call(calls, "team_dashboard")
    assert args[1] == ["alice", "bob"]
    assert kwargs == {"projects": ["PROJ"], "since": "2026-10-19"}


def test_team_dashboard_focuses_people_picker_on_first_project(env):
    _, install, _ = env
    pickers = install(FakePickers(projects=[["PROJ", "OPS"]], people=[["alice"]]))
    io = ScriptedIO([("ask", ""), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    assert pickers.calls[1] == ("people", {"project": "PROJ"})


def test_team_dashboard_all_projects_reaches_service_as_none(env):
    calls, install, _ = env
    pickers = install(FakePickers(projects=[None], people=[["alice"]]))
    io = ScriptedIO([("ask", "2w"), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    _, _, kwargs = _only_call(calls, "team_dashboard")
    assert kwargs["projects"] is None
    assert pickers.calls[1] == ("people", {"project": None})
    assert "Covers projects: all projects | from 2026-10-12 to today" in io.text


def test_header_names_projects_window_and_people(env):
    _, install, _ = env
    install(FakePickers(projects=[["PROJ", "OPS"]], people=[["alice", "bob"]]))
    io = ScriptedIO([("ask", "2026-10-01"), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    assert "Covers projects: PROJ, OPS | from 2026-10-01 to today | people: alice, bob" in io.shown


def test_since_prompt_lists_accepted_formats(env):
    _, install, _ = env
    install(FakePickers(projects=[["PROJ"]], people=[["alice"]]))
    io = ScriptedIO([("ask", ""), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    assert ("ask", "Since (7d, 2w, 1m, today, yesterday, YYYY-MM-DD)", "7d") in io.asked


def test_bad_since_is_reasked_without_losing_people(env):
    calls, install, _ = env
    pickers = install(FakePickers(projects=[["PROJ"]], people=[["alice"]]))
    io = ScriptedIO([("ask", "3x"), ("ask", "2026-13-45"), ("ask", "1m"), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    assert "cannot read date '3x'" in io.text
    assert "not a real date" in io.text
    assert pickers.count("people") == 1
    assert _only_call(calls, "team_dashboard")[2]["since"] == "2026-09-26"


def test_future_since_is_reasked_with_reason(env):
    calls, install, _ = env
    install(FakePickers(projects=[["PROJ"]], people=[["alice"]]))
    io = ScriptedIO([("ask", "2026-10-27"), ("ask", "today"), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    assert "since 2026-10-27 is in the future" in io.text
    assert _only_call(calls, "team_dashboard")[2]["since"] == "2026-10-26"


def test_back_at_since_returns_to_people_picker(env):
    calls, install, _ = env
    pickers = install(FakePickers(projects=[["PROJ"]], people=[["alice"], ["bob"]]))
    io = ScriptedIO([("ask", "b"), ("ask", ""), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    assert pickers.count("people") == 2
    assert _only_call(calls, "team_dashboard")[1][1] == ["bob"]


def test_back_at_first_prompt_leaves_section(env):
    calls, install, _ = env
    install(FakePickers(projects=[_Back]))

    with pytest.raises(_Back):
        menu_reports.run_team_dashboard(Session(ScriptedIO([])))
    assert calls == []


def test_back_at_people_returns_to_projects(env):
    calls, install, _ = env
    pickers = install(FakePickers(projects=[["PROJ"], ["OPS"]], people=[_Back, ["alice"]]))
    io = ScriptedIO([("ask", ""), LEAVE_REPORT])

    menu_reports.run_team_dashboard(Session(io))

    assert pickers.count("projects") == 2
    assert _only_call(calls, "team_dashboard")[2]["projects"] == ["OPS"]


# ------------------------------------------------------------ person activity


def test_person_activity_uses_person_picker_for_first_project(env):
    calls, install, _ = env
    pickers = install(FakePickers(projects=[["OPS", "PROJ"]], persons=["carol"]))
    io = ScriptedIO([("ask", "3d"), ("ask", ""), LEAVE_REPORT])

    menu_reports.run_person_activity(Session(io))

    assert pickers.calls[1] == ("person", {"project": "OPS"})
    _, args, kwargs = _only_call(calls, "person_activity")
    assert args[1] == "carol"
    assert kwargs == {"since": "2026-10-23", "until": None, "projects": ["OPS", "PROJ"]}


def test_person_activity_until_before_since_is_reasked(env):
    calls, install, _ = env
    install(FakePickers(projects=[["PROJ"]], persons=["carol"]))
    io = ScriptedIO([("ask", "2026-10-20"), ("ask", "2026-10-01"), ("ask", "yesterday"), LEAVE_REPORT])

    menu_reports.run_person_activity(Session(io))

    assert "until 2026-10-01 is before since 2026-10-20" in io.text
    assert _only_call(calls, "person_activity")[2]["until"] == "2026-10-25"


def test_person_activity_server_error_reoffers_person_and_keeps_projects(env):
    calls, install, reports = env
    pickers = install(FakePickers(projects=[["PROJ"]], persons=["alice", "bob"]))
    good = reports.person_activity

    def flaky(client, username, **kwargs):
        if username == "alice":
            raise JiraError("The value 'alice' does not exist\nfor the field 'assignee'.", status=400)
        return good(client, username, **kwargs)

    reports.person_activity = flaky
    io = ScriptedIO([("ask", ""), ("ask", ""), ("ask", ""), ("ask", ""), LEAVE_REPORT])

    menu_reports.run_person_activity(Session(io))

    assert pickers.count("projects") == 1
    assert pickers.count("person") == 2
    errors = [line for line in io.shown if line.startswith("error:")]
    assert errors == ["error: The value 'alice' does not exist for the field 'assignee'. (HTTP 400)"]
    assert _only_call(calls, "person_activity")[1][1] == "bob"


# -------------------------------------------------------------------- problems


@pytest.mark.parametrize("bad", ["0", "3651", "-1", "abc"])
def test_stale_days_out_of_range_is_reasked(env, bad):
    calls, install, _ = env
    install(FakePickers(projects=[["PROJ"]]))
    io = ScriptedIO([("choose", 0), ("ask", bad), ("ask", "30d"), LEAVE_REPORT])

    menu_reports.run_problems(Session(io))

    assert "days must be" in io.text
    _, _, kwargs = _only_call(calls, "stale_issues")
    assert kwargs == {"days": 30, "projects": ["PROJ"]}


def test_stale_days_upper_bound_is_accepted(env):
    calls, install, _ = env
    install(FakePickers(projects=[None]))
    io = ScriptedIO([("choose", 0), ("ask", "3650"), LEAVE_REPORT])

    menu_reports.run_problems(Session(io))

    assert _only_call(calls, "stale_issues")[2] == {"days": 3650, "projects": None}
    assert "Covers projects: all projects | not updated for 3650+ days" in io.shown


def test_back_at_problem_projects_returns_to_problem_list(env):
    calls, install, _ = env
    install(FakePickers(projects=[_Back, ["PROJ"]]))
    io = ScriptedIO([("choose", 1), ("choose", 2), LEAVE_REPORT])

    menu_reports.run_problems(Session(io))

    assert [call[0] for call in calls] == ["incomplete_issues"]


def test_back_at_problem_list_leaves_section(env):
    _, install, _ = env
    install(FakePickers())

    with pytest.raises(_Back):
        menu_reports.run_problems(Session(ScriptedIO([("choose", None)])))


def test_unassigned_accepts_all_projects(env):
    calls, install, _ = env
    install(FakePickers(projects=[None]))
    io = ScriptedIO([("choose", 3), LEAVE_REPORT])

    menu_reports.run_problems(Session(io))

    assert _only_call(calls, "unassigned_issues")[1][1] is None


def test_overdue_passes_chosen_projects(env):
    calls, install, _ = env
    install(FakePickers(projects=[["PROJ", "proj"]]))
    io = ScriptedIO([("choose", 1), LEAVE_REPORT])

    menu_reports.run_problems(Session(io))

    assert _only_call(calls, "overdue_issues")[2] == {"projects": ["PROJ"]}


# -------------------------------------------------------------------- workload


def test_workload_uses_picked_people_and_projects(env):
    calls, install, _ = env
    install(FakePickers(projects=[["PROJ"]], people=[["bob", "Bob", "carol"]]))
    io = ScriptedIO([("choose", 0), LEAVE_REPORT])

    menu_reports.run_workload(Session(io))

    _, args, kwargs = _only_call(calls, "workload")
    assert args[1] == ["bob", "carol"]
    assert kwargs == {"projects": ["PROJ"]}


def test_worklog_report_passes_validated_window(env):
    calls, install, _ = env
    install(FakePickers(projects=[None], people=[["alice"]]))
    io = ScriptedIO([("choose", 1), ("ask", "2w"), ("ask", "2026-10-20"), LEAVE_REPORT])

    menu_reports.run_workload(Session(io))

    _, args, kwargs = _only_call(calls, "worklog_report")
    assert args[1] == ["alice"]
    assert kwargs == {"since": "2026-10-12", "until": "2026-10-20", "projects": None}
    assert "Covers projects: all projects | from 2026-10-12 to 2026-10-20 | people: alice" in io.shown


def test_back_at_workload_people_returns_to_projects_then_submenu(env):
    calls, install, _ = env
    install(FakePickers(projects=[["PROJ"], _Back, ["OPS"]], people=[_Back, ["alice"]]))
    io = ScriptedIO([("choose", 0), ("choose", 0), LEAVE_REPORT])

    menu_reports.run_workload(Session(io))

    assert _only_call(calls, "workload")[2] == {"projects": ["OPS"]}


def test_refresh_shows_header_again(env):
    calls, install, _ = env
    install(FakePickers(projects=[["PROJ"]], people=[["alice"]]))
    io = ScriptedIO([("choose", 0), ("ask", "r"), LEAVE_REPORT])

    menu_reports.run_workload(Session(io))

    assert len([call for call in calls if call[0] == "workload"]) == 2
    assert io.shown.count("Covers projects: PROJ | people: alice") == 2
