"""Offline tests for ``jira_cli.services.projects`` and ``jira_cli.services.boards``.

The real core modules (``errors``, ``fields``, ``durations``) are imported;
only the HTTP client is faked.  Nothing here touches ``sys.modules``, so no
state leaks into other test files.

Nothing here touches the network -- every call goes through :class:`FakeClient`,
which answers from a canned routing table and records what it was asked to do.
"""

from __future__ import annotations

from typing import Any

import pytest

from jira_cli.errors import InputError, JiraError, NotFoundError
from jira_cli.services import boards, projects

# --------------------------------------------------------------------------- #
# fake client
# --------------------------------------------------------------------------- #


class FakeSettings:
    """The slice of ``config.Settings`` these services read."""

    def __init__(self, base_url: str = "https://jira.example.com") -> None:
        self.base_url = base_url
        self.read_only = False


class FakeClient:
    """A recording, offline stand-in for :class:`jira_cli.client.JiraClient`.

    ``gets`` / ``pages`` / ``posts`` map a path to the canned response.  Values
    may be callables taking ``params``.  An unmapped path raises, so a test that
    makes an unexpected request fails loudly instead of silently seeing ``None``.
    """

    def __init__(
        self,
        gets: dict[str, Any] | None = None,
        pages: dict[str, Any] | None = None,
        posts: dict[str, Any] | None = None,
    ) -> None:
        self.settings = FakeSettings()
        self.gets = gets or {}
        self.pages = pages or {}
        self.posts = posts or {}
        self.get_calls: list[tuple[str, dict | None]] = []
        self.paginate_calls: list[tuple[str, dict | None, str, int | None]] = []
        self.post_calls: list[tuple[str, Any]] = []

    # -- reads
    def get(self, path: str, params: dict | None = None) -> Any:
        self.get_calls.append((path, params))
        return self._answer(self.gets, path, params)

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        *,
        items_key: str = "values",
        page_size: int = 100,
        limit: int | None = None,
    ) -> list[dict]:
        self.paginate_calls.append((path, params, items_key, limit))
        return list(self._answer(self.pages, path, params) or [])

    # -- writes
    def post(self, path: str, json: Any = None, params: dict | None = None) -> Any:
        self.post_calls.append((path, json))
        return self._answer(self.posts, path, json)

    # -- misc
    def browse_url(self, key: str) -> str:
        return f"{self.settings.base_url}/browse/{key}"

    def _answer(self, table: dict[str, Any], path: str, payload: Any) -> Any:
        if path not in table:
            raise AssertionError(f"unexpected request to {path!r} (payload={payload!r})")
        value = table[path]
        return value(payload) if callable(value) else value

    @property
    def posted_paths(self) -> list[str]:
        return [path for path, _ in self.post_calls]


def board(board_id: int, name: str, board_type: str = "kanban") -> dict:
    """Raw board payload as this server returns it -- note: no ``location``."""
    return {"id": board_id, "name": name, "type": board_type}


def _raise(error: Exception):
    """A FakeClient route that fails the request with ``error``."""

    def answer(_payload: Any) -> Any:
        raise error

    return answer


#: Caller-supplied identifiers that would rewrite a URL path if interpolated raw.
_TRAVERSAL_PROJECT_KEYS = ["PROJ/../../serverInfo", "../PROJ", "PR%2FOJ", "PROJ?x=1"]
_TRAVERSAL_IDS = ["12/../../serverInfo", "../12", "12?x=1", "12%2F"]
_TRAVERSAL_ISSUE_KEYS = ["PROJ-1/../../project/PROJ", "PROJ-1?x=1", "../PROJ-1"]


# --------------------------------------------------------------------------- #
# projects: reads
# --------------------------------------------------------------------------- #


def test_list_projects_returns_key_name_lead_type():
    client = FakeClient(
        gets={
            "/rest/api/2/project": [
                {
                    "key": "PROJ",
                    "name": "Project",
                    "projectTypeKey": "software",
                    "lead": {"name": "alice", "displayName": "Alice"},
                },
                {"key": "OPS", "name": "Ops", "projectTypeKey": "business"},
            ]
        }
    )

    assert projects.list_projects(client) == [
        {
            "key": "PROJ",
            "name": "Project",
            "lead": "alice",
            "lead_name": "Alice",
            "type": "software",
        },
        {"key": "OPS", "name": "Ops", "lead": None, "lead_name": None, "type": "business"},
    ]


def test_list_projects_asks_the_server_to_expand_the_lead():
    # Jira Server omits ``lead`` from the project list unless expand=lead is sent.
    client = FakeClient(gets={"/rest/api/2/project": []})

    projects.list_projects(client)

    assert client.get_calls == [("/rest/api/2/project", {"expand": "lead"})]


