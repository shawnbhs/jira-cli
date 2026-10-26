"""Error output is safe to hand to a terminal or a model.

An HTTP error carries Jira's structured ``errorMessages``/``errors``
only, clipped and redacted. A non-JSON body (HTML error page, proxy page, Java
stack trace) is never quoted; only its size is reported. The URL an error
carries is the request path, with no host and no query values.

``describe_settings`` reports the secret as ``set``/``not set`` with no
length, and leaves filesystem paths out unless the caller asks for the local
(terminal) view. The MCP ``jira_doctor`` tool calls it without that flag.

No network: a fake session replays canned responses.
"""

# ruff: noqa: S105, S106

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jira_cli.client import MAX_ERROR_PART, MAX_ERROR_PARTS, JiraClient  # noqa: E402
from jira_cli.config import AUTH_PAT, Settings, describe_settings  # noqa: E402
from jira_cli.errors import JiraError, NotFoundError  # noqa: E402
from jira_cli.redact import MASK, register_secret, request_target  # noqa: E402
from jira_cli.services import reports  # noqa: E402

BASE = "https://jira.example.com"
SECRET = "pat-secret-value-778899"
INTERNAL_HOST = "app-node-07.corp.example.com"

HTML_500 = (
    "<!DOCTYPE html><html><head><title>Oops</title></head><body>"
    "<h1>System Error</h1><pre>java.lang.NullPointerException\n"
    "\tat com.atlassian.jira.issue.IssueManager.getIssue(IssueManager.java:412)\n"
    f"\tat {INTERNAL_HOST}:8080/secure/Dashboard.jspa</pre></body></html>"
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json = json_data
        self.headers: dict[str, str] = {}
        self.text = text if text is not None else ("" if json_data is None else "{...}")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeSession:
    def __init__(self, *responses):
        self.headers: dict[str, str] = {}
        self.auth = None
        self.queue = list(responses)

    def request(self, method, url, params=None, json=None, timeout=None, verify=None):
        item = self.queue.pop(0) if self.queue else FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """502/503/504 are retried on GET; do not wait between attempts."""
    monkeypatch.setattr("jira_cli.client.time.sleep", lambda _s: None)


def make_settings(**overrides) -> Settings:
    values: dict = {
        "base_url": BASE,
        "auth_mode": AUTH_PAT,
        "username": "alice",
        "secret": SECRET,
        "default_project": "PROJ",
    }
    values.update(overrides)
    return Settings(**values)


def make_client(*responses, **overrides) -> JiraClient:
    return JiraClient(make_settings(**overrides), session=FakeSession(*responses))


def raised(client: JiraClient, path: str = "/rest/api/2/myself", params=None) -> JiraError:
    with pytest.raises(JiraError) as caught:
        client.get(path, params)
    return caught.value


def everything(error: JiraError) -> str:
    """Every rendering of an error a caller could print or return."""
    return " | ".join([str(error), repr(error), error.message, error.url])


# ----------------------------------------------------------- non-JSON


def test_an_html_error_page_is_never_quoted_only_sized():
    error = raised(make_client(FakeResponse(500, None, text=HTML_500)))

    size = len(HTML_500.encode("utf-8"))
    assert error.message == f"Jira returned HTTP 500 (non-JSON response, {size} bytes)"
    text = everything(error)
    for leak in ("<html", "<pre>", "NullPointerException", "IssueManager.java", INTERNAL_HOST, "jspa"):
        assert leak not in text


def test_a_non_json_body_is_sized_in_bytes_not_characters():
    body = "Fehler: Überlastung"  # "Ü" is two bytes in UTF-8
    error = raised(make_client(*[FakeResponse(502, None, text=body)] * 3))

    assert len(body.encode("utf-8")) == len(body) + 1
    assert error.message.endswith(f"non-JSON response, {len(body) + 1} bytes)")


def test_an_empty_error_body_still_gives_the_status():
    error = raised(make_client(*[FakeResponse(503, None, text="")] * 3))

    assert error.message == "Jira returned HTTP 503"
    assert error.status == 503


def test_a_proxy_page_that_echoes_the_token_is_not_quoted():
    body = f"<html>upstream rejected Authorization: Bearer {SECRET}</html>"
    error = raised(make_client(*[FakeResponse(502, None, text=body)] * 3))

    assert SECRET not in everything(error)
    assert "upstream rejected" not in everything(error)


# ------------------------------------------------------------- JSON


def test_a_jql_400_keeps_jiras_wording_so_the_fallback_can_match_it():
    wording = "The value 'alice' does not exist for the field 'assignee'."
    error = raised(make_client(FakeResponse(400, {"errorMessages": [wording], "errors": {}})))

    assert error.message == wording
    assert reports._UNKNOWN_VALUE.findall(error.message) == ["assignee"]


def test_field_errors_are_kept_and_named():
    body = {"errorMessages": [], "errors": {"summary": "You must specify a summary of the issue."}}
    error = raised(make_client(FakeResponse(400, body)))

    assert error.message == "summary: You must specify a summary of the issue."


def test_each_error_message_is_clipped():
    error = raised(make_client(FakeResponse(400, {"errorMessages": ["x" * 5000]})))

    assert len(error.message) == MAX_ERROR_PART
    assert error.message.endswith("...")


def test_a_long_field_error_is_clipped_too():
    error = raised(make_client(FakeResponse(400, {"errors": {"description": "y" * 5000}})))

    assert len(error.message) <= len("description: ") + MAX_ERROR_PART


def test_the_number_of_error_messages_is_capped():
    messages = [f"problem {n}" for n in range(MAX_ERROR_PARTS + 7)]
    error = raised(make_client(FakeResponse(400, {"errorMessages": messages})))

    assert f"problem {MAX_ERROR_PARTS - 1}" in error.message
    assert f"problem {MAX_ERROR_PARTS};" not in error.message
    assert error.message.endswith("(+7 more)")


def test_a_multi_line_error_message_becomes_one_line():
    body = {"errorMessages": ["first line\n\tat com.example.Thing(Thing.java:1)\r\nlast"]}
    error = raised(make_client(FakeResponse(400, body)))

    assert "\n" not in error.message
    assert "\r" not in error.message
    assert "\t" not in error.message


def test_a_string_error_messages_value_is_not_split_into_characters():
    error = raised(make_client(FakeResponse(400, {"errorMessages": "Field 'foo' does not exist"})))

    assert error.message == "Field 'foo' does not exist"


def test_a_5xx_free_form_message_is_dropped():
    body = {"message": f"java.lang.IllegalStateException at {INTERNAL_HOST}", "status-code": 500}
    error = raised(make_client(FakeResponse(500, body)))

    assert error.message == "Jira returned HTTP 500"
    assert INTERNAL_HOST not in everything(error)


def test_a_4xx_free_form_message_is_kept():
    body = {"message": "Client must be authenticated to access this resource.", "status-code": 401}
    error = raised(make_client(FakeResponse(401, body)))

    assert error.message == "Client must be authenticated to access this resource."


def test_a_secret_inside_a_jira_error_message_is_still_redacted():
    body = {"errorMessages": [f"bad token {SECRET}"], "errors": {"pat": SECRET}}
    error = raised(make_client(FakeResponse(400, body)))

    assert SECRET not in everything(error)
    assert MASK in error.message


# ------------------------------------------------------------ the URL


def test_the_error_url_is_the_path_without_host_or_query():
    client = make_client(FakeResponse(404, {"errorMessages": ["Issue Does Not Exist"]}))
    with pytest.raises(NotFoundError) as caught:
        client.get("/rest/api/2/search?jql=assignee%20%3D%20alice%20AND%20project%3DPROJ")
    error = caught.value

    assert error.url == "/rest/api/2/search?..."
    assert "jira.example.com" not in everything(error)
    assert "alice" not in error.url
    assert str(error) == "Issue Does Not Exist (HTTP 404) [/rest/api/2/search?...]"


def test_the_error_url_keeps_a_context_path():
    client = make_client(FakeResponse(500, None, text=HTML_500), base_url=f"{BASE}/jira")
    error = raised(client, "/rest/api/2/myself")

    assert error.url == "/jira/rest/api/2/myself"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jira.example.com/rest/api/2/issue/PROJ-1", "/rest/api/2/issue/PROJ-1"),
        ("https://jira.example.com/rest/x?token=abc123def456", "/rest/x?..."),  # gitleaks:allow
        ("https://alice:hunter2xyz@jira.example.com/rest/x", "/rest/x"),
        ("https://jira.example.com", "/"),
        ("/rest/api/2/myself#frag", "/rest/api/2/myself"),
        ("/rest/api/2/myself", "/rest/api/2/myself"),
        ("", ""),
        (None, ""),
    ],
)
def test_request_target_keeps_only_the_path(url, expected):
    assert request_target(url) == expected


