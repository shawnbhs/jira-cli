"""Services fed with the REAL Jira Server 10.3 response shapes.

Every payload here comes from ``tests/fixtures/*.json`` (sanitized captures,
see ``tests/fixtures/SHAPES.md``). Only the HTTP client is faked: an
unregistered route raises :class:`NotFoundError`, exactly like the real server
answering 404 -- which is what Jira 10.3 does for the legacy createmeta.

Entirely offline; no ``sys.modules`` patching.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jira_cli.errors import InputError, NotFoundError
from jira_cli.services import boards, issues, projects, sprint_report
from jira_cli.services.issues import IssueSpec

FIXTURES = Path(__file__).resolve().parent / "fixtures"

API = "/rest/api/2"
AGILE = "/rest/agile/1.0"
GH = "/rest/greenhopper/1.0"

SPRINT_FIELD = "customfield_10109"  # "Sprint" in field_list.json
EPIC_FIELD = "customfield_10108"  # "Epic Link"
BOARD_ID = 18  # first scrum board in scrum_board_lookup.json ("Sample name 2")
SPRINT_ID = 6  # the sprint captured in sprint_detail.json


def load(name: str) -> Any:
    """Load one fixture as fresh, mutable data."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClient:
    """Route-table stand-in for ``JiraClient``; unknown routes answer 404."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.settings = SimpleNamespace(
            base_url="https://jira.example.com", field_overrides={}, default_project=None
        )
        self.routes: dict[str, Any] = {f"{API}/field": load("field_list")}
        self.routes.update(routes or {})
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> Any:
        self.calls.append((path, dict(params or {})))
        if path not in self.routes:
            raise NotFoundError("Jira returned HTTP 404", status=404, url=path)
        value = self.routes[path]
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    def paginate(self, path: str, params: dict | None = None, *, items_key: str = "values",
                 page_size: int = 100, limit: int | None = None) -> list[dict]:
        payload = self.get(path, params)
        items = payload if isinstance(payload, list) else (payload or {}).get(items_key) or []
        return list(items)[:limit] if limit else list(items)

    def browse_url(self, key: str) -> str:
        return f"{self.settings.base_url}/browse/{key}"

    def requested(self, path: str) -> bool:
        return any(called == path for called, _ in self.calls)


def board_payload() -> dict:
    """``GET /rest/agile/1.0/board/18`` built from the captured board list."""
    return next(b for b in load("scrum_board_lookup")["values"] if b["id"] == BOARD_ID)


def sprint_routes(**extra: Any) -> dict[str, Any]:
    """Routes reproducing the live sprint situation: agile list EMPTY."""
    routes = {
        f"{AGILE}/board/{BOARD_ID}/sprint": load("scrum_board_sprints"),
        f"{AGILE}/board/{BOARD_ID}": board_payload(),
        f"{GH}/sprint/picker": load("greenhopper_sprintpicker"),
        f"{AGILE}/sprint/{SPRINT_ID}": load("sprint_detail"),
    }
    routes.update(extra)
    return routes


def sprint_blob(**overrides: str) -> str:
    """The captured Sprint toString() blob, with attributes replaced."""
    blob = load("sprint_issues")["issues"][0]["fields"][SPRINT_FIELD][0]
    for key, value in overrides.items():
        start = blob.index(f",{key}=") + len(key) + 2
        end = blob.index(",", start) if key != "synced" else blob.index("]", start)
        blob = blob[:start] + value + blob[end:]
    return blob


# --------------------------------------------------------------------------- #
# normalize_issue against /rest/agile/1.0/sprint/{id}/issue
# --------------------------------------------------------------------------- #


def test_normalize_issue_reads_the_captured_agile_sprint_issue():
    raw = load("sprint_issues")["issues"][0]

    result = issues.normalize_issue(raw, FakeClient())

    assert result["key"] == "PROJ-68"
    assert result["sprint"] == "Sample Sprint"  # from the toString() blob
    assert result["status_category"] == "Done"  # branched on statusCategory.key
    assert result["assignee"] == raw["fields"]["assignee"]["name"]
    assert result["epic"] == "PROJ-1"
    assert result["project"] == "PROJ"


def test_normalize_issue_treats_empty_timetracking_as_untracked():
    raw = load("sprint_issues")["issues"][0]
    assert raw["fields"].get("timetracking", {}) == {}

    result = issues.normalize_issue(raw, FakeClient())

    assert (result["estimate_seconds"], result["remaining_seconds"], result["spent_seconds"]) == (
        None, None, None,
    )


def test_normalize_issue_falls_back_to_top_level_seconds_without_timetracking():
    raw = load("search_changelog")["issues"][0]
    assert "timetracking" not in raw["fields"]

    result = issues.normalize_issue(raw, FakeClient())

    assert result["spent_seconds"] == raw["fields"]["timespent"]
    assert result["remaining_seconds"] == raw["fields"]["timeestimate"]


def test_normalize_issue_prefers_timetracking_over_disagreeing_top_level_fields():
    raw = load("sprint_issues")["issues"][0]
    raw["fields"]["timeoriginalestimate"] = 999
    raw["fields"]["timetracking"] = {
        "originalEstimate": "1d 4h",
        "originalEstimateSeconds": 43200,
        "remainingEstimateSeconds": 10800,
        "timeSpentSeconds": 19800,
    }

    result = issues.normalize_issue(raw, FakeClient())

    assert (result["estimate_seconds"], result["remaining_seconds"], result["spent_seconds"]) == (
        43200, 10800, 19800,
    )


def test_normalize_issue_ignores_the_localised_status_category_name():
    raw = load("sprint_issues")["issues"][0]
    raw["fields"]["status"]["statusCategory"]["name"] = "Erledigt"

    assert issues.normalize_issue(raw, FakeClient())["status_category"] == "Done"


def test_normalize_issue_maps_a_status_category_that_only_has_an_id():
    raw = load("sprint_issues")["issues"][0]
    raw["fields"]["status"]["statusCategory"] = {"id": 4}

    assert issues.normalize_issue(raw, FakeClient())["status_category"] == "In Progress"


def test_normalize_issue_reads_an_unassigned_issue_as_none():
    raw = load("sprint_issues")["issues"][0]
    raw["fields"]["assignee"] = None

    result = issues.normalize_issue(raw, FakeClient())

    assert (result["assignee"], result["assignee_name"]) == (None, None)


def test_normalize_issue_uses_the_user_key_when_name_is_missing():
    raw = load("sprint_issues")["issues"][0]
    del raw["fields"]["assignee"]["name"]

    assert issues.normalize_issue(raw, FakeClient())["assignee"] == raw["fields"]["assignee"]["key"]


def test_normalize_issue_uses_agile_sprint_object_when_custom_field_is_empty():
    raw = load("sprint_issues")["issues"][0]
    raw["fields"][SPRINT_FIELD] = None

    assert issues.normalize_issue(raw, FakeClient())["sprint"] == "Sample name 1"


# --------------------------------------------------------------------------- #
# Sprint toString() blob parsing
# --------------------------------------------------------------------------- #


def test_parse_sprint_blob_reads_the_captured_blob():
    pairs = issues.parse_sprint_blob(sprint_blob())

    assert pairs["id"] == "6"
    assert pairs["state"] == "ACTIVE"
    assert pairs["name"] == "Sample Sprint"
    assert pairs["rapidViewId"] == "9"
    assert pairs["startDate"] == "2025-09-07T18:16:00.000+00:00"


def test_parse_sprint_blob_turns_java_null_into_none():
    pairs = issues.parse_sprint_blob(sprint_blob())

    assert pairs["completeDate"] is None
    assert pairs["incompleteIssuesDestinationId"] is None


def test_parse_sprint_blob_keeps_commas_inside_name_and_goal():
    blob = sprint_blob(name="Sprint 3, hotfix=yes", goal="ship login, then billing")

    pairs = issues.parse_sprint_blob(blob)

    assert pairs["name"] == "Sprint 3, hotfix=yes"
    assert pairs["goal"] == "ship login, then billing"
    assert pairs["id"] == "6"


def test_normalize_issue_sprint_name_survives_a_comma():
    raw = load("sprint_issues")["issues"][0]
    raw["fields"][SPRINT_FIELD] = [sprint_blob(name="Team A, week 2")]

    assert issues.normalize_issue(raw, FakeClient())["sprint"] == "Team A, week 2"


def test_current_sprint_is_the_last_element_when_none_is_active():
    value = [
        sprint_blob(id="9", name="Older", state="CLOSED"),
        sprint_blob(id="6", name="Newer", state="CLOSED"),
    ]

    assert issues.current_sprint(value)["name"] == "Newer"


def test_current_sprint_prefers_the_active_entry_over_a_later_future_one():
    value = [
        sprint_blob(id="5", name="Closed one", state="CLOSED"),
        sprint_blob(id="6", name="Running", state="ACTIVE"),
        sprint_blob(id="7", name="Planned", state="FUTURE"),
    ]

    current = issues.current_sprint(value)

    assert (current["id"], current["name"], current["board_id"]) == (6, "Running", 9)


def test_parse_sprint_field_accepts_the_cloud_dict_shape():
    value = [load("sprint_detail")]

    parsed = issues.parse_sprint_field(value)

    assert parsed[0]["id"] == 6
    assert parsed[0]["state"] == "ACTIVE"
    assert parsed[0]["board_id"] == 9


# --------------------------------------------------------------------------- #
# createmeta: Jira 10.3 two-hop endpoints (legacy single-shot is a 404)
# --------------------------------------------------------------------------- #


def createmeta_client() -> FakeClient:
    return FakeClient(
        {
            f"{API}/issue/createmeta/PROJ/issuetypes": load("createmeta_issuetypes"),
            f"{API}/issue/createmeta/PROJ/issuetypes/10000": load("createmeta_fields"),
        }
    )


def test_list_issue_types_reads_the_two_hop_createmeta():
    client = createmeta_client()

    result = projects.list_issue_types(client, "PROJ")

    assert result == ["Epic", "Story", "Task", "Sub-task", "Bug"]
    assert not client.requested(f"{API}/issue/createmeta")


def test_list_issue_types_falls_back_to_the_project_when_createmeta_is_missing():
    client = FakeClient({f"{API}/project/PROJ": load("project_detail") | {"key": "PROJ"}})

    result = projects.list_issue_types(client, "PROJ")

    assert result == [t["name"] for t in load("project_detail")["issueTypes"]]


def test_build_fields_resolves_the_subtask_type_through_two_hop_createmeta():
    client = createmeta_client()
    spec = IssueSpec(project="PROJ", summary="S", parent_key="PROJ-1")

    payload = issues.build_fields(client, spec)

    assert payload["issuetype"] == {"name": "Sub-task"}
    assert not client.requested(f"{API}/issue/createmeta")


def test_createmeta_fields_resolves_a_type_name_and_reads_hop_two():
    client = createmeta_client()

    rows = issues.createmeta_fields(client, "PROJ", "epic")

    expected = load("createmeta_fields")["values"]
    assert [r["field_id"] for r in rows] == [f["fieldId"] for f in expected]
    assert [r["required"] for r in rows] == [f["required"] for f in expected]


def test_createmeta_fields_rejects_an_unknown_type_name():
    with pytest.raises(InputError) as excinfo:
        issues.createmeta_fields(createmeta_client(), "PROJ", "Saga")

    assert "Sub-task" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# sprint discovery: agile list is empty on this server -> GreenHopper
# --------------------------------------------------------------------------- #


def test_find_sprints_falls_back_to_the_greenhopper_picker():
    client = FakeClient(sprint_routes())

    found = boards.find_sprints(client, BOARD_ID, state="active,future")

    assert found["method"] == boards.SPRINTS_VIA_PICKER
    # id 10 has no boardName and id 15 belongs to another board: both dropped.
    assert [s["id"] for s in found["sprints"]] == [6, 7, 8]
    assert [s["state"] for s in found["sprints"]] == ["active", "future", "future"]


def test_list_sprints_enriches_greenhopper_rows_with_iso_dates():
    client = FakeClient(sprint_routes())

    rows = boards.list_sprints(client, BOARD_ID, state="active")

    assert rows == [
        {
            "id": 6,
            "name": "Sample name 1",
            "state": "active",
            "start": "2025-09-07T18:16:00.000+00:00",
            "end": "2025-09-14T18:16:00.000+00:00",
            "goal": "Sample text 1",
        }
    ]


def test_active_sprint_is_found_when_the_agile_list_is_empty():
    client = FakeClient(sprint_routes())

    assert boards.active_sprint(client, BOARD_ID)["id"] == SPRINT_ID


def test_find_sprints_prefers_greenhopper_sprintquery_when_available():
    # sprintquery was not captured; this payload follows its documented shape.
    sprintquery = {
        "rapidViewId": BOARD_ID,
        "sprints": [
            {"id": 5, "name": "Old", "state": "CLOSED", "sequence": 5},
            {"id": 6, "name": "Now", "state": "ACTIVE", "sequence": 6},
        ],
    }
    client = FakeClient(sprint_routes(**{f"{GH}/sprintquery/{BOARD_ID}": sprintquery}))

    found = boards.find_sprints(client, BOARD_ID, state="active")

    assert found["method"] == boards.SPRINTS_VIA_SPRINTQUERY
    assert [s["id"] for s in found["sprints"]] == [6]
    assert not client.requested(f"{GH}/sprint/picker")


def test_find_sprints_uses_agile_without_touching_greenhopper_when_it_answers():
    agile = {"isLast": True, "values": [load("sprint_detail")]}
    client = FakeClient(sprint_routes(**{f"{AGILE}/board/{BOARD_ID}/sprint": agile}))

    found = boards.find_sprints(client, BOARD_ID)

    assert found["method"] == boards.SPRINTS_VIA_AGILE
    assert not client.requested(f"{GH}/sprint/picker")


def test_find_sprints_reports_none_when_no_source_knows_a_sprint():
    client = FakeClient({f"{AGILE}/board/{BOARD_ID}/sprint": load("scrum_board_sprints")})

    assert boards.find_sprints(client, BOARD_ID) == {"sprints": [], "method": "none"}


# --------------------------------------------------------------------------- #
# sprint_report against the captured GreenHopper shapes
# --------------------------------------------------------------------------- #


def report_routes(**extra: Any) -> dict[str, Any]:
    routes = sprint_routes(**{f"{GH}/rapid/charts/sprintreport": load("greenhopper_sprintreport")})
    routes.update(extra)
    return routes


def test_sprint_report_discovers_the_active_sprint_through_greenhopper():
    client = FakeClient(report_routes())

    report = sprint_report.sprint_report(client, BOARD_ID)

    assert report["meta"]["method"] == "greenhopper"
    assert report["meta"]["sprint_discovery"] == boards.SPRINTS_VIA_PICKER
    assert report["meta"]["sprint_id"] == SPRINT_ID
    assert report["rows"] == []
    assert report["summary"]["committed"] == 0


def test_sprint_report_uses_iso_dates_instead_of_greenhopper_display_dates():
    client = FakeClient(report_routes())

    sprint = sprint_report.sprint_report(client, BOARD_ID, SPRINT_ID)["meta"]["sprint"]

    assert sprint["start"] == "2025-09-07T18:16:00.000+00:00"
    assert sprint["complete"] is None  # GreenHopper sends "", agile omits it


def test_sprint_report_keeps_display_dates_when_agile_detail_is_missing():
    routes = report_routes()
    del routes[f"{AGILE}/sprint/{SPRINT_ID}"]

    report = sprint_report.sprint_report(FakeClient(routes), BOARD_ID, SPRINT_ID)

    assert report["meta"]["sprint_dates"] == "display"
    assert report["meta"]["sprint"]["start"] == "07/Sep/25 6:16 PM"
    assert report["meta"]["sprint_discovery"] == sprint_report.DISCOVERY_EXPLICIT


def test_sprint_report_reads_greenhopper_issue_rows_and_added_keys_dict():
    chart = load("greenhopper_sprintreport")
    contents = chart["contents"]
    contents["completedIssues"] = [
        {"key": "PROJ-1", "summary": "a", "typeName": "Story", "statusName": "Done",
         "assignee": "alice", "done": True, "estimateStatistic": {"statFieldValue": {"value": 3.0}}},
    ]
    contents["issuesNotCompletedInCurrentSprint"] = [
        {"key": "PROJ-2", "summary": "b", "typeName": "Task", "statusName": "To Do",
         "assignee": "bob", "done": False, "estimateStatistic": {"statFieldValue": {}}},
    ]
    contents["issueKeysAddedDuringSprint"] = {"PROJ-2": True}
    client = FakeClient(report_routes(**{f"{GH}/rapid/charts/sprintreport": chart}))

    report = sprint_report.sprint_report(client, BOARD_ID, SPRINT_ID)

    assert [(r["key"], r["story_points"], r["added_after_start"]) for r in report["rows"]] == [
        ("PROJ-1", 3.0, False),
        ("PROJ-2", None, True),
    ]
    assert report["summary"]["committed"] == 1
    assert report["summary"]["completion_pct"] == 100.0


def test_sprint_report_falls_back_to_agile_with_the_captured_sprint_issues():
    routes = report_routes(**{f"{AGILE}/sprint/{SPRINT_ID}/issue": load("sprint_issues")})
    del routes[f"{GH}/rapid/charts/sprintreport"]

    report = sprint_report.sprint_report(FakeClient(routes), BOARD_ID)

    assert report["meta"]["method"] == "agile"
    assert report["meta"]["sprint_discovery"] == boards.SPRINTS_VIA_PICKER
    assert [r["category"] for r in report["rows"]] == ["completed"] * 3


def test_sprint_report_treats_a_contentless_greenhopper_answer_as_a_failure():
    routes = report_routes(**{
        f"{GH}/rapid/charts/sprintreport": {"sprint": {"id": SPRINT_ID}},
        f"{AGILE}/sprint/{SPRINT_ID}/issue": load("sprint_issues"),
    })

    report = sprint_report.sprint_report(FakeClient(routes), BOARD_ID, SPRINT_ID)

    assert report["meta"]["method"] == "agile"


def test_sprint_report_says_so_when_no_source_knows_an_active_sprint():
    client = FakeClient({f"{AGILE}/board/{BOARD_ID}/sprint": load("scrum_board_sprints")})

    with pytest.raises(InputError) as excinfo:
        sprint_report.sprint_report(client, BOARD_ID)

    assert "GreenHopper" in str(excinfo.value)


def test_velocity_reads_the_captured_empty_chart():
    client = FakeClient({sprint_report.GREENHOPPER_VELOCITY: load("greenhopper_velocity")})

    report = sprint_report.velocity(client, BOARD_ID)

    assert report["meta"]["method"] == "greenhopper"
    assert report["rows"] == []


def test_velocity_casts_sprint_ids_to_string_keys_and_orders_by_sequence():
    chart = load("greenhopper_velocity")
    chart["sprints"] = [
        {"id": 8, "name": "Later", "state": "CLOSED", "sequence": 8},
        {"id": 6, "name": "Earlier", "state": "CLOSED", "sequence": 6},
    ]
    chart["velocityStatEntries"] = {
        "6": {"estimated": {"value": 5.0}, "completed": {"value": 4.0}},
        "8": {"estimated": {"value": 3.0}, "completed": {"value": 3.0}},
    }
    client = FakeClient({sprint_report.GREENHOPPER_VELOCITY: chart})

    rows = sprint_report.velocity(client, BOARD_ID)["rows"]

    assert [(r["sprint"], r["committed"], r["completed"]) for r in rows] == [
        ("Earlier", 5.0, 4.0),
        ("Later", 3.0, 3.0),
    ]