def test_list_projects_falls_back_to_the_lead_key_when_name_is_missing():
    client = FakeClient(
        gets={
            "/rest/api/2/project": [
                {"key": "PROJ", "name": "Project", "lead": {"key": "bob"}},
            ]
        }
    )

    [row] = projects.list_projects(client)

    assert (row["lead"], row["lead_name"]) == ("bob", None)


def test_get_project_includes_issue_types_components_and_versions():
    client = FakeClient(
        gets={
            "/rest/api/2/project/PROJ": {
                "id": "10000",
                "key": "PROJ",
                "name": "Project",
                "projectTypeKey": "software",
                "description": "a project",
                "lead": {"name": "alice", "displayName": "Alice"},
                "issueTypes": [{"name": "Task"}, {"name": "Story"}, {"id": "3"}],
                "components": [{"name": "api"}, {"name": "web"}],
                "versions": [{"name": "1.0", "released": True, "releaseDate": "2026-01-31"}],
            }
        }
    )

    result = projects.get_project(client, "proj")

    assert result["key"] == "PROJ"
    assert result["lead"] == "alice"
    assert result["lead_name"] == "Alice"
    assert result["issue_types"] == ["Task", "Story"]
    assert result["components"] == ["api", "web"]
    assert result["versions"] == [
        {"name": "1.0", "released": True, "release_date": "2026-01-31"}
    ]
    assert result["url"] == "https://jira.example.com/projects/PROJ"


def test_list_issue_types_prefers_the_createmeta_issuetypes_endpoint():
    client = FakeClient(
        gets={
            "/rest/api/2/issue/createmeta/PROJ/issuetypes": {
                "values": [{"id": "1", "name": "Bug"}, {"id": "3", "name": "Task"}]
            }
        }
    )

    assert projects.list_issue_types(client, "PROJ") == ["Bug", "Task"]
    assert [path for path, _ in client.get_calls] == [
        "/rest/api/2/issue/createmeta/PROJ/issuetypes"
    ]


def test_list_issue_types_reads_the_project_when_createmeta_is_unavailable():
    client = FakeClient(
        gets={
            "/rest/api/2/issue/createmeta/PROJ/issuetypes": _raise(
                NotFoundError("createmeta not found")
            ),
            "/rest/api/2/project/PROJ": {"key": "PROJ", "issueTypes": [{"name": "Bug"}]},
        }
    )

    assert projects.list_issue_types(client, "PROJ") == ["Bug"]


def test_list_priorities_keeps_server_order():
    client = FakeClient(
        gets={
            "/rest/api/2/priority": [
                {"name": "Highest"},
                {"name": "High"},
                {"name": "Medium"},
            ]
        }
    )

    assert projects.list_priorities(client) == ["Highest", "High", "Medium"]


def test_list_statuses_dedupes_across_issue_types_and_keeps_first_seen_order():
    client = FakeClient(
        gets={
            "/rest/api/2/project/PROJ/statuses": [
                {
                    "name": "Task",
                    "statuses": [
                        {"name": "To Do"},
                        {"name": "In Progress"},
                        {"name": "Done"},
                    ],
                },
                {
                    "name": "Bug",
                    "statuses": [
                        {"name": "To Do"},
                        {"name": "Blocked"},
                        {"name": "Done"},
                    ],
                },
            ]
        }
    )

    assert projects.list_statuses(client, "PROJ") == [
        "To Do",
        "In Progress",
        "Done",
        "Blocked",
    ]


# --------------------------------------------------------------------------- #
# projects: key validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["PROJ", "AB", "A1", "ABCDEFGHIJ", "proj"])
def test_validate_project_key_accepts_valid_keys(key):
    assert projects.validate_project_key(key) == key.upper()


@pytest.mark.parametrize("key", ["", "A", "1AB", "AB-CD", "ABCDEFGHIJK", "A B", None])
def test_validate_project_key_rejects_invalid_keys(key):
    with pytest.raises(InputError):
        projects.validate_project_key(key)


def test_create_project_rejects_an_invalid_key_before_any_request():
    client = FakeClient()

    with pytest.raises(InputError):
        projects.create_project(client, "1BAD", "Name", "alice")

    assert client.post_calls == []


def test_create_project_rejects_an_unknown_template():
    client = FakeClient()

    with pytest.raises(InputError) as excinfo:
        projects.create_project(client, "PROJ", "Project", "alice", template="waterfall")

    assert "template" in str(excinfo.value).lower()
    assert client.post_calls == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_create_project_rejects_a_blank_name_or_lead(blank):
    client = FakeClient()

    with pytest.raises(InputError):
        projects.create_project(client, "PROJ", blank, "alice")
    with pytest.raises(InputError):
        projects.create_project(client, "PROJ", "Project", blank)


