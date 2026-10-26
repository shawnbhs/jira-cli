"""HTTP access to Jira Server's REST v2 and Agile 1.0 APIs.

The client is deliberately small and strict:

* It only ever talks to ``settings.base_url``'s origin. An absolute URL that
  points elsewhere raises :class:`~jira_cli.errors.InputError` rather than
  sending the Authorization header to another host.
* Writes are refused outright in read-only mode, and **never retried** — a
  retried POST creates a second issue.
* Retries apply to ``GET`` only, on transient statuses and connection errors,
  honouring ``Retry-After``.
* Request paths are validated: dot segments, encoded separators and
  fragments are refused, so an interpolated value cannot walk to another
  endpoint (``PROJ-1/../../project/PROJ``).
* ``~/.netrc`` is never consulted for credentials; proxies and CA bundles
  from the environment still are.
* Nothing prints. Headers are never logged, the secret never reaches
  ``repr()``, and every message raised passes through
  :func:`jira_cli.redact.redact`.
* Error messages carry only Jira's structured ``errorMessages``/``errors``,
  each clipped; a non-JSON error body (an HTML error page, a proxy page, a
  stack trace) is never quoted, only its size is reported. Errors carry the
  request path, never the host or query values.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from .config import AUTH_PAT, Settings
from .errors import (
    AuthError,
    InputError,
    JiraError,
    JiraPermissionError,
    NotFoundError,
    PartialResultError,
    ReadOnlyError,
)
from .redact import redact, register_basic, register_secret

API = "/rest/api/2"
SEARCH_PATH = f"{API}/search"

RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (0.5, 1.0, 2.0)
MAX_RETRY_AFTER = 60.0
"""Cap on an honoured ``Retry-After``; a hostile value must not hang the CLI."""

DEFAULT_PAGE_SIZE = 100

MAX_REDIRECTS = 5
"""Redirect cap; requests' default of 30 only prolongs a redirect loop."""

DEFAULT_PORTS = {"https": 443, "http": 80}

MAX_ERROR_PART = 300
"""Characters kept from each Jira error message or field error."""

MAX_ERROR_PARTS = 10
"""Jira error messages kept; the rest are counted, not quoted."""

_CONNECTION_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("CERTIFICATE_VERIFY_FAILED", "certificate verify failed"), "TLS certificate verification failed"),
    (("SSLError", "SSL:"), "TLS handshake failed"),
    (
        ("NameResolutionError", "Name or service not known", "getaddrinfo", "nodename nor servname"),
        "host name could not be resolved",
    ),
    (("Connection refused", "ConnectionRefused"), "connection refused"),
    (("timed out", "Timeout"), "timed out"),
    (("ProxyError",), "proxy error"),
    (("TooManyRedirects",), "too many redirects"),
)
"""Fixed phrases for a connection failure. The exception text itself names the
host, the full URL and its query, so it is classified, never quoted."""

_DOT_SEGMENTS = frozenset({".", ".."})
_FORBIDDEN_PATH_ENCODINGS = re.compile(r"%(?:2e|2f|5c|25|00)", re.IGNORECASE)
"""Encoded ``.``, ``/``, backslash, ``%`` and NUL: a server or proxy may decode
them into a traversal after our literal checks have passed."""


def _check_path(path: str) -> None:
    """Refuse a URL path that could be resolved to a different endpoint.

    ``requests``/urllib3 collapse ``..`` before sending, and Tomcat (which
    Jira runs on) also treats ``..;`` as ``..``; both turn an interpolated
    value into a path traversal. Encoded separators and backslashes are
    refused for the same reason.

    Raises:
        InputError: the path contains a dot segment, an encoded dot/slash/
            backslash, a backslash, or a fragment.
    """
    if "#" in path:
        raise InputError(f"request path must not contain '#': {redact(path)!r}")
    if "\\" in path or _FORBIDDEN_PATH_ENCODINGS.search(path):
        raise InputError(
            f"request path contains an encoded or escaped separator: {redact(path)!r}"
        )
    for segment in path.split("/"):
        if segment.split(";", 1)[0] in _DOT_SEGMENTS:
            raise InputError(
                f"request path must not contain '.' or '..' segments: {redact(path)!r}"
            )


def _check_relative(text: str) -> None:
    """Validate a ``/path`` or ``/path?query`` request target.

    Some callers inline a query string; a single ``?`` is tolerated and the
    query is passed through untouched, but the path before it must be clean.

    Raises:
        InputError: more than one ``?``, or the path part fails
            :func:`_check_path`.
    """
    if text.count("?") > 1:
        raise InputError(f"request path must contain at most one '?': {redact(text)!r}")
    path, _, _query = text.partition("?")
    _check_path(path)


