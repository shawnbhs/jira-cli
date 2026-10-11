"""Regression tests for the redaction gaps found in review J9 (F2, F10)."""

from __future__ import annotations

import base64
import json
from urllib.parse import quote, quote_plus

import pytest

from jira_cli import redact as redact_mod
from jira_cli.redact import MASK, redact, register_basic, register_secret

SECRET = "p@ss w0rd!x"  # noqa: S105 - test fixture, not a real credential


@pytest.fixture(autouse=True)
def _isolated_secrets():
    previous = redact_mod.registered_secrets()
    redact_mod.clear_secrets()
    yield
    redact_mod.clear_secrets()
    for value in previous:
        register_secret(value)


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("https://jira.example.com/x?os_authToken=abc123def456", "abc123def456"),  # gitleaks:allow
        ("https://jira.example.com/x?jwt=eyJhbGciOi.payload.sig", "eyJhbGciOi"),
        ("Cookie: JSESSIONID=ABCDEF123456; seraph.rememberme.cookie=zz99", "ABCDEF123456"),
        ("Cookie: JSESSIONID=ABCDEF123456; seraph.rememberme.cookie=zz99", "zz99"),
        ("Set-Cookie: atlassian.xsrf.token=Q1W2E3R4T5; Path=/", "Q1W2E3R4T5"),
        ("X-Api-Key: key-0123456789", "key-0123456789"),  # gitleaks:allow
        ("X-Auth-Token: tok-0123456789", "tok-0123456789"),  # gitleaks:allow
        ("https://alice:p@ss123@jira.example.com/rest", "ss123"),
        ("login failed: password hunter2xyz rejected", "hunter2xyz"),
        ("token dGVzdC10b2tlbi0x", "dGVzdC10b2tlbi0x"),
        ("authorization: basic shortish", "shortish"),
        ('{"Authorization": "Bearer lettersonlytoken"}', "lettersonlytoken"),
    ],
)
def test_pattern_gaps_are_closed(text, leak):
    assert leak not in redact(text)


def test_url_with_at_in_password_keeps_the_host():
    assert redact("https://alice:p@ss123@jira.example.com/rest") == (
        f"https://alice:{MASK}@jira.example.com/rest"
    )


def test_short_registered_secret_is_masked():
    register_secret("ab12c")
    assert "ab12c" not in redact("server said: ab12c is wrong")


def test_three_char_values_are_not_registered():
    register_secret("abc")
    assert redact("abc stays") == "abc stays"


@pytest.mark.parametrize(
    "encode",
    [lambda s: quote(s, safe=""), quote_plus, lambda s: json.dumps(s)[1:-1]],
    ids=["url-quote", "url-quote-plus", "json-escaped"],
)
def test_encoded_forms_of_a_registered_secret_are_masked(encode):
    register_secret(SECRET)
    encoded = encode(SECRET)
    assert encoded not in redact(f"echo {encoded} echo")


def test_json_escaped_secret_with_quotes_is_masked():
    secret = 'tok"en\\123'  # noqa: S105 - test fixture
    register_secret(secret)
    assert json.dumps(secret)[1:-1] not in redact(json.dumps({"value": secret}))


def test_basic_credential_base64_is_masked_anywhere():
    register_basic("alice", SECRET)
    blob = base64.b64encode(f"alice:{SECRET}".encode()).decode()
    assert blob not in redact(f"header dump: {blob}")


@pytest.mark.parametrize(
    "prose",
    [
        "Basic authentication is disabled on this server",
        "Bearer tokens are not accepted here",
        "Your password reset link expired",
        "The token expired, please log in again",
        "password is required",
    ],
)
def test_ordinary_prose_is_not_over_redacted(prose):
    assert redact(prose) == prose
