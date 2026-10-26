"""Menu-written exports and plans default outside the working directory.

Run from inside a git checkout, a default of ``./report.csv`` or ``./plan.yaml``
leaves people and worklog data where ``git add -A`` commits it.  These tests pin
the per-user default location, its private modes, that the working directory is
left untouched, and that the repo's ``.gitignore`` still catches typed paths at
the repo root.  Offline; files live in ``tmp_path``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from jira_cli.cli import menu_plan
from jira_cli.cli import menu_settings as section
from jira_cli.cli.menu import _Back, _Session

REPORT = {
    "title": "Team dashboard",
    "columns": ["user", "open"],
    "rows": [{"user": "alice", "open": 2}, {"user": "bob", "open": 1}],
}
POSIX_MODES = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


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

    def default_for(self, prompt):
        return next(entry[2] for entry in self.asked if entry[0] == "ask" and entry[1] == prompt)


class _Settings:
    default_project = "PROJ"


def _session(script) -> _Session:
    return _Session(client=object(), settings=_Settings(), io=ScriptedIO(script))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A working directory that is a git checkout, plus a separate data home."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    data_home = tmp_path / "data-home"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.chdir(checkout)
    return types.SimpleNamespace(checkout=checkout, data=data_home / "jira-cli")


def _files_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != ".git")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------------ user_data_dir


def test_data_dir_uses_an_absolute_xdg_data_home(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "xdg")}

    assert section.user_data_dir(env, windows=False) == tmp_path / "xdg" / "jira-cli"


def test_data_dir_ignores_a_relative_xdg_data_home_instead_of_using_the_cwd():
    env = {"XDG_DATA_HOME": "relative/data"}

    assert section.user_data_dir(env, windows=False) == Path.home() / ".local" / "share" / "jira-cli"


def test_data_dir_defaults_to_local_share_when_xdg_data_home_is_unset():
    assert section.user_data_dir({}, windows=False) == Path.home() / ".local" / "share" / "jira-cli"


def test_data_dir_on_windows_uses_localappdata(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "Local"), "XDG_DATA_HOME": str(tmp_path / "xdg")}

    assert section.user_data_dir(env, windows=True) == tmp_path / "Local" / "jira-cli"


def test_data_dir_on_windows_without_localappdata_falls_back_under_home():
    assert section.user_data_dir({}, windows=True) == Path.home() / "AppData" / "Local" / "jira-cli"


# ------------------------------------------------------------------- export


def test_export_default_is_the_full_path_in_the_data_dir(repo):
    session = _session([("ask", "csv"), ("ask", "b")])

    section.export_report(session, REPORT)

    expected = repo.data / "exports" / "team-dashboard.csv"
    assert session.io.default_for("Output file") == str(expected)


def test_accepting_the_export_default_leaves_the_git_checkout_untouched(repo):
    session = _session([("ask", "csv"), ("ask", ""), ("confirm", True)])

    section.export_report(session, REPORT)

    written = repo.data / "exports" / "team-dashboard.csv"
    assert written.read_text(encoding="utf-8").splitlines()[0] == "user,open"
    assert _files_in(repo.checkout) == []
    assert f"file   {written}" in session.io.text


@POSIX_MODES
def test_export_default_directory_is_created_private(repo):
    session = _session([("ask", "json"), ("ask", ""), ("confirm", True)])

    section.export_report(session, REPORT)

    assert _mode(repo.data / "exports") == 0o700
    assert _mode(repo.data) == 0o700
    assert _mode(repo.data / "exports" / "team-dashboard.json") == 0o600


def test_a_typed_relative_export_path_is_still_honoured(repo):
    session = _session([("ask", "md"), ("ask", "mine.md"), ("confirm", True)])

    section.export_report(session, REPORT)

    assert (repo.checkout / "mine.md").is_file()
    assert not (repo.data / "exports").exists()


def test_a_typed_export_path_inside_git_is_still_refused(repo):
    session = _session([("ask", "md"), ("ask", ".git/x.md"), ("ask", "b")])

    section.export_report(session, REPORT)

    assert "refusing to write inside a .git directory" in session.io.text
    assert not (repo.checkout / ".git" / "x.md").exists()


# -------------------------------------------------------------------- plans


@pytest.fixture
def plan_service(monkeypatch):
    module = types.ModuleType("jira_cli.services.plan")
    module.new_plan_template = lambda: "issues: []\n"
    monkeypatch.setitem(sys.modules, "jira_cli.services.plan", module)
    return module


def test_new_plan_default_is_the_full_path_in_the_data_dir(repo, plan_service):
    session = _session([("choose", 0), ("ask", "b")])

    with pytest.raises(_Back):
        menu_plan.run(session)

    assert session.io.default_for("New plan file") == str(repo.data / "plans" / "plan.yaml")


def test_accepting_the_plan_default_writes_outside_the_git_checkout(repo, plan_service):
    session = _session([("choose", 0), ("ask", ""), ("confirm", True)])

    menu_plan.run(session)

    written = repo.data / "plans" / "plan.yaml"
    assert written.read_text(encoding="utf-8") == "issues: []\n"
    assert _files_in(repo.checkout) == []
    assert f"file {written.resolve()}" in session.io.text


@POSIX_MODES
def test_new_plan_file_and_its_directory_are_private(repo, plan_service):
    session = _session([("choose", 0), ("ask", ""), ("confirm", True)])

    menu_plan.run(session)

    assert _mode(repo.data / "plans") == 0o700
    assert _mode(repo.data / "plans" / "plan.yaml") == 0o600


def test_declining_the_plan_preview_creates_no_directory(repo, plan_service):
    session = _session([("choose", 0), ("ask", ""), ("confirm", False)])

    menu_plan.run(session)

    assert not (repo.data / "plans").exists()


def test_a_plan_in_the_data_dir_is_offered_from_any_working_directory(repo, plan_service):
    plans = repo.data / "plans"
    plans.mkdir(parents=True)
    (plans / "sprint.yaml").write_text("issues: []\n", encoding="utf-8")
    session = _session([("choose", 1), ("choose", 99)])  # 99: out of range, backs out

    with pytest.raises(_Back):
        menu_plan.run(session)

    options = next(entry[2] for entry in session.io.asked if entry[1] == "Plan file")
    assert str((plans / "sprint.yaml").resolve()) in options


# ---------------------------------------------------------------- .gitignore

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ignored(rel: str) -> bool:
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("needs git and a checkout")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "check-ignore", "--no-index", "-q", rel],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    "rel",
    [
        "plan.yaml",
        "my-plan.yml",
        "report.md",
        "sprint-12-report.md",
        "velocity.csv",
        "worklog.json",
        "exports/a.md",
    ],
)
def test_report_and_plan_files_at_the_repo_root_are_ignored(rel):
    assert _ignored(rel)


@pytest.mark.parametrize(
    "rel", ["tests/fixtures/new.json", "docs/PLAN_FORMAT.md", "README.md", "pyproject.toml"]
)
def test_tracked_kinds_of_file_are_not_ignored(rel):
    assert not _ignored(rel)
