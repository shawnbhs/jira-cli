"""Tests for menu sections 13-15 (``jira_cli.cli.menu_settings``).

Fully offline: the terminal is a scripted IO, Jira is a small fake client, and
the local services (saved filters, export) are the real ones writing into
``tmp_path``.  Only ``services.issues`` is faked, because running a filter would
otherwise need a whole search payload.
"""

from __future__ import annotations

import sys
import types

import pytest

from jira_cli import config
from jira_cli.cli import main as cli_main
from jira_cli.cli import menu
from jira_cli.cli import menu_settings as section
from jira_cli.errors import AuthError, JiraError, NotFoundError
from jira_cli.services import filters as filters_service

SECRET = "fake-token-0123456789"  # noqa: S105 - fake credential for the masking test


# --------------------------------------------------------------------- scripted IO


class ScriptedIO:
    """A ``MenuIO`` whose answers come from a script of ``(kind, value)`` pairs."""

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

    def confirms(self) -> int:
        return sum(1 for entry in self.asked if entry[0] == "confirm")


# ------------------------------------------------------------------------- fakes


class FakeClient:
    """Answers the few GETs these sections make; records every count query."""

    def __init__(self, *, favourites=None, favourites_error=None, count=None):
        self.favourites = favourites or []
        self.favourites_error = favourites_error
        self.count_answer = count or (lambda jql: 0)
        self.counted: list[str] = []

    def get(self, path, params=None):
        if path.endswith("/filter/favourite"):
            if self.favourites_error is not None:
                raise self.favourites_error
            return self.favourites
        if path.endswith("/serverInfo"):
            return {"version": "10.3.0", "deploymentType": "Server"}
        if path.endswith("/field"):
            return [
                {"id": "customfield_10001", "name": "Sprint"},
                {"id": "customfield_10002", "name": "Epic Link"},
            ]
        raise AssertionError(f"unexpected GET {path}")

    def count(self, jql):
        self.counted.append(jql)
        return self.count_answer(jql)

    def myself(self):
        return {"name": "alice", "displayName": "Alice Example"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("JIRA_CLI_ENV_FILE", raising=False)
    # Export defaults live in the per-user data dir; keep it in tmp_path.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    return config.Settings(
        base_url="https://jira.example.com",
        auth_mode=config.AUTH_PAT,
        username="alice",
        secret=SECRET,
        default_project="PROJ",
        config_dir=tmp_path,
        teams_file=tmp_path / "teams.yaml",
        filters_file=tmp_path / "filters.yaml",
    )


@pytest.fixture
def searches(monkeypatch):
    """Replace ``services.issues`` with a fake that records each JQL it runs."""
    seen: list[str] = []

    def search_issues(client, jql, limit=50):
        seen.append(jql)
        return [{"key": "PROJ-1", "summary": "An issue", "status": "To Do"}]

    module = types.ModuleType("jira_cli.services.issues")
    module.search_issues = search_issues
    monkeypatch.setitem(sys.modules, "jira_cli.services.issues", module)
    return seen


def session_for(settings, client, script) -> menu._Session:
    return menu._Session(client=client, settings=settings, io=ScriptedIO(script))


def save_local(settings, name, jql):
    filters_service.save_filter(settings.filters_file, name, jql)


FAVOURITE = {"id": "10", "name": "Team board", "jql": "project = PROJ AND sprint in openSprints()"}

#: Script steps that leave the filters section from its action menu.
LEAVE = [("choose", None)]
RUN, SAVE, DELETE = 0, 1, 2


# ------------------------------------------------------------------ saved filters


def test_filters_list_numbers_local_filters_before_server_favourites(settings):
    save_local(settings, "bugs", "type = Bug")
    session = session_for(settings, FakeClient(favourites=[FAVOURITE]), LEAVE)

    section.run_filters(session)

    lines = session.io.text.splitlines()
    local_line = next(line for line in lines if "bugs" in line)
    server_line = next(line for line in lines if "Team board" in line)
    assert local_line.split()[:2] == ["1", "local"]
    assert server_line.split()[:2] == ["2", "server"]


def test_filters_say_so_when_there_is_nothing_saved(settings):
    session = session_for(settings, FakeClient(), LEAVE)

    section.run_filters(session)

    assert "no saved filters and no server favourites" in session.io.text


def test_unreadable_server_favourites_still_list_local_filters(settings):
    save_local(settings, "bugs", "type = Bug")
    client = FakeClient(favourites_error=NotFoundError("filter endpoint missing", status=404))
    session = session_for(settings, client, LEAVE)

    section.run_filters(session)

    assert "bugs" in session.io.text
    assert "server favourites unavailable: filter endpoint missing" in session.io.text


def test_run_filter_by_number_runs_a_server_favourite(settings, searches):
    save_local(settings, "bugs", "type = Bug")
    script = [("choose", RUN), ("ask", "2"), ("ask", "b"), *LEAVE]
    session = session_for(settings, FakeClient(favourites=[FAVOURITE]), script)

    section.run_filters(session)

    assert searches == [FAVOURITE["jql"]]
    assert session.last_report["title"] == "Filter: Team board"