class _BearerAuth(requests.auth.AuthBase):
    """Send a PAT as ``Authorization: Bearer``.

    Installing the PAT as ``session.auth`` (rather than a default header)
    stops requests from substituting ``~/.netrc`` credentials, which it does
    whenever ``session.auth`` is empty.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = "Bearer " + self._token
        return request

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _BearerAuth) and other._token == self._token

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return "_BearerAuth(***)"


class _NoNetrcSession(requests.Session):
    """A ``requests.Session`` that never reads ``~/.netrc``.

    ``trust_env`` stays on so ``HTTPS_PROXY``/``NO_PROXY`` and
    ``REQUESTS_CA_BUNDLE`` keep working on corporate networks; turning it off
    would drop proxy support. requests consults netrc in two places, both
    neutralised: ``prepare_request`` (skipped because ``auth`` is always set,
    see :meth:`JiraClient._apply_auth`) and ``rebuild_auth`` on a redirect
    (overridden here to strip only, never to add).
    """

    def rebuild_auth(self, prepared_request: requests.PreparedRequest, response: Any) -> None:
        """Strip ``Authorization`` when a redirect changes host; never add netrc auth."""
        headers = prepared_request.headers
        if "Authorization" in headers and self.should_strip_auth(
            response.request.url, prepared_request.url
        ):
            del headers["Authorization"]


def _clip(value: Any) -> str:
    """One line of at most :data:`MAX_ERROR_PART` characters."""
    text = " ".join(str(value).split())
    if len(text) > MAX_ERROR_PART:
        text = text[: MAX_ERROR_PART - 3].rstrip() + "..."
    return text


def _join_messages(payload: Any, *, allow_message: bool = True) -> str:
    """Flatten Jira's ``errorMessages``/``errors`` into one line.

    Each message is whitespace-collapsed and clipped to
    :data:`MAX_ERROR_PART` characters; at most :data:`MAX_ERROR_PARTS` are
    kept. Jira's own wording (for example ``The value 'x' does not exist for
    the field 'assignee'.``) is otherwise kept verbatim, because callers match
    on it. The free-form ``message`` key is used only when ``allow_message``
    is true: on a 5xx it is often an exception dump.
    """
    if not isinstance(payload, dict):
        return ""
    messages = payload.get("errorMessages")
    if isinstance(messages, str):
        messages = [messages]
    parts: list[str] = []
    if isinstance(messages, list):
        parts.extend(_clip(m) for m in messages if m)
    field_errors = payload.get("errors")
    if isinstance(field_errors, dict):
        parts.extend(f"{_clip(name)}: {_clip(text)}" for name, text in field_errors.items())
    message = payload.get("message")
    if not parts and allow_message and isinstance(message, str) and message.strip():
        parts.append(_clip(message))
    parts = [part for part in parts if part]
    if len(parts) > MAX_ERROR_PARTS:
        extra = len(parts) - MAX_ERROR_PARTS
        parts = [*parts[:MAX_ERROR_PARTS], f"(+{extra} more)"]
    return "; ".join(parts)


def _body_size(response: Any) -> int:
    """Byte length of a response body, without keeping any of it."""
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return len(content)
    text = getattr(response, "text", "") or ""
    return len(str(text).encode("utf-8", "replace"))


def _connection_reason(exc: BaseException | None) -> str:
    """``ConnectionError (host name could not be resolved)`` — never the raw text."""
    if exc is None:
        return "connection failed"
    name = type(exc).__name__
    raw = f"{name} {exc}"
    for needles, reason in _CONNECTION_REASONS:
        if any(needle.lower() in raw.lower() for needle in needles):
            return f"{name} ({reason})"
    return name


class JiraClient:
    """A thin, retrying, read-only-aware wrapper around ``requests.Session``."""

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        """Build a client.

        Args:
            settings: Resolved configuration, including the credential.
            session: Injected session, mainly for tests. A fresh
                ``requests.Session`` is created when omitted.
        """
        self.settings = settings
        self._session = session if session is not None else _NoNetrcSession()
        self._session.max_redirects = MAX_REDIRECTS
        self._myself: dict | None = None
        register_secret(settings.secret)
        self._apply_auth()

    # -- construction helpers ------------------------------------------------

    def _apply_auth(self) -> None:
        """Install the credential on the session.

        A PAT goes through :class:`_BearerAuth`; basic auth uses requests'
        own tuple. Either way ``session.auth`` is set, so the value never sits
        in our header dict and requests never falls back to ``~/.netrc``.
        """
        self._session.headers.update({"Accept": "application/json"})
        if self.settings.auth_mode == AUTH_PAT:
            self._session.auth = _BearerAuth(self.settings.secret)
        else:
            register_basic(self.settings.username, self.settings.secret)
            self._session.auth = (self.settings.username, self.settings.secret)

    def __repr__(self) -> str:
        return (
            f"JiraClient(base_url={self.settings.base_url!r}, "
            f"auth_mode={self.settings.auth_mode!r}, "
            f"read_only={self.settings.read_only!r})"
        )

    __str__ = __repr__

    # -- url handling --------------------------------------------------------

    @staticmethod
    def _origin_of(url: str) -> tuple[str, str, int | None]:
        """``(scheme, host, port)`` with the scheme's default port filled in."""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        return scheme, (parts.hostname or "").lower(), parts.port or DEFAULT_PORTS.get(scheme)

    def _origin(self) -> tuple[str, str, int | None]:
        return self._origin_of(self.settings.base_url)

    def build_url(self, path: str) -> str:
        """Turn ``/rest/...`` into an absolute URL, or validate an absolute one.

        Raises:
            InputError: ``path`` is empty, is not rooted at ``/``, fails the
                traversal checks of :func:`_check_path`, or is an absolute URL
                whose origin is not ``settings.base_url``'s. The origin check
                exists so a redirect or a caller mistake can never send the
                credential to a third-party host.
        """
        text = (path or "").strip()
        if not text:
            raise InputError("a request path is required")
        if "://" in text:
            if self._origin_of(text) != self._origin():
                raise InputError(
                    f"refusing to call {redact(text)}: it is not on "
                    f"{self.settings.base_url}"
                )
            parts = urlsplit(text)
            _check_relative(parts.path + (f"?{parts.query}" if parts.query else ""))
            if "#" in text:
                raise InputError(f"request URL must not contain '#': {redact(text)!r}")
            return text
        if not text.startswith("/"):
            raise InputError(f"request path must start with '/', got {text!r}")
        _check_relative(text)
        return f"{self.settings.base_url}{text}"

    def _is_search_url(self, url: str) -> bool:
        """True only for the bare search endpoint: exact path, no query.

        A suffix match on the URL string would let
        ``/rest/api/2/issue?x=/rest/api/2/search`` pass as a read.
        """
        parts = urlsplit(url)
        context = urlsplit(self.settings.base_url).path.rstrip("/")
        return not parts.query and parts.path.rstrip("/") == f"{context}{SEARCH_PATH}"

    # -- read-only gate ------------------------------------------------------

    def ensure_writable(self) -> None:
        """Raise :class:`ReadOnlyError` when this client must not write."""
        if self.settings.read_only:
            raise ReadOnlyError(
                "this jira-cli is in read-only mode (JIRA_READ_ONLY); "
                "no changes were made"
            )

    # -- request plumbing ----------------------------------------------------

    def _raise_for_status(self, response: Any, url: str) -> None:
        """Raise the error class for ``response``'s status, if it is an error.

        The message is Jira's structured ``errorMessages``/``errors`` only
        (clipped and redacted). A body that is not JSON is never quoted: an
        HTML error page or stack trace would carry internal host names and
        code paths into the terminal or the model. Only its size is given.
        The URL is reduced to its path by :class:`JiraError`.
        """
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 400:
            return
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON error body is normal
            size = _body_size(response)
            detail = f"Jira returned HTTP {status} (non-JSON response, {size} bytes)" if size else ""
        else:
            detail = _join_messages(payload, allow_message=status < 500)
        message = redact(detail.strip() or f"Jira returned HTTP {status}")
        safe_url = url
        if status == 401:
            raise AuthError(message, status=status, url=safe_url)
        if status == 403:
            raise JiraPermissionError(message, status=status, url=safe_url)
        if status == 404:
            raise NotFoundError(message, status=status, url=safe_url)
        raise JiraError(message, status=status, url=safe_url)

    @staticmethod
    def _retry_delay(response: Any, attempt: int) -> float:
        """Seconds to wait before the next attempt, honouring ``Retry-After``."""
        headers = getattr(response, "headers", None) or {}
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
        if raw is not None:
            try:
                return max(0.0, min(float(str(raw).strip()), MAX_RETRY_AFTER))
            except (TypeError, ValueError):
                pass
        return RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]

    @staticmethod
    def _parse(response: Any) -> Any:
        """Parsed JSON body, or ``None`` for 204 / an empty body."""
        if int(getattr(response, "status_code", 0) or 0) == 204:
            return None
        text = getattr(response, "text", None)
        if text is not None and not str(text).strip():
            return None
        try:
            return response.json()
        except Exception:  # noqa: BLE001 - treat unparsable success as empty
            return None

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
    ) -> Any:
        url = self.build_url(path)
        verb = method.upper()
        retryable = verb == "GET"
        attempts = RETRY_ATTEMPTS if retryable else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = self._session.request(
                    verb,
                    url,
                    params=params,
                    json=json,
                    timeout=self.settings.timeout,
                    verify=self.settings.verify_tls,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
                continue

            status = int(getattr(response, "status_code", 0) or 0)
            if retryable and status in RETRY_STATUSES and attempt + 1 < attempts:
                time.sleep(self._retry_delay(response, attempt))
                continue

            self._raise_for_status(response, url)
            return self._parse(response)

        raise JiraError(
            f"could not reach Jira after {attempts} attempt(s): {_connection_reason(last_error)}",
            url=url,
        )

    # -- verbs ---------------------------------------------------------------

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET ``path``. Retried on transient failures."""
        return self._request("GET", path, params=params)

    def post(self, path: str, json: Any = None, params: dict | None = None) -> Any:
        """POST ``path``. Refused in read-only mode; never retried.

        ``POST /rest/api/2/search`` is a read and is therefore allowed in
        read-only mode.
        """
        if not self._is_search_url(self.build_url(path)):
            self.ensure_writable()
        return self._request("POST", path, json=json, params=params)

    def put(self, path: str, json: Any = None, params: dict | None = None) -> Any:
        """PUT ``path``. Refused in read-only mode; never retried."""
        self.ensure_writable()
        return self._request("PUT", path, json=json, params=params)

    def delete(self, path: str, params: dict | None = None) -> Any:
        """DELETE ``path``. Refused in read-only mode; never retried."""
        self.ensure_writable()
        return self._request("DELETE", path, params=params)

    # -- paging --------------------------------------------------------------

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        *,
        items_key: str = "values",
        page_size: int = DEFAULT_PAGE_SIZE,
        limit: int | None = None,
    ) -> list[dict]:
        """Walk a ``startAt``/``maxResults`` endpoint and collect its items.

        Stops on ``isLast``, on an empty page, or when ``limit`` is reached.

        Raises:
            PartialResultError: The server reported a ``total`` larger than the
                number of items collected while no ``limit`` capped the walk.
                Returning a short list silently would corrupt every report
                built on top of it.
        """
        if page_size <= 0:
            raise InputError(f"page_size must be positive, got {page_size}")
        collected: list[dict] = []
        start_at = 0
        total: int | None = None

        while True:
            want = page_size if limit is None else min(page_size, limit - len(collected))
            if want <= 0:
                break
            page_params = dict(params or {})
            page_params.update({"startAt": start_at, "maxResults": want})
            payload = self.get(path, page_params)

            if isinstance(payload, list):
                items = payload
                is_last = len(items) < want
            elif isinstance(payload, dict):
                items = payload.get(items_key) or []
                if isinstance(payload.get("total"), int):
                    total = payload["total"]
                is_last = bool(payload.get("isLast")) or len(items) < want
            else:
                break

            collected.extend(items)
            if is_last or not items:
                break
            start_at += len(items)

        if limit is not None:
            collected = collected[:limit]
            if len(collected) >= limit:
                return collected
        if total is not None and len(collected) < total:
            raise PartialResultError(
                f"{path} reported {total} items but only {len(collected)} "
                "could be fetched; the result would be incomplete",
                url=self.build_url(path),
            )
        return collected

    # -- search --------------------------------------------------------------

    def search(
        self,
        jql: str,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Run a JQL search and return the raw issue dicts."""
        if not (jql or "").strip():
            raise InputError("a JQL query is required")
        params: dict[str, Any] = {"jql": jql}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = ",".join(expand)
        return self.paginate(SEARCH_PATH, params, items_key="issues", limit=limit)

    def count(self, jql: str) -> int:
        """Return how many issues match ``jql`` (server-side ``total``)."""
        if not (jql or "").strip():
            raise InputError("a JQL query is required")
        payload = self.get(SEARCH_PATH, {"jql": jql, "maxResults": 0}) or {}
        total = payload.get("total") if isinstance(payload, dict) else None
        return int(total or 0)

    # -- identity ------------------------------------------------------------

    def myself(self) -> dict:
        """Return (and cache) ``/rest/api/2/myself``.

        On success in PAT mode the resolved username is remembered on
        ``settings.username`` only if it was empty, so services can rely on it.
        """
        if self._myself is None:
            payload = self.get(f"{API}/myself") or {}
            self._myself = payload if isinstance(payload, dict) else {}
            name = self._myself.get("name")
            if name and not self.settings.username:
                object.__setattr__(self.settings, "username", str(name))
        return self._myself

    def browse_url(self, key: str) -> str:
        """Human-facing URL for an issue key."""
        return f"{self.settings.base_url}/browse/{key}"


__all__ = ["API", "JiraClient", "SEARCH_PATH"]
