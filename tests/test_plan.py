"""Tests for ``jira_cli.services.plan`` - the draft / agree / bulk-create workflow.

Everything here is offline. The module under test and the core modules it uses
(``errors``, ``durations``, ``report``) are the real ones. The sibling services
(``issues``, ``projects``, ``people``, ``boards``) are also the real modules, but
the functions ``plan`` calls on them are replaced per test with
``monkeypatch.setattr``. ``plan`` resolves those services lazily through
``importlib.import_module``, so patching attributes on the real module objects is
enough, and monkeypatch restores every original after each test - nothing leaks
into other test files.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from jira_cli import errors
from jira_cli.services import boards as boards_mod
from jira_cli.services import issues as issues_mod
from jira_cli.services import people as people_mod
from jira_cli.services import plan
from jira_cli.services import projects as projects_mod
from jira_cli.services.issues import IssueSpec

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


@dataclass
class FakeSettings:
    hours_per_day: float = 8
    days_per_week: float = 5


class FakeClient:
    """A client that answers only the project-existence probe ``plan`` makes."""

    def __init__(self, projects=("PROJ",), settings: FakeSettings | None = None):
        self.settings = settings or FakeSettings()
        self.projects = set(projects)
        self.gets: list[str] = []

    def get(self, path, params=None):
        self.gets.append(path)
        prefix = "/rest/api/2/project/"
        if path.startswith(prefix):
            key = path[len(prefix):]
            if key in self.projects:
                return {"key": key}
            raise errors.NotFoundError(f"project {key} does not exist")
        raise AssertionError(f"unexpected GET {path}")


class Recorder:
    """Records ``create_issue`` calls and hands back keys, with scripted failures."""

    def __init__(self, fail: dict | None = None):
        self.specs: list[IssueSpec] = []
        self.fail = dict(fail or {})
        self.counter = 0

    def create(self, client, spec):
        self.specs.append(spec)
        problem = self.fail.get(spec.summary)
        if problem is not None:
            raise problem
        self.counter += 1
        key = f"PROJ-{100 + self.counter}"
        return {"key": key, "url": f"https://jira.example.com/browse/{key}", "warnings": []}

    def by_summary(self, summary: str) -> IssueSpec:
        for spec in self.specs:
            if spec.summary == summary:
                return spec
        raise AssertionError(f"no issue created with summary {summary!r}")


def _unstubbed(name: str):
    def fail(*args, **kwargs):
        raise AssertionError(f"{name} was not stubbed for this test")

    return fail


@pytest.fixture(autouse=True)
def fake_services(monkeypatch):
    """Install offline default fakes on the real service modules for one test.

    Tests override individual functions by assigning or ``monkeypatch.setattr``-ing
    them again; because each attribute was first set through ``monkeypatch`` here,
    its original real function is restored at teardown either way.
    """
    monkeypatch.setattr(issues_mod, "create_issue", _unstubbed("create_issue"))
    monkeypatch.setattr(issues_mod, "link_issues", _unstubbed("link_issues"))
    monkeypatch.setattr(
        projects_mod, "list_issue_types", lambda client, project_key: ["Task", "Story"]
    )
    monkeypatch.setattr(
        people_mod,
        "assignable_users",
        lambda client, project_key, query="": [{"username": "alice"}, {"username": "bob"}],
    )
    monkeypatch.setattr(
        boards_mod, "active_sprint", lambda client, board_id: {"id": 77, "name": "S1"}
    )
    monkeypatch.setattr(
        boards_mod, "get_board", lambda client, board_id: {"id": board_id, "type": "scrum"}
    )




def write_yaml(tmp_path: Path, body: dict | str, name: str = "plan.yaml") -> Path:
    path = tmp_path / name
    text = body if isinstance(body, str) else yaml.safe_dump(body, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def simple_plan(**issue_overrides) -> dict:
    """A one-issue plan that validates cleanly against the default stubs."""
    issue = {
        "ref": "t1",
        "summary": "Design the import pipeline",
        "description": "Pick a format.",
        "assignee": "alice",
        "estimate": "4h",
    }
    issue.update(issue_overrides)
    return {"defaults": {"project": "PROJ", "issue_type": "Task"}, "issues": [issue]}


def errors_for(result: dict, field: str) -> list[dict]:
    return [item for item in result["errors"] if item["field"] == field]


def warnings_for(result: dict, field: str) -> list[dict]:
    return [item for item in result["warnings"] if item["field"] == field]


# --------------------------------------------------------------------------- #
# load_plan
# --------------------------------------------------------------------------- #


def test_load_plan_reads_defaults_issues_and_records_the_source(tmp_path):
    path = write_yaml(tmp_path, simple_plan())
    loaded = plan.load_plan(path)
    assert loaded["defaults"]["project"] == "PROJ"
    assert [issue["ref"] for issue in loaded["issues"]] == ["t1"]
    assert loaded["_source"] == str(path)


def test_load_plan_missing_file_raises_input_error(tmp_path):
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(tmp_path / "nope.yaml")
    assert "not found" in str(caught.value)


def test_load_plan_rejects_invalid_yaml(tmp_path):
    path = write_yaml(tmp_path, "issues: [\n  - ref: t1\n")
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    assert "not valid YAML" in str(caught.value)


def test_load_plan_rejects_a_document_that_is_not_a_mapping(tmp_path):
    path = write_yaml(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    assert "must be a mapping" in str(caught.value)


def test_load_plan_requires_an_issues_list(tmp_path):
    path = write_yaml(tmp_path, {"defaults": {"project": "PROJ"}})
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    assert "'issues' is required" in str(caught.value)


def test_load_plan_names_the_ref_and_the_field_for_an_unknown_issue_key(tmp_path):
    path = write_yaml(
        tmp_path, {"issues": [{"ref": "t1", "summary": "x", "assigne": "alice"}]}
    )
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    message = str(caught.value)
    assert "'t1'" in message and "assigne" in message


def test_load_plan_names_the_ref_and_the_field_for_a_wrongly_typed_estimate(tmp_path):
    path = write_yaml(tmp_path, {"issues": [{"ref": "t2", "summary": "x", "estimate": 4}]})
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    message = str(caught.value)
    assert "'t2'" in message and "'estimate'" in message


def test_load_plan_rejects_a_non_numeric_story_points_value(tmp_path):
    path = write_yaml(
        tmp_path, {"issues": [{"ref": "t3", "summary": "x", "story_points": "big"}]}
    )
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    assert "'story_points'" in str(caught.value)


def test_load_plan_rejects_an_unknown_top_level_key(tmp_path):
    path = write_yaml(tmp_path, {"issues": [], "sprintz": 3})
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    assert "sprintz" in str(caught.value)


def test_load_plan_rejects_an_unknown_defaults_key(tmp_path):
    path = write_yaml(tmp_path, {"defaults": {"projekt": "PROJ"}, "issues": []})
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    assert "projekt" in str(caught.value)


def test_load_plan_rejects_a_malformed_link_entry(tmp_path):
    path = write_yaml(
        tmp_path, {"issues": [{"ref": "t1", "summary": "x", "links": [{"to": "t2"}]}]}
    )
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    message = str(caught.value)
    assert "links[0].type" in message and "'t1'" in message


def test_load_plan_rejects_an_unknown_estimate_by_value(tmp_path):
    path = write_yaml(
        tmp_path,
        {"issues": [{"ref": "t1", "summary": "x", "estimate_by": "robot"}]},
    )
    with pytest.raises(errors.InputError) as caught:
        plan.load_plan(path)
    message = str(caught.value)
    assert "'estimate_by'" in message and "'t1'" in message


def test_load_plan_accepts_agent_and_human_estimate_owners(tmp_path):
    path = write_yaml(
        tmp_path,
        {
            "issues": [
                {"ref": "t1", "summary": "a", "estimate": "2h", "estimate_by": "agent"},
                {"ref": "t2", "summary": "b", "estimate": "1d", "estimate_by": "human"},
            ]
        },
    )
    loaded = plan.load_plan(path)
    assert loaded["issues"][0]["estimate_by"] == "agent"


# --------------------------------------------------------------------------- #
# plan_from_csv
# --------------------------------------------------------------------------- #


CSV_BODY = (
    "ref,summary,description,assignee,estimate,due,type,epic,parent,labels,"
    "story_points,project,estimate_by\n"
    "t1,Design,Pick a format,alice,4h,2026-10-20,Story,PROJ-12,,backend;api,3,PROJ,human\n"
    "t2,Build,,bob,2d,,Task,,t1,,,PROJ,agent\n"
    ",,,,,,,,,,,,\n"
)


def test_plan_from_csv_maps_every_documented_column(tmp_path):
    path = tmp_path / "plan.csv"
    path.write_text(CSV_BODY, encoding="utf-8")

    loaded = plan.plan_from_csv(path)

    assert [issue["ref"] for issue in loaded["issues"]] == ["t1", "t2"]
    first = loaded["issues"][0]
    assert first["summary"] == "Design"
    assert first["description"] == "Pick a format"
    assert first["assignee"] == "alice"
    assert first["estimate"] == "4h"
    assert first["due"] == "2026-10-20"
    assert first["type"] == "Story"
    assert first["epic"] == "PROJ-12"
    assert first["labels"] == ["backend", "api"]
    assert first["story_points"] == 3.0
    assert first["project"] == "PROJ"
    assert first["estimate_by"] == "human"
    assert loaded["issues"][1]["parent"] == "t1"
    assert loaded["_source"] == str(path)


def test_plan_from_csv_omits_empty_cells_so_defaults_still_apply(tmp_path):
    path = tmp_path / "plan.csv"
    path.write_text(CSV_BODY, encoding="utf-8")
    loaded = plan.plan_from_csv(path)
    assert "description" not in loaded["issues"][1]
    assert "epic" not in loaded["issues"][1]


def test_plan_from_csv_rejects_an_unknown_column(tmp_path):
    path = tmp_path / "plan.csv"
    path.write_text("ref,summary,wat\nt1,Design,x\n", encoding="utf-8")
    with pytest.raises(errors.InputError) as caught:
        plan.plan_from_csv(path)
    assert "wat" in str(caught.value)


def test_plan_from_csv_requires_a_summary_column(tmp_path):
    path = tmp_path / "plan.csv"
    path.write_text("ref,assignee\nt1,alice\n", encoding="utf-8")
    with pytest.raises(errors.InputError) as caught:
        plan.plan_from_csv(path)
    assert "'summary' column is required" in str(caught.value)


def test_plan_from_csv_rejects_a_non_numeric_story_points_cell(tmp_path):
    path = tmp_path / "plan.csv"
    path.write_text("ref,summary,story_points\nt1,Design,lots\n", encoding="utf-8")
    with pytest.raises(errors.InputError) as caught:
        plan.plan_from_csv(path)
    assert "'story_points'" in str(caught.value)


def test_plan_from_csv_text_builds_the_same_plan_as_plan_from_csv(tmp_path):
    text = "\ufeffref,summary,labels\nt1,Do it,a;b\n"
    path = tmp_path / "plan.csv"
    path.write_text(text, encoding="utf-8")

    from_text = plan.plan_from_csv_text(text, source=path)

    assert from_text == plan.plan_from_csv(path)
    assert from_text["issues"] == [{"ref": "t1", "summary": "Do it", "labels": ["a", "b"]}]


def test_plan_from_csv_missing_file_raises_input_error(tmp_path):
    with pytest.raises(errors.InputError):
        plan.plan_from_csv(tmp_path / "nope.csv")


# --------------------------------------------------------------------------- #
# new_plan_template
# --------------------------------------------------------------------------- #


def test_new_plan_template_is_loadable_yaml_that_passes_the_schema(tmp_path):
    path = write_yaml(tmp_path, plan.new_plan_template())
    loaded = plan.load_plan(path)
    assert loaded["defaults"]["project"] == "PROJ"
    assert [issue["ref"] for issue in loaded["issues"]] == ["t1", "t2"]


def test_new_plan_template_is_commented_and_documents_the_estimate_vocabulary():
    text = plan.new_plan_template()
    assert text.count("#") > 20
    assert "4h" in text and "2d" in text
    assert '"ask"' in text and '"none"' in text
    assert "estimate_by" in text
    assert "agent" in text and "human" in text


def test_new_plan_template_marks_one_estimate_as_ask_so_apply_is_blocked(tmp_path):
    path = write_yaml(tmp_path, plan.new_plan_template())
    loaded = plan.load_plan(path)
    result = plan.validate_plan(FakeClient(), loaded)
    assert result["ok"] is False
    assert errors_for(result, "estimate")


# --------------------------------------------------------------------------- #
# validate_plan - local checks
# --------------------------------------------------------------------------- #


def test_validate_plan_accepts_a_clean_plan():
    result = plan.validate_plan(FakeClient(), simple_plan())
    assert result == {"ok": True, "errors": [], "warnings": []}


def test_validate_plan_reports_a_missing_summary_with_ref_and_field():
    result = plan.validate_plan(
        FakeClient(), {"defaults": {"project": "PROJ"}, "issues": [{"ref": "t1"}]}
    )
    assert result["ok"] is False
    assert errors_for(result, "summary")[0]["ref"] == "t1"


def test_validate_plan_reports_a_blank_summary():
    result = plan.validate_plan(
        FakeClient(),
        {"defaults": {"project": "PROJ"}, "issues": [{"ref": "t1", "summary": "   "}]},
    )
    assert errors_for(result, "summary")


def test_validate_plan_reports_duplicate_refs():
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "t1", "summary": "a", "assignee": "alice", "description": "d"},
            {"ref": "t1", "summary": "b", "assignee": "alice", "description": "d"},
        ],
    }
    result = plan.validate_plan(FakeClient(), body)
    duplicates = errors_for(result, "ref")
    assert len(duplicates) == 1
    assert "duplicate ref 't1'" in duplicates[0]["message"]


def test_validate_plan_reports_an_unknown_parent_ref():
    result = plan.validate_plan(FakeClient(), simple_plan(parent="ghost"))
    assert errors_for(result, "parent")[0]["ref"] == "t1"


def test_validate_plan_accepts_an_existing_issue_key_as_parent():
    result = plan.validate_plan(FakeClient(), simple_plan(parent="PROJ-12"))
    assert result["ok"] is True


def test_validate_plan_reports_an_unknown_link_target():
    result = plan.validate_plan(
        FakeClient(), simple_plan(links=[{"type": "Blocks", "to": "ghost"}])
    )
    assert errors_for(result, "links[0].to")


def test_validate_plan_accepts_a_link_to_an_existing_key():
    result = plan.validate_plan(
        FakeClient(), simple_plan(links=[{"type": "Blocks", "to": "PROJ-9"}])
    )
    assert result["ok"] is True


TRAVERSAL_KEY = "PROJ-1/../../project/PROJ"


@pytest.mark.parametrize(
    "parent", [TRAVERSAL_KEY, "PROJ-1?x=/rest/api/2/search", "proj-12", "PROJ-", "PROJ-1 2"]
)
def test_validate_plan_rejects_a_parent_that_is_not_a_valid_issue_key(parent):
    result = plan.validate_plan(FakeClient(), simple_plan(parent=parent))
    assert result["ok"] is False
    assert errors_for(result, "parent")


@pytest.mark.parametrize("target", [TRAVERSAL_KEY, "PROJ-9#frag", "proj-9"])
def test_validate_plan_rejects_a_link_target_that_is_not_a_valid_issue_key(target):
    result = plan.validate_plan(
        FakeClient(), simple_plan(links=[{"type": "Blocks", "to": target}])
    )
    assert result["ok"] is False
    assert errors_for(result, "links[0].to")


@pytest.mark.parametrize("epic", [TRAVERSAL_KEY, "Import epic", "PROJ 12"])
def test_validate_plan_rejects_an_epic_that_is_not_a_valid_issue_key(epic):
    result = plan.validate_plan(FakeClient(), simple_plan(epic=epic))
    assert result["ok"] is False
    assert errors_for(result, "epic")[0]["ref"] == "t1"


def test_validate_plan_rejects_an_invalid_default_epic():
    body = simple_plan()
    body["defaults"]["epic"] = TRAVERSAL_KEY
    result = plan.validate_plan(FakeClient(), body)
    assert errors_for(result, "epic")


def test_validate_plan_accepts_an_existing_epic_key():
    result = plan.validate_plan(FakeClient(), simple_plan(epic="PROJ-12"))
    assert errors_for(result, "epic") == []


def test_apply_plan_refuses_a_traversal_parent_before_creating_anything(tmp_path):
    issues_mod.create_issue = lambda client, spec: pytest.fail("nothing may be created")
    with pytest.raises(errors.InputError) as caught:
        plan.apply_plan(
            FakeClient(),
            simple_plan(parent=TRAVERSAL_KEY),
            state_path=tmp_path / "s.json",
            dry_run=False,
        )
    assert "parent" in str(caught.value)


def test_apply_plan_refuses_a_traversal_link_target_before_creating_anything(tmp_path):
    issues_mod.create_issue = lambda client, spec: pytest.fail("nothing may be created")
    issues_mod.link_issues = lambda *a, **k: pytest.fail("nothing may be linked")
    with pytest.raises(errors.InputError):
        plan.apply_plan(
            FakeClient(),
            simple_plan(links=[{"type": "Blocks", "to": TRAVERSAL_KEY}]),
            state_path=tmp_path / "s.json",
            dry_run=False,
        )


def test_apply_plan_sends_an_existing_parent_key_without_surrounding_spaces(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    plan.apply_plan(
        FakeClient(), simple_plan(parent=" PROJ-12 "), state_path=tmp_path / "s.json",
        dry_run=False,
    )
    assert recorder.specs[0].parent_key == "PROJ-12"


def test_apply_plan_rejects_a_state_file_whose_created_key_is_not_an_issue_key(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"created": {"t1": {"key": TRAVERSAL_KEY}}}), encoding="utf-8"
    )
    issues_mod.create_issue = lambda client, spec: pytest.fail("nothing may be created")
    with pytest.raises(errors.InputError) as caught:
        plan.apply_plan(FakeClient(), simple_plan(), state_path=state_path, dry_run=False)
    assert "corrupt" in str(caught.value)
    assert "invalid issue key" in str(caught.value)


def test_validate_plan_reports_self_parenting_once():
    result = plan.validate_plan(FakeClient(), simple_plan(parent="t1"))
    parent_errors = errors_for(result, "parent")
    assert len(parent_errors) == 1
    assert "its own parent" in parent_errors[0]["message"]


def test_validate_plan_reports_a_two_issue_parent_cycle():
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "a", "summary": "a", "assignee": "alice", "description": "d",
             "parent": "b"},
            {"ref": "b", "summary": "b", "assignee": "alice", "description": "d",
             "parent": "a"},
        ],
    }
    result = plan.validate_plan(FakeClient(), body)
    cycles = [item for item in errors_for(result, "parent") if "cycle" in item["message"]]
    assert len(cycles) == 1
    assert "a -> b -> a" in cycles[0]["message"] or "b -> a -> b" in cycles[0]["message"]


def test_validate_plan_reports_a_three_issue_parent_cycle():
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "a", "summary": "a", "parent": "c"},
            {"ref": "b", "summary": "b", "parent": "a"},
            {"ref": "c", "summary": "c", "parent": "b"},
        ],
    }
    result = plan.validate_plan(FakeClient(), body)
    assert [item for item in errors_for(result, "parent") if "cycle" in item["message"]]


def test_validate_plan_treats_a_remaining_ask_estimate_as_a_blocking_error():
    result = plan.validate_plan(FakeClient(), simple_plan(estimate="ask"))
    estimate_errors = errors_for(result, "estimate")
    assert result["ok"] is False
    assert "still 'ask'" in estimate_errors[0]["message"]


def test_validate_plan_accepts_none_as_a_deliberate_non_estimate():
    result = plan.validate_plan(FakeClient(), simple_plan(estimate="none"))
    assert result["ok"] is True


def test_validate_plan_reports_an_unparseable_duration():
    result = plan.validate_plan(FakeClient(), simple_plan(estimate="soon"))
    assert "invalid duration" in errors_for(result, "estimate")[0]["message"]


@pytest.mark.parametrize("text", ["30m", "4h", "2d", "1w 2d 3h"])
def test_validate_plan_accepts_every_documented_duration_form(text):
    assert plan.validate_plan(FakeClient(), simple_plan(estimate=text))["ok"] is True


def test_validate_plan_reports_a_bad_due_date():
    result = plan.validate_plan(FakeClient(), simple_plan(due="next tuesday"))
    assert "invalid due date" in errors_for(result, "due")[0]["message"]


@pytest.mark.parametrize("text", ["2026-10-20", "today", "3d", "2w"])
def test_validate_plan_accepts_every_documented_due_form(text):
    assert plan.validate_plan(FakeClient(), simple_plan(due=text))["ok"] is True


def test_validate_plan_warns_once_per_agent_suggested_estimate():
    body = simple_plan(estimate="6h", estimate_by="agent")
    result = plan.validate_plan(FakeClient(), body)
    suggestions = warnings_for(result, "estimate_by")
    assert result["ok"] is True
    assert len(suggestions) == 1
    assert suggestions[0]["ref"] == "t1"


def test_validate_plan_does_not_warn_for_a_human_owned_estimate():
    result = plan.validate_plan(
        FakeClient(), simple_plan(estimate="6h", estimate_by="human")
    )
    assert warnings_for(result, "estimate_by") == []


def test_validate_plan_reports_an_empty_plan():
    result = plan.validate_plan(FakeClient(), {"issues": []})
    assert result["ok"] is False
    assert errors_for(result, "issues")


def test_validate_plan_reports_a_schema_problem_instead_of_raising():
    result = plan.validate_plan(FakeClient(), {"issues": [{"summary": 4}]})
    assert result["ok"] is False
    assert errors_for(result, "plan")


# --------------------------------------------------------------------------- #
# validate_plan - server-backed checks
# --------------------------------------------------------------------------- #


def test_validate_plan_requires_a_project():
    result = plan.validate_plan(FakeClient(), {"issues": [{"ref": "t1", "summary": "a"}]})
    assert "project is required" in errors_for(result, "project")[0]["message"]


def test_validate_plan_reports_an_unknown_project():
    result = plan.validate_plan(FakeClient(projects=("OTHER",)), simple_plan())
    assert "unknown project 'PROJ'" in errors_for(result, "project")[0]["message"]


def test_validate_plan_reports_an_issue_type_the_project_does_not_have():
    calls: list[str] = []

    def list_issue_types(client, project_key):
        calls.append(project_key)
        return ["Task", "Story"]

    projects_mod.list_issue_types = list_issue_types
    result = plan.validate_plan(FakeClient(), simple_plan(type="Epic"))
    type_errors = errors_for(result, "type")
    assert "does not exist in project PROJ" in type_errors[0]["message"]
    assert "Task, Story" in type_errors[0]["message"]
    assert calls == ["PROJ"]


def test_validate_plan_caches_issue_types_per_project():
    calls: list[str] = []
    projects_mod.list_issue_types = lambda client, project_key: (
        calls.append(project_key) or ["Task"]
    )
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "a", "summary": "a", "assignee": "alice", "description": "d"},
            {"ref": "b", "summary": "b", "assignee": "alice", "description": "d"},
        ],
    }
    plan.validate_plan(FakeClient(), body)
    assert calls == ["PROJ"]


def test_validate_plan_reports_an_unassignable_user():
    people_mod.assignable_users = lambda client, project_key, query="": [
        {"username": "bob"}
    ]
    result = plan.validate_plan(FakeClient(), simple_plan(assignee="carol"))
    assert "not assignable in project PROJ" in errors_for(result, "assignee")[0]["message"]


def test_validate_plan_caches_assignable_users_per_project():
    calls: list[str] = []

    def assignable_users(client, project_key, query=""):
        calls.append(project_key)
        return [{"username": "alice"}]

    people_mod.assignable_users = assignable_users
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "a", "summary": "a", "assignee": "alice", "description": "d"},
            {"ref": "b", "summary": "b", "assignee": "alice", "description": "d"},
        ],
    }
    plan.validate_plan(FakeClient(), body)
    assert calls == ["PROJ"]


def test_validate_plan_reports_sprint_active_when_the_board_has_none():
    boards_mod.active_sprint = lambda client, board_id: None
    body = simple_plan()
    body["defaults"].update({"sprint": "active", "board": 12})
    result = plan.validate_plan(FakeClient(), body)
    assert "no active sprint" in errors_for(result, "sprint")[0]["message"]


def _no_sprints(client, board_id):
    raise errors.JiraError("The board doesn't support sprints (HTTP 400)")


def _board_of_type(board_type):
    return lambda client, board_id: {"id": board_id, "type": board_type}


def test_validate_plan_rejects_sprint_active_on_a_kanban_board():
    boards_mod.active_sprint = _no_sprints
    boards_mod.get_board = _board_of_type("kanban")
    body = simple_plan()
    body["defaults"].update({"sprint": "active", "board": 37})

    result = plan.validate_plan(FakeClient(), body)

    assert result["ok"] is False
    [problem] = errors_for(result, "sprint")
    assert problem["message"] == (
        "board 37 is kanban and has no sprints; remove 'sprint' or use a scrum board"
    )
    assert warnings_for(result, "sprint") == []


def test_validate_plan_rejects_sprint_active_on_a_kanban_board_with_no_sprint_error():
    # Some servers answer an empty sprint list instead of HTTP 400 for kanban.
    boards_mod.active_sprint = lambda client, board_id: None
    boards_mod.get_board = _board_of_type("Kanban")
    body = simple_plan()
    body["defaults"].update({"sprint": "active", "board": 37})

    result = plan.validate_plan(FakeClient(), body)

    assert "is kanban and has no sprints" in errors_for(result, "sprint")[0]["message"]


def test_validate_plan_only_warns_when_the_board_type_cannot_be_read():
    def unreadable(client, board_id):
        raise errors.JiraError("board lookup failed")

    boards_mod.active_sprint = _no_sprints
    boards_mod.get_board = unreadable
    body = simple_plan()
    body["defaults"].update({"sprint": "active", "board": 37})

    result = plan.validate_plan(FakeClient(), body)

    assert result["ok"] is True
    assert "could not read the active sprint" in warnings_for(result, "sprint")[0]["message"]


def test_validate_plan_only_warns_when_a_scrum_board_sprint_read_fails():
    boards_mod.active_sprint = _no_sprints
    body = simple_plan()
    body["defaults"].update({"sprint": "active", "board": 12})

    result = plan.validate_plan(FakeClient(), body)

    assert result["ok"] is True
    assert errors_for(result, "sprint") == []
    assert "could not read the active sprint" in warnings_for(result, "sprint")[0]["message"]


def test_validate_plan_propagates_an_auth_error_from_the_board_type_lookup():
    def denied(client, board_id):
        raise errors.AuthError("token rejected")

    boards_mod.active_sprint = _no_sprints
    boards_mod.get_board = denied
    body = simple_plan()
    body["defaults"].update({"sprint": "active", "board": 37})

    with pytest.raises(errors.AuthError):
        plan.validate_plan(FakeClient(), body)


def test_validate_plan_accepts_sprint_active_when_the_board_has_one():
    body = simple_plan()
    body["defaults"].update({"sprint": "active", "board": 12})
    assert plan.validate_plan(FakeClient(), body)["ok"] is True


def test_validate_plan_requires_a_board_for_sprint_active():
    body = simple_plan()
    body["defaults"]["sprint"] = "active"
    result = plan.validate_plan(FakeClient(), body)
    assert "needs a board id" in errors_for(result, "board")[0]["message"]


def test_validate_plan_rejects_a_sprint_that_is_neither_active_nor_an_id():
    body = simple_plan()
    body["defaults"]["sprint"] = "next one"
    result = plan.validate_plan(FakeClient(), body)
    assert "'active' or a numeric sprint id" in errors_for(result, "sprint")[0]["message"]


def test_validate_plan_accepts_a_numeric_sprint_id_without_touching_the_board():
    boards_mod.active_sprint = lambda client, board_id: pytest.fail(
        "a numeric sprint id must not trigger a board lookup"
    )
    body = simple_plan()
    body["defaults"]["sprint"] = 88
    assert plan.validate_plan(FakeClient(), body)["ok"] is True


def test_validate_plan_warns_about_a_missing_description_and_assignee():
    body = {"defaults": {"project": "PROJ"}, "issues": [{"ref": "t1", "summary": "a"}]}
    result = plan.validate_plan(FakeClient(), body)
    assert warnings_for(result, "description")[0]["ref"] == "t1"
    assert warnings_for(result, "assignee")[0]["ref"] == "t1"


def test_validate_plan_turns_a_server_failure_into_a_warning_not_an_error():
    def explode(client, project_key):
        raise errors.JiraError("service unavailable")

    projects_mod.list_issue_types = explode
    result = plan.validate_plan(FakeClient(), simple_plan())
    assert result["ok"] is True
    assert "could not list issue types" in warnings_for(result, "type")[0]["message"]


def test_validate_plan_lets_an_auth_error_propagate():
    def explode(client, project_key):
        raise errors.AuthError("401")

    projects_mod.list_issue_types = explode
    with pytest.raises(errors.AuthError):
        plan.validate_plan(FakeClient(), simple_plan())


# --------------------------------------------------------------------------- #
# preview_plan
# --------------------------------------------------------------------------- #


def preview_body() -> dict:
    return {
        "defaults": {"project": "PROJ", "issue_type": "Task", "sprint": "active",
                     "board": 12},
        "issues": [
            {"ref": "t1", "summary": "Design", "assignee": "alice", "estimate": "4h",
             "due": "2026-10-20", "type": "Story"},
            {"ref": "t2", "summary": "Build", "assignee": "bob", "estimate": "1d",
             "parent": "t1"},
            {"ref": "t3", "summary": "Document", "estimate": "4h", "assignee": "alice"},
            {"ref": "t4", "summary": "Decide", "estimate": "ask", "assignee": "bob"},
        ],
    }


def test_preview_plan_returns_the_contract_report_shape():
    result = plan.preview_plan(preview_body(), FakeSettings())
    assert result["title"] == "Plan preview"
    assert result["columns"] == [
        "ref", "project", "type", "summary", "assignee", "estimate", "due",
        "parent", "sprint",
    ]
    assert set(result["rows"][0]) == set(result["columns"])
    assert "generated_at" in result["meta"]


def test_preview_plan_has_one_row_per_issue_with_the_requested_fields():
    rows = plan.preview_plan(preview_body(), FakeSettings())["rows"]
    assert [row["ref"] for row in rows] == ["t1", "t2", "t3", "t4"]
    assert rows[0] == {
        "ref": "t1", "project": "PROJ", "type": "Story", "summary": "Design",
        "assignee": "alice", "estimate": "4h", "due": "2026-10-20", "parent": "-",
        "sprint": "active",
    }
    assert rows[1]["parent"] == "t1"
    assert rows[2]["assignee"] == "alice"


def test_preview_plan_uses_a_dash_for_every_absent_value():
    body = {"defaults": {}, "issues": [{"ref": "t1", "summary": "x"}]}
    row = plan.preview_plan(body, FakeSettings())["rows"][0]
    assert row["project"] == "-"
    assert row["assignee"] == "-"
    assert row["estimate"] == "-"
    assert row["due"] == "-"
    assert row["parent"] == "-"
    assert row["sprint"] == "-"


def test_preview_plan_totals_the_estimates_and_counts_the_issues():
    summary = plan.preview_plan(preview_body(), FakeSettings())["summary"]
    assert summary["issues"] == 4
    # 4h + 1d(8h) + 4h = 16h = 2d on an 8h day; "ask" contributes nothing.
    assert summary["total_estimate_seconds"] == 16 * 3600
    assert summary["total_estimate"] == "2d"
    assert summary["without_estimate"] == 1


def test_preview_plan_reports_count_and_estimate_per_assignee():
    per_assignee = plan.preview_plan(preview_body(), FakeSettings())["summary"][
        "per_assignee"
    ]
    assert per_assignee["alice"]["count"] == 2
    assert per_assignee["alice"]["estimate_seconds"] == 8 * 3600
    assert per_assignee["alice"]["estimate"] == "1d"
    assert per_assignee["bob"]["count"] == 2
    assert per_assignee["bob"]["estimate_seconds"] == 8 * 3600


def test_preview_plan_buckets_issues_with_no_assignee_as_unassigned():
    body = {"defaults": {"project": "PROJ"}, "issues": [{"ref": "t1", "summary": "x"}]}
    per_assignee = plan.preview_plan(body, FakeSettings())["summary"]["per_assignee"]
    assert per_assignee["unassigned"]["count"] == 1


def test_preview_plan_lists_the_refs_still_waiting_for_an_estimate_answer():
    summary = plan.preview_plan(preview_body(), FakeSettings())["summary"]
    assert summary["needs_estimate_answer"] == ["t4"]


def test_preview_plan_lists_agent_suggested_refs_for_review():
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "t1", "summary": "a", "estimate": "2h", "estimate_by": "agent"},
            {"ref": "t2", "summary": "b", "estimate": "2h", "estimate_by": "human"},
        ],
    }
    summary = plan.preview_plan(body, FakeSettings())["summary"]
    assert summary["agent_estimated"] == ["t1"]


def test_preview_plan_works_on_an_invalid_plan_so_a_draft_can_still_be_shown():
    body = {"defaults": {"project": "PROJ"},
            "issues": [{"ref": "t1", "summary": "x", "estimate": "soon"}]}
    result = plan.preview_plan(body, FakeSettings())
    assert result["rows"][0]["estimate"] == "soon"
    assert result["summary"]["total_estimate_seconds"] == 0


def test_preview_plan_honours_a_custom_hours_per_day():
    body = {"defaults": {"project": "PROJ"},
            "issues": [{"ref": "t1", "summary": "x", "estimate": "1d"}]}
    summary = plan.preview_plan(body, FakeSettings(hours_per_day=6))["summary"]
    assert summary["total_estimate_seconds"] == 6 * 3600


# --------------------------------------------------------------------------- #
# apply_plan - dry run and refusal
# --------------------------------------------------------------------------- #


def apply_body() -> dict:
    return {
        "defaults": {"project": "PROJ", "issue_type": "Task"},
        "issues": [
            {"ref": "child", "summary": "Build", "assignee": "alice",
             "description": "d", "estimate": "2h", "parent": "root"},
            {"ref": "root", "summary": "Design", "assignee": "alice",
             "description": "d", "estimate": "4h"},
        ],
    }


def test_apply_plan_dry_run_creates_nothing_and_returns_the_parents_first_order():
    issues_mod.create_issue = lambda client, spec: pytest.fail(
        "a dry run must not create anything"
    )
    result = plan.apply_plan(FakeClient(), apply_body(), dry_run=True)
    assert result["dry_run"] is True
    assert result["order"] == ["root", "child"]
    assert result["created"] == []
    assert [item["ref"] for item in result["planned"]] == ["root", "child"]


def test_apply_plan_dry_run_still_returns_the_order_for_an_invalid_plan():
    body = apply_body()
    body["issues"][1]["estimate"] = "ask"
    result = plan.apply_plan(FakeClient(), body, dry_run=True)
    assert result["ok"] is False
    assert result["order"] == ["root", "child"]
    assert result["validation"]["errors"]


def test_apply_plan_refuses_to_run_a_plan_with_validation_errors():
    body = apply_body()
    body["issues"][1]["estimate"] = "ask"
    issues_mod.create_issue = lambda client, spec: pytest.fail(
        "nothing may be created while the plan has errors"
    )
    with pytest.raises(errors.InputError) as caught:
        plan.apply_plan(FakeClient(), body, dry_run=False)
    message = str(caught.value)
    assert "refusing to apply" in message
    assert "estimate" in message


# --------------------------------------------------------------------------- #
# apply_plan - creation
# --------------------------------------------------------------------------- #


def test_apply_plan_creates_the_parent_before_the_child(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    result = plan.apply_plan(
        FakeClient(), apply_body(), state_path=tmp_path / "s.json", dry_run=False
    )
    assert [spec.summary for spec in recorder.specs] == ["Design", "Build"]
    assert [item["ref"] for item in result["created"]] == ["root", "child"]
    assert result["ok"] is True


def test_apply_plan_rewrites_a_parent_ref_into_the_created_key(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    result = plan.apply_plan(
        FakeClient(), apply_body(), state_path=tmp_path / "s.json", dry_run=False
    )
    root_key = next(item["key"] for item in result["created"] if item["ref"] == "root")
    assert recorder.by_summary("Build").parent_key == root_key
    assert recorder.by_summary("Design").parent_key is None


def test_apply_plan_passes_an_existing_key_parent_through_unchanged(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [{"ref": "t1", "summary": "Build", "description": "d",
                    "assignee": "alice", "estimate": "1h", "parent": "PROJ-12"}],
    }
    plan.apply_plan(FakeClient(), body, state_path=tmp_path / "s.json", dry_run=False)
    assert recorder.by_summary("Build").parent_key == "PROJ-12"


def test_apply_plan_builds_a_full_issue_spec_from_the_plan(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    body = {
        "defaults": {"project": "PROJ", "issue_type": "Task", "labels": ["ai"],
                     "priority": "Medium"},
        "issues": [{
            "ref": "t1", "summary": "Design", "description": "Pick a format.",
            "assignee": "alice", "estimate": "4h", "due": "2026-10-20",
            "type": "Story", "epic": "PROJ-12", "labels": ["backend"],
            "story_points": 3,
        }],
    }
    plan.apply_plan(FakeClient(), body, state_path=tmp_path / "s.json", dry_run=False)
    spec = recorder.by_summary("Design")
    assert spec.project == "PROJ"
    assert spec.issue_type == "Story"
    assert spec.description == "Pick a format."
    assert spec.assignee == "alice"
    assert spec.priority == "Medium"
    assert spec.original_estimate == "4h"
    assert spec.due_date == "2026-10-20"
    assert spec.epic_key == "PROJ-12"
    assert spec.labels == ["ai", "backend"]
    assert spec.story_points == 3


def test_apply_plan_sends_no_estimate_for_a_deliberate_none(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    body = simple_plan(estimate="none")
    plan.apply_plan(FakeClient(), body, state_path=tmp_path / "s.json", dry_run=False)
    assert recorder.specs[0].original_estimate is None


def test_apply_plan_resolves_sprint_active_to_the_board_sprint_id_once(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    calls: list[int] = []
    boards_mod.active_sprint = lambda client, board_id: (
        calls.append(board_id) or {"id": 77}
    )
    body = apply_body()
    body["defaults"].update({"sprint": "active", "board": 12})

    plan.apply_plan(FakeClient(), body, state_path=tmp_path / "s.json", dry_run=False)

    assert [spec.sprint_id for spec in recorder.specs] == [77, 77]
    # Two issues, one board: one lookup in the validation pass and one in the
    # creation pass. A third call would mean the per-board cache is not working.
    assert calls == [12, 12]


def test_apply_plan_passes_a_numeric_sprint_id_straight_through(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    body = simple_plan()
    body["defaults"]["sprint"] = 88
    plan.apply_plan(FakeClient(), body, state_path=tmp_path / "s.json", dry_run=False)
    assert recorder.specs[0].sprint_id == 88


def test_apply_plan_creates_links_after_every_issue_exists(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    order: list[str] = []
    links: list[tuple] = []

    def create(client, spec):
        order.append(f"create:{spec.summary}")
        return recorder.create(client, spec)

    def link_issues(client, from_key, to_key, link_type="Relates"):
        order.append(f"link:{from_key}->{to_key}")
        links.append((from_key, to_key, link_type))
        return {"ok": True}

    issues_mod.create_issue = create
    issues_mod.link_issues = link_issues
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "t1", "summary": "Design", "description": "d", "assignee": "alice",
             "estimate": "1h", "links": [{"type": "Blocks", "to": "t2"}]},
            {"ref": "t2", "summary": "Build", "description": "d", "assignee": "alice",
             "estimate": "1h"},
        ],
    }

    result = plan.apply_plan(
        FakeClient(), body, state_path=tmp_path / "s.json", dry_run=False
    )

    assert [step for step in order if step.startswith("create")] == [
        "create:Design", "create:Build"
    ]
    assert order[-1].startswith("link:")
    assert links == [("PROJ-101", "PROJ-102", "Blocks")]
    assert result["links"] == [
        {"from": "t1", "to": "t2", "type": "Blocks", "ok": True}
    ]


def test_apply_plan_records_a_link_failure_without_failing_the_run(tmp_path):
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    issues_mod.link_issues = lambda *a, **k: (_ for _ in ()).throw(
        errors.JiraError("no such link type")
    )
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "t1", "summary": "Design", "description": "d", "assignee": "alice",
             "estimate": "1h", "links": [{"type": "Nope", "to": "PROJ-9"}]},
        ],
    }
    result = plan.apply_plan(
        FakeClient(), body, state_path=tmp_path / "s.json", dry_run=False
    )
    assert result["created"]
    assert result["links"][0]["ok"] is False
    assert "no such link type" in result["links"][0]["error"]


# --------------------------------------------------------------------------- #
# apply_plan - state file, resume
# --------------------------------------------------------------------------- #


def test_apply_plan_writes_the_state_file_after_each_success(tmp_path):
    state_path = tmp_path / "state.json"
    seen: list[dict] = []

    recorder = Recorder()

    def create(client, spec):
        # Observe the state file as it looked *before* this creation.
        seen.append(
            json.loads(state_path.read_text(encoding="utf-8"))["created"]
            if state_path.exists()
            else {}
        )
        return recorder.create(client, spec)

    issues_mod.create_issue = create
    plan.apply_plan(FakeClient(), apply_body(), state_path=state_path, dry_run=False)

    assert seen[0] == {}, "nothing is recorded before the first creation"
    assert list(seen[1]) == ["root"], "the parent is persisted before the child runs"
    final = json.loads(state_path.read_text(encoding="utf-8"))
    assert sorted(final["created"]) == ["child", "root"]
    assert final["created"]["root"]["key"] == "PROJ-101"


def test_apply_plan_state_file_is_owner_only(tmp_path):
    state_path = tmp_path / "state.json"
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    plan.apply_plan(FakeClient(), apply_body(), state_path=state_path, dry_run=False)
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_apply_plan_defaults_the_state_file_next_to_the_plan(tmp_path):
    path = write_yaml(tmp_path, apply_body(), name="work.yaml")
    loaded = plan.load_plan(path)
    recorder = Recorder()
    issues_mod.create_issue = recorder.create

    result = plan.apply_plan(FakeClient(), loaded, dry_run=False)

    expected = tmp_path / "work.applied.json"
    assert result["state_path"] == str(expected)
    assert expected.exists()


def test_apply_plan_warns_when_an_in_memory_plan_cannot_be_resumed():
    recorder = Recorder()
    issues_mod.create_issue = recorder.create
    result = plan.apply_plan(FakeClient(), apply_body(), dry_run=False)
    assert result["state_path"] is None
    assert any("cannot be resumed" in line for line in result["warnings"])


def test_apply_plan_resumes_after_a_mid_run_failure(tmp_path):
    """The headline safety property: a failed run is re-runnable, not re-creating."""
    state_path = tmp_path / "state.json"
    body = apply_body()

    first_recorder = Recorder(fail={"Build": errors.JiraError("gateway timeout")})
    issues_mod.create_issue = first_recorder.create

    first = plan.apply_plan(FakeClient(), body, state_path=state_path, dry_run=False)

    assert [item["ref"] for item in first["created"]] == ["root"]
    assert [item["ref"] for item in first["failed"]] == ["child"]
    assert first["ok"] is False
    assert first["stopped"] is False
    root_key = first["created"][0]["key"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["created"] == {
        "root": {"key": root_key, "url": f"https://jira.example.com/browse/{root_key}"}
    }

    second_recorder = Recorder()
    issues_mod.create_issue = second_recorder.create

    second = plan.apply_plan(FakeClient(), body, state_path=state_path, dry_run=False)

    assert [spec.summary for spec in second_recorder.specs] == ["Build"], (
        "the already-created parent must not be created a second time"
    )
    assert [item["ref"] for item in second["skipped"]] == ["root"]
    assert second["skipped"][0]["key"] == root_key
    assert [item["ref"] for item in second["created"]] == ["child"]
    assert second["ok"] is True
    assert second_recorder.by_summary("Build").parent_key == root_key
    assert sorted(
        json.loads(state_path.read_text(encoding="utf-8"))["created"]
    ) == ["child", "root"]


def test_apply_plan_resume_does_not_recreate_links(tmp_path):
    state_path = tmp_path / "state.json"
    body = {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": "t1", "summary": "Design", "description": "d", "assignee": "alice",
             "estimate": "1h", "links": [{"type": "Blocks", "to": "PROJ-9"}]},
            {"ref": "t2", "summary": "Build", "description": "d", "assignee": "alice",
             "estimate": "1h"},
        ],
    }
    calls: list[tuple] = []
    issues_mod.link_issues = lambda client, a, b, t="Relates": calls.append((a, b, t))

    first_recorder = Recorder(fail={"Build": errors.JiraError("boom")})
    issues_mod.create_issue = first_recorder.create
    plan.apply_plan(FakeClient(), body, state_path=state_path, dry_run=False)
    assert calls == [("PROJ-101", "PROJ-9", "Blocks")]

    second_recorder = Recorder()
    issues_mod.create_issue = second_recorder.create
    second = plan.apply_plan(FakeClient(), body, state_path=state_path, dry_run=False)

    assert calls == [("PROJ-101", "PROJ-9", "Blocks")], "the link was already created"
    assert second["links"][0]["ok"] is True
    assert "previous run" in second["links"][0]["skipped"]


def test_apply_plan_rejects_a_corrupt_state_file(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{\"created\": []}", encoding="utf-8")
    issues_mod.create_issue = lambda client, spec: pytest.fail(
        "a corrupt state file must stop the run before anything is created"
    )
    with pytest.raises(errors.InputError) as caught:
        plan.apply_plan(FakeClient(), apply_body(), state_path=state_path, dry_run=False)
    assert "corrupt" in str(caught.value)


# --------------------------------------------------------------------------- #
# apply_plan - failure policy
# --------------------------------------------------------------------------- #


def three_issue_body() -> dict:
    return {
        "defaults": {"project": "PROJ"},
        "issues": [
            {"ref": f"t{index}", "summary": name, "description": "d",
             "assignee": "alice", "estimate": "1h"}
            for index, name in enumerate(("One", "Two", "Three"), start=1)
        ],
    }


@pytest.mark.parametrize("fatal", ["AuthError", "JiraPermissionError"])
def test_apply_plan_stops_at_the_first_auth_or_permission_error(tmp_path, fatal):
    exception = getattr(errors, fatal)("denied")
    recorder = Recorder(fail={"Two": exception})
    issues_mod.create_issue = recorder.create

    result = plan.apply_plan(
        FakeClient(), three_issue_body(), state_path=tmp_path / "s.json", dry_run=False
    )

    assert [spec.summary for spec in recorder.specs] == ["One", "Two"]
    assert [item["ref"] for item in result["created"]] == ["t1"]
    assert result["stopped"] is True
    assert result["ok"] is False
    failed = {item["ref"]: item["error"] for item in result["failed"]}
    assert "denied" in failed["t2"]
    assert "not attempted" in failed["t3"]


def test_apply_plan_skips_links_entirely_once_the_run_has_stopped(tmp_path):
    recorder = Recorder(fail={"Two": errors.AuthError("denied")})
    issues_mod.create_issue = recorder.create
    issues_mod.link_issues = lambda *a, **k: pytest.fail(
        "links must not be attempted after an auth failure"
    )
    body = three_issue_body()
    body["issues"][0]["links"] = [{"type": "Blocks", "to": "PROJ-9"}]
    result = plan.apply_plan(
        FakeClient(), body, state_path=tmp_path / "s.json", dry_run=False
    )
    assert result["links"] == []


def test_apply_plan_collects_other_failures_and_keeps_going(tmp_path):
    recorder = Recorder(fail={"Two": errors.JiraError("field required")})
    issues_mod.create_issue = recorder.create

    result = plan.apply_plan(
        FakeClient(), three_issue_body(), state_path=tmp_path / "s.json", dry_run=False
    )

    assert [spec.summary for spec in recorder.specs] == ["One", "Two", "Three"]
    assert [item["ref"] for item in result["created"]] == ["t1", "t3"]
    assert [item["ref"] for item in result["failed"]] == ["t2"]
    assert result["stopped"] is False
    assert result["ok"] is False


def test_apply_plan_reports_a_create_result_with_no_key_as_a_failure(tmp_path):
    issues_mod.create_issue = lambda client, spec: {"url": "x"}
    result = plan.apply_plan(
        FakeClient(), simple_plan(), state_path=tmp_path / "s.json", dry_run=False
    )
    assert result["created"] == []
    assert "no issue key" in result["failed"][0]["error"]


def test_apply_plan_carries_create_issue_warnings_through(tmp_path):
    issues_mod.create_issue = lambda client, spec: {
        "key": "PROJ-500", "url": "u", "warnings": ["sprint add failed"]
    }
    result = plan.apply_plan(
        FakeClient(), simple_plan(), state_path=tmp_path / "s.json", dry_run=False
    )
    assert result["created"][0]["warnings"] == ["sprint add failed"]


# --------------------------------------------------------------------------- #
# save_plan
# --------------------------------------------------------------------------- #


def test_save_plan_round_trips_through_load_plan(tmp_path):
    path = tmp_path / "out.yaml"
    body = simple_plan()
    body["name"] = "Import work"

    written = plan.save_plan(body, path)

    assert written == path
    reloaded = plan.load_plan(path)
    assert reloaded["name"] == "Import work"
    assert reloaded["issues"] == body["issues"]


def test_save_plan_drops_internal_keys(tmp_path):
    path = tmp_path / "out.yaml"
    body = simple_plan()
    body["_source"] = "/somewhere/else.yaml"
    plan.save_plan(body, path)
    assert "_source" not in yaml.safe_load(path.read_text(encoding="utf-8"))


def test_save_plan_leaves_no_temporary_file_behind(tmp_path):
    plan.save_plan(simple_plan(), tmp_path / "out.yaml")
    assert sorted(item.name for item in tmp_path.iterdir()) == ["out.yaml"]


def test_save_plan_replaces_an_existing_file_atomically(tmp_path):
    path = tmp_path / "out.yaml"
    path.write_text("issues: []\n", encoding="utf-8")
    plan.save_plan(simple_plan(), path)
    assert plan.load_plan(path)["issues"][0]["ref"] == "t1"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["out.yaml"]


def test_save_plan_refuses_to_write_over_a_directory(tmp_path):
    target = tmp_path / "adir"
    target.mkdir()
    with pytest.raises(errors.InputError) as caught:
        plan.save_plan(simple_plan(), target)
    assert "directory" in str(caught.value)


def test_save_plan_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "nested" / "deeper" / "out.yaml"
    plan.save_plan(simple_plan(), path)
    assert path.exists()
