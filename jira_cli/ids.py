"""Validation of identifiers that end up inside Jira REST URL paths.

Every issue key, project key and numeric id that a caller (CLI user, MCP
agent, plan file) supplies must pass through one of these helpers before it is
interpolated into a URL path. This blocks path traversal such as
``PROJ-1/../../project/PROJ`` turning one endpoint into another.
"""

from __future__ import annotations

import re

from .errors import InputError

__all__ = [
    "ISSUE_KEY_RE",
    "PROJECT_KEY_RE",
    "validate_int_id",
    "validate_issue_key",
    "validate_project_key",
]

# ``[0-9]`` and re.ASCII: ``\d`` alone would also accept non-ASCII digits.
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$", re.ASCII)
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$", re.ASCII)


def validate_issue_key(value: object, *, what: str = "issue key") -> str:
    """Return ``value`` normalised to upper case if it is a valid issue key.

    Raises:
        InputError: when the value is not shaped like ``PROJ-123``.
    """
    text = str(value).strip().upper() if value is not None else ""
    if not ISSUE_KEY_RE.match(text):
        raise InputError(f"invalid {what} {value!r}: expected something like PROJ-123")
    return text


def validate_project_key(value: object, *, what: str = "project key") -> str:
    """Return ``value`` normalised to upper case if it is a valid project key.

    Raises:
        InputError: when the value is not shaped like ``PROJ``.
    """
    text = str(value).strip().upper() if value is not None else ""
    if not PROJECT_KEY_RE.match(text):
        raise InputError(f"invalid {what} {value!r}: expected letters/digits like PROJ")
    return text


def validate_int_id(value: object, *, what: str = "id") -> int:
    """Return ``value`` as a positive int (board, sprint, filter, worklog ids).

    Raises:
        InputError: when the value is not a positive integer.
    """
    if isinstance(value, bool):
        raise InputError(f"invalid {what} {value!r}: expected a positive integer")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise InputError(f"invalid {what} {value!r}: expected a positive integer") from None
    if number <= 0 or str(value).strip() != str(number):
        raise InputError(f"invalid {what} {value!r}: expected a positive integer")
    return number
