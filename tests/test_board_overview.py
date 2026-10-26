"""Offline tests for ``jira_cli.services.board_overview``.

:class:`FakeJira` is a fake, not a script: it holds boards, statuses and issues
and answers the agile endpoints by evaluating the small JQL subset the service
emits (``status in/not in (...)``, ``sprint in (...)`` joined by ``AND``).  The
shapes mirror a live capture from Jira Software Server (kanban configs carry a
status-less "Backlog" slot at ``columns[0]``; ``/backlog`` answers 400 on
kanban; column statuses carry ids only).  No network, no real names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from jira_cli.errors import InputError, JiraError, NotFoundError
from jira_cli.services import board_overview as bo

AGILE = "/rest/agile/1.0"
STATUSES = {
    "10000": "To Do",
    "10100": "Backlog",
    "10101": "Selected for Development",
    "3": "In Progress",
    "10001": "Done",
    "10500": "Blocked",
}
PSEUDO_BACKLOG = ("Backlog", [])  # kanban columns[0] when the kanban backlog is off


@dataclass
class Issue:
    key: str
    status: str
    sprint: int | None = None
    assignee: str | None = "alice"
    reporter: str | None = "bob"


@dataclass
class Board:
    id: int
    name: str
    type: str
    columns: list[tuple[str, list[str]]]
    issues: list[Issue] = field(default_factory=list)
    sprints: list[dict] = field(default_factory=list)
    kanplan: bool = False
    project: str = "PROJ"
    broken: bool = False
    #: ``GET /board/{id}/project`` answers with the project (empty on some live boards).
    agile_knows_project: bool = True
    #: Saved-filter JQL; ``None`` means ``project = <project> ORDER BY Rank ASC``.
    filter_jql: str | None = None
    #: Project lookups fail with a server error (the row must survive it).
    project_lookup_fails: bool = False


def _person(username: str | None) -> dict | None:
    if username is None:
        return None
    return {"name": username, "key": username, "displayName": username.title() + " Example"}


def _raw_issue(issue: Issue) -> dict:
    return {
        "key": issue.key,
        "fields": {
            "summary": f"Summary of {issue.key}",
            "status": {"id": issue.status, "name": STATUSES.get(issue.status, issue.status)},
            "issuetype": {"id": "1", "name": "Task"},
            "priority": {"id": "3", "name": "Medium"},
            "assignee": _person(issue.assignee),
            "reporter": _person(issue.reporter),
            "updated": "2026-10-01T10:00:00.000+0000",
        },
    }


_CLAUSE = re.compile(r"(status|sprint) (not in|in) \(([^)]*)\)")


def _matches(issue: Issue, jql: str | None) -> bool:
    if not jql:
        return True
    for name, operator, values in _CLAUSE.findall(jql):
        wanted = {v.strip().strip('"') for v in values.split(",")}
        actual = issue.status if name == "status" else str(issue.sprint)
        if (actual in wanted) != (operator == "in"):
            return False
    return True


class FakeJira:
    """Behaving stand-in for :class:`jira_cli.client.JiraClient` (reads only)."""

    def __init__(self, *boards: Board, status_list_fails: bool = False) -> None:
        self.boards = {b.id: b for b in boards}
        self.status_list_fails = status_list_fails
        self.calls: list[tuple[str, str, dict]] = []

    # -- routing helpers
    def _board(self, board_id: str) -> Board:
        board = self.boards.get(int(board_id))
        if board is None:
            raise NotFoundError("board does not exist", status=404)
        return board

    def _selected(self, path: str, params: dict) -> list[Issue]:
        match = re.fullmatch(rf"{AGILE}/board/(\d+)/(issue|backlog)", path)
        assert match, f"unexpected issue path {path}"
        board = self._board(match.group(1))
        if match.group(2) == "backlog":
            if board.type == "kanban":
                raise JiraError("The backlog is not available on Kanban boards", status=400)
            pool = [i for i in board.issues if i.sprint is None]
        else:
            pool = board.issues
        return [i for i in pool if _matches(i, params.get("jql"))]

    # -- client surface
    def get(self, path: str, params: dict | None = None) -> Any:
        params = dict(params or {})
        self.calls.append(("get", path, params))
        if path == "/rest/api/2/status":
            if self.status_list_fails:
                raise JiraError("status list unavailable", status=500)
            return [{"id": k, "name": v} for k, v in STATUSES.items()]
        if path == "/rest/greenhopper/1.0/rapidviewconfig/editmodel.json":
            return {"isKanPlanEnabled": self._board(params["rapidViewId"]).kanplan}
        if path.startswith("/rest/greenhopper/1.0/"):
            return {}  # no GreenHopper sprint fallback data
        if m := re.fullmatch(rf"{AGILE}/board/(\d+)", path):
            board = self._board(m.group(1))
            return {"id": board.id, "name": board.name, "type": board.type}
        if m := re.fullmatch(rf"{AGILE}/board/(\d+)/configuration", path):
            board = self._board(m.group(1))
            if board.broken:
                raise JiraError("configuration exploded", status=500)
            columns = [{"name": n, "statuses": [{"id": s} for s in sids]} for n, sids in board.columns]
            return {
                "id": board.id,
                "type": board.type,
                "filter": {"id": str(board.id * 10)},
                "columnConfig": {"columns": columns},
            }
        if m := re.fullmatch(rf"{AGILE}/board/(\d+)/project", path):
            board = self._board(m.group(1))
            if board.project_lookup_fails:
                raise JiraError("project lookup failed", status=500)
            known = [{"key": board.project, "name": f"{board.project} name"}]
            values = known if board.agile_knows_project else []
            return {"maxResults": 50, "startAt": 0, "total": len(values), "values": values}
        if m := re.fullmatch(r"/rest/api/2/filter/(\d+)", path):
            board = self._board(str(int(m.group(1)) // 10))
            if board.project_lookup_fails:
                raise JiraError("filter lookup failed", status=500)
            jql = board.filter_jql or f"project = {board.project} ORDER BY Rank ASC"
            return {"id": m.group(1), "jql": jql}
        selected = self._selected(path, params)
        assert params.get("maxResults") == 0, "a GET on an issue list must be a count"
        return {"maxResults": 1, "total": len(selected), "issues": [_raw_issue(i) for i in selected[:1]]}

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        *,
        items_key: str = "values",
        page_size: int = 100,
        limit: int | None = None,
    ) -> list[dict]:
        params = dict(params or {})
        self.calls.append(("paginate", path, params))
        if path == f"{AGILE}/board":
            wanted = params.get("projectKeyOrId")
            return [
                {"id": b.id, "name": b.name, "type": b.type}
                for b in self.boards.values()
                if wanted in (None, b.project)
            ]
        if m := re.fullmatch(rf"{AGILE}/board/(\d+)/sprint", path):
            states = set((params.get("state") or "").split(","))
            return [s for s in self._board(m.group(1)).sprints if s["state"] in states]
        assert params.get("fields") == bo.ISSUE_FIELDS
        rows = [_raw_issue(i) for i in self._selected(path, params)]
        return rows[:limit] if limit is not None else rows

    def issue_fetches(self) -> list[tuple[str, str, dict]]:
        return [
            c for c in self.calls if c[0] == "paginate" and "/sprint" not in c[1] and c[1] != f"{AGILE}/board"
        ]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def kanban_board(**overrides: Any) -> Board:
    values: dict[str, Any] = {
        "id": 37,
        "name": "PROJ board",
        "type": "kanban",
        "columns": [
            PSEUDO_BACKLOG,
            ("Backlog", ["10100"]),
            ("Selected for Development", ["10101"]),
            ("In Progress", ["3"]),
            ("Done", ["10001"]),
        ],
        "issues": [
            Issue("PROJ-1", "10100"),
            Issue("PROJ-2", "10100", assignee=None),
            Issue("PROJ-3", "10101"),
            Issue("PROJ-4", "3", assignee="carol"),
            Issue("PROJ-5", "10001"),
            Issue("PROJ-6", "10001"),
            Issue("PROJ-7", "10001"),
        ],
    }
    values.update(overrides)
    return Board(**values)


def scrum_board(**overrides: Any) -> Board:
    values: dict[str, Any] = {
        "id": 50,
        "name": "PROJ scrum",
        "type": "scrum",
        "columns": [("To Do", ["10000"]), ("In Progress", ["3"]), ("Done", ["10001"])],
        "sprints": [
            {"id": 16, "name": "Sprint 4", "state": "active", "endDate": "2026-11-07T09:55:00.000+0000"},
            {"id": 17, "name": "Sprint 5", "state": "future"},
        ],
        "issues": [
            Issue("PROJ-10", "10000", sprint=16),
            Issue("PROJ-11", "3", sprint=16, assignee="carol"),
            Issue("PROJ-12", "3", sprint=16),
            Issue("PROJ-13", "10001", sprint=16),
            Issue("PROJ-14", "10000", sprint=17),  # future sprint: neither board nor backlog list
            Issue("PROJ-15", "10000"),
            Issue("PROJ-16", "10000", reporter="carol"),
        ],
    }
    values.update(overrides)
    return Board(**values)


def counts(overview: dict) -> dict[str, int]:
    return {c["name"]: c["count"] for c in overview["columns"]}


# --------------------------------------------------------------------------- #
# kanban
# --------------------------------------------------------------------------- #


def test_kanban_without_backlog_drops_empty_backlog_slot_and_counts_every_column():
    overview = bo.board_overview(FakeJira(kanban_board()), 37)

    assert overview["board"] == {"id": 37, "name": "PROJ board", "type": "kanban", "project": None}
    assert counts(overview) == {"Backlog": 2, "Selected for Development": 1, "In Progress": 1, "Done": 3}
    assert [c["name"] for c in overview["columns"]] == [
        "Backlog",
        "Selected for Development",
        "In Progress",
        "Done",
    ]
    assert overview["backlog"] is None
    assert overview["sprints"] == []
    assert overview["total"] == 7


def test_kanban_column_lists_status_names_and_issue_rows():
    overview = bo.board_overview(FakeJira(kanban_board()), 37)

    backlog_column = overview["columns"][0]
    assert backlog_column["statuses"] == ["Backlog"]
    assert [r["key"] for r in backlog_column["issues"]] == ["PROJ-1", "PROJ-2"]
    assert backlog_column["issues"][0] == {
        "key": "PROJ-1",
        "summary": "Summary of PROJ-1",
        "status": "Backlog",
        "type": "Task",
        "priority": "Medium",
        "assignee": "Alice Example",
        "assignee_username": "alice",
        "reporter": "Bob Example",
        "reporter_username": "bob",
        "updated": "2026-10-01T10:00:00.000+0000",
    }


def test_unassigned_issue_row_has_no_assignee():
    overview = bo.board_overview(FakeJira(kanban_board()), 37)

    unassigned = overview["columns"][0]["issues"][1]
    assert unassigned["assignee"] is None
    assert unassigned["assignee_username"] is None


def test_kanban_with_backlog_enabled_reports_backlog_only_once():
    board = kanban_board(
        kanplan=True,
        columns=[
            ("Backlog", ["10100"]),
            ("Selected for Development", ["10101"]),
            ("In Progress", ["3"]),
            ("Done", ["10001"]),
        ],
    )
    overview = bo.board_overview(FakeJira(board), 37)

    assert overview["backlog"]["count"] == 2
    assert [r["key"] for r in overview["backlog"]["issues"]] == ["PROJ-1", "PROJ-2"]
    assert counts(overview) == {"Selected for Development": 1, "In Progress": 1, "Done": 3}
    assert overview["total"] == 7


def test_kanban_never_calls_backlog_endpoint():
    jira = FakeJira(kanban_board())
    bo.board_overview(jira, 37)

    assert not [c for c in jira.calls if c[1].endswith("/backlog")]


# --------------------------------------------------------------------------- #
# scrum
# --------------------------------------------------------------------------- #


def test_scrum_columns_count_only_active_sprint_and_backlog_is_separate():
    overview = bo.board_overview(FakeJira(scrum_board()), 50)

    assert counts(overview) == {"To Do": 1, "In Progress": 2, "Done": 1}
    assert overview["backlog"]["count"] == 2
    assert [r["key"] for r in overview["backlog"]["issues"]] == ["PROJ-15", "PROJ-16"]
    assert overview["backlog"]["issues"][1]["reporter_username"] == "carol"
    assert overview["sprints"] == [
        {"id": 16, "name": "Sprint 4", "state": "active", "end_date": "2026-11-07T09:55:00.000+0000"}
    ]
    assert overview["total"] == 6


def test_scrum_without_active_sprint_has_zero_columns_and_still_counts_backlog():
    board = scrum_board(sprints=[], issues=[Issue("PROJ-20", "10000"), Issue("PROJ-21", "3")])
    jira = FakeJira(board)
    overview = bo.board_overview(jira, 50)

    assert overview["sprints"] == []
    assert counts(overview) == {"To Do": 0, "In Progress": 0, "Done": 0}
    assert overview["unmapped"] == {"count": 0, "issues": []}
    assert overview["backlog"]["count"] == 2
    assert overview["total"] == 2
    assert not [c for c in jira.calls if c[1].endswith("/board/50/issue")]


# --------------------------------------------------------------------------- #
# unmapped, include_issues, limit
# --------------------------------------------------------------------------- #


def test_issue_with_status_in_no_column_is_unmapped_and_in_total():
    board = kanban_board(issues=[Issue("PROJ-1", "10100"), Issue("PROJ-9", "10500")])
    overview = bo.board_overview(FakeJira(board), 37)

    assert overview["unmapped"]["count"] == 1
    assert [r["key"] for r in overview["unmapped"]["issues"]] == ["PROJ-9"]
    assert overview["total"] == 2


def test_counts_only_mode_fills_counts_and_fetches_no_issues():
    jira = FakeJira(kanban_board())
    overview = bo.board_overview(jira, 37, include_issues=False)

    assert counts(overview)["Done"] == 3
    assert all(c["issues"] == [] for c in overview["columns"])
    assert jira.issue_fetches() == []


def test_limit_truncates_a_full_column_and_flags_it():
    overview = bo.board_overview(FakeJira(kanban_board()), 37, limit=2)

    done = overview["columns"][3]
    assert done["count"] == 3
    assert len(done["issues"]) == 2
    assert done["truncated"] is True
    assert "truncated" not in overview["columns"][0]


def test_empty_column_is_not_fetched():
    jira = FakeJira(kanban_board(issues=[Issue("PROJ-1", "10100")]))
    bo.board_overview(jira, 37)

    fetched = [c[2]["jql"] for c in jira.issue_fetches()]
    assert fetched == ["status in (10100)"]


def test_status_names_fall_back_to_ids_when_status_list_fails():
    overview = bo.board_overview(FakeJira(kanban_board(), status_list_fails=True), 37)

    assert overview["columns"][0]["statuses"] == ["10100"]


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [0, -1, "abc", "", None, True, "3.5"])
def test_invalid_board_id_raises_input_error_without_calls(bad):
    jira = FakeJira(kanban_board())
    with pytest.raises(InputError):
        bo.board_overview(jira, bad)
    assert jira.calls == []


@pytest.mark.parametrize("bad", [0, -5, "many"])
def test_invalid_limit_raises_input_error(bad):
    with pytest.raises(InputError):
        bo.board_overview(FakeJira(kanban_board()), 37, limit=bad)


def test_unknown_board_raises_not_found():
    with pytest.raises(NotFoundError):
        bo.board_overview(FakeJira(kanban_board()), 999)


# --------------------------------------------------------------------------- #
# summaries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("workers", [1, 6])
def test_summaries_one_row_per_board_in_order_with_failing_board_isolated(workers):
    jira = FakeJira(
        kanban_board(),
        Board(id=40, name="Broken", type="kanban", columns=[PSEUDO_BACKLOG], broken=True),
        scrum_board(),
    )
    rows = bo.list_board_summaries(jira, max_workers=workers)

    assert [r["id"] for r in rows] == [37, 40, 50]
    kanban, broken, scrum = rows
    assert kanban == {
        "id": 37,
        "name": "PROJ board",
        "type": "kanban",
        "project": "PROJ",
        "total": 7,
        "backlog": None,
        "columns": [
            {"name": "Backlog", "count": 2},
            {"name": "Selected for Development", "count": 1},
            {"name": "In Progress", "count": 1},
            {"name": "Done", "count": 3},
        ],
        "sprint": None,
        "error": None,
    }
    assert "configuration exploded" in broken["error"]
    assert (broken["total"], broken["backlog"], broken["columns"]) == (0, None, [])
    assert scrum["sprint"] == "Sprint 4"
    assert scrum["backlog"] == 2
    assert scrum["total"] == 6


def test_summaries_use_count_queries_only():
    jira = FakeJira(kanban_board(), scrum_board())
    bo.list_board_summaries(jira)

    assert jira.issue_fetches() == []


def test_summaries_for_project_pass_project_filter_and_tag_rows():
    other = scrum_board(id=60, name="Other", project="OTHER")
    jira = FakeJira(kanban_board(), other)
    rows = bo.list_board_summaries(jira, "proj")

    assert [r["id"] for r in rows] == [37]
    assert rows[0]["project"] == "PROJ"


def test_summaries_reject_non_positive_workers():
    with pytest.raises(InputError):
        bo.list_board_summaries(FakeJira(kanban_board()), max_workers=0)


def test_unfiltered_summaries_fill_project_from_the_agile_board_project_endpoint():
    jira = FakeJira(kanban_board(project="ALPHA"), scrum_board(project="BRAVO"))

    rows = bo.list_board_summaries(jira, max_workers=1)

    assert [r["project"] for r in rows] == ["ALPHA", "BRAVO"]
    assert not [c for c in jira.calls if c[1].startswith("/rest/api/2/filter/")]


@pytest.mark.parametrize(
    ("jql", "expected"),
    [
        ("project = PROJ ORDER BY Rank ASC", "PROJ"),
        ('project = "proj" AND issuetype = Bug ORDER BY Rank ASC', "PROJ"),
        ("project in (ABC, XYZ) ORDER BY Rank ASC", "ABC,XYZ"),
        ('project in ("10001", "10002")', None),  # numeric ids: not nameable cheaply
        ("project = PROJ OR project = ABC", None),  # OR: restriction unknowable
        ("assignee = currentUser() AND project = PROJ", None),  # not a leading clause
    ],
)
def test_project_falls_back_to_the_board_filter_jql_when_agile_has_none(jql, expected):
    board = kanban_board(project="PROJ", agile_knows_project=False, filter_jql=jql)

    rows = bo.list_board_summaries(FakeJira(board))

    assert rows[0]["project"] == expected
    assert rows[0]["error"] is None


def test_project_lookup_failure_leaves_project_none_but_keeps_the_counts():
    rows = bo.list_board_summaries(FakeJira(kanban_board(project_lookup_fails=True)))

    assert rows[0]["project"] is None
    assert rows[0]["error"] is None
    assert rows[0]["total"] == 7


def test_summaries_for_a_project_skip_the_per_board_project_lookup():
    jira = FakeJira(kanban_board())

    bo.list_board_summaries(jira, "PROJ")

    assert not [c for c in jira.calls if c[1].endswith("/project") or "/filter/" in c[1]]


def test_failed_board_row_does_not_look_up_its_project():
    broken = Board(id=40, name="Broken", type="kanban", columns=[PSEUDO_BACKLOG], broken=True)
    jira = FakeJira(broken)

    rows = bo.list_board_summaries(jira)

    assert rows[0]["project"] is None
    assert not [c for c in jira.calls if c[1].endswith("/project")]
