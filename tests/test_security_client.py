"""Security regression tests for :mod:`jira_cli.client`.

Covers the W3-12 audit findings owned by the client:

* F2 — the read-only exemption for ``POST /search`` must match the parsed
  path exactly, not a string suffix.
* F1 (defence in depth) — request paths with dot segments, encoded
  separators or fragments are refused before anything is sent.
* F8 — redirects are capped and ``~/.netrc`` never supplies credentials.
* F9 — default ports are normalised in the origin check.

Offline only: a recording fake session, or a real ``requests.Session`` with a
fake transport adapter mounted. ``S105``/``S106`` are silenced: the
credentials below are fixture strings.
"""

# ruff: noqa: S105, S106

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jira_cli.client import MAX_REDIRECTS, SEARCH_PATH, JiraClient  # noqa: E402
from jira_cli.config import AUTH_BASIC, AUTH_PAT, Settings  # noqa: E402
from jira_cli.errors import InputError, JiraError, ReadOnlyError  # noqa: E402

BASE = "https://jira.example.com"
SECRET = "pat-secret-value-123456"
NETRC_CREDENTIALS = ("mallory", "netrc-password-value")


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


class RecordingSession:
    """Records ``request`` calls and answers ``200 {}``."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.auth = None
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **_kwargs):
        self.calls.append((method, url))
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response


def make_client(**overrides) -> tuple[JiraClient, RecordingSession]:
    session = RecordingSession()
    return JiraClient(make_settings(**overrides), session=session), session


class FakeAdapter(BaseAdapter):
    """Transport stand-in: records prepared requests, replays a script.

    ``script`` maps a URL to ``(status, headers)``; unknown URLs get 200.
    """

    def __init__(self, script: dict[str, tuple[int, dict]] | None = None) -> None:
        super().__init__()
        self.script = script or {}
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **_kwargs):
        self.sent.append(request)
        status, headers = self.script.get(request.url, (200, {}))
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = b"{}"
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        """Nothing to release."""


def make_transport_client(
    adapter: FakeAdapter, **overrides
) -> JiraClient:
    """A client on its default (real) session with ``adapter`` mounted."""
    client = JiraClient(make_settings(**overrides))
    client._session.mount("https://", adapter)
    client._session.mount("http://", adapter)
    return client


@pytest.fixture
def netrc_has_credentials(monkeypatch):
    """Make every netrc lookup answer with a credential, as a real file would."""
    monkeypatch.setattr(
        requests.sessions, "get_netrc_auth", lambda *_a, **_k: NETRC_CREDENTIALS
    )


# --------------------------------------------------------------------------- #
# F2: read-only exemption for search
# --------------------------------------------------------------------------- #


def test_post_with_search_path_hidden_in_query_is_refused_in_read_only_mode():
    client, session = make_client(read_only=True)
    with pytest.raises(ReadOnlyError):
        client.post("/rest/api/2/issue?x=/rest/api/2/search", {"fields": {}})
    assert session.calls == []


def test_post_to_a_path_merely_ending_in_search_is_refused_in_read_only_mode():
    client, session = make_client(read_only=True)
    with pytest.raises(ReadOnlyError):
        client.post("/rest/api/2/issue/rest/api/2/search", {})
    assert session.calls == []


def test_post_to_search_with_a_query_is_refused_in_read_only_mode():
    client, session = make_client(read_only=True)
    with pytest.raises(ReadOnlyError):
        client.post(f"{SEARCH_PATH}?jql=x", {})
    assert session.calls == []


def test_post_to_the_exact_search_path_is_allowed_in_read_only_mode():
    client, session = make_client(read_only=True)
    client.post(SEARCH_PATH, {"jql": "project = PROJ"})
    assert session.calls == [("POST", f"{BASE}{SEARCH_PATH}")]


def test_post_to_search_under_a_context_path_is_allowed_in_read_only_mode():
    client, session = make_client(read_only=True, base_url=f"{BASE}/jira")
    client.post(SEARCH_PATH, {"jql": "project = PROJ"})
    assert session.calls == [("POST", f"{BASE}/jira{SEARCH_PATH}")]


def test_post_to_an_absolute_search_url_is_allowed_in_read_only_mode():
    client, session = make_client(read_only=True)
    client.post(f"{BASE}{SEARCH_PATH}", {"jql": "project = PROJ"})
    assert len(session.calls) == 1


# --------------------------------------------------------------------------- #
# F1 defence in depth: path traversal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/rest/api/2/issue/PROJ-1/../../project/PROJ",
        "/rest/api/2/issue/./PROJ-1",
        "/rest/api/2/issue/PROJ-1/..",
        "/rest/api/2/issue/..;/project/PROJ",
        "/rest/api/2/issue/PROJ-1/%2e%2e/%2E%2E/project/PROJ",
        "/rest/api/2/issue/PROJ-1%2f..%2fproject",
        "/rest/api/2/issue/PROJ-1%5c..%5cproject",
        "/rest/api/2/issue/PROJ-1\\..\\project",
        "/rest/api/2/issue/PROJ-1%252e%252e",
        "/rest/api/2/issue/PROJ-1#/../project",
        "/rest/api/2/issue/PROJ-1?a=1?b=2",
        "/rest/api/2/issue/../project?expand=all",
    ],
)
def test_traversal_paths_are_refused_before_sending(path):
    client, session = make_client()
    with pytest.raises(InputError):
        client.delete(path)
    assert session.calls == []


def test_the_audit_probe_key_cannot_turn_delete_issue_into_delete_project():
    client, session = make_client()
    with pytest.raises(InputError, match=r"'\.' or '\.\.'"):
        client.delete("/rest/api/2/issue/PROJ-1/../../project/PROJ")
    assert session.calls == []


def test_an_absolute_url_with_a_traversal_is_refused():
    client, session = make_client()
    with pytest.raises(InputError):
        client.get(f"{BASE}/rest/api/2/issue/PROJ-1/../../project/PROJ")
    assert session.calls == []


def test_an_absolute_url_with_a_fragment_is_refused():
    client, _ = make_client()
    with pytest.raises(InputError, match="#"):
        client.build_url(f"{BASE}/rest/api/2/myself#frag")


def test_a_single_inline_query_string_is_still_accepted():
    client, _ = make_client()
    url = client.build_url("/rest/api/2/issue/PROJ-1?expand=changelog")
    assert url == f"{BASE}/rest/api/2/issue/PROJ-1?expand=changelog"


def test_dots_inside_a_segment_are_not_dot_segments():
    client, _ = make_client()
    url = client.build_url("/rest/api/2/attachment/report.v1.json")
    assert url == f"{BASE}/rest/api/2/attachment/report.v1.json"


def test_a_query_value_may_contain_dots_and_slashes():
    client, _ = make_client()
    url = client.build_url("/rest/api/2/search?jql=a/../b")
    assert url.endswith("?jql=a/../b")


# --------------------------------------------------------------------------- #
# F9: default port normalisation
# --------------------------------------------------------------------------- #


def test_explicit_default_https_port_is_the_same_origin():
    client, _ = make_client()
    url = f"https://jira.example.com:443{SEARCH_PATH}"
    assert client.build_url(url) == url


def test_base_url_with_explicit_443_accepts_a_portless_url():
    client, _ = make_client(base_url="https://jira.example.com:443")
    url = f"https://jira.example.com{SEARCH_PATH}"
    assert client.build_url(url) == url


def test_explicit_default_http_port_is_the_same_origin():
    client, _ = make_client(base_url="http://jira.example.com")
    url = f"http://jira.example.com:80{SEARCH_PATH}"
    assert client.build_url(url) == url


def test_port_443_over_http_is_still_another_origin():
    client, _ = make_client()
    with pytest.raises(InputError, match="not on"):
        client.build_url(f"http://jira.example.com:443{SEARCH_PATH}")


# --------------------------------------------------------------------------- #
# F8: redirect cap
# --------------------------------------------------------------------------- #


def test_the_default_session_caps_redirects():
    client = JiraClient(make_settings())
    assert client._session.max_redirects == MAX_REDIRECTS == 5


def test_an_injected_session_is_capped_too():
    client, session = make_client()
    assert session.max_redirects == MAX_REDIRECTS


def test_a_redirect_loop_stops_after_the_cap():
    loop = f"{BASE}/rest/api/2/issue"
    adapter = FakeAdapter({loop: (307, {"Location": loop})})
    client = make_transport_client(adapter)
    with pytest.raises(JiraError, match="attempt"):
        client.post("/rest/api/2/issue", {"fields": {}})
    assert len(adapter.sent) == 6


# --------------------------------------------------------------------------- #
# F8: no ~/.netrc credentials
# --------------------------------------------------------------------------- #


def test_netrc_fixture_really_injects_credentials_into_a_plain_session(
    netrc_has_credentials,
):
    """Guard: proves the fixture would expose a netrc leak if one existed."""
    adapter = FakeAdapter()
    session = requests.Session()
    session.mount("https://", adapter)
    session.get(f"{BASE}/x")
    assert adapter.sent[0].headers["Authorization"].startswith("Basic ")


def test_pat_requests_carry_the_bearer_token_and_not_netrc(netrc_has_credentials):
    adapter = FakeAdapter()
    client = make_transport_client(adapter)
    client.get("/rest/api/2/myself")
    assert adapter.sent[0].headers["Authorization"] == f"Bearer {SECRET}"


def test_basic_requests_use_the_configured_user_and_not_netrc(netrc_has_credentials):
    adapter = FakeAdapter()
    client = make_transport_client(adapter, auth_mode=AUTH_BASIC, secret="pw-value-1")
    client.get("/rest/api/2/myself")
    expected = requests.auth._basic_auth_str("alice", "pw-value-1")
    assert adapter.sent[0].headers["Authorization"] == expected


def test_a_cross_host_redirect_drops_auth_and_does_not_add_netrc(netrc_has_credentials):
    start = f"{BASE}/rest/api/2/myself"
    adapter = FakeAdapter({start: (302, {"Location": "https://other.example.com/x"})})
    client = make_transport_client(adapter)
    client.get("/rest/api/2/myself")
    assert [r.url for r in adapter.sent] == [start, "https://other.example.com/x"]
    assert "Authorization" not in adapter.sent[1].headers


def test_a_same_host_redirect_keeps_the_bearer_token(netrc_has_credentials):
    start = f"{BASE}/rest/api/2/myself"
    target = f"{BASE}/rest/api/2/user"
    adapter = FakeAdapter({start: (302, {"Location": target})})
    client = make_transport_client(adapter)
    client.get("/rest/api/2/myself")
    assert adapter.sent[1].headers["Authorization"] == f"Bearer {SECRET}"


def test_proxy_settings_from_the_environment_are_still_honoured():
    client = JiraClient(make_settings())
    assert client._session.trust_env is True


def test_the_bearer_auth_repr_hides_the_token():
    client = JiraClient(make_settings())
    assert SECRET not in repr(client._session.auth)