def test_run_filter_by_name_ignores_case(settings, searches):
    save_local(settings, "bugs", "type = Bug")
    script = [("choose", RUN), ("ask", "BUGS"), ("ask", "b"), *LEAVE]
    session = session_for(settings, FakeClient(), script)

    section.run_filters(session)

    assert searches == ["type = Bug"]


def test_run_filter_reasks_for_an_unknown_number(settings, searches):
    save_local(settings, "bugs", "type = Bug")
    script = [("choose", RUN), ("ask", "9"), ("ask", "1"), ("ask", "b"), *LEAVE]
    session = session_for(settings, FakeClient(), script)

    section.run_filters(session)

    assert "no filter '9'; type a number 1-1 or a name" in session.io.text
    assert searches == ["type = Bug"]


def test_back_inside_an_action_returns_to_the_filter_list(settings, searches):
    save_local(settings, "bugs", "type = Bug")
    script = [("choose", RUN), ("ask", "b"), ("choose", RUN), ("ask", "1"), ("ask", "b"), *LEAVE]
    session = session_for(settings, FakeClient(), script)

    section.run_filters(session)

    assert searches == ["type = Bug"]


def test_save_filter_checks_jql_on_the_server_before_the_preview(settings):
    def count(jql):
        if jql == "projct = PROJ":
            raise JiraError("Field 'projct' does not exist", status=400)
        return 3

    client = FakeClient(count=count)
    script = [
        ("choose", SAVE),
        ("ask", "open-work"),
        ("ask", "projct = PROJ"),
        ("ask", "project = PROJ"),
        ("ask", ""),
        ("confirm", True),
        *LEAVE,
    ]
    session = session_for(settings, client, script)

    section.run_filters(session)

    text = session.io.text
    assert client.counted == ["projct = PROJ", "project = PROJ"]
    assert text.index("the server rejected this JQL") < text.index("Save filter")
    assert "matches     3 issues now" in text
    assert filters_service.list_filters(settings.filters_file) == {
        "open-work": {"jql": "project = PROJ", "description": ""}
    }


def test_save_filter_lets_a_non_jql_server_error_through(settings):
    def count(jql):
        raise AuthError("credential rejected", status=401)

    script = [("choose", SAVE), ("ask", "open-work"), ("ask", "project = PROJ")]
    session = session_for(settings, FakeClient(count=count), script)

    with pytest.raises(AuthError):
        section.run_filters(session)
    assert not settings.filters_file.exists()


def test_save_filter_reasks_an_invalid_name(settings):
    script = [
        ("choose", SAVE),
        ("ask", "open work"),
        ("ask", "open-work"),
        ("ask", "project = PROJ"),
        ("ask", ""),
        ("confirm", True),
        *LEAVE,
    ]
    session = session_for(settings, FakeClient(), script)

    section.run_filters(session)

    assert "a filter name is 1-64 letters" in session.io.text
    assert list(filters_service.list_filters(settings.filters_file)) == ["open-work"]


def test_save_filter_with_a_name_in_other_case_replaces_the_saved_one(settings):
    save_local(settings, "bugs", "type = Bug")
    script = [
        ("choose", SAVE),
        ("ask", "BUGS"),
        ("ask", "type = Bug AND resolution is EMPTY"),
        ("ask", ""),
        ("confirm", True),
        *LEAVE,
    ]
    session = session_for(settings, FakeClient(), script)

    section.run_filters(session)

    assert "replaces    the saved filter bugs (type = Bug)" in session.io.text
    assert filters_service.list_filters(settings.filters_file) == {
        "bugs": {"jql": "type = Bug AND resolution is EMPTY", "description": ""}
    }


def test_save_filter_cancelled_at_the_preview_writes_nothing(settings):
    script = [
        ("choose", SAVE),
        ("ask", "open-work"),
        ("ask", "project = PROJ"),
        ("ask", ""),
        ("confirm", False),
        *LEAVE,
    ]
    session = session_for(settings, FakeClient(), script)

    section.run_filters(session)

    assert "cancelled; nothing was written" in session.io.text
    assert not settings.filters_file.exists()


def test_delete_local_filter_by_number_after_confirm(settings):
    save_local(settings, "bugs", "type = Bug")
    save_local(settings, "mine", "assignee = currentUser()")
    script = [("choose", DELETE), ("ask", "2"), ("confirm", True), *LEAVE]
    session = session_for(settings, FakeClient(), script)

    section.run_filters(session)

    assert list(filters_service.list_filters(settings.filters_file)) == ["bugs"]
    assert "filter mine deleted" in session.io.text


