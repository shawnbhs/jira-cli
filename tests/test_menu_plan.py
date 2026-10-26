"""Tests for menu section 10, plan files (``jira_cli.cli.menu_plan``).

Offline: plan files live in ``tmp_path``; the plan service keeps its real, pure
file loading and ``effective_issues`` while the Jira-facing ``validate_plan`` and
``apply_plan`` are recorded fakes.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from jira_cli.cli import menu_plan
from jira_cli.cli.menu import _Back, _Session
from jira_cli.services import plan as real_plan

VALID_PLAN = """\
defaults:
  project: PROJ
issues:
  - ref: epic1
    summary: Build the login page
    type: Story
    assignee: alice
  - ref: task1
    summary: Write the login tests
    parent: epic1
    assignee: bob
"""

NAN_POINTS_PLAN = """\
issues:
  - ref: s1
    project: PROJ
    summary: Points that cannot be sent
    story_points: .nan
"""


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

    def options(self, title):
        return next(entry[2] for entry in self.asked if entry[0] == "choose" and entry[1] == title)


@dataclass
class FakeSettings:
    default_project: str = "PROJ"


class FakePlanService:
    """Real file loading and merging; Jira-facing calls are recorded fakes."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.validation = {"ok": True, "errors": [], "warnings": []}
        self.module = types.ModuleType("jira_cli.services.plan")
        self.module.load_plan = real_plan.load_plan
        self.module.effective_issues = real_plan.effective_issues
        self.module.default_state_path = real_plan.default_state_path
        self.module.new_plan_template = lambda: "issues: []\n"
        self.module.validate_plan = self.validate_plan
        self.module.apply_plan = self.apply_plan
        self.module.preview_plan = self.preview_plan

    def validate_plan(self, client, plan):
        self.calls.append(("validate_plan",))
        return self.validation

    def preview_plan(self, plan, settings=None):
        self.calls.append(("preview_plan",))
        return {"title": "Plan preview", "columns": ["ref"], "rows": [], "summary": {}, "meta": {}}

    def apply_plan(self, client, plan, *, dry_run=True, state_path=None):
        self.calls.append(("apply_plan", dry_run))
        if not self.validation["ok"]:
            return {"ok": False, "planned": [], "created": [], "validation": self.validation}
        if dry_run:
            planned = [
                {k: issue[k] for k in ("ref", "project", "type", "summary", "parent")}
                for issue in real_plan.effective_issues(plan)
            ]
            return {"ok": True, "planned": planned, "created": [], "validation": self.validation}
        return {
            "ok": True,
            "created": [{"ref": "epic1", "key": "PROJ-1", "url": "u1"}],
            "skipped": [],
            "failed": [],
            "warnings": [],
            "validation": self.validation,
        }

    @property
    def real_applies(self):
        return [call for call in self.calls if call == ("apply_plan", False)]


