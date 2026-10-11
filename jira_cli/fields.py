"""Custom-field discovery.

Jira custom field ids (``customfield_10109``) differ per server, so nothing in
this package may hardcode one. :class:`FieldMap` looks a field up by its
**display name** and caches the server's field list for the lifetime of the
object.

``settings.field_overrides`` (from ``JIRA_FIELD_<NAME>=customfield_x``) always
wins, which is the escape hatch for a server where two fields share a name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import JiraError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import JiraClient

FIELD_PATH = "/rest/api/2/field"

SPRINT_NAME = "Sprint"
EPIC_LINK_NAME = "Epic Link"
EPIC_NAME_NAME = "Epic Name"
STORY_POINTS_NAME = "Story Points"


def normalize_key(name: str) -> str:
    """Normalise a display name to the override key shape (``EPIC_LINK``)."""
    return "_".join((name or "").strip().upper().split())


class FieldMap:
    """Lazy display-name -> field-id lookup for one Jira server."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        self._by_name: dict[str, str] | None = None
        raw_overrides: dict[str, str] = dict(
            getattr(getattr(client, "settings", None), "field_overrides", None) or {}
        )
        self._overrides = {normalize_key(k): v for k, v in raw_overrides.items() if v}

    def _load(self) -> dict[str, str]:
        """Fetch ``/rest/api/2/field`` once.

        A failure is cached as "no fields known" rather than raised: a missing
        custom field must degrade a report, not abort one. Callers that need a
        specific field raise their own InputError when :meth:`id` returns None.
        """
        if self._by_name is None:
            try:
                payload: Any = self._client.get(FIELD_PATH) or []
            except JiraError:
                payload = []
            mapping: dict[str, str] = {}
            if isinstance(payload, list):
                for entry in payload:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    field_id = entry.get("id") or entry.get("key")
                    if name and field_id:
                        # First definition wins: Jira lists system fields first.
                        mapping.setdefault(normalize_key(str(name)), str(field_id))
            self._by_name = mapping
        return self._by_name

    def id(self, name: str) -> str | None:
        """Return the field id for display name ``name``, case-insensitively."""
        key = normalize_key(name)
        if not key:
            return None
        if key in self._overrides:
            return self._overrides[key]
        return self._load().get(key)

    @property
    def sprint(self) -> str | None:
        """Id of the ``Sprint`` field, or ``None`` when this Jira has none."""
        return self.id(SPRINT_NAME)

    @property
    def epic_link(self) -> str | None:
        """Id of the ``Epic Link`` field, or ``None``."""
        return self.id(EPIC_LINK_NAME)

    @property
    def epic_name(self) -> str | None:
        """Id of the ``Epic Name`` field, or ``None``."""
        return self.id(EPIC_NAME_NAME)

    @property
    def story_points(self) -> str | None:
        """Id of the ``Story Points`` field, or ``None``."""
        return self.id(STORY_POINTS_NAME)

    def __repr__(self) -> str:
        known = "unloaded" if self._by_name is None else f"{len(self._by_name)} fields"
        return f"FieldMap({known}, {len(self._overrides)} override(s))"


__all__ = ["FIELD_PATH", "FieldMap", "normalize_key"]
