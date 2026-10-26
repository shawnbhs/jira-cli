"""Single-use confirmation tokens for MCP write tools.

The MCP server never mutates Jira on the first call to a write tool. It builds a
preview, mints a token with :meth:`ConfirmStore.issue`, and returns both to the
client. Only a *second* call that carries the token **and** byte-identical
arguments is allowed to execute.

What the token is, and is not: it is an argument-binding, anti-tamper check
that the executing call is exactly the previewed one (the MCP server also adds
the values it resolved from the arguments to the digested mapping). It is
**not human consent** -- the token is returned to the same model that redeems
it. Human approval is the MCP client's per-call tool approval, which the
server's ``ToolAnnotations`` let the client apply to write tools only.

Design notes (the threat being defended against is a prompt-injected write):

* A token is bound to the tool name *and* to the SHA-256 of the canonical JSON of
  the arguments, so an approved token cannot be replayed against a different tool
  or against mutated arguments.
* A token is single use. Any match on the token value consumes it — including a
  match where the tool or the argument digest is wrong. A tampering attempt
  therefore burns the approval instead of letting the caller retry with the
  correct arguments.
* Tokens expire after ``ttl`` seconds (15 minutes by default).
* The clock is injectable so expiry is testable without sleeping.
* The store is bounded (256 entries by default); inserting past the bound evicts
  the oldest entry, so an attacker cannot grow the process's memory by spamming
  first calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["CONFIRM_TOKEN_ARG", "ConfirmStore", "canonical_json", "args_digest"]

#: Name of the argument that carries the token. It is excluded from the digest so
#: that the first call (token absent) and the second call (token present) hash the
#: same arguments.
CONFIRM_TOKEN_ARG = "confirm_token"  # noqa: S105 - an argument name, not a secret

#: Default time-to-live of a token, in seconds.
DEFAULT_TTL = 900

#: Default maximum number of live tokens kept in memory.
DEFAULT_MAX_ENTRIES = 256


def canonical_json(args: Mapping[str, Any] | None) -> str:
    """Return a stable JSON text for *args*, ignoring the confirm-token argument.

    Keys are sorted and separators are compact so that two logically identical
    argument mappings always produce the same text. Values that are not natively
    JSON-serialisable are rendered with :func:`repr`-ish ``str`` coercion rather
    than raising, because the digest only has to be *stable*, not reversible.
    """
    payload = {
        str(key): value
        for key, value in (args or {}).items()
        if str(key) != CONFIRM_TOKEN_ARG
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def args_digest(args: Mapping[str, Any] | None) -> str:
    """Return the SHA-256 hex digest of :func:`canonical_json` of *args*."""
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Entry:
    """One pending confirmation."""

    tool: str
    digest: str
    expires_at: float


class ConfirmStore:
    """In-memory store of pending write confirmations.

    Args:
        ttl: Seconds a token stays valid. Defaults to 900 (15 minutes).
        max_entries: Maximum number of pending tokens. The oldest is evicted
            when a new token would exceed the bound.
        clock: Zero-argument callable returning a monotonically increasing float
            number of seconds. Injectable for tests.
        token_factory: Zero-argument callable returning a fresh token string.
            Defaults to ``secrets.token_urlsafe(16)``.
    """

    def __init__(
        self,
        ttl: float = DEFAULT_TTL,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(16))
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    # -- public API ---------------------------------------------------------

    def issue(self, tool: str, args: Mapping[str, Any] | None = None) -> dict:
        """Mint a single-use token binding *tool* to the digest of *args*.

        Returns a dict with ``confirm_token``, ``expires_in`` and ``tool`` —
        safe to merge into a ``needs_confirmation`` tool result. No secret other
        than the token itself is included.
        """
        now = self._clock()
        self._purge(now)
        token = self._token_factory()
        self._entries[token] = _Entry(
            tool=str(tool),
            digest=args_digest(args),
            expires_at=now + self.ttl,
        )
        self._entries.move_to_end(token)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return {
            "confirm_token": token,
            "expires_in": int(self.ttl),
            "tool": str(tool),
        }

    def check(self, tool: str, args: Mapping[str, Any] | None, token: str | None) -> bool:
        """Consume *token* and report whether it approves this exact call.

        ``True`` only when the token is live, was issued for *tool*, and the
        canonical digest of *args* matches the digest recorded at issue time.

        The token is consumed whenever its value matches a stored entry — even
        when the tool or the arguments do not match — so a tampered retry cannot
        fall back to the approved arguments.
        """
        if not isinstance(token, str) or not token:
            return False
        now = self._clock()
        self._purge(now)
        found = self._pop_matching(token)
        if found is None:
            return False
        entry = found
        if entry.expires_at <= now:
            return False
        tool_ok = hmac.compare_digest(entry.tool.encode("utf-8"), str(tool).encode("utf-8"))
        digest_ok = hmac.compare_digest(entry.digest, args_digest(args))
        return bool(tool_ok and digest_ok)

    def pending(self) -> int:
        """Number of live (unexpired, unconsumed) tokens."""
        self._purge(self._clock())
        return len(self._entries)

    def clear(self) -> None:
        """Drop every pending token."""
        self._entries.clear()

    # -- internals ----------------------------------------------------------

    def _pop_matching(self, token: str) -> _Entry | None:
        """Find and remove the entry whose key equals *token*, in constant time.

        Every stored key is compared with :func:`hmac.compare_digest` rather than
        using a dict lookup, so the comparison does not leak the stored token
        through timing.
        """
        probe = token.encode("utf-8")
        match_key: str | None = None
        for key in self._entries:
            if hmac.compare_digest(key.encode("utf-8"), probe):
                match_key = key
        if match_key is None:
            return None
        return self._entries.pop(match_key)

    def _purge(self, now: float) -> None:
        """Drop expired entries. Entries are inserted in clock order."""
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