# --------------------------------------------------------------------------- #
# projects: create_project
# --------------------------------------------------------------------------- #


def _create_project_client(existing_boards: list[dict]) -> FakeClient:
    return FakeClient(
        gets={"/rest/api/2/project/PROJ": {"key": "PROJ", "name": "Project"}},
        pages={"/rest/agile/1.0/board": existing_boards},
        posts={
            "/rest/api/2/project": {"id": "10010", "key": "PROJ"},
            "/rest/api/2/filter": {"id": 501},
            "/rest/agile/1.0/board": board(42, "PROJ board", "kanban"),
        },
    )


@pytest.mark.parametrize(
    ("template", "template_key"),
    [
        ("kanban", "com.pyxis.greenhopper.jira:gh-kanban-template"),
        ("scrum", "com.pyxis.greenhopper.jira:gh-scrum-template"),
        ("basic", "com.pyxis.greenhopper.jira:basic-software-development-template"),
    ],
)
def test_create_project_posts_the_server_payload_for_each_template(template, template_key):
    client = _create_project_client([board(7, "PROJ board")])

    projects.create_project(
        client, "proj", " Project ", " alice ", template=template, description="desc"
    )

    path, payload = client.post_calls[0]
    assert path == "/rest/api/2/project"
    assert payload == {
        "key": "PROJ",
        "name": "Project",
        "lead": "alice",
        "projectTypeKey": "software",
        "projectTemplateKey": template_key,
        "description": "desc",
    }


def test_create_project_omits_description_when_not_given():
    client = _create_project_client([board(7, "PROJ board")])

    projects.create_project(client, "PROJ", "Project", "alice")

    assert "description" not in client.post_calls[0][1]


def test_create_project_reuses_the_board_jira_already_created():
    client = _create_project_client([board(7, "PROJ board", "kanban")])

    result = projects.create_project(client, "PROJ", "Project", "alice")

    assert result["key"] == "PROJ"
    assert result["id"] == "10010"
    assert result["url"] == "https://jira.example.com/projects/PROJ"
    assert result["board"]["id"] == 7
    assert result["board_created"] is False
    assert result["warnings"] == []
    # Only the project itself was created -- no filter, no second board.
    assert client.posted_paths == ["/rest/api/2/project"]


def test_create_project_creates_a_board_only_when_the_project_has_none():
    client = _create_project_client([])

    result = projects.create_project(client, "PROJ", "Project", "alice", template="scrum")

    assert result["board"]["id"] == 42
    assert result["board_created"] is True
    assert client.posted_paths == [
        "/rest/api/2/project",
        "/rest/api/2/filter",
        "/rest/agile/1.0/board",
    ]
    assert client.post_calls[2][1]["type"] == "scrum"


def test_create_project_reports_a_board_failure_as_a_warning_not_an_error():
    client = _create_project_client([])
    client.posts["/rest/api/2/filter"] = lambda payload: (_ for _ in ()).throw(
        JiraError("filter creation denied")
    )

    result = projects.create_project(client, "PROJ", "Project", "alice")

    assert result["key"] == "PROJ"
    assert result["board"] is None
    assert result["board_created"] is False
    assert len(result["warnings"]) == 1
    assert "board setup failed" in result["warnings"][0]


# --------------------------------------------------------------------------- #
# projects: components and versions
# --------------------------------------------------------------------------- #


def test_create_component_sends_the_lead_as_a_server_username():
    client = FakeClient(
        posts={
            "/rest/api/2/component": {
                "id": "900",
                "name": "api",
                "project": "PROJ",
                "lead": {"name": "alice"},
            }
        }
    )

    result = projects.create_component(client, "proj", " api ", lead=" alice ")

    assert client.post_calls == [
        ("/rest/api/2/component", {"name": "api", "project": "PROJ", "leadUserName": "alice"})
    ]
    assert result == {"id": "900", "name": "api", "project": "PROJ", "lead": "alice"}


def test_create_component_omits_the_lead_when_none_is_given():
    client = FakeClient(posts={"/rest/api/2/component": {"id": "901", "name": "web"}})

    projects.create_component(client, "PROJ", "web")

    assert client.post_calls[0][1] == {"name": "web", "project": "PROJ"}


def test_create_component_rejects_a_blank_name():
    client = FakeClient()

    with pytest.raises(InputError):
        projects.create_component(client, "PROJ", "  ")

    assert client.post_calls == []


