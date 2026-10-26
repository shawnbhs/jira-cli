"""Tests for the interactive main menu (``jira_cli.cli.menu``).

``menu`` owns the main menu, the runner that keeps the session alive, and the
shared prompt helpers.  Each section's own behaviour is tested in
``tests/test_menu_<section>.py``; here every section function is replaced by a
recorder, so these tests only prove that the right section runs and that the
runner survives whatever the section does.

Everything is offline: a scripted :class:`ScriptedIO` replaces the terminal and the
one service module the menu itself calls is a recording fake in ``sys.modules``.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from jira_cli.cli import (
    menu,
    menu_actions,
    menu_boards,
    menu_create,
    menu_find,
    menu_members,
    menu_plan,
    menu_reports,
    menu_settings,
    menu_sprint,
    menu_teams,
)
from jira_cli.errors import InputError, JiraError

# --------------------------------------------------------------------- scripted IO

_INTERRUPT = object()
_EOF = object()


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
        if value is _INTERRUPT:
            raise KeyboardInterrupt
        if value is _EOF:
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


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


ISSUE = {"key": "PROJ-1", "summary": "An issue", "status": "To Do", "assignee": "alice"}


@pytest.fixture
def env(monkeypatch):
    """Fake ``services.issues`` and plain-text rendering; returns ``(calls, client, settings)``."""
    calls: list[tuple] = []

    def my_issues(client, include_done=False):
        calls.append(("my_issues", include_done))
        return [dict(ISSUE)]

    fakes = {
        "jira_cli.services.issues": _module("jira_cli.services.issues", my_issues=my_issues),
        "jira_cli.cli.output": _module(
            "jira_cli.cli.output",
            render_report=lambda rep, fmt: f"[report {rep['title']} rows={len(rep['rows'])}]",
            render_table=lambda columns, rows: f"[table {len(rows)} rows]",
        ),
    }
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)
    settings = types.SimpleNamespace(base_url="https://jira.example.com", default_project="PROJ")
    return calls, object(), settings


def names(calls) -> list[str]:
    return [call[0] for call in calls]


# ------------------------------------------------------------- main-menu mapping

#: Every main-menu number, the section module it opens, and the function it calls.
SECTIONS = [
    ("2", menu_reports, "run_team_dashboard"),
    ("3", menu_reports, "run_person_activity"),
    ("4", menu_reports, "run_problems"),
    ("5", menu_reports, "run_workload"),
    ("6", menu_sprint, "run"),
    ("7", menu_boards, "run_overview"),
    ("8", menu_find, "run"),
    ("9", menu_create, "run"),
    ("10", menu_actions, "run"),
    ("11", menu_plan, "run"),
    ("12", menu_teams, "run"),
    ("13", menu_boards, "run"),
    ("14", menu_settings, "run_filters"),
    ("15", menu_settings, "run_export_last"),
    ("16", menu_settings, "run_settings"),
    ("17", menu_members, "run"),
]

EXPECTED_MENU = [
    "1. My issues",
    "2. Team dashboard",
    "3. Person activity",
    "4. Problems (stale / overdue / incomplete / unassigned)",
    "5. Workload and worklogs",
    "6. Sprint report and velocity",
    "7. Boards overview",
    "8. Find issue (key, JQL or saved filter)",
    "9. Create issue (guided)",
    "10. Issue actions",
    "11. Plan files",
    "12. Teams",
    "13. Projects and boards",
    "14. Saved filters",
    "15. Export last report",
    "16. Settings / doctor",
    "17. Project members",
]


def _short(module: types.ModuleType) -> str:
    return module.__name__.rsplit(".", 1)[-1]


@pytest.fixture
def recorded(monkeypatch):
    """Replace every section function with a recorder; returns the list of calls."""
    seen: list[tuple[str, str, object]] = []
    for _key, module, function in SECTIONS:
        short = _short(module)

        def recorder(session, _m=short, _f=function):
            seen.append((_m, _f, session))

        monkeypatch.setattr(module, function, recorder)
    return seen


@pytest.mark.parametrize(("key", "module", "function"), SECTIONS, ids=[s[0] for s in SECTIONS])
def test_each_main_menu_number_runs_its_section(env, recorded, key, module, function):
    _calls, client, settings = env
    io = ScriptedIO([("ask", key), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0

    assert [(m, f) for m, f, _session in recorded] == [(_short(module), function)]
    session = recorded[0][2]
    assert session.client is client and session.settings is settings and session.io is io


def test_main_menu_text_shows_every_item_in_order():
    lines = [line.strip() for line in menu.main_menu_text().splitlines() if line.strip()]

    assert lines == ["jira-cli - main menu", *EXPECTED_MENU, "q. quit"]


def test_every_listed_number_has_a_handler():
    assert sorted(menu._MAIN_ACTIONS, key=int) == [str(n) for n in range(1, 18)]


def test_one_session_is_shared_by_every_section(env, recorded):
    _calls, client, settings = env
    io = ScriptedIO([("ask", "8"), ("ask", "10"), ("ask", "q")])

    menu.run(client, settings, io=io)

    assert recorded[0][2] is recorded[1][2]


def test_importing_menu_does_not_import_any_section():
    """Sections import helpers from ``menu``; ``menu`` must load them only on use."""
    code = (
        "import sys, jira_cli.cli.menu\n"
        "print(sorted(m for m in sys.modules if m.startswith('jira_cli.cli.menu_')))\n"
    )
    repo = Path(__file__).resolve().parents[1]
    out = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a literal script
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=repo
    )

    assert out.stdout.strip() == "[]"


# ------------------------------------------------------------ main-menu answers


@pytest.mark.parametrize("word", ["q", "quit", "exit", "Q"])
def test_quit_words_end_the_menu_with_exit_code_zero(env, word):
    _calls, client, settings = env
    io = ScriptedIO([("ask", word)])

    assert menu.run(client, settings, io=io) == 0
    assert "main menu" in io.text


def test_invalid_choice_is_rejected_without_quitting(env, recorded):
    _calls, client, settings = env
    io = ScriptedIO([("ask", "99"), ("ask", "18"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert "invalid choice: 99" in io.text
    assert "invalid choice: 18" in io.text
    assert recorded == []


@pytest.mark.parametrize("answer", ["", "b", "!back"])
def test_empty_or_back_at_the_main_menu_shows_it_again(env, recorded, answer):
    _calls, client, settings = env
    io = ScriptedIO([("ask", answer), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert io.text.count("main menu") == 2
    assert "invalid choice" not in io.text


@pytest.mark.parametrize("closed", [_INTERRUPT, _EOF], ids=["ctrl-c", "eof"])
def test_ctrl_c_or_closed_input_at_the_main_menu_quits(env, closed):
    _calls, client, settings = env
    io = ScriptedIO([("ask", closed)])

    assert menu.run(client, settings, io=io) == 0


# ------------------------------------------------- the runner survives a section


def _raise_in_section(monkeypatch, exc):
    def section(session):
        raise exc

    monkeypatch.setattr(menu_find, "run", section)


def test_back_from_a_section_returns_to_the_main_menu(env, monkeypatch):
    _calls, client, settings = env
    _raise_in_section(monkeypatch, menu._Back())
    io = ScriptedIO([("ask", "8"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert io.text.count("main menu") == 2
    assert "error" not in io.text


@pytest.mark.parametrize(
    "exc",
    [JiraError("permission denied"), InputError("permission denied")],
    ids=["JiraError", "InputError"],
)
def test_jira_and_input_errors_print_one_line_and_keep_the_menu(env, monkeypatch, exc):
    calls, client, settings = env
    _raise_in_section(monkeypatch, exc)
    io = ScriptedIO([("ask", "8"), ("ask", "1"), ("confirm", False), ("ask", "b"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert "error: permission denied" in io.text
    assert names(calls) == ["my_issues"], "the menu is still alive afterwards"


def test_error_text_from_the_server_is_made_terminal_safe(env, monkeypatch):
    _calls, client, settings = env
    _raise_in_section(monkeypatch, JiraError("denied\x1b]52;c;eA==\x07"))
    io = ScriptedIO([("ask", "8"), ("ask", "q")])

    menu.run(client, settings, io=io)

    assert "error: denied" in io.text
    assert "\x1b" not in io.text


def test_unexpected_exception_is_reported_redacted_and_the_menu_continues(env, monkeypatch):
    _calls, client, settings = env
    _raise_in_section(monkeypatch, ValueError("bad value token=QQQQLEAKME1234"))
    io = ScriptedIO([("ask", "8"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert "error: unexpected ValueError" in io.text
    assert "QQQQLEAKME1234" not in io.text
    assert "Traceback" not in io.text


def test_os_error_names_the_file_and_keeps_the_menu(env, monkeypatch, tmp_path):
    _calls, client, settings = env
    target = tmp_path / "out.json"
    _raise_in_section(monkeypatch, PermissionError(13, "Permission denied", str(target)))
    io = ScriptedIO([("ask", "8"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert f"error: Permission denied: {target}" in io.text


def test_ctrl_c_inside_a_section_returns_to_the_main_menu(env, monkeypatch):
    _calls, client, settings = env
    _raise_in_section(monkeypatch, KeyboardInterrupt())
    io = ScriptedIO([("ask", "8"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert io.text.count("main menu") == 2
    assert "error" not in io.text


@pytest.mark.parametrize("exc", [EOFError(), menu._EndOfInput()], ids=["EOFError", "_EndOfInput"])
def test_closed_input_inside_a_section_quits_without_reading_again(env, monkeypatch, exc):
    _calls, client, settings = env
    _raise_in_section(monkeypatch, exc)
    io = ScriptedIO([("ask", "8")])

    assert menu.run(client, settings, io=io) == 0
    assert io.text.count("main menu") == 1
    assert "error" not in io.text


def test_ctrl_c_at_a_section_prompt_goes_back_to_the_main_menu(env, monkeypatch):
    _calls, client, settings = env
    monkeypatch.setattr(menu_find, "run", lambda session: menu._ask(session.io, "Issue key"))
    io = ScriptedIO([("ask", "8"), ("ask", _INTERRUPT), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert io.text.count("main menu") == 2


# --------------------------------------------------------------- 1. My issues


def test_my_issues_shows_the_report_and_refresh_rebuilds_it(env):
    calls, client, settings = env
    io = ScriptedIO([("ask", "1"), ("confirm", True), ("ask", "r"), ("ask", "b"), ("ask", "q")])

    assert menu.run(client, settings, io=io) == 0
    assert calls == [("my_issues", True), ("my_issues", True)]
    assert "[report My issues rows=1]" in io.text


def test_report_export_uses_the_shared_export_flow(env, monkeypatch):
    _calls, client, settings = env
    exported: list[dict] = []
    monkeypatch.setattr(menu_settings, "export_report", lambda session, report: exported.append(report))
    io = ScriptedIO([("ask", "1"), ("confirm", False), ("ask", "e"), ("ask", "b"), ("ask", "q")])

    menu.run(client, settings, io=io)

    assert [report["title"] for report in exported] == ["My issues"]


def test_the_report_shown_last_is_what_export_last_exports(env, monkeypatch):
    _calls, client, settings = env
    exported: list[dict] = []
    monkeypatch.setattr(menu_settings, "export_report", lambda session, report: exported.append(report))
    io = ScriptedIO([("ask", "1"), ("confirm", False), ("ask", "b"), ("ask", "15"), ("ask", "q")])

    menu.run(client, settings, io=io)

    assert [report["title"] for report in exported] == ["My issues"]


def test_export_helper_kept_for_sections_delegates_to_the_shared_flow(monkeypatch):
    exported: list[tuple] = []
    monkeypatch.setattr(
        menu_settings, "export_report", lambda session, report: exported.append((session, report))
    )
    session = menu._Session(client=object(), settings=None, io=ScriptedIO([]))

    menu._export_report(session, {"title": "t"})

    assert exported == [(session, {"title": "t"})]


def test_unknown_answer_after_a_report_is_explained(env):
    _calls, client, settings = env
    io = ScriptedIO([("ask", "1"), ("confirm", False), ("ask", "x"), ("ask", "b"), ("ask", "q")])

    menu.run(client, settings, io=io)

    assert "type e, r or b" in io.text


# ------------------------------------------------------------- y/N: b is back


def _terminal(monkeypatch, answers):
    """A real :class:`menu.MenuIO` reading ``answers`` instead of the keyboard."""
    feed = iter(answers)

    def fake_input(prompt=""):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    return menu.MenuIO()


@pytest.mark.parametrize("answer", ["b", "B", "!back", " b "])
def test_b_at_a_yes_no_question_goes_back(monkeypatch, answer):
    io = _terminal(monkeypatch, [answer])

    with pytest.raises(menu._Back):
        menu._confirm(io, "Proceed?")


@pytest.mark.parametrize(
    ("answer", "expected"), [("y", True), ("YES", True), ("n", False), ("", False), ("x", False)]
)
def test_yes_no_answers(monkeypatch, answer, expected):
    io = _terminal(monkeypatch, [answer])

    assert menu._confirm(io, "Proceed?") is expected


def test_yes_no_prompt_tells_the_operator_that_b_goes_back(monkeypatch):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "n")

    menu.MenuIO().confirm("Proceed?")

    assert prompts == ["Proceed? [y/N, b = back]: "]


def test_ctrl_c_at_a_yes_no_question_means_no():
    io = ScriptedIO([("confirm", _INTERRUPT)])

    assert menu._confirm(io, "Proceed?") is False


def test_b_at_a_preview_goes_back_without_saying_cancelled(monkeypatch):
    session = menu._Session(client=object(), settings=None, io=_terminal(monkeypatch, ["b"]))
    shown: list[str] = []
    session.io.show = shown.append

    with pytest.raises(menu._Back):
        menu._preview_and_confirm(session, "Assign PROJ-1", ["assignee -> bob"])

    assert "cancelled" not in "\n".join(shown)


def test_no_at_a_preview_cancels_and_says_nothing_was_written(monkeypatch):
    session = menu._Session(client=object(), settings=None, io=_terminal(monkeypatch, ["n"]))
    shown: list[str] = []
    session.io.show = shown.append

    assert menu._preview_and_confirm(session, "Assign PROJ-1", ["assignee -> bob"]) is False
    assert "cancelled; nothing was written" in shown


def test_b_at_a_yes_no_in_a_section_returns_to_the_main_menu(env, monkeypatch, capsys):
    calls, client, settings = env
    io = _terminal(monkeypatch, ["1", "b", "q"])  # My issues, then b at "Include resolved issues?"

    assert menu.run(client, settings, io=io) == 0
    assert calls == []
    assert capsys.readouterr().out.count("main menu") == 2


# ------------------------------------------------------------------ terminal IO


def test_default_menu_io_reads_and_writes(monkeypatch, capsys):
    io = _terminal(monkeypatch, ["", "2", "yes"])

    assert io.ask("Name", "alice") == ""
    assert io.choose("Pick", ["a", "b"]) == 1
    assert io.confirm("Proceed?") is True
    io.show("hello")
    assert "hello" in capsys.readouterr().out


def test_closed_input_at_a_section_prompt_quits_the_menu(env, monkeypatch, capsys):
    """A pipe that ends mid-section must quit, not loop on "back" forever."""
    calls, client, settings = env
    io = _terminal(monkeypatch, ["1"])  # the input ends at "Include resolved issues?"

    assert menu.run(client, settings, io=io) == 0
    assert calls == []
    assert capsys.readouterr().out.count("main menu") == 1


def test_closed_input_is_not_turned_into_back_by_a_prompt(monkeypatch):
    io = _terminal(monkeypatch, [])

    with pytest.raises(menu._EndOfInput):
        menu._ask(io, "Issue key")


# ---------------------------------------------------------- shared helpers kept


def test_session_starts_with_empty_navigation_state():
    session = menu._Session(client=object(), settings=None, io=ScriptedIO([]))

    assert session.last_report is None
    assert session.last_key is None
    assert session.last_board is None
    assert session.last_jql is None
    assert session.recent_keys == []


ESC_SUMMARY = "evil\x1b]52;c;cm0gLXJmIH4=\x07\x1b[2K\rfake"


def test_issue_detail_strips_escape_sequences_from_server_text():
    text = menu._issue_text(
        dict(
            ISSUE,
            summary=ESC_SUMMARY,
            description="line one\n\x1b[31mred\u202e",
            comments=[{"author": "bob\x9b", "created": "2026-10-01", "body": "hi\x1b]0;title\x07"}],
        )
    )

    assert "\x1b" not in text
    assert "\x9b" not in text
    assert "\u202e" not in text
    assert "\r" not in text
    assert "line one\n" in text, "description newlines are kept"


def test_preview_lines_are_folded_to_one_safe_line_each():
    io = ScriptedIO([("confirm", False)])
    session = menu._Session(client=object(), settings=None, io=io)

    menu._preview_and_confirm(session, "Move PROJ-1", ["Start\x1b[2K -> Done\nfake line"])

    assert "\x1b" not in io.text
    assert "Done fake line" in io.text


def test_as_path_falls_back_when_the_setting_is_missing():
    assert menu._as_path(None, "teams.yaml") == Path("teams.yaml")
    assert menu._as_path("/x/filters.yaml", "filters.yaml") == Path("/x/filters.yaml")