def test_jira_error_reduces_any_url_it_is_given():
    error = JiraError("nope", status=500, url=f"https://{INTERNAL_HOST}:8443/rest/x?jql=key%3DPROJ-1")

    assert error.url == "/rest/x?..."
    assert INTERNAL_HOST not in everything(error)


def test_a_registered_secret_in_the_path_is_still_masked():
    register_secret(SECRET)

    error = JiraError("nope", url=f"https://jira.example.com/rest/{SECRET}/x")

    assert SECRET not in everything(error)
    assert error.url == f"/rest/{MASK}/x"


# ------------------------------------------------- connection failures


def test_a_connection_failure_does_not_quote_host_or_query():
    raw = requests.ConnectionError(
        f"HTTPSConnectionPool(host='{INTERNAL_HOST}', port=443): Max retries exceeded with "
        "url: /rest/api/2/search?jql=assignee%3Dalice (Caused by NameResolutionError("
        f"\"Failed to resolve '{INTERNAL_HOST}' ([Errno -2] Name or service not known)\"))"
    )
    error = raised(make_client(raw, raw, raw), "/rest/api/2/search")

    text = everything(error)
    assert INTERNAL_HOST not in text
    assert "alice" not in text
    assert "could not reach Jira after 3 attempt(s)" in error.message
    assert "ConnectionError (host name could not be resolved)" in error.message