def test_create_version_sends_the_release_date():
    client = FakeClient(
        posts={
            "/rest/api/2/version": {
                "id": "77",
                "name": "1.0",
                "project": "PROJ",
                "releaseDate": "2026-03-01",
                "released": False,
            }
        }
    )

    result = projects.create_version(client, "PROJ", "1.0", release_date="2026-03-01")

    assert client.post_calls[0][1] == {
        "name": "1.0",
        "project": "PROJ",
        "releaseDate": "2026-03-01",
    }
    assert result == {
        "id": "77",
        "name": "1.0",
        "project": "PROJ",
        "release_date": "2026-03-01",
        "released": False,
    }


@pytest.mark.parametrize("bad_date", ["2026-3-1", "01-03-2026", "tomorrow"])
def test_create_version_rejects_a_malformed_release_date(bad_date):
    client = FakeClient()

    with pytest.raises(InputError):
        projects.create_version(client, "PROJ", "1.0", release_date=bad_date)

    assert client.post_calls == []


# --------------------------------------------------------------------------- #
# boards: listing and lookup
# --------------------------------------------------------------------------- #


def test_list_boards_filters_server_side_by_project_key():
    client = FakeClient(pages={"/rest/agile/1.0/board": [board(12, "PROJ board", "scrum")]})

    result = boards.list_boards(client, "proj")

    path, params, items_key, _ = client.paginate_calls[0]
    assert path == "/rest/agile/1.0/board"
    assert params == {"projectKeyOrId": "PROJ"}
    assert items_key == "values"
    # The list response has no location.projectKey, so the asked-for key is used.
    assert result == [{"id": 12, "name": "PROJ board", "type": "scrum", "project": "PROJ"}]


def test_list_boards_without_a_project_sends_no_filter_and_leaves_project_none():
    client = FakeClient(pages={"/rest/agile/1.0/board": [board(1, "Some board")]})

    assert boards.list_boards(client)[0]["project"] is None
    assert client.paginate_calls[0][1] is None


def test_list_boards_prefers_an_explicit_location_project_key_when_present():
    raw = board(5, "Mixed board")
    raw["location"] = {"projectKey": "OTHER"}
    client = FakeClient(pages={"/rest/agile/1.0/board": [raw]})

    assert boards.list_boards(client, "PROJ")[0]["project"] == "OTHER"


def test_list_boards_passes_the_board_type_filter():
    client = FakeClient(pages={"/rest/agile/1.0/board": []})

    boards.list_boards(client, "PROJ", board_type="Scrum")

    assert client.paginate_calls[0][1] == {"projectKeyOrId": "PROJ", "type": "scrum"}


def test_list_boards_rejects_an_unknown_board_type():
    client = FakeClient()

    with pytest.raises(InputError):
        boards.list_boards(client, "PROJ", board_type="sprintish")


def test_get_board_exposes_the_backing_filter_id():
    client = FakeClient(
        gets={
            "/rest/agile/1.0/board/12": {
                "id": 12,
                "name": "PROJ board",
                "type": "scrum",
                "filter": {"id": 501},
            }
        }
    )

    assert boards.get_board(client, "12") == {
        "id": 12,
        "name": "PROJ board",
        "type": "scrum",
        "project": None,
        "filter_id": 501,
    }


@pytest.mark.parametrize("bad_id", ["", "abc", 0, -3, None])
def test_get_board_rejects_an_invalid_board_id(bad_id):
    client = FakeClient()

    with pytest.raises(InputError):
        boards.get_board(client, bad_id)


def test_board_for_project_returns_none_when_the_project_has_no_board():
    client = FakeClient(pages={"/rest/agile/1.0/board": []})

    assert boards.board_for_project(client, "PROJ") is None


def test_board_for_project_returns_the_only_board_without_a_tie_break_lookup():
    client = FakeClient(pages={"/rest/agile/1.0/board": [board(12, "Delivery")]})

    assert boards.board_for_project(client, "PROJ")["id"] == 12
    # No project fetch was needed to break a tie.
    assert client.get_calls == []


def test_board_for_project_prefers_a_board_named_after_the_project_key():
    client = FakeClient(
        pages={
            "/rest/agile/1.0/board": [
                board(1, "Shared delivery"),
                board(2, "proj board", "scrum"),
            ]
        }
    )

    assert boards.board_for_project(client, "PROJ")["id"] == 2


def test_board_for_project_falls_back_to_the_project_display_name():
    client = FakeClient(
        gets={"/rest/api/2/project/PROJ": {"key": "PROJ", "name": "Payments"}},
        pages={
            "/rest/agile/1.0/board": [
                board(1, "Shared delivery"),
                board(2, "Payments team"),
            ]
        },
    )

    assert boards.board_for_project(client, "PROJ")["id"] == 2


