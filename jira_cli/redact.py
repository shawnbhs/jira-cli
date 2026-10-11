"""Secret scrubbing for everything this package is about to print, log or raise.

Nothing in jira-cli writes a credential to a stream. Two layers protect that:

1. *Pattern* redaction — recognises the shapes a Jira credential takes
   (``Authorization`` headers, cookies, ``JIRA_PAT=...`` assignments,
   ``?token=...`` / ``?os_authToken=...`` query parameters, URL userinfo)
   even for secrets this process has never seen.
2. *Literal* redaction — :func:`register_secret` records the exact values
   loaded from the environment, plus the encodings a server or library may
   echo them in, so a token that leaks in an unexpected shape is still masked.

:func:`redact` is cheap enough to call on every error message and every log
line, and it is applied in exactly those places.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote, quote_plus

MASK = "***"
"""Replacement text for every redacted value."""

_MIN_LITERAL_LENGTH = 4
"""Shorter literals are too generic to mask without mangling ordinary text.

Registered values are real credentials loaded from the environment, so even a
short one is masked: a server that echoes a 5-character password back must
not print it.
"""

_SECRET_LITERALS: set[str] = set()

# Keys whose *value* is always a credential, in env-file / shell / CLI shapes.
_SECRET_KEYS = (
    "JIRA_PAT",
    "JIRA_PASSWORD",
    "JIRA_API_TOKEN",
    "JIRA_TOKEN",
    "GITLAB_TOKEN",
    "PRIVATE-TOKEN",
    "os_authToken",
    "seraph.rememberme.cookie",
    "atlassian.xsrf.token",
    "JSESSIONID",
    "x-api-key",
    "x-auth-token",
    "auth_token",
    "access_token",
    "api_token",
    "apitoken",
    "api_key",
    "api-key",
    "apikey",
    "token",
    "password",
    "passwd",
    "pwd",
    "secret",
    "jwt",
    "pat",
)

_KEY_ALTERNATION = "|".join(re.escape(key) for key in _SECRET_KEYS)

# Words that may follow "password"/"token" in prose and are not a credential.
_PROSE_STOPWORDS = r"(?:is|was|for|of|has|must|reset|expired|required)\b"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Whole Cookie / Set-Cookie header values: every pair is a session secret.
    (
        re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]+"),
        rf"\1{MASK}",
    ),
    # Authorization: Bearer x / Basic x — always, whatever the value looks like,
    # including the JSON-quoted form '"Authorization": "Bearer x"'.
    (
        re.compile(
            r"(?i)\b(authorization[\"']?\s*[=:]\s*[\"']?(?:bearer|basic)\s+)[^\s\"',;]+"
        ),
        rf"\1{MASK}",
    ),
    # A bare "Bearer x" / "Basic x" only when x is token-shaped: 8+ characters
    # including a digit or symbol, so prose like "Basic authentication is
    # disabled" survives.
    (
        re.compile(
            r"(?i)\b(bearer|basic)\s+"
            r"(?=[A-Za-z0-9\-._~+/]*[0-9+/_\-.~])[A-Za-z0-9\-._~+/]{8,}={0,2}"
        ),
        rf"\1 {MASK}",
    ),
    # KEY=value / KEY: value / KEY = "value"  — quoted or bare
    (
        re.compile(
            rf"(?i)(?<![\w.-])({_KEY_ALTERNATION})(\s*[=:]\s*)"
            rf"(\"[^\"]*\"|'[^']*'|[^\s,;&)\]}}]+)"
        ),
        rf"\1\2{MASK}",
    ),
    # Prose: "password hunter2xyz", "token dGVzdC..." (no = or :). The value
    # must contain a digit or symbol, so "password reset" or "token expired"
    # stay readable.
    (
        re.compile(
            rf"(?i)\b(password|passwd|token)(\s+)(?!{_PROSE_STOPWORDS})"
            r"(?=\S*[0-9!@#$%^&*_+=/\\-])(\S{6,})"
        ),
        rf"\1\2{MASK}",
    ),
    # URL credentials: https://user:password@host — the password may itself
    # contain '@', so match up to the LAST '@' before the host.
    (
        re.compile(r"(?i)(//[^/\s:@]+:)([^/\s]+)(@[^@/\s?#]+)"),
        rf"\1{MASK}\3",
    ),
)


def register_secret(value: Any) -> None:
    """Remember ``value`` so :func:`redact` masks it wherever it appears.

    Called by :mod:`jira_cli.config` and :mod:`jira_cli.client` as soon as a
    credential is loaded. Values shorter than four characters are ignored:
    they are too generic to mask without corrupting unrelated text. The
    URL-encoded and JSON-escaped forms are registered too.
    """
    if value is None:
        return
    text = str(value).strip()
    if len(text) < _MIN_LITERAL_LENGTH:
        return
    variants = {
        text,
        quote(text, safe=""),
        quote_plus(text),
        json.dumps(text)[1:-1],
    }
    _SECRET_LITERALS.update(v for v in variants if len(v) >= _MIN_LITERAL_LENGTH)


def register_basic(username: Any, secret: Any) -> None:
    """Register ``secret`` and the base64 ``username:secret`` Basic credential.

    That base64 string is what travels in an ``Authorization: Basic`` header,
    so it is masked even where no header name precedes it.
    """
    register_secret(secret)
    if secret is None or not str(secret).strip():
        return
    user = "" if username is None else str(username)
    pair = f"{user}:{str(secret).strip()}"
    encoded = base64.b64encode(pair.encode("utf-8")).decode("ascii")
    _SECRET_LITERALS.add(encoded)
    stripped = encoded.rstrip("=")
    if len(stripped) >= _MIN_LITERAL_LENGTH:
        _SECRET_LITERALS.add(stripped)


def registered_secrets() -> frozenset[str]:
    """Return the registered literals. Exposed for tests and diagnostics only."""
    return frozenset(_SECRET_LITERALS)


def clear_secrets() -> None:
    """Forget every registered literal. Used by tests to stay isolated."""
    _SECRET_LITERALS.clear()


def redact(text: Any) -> str:
    """Return ``text`` with every recognisable credential replaced by ``***``.

    Accepts any object (it is stringified first) so callers can hand it an
    exception, a dict or a URL without thinking about types.
    """
    if text is None:
        return ""
    result = text if isinstance(text, str) else str(text)

    # Literals first: a known token must not survive because a pattern
    # rewrote the text around it.
    for literal in sorted(_SECRET_LITERALS, key=len, reverse=True):
        if literal in result:
            result = result.replace(literal, MASK)

    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


__all__ = [
    "MASK",
    "clear_secrets",
    "redact",
    "register_basic",
    "register_secret",
    "registered_secrets",
]
