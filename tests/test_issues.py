"""Tests for :mod:`jira_cli.services.issues`.

Offline and deterministic: a fake client answers a registered route table and
records every call, so no network, no clock and no real Jira are involved.

The real core modules (``errors``, ``fields``, ``durations``) are imported
directly; nothing is injected into :data:`sys.modules`, so this file cannot
leak stand-in modules into the rest of the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jira_cli.errors import InputError, JiraError, NotFoundError, ReadOnlyError  # noqa: E402
from jira_cli.fields import FieldMap  # noqa: E402
from jira_cli.services import issues  # noqa: E402
from jira_cli.services.issues import IssueSpec  # noqa: E402

API = "/rest/api/2"
AGILE = "/rest/agile/1.0"

#: Field ids this fake Jira reports, keyed by display name.
FIELD_LIST = [
    {"id": "summary", "name": "Summary"},
    {"id": "customfield_10109", "name": "Sprint"},
    {"id": "customfield_10108", "name": "Epic Link"},
    {"id": "customfield_10104", "name": "Epic Name"},
    {"id": "customfield_10110", "name": "Story Points"},
]
SPRINT_FIELD = "customfield_10109"
EPIC_FIELD = "customfield_10108"
POINTS_FIELD = "customfield_10110"


# --------------------------------------------------------------------------- #
# fake client
# --------------------------------------------------------------------------- #


class FakeSettings:
    """The slice of ``Settings`` that ``services.issues`` actually reads."""

    def __init__(
        self,
        *,
        base_url: str = "https://jira.example.com",
        default_project: str | None = None,
        read_only: bool = False,
        field_overrides: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url
        self.default_project = default_project
        self.read_only = read_only
        self.field_overrides = field_overrides or {}


class FakeClient:
    """A route-table fake standing in for ``JiraClient``.

    ``responses`` maps ``(method, path)`` to the value to return.  Registering
    an exception instance makes that route raise it; registering a list makes
    successive calls pop successive values.  An unregistered route raises
    ``AssertionError`` so a test can never pass by accident.
    """

    def __init__(
        self,
        responses: dict[tuple[str, str], object] | None = None,
        *,
        settings: FakeSettings | None = None,
        search_results: list[dict] | None = None,
        paginated: dict[str, object] | None = None,
        fields: list[dict] | None = None,
    ) -> None:
        self.settings = settings or FakeSettings()
        self.responses: dict[tuple[str, str], object] = {
            ("GET", f"{API}/field"): list(FIELD_LIST if fields is None else fields),
        }
        self.responses.update(responses or {})
        self.search_results = list(search_results or [])
        self.paginated = dict(paginated or {})
        self.calls: list[tuple[str, str, object]] = []
        self.search_calls: list[dict] = []
        self.writable_checks = 0

    # -- plumbing ---------------------------------------------------------- #

    def _answer(self, method: str, path: str, payload: object = None) -> object:
        self.calls.append((method, path, payload))
        try:
            value = self.responses[(method, path)]
        except KeyError:
            raise AssertionError(f"unexpected request: {method} {path}") from None
        if isinstance(value, list) and value and isinstance(value[0], _Sequenced):
            value = value.pop(0).value
        if isinstance(value, BaseException):
            raise value
        return value

    def paths(self, method: str | None = None) -> list[str]:
        """Paths requested so far, ignoring the lazy field-map lookup."""
        return [
            path
            for verb, path, _ in self.calls
            if path != f"{API}/field" and (method is None or verb == method)
        ]

    def payload_for(self, method: str, path: str) -> object:
        for verb, called_path, payload in self.calls:
            if verb == method and called_path == path:
                return payload
        raise AssertionError(f"{method} {path} was never called")

    # -- JiraClient surface ------------------------------------------------ #

    def get(self, path: str, params: dict | None = None) -> object:
        return self._answer("GET", path, params)

    def post(self, path: str, json: object = None, params: dict | None = None) -> object:
        return self._answer("POST", path, json)

    def put(self, path: str, json: object = None, params: dict | None = None) -> object:
        return self._answer("PUT", path, json)

    def delete(self, path: str, params: dict | None = None) -> object:
        return self._answer("DELETE", path, params)

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        *,
        items_key: str = "values",
        page_size: int = 100,
        limit: int | None = None,
    ) -> list[dict]:
        self.calls.append(("PAGINATE", path, items_key))
        try:
            value = self.paginated[path]
        except KeyError:
            raise AssertionError(f"unexpected pagination: {path}") from None
        if isinstance(value, BaseException):
            raise value
        return list(value)

    def search(
        self,
        jql: str,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        self.search_calls.append({"jql": jql, "limit": limit})
        return list(self.search_results)

    def count(self, jql: str) -> int:
        return len(self.search_results)

    def myself(self) -> dict:
        return {"name": "alice", "displayName": "Alice Example"}

    def browse_url(self, key: str) -> str:
        return f"{self.settings.base_url}/browse/{key}"

    def ensure_writable(self) -> None:
        self.writable_checks += 1
        if self.settings.read_only:
            raise ReadOnlyError("this client is read-only")


class _Sequenced:
    """Wrapper marking one element of a per-call response sequence."""

    def __init__(self, value: object) -> None:
        self.value = value


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #


def raw_issue(**overrides) -> dict:
    """A fully populated raw issue as Server 10.3 returns it."""
    fields = {
        "summary": "Add a login page",
        "issuetype": {"name": "Story", "subtask": False},
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "priority": {"name": "High"},
        "assignee": {"name": "alice", "displayName": "Alice Example"},
        "reporter": {"name": "bob", "displayName": "Bob Example"},
        "project": {"key": "PROJ", "name": "Project"},
        "created": "2026-10-01T09:00:00.000+0000",
        "updated": "2026-10-05T11:30:00.000+0000",
        "resolutiondate": None,
        "duedate": "2026-10-20",
        "labels": ["backend", "api"],
        "timeoriginalestimate": 10800,
        "timeestimate": 7200,
        "timespent": 3600,
        "description": "A description.",
        "parent": {"key": "PROJ-1"},
        POINTS_FIELD: 3.0,
        EPIC_FIELD: "PROJ-7",
        SPRINT_FIELD: ["Sprint 1"],
    }
    fields.update(overrides.pop("fields", {}))
    issue = {"id": "10001", "key": "PROJ-42", "fields": fields}
    issue.update(overrides)
    return issue


TRANSITIONS = {
    "transitions": [
        {"id": 11, "name": "Start Progress", "to": {"name": "In Progress"}},
        {"id": 21, "name": "Resolve Issue", "to": {"name": "Resolved"}},
        {"id": 31, "name": "Close Issue", "to": {"name": "Closed"}},
    ]
}

LINK_TYPES = {
    "issueLinkTypes": [
        {"id": 10000, "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        {"id": 10001, "name": "Relates", "inward": "relates to", "outward": "relates to"},
    ]
}


# --------------------------------------------------------------------------- #
# normalize_issue
# --------------------------------------------------------------------------- #


def test_normalize_issue_maps_every_contract_key():
    client = FakeClient()
    result = issues.normalize_issue(raw_issue(), client)

    assert result == {
        "key": "PROJ-42",
        "url": "https://jira.example.com/browse/PROJ-42",
        "summary": "Add a login page",
        "type": "Story",
        "status": "In Progress",
        "status_category": "In Progress",
        "priority": "High",
        "assignee": "alice",
        "assignee_name": "Alice Example",
        "reporter": "bob",
        "project": "PROJ",
        "created": "2026-10-01T09:00:00.000+0000",
        "updated": "2026-10-05T11:30:00.000+0000",
        "resolved": None,
        "due": "2026-10-20",
        "labels": ["backend", "api"],
        "estimate_seconds": 10800,
        "remaining_seconds": 7200,
        "spent_seconds": 3600,
        "story_points": 3.0,
        "epic": "PROJ-7",
        "parent": "PROJ-1",
        "sprint": "Sprint 1",
        "description": "A description.",
    }


def test_normalize_issue_tolerates_a_bare_key_only_issue():
    client = FakeClient()
    result = issues.normalize_issue({"key": "PROJ-9"}, client)

    assert result["key"] == "PROJ-9"
    assert result["url"] == "https://jira.example.com/browse/PROJ-9"
    assert result["labels"] == []
    assert result["status"] is None
    assert result["status_category"] is None
    assert result["assignee"] is None
    assert result["assignee_name"] is None
    assert result["sprint"] is None
    assert result["estimate_seconds"] is None


def test_normalize_issue_tolerates_an_empty_dict():
    client = FakeClient()
    result = issues.normalize_issue({}, client)

    assert result["key"] is None
    assert result["url"] is None
    assert result["labels"] == []


@pytest.mark.parametrize(
    ("category_key", "expected"),
    [
        ("new", "To Do"),
        ("indeterminate", "In Progress"),
        ("done", "Done"),
        ("undefined", "To Do"),
        ("NEW", "To Do"),
    ],
)
def test_normalize_issue_maps_status_categories(category_key, expected):
    client = FakeClient()
    raw = raw_issue(fields={"status": {"name": "X", "statusCategory": {"key": category_key}}})

    assert issues.normalize_issue(raw, client)["status_category"] == expected


def test_normalize_issue_leaves_unknown_status_category_as_none():
    client = FakeClient()
    raw = raw_issue(fields={"status": {"name": "X", "statusCategory": {"key": "weird"}}})

    assert issues.normalize_issue(raw, client)["status_category"] is None


def test_normalize_issue_reads_estimates_from_the_timetracking_block():
    client = FakeClient()
    raw = raw_issue(
        fields={
            "timeoriginalestimate": None,
            "timeestimate": None,
            "timespent": None,
            "timetracking": {
                "originalEstimateSeconds": 28800,
                "remainingEstimateSeconds": 14400,
                "timeSpentSeconds": 1800,
            },
        }
    )
    result = issues.normalize_issue(raw, client)

    assert (result["estimate_seconds"], result["remaining_seconds"], result["spent_seconds"]) == (
        28800,
        14400,
        1800,
    )


def test_normalize_issue_returns_none_story_points_when_the_field_is_absent():
    client = FakeClient(fields=[{"id": "summary", "name": "Summary"}])
    result = issues.normalize_issue(raw_issue(), client)

    assert result["story_points"] is None
    assert result["epic"] is None
    assert result["sprint"] is None


def test_normalize_issue_reuses_a_supplied_field_map():
    client = FakeClient()
    fmap = FieldMap(client)
    issues.normalize_issue(raw_issue(), client, fmap)
    issues.normalize_issue(raw_issue(), client, fmap)

    assert [p for _, p, _ in client.calls].count(f"{API}/field") <= 1


# --------------------------------------------------------------------------- #
# sprint parsing
# --------------------------------------------------------------------------- #

GREENHOPPER = (
    "com.atlassian.greenhopper.service.sprint.Sprint@1a2b"
    "[id=1,rapidViewId=2,state=ACTIVE,name=Sprint 1,startDate=2026-10-01T00:00:00.000Z,"
    "endDate=2026-10-14T00:00:00.000Z,completeDate=<null>,sequence=1,goal=ship it]"
)


def test_sprint_parses_the_greenhopper_tostring_form():
    client = FakeClient()
    raw = raw_issue(fields={SPRINT_FIELD: [GREENHOPPER]})

    assert issues.normalize_issue(raw, client)["sprint"] == "Sprint 1"


def test_sprint_parses_dict_entries():
    client = FakeClient()
    raw = raw_issue(
        fields={SPRINT_FIELD: [{"id": 5, "name": "Sprint 5", "state": "active"}]}
    )

    assert issues.normalize_issue(raw, client)["sprint"] == "Sprint 5"


def test_sprint_prefers_the_active_sprint_over_a_higher_id():
    client = FakeClient()
    raw = raw_issue(
        fields={
            SPRINT_FIELD: [
                {"id": 9, "name": "Sprint 9", "state": "FUTURE"},
                {"id": 4, "name": "Sprint 4", "state": "ACTIVE"},
                {"id": 1, "name": "Sprint 1", "state": "CLOSED"},
            ]
        }
    )

    assert issues.normalize_issue(raw, client)["sprint"] == "Sprint 4"


def test_sprint_falls_back_to_the_highest_id_when_none_is_active():
    client = FakeClient()
    raw = raw_issue(
        fields={
            SPRINT_FIELD: [
                {"id": 2, "name": "Sprint 2", "state": "CLOSED"},
                {"id": 7, "name": "Sprint 7", "state": "FUTURE"},
            ]
        }
    )

    assert issues.normalize_issue(raw, client)["sprint"] == "Sprint 7"


def test_sprint_mixes_string_and_dict_entries():
    client = FakeClient()
    raw = raw_issue(
        fields={
            SPRINT_FIELD: [
                GREENHOPPER.replace("state=ACTIVE", "state=CLOSED"),
                {"id": 3, "name": "Sprint 3", "state": "ACTIVE"},
            ]
        }
    )

    assert issues.normalize_issue(raw, client)["sprint"] == "Sprint 3"


def test_sprint_accepts_a_single_non_list_value():
    client = FakeClient()
    raw = raw_issue(fields={SPRINT_FIELD: GREENHOPPER})

    assert issues.normalize_issue(raw, client)["sprint"] == "Sprint 1"


def test_sprint_treats_a_bare_string_as_the_name():
    client = FakeClient()
    raw = raw_issue(fields={SPRINT_FIELD: ["Sprint 1"]})

    assert issues.normalize_issue(raw, client)["sprint"] == "Sprint 1"


def test_sprint_reads_null_name_as_none():
    client = FakeClient()
    nameless = GREENHOPPER.replace("name=Sprint 1", "name=<null>")
    raw = raw_issue(fields={SPRINT_FIELD: [nameless]})

    assert issues.normalize_issue(raw, client)["sprint"] is None


def test_sprint_ignores_unparseable_entries():
    client = FakeClient()
    raw = raw_issue(fields={SPRINT_FIELD: ["", None, 17]})

    assert issues.normalize_issue(raw, client)["sprint"] is None


def test_sprint_is_none_when_the_field_value_is_none():
    client = FakeClient()
    raw = raw_issue(fields={SPRINT_FIELD: None})

    assert issues.normalize_issue(raw, client)["sprint"] is None


# --------------------------------------------------------------------------- #
# get_issue
# --------------------------------------------------------------------------- #


def test_get_issue_uses_inline_comments_without_a_second_request():
    raw = raw_issue(
        fields={
            "comment": {
                "comments": [
                    {
                        "id": 1,
                        "author": {"name": "alice"},
                        "created": "2026-10-02T10:00:00.000+0000",
                        "body": "Looks good.",
                    }
                ]
            }
        }
    )
    client = FakeClient({("GET", f"{API}/issue/PROJ-42"): raw})
    result = issues.get_issue(client, "PROJ-42")

    assert result["comments"] == [
        {
            "author": "alice",
            "created": "2026-10-02T10:00:00.000+0000",
            "body": "Looks good.",
        }
    ]
    assert client.paths("PAGINATE") == []


def test_get_issue_paginates_comments_when_jira_inlined_none():
    client = FakeClient(
        {("GET", f"{API}/issue/PROJ-42"): raw_issue()},
        paginated={
            f"{API}/issue/PROJ-42/comment": [
                {"id": 7, "author": {"name": "bob"}, "created": "c", "body": "b"}
            ]
        },
    )
    result = issues.get_issue(client, "PROJ-42")

    assert result["comments"] == [{"author": "bob", "created": "c", "body": "b"}]


def test_get_issue_skips_comments_when_not_requested():
    client = FakeClient({("GET", f"{API}/issue/PROJ-42"): raw_issue()})
    result = issues.get_issue(client, "PROJ-42", comments=False)

    assert "comments" not in result
    assert client.paths("PAGINATE") == []


def test_get_issue_expands_and_flattens_the_changelog():
    raw = raw_issue(
        changelog={
            "histories": [
                {
                    "author": {"name": "alice"},
                    "created": "2026-10-03T08:00:00.000+0000",
                    "items": [
                        {
                            "field": "status",
                            "fromString": "To Do",
                            "toString": "In Progress",
                        }
                    ],
                }
            ]
        }
    )
    client = FakeClient({("GET", f"{API}/issue/PROJ-42"): raw})
    result = issues.get_issue(client, "PROJ-42", comments=False, changelog=True)

    assert client.payload_for("GET", f"{API}/issue/PROJ-42") == {"expand": "changelog"}
    assert result["changelog"] == [
        {
            "author": "alice",
            "created": "2026-10-03T08:00:00.000+0000",
            "items": [{"field": "status", "from": "To Do", "to": "In Progress"}],
        }
    ]


def test_get_issue_returns_an_empty_changelog_when_jira_sent_none():
    client = FakeClient({("GET", f"{API}/issue/PROJ-42"): raw_issue()})
    result = issues.get_issue(client, "PROJ-42", comments=False, changelog=True)

    assert result["changelog"] == []


def test_get_issue_propagates_a_not_found_error():
    client = FakeClient({("GET", f"{API}/issue/NOPE-1"): NotFoundError("no such issue")})

    with pytest.raises(NotFoundError):
        issues.get_issue(client, "NOPE-1")


# --------------------------------------------------------------------------- #
# search_issues / my_issues
# --------------------------------------------------------------------------- #


def test_search_issues_normalises_every_hit():
    client = FakeClient(search_results=[raw_issue(), raw_issue(key="PROJ-43")])
    result = issues.search_issues(client, "project = PROJ", limit=10)

    assert [r["key"] for r in result] == ["PROJ-42", "PROJ-43"]
    assert client.search_calls == [{"jql": "project = PROJ", "limit": 10}]


def test_search_issues_returns_an_empty_list_for_no_hits():
    client = FakeClient(search_results=[])

    assert issues.search_issues(client, "project = EMPTY") == []


@pytest.mark.parametrize("jql", ["", "   ", None])
def test_search_issues_rejects_an_empty_jql(jql):
    client = FakeClient()

    with pytest.raises(InputError):
        issues.search_issues(client, jql)


def test_search_issues_resolves_the_field_map_once_for_the_whole_page():
    client = FakeClient(search_results=[raw_issue() for _ in range(4)])
    issues.search_issues(client, "project = PROJ")

    assert [p for _, p, _ in client.calls].count(f"{API}/field") == 1


def test_my_issues_excludes_done_by_default():
    client = FakeClient(search_results=[raw_issue()])
    issues.my_issues(client)

    jql = client.search_calls[0]["jql"]
    assert jql.startswith("assignee = currentUser()")
    assert "statusCategory != Done" in jql
    assert jql.endswith("ORDER BY updated DESC")
    assert client.search_calls[0]["limit"] == 200


def test_my_issues_includes_done_when_asked():
    client = FakeClient(search_results=[])
    issues.my_issues(client, include_done=True, limit=5)

    assert "statusCategory" not in client.search_calls[0]["jql"]
    assert client.search_calls[0]["limit"] == 5


# --------------------------------------------------------------------------- #
# build_fields
# --------------------------------------------------------------------------- #


def test_build_fields_builds_the_full_server_payload():
    client = FakeClient()
    spec = IssueSpec(
        project="PROJ",
        summary="  Add a login page  ",
        issue_type="Story",
        description="Body text.",
        assignee="alice",
        priority="High",
        labels=["backend", "api"],
        due_date="2026-10-20",
        original_estimate="3h",
        epic_key="PROJ-7",
        components=["API"],
        fix_versions=["1.2.0"],
        story_points=5,
    )
    payload = issues.build_fields(client, spec)

    assert payload == {
        "project": {"key": "PROJ"},
        "summary": "Add a login page",
        "issuetype": {"name": "Story"},
        "description": "Body text.",
        "assignee": {"name": "alice"},
        "priority": {"name": "High"},
        "labels": ["backend", "api"],
        "duedate": "2026-10-20",
        "timetracking": {"originalEstimate": "3h"},
        "components": [{"name": "API"}],
        "fixVersions": [{"name": "1.2.0"}],
        EPIC_FIELD: "PROJ-7",
        POINTS_FIELD: 5,
    }


def test_build_fields_emits_only_the_mandatory_keys_for_a_bare_spec():
    client = FakeClient()
    payload = issues.build_fields(client, IssueSpec(project="PROJ", summary="Bare"))

    assert payload == {
        "project": {"key": "PROJ"},
        "summary": "Bare",
        "issuetype": {"name": "Task"},
    }


def test_build_fields_falls_back_to_the_configured_default_project():
    client = FakeClient(settings=FakeSettings(default_project="PROJ"))
    payload = issues.build_fields(client, IssueSpec(project="", summary="S"))

    assert payload["project"] == {"key": "PROJ"}


def test_build_fields_requires_a_project():
    client = FakeClient()

    with pytest.raises(InputError, match="project"):
        issues.build_fields(client, IssueSpec(project="", summary="S"))


@pytest.mark.parametrize("summary", ["", "   "])
def test_build_fields_requires_a_summary(summary):
    client = FakeClient()

    with pytest.raises(InputError, match="summary"):
        issues.build_fields(client, IssueSpec(project="PROJ", summary=summary))


def test_build_fields_requires_an_issue_type():
    client = FakeClient()

    with pytest.raises(InputError, match="issue type"):
        issues.build_fields(client, IssueSpec(project="PROJ", summary="S", issue_type=""))


def test_build_fields_rejects_a_label_containing_whitespace():
    client = FakeClient()
    spec = IssueSpec(project="PROJ", summary="S", labels=["two words"])

    with pytest.raises(InputError, match="whitespace"):
        issues.build_fields(client, spec)


def test_build_fields_drops_blank_labels():
    client = FakeClient()
    spec = IssueSpec(project="PROJ", summary="S", labels=["keep", "  ", ""])

    assert issues.build_fields(client, spec)["labels"] == ["keep"]


@pytest.mark.parametrize("due", ["20-10-2026", "2026/10/20", "tomorrow", "2026-10"])
def test_build_fields_rejects_a_non_iso_due_date(due):
    client = FakeClient()
    spec = IssueSpec(project="PROJ", summary="S", due_date=due)

    with pytest.raises(InputError, match="YYYY-MM-DD"):
        issues.build_fields(client, spec)


def test_build_fields_normalises_the_original_estimate_to_jira_format():
    client = FakeClient()
    spec = IssueSpec(project="PROJ", summary="S", original_estimate="90m")

    assert issues.build_fields(client, spec)["timetracking"] == {
        "originalEstimate": "1h 30m"
    }


def test_build_fields_rejects_an_unparseable_estimate():
    client = FakeClient()
    spec = IssueSpec(project="PROJ", summary="S", original_estimate="soon")

    with pytest.raises(InputError):
        issues.build_fields(client, spec)


def test_build_fields_sets_a_null_assignee_for_an_explicit_empty_username():
    client = FakeClient()
    spec = IssueSpec(project="PROJ", summary="S", assignee="")

    assert issues.build_fields(client, spec)["assignee"] == {"name": ""}


def test_build_fields_errors_when_the_epic_link_field_is_missing():
    client = FakeClient(fields=[{"id": "summary", "name": "Summary"}])
    spec = IssueSpec(project="PROJ", summary="S", epic_key="PROJ-7")

    with pytest.raises(InputError, match="Epic Link"):
        issues.build_fields(client, spec)


def test_build_fields_errors_when_the_story_points_field_is_missing():
    client = FakeClient(fields=[{"id": "summary", "name": "Summary"}])
    spec = IssueSpec(project="PROJ", summary="S", story_points=3)

    with pytest.raises(InputError, match="Story Points"):
        issues.build_fields(client, spec)


def test_build_fields_keeps_zero_story_points():
    client = FakeClient()
    spec = IssueSpec(project="PROJ", summary="S", story_points=0)

    assert issues.build_fields(client, spec)[POINTS_FIELD] == 0


# --- sub-task resolution --------------------------------------------------- #
#
# Jira 10.3 answers issue types through the two-hop createmeta (hop one is
# ``/issue/createmeta/{project}/issuetypes``); the legacy single-shot
# ``/issue/createmeta`` 404s there but is still tried for older servers, and
# ``/project/{key}`` is the last resort.

ISSUETYPES_PATH = f"{API}/issue/createmeta/PROJ/issuetypes"
LEGACY_CREATEMETA_PATH = f"{API}/issue/createmeta"
PROJECT_PATH = f"{API}/project/PROJ"

PROJ_ISSUE_TYPES = [
    {"id": "1", "name": "Task", "subtask": False},
    {"id": "5", "name": "Sub-task", "subtask": True},
    {"id": "6", "name": "Technical sub-task", "subtask": True},
]
#: Hop-one answer of the Jira 9+/10 createmeta (paged ``values``).
CREATEMETA_ISSUETYPES = {"maxResults": 50, "startAt": 0, "total": 3, "values": PROJ_ISSUE_TYPES}


def _two_hop_client() -> FakeClient:
    """A Jira 10.3 shape: the new createmeta answers, nothing else is registered."""
    return FakeClient({("GET", ISSUETYPES_PATH): CREATEMETA_ISSUETYPES})


def test_build_fields_adds_parent_and_a_subtask_type():
    client = _two_hop_client()
    spec = IssueSpec(project="PROJ", summary="S", parent_key="PROJ-1")
    payload = issues.build_fields(client, spec)

    assert payload["parent"] == {"key": "PROJ-1"}
    assert payload["issuetype"] == {"name": "Sub-task"}
    assert client.paths("GET") == [ISSUETYPES_PATH]


def test_build_fields_honours_a_requested_subtask_type_case_insensitively():
    client = _two_hop_client()
    spec = IssueSpec(
        project="PROJ",
        summary="S",
        issue_type="technical SUB-TASK",
        parent_key="PROJ-1",
    )

    assert issues.build_fields(client, spec)["issuetype"] == {
        "name": "Technical sub-task"
    }


def test_build_fields_ignores_a_requested_type_that_is_not_a_subtask_type():
    client = _two_hop_client()
    spec = IssueSpec(project="PROJ", summary="S", issue_type="Bug", parent_key="PROJ-1")

    assert issues.build_fields(client, spec)["issuetype"] == {"name": "Sub-task"}


def test_build_fields_uses_legacy_createmeta_matching_project_case_insensitively():
    meta = {"projects": [{"key": "proj", "issuetypes": [{"name": "Subtask", "subtask": True}]}]}
    client = FakeClient(
        {
            ("GET", ISSUETYPES_PATH): NotFoundError("no two-hop createmeta on this server"),
            ("GET", LEGACY_CREATEMETA_PATH): meta,
        }
    )
    spec = IssueSpec(project="PROJ", summary="S", parent_key="PROJ-1")

    assert issues.build_fields(client, spec)["issuetype"] == {"name": "Subtask"}
    assert client.paths("GET") == [ISSUETYPES_PATH, LEGACY_CREATEMETA_PATH]


def test_build_fields_falls_back_to_project_issue_types_when_createmeta_fails():
    client = FakeClient(
        {
            ("GET", ISSUETYPES_PATH): NotFoundError("no two-hop createmeta"),
            ("GET", LEGACY_CREATEMETA_PATH): NotFoundError("legacy createmeta removed"),
            ("GET", PROJECT_PATH): {
                "issueTypes": [
                    {"name": "Task", "subtask": False},
                    {"name": "Sub-task", "subtask": True},
                ]
            },
        }
    )
    spec = IssueSpec(project="PROJ", summary="S", parent_key="PROJ-1")

    assert issues.build_fields(client, spec)["issuetype"] == {"name": "Sub-task"}


def _no_types_client() -> FakeClient:
    """Every issue-type source refuses, so Jira reveals nothing."""
    return FakeClient(
        {
            ("GET", ISSUETYPES_PATH): NotFoundError("no two-hop createmeta"),
            ("GET", LEGACY_CREATEMETA_PATH): JiraError("denied"),
            ("GET", PROJECT_PATH): JiraError("denied"),
        }
    )


def test_build_fields_defaults_to_sub_task_when_jira_reveals_no_types():
    client = _no_types_client()
    spec = IssueSpec(project="PROJ", summary="S", parent_key="PROJ-1")

    assert issues.build_fields(client, spec)["issuetype"] == {"name": "Sub-task"}


def test_build_fields_keeps_an_explicit_type_when_jira_reveals_no_types():
    client = _no_types_client()
    spec = IssueSpec(
        project="PROJ", summary="S", issue_type="Technical sub-task", parent_key="PROJ-1"
    )

    assert issues.build_fields(client, spec)["issuetype"] == {
        "name": "Technical sub-task"
    }


# --------------------------------------------------------------------------- #
# create_issue
# --------------------------------------------------------------------------- #


def test_create_issue_posts_the_payload_and_returns_key_url_warnings():
    client = FakeClient({("POST", f"{API}/issue"): {"id": "1", "key": "PROJ-100"}})
    result = issues.create_issue(client, IssueSpec(project="PROJ", summary="New work"))

    assert result == {
        "key": "PROJ-100",
        "url": "https://jira.example.com/browse/PROJ-100",
        "warnings": [],
    }
    assert client.payload_for("POST", f"{API}/issue") == {
        "fields": {
            "project": {"key": "PROJ"},
            "summary": "New work",
            "issuetype": {"name": "Task"},
        }
    }
    assert client.writable_checks >= 1


def test_create_issue_refuses_to_write_in_read_only_mode():
    client = FakeClient(settings=FakeSettings(read_only=True))

    with pytest.raises(ReadOnlyError):
        issues.create_issue(client, IssueSpec(project="PROJ", summary="S"))

    assert client.paths("POST") == []


def test_create_issue_raises_when_jira_returns_no_key():
    client = FakeClient({("POST", f"{API}/issue"): {"id": "1"}})

    with pytest.raises(JiraError, match="did not return a key"):
        issues.create_issue(client, IssueSpec(project="PROJ", summary="S"))


def test_create_issue_raises_when_jira_returns_an_empty_body():
    client = FakeClient({("POST", f"{API}/issue"): None})

    with pytest.raises(JiraError, match="did not return a key"):
        issues.create_issue(client, IssueSpec(project="PROJ", summary="S"))


def test_create_issue_adds_the_new_issue_to_a_sprint():
    client = FakeClient(
        {
            ("POST", f"{API}/issue"): {"key": "PROJ-100"},
            ("POST", f"{AGILE}/sprint/12/issue"): None,
        }
    )
    result = issues.create_issue(
        client, IssueSpec(project="PROJ", summary="S", sprint_id=12)
    )

    assert result["warnings"] == []
    assert client.payload_for("POST", f"{AGILE}/sprint/12/issue") == {
        "issues": ["PROJ-100"]
    }


def test_create_issue_turns_a_sprint_failure_into_a_warning():
    client = FakeClient(
        {
            ("POST", f"{API}/issue"): {"key": "PROJ-100"},
            ("POST", f"{AGILE}/sprint/12/issue"): JiraError("sprint is closed"),
        }
    )
    result = issues.create_issue(
        client, IssueSpec(project="PROJ", summary="S", sprint_id=12)
    )

    assert result["key"] == "PROJ-100"
    assert len(result["warnings"]) == 1
    assert "sprint 12" in result["warnings"][0]
    assert "sprint is closed" in result["warnings"][0]


def test_create_issue_creates_links_after_create():
    client = FakeClient(
        {
            ("POST", f"{API}/issue"): {"key": "PROJ-100"},
            ("GET", f"{API}/issueLinkType"): LINK_TYPES,
            ("POST", f"{API}/issueLink"): None,
        }
    )
    spec = IssueSpec(project="PROJ", summary="S", links=[("Blocks", "PROJ-9")])
    result = issues.create_issue(client, spec)

    assert result["warnings"] == []
    assert client.payload_for("POST", f"{API}/issueLink") == {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": "PROJ-100"},
        "outwardIssue": {"key": "PROJ-9"},
    }


def test_create_issue_turns_a_link_failure_into_a_warning():
    client = FakeClient(
        {
            ("POST", f"{API}/issue"): {"key": "PROJ-100"},
            ("GET", f"{API}/issueLinkType"): LINK_TYPES,
            ("POST", f"{API}/issueLink"): JiraError("link not permitted"),
        }
    )
    spec = IssueSpec(project="PROJ", summary="S", links=[("Blocks", "PROJ-9")])
    result = issues.create_issue(client, spec)

    assert result["key"] == "PROJ-100"
    assert len(result["warnings"]) == 1
    assert "PROJ-100 -> PROJ-9" in result["warnings"][0]
    assert "Blocks" in result["warnings"][0]


def test_create_issue_turns_an_invalid_link_type_into_a_warning():
    client = FakeClient(
        {
            ("POST", f"{API}/issue"): {"key": "PROJ-100"},
            ("GET", f"{API}/issueLinkType"): LINK_TYPES,
        }
    )
    spec = IssueSpec(project="PROJ", summary="S", links=[("Nonsense", "PROJ-9")])
    result = issues.create_issue(client, spec)

    assert len(result["warnings"]) == 1
    assert "Nonsense" in result["warnings"][0]


def test_create_issue_collects_warnings_from_both_sprint_and_links():
    client = FakeClient(
        {
            ("POST", f"{API}/issue"): {"key": "PROJ-100"},
            ("POST", f"{AGILE}/sprint/3/issue"): JiraError("no such sprint"),
            ("GET", f"{API}/issueLinkType"): LINK_TYPES,
            ("POST", f"{API}/issueLink"): JiraError("nope"),
        }
    )
    spec = IssueSpec(
        project="PROJ",
        summary="S",
        sprint_id=3,
        links=[("Blocks", "PROJ-8"), ("Relates", "PROJ-9")],
    )
    result = issues.create_issue(client, spec)

    assert len(result["warnings"]) == 3


def test_create_issue_lets_a_validation_error_through_before_posting():
    client = FakeClient()

    with pytest.raises(InputError):
        issues.create_issue(client, IssueSpec(project="PROJ", summary=""))

    assert client.paths("POST") == []


# --------------------------------------------------------------------------- #
# update_fields
# --------------------------------------------------------------------------- #


def test_update_fields_maps_every_allowed_key():
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42"): None})
    changes = {
        "summary": "  Renamed  ",
        "description": "New body.",
        "priority": "Low",
        "labels": ["ops"],
        "due_date": "2026-11-01",
        "original_estimate": "2d",
        "remaining_estimate": "4h",
        "story_points": 8,
        "components": ["API"],
        "fix_versions": ["2.0"],
    }
    result = issues.update_fields(client, "PROJ-42", changes)

    payload = client.payload_for("PUT", f"{API}/issue/PROJ-42")["fields"]
    assert payload["summary"] == "Renamed"
    assert payload["description"] == "New body."
    assert payload["priority"] == {"name": "Low"}
    assert payload["labels"] == ["ops"]
    assert payload["duedate"] == "2026-11-01"
    assert payload["timetracking"] == {
        "originalEstimate": "2d",
        "remainingEstimate": "4h",
    }
    assert payload[POINTS_FIELD] == 8
    assert payload["components"] == [{"name": "API"}]
    assert payload["fixVersions"] == [{"name": "2.0"}]
    assert result == {
        "key": "PROJ-42",
        "url": "https://jira.example.com/browse/PROJ-42",
        "updated": sorted(changes),
    }


def test_update_fields_clears_a_due_date_with_a_falsy_value():
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42"): None})
    issues.update_fields(client, "PROJ-42", {"due_date": None})

    assert client.payload_for("PUT", f"{API}/issue/PROJ-42") == {
        "fields": {"duedate": None}
    }


def test_update_fields_clears_labels_with_an_empty_list():
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42"): None})
    issues.update_fields(client, "PROJ-42", {"labels": []})

    assert client.payload_for("PUT", f"{API}/issue/PROJ-42") == {
        "fields": {"labels": []}
    }


def test_update_fields_splits_comma_separated_labels_instead_of_iterating_characters():
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42"): None})
    issues.update_fields(client, "PROJ-42", {"labels": "urgent,backend"})

    assert client.payload_for("PUT", f"{API}/issue/PROJ-42") == {
        "fields": {"labels": ["urgent", "backend"]}
    }


def test_update_fields_splits_comma_separated_components_and_fix_versions():
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42"): None})
    issues.update_fields(client, "PROJ-42", {"components": "Core, API", "fix_versions": "2.0"})

    assert client.payload_for("PUT", f"{API}/issue/PROJ-42") == {
        "fields": {"components": [{"name": "Core"}, {"name": "API"}], "fixVersions": [{"name": "2.0"}]}
    }


def test_update_fields_rejects_a_non_string_label_list():
    client = FakeClient()

    with pytest.raises(InputError, match="labels"):
        issues.update_fields(client, "PROJ-42", {"labels": 5})


def test_update_fields_rejects_an_overlong_summary():
    client = FakeClient()

    with pytest.raises(InputError, match="255"):
        issues.update_fields(client, "PROJ-42", {"summary": "s" * 256})


def test_update_fields_rejects_an_unknown_key():
    client = FakeClient()

    with pytest.raises(InputError, match="unknown field"):
        issues.update_fields(client, "PROJ-42", {"assignee": "alice"})

    assert client.paths("PUT") == []


def test_update_fields_rejects_no_changes():
    client = FakeClient()

    with pytest.raises(InputError, match="no changes"):
        issues.update_fields(client, "PROJ-42", {})


def test_update_fields_rejects_an_empty_summary():
    client = FakeClient()

    with pytest.raises(InputError, match="summary"):
        issues.update_fields(client, "PROJ-42", {"summary": "   "})


def test_update_fields_rejects_a_bad_due_date():
    client = FakeClient()

    with pytest.raises(InputError, match="YYYY-MM-DD"):
        issues.update_fields(client, "PROJ-42", {"due_date": "next week"})


def test_update_fields_rejects_a_label_with_whitespace():
    client = FakeClient()

    with pytest.raises(InputError, match="whitespace"):
        issues.update_fields(client, "PROJ-42", {"labels": ["two words"]})


def test_update_fields_errors_when_story_points_is_unavailable():
    client = FakeClient(fields=[{"id": "summary", "name": "Summary"}])

    with pytest.raises(InputError, match="Story Points"):
        issues.update_fields(client, "PROJ-42", {"story_points": 3})


def test_update_fields_refuses_to_write_in_read_only_mode():
    client = FakeClient(settings=FakeSettings(read_only=True))

    with pytest.raises(ReadOnlyError):
        issues.update_fields(client, "PROJ-42", {"summary": "X"})


def test_update_fields_resolves_the_field_map_only_once():
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42"): None})
    issues.update_fields(client, "PROJ-42", {"story_points": 1, "summary": "S"})

    assert [p for _, p, _ in client.calls].count(f"{API}/field") == 1


# --------------------------------------------------------------------------- #
# assign
# --------------------------------------------------------------------------- #


def test_assign_sets_the_username_by_name():
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42/assignee"): None})
    result = issues.assign(client, "PROJ-42", "alice")

    assert client.payload_for("PUT", f"{API}/issue/PROJ-42/assignee") == {"name": "alice"}
    assert result == {
        "key": "PROJ-42",
        "url": "https://jira.example.com/browse/PROJ-42",
        "assignee": "alice",
    }


def test_assign_trims_the_username():
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42/assignee"): None})
    issues.assign(client, "PROJ-42", "  bob  ")

    assert client.payload_for("PUT", f"{API}/issue/PROJ-42/assignee") == {"name": "bob"}


@pytest.mark.parametrize("value", [None, "", "none", "NONE", "null", "unassigned", "-"])
def test_assign_unassigns_for_every_empty_spelling(value):
    client = FakeClient({("PUT", f"{API}/issue/PROJ-42/assignee"): None})
    result = issues.assign(client, "PROJ-42", value)

    assert client.payload_for("PUT", f"{API}/issue/PROJ-42/assignee") == {"name": None}
    assert result["assignee"] is None


def test_assign_refuses_to_write_in_read_only_mode():
    client = FakeClient(settings=FakeSettings(read_only=True))

    with pytest.raises(ReadOnlyError):
        issues.assign(client, "PROJ-42", "alice")


# --------------------------------------------------------------------------- #
# transitions
# --------------------------------------------------------------------------- #


def test_list_transitions_returns_id_name_and_target_status():
    client = FakeClient({("GET", f"{API}/issue/PROJ-42/transitions"): TRANSITIONS})

    assert issues.list_transitions(client, "PROJ-42") == [
        {"id": "11", "name": "Start Progress", "to": "In Progress"},
        {"id": "21", "name": "Resolve Issue", "to": "Resolved"},
        {"id": "31", "name": "Close Issue", "to": "Closed"},
    ]


def test_list_transitions_returns_an_empty_list_when_there_are_none():
    client = FakeClient({("GET", f"{API}/issue/PROJ-42/transitions"): {}})

    assert issues.list_transitions(client, "PROJ-42") == []


def _transition_client(extra: dict | None = None) -> FakeClient:
    responses = {
        ("GET", f"{API}/issue/PROJ-42/transitions"): TRANSITIONS,
        ("POST", f"{API}/issue/PROJ-42/transitions"): None,
    }
    responses.update(extra or {})
    return FakeClient(responses)


@pytest.mark.parametrize(
    "to",
    ["Resolve Issue", "resolve issue", "RESOLVE ISSUE", "Resolved", "resolved", "21"],
)
def test_transition_matches_name_status_and_id_case_insensitively(to):
    client = _transition_client()
    result = issues.transition(client, "PROJ-42", to)

    assert client.payload_for("POST", f"{API}/issue/PROJ-42/transitions") == {
        "transition": {"id": "21"}
    }
    assert result == {
        "key": "PROJ-42",
        "url": "https://jira.example.com/browse/PROJ-42",
        "transition": "Resolve Issue",
        "status": "Resolved",
        "comment_added": False,
    }


def test_transition_adds_a_comment_in_the_same_call():
    client = _transition_client()
    result = issues.transition(client, "PROJ-42", "Close Issue", comment="Shipped.")

    assert client.payload_for("POST", f"{API}/issue/PROJ-42/transitions") == {
        "transition": {"id": "31"},
        "update": {"comment": [{"add": {"body": "Shipped."}}]},
    }
    assert result["comment_added"] is True


def test_transition_errors_and_lists_options_when_nothing_matches():
    client = _transition_client()

    with pytest.raises(InputError) as caught:
        issues.transition(client, "PROJ-42", "Teleport")

    message = str(caught.value)
    assert "Teleport" in message
    for name in ("Start Progress", "Resolve Issue", "Close Issue"):
        assert name in message
    assert "id 21" in message
    assert client.paths("POST") == []


def test_transition_errors_when_there_are_no_transitions_at_all():
    client = FakeClient({("GET", f"{API}/issue/PROJ-42/transitions"): {}})

    with pytest.raises(InputError, match="none"):
        issues.transition(client, "PROJ-42", "Done")


def test_transition_errors_and_lists_candidates_when_ambiguous():
    ambiguous = {
        "transitions": [
            {"id": 11, "name": "Done", "to": {"name": "Closed"}},
            {"id": 12, "name": "Finish", "to": {"name": "Done"}},
        ]
    }
    client = FakeClient({("GET", f"{API}/issue/PROJ-42/transitions"): ambiguous})

    with pytest.raises(InputError) as caught:
        issues.transition(client, "PROJ-42", "done")

    message = str(caught.value)
    assert "ambiguous" in message
    assert "id 11" in message
    assert "id 12" in message
    assert client.paths("POST") == []


@pytest.mark.parametrize("to", ["", "   ", None])
def test_transition_requires_a_target(to):
    client = _transition_client()

    with pytest.raises(InputError, match="required"):
        issues.transition(client, "PROJ-42", to)


def test_transition_refuses_to_write_in_read_only_mode():
    client = FakeClient(settings=FakeSettings(read_only=True))

    with pytest.raises(ReadOnlyError):
        issues.transition(client, "PROJ-42", "Done")

    assert client.paths() == []


# --------------------------------------------------------------------------- #
# comments
# --------------------------------------------------------------------------- #


def test_add_comment_posts_the_body_and_returns_the_created_comment():
    created = {
        "id": 99,
        "author": {"name": "alice", "displayName": "Alice Example"},
        "created": "2026-10-06T12:00:00.000+0000",
        "body": "Done.",
    }
    client = FakeClient({("POST", f"{API}/issue/PROJ-42/comment"): created})
    result = issues.add_comment(client, "PROJ-42", "Done.")

    assert client.payload_for("POST", f"{API}/issue/PROJ-42/comment") == {"body": "Done."}
    assert result == {
        "id": "99",
        "author": "alice",
        "created": "2026-10-06T12:00:00.000+0000",
        "body": "Done.",
        "key": "PROJ-42",
        "url": "https://jira.example.com/browse/PROJ-42",
    }


def test_add_comment_tolerates_an_empty_response_body():
    client = FakeClient({("POST", f"{API}/issue/PROJ-42/comment"): None})
    result = issues.add_comment(client, "PROJ-42", "Done.")

    assert result["id"] is None
    assert result["key"] == "PROJ-42"


@pytest.mark.parametrize("body", ["", "   ", None])
def test_add_comment_rejects_an_empty_body(body):
    client = FakeClient()

    with pytest.raises(InputError, match="empty"):
        issues.add_comment(client, "PROJ-42", body)

    assert client.paths("POST") == []


def test_add_comment_refuses_to_write_in_read_only_mode():
    client = FakeClient(settings=FakeSettings(read_only=True))

    with pytest.raises(ReadOnlyError):
        issues.add_comment(client, "PROJ-42", "hi")


def test_list_comments_returns_every_page_with_ids():
    client = FakeClient(
        paginated={
            f"{API}/issue/PROJ-42/comment": [
                {"id": 1, "author": {"name": "alice"}, "created": "t1", "body": "one"},
                {"id": 2, "author": None, "created": "t2", "body": "two"},
            ]
        }
    )

    assert issues.list_comments(client, "PROJ-42") == [
        {"id": "1", "author": "alice", "created": "t1", "body": "one"},
        {"id": "2", "author": None, "created": "t2", "body": "two"},
    ]


def test_list_comments_returns_an_empty_list_for_no_comments():
    client = FakeClient(paginated={f"{API}/issue/PROJ-42/comment": []})

    assert issues.list_comments(client, "PROJ-42") == []


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def test_list_link_types_flattens_the_response():
    client = FakeClient({("GET", f"{API}/issueLinkType"): LINK_TYPES})

    assert issues.list_link_types(client) == [
        {
            "id": "10000",
            "name": "Blocks",
            "inward": "is blocked by",
            "outward": "blocks",
        },
        {
            "id": "10001",
            "name": "Relates",
            "inward": "relates to",
            "outward": "relates to",
        },
    ]


def test_list_link_types_tolerates_an_empty_response():
    client = FakeClient({("GET", f"{API}/issueLinkType"): {}})

    assert issues.list_link_types(client) == []


def test_link_issues_posts_inward_and_outward_keys():
    client = FakeClient(
        {
            ("GET", f"{API}/issueLinkType"): LINK_TYPES,
            ("POST", f"{API}/issueLink"): None,
        }
    )
    result = issues.link_issues(client, "PROJ-1", "PROJ-2", "Blocks")

    assert client.payload_for("POST", f"{API}/issueLink") == {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": "PROJ-1"},
        "outwardIssue": {"key": "PROJ-2"},
    }
    assert result == {
        "from": "PROJ-1",
        "to": "PROJ-2",
        "type": "Blocks",
        "url": "https://jira.example.com/browse/PROJ-1",
    }


def test_link_issues_defaults_to_relates():
    client = FakeClient(
        {
            ("GET", f"{API}/issueLinkType"): LINK_TYPES,
            ("POST", f"{API}/issueLink"): None,
        }
    )

    assert issues.link_issues(client, "PROJ-1", "PROJ-2")["type"] == "Relates"


def test_link_issues_canonicalises_the_link_type_casing():
    client = FakeClient(
        {
            ("GET", f"{API}/issueLinkType"): LINK_TYPES,
            ("POST", f"{API}/issueLink"): None,
        }
    )

    assert issues.link_issues(client, "PROJ-1", "PROJ-2", "blOcKs")["type"] == "Blocks"


def test_link_issues_rejects_an_unknown_link_type_and_lists_the_valid_ones():
    client = FakeClient({("GET", f"{API}/issueLinkType"): LINK_TYPES})

    with pytest.raises(InputError) as caught:
        issues.link_issues(client, "PROJ-1", "PROJ-2", "Duplicates")

    assert "Blocks" in str(caught.value)
    assert "Relates" in str(caught.value)
    assert client.paths("POST") == []


def test_link_issues_uses_the_given_type_when_link_types_cannot_be_read():
    client = FakeClient(
        {
            ("GET", f"{API}/issueLinkType"): JiraError("not permitted"),
            ("POST", f"{API}/issueLink"): None,
        }
    )

    assert issues.link_issues(client, "PROJ-1", "PROJ-2", "Blocks")["type"] == "Blocks"


def test_link_issues_uses_the_given_type_when_jira_lists_no_types():
    client = FakeClient(
        {
            ("GET", f"{API}/issueLinkType"): {"issueLinkTypes": []},
            ("POST", f"{API}/issueLink"): None,
        }
    )

    assert issues.link_issues(client, "PROJ-1", "PROJ-2", "Blocks")["type"] == "Blocks"


@pytest.mark.parametrize(("from_key", "to_key"), [("", "PROJ-2"), ("PROJ-1", ""), ("", "")])
def test_link_issues_requires_both_keys(from_key, to_key):
    client = FakeClient()

    with pytest.raises(InputError, match="both issue keys"):
        issues.link_issues(client, from_key, to_key)


def test_link_issues_refuses_a_self_link():
    client = FakeClient()

    with pytest.raises(InputError, match="itself"):
        issues.link_issues(client, "PROJ-1", "PROJ-1")


@pytest.mark.parametrize("link_type", ["", "   "])
def test_link_issues_requires_a_link_type(link_type):
    client = FakeClient()

    with pytest.raises(InputError, match="link type is required"):
        issues.link_issues(client, "PROJ-1", "PROJ-2", link_type)


def test_link_issues_refuses_to_write_in_read_only_mode():
    client = FakeClient(settings=FakeSettings(read_only=True))

    with pytest.raises(ReadOnlyError):
        issues.link_issues(client, "PROJ-1", "PROJ-2")


# --------------------------------------------------------------------------- #
# delete_issue
# --------------------------------------------------------------------------- #


def test_delete_issue_calls_delete_and_confirms():
    client = FakeClient({("DELETE", f"{API}/issue/PROJ-42"): None})

    assert issues.delete_issue(client, "PROJ-42") == {"key": "PROJ-42", "deleted": True}
    assert client.paths("DELETE") == [f"{API}/issue/PROJ-42"]


@pytest.mark.parametrize("key", ["", "   ", None])
def test_delete_issue_requires_a_key(key):
    client = FakeClient()

    with pytest.raises(InputError, match="issue key"):
        issues.delete_issue(client, key)

    assert client.paths("DELETE") == []


def test_delete_issue_refuses_to_write_in_read_only_mode():
    client = FakeClient(settings=FakeSettings(read_only=True))

    with pytest.raises(ReadOnlyError):
        issues.delete_issue(client, "PROJ-42")


def test_delete_issue_propagates_a_jira_refusal():
    client = FakeClient(
        {("DELETE", f"{API}/issue/PROJ-42"): JiraError("issue has sub-tasks")}
    )

    with pytest.raises(JiraError, match="sub-tasks"):
        issues.delete_issue(client, "PROJ-42")


# --------------------------------------------------------------------------- #
# path-segment validation (no traversal into other endpoints)
# --------------------------------------------------------------------------- #

#: Issue keys that must never reach a URL path.  The first one is the audited
#: exploit: urllib3 normalises ``..`` so it would become ``/project/PROJ``.
BAD_ISSUE_KEYS = [
    "PROJ-1/../../project/PROJ",
    "../PROJ-1",
    "PROJ-1?expand=x",
    "PROJ-1#frag",
    "PROJ 1",
    "PROJ- 1",
    "PROJ-1/comment",
    "PROJ-1%2F..",
    "PROJ",
    "0",
]
BAD_PROJECT_KEYS = ["PROJ/../../serverInfo", "PROJ?x=1", "PROJ#x", "PR OJ", "..", "1PROJ"]

ISSUE_KEY_OPERATIONS = {
    "get_issue": lambda c, k: issues.get_issue(c, k),
    "update_fields": lambda c, k: issues.update_fields(c, k, {"summary": "X"}),
    "assign": lambda c, k: issues.assign(c, k, "alice"),
    "list_transitions": lambda c, k: issues.list_transitions(c, k),
    "transition": lambda c, k: issues.transition(c, k, "Done"),
    "add_comment": lambda c, k: issues.add_comment(c, k, "hi"),
    "list_comments": lambda c, k: issues.list_comments(c, k),
    "link_issues_from": lambda c, k: issues.link_issues(c, k, "PROJ-2"),
    "link_issues_to": lambda c, k: issues.link_issues(c, "PROJ-2", k),
    "delete_issue": lambda c, k: issues.delete_issue(c, k),
}

PROJECT_KEY_OPERATIONS = {
    "createmeta_issue_types": lambda c, p: issues.createmeta_issue_types(c, p),
    "createmeta_fields": lambda c, p: issues.createmeta_fields(c, p, "Task"),
    "build_fields": lambda c, p: issues.build_fields(c, IssueSpec(project=p, summary="S")),
    "create_issue": lambda c, p: issues.create_issue(c, IssueSpec(project=p, summary="S")),
}


@pytest.mark.parametrize("operation", sorted(ISSUE_KEY_OPERATIONS))
@pytest.mark.parametrize("bad_key", BAD_ISSUE_KEYS)
def test_issue_operations_reject_unsafe_keys_before_any_request(operation, bad_key):
    client = FakeClient()

    with pytest.raises(InputError):
        ISSUE_KEY_OPERATIONS[operation](client, bad_key)

    assert client.calls == []


@pytest.mark.parametrize("operation", sorted(PROJECT_KEY_OPERATIONS))
@pytest.mark.parametrize("bad_project", BAD_PROJECT_KEYS)
def test_project_operations_reject_unsafe_project_keys_before_any_request(
    operation, bad_project
):
    client = FakeClient()

    with pytest.raises(InputError):
        PROJECT_KEY_OPERATIONS[operation](client, bad_project)

    assert client.paths() == []


def test_delete_issue_never_turns_a_traversal_key_into_a_project_delete():
    client = FakeClient({("DELETE", f"{API}/project/PROJ"): None})

    with pytest.raises(InputError, match="issue key"):
        issues.delete_issue(client, "PROJ-1/../../project/PROJ")

    assert client.paths("DELETE") == []


def test_build_fields_rejects_an_unsafe_parent_key():
    client = _two_hop_client()
    spec = IssueSpec(project="PROJ", summary="S", parent_key="PROJ-1/../PROJ-2")

    with pytest.raises(InputError, match="parent key"):
        issues.build_fields(client, spec)

    assert client.paths() == []


def test_create_issue_rejects_an_unsafe_sprint_id_before_creating_the_issue():
    client = FakeClient({("POST", f"{API}/issue"): {"key": "PROJ-100"}})
    spec = IssueSpec(project="PROJ", summary="S", sprint_id="12/../../13")  # type: ignore[arg-type]

    with pytest.raises(InputError, match="sprint id"):
        issues.create_issue(client, spec)

    assert client.paths("POST") == []


def test_createmeta_fields_rejects_an_unsafe_issue_type_id():
    client = FakeClient(
        {("GET", ISSUETYPES_PATH): {"values": [{"id": "5/../../x", "name": "Evil"}]}}
    )

    with pytest.raises(InputError, match="issue type id"):
        issues.createmeta_fields(client, "PROJ", "Evil")

    assert client.paths("GET") == [ISSUETYPES_PATH]


def test_issue_keys_are_normalised_to_upper_case_in_the_path():
    client = FakeClient({("DELETE", f"{API}/issue/PROJ-42"): None})

    assert issues.delete_issue(client, " proj-42 ") == {"key": "PROJ-42", "deleted": True}
    assert client.paths("DELETE") == [f"{API}/issue/PROJ-42"]


def test_a_numeric_issue_id_is_accepted_in_the_path():
    client = FakeClient({("GET", f"{API}/issue/10001"): raw_issue()})

    assert issues.get_issue(client, "10001", comments=False)["key"] == "PROJ-42"


def test_project_keys_are_normalised_to_upper_case_in_the_path():
    client = _two_hop_client()

    assert issues.createmeta_issue_types(client, "proj") == PROJ_ISSUE_TYPES


# --------------------------------------------------------------------------- #
# module surface
# --------------------------------------------------------------------------- #


def test_module_exposes_every_contract_function():
    expected = [
        "normalize_issue",
        "get_issue",
        "search_issues",
        "my_issues",
        "build_fields",
        "create_issue",
        "update_fields",
        "assign",
        "list_transitions",
        "transition",
        "add_comment",
        "list_comments",
        "link_issues",
        "list_link_types",
        "delete_issue",
    ]

    missing = [name for name in expected if not callable(getattr(issues, name, None))]
    assert missing == []


def test_issue_spec_defaults_are_independent_per_instance():
    first = IssueSpec(project="PROJ", summary="A")
    second = IssueSpec(project="PROJ", summary="B")
    first.labels.append("x")

    assert second.labels == []
    assert (first.issue_type, first.story_points, first.sprint_id) == ("Task", None, None)