def test_board_for_project_falls_back_to_the_first_board():
    client = FakeClient(
        gets={"/rest/api/2/project/PROJ": {"key": "PROJ", "name": "Payments"}},
        pages={
            "/rest/agile/1.0/board": [board(1, "Shared delivery"), board(2, "Other")]
        },
    )

    assert boards.board_for_project(client, "PROJ")["id"] == 1


def test_board_for_project_survives_an_unreadable_project_during_the_tie_break():
    client = FakeClient(
        gets={
            "/rest/api/2/project/PROJ": lambda params: (_ for _ in ()).throw(
                JiraError("no permission")
            )
        },
        pages={"/rest/agile/1.0/board": [board(1, "Alpha"), board(2, "Beta")]},
    )

    assert boards.board_for_project(client, "PROJ")["id"] == 1


def test_board_for_project_rejects_a_blank_project_key():
    client = FakeClient()

    with pytest.raises(InputError):
        boards.board_for_project(client, "  ")


# --------------------------------------------------------------------------- #
# boards: ensure_board
# --------------------------------------------------------------------------- #


def test_ensure_board_always_reuses_an_existing_board_even_of_another_type():
    client = FakeClient(pages={"/rest/agile/1.0/board": [board(12, "PROJ board", "kanban")]})

    result = boards.ensure_board(client, "PROJ", board_type="scrum")

    assert result == {
        "board": {"id": 12, "name": "PROJ board", "type": "kanban", "project": "PROJ"},
        "created": False,
    }
    assert client.post_calls == []


def test_ensure_board_creates_a_filter_then_a_board_when_none_exists():
    client = FakeClient(
        pages={"/rest/agile/1.0/board": []},
        posts={
            "/rest/api/2/filter": {"id": 501, "name": "PROJ board filter"},
            "/rest/agile/1.0/board": board(42, "PROJ board", "kanban"),
        },
    )

    result = boards.ensure_board(client, "proj")

    filter_path, filter_payload = client.post_calls[0]
    assert filter_path == "/rest/api/2/filter"
    assert filter_payload["name"] == "PROJ board filter"
    assert filter_payload["jql"] == 'project = "PROJ" ORDER BY Rank ASC'

    board_path, board_payload = client.post_calls[1]
    assert board_path == "/rest/agile/1.0/board"
    assert board_payload == {"name": "PROJ board", "type": "kanban", "filterId": 501}

    assert result["created"] is True
    assert result["board"]["id"] == 42


def test_ensure_board_uses_the_requested_name_and_type():
    client = FakeClient(
        pages={"/rest/agile/1.0/board": []},
        posts={
            "/rest/api/2/filter": {"id": 7},
            "/rest/agile/1.0/board": {"id": 43},
        },
    )

    result = boards.ensure_board(client, "PROJ", board_type="scrum", name=" Delivery ")

    assert client.post_calls[0][1]["name"] == "Delivery filter"
    assert client.post_calls[1][1] == {"name": "Delivery", "type": "scrum", "filterId": 7}
    # The server echoed neither name nor type; the requested values fill the gap.
    assert result["board"] == {
        "id": 43,
        "name": "Delivery",
        "type": "scrum",
        "project": "PROJ",
    }


def test_ensure_board_fails_loudly_when_jira_returns_no_filter_id():
    client = FakeClient(
        pages={"/rest/agile/1.0/board": []},
        posts={"/rest/api/2/filter": {"name": "PROJ board filter"}},
    )

    with pytest.raises(JiraError):
        boards.ensure_board(client, "PROJ")

    assert client.posted_paths == ["/rest/api/2/filter"]


def test_ensure_board_rejects_an_unknown_board_type_before_touching_the_server():
    client = FakeClient()

    with pytest.raises(InputError):
        boards.ensure_board(client, "PROJ", board_type="waterfall")

    assert client.paginate_calls == []


# --------------------------------------------------------------------------- #
# boards: sprints
# --------------------------------------------------------------------------- #

_SPRINTS = [
    {
        "id": 5,
        "name": "Sprint 5",
        "state": "active",
        "startDate": "2026-10-01T09:00:00.000Z",
        "endDate": "2026-10-15T09:00:00.000Z",
        "goal": "ship it",
    },
    {"id": 6, "name": "Sprint 6", "state": "future"},
]


def test_list_sprints_normalises_dates_and_passes_the_state_filter():
    client = FakeClient(pages={"/rest/agile/1.0/board/12/sprint": _SPRINTS})

    result = boards.list_sprints(client, 12)

    assert client.paginate_calls[0][1] == {"state": "active,future"}
    assert result[0] == {
        "id": 5,
        "name": "Sprint 5",
        "state": "active",
        "start": "2026-10-01T09:00:00.000Z",
        "end": "2026-10-15T09:00:00.000Z",
        "goal": "ship it",
    }
    assert result[1] == {
        "id": 6,
        "name": "Sprint 6",
        "state": "future",
        "start": None,
        "end": None,
        "goal": None,
    }


