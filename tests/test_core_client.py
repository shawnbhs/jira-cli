"""Tests for :mod:`jira_cli.client` against a fake ``requests.Session``.

No network: a :class:`FakeSession` records every call and replays queued
responses. ``time.sleep`` is patched in the retry tests so the suite stays
fast and deterministic. ``S105``/``S106`` are silenced for the whole file: the
credentials below are offline fixture strings.
"""

# ruff: noqa: S105, S106

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jira_cli.client import API, SEARCH_PATH, JiraClient  # noqa: E402
from jira_cli.config import AUTH_BASIC, AUTH_PAT, Settings  # noqa: E402
from jira_cli.errors import (  # noqa: E402
    AuthError,
    InputError,
    JiraError,
    JiraPermissionError,
    NotFoundError,
    PartialResultError,
    ReadOnlyError,
)

BASE = "https://jira.example.com"
SECRET = "pat-secret-value-123456"


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code=200, json_data=None, text=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        if text is not None:
            self.text = text
        elif json_data is None:
            self.text = ""
        else:
            self.text = "{...}"

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeSession:
    """Records requests and replays a queue of responses or exceptions."""

    def __init__(self, *responses):
        self.headers: dict[str, str] = {}
        self.auth = None
        self.queue = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, params=None, json=None, timeout=None, verify=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "timeout": timeout,
                "verify": verify,
            }
        )
        item = self.queue.pop(0) if self.queue else FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item


def make_settings(**overrides) -> Settings:
    values: dict = {
        "base_url": BASE,
        "auth_mode": AUTH_PAT,
        "username": "alice",
        "secret": SECRET,
        "default_project": "PROJ",
        "verify_tls": True,
        "timeout": 20.0,
        "read_only": False,
    }
    values.update(overrides)
    return Settings(**values)


def make_client(*responses, **setting_overrides) -> tuple[JiraClient, FakeSession]:
    session = FakeSession(*responses)
    return JiraClient(make_settings(**setting_overrides), session=session), session


def page(values, *, total=None, is_last=None, key="values"):
    body: dict = {key: values, "startAt": 0, "maxResults": len(values)}
    if total is not None:
        body["total"] = total
    if is_last is not None:
        body["isLast"] = is_last
    return FakeResponse(200, body)


# --------------------------------------------------------------------------- #
# auth wiring
# --------------------------------------------------------------------------- #


def test_pat_mode_signs_requests_with_a_bearer_header():
    client, session = make_client()
    prepared = requests.Request("GET", f"{BASE}/rest/api/2/myself").prepare()
    session.auth(prepared)
    assert prepared.headers["Authorization"] == f"Bearer {SECRET}"
    assert "Authorization" not in session.headers
    assert client.settings.auth_mode == AUTH_PAT


def test_basic_mode_uses_session_auth_and_no_header():
    _, session = make_client(auth_mode=AUTH_BASIC, secret="password-value-1")
    assert "Authorization" not in session.headers
    assert session.auth == ("alice", "password-value-1")


def test_every_request_carries_timeout_and_verify():
    client, session = make_client(FakeResponse(200, {"ok": True}))
    client.get("/rest/api/2/myself")
    assert session.calls[0]["timeout"] == 20.0
    assert session.calls[0]["verify"] is True


def test_verify_tls_path_is_passed_through():
    client, session = make_client(FakeResponse(200, {}), verify_tls="/etc/ca.pem")
    client.get("/rest/api/2/myself")
    assert session.calls[0]["verify"] == "/etc/ca.pem"


# --------------------------------------------------------------------------- #
# url handling / origin check
# --------------------------------------------------------------------------- #


def test_relative_path_is_prefixed_with_base_url():
    client, _ = make_client()
    assert client.build_url("/rest/api/2/myself") == f"{BASE}/rest/api/2/myself"


def test_absolute_url_on_the_same_origin_is_accepted():
    client, _ = make_client()
    same = f"{BASE}/rest/api/2/search"
    assert client.build_url(same) == same


def test_absolute_url_on_another_host_is_refused():
    client, _ = make_client()
    with pytest.raises(InputError, match="not on"):
        client.build_url("https://evil.example.com/rest/api/2/myself")


def test_absolute_url_with_another_scheme_is_refused():
    client, _ = make_client()
    with pytest.raises(InputError, match="not on"):
        client.build_url("http://jira.example.com/rest/api/2/myself")