def test_delete_refuses_a_server_favourite_and_asks_again(settings):
    save_local(settings, "bugs", "type = Bug")
    script = [("choose", DELETE), ("ask", "2"), ("ask", "Bugs"), ("confirm", False), *LEAVE]
    session = session_for(settings, FakeClient(favourites=[FAVOURITE]), script)

    section.run_filters(session)

    assert "Team board is a server favourite; only local filters can be deleted" in session.io.text
    assert list(filters_service.list_filters(settings.filters_file)) == ["bugs"]


# ------------------------------------------------------------------------ export

REPORT = {
    "title": "Team dashboard",
    "columns": ["user", "open"],
    "rows": [{"user": "alice", "open": 2}],
    "summary": {},
    "meta": {},
}


def test_export_last_without_a_report_says_so(settings):
    session = session_for(settings, FakeClient(), [])

    section.run_export_last(session)

    assert "no report to export yet; run a report first" in session.io.text


def test_export_reasks_a_bad_format_and_previews_the_absolute_path(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = [("ask", "xls"), ("ask", "2"), ("ask", ""), ("confirm", True)]
    session = session_for(settings, FakeClient(), script)
    session.last_report = REPORT

    section.run_export_last(session)

    target = tmp_path / "xdg-data" / "jira-cli" / "exports" / "team-dashboard.csv"
    assert "unknown format 'xls'; type 1-3 or md/csv/json" in session.io.text
    assert f"file   {target}" in session.io.text
    assert target.read_text(encoding="utf-8").splitlines()[0] == "user,open"


def test_export_rejects_dot_and_git_targets_before_asking_to_confirm(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = [
        ("ask", "md"),
        ("ask", "."),
        ("ask", ".git/report.md"),
        ("ask", "out/report.md"),
        ("confirm", False),
    ]
    session = session_for(settings, FakeClient(), script)
    session.last_report = REPORT

    section.run_export_last(session)

    text = session.io.text
    assert ". is not a file path; choose another file" in text
    assert "refusing to write inside a .git directory" in text
    assert f"file   {tmp_path / 'out' / 'report.md'}" in text
    assert session.io.confirms() == 1
    assert not (tmp_path / "out").exists()


def test_export_back_at_the_format_prompt_writes_nothing(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = session_for(settings, FakeClient(), [("ask", "b")])
    session.last_report = REPORT

    section.run_export_last(session)

    assert session.io.confirms() == 0
    assert list(tmp_path.iterdir()) == []


def test_export_preview_notes_that_an_existing_file_is_replaced(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    existing = section.default_export_dir() / "team-dashboard.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("old", encoding="utf-8")
    session = session_for(settings, FakeClient(), [("ask", "1"), ("ask", ""), ("confirm", False)])
    session.last_report = REPORT

    section.run_export_last(session)

    assert "the existing file is replaced" in session.io.text
    assert existing.read_text(encoding="utf-8") == "old"


# ------------------------------------------------------------- settings / doctor


def test_settings_show_the_settings_but_never_the_secret(settings):
    session = session_for(settings, FakeClient(), [])

    section.run_settings(session)

    text = session.io.text
    assert "https://jira.example.com" in text
    # describe_settings reports only set/not set; not even the length leaks.
    assert any(line.split() == ["secret", "set"] for line in text.splitlines())
    assert f"{len(SECRET)} chars" not in text
    assert SECRET not in text


def test_settings_run_the_doctor_checks_and_list_pass_warn_fail(settings):
    session = session_for(settings, FakeClient(), [])

    section.run_settings(session)

    text = session.io.text
    assert "PASS  serverInfo" in text and "10.3.0 Server" in text
    assert "PASS  myself" in text and "alice (Alice Example)" in text
    assert "WARN  env-file" in text
    assert "PASS  field.sprint" in text and "customfield_10001" in text
    assert "WARN  field.story_points" in text
    assert text.rstrip().endswith("0 failed, 2 warnings, 14 checks")


def test_doctor_runs_exactly_the_cli_doctor_checks(settings, monkeypatch):
    seen = []

    def only_check(ctx, record):
        seen.append((ctx.settings, ctx.client))
        record("PASS", "only", "ran")

    monkeypatch.setattr(cli_main, "_DOCTOR_CHECKS", (only_check,))
    client = FakeClient()
    session = session_for(settings, client, [])

    results = section.run_doctor_checks(session)

    assert results == [{"status": "PASS", "check": "only", "detail": "ran"}]
    assert seen == [(settings, client)]


def test_doctor_records_a_raising_check_as_fail(settings, monkeypatch):
    def _check_broken(ctx, record):
        raise JiraError("server unreachable")

    monkeypatch.setattr(cli_main, "_DOCTOR_CHECKS", (_check_broken,))
    session = session_for(settings, FakeClient(), [])

    section.run_settings(session)

    assert "FAIL  check_broken" in session.io.text
    assert "1 failed, 0 warnings, 1 checks" in session.io.text


def test_doctor_text_folds_control_characters_out_of_details():
    text = section.doctor_text([{"status": "PASS", "check": "myself", "detail": "a\x1b[2Jb"}])

    assert "\x1b" not in text