@pytest.mark.parametrize("state", ["", "all"])
def test_list_sprints_asks_for_every_state_when_told_to(state):
    client = FakeClient(pages={"/rest/agile/1.0/board/12/sprint": _SPRINTS})

    boards.list_sprints(client, 12, state=state)

    assert client.paginate_calls[0][1] is None


def test_list_sprints_rejects_an_unknown_state():
    client = FakeClient()

    with pytest.raises(InputError) as excinfo:
        boards.list_sprints(client, 12, state="active,soon")

    assert "soon" in str(excinfo.value)


def test_active_sprint_returns_the_active_one():
    client = FakeClient(pages={"/rest/agile/1.0/board/12/sprint": _SPRINTS})

    assert boards.active_sprint(client, 12)["id"] == 5
    assert client.paginate_calls[0][1] == {"state": "active"}


def test_active_sprint_returns_none_when_the_board_has_no_active_sprint():
    # Agile answers empty, so both GreenHopper fallbacks are consulted; neither
    # knows an active sprint for this board.
    client = FakeClient(
        gets={
            "/rest/greenhopper/1.0/sprintquery/12": {"rapidViewId": 12, "sprints": []},
            "/rest/greenhopper/1.0/sprint/picker": {
                "suggestions": [],
                "allMatches": [
                    {"id": 8, "name": "Sprint 8", "stateKey": "FUTURE", "boardName": "PROJ board"}
                ],
            },
            "/rest/agile/1.0/board/12": board(12, "PROJ board", "scrum"),
        },
        pages={"/rest/agile/1.0/board/12/sprint": []},
    )

    assert boards.active_sprint(client, 12) is None
    asked = [path for path, _ in client.get_calls]
    assert "/rest/greenhopper/1.0/sprintquery/12" in asked
    assert "/rest/greenhopper/1.0/sprint/picker" in asked


def test_create_sprint_posts_the_payload_for_a_scrum_board():
    client = FakeClient(
        gets={"/rest/agile/1.0/board/12": board(12, "PROJ board", "scrum")},
        posts={
            "/rest/agile/1.0/sprint": {
                "id": 9,
                "name": "Sprint 7",
                "state": "future",
                "startDate": "2026-11-01",
                "endDate": "2026-11-15",
                "goal": "stabilise",
            }
        },
    )

    result = boards.create_sprint(
        client, 12, " Sprint 7 ", start="2026-11-01", end="2026-11-15", goal="stabilise"
    )

    assert client.post_calls == [
        (
            "/rest/agile/1.0/sprint",
            {
                "name": "Sprint 7",
                "originBoardId": 12,
                "startDate": "2026-11-01",
                "endDate": "2026-11-15",
                "goal": "stabilise",
            },
        )
    ]
    assert result["id"] == 9
    assert result["board_id"] == 12
    assert result["start"] == "2026-11-01"


def test_create_sprint_omits_optional_fields():
    client = FakeClient(
        gets={"/rest/agile/1.0/board/12": board(12, "PROJ board", "scrum")},
        posts={"/rest/agile/1.0/sprint": {"id": 9}},
    )

    result = boards.create_sprint(client, 12, "Sprint 7")

    assert client.post_calls[0][1] == {"name": "Sprint 7", "originBoardId": 12}
    assert result["name"] == "Sprint 7"


def test_create_sprint_refuses_a_kanban_board_and_says_why():
    client = FakeClient(gets={"/rest/agile/1.0/board/12": board(12, "PROJ board", "kanban")})

    with pytest.raises(InputError) as excinfo:
        boards.create_sprint(client, 12, "Sprint 7")

    message = str(excinfo.value).lower()
    assert "kanban" in message
    assert "sprint" in message
    assert client.post_calls == []


def test_create_sprint_rejects_a_blank_name_before_reading_the_board():
    client = FakeClient()

    with pytest.raises(InputError):
        boards.create_sprint(client, 12, "   ")

    assert client.get_calls == []


# --------------------------------------------------------------------------- #
# boards: sprint membership and backlog
# --------------------------------------------------------------------------- #


def test_add_to_sprint_posts_normalised_deduped_keys():
    client = FakeClient(posts={"/rest/agile/1.0/sprint/5/issue": None})

    result = boards.add_to_sprint(client, "5", ["proj-1", "PROJ-2", "PROJ-1"])

    assert client.post_calls == [
        ("/rest/agile/1.0/sprint/5/issue", {"issues": ["PROJ-1", "PROJ-2"]})
    ]
    assert result == {"sprint_id": 5, "issues": ["PROJ-1", "PROJ-2"], "moved": 2}