@pytest.fixture
def plan_service(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # The new-plan default and the picker read the per-user data dir.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    fake = FakePlanService()
    monkeypatch.setitem(sys.modules, "jira_cli.services.plan", fake.module)
    return fake


def _session(script) -> _Session:
    return _Session(client=object(), settings=FakeSettings(), io=ScriptedIO(script))


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ apply (B1, B2, B5)


def test_dry_run_lists_every_planned_issue_before_asking(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", VALID_PLAN)
    session = _session([("choose", 3), ("choose", 0), ("confirm", False)])

    menu_plan.run(session)

    text = session.io.text
    assert "would create 2 issue(s)" in text
    assert "Build the login page" in text and "Write the login tests" in text
    assert "alice" in text and "bob" in text
    assert plan_service.real_applies == []


def test_confirmed_apply_runs_for_real_and_reports_created_keys(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", VALID_PLAN)
    session = _session([("choose", 3), ("choose", 0), ("confirm", True)])

    menu_plan.run(session)

    assert plan_service.real_applies == [("apply_plan", False)]
    assert "PROJ-1" in session.io.text
    assert "created 1, skipped 0, failed 0" in session.io.text


def test_invalid_plan_shows_errors_and_never_offers_the_real_apply(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", VALID_PLAN)
    plan_service.validation = {
        "ok": False,
        "errors": [{"ref": "task1", "field": "assignee", "message": "'zed' is not assignable"}],
        "warnings": [],
    }
    session = _session([("choose", 3), ("choose", 0)])  # no confirm scripted

    menu_plan.run(session)

    assert "task1.assignee: 'zed' is not assignable" in session.io.text
    assert "nothing can be applied" in session.io.text
    assert not any(entry[0] == "confirm" for entry in session.io.asked)
    assert plan_service.real_applies == []


def test_non_finite_story_points_block_the_apply_before_any_confirmation(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", NAN_POINTS_PLAN)
    session = _session([("choose", 3), ("choose", 0)])

    menu_plan.run(session)

    assert "s1.story_points: story_points must be a finite number >= 0, got nan" in session.io.text
    assert not any(entry[0] == "confirm" for entry in session.io.asked)


def test_validate_reports_non_finite_story_points_as_an_error(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", NAN_POINTS_PLAN)
    session = _session([("choose", 1), ("choose", 0)])

    menu_plan.run(session)

    assert "plan has 1 error(s)" in session.io.text


def test_sprint_warning_is_listed_under_its_own_heading_and_in_the_preview(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", VALID_PLAN)
    plan_service.validation = {
        "ok": True,
        "errors": [],
        "warnings": [{"ref": "epic1", "field": "sprint", "message": "board 7 does not support sprints"}],
    }
    session = _session([("choose", 3), ("choose", 0), ("confirm", False)])

    menu_plan.run(session)

    text = session.io.text
    assert "WARNINGS (1)" in text
    assert "! epic1.sprint: board 7 does not support sprints" in text
    assert "WARNING      1 warning(s) listed above" in text


# ------------------------------------------------------------------ validate


def test_validate_separates_errors_from_warnings(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", VALID_PLAN)
    plan_service.validation = {
        "ok": False,
        "errors": [{"ref": "epic1", "field": "type", "message": "unknown type"}],
        "warnings": [{"ref": None, "field": "estimate", "message": "no estimate"}],
    }
    session = _session([("choose", 1), ("choose", 0)])

    menu_plan.run(session)

    text = session.io.text
    assert text.index("ERRORS (1)") < text.index("x epic1.type: unknown type")
    assert text.index("WARNINGS (1)") < text.index("! plan.estimate: no estimate")


def test_valid_plan_without_warnings_says_so(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", VALID_PLAN)
    session = _session([("choose", 1), ("choose", 0)])

    menu_plan.run(session)

    assert session.io.shown[-1] == "plan is valid"


# ------------------------------------------------------------------ choosing the file


def test_picker_lists_yaml_files_in_the_working_directory(plan_service, tmp_path):
    _write(tmp_path / "b.yml", VALID_PLAN)
    _write(tmp_path / "a.yaml", VALID_PLAN)
    _write(tmp_path / "notes.txt", "not a plan")
    session = _session([("choose", 1), ("choose", 1)])

    menu_plan.run(session)

    assert session.io.options("Plan file") == ["a.yaml", "b.yml", "Type a path..."]
    assert "plan is valid" in session.io.text


def test_typed_path_that_does_not_exist_is_asked_again(plan_service, tmp_path):
    _write(tmp_path / "elsewhere.plan", VALID_PLAN)  # not *.yaml, so nothing is listed
    session = _session([("choose", 1), ("ask", "missing.yaml"), ("ask", "elsewhere.plan")])

    menu_plan.run(session)

    assert "does not exist; type another path" in session.io.text
    assert "plan is valid" in session.io.text


def test_broken_yaml_is_reported_and_the_file_asked_again(plan_service, tmp_path):
    _write(tmp_path / "bad.yaml", "issues: [unclosed\n")
    _write(tmp_path / "good.yaml", VALID_PLAN)
    session = _session([("choose", 1), ("choose", 0), ("choose", 1)])

    menu_plan.run(session)

    assert "is not valid YAML" in session.io.text
    assert "plan is valid" in session.io.text


def test_a_plan_used_earlier_is_offered_first_as_recent(plan_service, tmp_path):
    _write(tmp_path / "a.yaml", VALID_PLAN)
    _write(tmp_path / "z.yaml", VALID_PLAN)
    session = _session([("choose", 1), ("choose", 1), ("choose", 1), ("choose", 0)])

    menu_plan.run(session)
    menu_plan.run(session)

    second_listing = [entry for entry in session.io.asked if entry[1] == "Plan file"][1][2]
    assert second_listing[0] == "z.yaml  (recent)"


def test_back_at_the_file_picker_leaves_the_section(plan_service, tmp_path):
    _write(tmp_path / "a.yaml", VALID_PLAN)
    session = _session([("choose", 1), ("choose", None)])

    with pytest.raises(_Back):
        menu_plan.run(session)


# ------------------------------------------------------------------ new plan file


def test_new_template_preview_names_the_absolute_path(plan_service, tmp_path):
    session = _session([("choose", 0), ("ask", "new.yaml"), ("confirm", True)])

    menu_plan.run(session)

    assert (tmp_path / "new.yaml").read_text(encoding="utf-8") == "issues: []\n"
    assert f"file {tmp_path.resolve() / 'new.yaml'}" in session.io.text


def test_new_template_in_a_missing_directory_is_asked_again(plan_service, tmp_path):
    session = _session([("choose", 0), ("ask", "nowhere/new.yaml"), ("ask", "here.yaml"), ("confirm", False)])

    menu_plan.run(session)

    assert "does not exist; type another path" in session.io.text
    assert not (tmp_path / "here.yaml").exists()


def test_new_template_keeps_an_existing_file_unless_overwrite_is_confirmed(plan_service, tmp_path):
    _write(tmp_path / "plan.yaml", VALID_PLAN)
    session = _session([("choose", 0), ("ask", "plan.yaml"), ("confirm", False)])

    menu_plan.run(session)

    assert (tmp_path / "plan.yaml").read_text(encoding="utf-8") == VALID_PLAN
