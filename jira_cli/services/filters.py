"""Saved JQL filters: a local YAML file, plus the server's favourites.

The local store is a single YAML mapping of filter name to ``{"jql", "description"}``.
An older format stored the JQL string directly (``{name: "<jql>"}``); it is still
read so an existing file keeps working, and it is rewritten into the current
shape the first time anything is saved.

The file can disclose which projects and people a user watches, so it is written
0600 inside a 0700 directory, and replaced atomically — a half-written filter
file would lose every other saved filter.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from jira_cli.errors import InputError

#: A filter name must be safe as a YAML key and as a CLI argument.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

#: ``.`` and ``..`` match the pattern but are never acceptable names.
_RESERVED_NAMES = (".", "..")

SERVER_FAVOURITES = "/rest/api/2/filter/favourite"


def list_filters(path: Path) -> dict:
    """Read every saved filter.

    Args:
        path: the filters YAML file. A missing file is not an error.

    Returns:
        ``{name: {"jql": str, "description": str}}``, sorted by name. The
        legacy ``{name: "<jql>"}`` form is normalised into the same shape, with
        an empty description.

    Raises:
        InputError: the file exists but is not a YAML mapping.
    """
    raw = _read_yaml(Path(path))
    result: dict[str, dict] = {}
    for name, value in raw.items():
        entry = _normalize_entry(str(name), value)
        if entry is not None:
            result[str(name)] = entry
    return dict(sorted(result.items()))


def get_filter(path: Path, name: str) -> dict:
    """Return one saved filter.

    Args:
        path: the filters YAML file.
        name: the filter name.

    Returns:
        ``{"name": str, "jql": str, "description": str}``.

    Raises:
        InputError: no filter of that name is saved.
    """
    validated = _validate_name(name)
    entry = list_filters(path).get(validated)
    if entry is None:
        raise InputError(f"no saved filter named {validated!r}")
    return {"name": validated, **entry}


def save_filter(
    path: Path, name: str, jql: str, description: str | None = None
) -> dict:
    """Create or overwrite a saved filter.

    Args:
        path: the filters YAML file; it and its parent are created if needed.
        name: filter name, matching ``^[A-Za-z0-9_.-]{1,64}$``.
        jql: the JQL to store; must be non-blank.
        description: optional free text.

    Returns:
        ``{"name", "jql", "description", "path", "created"}`` — ``created`` is
        ``False`` when an existing filter of that name was replaced.

    Raises:
        InputError: the name or the JQL is invalid.
    """
    validated = _validate_name(name)
    if not isinstance(jql, str) or not jql.strip():
        raise InputError("jql must be a non-empty string")

    target = Path(path)
    existing = list_filters(target)
    created = validated not in existing
    existing[validated] = {
        "jql": jql.strip(),
        "description": (description or "").strip(),
    }
    _write_yaml(target, existing)
    return {
        "name": validated,
        "jql": existing[validated]["jql"],
        "description": existing[validated]["description"],
        "path": str(target),
        "created": created,
    }


def delete_filter(path: Path, name: str) -> bool:
    """Remove a saved filter.

    Args:
        path: the filters YAML file.
        name: the filter name.

    Returns:
        ``True`` when a filter was removed, ``False`` when there was none.

    Raises:
        InputError: the name is not a valid filter name.
    """
    validated = _validate_name(name)
    target = Path(path)
    existing = list_filters(target)
    if validated not in existing:
        return False
    del existing[validated]
    _write_yaml(target, existing)
    return True


def server_filters(client) -> list[dict]:
    """List the authenticated user's favourite filters on the Jira server.

    Args:
        client: a ``JiraClient``.

    Returns:
        ``[{"id", "name", "jql", "owner"}]``; ``owner`` is a username or
        ``None``.
    """
    payload = client.get(SERVER_FAVOURITES)
    entries = payload if isinstance(payload, list) else (payload or {}).get("values") or []
    result = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        owner = raw.get("owner") or {}
        result.append(
            {
                "id": str(raw.get("id")) if raw.get("id") is not None else None,
                "name": raw.get("name"),
                "jql": raw.get("jql"),
                "owner": owner.get("name") or owner.get("displayName"),
            }
        )
    return result


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _validate_name(name: Any) -> str:
    """Return ``name`` if it is an acceptable filter name, else raise."""
    text = "" if name is None else str(name).strip()
    if not NAME_PATTERN.match(text) or text in _RESERVED_NAMES:
        raise InputError(
            "filter name must be 1-64 characters of letters, digits, '_', '.' or '-'"
        )
    return text


def _normalize_entry(name: str, value: Any) -> dict | None:
    """Coerce a stored value into ``{"jql", "description"}``, or drop it."""
    if isinstance(value, str):
        return {"jql": value, "description": ""} if value.strip() else None
    if isinstance(value, dict):
        jql = value.get("jql")
        if not isinstance(jql, str) or not jql.strip():
            raise InputError(f"saved filter {name!r} has no jql")
        return {"jql": jql, "description": str(value.get("description") or "")}
    raise InputError(f"saved filter {name!r} is not a string or a mapping")


def _read_yaml(path: Path) -> dict:
    """Load the filters file, returning ``{}`` when it does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except IsADirectoryError as exc:
        raise InputError(f"{path} is a directory, not a filters file") from exc
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InputError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InputError(f"{path} must contain a mapping of filter name to filter")
    return data


def _write_yaml(path: Path, filters: dict) -> None:
    """Replace the filters file atomically with 0600 inside a 0700 directory."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod(parent, 0o700)

    text = yaml.safe_dump(
        {name: dict(entry) for name, entry in sorted(filters.items())},
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
    )
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _chmod(path: Path, mode: int) -> None:
    """Best-effort permission tightening (a no-op on filesystems without it)."""
    try:
        os.chmod(path, mode)
    except (NotImplementedError, OSError):
        pass