@pytest.mark.parametrize("keys", [[], ["", "  "], [None], "", 5])
def test_add_to_sprint_rejects_an_empty_or_invalid_key_list(keys):
    client = FakeClient()

    with pytest.raises(InputError):
        boards.add_to_sprint(client, 5, keys)

    assert client.post_calls == []


def test_add_to_sprint_rejects_an_invalid_sprint_id():
    client = FakeClient()

    with pytest.raises(InputError):
        boards.add_to_sprint(client, "not-a-sprint", ["PROJ-1"])


def test_add_to_sprint_accepts_a_single_key_as_a_string():
    client = FakeClient(posts={"/rest/agile/1.0/sprint/5/issue": None})

    assert boards.add_to_sprint(client, 5, "PROJ-9")["issues"] == ["PROJ-9"]


def test_move_to_backlog_posts_to_the_agile_backlog_endpoint():
    client = FakeClient(posts={"/rest/agile/1.0/backlog/issue": None})

    result = boards.move_to_backlog(client, ["PROJ-1", "PROJ-2"])

    assert client.post_calls == [
        ("/rest/agile/1.0/backlog/issue", {"issues": ["PROJ-1", "PROJ-2"]})
    ]
    assert result == {"issues": ["PROJ-1", "PROJ-2"], "moved": 2}


def test_move_to_backlog_rejects_an_empty_key_list():
    client = FakeClient()

    with pytest.raises(InputError):
        boards.move_to_backlog(client, [])

    assert client.post_calls == []


# --------------------------------------------------------------------------- #
# boards: board_issues
# --------------------------------------------------------------------------- #


#: The ``/rest/api/2/field`` payload the real FieldMap resolves custom fields from.
_FIELD_DEFINITIONS = [
    {"id": "summary", "name": "Summary"},
    {"id": "customfield_10109", "name": "Sprint"},
    {"id": "customfield_10108", "name": "Epic Link"},
    {"id": "customfield_10104", "name": "Epic Name"},
    {"id": "customfield_10110", "name": "Story Points"},
]


def _raw_issue(key: str, status: str = "To Do", category: str = "new") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"summary of {key}",
            "issuetype": {"name": "Task"},
            "status": {"name": status, "statusCategory": {"key": category}},
            "project": {"key": "PROJ"},
            "labels": ["backend"],
        },
    }


def test_board_issues_pages_on_the_issues_key_and_normalises_every_issue():
    client = FakeClient(
        gets={"/rest/api/2/field": _FIELD_DEFINITIONS},
        pages={"/rest/agile/1.0/board/12/issue": [_raw_issue("PROJ-1"), _raw_issue("PROJ-2")]},
    )

    result = boards.board_issues(client, 12, limit=50)

    path, params, items_key, limit = client.paginate_calls[0]
    assert path == "/rest/agile/1.0/board/12/issue"
    assert params is None
    assert items_key == "issues"
    assert limit == 50

    assert [i["key"] for i in result] == ["PROJ-1", "PROJ-2"]
    first = result[0]
    assert first["summary"] == "summary of PROJ-1"
    assert first["status"] == "To Do"
    assert first["status_category"] == "To Do"
    assert first["project"] == "PROJ"
    assert first["labels"] == ["backend"]
    assert first["url"] == "https://jira.example.com/browse/PROJ-1"


def test_board_issues_passes_extra_jql_to_the_server():
    client = FakeClient(pages={"/rest/agile/1.0/board/12/issue": []})

    assert boards.board_issues(client, 12, jql=" status = Done ") == []
    assert client.paginate_calls[0][1] == {"jql": "status = Done"}


@pytest.mark.parametrize("bad_limit", [0, -1, "many"])
def test_board_issues_rejects_a_non_positive_limit(bad_limit):
    client = FakeClient()

    with pytest.raises(InputError):
        boards.board_issues(client, 12, limit=bad_limit)

    assert client.paginate_calls == []


# --------------------------------------------------------------------------- #
# path traversal: caller-supplied identifiers never reach a URL path unchecked
# --------------------------------------------------------------------------- #


def _assert_no_request(client: FakeClient) -> None:
    assert client.get_calls == []
    assert client.paginate_calls == []
    assert client.post_calls == []