@pytest.mark.parametrize(
    ("exc", "phrase"),
    [
        (requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] x"), "certificate verification"),
        (requests.exceptions.ConnectTimeout("connect timed out"), "timed out"),
        (requests.ConnectionError("[Errno 111] Connection refused"), "connection refused"),
        (requests.exceptions.ProxyError("Unable to connect to proxy"), "proxy error"),
        (requests.ConnectionError("something odd"), "ConnectionError"),
    ],
)
def test_a_connection_failure_is_classified(exc, phrase):
    error = raised(make_client(exc, exc, exc))

    assert phrase in error.message
    assert str(exc) not in error.message


# --------------------------------------------------------------- describe_settings


def test_describe_settings_says_set_without_a_length():
    described = describe_settings(make_settings())

    assert described["secret"] == "set"
    assert not re.search(r"\d", described["secret"])
    assert str(len(SECRET)) not in repr({k: v for k, v in described.items() if k == "secret"})
    assert SECRET not in repr(described)


def test_describe_settings_says_not_set_for_an_empty_secret():
    assert describe_settings(make_settings(secret=""))["secret"] == "not set"


def test_describe_settings_has_no_filesystem_paths_by_default(tmp_path):
    config_dir = tmp_path / "home" / "alice" / ".config" / "jira-cli"
    settings = make_settings(
        config_dir=config_dir,
        teams_file=config_dir / "teams.yaml",
        filters_file=config_dir / "filters.yaml",
    )

    described = describe_settings(settings)

    assert not {"config_dir", "teams_file", "filters_file"} & set(described)
    assert str(tmp_path) not in repr(described)


def test_describe_settings_local_view_adds_the_paths(tmp_path):
    config_dir = tmp_path / "cfg"
    settings = make_settings(
        config_dir=config_dir,
        teams_file=config_dir / "teams.yaml",
        filters_file=config_dir / "filters.yaml",
    )

    described = describe_settings(settings, local=True)

    assert described["config_dir"] == str(config_dir)
    assert described["teams_file"] == str(config_dir / "teams.yaml")
    assert described["filters_file"] == str(config_dir / "filters.yaml")
    assert described["secret"] == "set"


def test_the_mcp_doctor_uses_the_non_local_view():
    """``jira_doctor`` must not opt in to paths; guard the call site."""
    source = (Path(__file__).resolve().parents[1] / "jira_cli" / "mcp_server.py").read_text(encoding="utf-8")
    calls = re.findall(r"describe_settings\(([^)]*)\)", source)

    assert calls, "mcp_server.py no longer calls describe_settings"
    assert all("local" not in args for args in calls)