def test_absolute_url_with_another_port_is_refused():
    client, _ = make_client()
    with pytest.raises(InputError, match="not on"):
        client.build_url("https://jira.example.com:8443/rest/api/2/myself")


def test_a_foreign_url_never_reaches_the_session():
    client, session = make_client()
    with pytest.raises(InputError):
        client.get("https://evil.example.com/steal")
    assert session.calls == []


def test_path_must_be_rooted():
    client, _ = make_client()
    with pytest.raises(InputError, match="must start with"):
        client.get("rest/api/2/myself")


def test_empty_path_is_an_input_error():
    client, _ = make_client()
    with pytest.raises(InputError, match="required"):
        client.get("")


# --------------------------------------------------------------------------- #
# status mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthError),
        (403, JiraPermissionError),
        (404, NotFoundError),
        (400, JiraError),
        (409, JiraError),
        (500, JiraError),
    ],
)
def test_status_codes_map_to_their_exception(status, expected):
    client, _ = make_client(FakeResponse(status, {"errorMessages": ["nope"]}))
    with pytest.raises(expected) as caught:
        client.get("/rest/api/2/myself")
    assert caught.value.status == status


def test_error_messages_from_jira_are_joined_into_the_message():
    body = {"errorMessages": ["first problem"], "errors": {"summary": "is required"}}
    client, _ = make_client(FakeResponse(400, body))
    with pytest.raises(JiraError) as caught:
        client.get("/rest/api/2/issue/PROJ-1")
    assert "first problem" in caught.value.message
    assert "summary: is required" in caught.value.message


def test_non_json_error_body_still_produces_a_message():
    client, _ = make_client(FakeResponse(500, None, text="<html>boom</html>"))
    with pytest.raises(JiraError) as caught:
        client.get("/rest/api/2/myself")
    assert "boom" in caught.value.message


def test_204_returns_none():
    client, _ = make_client(FakeResponse(204, None))
    assert client.get("/rest/api/2/myself") is None


def test_empty_body_returns_none():
    client, _ = make_client(FakeResponse(200, None, text="   "))
    assert client.get("/rest/api/2/myself") is None


# --------------------------------------------------------------------------- #
# retries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_get_retries_transient_statuses_then_succeeds(status):
    client, session = make_client(
        FakeResponse(status, {"errorMessages": ["busy"]}), FakeResponse(200, {"ok": 1})
    )
    with mock.patch("jira_cli.client.time.sleep") as sleeper:
        assert client.get("/rest/api/2/myself") == {"ok": 1}
    assert len(session.calls) == 2
    assert sleeper.call_count == 1


def test_get_gives_up_after_three_attempts():
    client, session = make_client(*[FakeResponse(503, {"errorMessages": ["busy"]})] * 3)
    with mock.patch("jira_cli.client.time.sleep"), pytest.raises(JiraError) as caught:
        client.get("/rest/api/2/myself")
    assert len(session.calls) == 3
    assert caught.value.status == 503


def test_retry_after_header_is_honoured():
    client, _ = make_client(
        FakeResponse(429, {}, headers={"Retry-After": "7"}), FakeResponse(200, {"ok": 1})
    )
    with mock.patch("jira_cli.client.time.sleep") as sleeper:
        client.get("/rest/api/2/myself")
    sleeper.assert_called_once_with(7.0)


def test_retry_after_is_capped_so_a_hostile_value_cannot_hang_the_cli():
    client, _ = make_client(
        FakeResponse(503, {}, headers={"Retry-After": "99999"}), FakeResponse(200, {})
    )
    with mock.patch("jira_cli.client.time.sleep") as sleeper:
        client.get("/rest/api/2/myself")
    sleeper.assert_called_once_with(60.0)


