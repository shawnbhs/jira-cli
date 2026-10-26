"""Exception hierarchy for jira-cli.

Every error the package raises is a :class:`JiraError`, so a caller can wrap
one ``except`` around any operation. The important property of this hierarchy
is that **stringifying an error can never leak a credential**: both ``str()``
and ``repr()`` run the message through :func:`jira_cli.redact.redact`.
The URL an error carries is reduced to its path (no host, no query values)
by :func:`jira_cli.redact.request_target`, because error text reaches the
terminal and the MCP client verbatim.
"""

from __future__ import annotations

from .redact import redact, request_target


class JiraError(Exception):
    """Base class for every jira-cli failure.

    Args:
        message: Human-readable description. Redacted on output.
        status: HTTP status code when the failure came from Jira, else ``None``.
        url: Request URL when relevant. Only its path is kept (host and
            query values dropped), and that path is redacted.
    """

    def __init__(
        self,
        message: str = "",
        status: int | None = None,
        url: str = "",
    ) -> None:
        self.message = redact(message)
        self.status = status
        self.url = request_target(url)
        super().__init__(self.message)

    def __str__(self) -> str:
        parts = [self.message or self.__class__.__name__]
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        if self.url:
            parts.append(f"[{self.url}]")
        return " ".join(parts)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(message={self.message!r}, "
            f"status={self.status!r}, url={self.url!r})"
        )


class AuthError(JiraError):
    """HTTP 401 — the credential was rejected."""


class JiraPermissionError(JiraError):
    """HTTP 403 — authenticated, but not allowed to do this."""


class NotFoundError(JiraError):
    """HTTP 404 — the issue, project, board or endpoint does not exist."""


class ReadOnlyError(JiraError):
    """A write was attempted while ``settings.read_only`` is set."""


class PartialResultError(JiraError):
    """Pagination collected fewer items than the server's ``total`` claimed.

    Raised rather than returning a short list, because a silently truncated
    report is worse than a loud failure.
    """


class InputError(JiraError):
    """Local validation failed. ``status`` is always ``None``."""

    def __init__(self, message: str = "", url: str = "") -> None:
        super().__init__(message, status=None, url=url)


class ConfigError(JiraError):
    """Configuration is missing or invalid. ``status`` is always ``None``."""

    def __init__(self, message: str = "", url: str = "") -> None:
        super().__init__(message, status=None, url=url)


__all__ = [
    "AuthError",
    "ConfigError",
    "InputError",
    "JiraError",
    "JiraPermissionError",
    "NotFoundError",
    "PartialResultError",
    "ReadOnlyError",
]