@pytest.mark.parametrize("key", _TRAVERSAL_PROJECT_KEYS)
@pytest.mark.parametrize(
    "call",
    [
        lambda c, k: projects.get_project(c, k),
        lambda c, k: projects.list_issue_types(c, k),
        lambda c, k: projects.list_statuses(c, k),
        lambda c, k: projects.create_project(c, k, "Project", "alice"),
        lambda c, k: projects.create_component(c, k, "api"),
        lambda c, k: projects.create_version(c, k, "1.0"),
        lambda c, k: boards.list_boards(c, k),
        lambda c, k: boards.board_for_project(c, k),
        lambda c, k: boards.ensure_board(c, k),
    ],
    ids=[
        "get_project",
        "list_issue_types",
        "list_statuses",
        "create_project",
        "create_component",
        "create_version",
        "list_boards",
        "board_for_project",
        "ensure_board",
    ],
)
def test_traversal_project_key_is_rejected_before_any_request(call, key):
    client = FakeClient()

    with pytest.raises(InputError):
        call(client, key)

    _assert_no_request(client)


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
@pytest.mark.parametrize(
    "call",
    [
        lambda c, i: boards.get_board(c, i),
        lambda c, i: boards.list_sprints(c, i),
        lambda c, i: boards.find_sprints(c, i),
        lambda c, i: boards.greenhopper_sprints(c, i),
        lambda c, i: boards.active_sprint(c, i),
        lambda c, i: boards.create_sprint(c, i, "Sprint 1"),
        lambda c, i: boards.board_issues(c, i),
        lambda c, i: boards.add_to_sprint(c, i, ["PROJ-1"]),
    ],
    ids=[
        "get_board",
        "list_sprints",
        "find_sprints",
        "greenhopper_sprints",
        "active_sprint",
        "create_sprint",
        "board_issues",
        "add_to_sprint",
    ],
)
def test_traversal_board_or_sprint_id_is_rejected_before_any_request(call, bad_id):
    client = FakeClient()

    with pytest.raises(InputError):
        call(client, bad_id)

    _assert_no_request(client)


@pytest.mark.parametrize("bad_id", [True, 1.5, "1e3", " 0 "])
def test_board_id_must_be_a_plain_positive_integer(bad_id):
    client = FakeClient()

    with pytest.raises(InputError):
        boards.get_board(client, bad_id)

    _assert_no_request(client)


@pytest.mark.parametrize("key", _TRAVERSAL_ISSUE_KEYS)
@pytest.mark.parametrize(
    "call",
    [
        lambda c, k: boards.add_to_sprint(c, 5, ["PROJ-2", k]),
        lambda c, k: boards.move_to_backlog(c, [k]),
    ],
    ids=["add_to_sprint", "move_to_backlog"],
)
def test_malformed_issue_key_in_a_sprint_move_is_rejected_before_any_request(call, key):
    client = FakeClient()

    with pytest.raises(InputError):
        call(client, key)

    _assert_no_request(client)


def test_sprint_enrichment_skips_a_server_row_with_a_malformed_id():
    # GreenHopper rows are server data, but their id still lands in a path.
    client = FakeClient(
        gets={
            "/rest/greenhopper/1.0/sprintquery/12": {
                "sprints": [{"id": "5/../../serverInfo", "name": "odd", "state": "ACTIVE"}]
            }
        },
        pages={"/rest/agile/1.0/board/12/sprint": []},
    )

    result = boards.find_sprints(client, 12, state="active")

    assert result["method"] == "greenhopper-sprintquery"
    assert [s["name"] for s in result["sprints"]] == ["odd"]
    assert [path for path, _ in client.get_calls] == ["/rest/greenhopper/1.0/sprintquery/12"]


# --------------------------------------------------------------------------- #
# consumer contract: names plan.py and cli/menu.py import from boards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        # CONTRACT section 3
        "list_boards",
        "get_board",
        "board_for_project",
        "ensure_board",
        "list_sprints",
        "active_sprint",
        "create_sprint",
        "add_to_sprint",
        "move_to_backlog",
        "board_issues",
    ],
)
def test_boards_exports_every_name_its_consumers_use(name):
    # services/plan.py uses active_sprint; cli/menu.py uses board_for_project,
    # ensure_board, list_sprints and create_sprint.
    assert callable(getattr(boards, name))
    assert name in boards.__all__


@pytest.mark.parametrize(
    "name",
    [
        "list_projects",
        "get_project",
        "list_issue_types",
        "list_priorities",
        "list_statuses",
        "create_project",
        "create_component",
        "create_version",
    ],
)
def test_projects_exports_every_contract_name(name):
    assert callable(getattr(projects, name))
    assert name in projects.__all__


def test_template_keys_match_this_server():
    assert projects.TEMPLATE_KEYS == {
        "kanban": "com.pyxis.greenhopper.jira:gh-kanban-template",
        "scrum": "com.pyxis.greenhopper.jira:gh-scrum-template",
        "basic": "com.pyxis.greenhopper.jira:basic-software-development-template",
    }