def test_unparsable_retry_after_falls_back_to_the_backoff_schedule():
    client, _ = make_client(
        FakeResponse(503, {}, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        FakeResponse(200, {}),
    )
    with mock.patch("jira_cli.client.time.sleep") as sleeper:
        client.get("/rest/api/2/myself")
    sleeper.assert_called_once_with(0.5)


def test_backoff_schedule_is_half_then_one_second():
    client, _ = make_client(
        FakeResponse(503, {}), FakeResponse(503, {}), FakeResponse(200, {"ok": 1})
    )
    with mock.patch("jira_cli.client.time.sleep") as sleeper:
        client.get("/rest/api/2/myself")
    assert [call.args[0] for call in sleeper.call_args_list] == [0.5, 1.0]


def test_connection_errors_are_retried_then_reported():
    client, session = make_client(
        requests.ConnectionError("dns"),
        requests.ConnectionError("dns"),
        requests.ConnectionError("dns"),
    )
    with mock.patch("jira_cli.client.time.sleep"), pytest.raises(JiraError, match="attempt"):
        client.get("/rest/api/2/myself")
    assert len(session.calls) == 3


def test_a_connection_error_that_recovers_returns_the_payload():
    client, _ = make_client(requests.ConnectionError("blip"), FakeResponse(200, {"ok": 1}))
    with mock.patch("jira_cli.client.time.sleep"):
        assert client.get("/rest/api/2/myself") == {"ok": 1}


def test_writes_are_never_retried():
    client, session = make_client(FakeResponse(503, {"errorMessages": ["busy"]}))
    with mock.patch("jira_cli.client.time.sleep") as sleeper, pytest.raises(JiraError):
        client.post("/rest/api/2/issue", {"fields": {}})
    assert len(session.calls) == 1
    assert sleeper.call_count == 0


def test_a_write_connection_error_is_not_retried():
    client, session = make_client(requests.ConnectionError("down"))
    with mock.patch("jira_cli.client.time.sleep"), pytest.raises(JiraError):
        client.put("/rest/api/2/issue/PROJ-1", {"fields": {}})
    assert len(session.calls) == 1


# --------------------------------------------------------------------------- #
# read-only mode
# --------------------------------------------------------------------------- #


def test_ensure_writable_raises_in_read_only_mode():
    client, _ = make_client(read_only=True)
    with pytest.raises(ReadOnlyError, match="read-only"):
        client.ensure_writable()


def test_ensure_writable_is_silent_when_writable():
    client, _ = make_client()
    assert client.ensure_writable() is None


@pytest.mark.parametrize("verb", ["post", "put", "delete"])
def test_writes_are_refused_in_read_only_mode(verb):
    client, session = make_client(read_only=True)
    with pytest.raises(ReadOnlyError):
        getattr(client, verb)("/rest/api/2/issue/PROJ-1")
    assert session.calls == []


def test_get_still_works_in_read_only_mode():
    client, _ = make_client(FakeResponse(200, {"ok": 1}), read_only=True)
    assert client.get("/rest/api/2/myself") == {"ok": 1}


def test_post_to_search_is_treated_as_a_read():
    client, session = make_client(
        FakeResponse(200, {"issues": [], "total": 0}), read_only=True
    )
    client.post(SEARCH_PATH, {"jql": "project = PROJ"})
    assert len(session.calls) == 1


# --------------------------------------------------------------------------- #
# pagination
# --------------------------------------------------------------------------- #


def test_paginate_walks_until_is_last():
    client, session = make_client(
        page([{"id": 1}, {"id": 2}], total=3, is_last=False),
        page([{"id": 3}], total=3, is_last=True),
    )
    assert client.paginate("/rest/agile/1.0/board", page_size=2) == [
        {"id": 1},
        {"id": 2},
        {"id": 3},
    ]
    assert session.calls[0]["params"]["startAt"] == 0
    assert session.calls[1]["params"]["startAt"] == 2


def test_paginate_stops_on_a_short_page():
    client, session = make_client(page([{"id": 1}], total=1))
    assert client.paginate("/rest/agile/1.0/board", page_size=50) == [{"id": 1}]
    assert len(session.calls) == 1


def test_paginate_stops_on_an_empty_page():
    client, _ = make_client(page([]))
    assert client.paginate("/rest/agile/1.0/board") == []


def test_paginate_uses_a_custom_items_key():
    client, _ = make_client(page([{"id": 1}], total=1, key="issues"))
    assert client.paginate("/x/y", items_key="issues") == [{"id": 1}]


def test_paginate_respects_limit_and_does_not_over_fetch():
    client, session = make_client(page([{"id": 1}, {"id": 2}], total=99, is_last=False))
    assert client.paginate("/x/y", page_size=10, limit=2) == [{"id": 1}, {"id": 2}]
    assert session.calls[0]["params"]["maxResults"] == 2


def test_limit_suppresses_the_partial_result_error():
    client, _ = make_client(page([{"id": 1}], total=500, is_last=False))
    assert client.paginate("/x/y", page_size=1, limit=1) == [{"id": 1}]


def test_paginate_raises_partial_result_when_total_is_not_reached():
    client, _ = make_client(page([{"id": 1}], total=9, is_last=True))
    with pytest.raises(PartialResultError, match="9 items"):
        client.paginate("/rest/agile/1.0/board")


def test_partial_result_error_has_no_status():
    client, _ = make_client(page([{"id": 1}], total=9, is_last=True))
    with pytest.raises(PartialResultError) as caught:
        client.paginate("/rest/agile/1.0/board")
    assert caught.value.status is None


def test_paginate_accepts_a_bare_list_response():
    client, _ = make_client(FakeResponse(200, [{"id": 1}]))
    assert client.paginate("/rest/api/2/project", page_size=10) == [{"id": 1}]


def test_paginate_rejects_a_non_positive_page_size():
    client, _ = make_client()
    with pytest.raises(InputError, match="page_size"):
        client.paginate("/x/y", page_size=0)


# --------------------------------------------------------------------------- #
# search / count / myself / browse_url
# --------------------------------------------------------------------------- #


def test_search_sends_jql_fields_and_expand():
    client, session = make_client(page([{"key": "PROJ-1"}], total=1, key="issues"))
    issues = client.search("project = PROJ", fields=["summary", "status"], expand=["changelog"])
    params = session.calls[0]["params"]
    assert issues == [{"key": "PROJ-1"}]
    assert params["jql"] == "project = PROJ"
    assert params["fields"] == "summary,status"
    assert params["expand"] == "changelog"


def test_search_hits_the_rest_v2_search_endpoint():
    client, session = make_client(page([], total=0, key="issues"))
    client.search("project = PROJ")
    assert session.calls[0]["url"] == f"{BASE}{SEARCH_PATH}"


def test_search_requires_a_jql_string():
    client, _ = make_client()
    with pytest.raises(InputError, match="JQL"):
        client.search("   ")


def test_count_asks_for_zero_results_and_returns_total():
    client, session = make_client(FakeResponse(200, {"total": 42, "issues": []}))
    assert client.count("project = PROJ") == 42
    assert session.calls[0]["params"]["maxResults"] == 0


def test_count_of_a_missing_total_is_zero():
    client, _ = make_client(FakeResponse(200, {}))
    assert client.count("project = PROJ") == 0


def test_count_requires_a_jql_string():
    client, _ = make_client()
    with pytest.raises(InputError, match="JQL"):
        client.count("")


def test_myself_is_fetched_once_and_cached():
    client, session = make_client(FakeResponse(200, {"name": "alice"}))
    assert client.myself() == {"name": "alice"}
    assert client.myself() == {"name": "alice"}
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == f"{BASE}{API}/myself"


def test_myself_fills_an_empty_username():
    client, _ = make_client(FakeResponse(200, {"name": "bob"}), username="")
    client.myself()
    assert client.settings.username == "bob"


def test_myself_does_not_overwrite_a_configured_username():
    client, _ = make_client(FakeResponse(200, {"name": "bob"}))
    client.myself()
    assert client.settings.username == "alice"


def test_browse_url_is_built_from_base_url():
    client, _ = make_client()
    assert client.browse_url("PROJ-7") == f"{BASE}/browse/PROJ-7"


# --------------------------------------------------------------------------- #
# the secret never escapes
# --------------------------------------------------------------------------- #


def test_secret_is_absent_from_repr_and_str():
    client, _ = make_client()
    assert SECRET not in repr(client)
    assert SECRET not in str(client)


def test_secret_is_absent_from_an_error_message_that_quotes_it_back():
    client, _ = make_client(FakeResponse(400, {"errorMessages": [f"bad token {SECRET}"]}))
    with pytest.raises(JiraError) as caught:
        client.get("/rest/api/2/myself")
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert "***" in caught.value.message


def test_secret_is_absent_from_a_refused_foreign_url_error():
    client, _ = make_client()
    with pytest.raises(InputError) as caught:
        client.get(f"https://evil.example.com/x?token={SECRET}")
    assert SECRET not in str(caught.value)


def test_an_authorization_header_echoed_by_jira_is_redacted():
    client, _ = make_client(
        FakeResponse(400, {"errorMessages": ["sent Authorization: Bearer abcdef123456"]})
    )
    with pytest.raises(JiraError) as caught:
        client.get("/rest/api/2/myself")
    assert "abcdef123456" not in str(caught.value)
