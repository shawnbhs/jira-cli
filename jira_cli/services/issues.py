"""Issue operations for Jira Software Server 10.3 (REST v2 + Agile 1.0).

Everything in this module speaks the *normalised issue dict* described in the
project contract.  Users are identified by their ``name`` (username), which is
the Server convention -- ``accountId`` is Jira Cloud only.

No function in this module prints; failures are raised as ``jira_cli.errors``
exceptions.  All writes go through the client, which refuses to mutate anything
when the settings say read-only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..durations import to_jira
from ..errors import InputError, JiraError
from ..fields import FieldMap
from ..ids import validate_int_id, validate_issue_key, validate_project_key

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing requests
    from ..client import JiraClient

API = "/rest/api/2"
AGILE = "/rest/agile/1.0"

#: Jira's status category keys mapped onto the three names used in reports.
STATUS_CATEGORIES = {
    "new": "To Do",
    "undefined": "To Do",
    "indeterminate": "In Progress",
    "done": "Done",
}

#: Fallback when a ``statusCategory`` carries an ``id`` but no ``key``
#: (ids observed on Jira Server 10.3: 2 new, 4 indeterminate, 3 done; 1 undefined).
STATUS_CATEGORY_IDS = {1: "undefined", 2: "new", 3: "done", 4: "indeterminate"}

#: Keys accepted by :func:`update_fields`.
UPDATE_KEYS = (
    "summary",
    "description",
    "priority",
    "labels",
    "due_date",
    "original_estimate",
    "remaining_estimate",
    "story_points",
    "components",
    "fix_versions",
)

#: Fields of :func:`update_fields` that carry a list of names.
LIST_UPDATE_KEYS = ("labels", "components", "fix_versions")
#: Longest issue summary Jira accepts.
SUMMARY_MAX_LENGTH = 255
#: Longest description this module will send.
DESCRIPTION_MAX_LENGTH = 32_768
#: Most names one list field (labels, components, fix versions) may carry.
LIST_FIELD_MAX_ITEMS = 100

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Attribute names Jira Server writes into the Sprint ``toString()`` blob.
#: The blob is split only at ``,<known key>=`` boundaries because the free-text
#: values (``name``, ``goal``) are unquoted and may contain commas themselves.
SPRINT_BLOB_KEYS = (
    "id",
    "rapidViewId",
    "state",
    "name",
    "startDate",
    "endDate",
    "completeDate",
    "activatedDate",
    "sequence",
    "goal",
    "autoStartStop",
    "synced",
    "incompleteIssuesDestinationId",
)
_SPRINT_SPLIT_RE = re.compile(r",(?=(?:" + "|".join(SPRINT_BLOB_KEYS) + r")=)")
_JAVA_NULL = {"<null>", "null", ""}
_UNASSIGNED = {"", "none", "null", "unassigned", "-"}


@dataclass
class IssueSpec:
    """Everything needed to create one issue.

    ``parent_key`` turns the issue into a sub-task: Jira then requires both a
    ``parent`` and an issue type whose ``subtask`` flag is true.
    """

    project: str
    summary: str
    issue_type: str = "Task"
    description: str | None = None
    assignee: str | None = None
    priority: str | None = None
    labels: list[str] = field(default_factory=list)
    due_date: str | None = None  # YYYY-MM-DD
    original_estimate: str | None = None  # "3h", "2d"
    epic_key: str | None = None
    parent_key: str | None = None  # parent -> Sub-task
    components: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)
    story_points: float | None = None
    sprint_id: int | None = None
    links: list[tuple[str, str]] = field(default_factory=list)  # (type, other_key)


# --------------------------------------------------------------------------- #
# path-segment validation
# --------------------------------------------------------------------------- #


def _issue_ref(key: Any) -> str:
    """Validate an issue reference before it goes into a URL path.

    Accepts an issue key (``PROJ-123``, upper-cased) or a numeric issue id
    (``10001``), both of which Jira's ``/issue/{issueIdOrKey}`` takes.  Anything
    else -- notably ``PROJ-1/../../project/PROJ``, which urllib3 would
    normalise into a different endpoint -- raises :class:`InputError` before
    any request is made.
    """
    text = str(key).strip() if key is not None else ""
    if text.isdigit():
        return str(validate_int_id(text, what="issue id"))
    return validate_issue_key(key)


# --------------------------------------------------------------------------- #
# small readers
# --------------------------------------------------------------------------- #


def _name_of(value: Any) -> str | None:
    """Return ``value["name"]`` when *value* is a dict, else ``None``."""
    return value.get("name") if isinstance(value, dict) else None


def _user_name(value: Any) -> str | None:
    """Username of a Server user object: ``name``, else the internal ``key``.

    Server has no ``accountId``; ``displayName`` is never an identifier.
    """
    if not isinstance(value, dict):
        return None
    return value.get("name") or value.get("key") or None


def _status_category(status: dict) -> str | None:
    """Map ``status.statusCategory`` to a report name, branching on ``key``.

    ``name`` is localised and therefore ignored; ``id`` is only a fallback.
    """
    category = status.get("statusCategory") if isinstance(status, dict) else None
    if not isinstance(category, dict):
        return None
    key = str(category.get("key") or "").lower()
    if not key:
        key = STATUS_CATEGORY_IDS.get(_int_or_none(category.get("id")) or -1, "")
    return STATUS_CATEGORIES.get(key)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(*candidates: Any) -> int | None:
    """Return the first candidate that is an int-ish value."""
    for candidate in candidates:
        if candidate is None:
            continue
        number = _int_or_none(candidate)
        if number is not None:
            return number
    return None


def _java_value(value: Any) -> str | None:
    """Return ``value`` stripped, or ``None`` for Java's literal ``<null>``."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in _JAVA_NULL else text


def parse_sprint_blob(text: str) -> dict[str, str | None]:
    """Parse one Jira Server Sprint ``toString()`` blob into its attributes.

    Example input (one element of the Sprint custom field)::

        com.atlassian.greenhopper.service.sprint.Sprint@1a2b[id=6,state=ACTIVE,
        name=Sprint, with a comma,startDate=...,completeDate=<null>,...]

    The text between the first ``[`` and the last ``]`` is split only where a
    comma is followed by a *known* attribute name and ``=`` (see
    :data:`SPRINT_BLOB_KEYS`), so commas inside ``name`` or ``goal`` survive.
    Java's literal ``<null>`` becomes ``None``.

    Returns:
        ``{attribute: value-or-None}``; an empty dict when ``text`` has no
        ``[...]`` body.
    """
    if not isinstance(text, str) or "[" not in text or not text.rstrip().endswith("]"):
        return {}
    inner = text[text.index("[") + 1 : text.rstrip().rindex("]")]
    pairs: dict[str, str | None] = {}
    for chunk in _SPRINT_SPLIT_RE.split(inner):
        name, sep, value = chunk.partition("=")
        if sep and name.strip():
            pairs[name.strip()] = _java_value(value)
    return pairs


def _parse_sprint_entry(entry: Any) -> dict | None:
    """Parse one value of the Sprint custom field into a small sprint dict.

    Handles the three shapes seen in the wild: the Server ``toString()`` blob
    (see :func:`parse_sprint_blob`), a Cloud/agile style dict, and a bare name.

    Returns:
        ``{"id", "name", "state", "start", "end", "complete", "goal",
        "board_id"}`` or ``None`` when the entry is not parseable at all.
    """
    if isinstance(entry, dict):
        return {
            "id": _int_or_none(entry.get("id")),
            "name": _java_value(entry.get("name")),
            "state": str(entry.get("state") or "").upper(),
            "start": _java_value(entry.get("startDate")),
            "end": _java_value(entry.get("endDate")),
            "complete": _java_value(entry.get("completeDate")),
            "goal": _java_value(entry.get("goal")),
            "board_id": _int_or_none(entry.get("originBoardId") or entry.get("boardId")),
        }
    if not isinstance(entry, str) or not entry.strip():
        return None
    pairs = parse_sprint_blob(entry)
    if pairs:
        return {
            "id": _int_or_none(pairs.get("id")),
            "name": pairs.get("name"),
            "state": str(pairs.get("state") or "").upper(),
            "start": pairs.get("startDate"),
            "end": pairs.get("endDate"),
            "complete": pairs.get("completeDate"),
            "goal": pairs.get("goal"),
            "board_id": _int_or_none(pairs.get("rapidViewId")),
        }
    if "[" in entry and entry.rstrip().endswith("]"):
        return None  # a blob shape with nothing recognisable inside
    # A bare string: treat it as the sprint name.
    return {
        "id": None,
        "name": entry.strip(),
        "state": "",
        "start": None,
        "end": None,
        "complete": None,
        "goal": None,
        "board_id": None,
    }


def parse_sprint_field(raw_value: Any) -> list[dict]:
    """Parse the whole Sprint custom field value, keeping server order.

    Jira returns a list (an issue carried over keeps every sprint it was in);
    a single non-list value is accepted too. Unparseable entries are dropped.
    """
    if raw_value is None:
        return []
    entries = raw_value if isinstance(raw_value, list) else [raw_value]
    return [p for p in (_parse_sprint_entry(e) for e in entries) if p]


def current_sprint(raw_value: Any) -> dict | None:
    """Pick the issue's current sprint from the Sprint field value.

    Rule: the **last ACTIVE** entry when one exists, otherwise the **last**
    entry. Jira appends sprints in the order the issue joined them, so the last
    element is the current one; an ACTIVE sprint still wins over a FUTURE one
    the issue was already planned into.
    """
    parsed = parse_sprint_field(raw_value)
    if not parsed:
        return None
    active = [p for p in parsed if p["state"] == "ACTIVE"]
    return (active or parsed)[-1]


def _latest_sprint_name(raw_value: Any) -> str | None:
    """Name of :func:`current_sprint`, or ``None``."""
    chosen = current_sprint(raw_value)
    return (chosen or {}).get("name") or None


def _comment_entry(raw: dict, *, include_id: bool) -> dict:
    entry = {
        "author": _name_of(raw.get("author")),
        "created": raw.get("created"),
        "body": raw.get("body"),
    }
    if include_id:
        return {"id": str(raw.get("id")) if raw.get("id") is not None else None, **entry}
    return entry


def _changelog_entries(raw: dict) -> list[dict]:
    histories = ((raw.get("changelog") or {}).get("histories")) or []
    entries = []
    for history in histories:
        entries.append(
            {
                "author": _name_of(history.get("author")),
                "created": history.get("created"),
                "items": [
                    {
                        "field": item.get("field"),
                        "from": item.get("fromString"),
                        "to": item.get("toString"),
                    }
                    for item in (history.get("items") or [])
                ],
            }
        )
    return entries


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def normalize_issue(raw: dict, client: JiraClient, fields: FieldMap | None = None) -> dict:
    """Turn a raw Jira issue into the normalised dict used everywhere.

    Tolerates missing sections: anything Jira did not return becomes ``None``
    (or an empty list for ``labels``).
    """
    fmap = fields if fields is not None else FieldMap(client)
    raw = raw or {}
    data = raw.get("fields") or {}
    key = raw.get("key")

    status = data.get("status") or {}
    # ``timetracking`` is ``{}`` when nothing is tracked and missing entirely
    # when ``fields=`` restricted the response; the top-level seconds fields
    # are the fallback (they can disagree when a field config hides one).
    timetracking = data.get("timetracking")
    if not isinstance(timetracking, dict):
        timetracking = {}

    assignee = data.get("assignee")
    story_points_field = fmap.story_points
    epic_field = fmap.epic_link
    sprint_field = fmap.sprint

    return {
        "key": key,
        "url": client.browse_url(key) if key else None,
        "summary": data.get("summary"),
        "type": _name_of(data.get("issuetype")),
        "status": status.get("name"),
        "status_category": _status_category(status),
        "priority": _name_of(data.get("priority")),
        "assignee": _user_name(assignee),
        "assignee_name": assignee.get("displayName") if isinstance(assignee, dict) else None,
        "reporter": _user_name(data.get("reporter")),
        "project": (data.get("project") or {}).get("key"),
        "created": data.get("created"),
        "updated": data.get("updated"),
        "resolved": data.get("resolutiondate"),
        "due": data.get("duedate"),
        "labels": list(data.get("labels") or []),
        "estimate_seconds": _first_int(
            timetracking.get("originalEstimateSeconds"), data.get("timeoriginalestimate")
        ),
        "remaining_seconds": _first_int(
            timetracking.get("remainingEstimateSeconds"), data.get("timeestimate")
        ),
        "spent_seconds": _first_int(
            timetracking.get("timeSpentSeconds"), data.get("timespent")
        ),
        "story_points": data.get(story_points_field) if story_points_field else None,
        "epic": data.get(epic_field) if epic_field else None,
        "parent": (data.get("parent") or {}).get("key"),
        "sprint": _issue_sprint_name(data, sprint_field),
        "description": data.get("description"),
    }


def _issue_sprint_name(data: dict, sprint_field: str | None) -> str | None:
    """Current sprint name: the Sprint custom field, else agile ``fields.sprint``.

    ``/rest/agile/1.0/sprint/{id}/issue`` adds a proper ``fields.sprint`` object
    next to the Server ``toString()`` blob list; it is used when the custom
    field is unknown or empty.
    """
    if sprint_field:
        name = _latest_sprint_name(data.get(sprint_field))
        if name:
            return name
    agile = data.get("sprint")
    if isinstance(agile, dict):
        return _latest_sprint_name(agile)
    return None


def get_issue(
    client: JiraClient, key: str, *, comments: bool = True, changelog: bool = False
) -> dict:
    """Fetch one issue, normalised, optionally with comments and changelog."""
    key = _issue_ref(key)
    params = {"expand": "changelog"} if changelog else None
    raw = client.get(f"{API}/issue/{key}", params)
    issue = normalize_issue(raw, client)
    if comments:
        inline = ((raw.get("fields") or {}).get("comment") or {}).get("comments")
        if inline is None:
            issue["comments"] = [
                _comment_entry(c, include_id=False) for c in _raw_comments(client, key)
            ]
        else:
            issue["comments"] = [_comment_entry(c, include_id=False) for c in inline]
    if changelog:
        issue["changelog"] = _changelog_entries(raw)
    return issue


def search_issues(client: JiraClient, jql: str, limit: int = 50) -> list[dict]:
    """Run a JQL search and normalise every hit."""
    if not (jql or "").strip():
        raise InputError("a JQL query is required")
    fmap = FieldMap(client)
    return [normalize_issue(raw, client, fmap) for raw in client.search(jql, limit=limit)]


def my_issues(client: JiraClient, include_done: bool = False, limit: int = 200) -> list[dict]:
    """Issues assigned to the authenticated user, newest update first."""
    jql = "assignee = currentUser()"
    if not include_done:
        jql += " AND statusCategory != Done"
    return search_issues(client, jql + " ORDER BY updated DESC", limit=limit)


# --------------------------------------------------------------------------- #
# create / update
# --------------------------------------------------------------------------- #


def _values(payload: Any, *legacy_keys: str) -> list[dict]:
    """Items of a paged ``{"values": [...]}`` payload (or a bare list)."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("values", *legacy_keys):
        items = payload.get(key)
        if isinstance(items, list):
            return [p for p in items if isinstance(p, dict)]
    return []


def createmeta_issue_types(client: JiraClient, project: str) -> list[dict]:
    """Issue types a user may create in ``project`` (Jira 9+/10 createmeta).

    Jira Server 10.3 removed the legacy single-shot
    ``GET /rest/api/2/issue/createmeta?projectKeys=..`` (it answers 404). Its
    replacement is two-hop: this function is hop one,
    ``GET /rest/api/2/issue/createmeta/{project}/issuetypes``; hop two is
    :func:`createmeta_fields`.

    Returns:
        Raw issue-type dicts ``{"id", "name", "subtask", ...}`` in server order.

    Raises:
        InputError: If ``project`` is not a valid project key.
        JiraError: From the client when the endpoint is unavailable.
    """
    project = validate_project_key(project)
    payload = client.get(f"{API}/issue/createmeta/{project}/issuetypes")
    return _values(payload)


def createmeta_fields(client: JiraClient, project: str, issue_type: str) -> list[dict]:
    """Create-screen fields of one issue type (createmeta hop two).

    Args:
        client: Configured Jira client.
        project: Project key.
        issue_type: Issue type **id** or **name** (case-insensitive); a name
            is resolved through :func:`createmeta_issue_types` first.

    Returns:
        ``[{"field_id", "name", "required", "type", "custom"}]``.

    Raises:
        InputError: If ``project`` is not a valid project key, or
            ``issue_type`` names no issue type of the project.
        JiraError: From the client.
    """
    project = validate_project_key(project)
    wanted = str(issue_type or "").strip()
    if not wanted:
        raise InputError("an issue type is required")
    type_id = wanted
    if not wanted.isdigit():
        types = createmeta_issue_types(client, project)
        match = [t for t in types if str(t.get("name") or "").lower() == wanted.lower()]
        if not match:
            names = ", ".join(str(t.get("name")) for t in types) or "none"
            raise InputError(f"unknown issue type {issue_type!r} in {project}; available: {names}")
        type_id = str(match[0].get("id"))
    type_id = str(validate_int_id(type_id, what="issue type id"))
    payload = client.get(f"{API}/issue/createmeta/{project}/issuetypes/{type_id}")
    rows = []
    for item in _values(payload, "fields"):
        schema = item.get("schema") or {}
        rows.append(
            {
                "field_id": item.get("fieldId") or item.get("key"),
                "name": item.get("name"),
                "required": bool(item.get("required")),
                "type": schema.get("type"),
                "custom": schema.get("custom"),
            }
        )
    return rows


def _legacy_createmeta_types(client: JiraClient, project: str) -> list[dict]:
    """Issue types from the pre-Jira-9 single-shot createmeta (404 on 10.3)."""
    meta = client.get(
        f"{API}/issue/createmeta",
        {"projectKeys": project, "expand": "projects.issuetypes"},
    )
    for entry in (meta or {}).get("projects") or []:
        entry_key = str(entry.get("key") or "")
        if not entry_key or entry_key.upper() == project.upper():
            types = entry.get("issuetypes") or []
            if types:
                return types
    return []


def _project_issue_types(client: JiraClient, project: str) -> list[dict]:
    """Issue types for a project; empty list when Jira will not say.

    Sources, first non-empty wins: the Jira 9+/10 createmeta endpoint, the
    legacy createmeta (older servers), then ``/project/{key}`` ``issueTypes``.
    Every source failure is swallowed -- the caller has a sane default.  An
    invalid project key is not a source failure and is raised up front.
    """
    project = validate_project_key(project)
    for source in (createmeta_issue_types, _legacy_createmeta_types):
        try:
            types = source(client, project)
        except JiraError:
            continue
        if types:
            return types
    try:
        detail = client.get(f"{API}/project/{project}")
    except JiraError:
        return []
    return (detail or {}).get("issueTypes") or []


def _subtask_type_name(client: JiraClient, project: str, requested: str) -> str:
    """Resolve an issue type with ``subtask: true`` for a sub-task create.

    Preference order: the requested type when it really is a sub-task type on
    this project, else the project's first sub-task type, else the requested
    name when the caller asked for something other than the ``IssueSpec``
    default, else the Server default name ``"Sub-task"``.  The last two
    branches only run when Jira would not tell us the project's types.
    """
    subtasks = [t for t in _project_issue_types(client, project) if t.get("subtask")]
    wanted = (requested or "").strip().lower()
    for candidate in subtasks:
        if str(candidate.get("name") or "").lower() == wanted:
            return str(candidate["name"])
    if subtasks:
        return str(subtasks[0].get("name") or "Sub-task")
    if wanted and wanted != "task":
        return requested.strip()
    return "Sub-task"


def _validate_labels(labels: list[str]) -> list[str]:
    cleaned = []
    for label in labels:
        text = str(label).strip()
        if not text:
            continue
        if re.search(r"\s", text):
            raise InputError(f"label {text!r} must not contain whitespace")
        cleaned.append(text)
    return cleaned


def name_list(field_name: str, value: Any) -> list[str]:
    """Normalise a list-of-names field to a list of non-empty, stripped strings.

    ``"a, b"`` and ``["a", "b"]`` both become ``["a", "b"]``; ``None`` becomes
    ``[]``. A plain string is split on commas — it is never iterated character
    by character. Any other type is refused.

    Raises:
        InputError: for a non-string item, a value of another type, or more
            than :data:`LIST_FIELD_MAX_ITEMS` names.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)) and all(isinstance(part, str) for part in value):
        parts = list(value)
    else:
        raise InputError(
            f"{field_name} must be a list of strings or a comma-separated string, "
            f"got {type(value).__name__}"
        )
    names = [part.strip() for part in parts if part.strip()]
    if len(names) > LIST_FIELD_MAX_ITEMS:
        raise InputError(f"{field_name} has {len(names)} items; at most {LIST_FIELD_MAX_ITEMS} allowed")
    return names


def normalize_changes(changes: Any) -> dict[str, Any]:
    """Validate and normalise an :func:`update_fields` ``changes`` mapping.

    This is the single place field names are checked and values are put in
    the shape that is sent to Jira, so a preview built from its result shows
    exactly what :func:`update_fields` will send. It is idempotent:
    normalising an already normalised mapping returns an equal mapping.

    Raises:
        InputError: for a non-mapping, an empty mapping, an unknown field, or a
            value of the wrong type or size.
    """
    if not isinstance(changes, Mapping):
        raise InputError("changes must be a mapping of field to new value")
    if not changes:
        raise InputError("no changes given")
    unknown = sorted(str(name) for name in set(changes) - set(UPDATE_KEYS))
    if unknown:
        raise InputError(
            f"unknown field(s): {', '.join(unknown)}; allowed: {', '.join(UPDATE_KEYS)}"
        )
    out: dict[str, Any] = {}
    for name, value in changes.items():
        if name in LIST_UPDATE_KEYS:
            names = name_list(name, value)
            out[name] = _validate_labels(names) if name == "labels" else names
        elif name == "summary":
            text = str(value or "").strip()
            if not text:
                raise InputError("summary must not be empty")
            if len(text) > SUMMARY_MAX_LENGTH:
                raise InputError(f"summary is longer than {SUMMARY_MAX_LENGTH} characters")
            out[name] = text
        elif name == "description":
            if value is not None and not isinstance(value, str):
                raise InputError("description must be a string")
            if value is not None and len(value) > DESCRIPTION_MAX_LENGTH:
                raise InputError(f"description is longer than {DESCRIPTION_MAX_LENGTH} characters")
            out[name] = value
        elif name == "due_date":
            out[name] = _validate_date(value, "due date") if value else None
        else:
            out[name] = value
    return out


def _validate_date(value: str, label: str) -> str:
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise InputError(f"{label} must be YYYY-MM-DD, got {text!r}")
    return text


def build_fields(
    client: JiraClient, spec: IssueSpec, fields: FieldMap | None = None
) -> dict:
    """Build and validate the ``fields`` payload for an issue create.

    Returns the inner ``fields`` object; :func:`create_issue` wraps it in
    ``{"fields": ...}``.  Raises :class:`InputError` for anything this process
    can tell is wrong without talking to Jira.
    """
    fmap = fields if fields is not None else FieldMap(client)
    project = (spec.project or "").strip() or getattr(client.settings, "default_project", None)
    if not project:
        raise InputError("a project key is required")
    project = validate_project_key(project)
    summary = (spec.summary or "").strip()
    if not summary:
        raise InputError("a summary is required")

    payload: dict[str, Any] = {"project": {"key": project}, "summary": summary}

    if spec.parent_key:
        payload["parent"] = {"key": validate_issue_key(spec.parent_key, what="parent key")}
        payload["issuetype"] = {"name": _subtask_type_name(client, project, spec.issue_type)}
    else:
        issue_type = (spec.issue_type or "").strip()
        if not issue_type:
            raise InputError("an issue type is required")
        payload["issuetype"] = {"name": issue_type}

    if spec.description is not None:
        payload["description"] = spec.description
    if spec.assignee is not None:
        payload["assignee"] = {"name": spec.assignee}
    if spec.priority:
        payload["priority"] = {"name": spec.priority}
    if spec.labels:
        payload["labels"] = _validate_labels(name_list("labels", spec.labels))
    if spec.due_date:
        payload["duedate"] = _validate_date(spec.due_date, "due date")
    if spec.original_estimate:
        payload["timetracking"] = {"originalEstimate": to_jira(spec.original_estimate)}
    if spec.components:
        payload["components"] = [
            {"name": name} for name in name_list("components", spec.components)
        ]
    if spec.fix_versions:
        payload["fixVersions"] = [
            {"name": name} for name in name_list("fix_versions", spec.fix_versions)
        ]
    if spec.epic_key:
        epic_field = fmap.epic_link
        if not epic_field:
            raise InputError("this Jira has no 'Epic Link' field; cannot set an epic")
        payload[epic_field] = spec.epic_key
    if spec.story_points is not None:
        points_field = fmap.story_points
        if not points_field:
            raise InputError("this Jira has no 'Story Points' field")
        payload[points_field] = spec.story_points
    return payload


def create_issue(client: JiraClient, spec: IssueSpec) -> dict:
    """Create one issue, then add it to a sprint and create its links.

    Post-create steps are best effort: a sprint or link failure is reported in
    ``warnings`` rather than raised, because the issue itself already exists.
    """
    client.ensure_writable()
    # Validated up front: the sprint id lands in a URL path, and a bad value
    # must fail before the issue is created, not as a post-create warning.
    sprint_id = (
        validate_int_id(spec.sprint_id, what="sprint id") if spec.sprint_id is not None else None
    )
    fmap = FieldMap(client)
    payload = build_fields(client, spec, fmap)
    created = client.post(f"{API}/issue", {"fields": payload}) or {}
    key = created.get("key")
    if not key:
        raise JiraError("Jira did not return a key for the created issue")

    warnings: list[str] = []
    if sprint_id is not None:
        try:
            client.post(f"{AGILE}/sprint/{sprint_id}/issue", {"issues": [key]})
        except JiraError as exc:
            warnings.append(f"could not add {key} to sprint {sprint_id}: {exc}")
    for link_type, other_key in spec.links:
        try:
            link_issues(client, key, other_key, link_type)
        except JiraError as exc:
            warnings.append(f"could not link {key} -> {other_key} ({link_type}): {exc}")
    return {"key": key, "url": client.browse_url(key), "warnings": warnings}


def update_fields(client: JiraClient, key: str, changes: dict) -> dict:
    """Edit an existing issue. ``changes`` uses the contract's field names."""
    client.ensure_writable()
    key = _issue_ref(key)
    # One normaliser shared with the MCP preview: what was previewed is sent.
    changes = normalize_changes(changes)

    payload: dict[str, Any] = {}
    timetracking: dict[str, str] = {}
    fmap: FieldMap | None = None
    for name, value in changes.items():
        if name == "summary":
            payload["summary"] = value
        elif name == "description":
            payload["description"] = value
        elif name == "priority":
            payload["priority"] = {"name": value}
        elif name == "labels":
            payload["labels"] = value
        elif name == "due_date":
            payload["duedate"] = value
        elif name == "original_estimate":
            timetracking["originalEstimate"] = to_jira(value)
        elif name == "remaining_estimate":
            timetracking["remainingEstimate"] = to_jira(value)
        elif name == "story_points":
            if fmap is None:
                fmap = FieldMap(client)
            points_field = fmap.story_points
            if not points_field:
                raise InputError("this Jira has no 'Story Points' field")
            payload[points_field] = value
        elif name == "components":
            payload["components"] = [{"name": n} for n in value]
        elif name == "fix_versions":
            payload["fixVersions"] = [{"name": n} for n in value]
    if timetracking:
        payload["timetracking"] = timetracking

    client.put(f"{API}/issue/{key}", {"fields": payload})
    return {"key": key, "url": client.browse_url(key), "updated": sorted(changes)}


def assign(client: JiraClient, key: str, username: str | None) -> dict:
    """Assign an issue. ``None`` (or ``"none"``/empty) unassigns it."""
    client.ensure_writable()
    key = _issue_ref(key)
    name: str | None = None
    if username is not None and str(username).strip().lower() not in _UNASSIGNED:
        name = str(username).strip()
    client.put(f"{API}/issue/{key}/assignee", {"name": name})
    return {"key": key, "url": client.browse_url(key), "assignee": name}


# --------------------------------------------------------------------------- #
# transitions
# --------------------------------------------------------------------------- #


def list_transitions(client: JiraClient, key: str) -> list[dict]:
    """Transitions currently available on an issue."""
    key = _issue_ref(key)
    data = client.get(f"{API}/issue/{key}/transitions", {"expand": "transitions.fields"}) or {}
    return [
        {
            "id": str(item.get("id")),
            "name": item.get("name"),
            "to": _name_of(item.get("to")),
        }
        for item in data.get("transitions") or []
    ]


def _match_transition(options: list[dict], to: str) -> dict:
    wanted = (to or "").strip().lower()
    if not wanted:
        raise InputError("a transition name, target status, or id is required")
    matches: dict[str, dict] = {}
    for option in options:
        candidates = {
            str(option.get("id") or "").lower(),
            str(option.get("name") or "").lower(),
            str(option.get("to") or "").lower(),
        }
        if wanted in candidates - {""}:
            matches[str(option.get("id"))] = option
    listing = ", ".join(
        f"{o['name']} (id {o['id']} -> {o['to']})" for o in options
    ) or "none"
    if not matches:
        raise InputError(f"no transition matches {to!r}; available: {listing}")
    if len(matches) > 1:
        ambiguous = ", ".join(
            f"{o['name']} (id {o['id']} -> {o['to']})" for o in matches.values()
        )
        raise InputError(f"{to!r} is ambiguous; it matches: {ambiguous}")
    return next(iter(matches.values()))


def transition(client: JiraClient, key: str, to: str, comment: str | None = None) -> dict:
    """Move an issue. ``to`` is a transition name, target status name, or id."""
    client.ensure_writable()
    key = _issue_ref(key)
    chosen = _match_transition(list_transitions(client, key), to)
    payload: dict[str, Any] = {"transition": {"id": chosen["id"]}}
    if comment:
        payload["update"] = {"comment": [{"add": {"body": comment}}]}
    client.post(f"{API}/issue/{key}/transitions", payload)
    return {
        "key": key,
        "url": client.browse_url(key),
        "transition": chosen["name"],
        "status": chosen["to"],
        "comment_added": bool(comment),
    }


# --------------------------------------------------------------------------- #
# comments and links
# --------------------------------------------------------------------------- #


def _raw_comments(client: JiraClient, key: str) -> list[dict]:
    return client.paginate(f"{API}/issue/{key}/comment", items_key="comments")


def add_comment(client: JiraClient, key: str, body: str) -> dict:
    """Add a comment to an issue."""
    client.ensure_writable()
    key = _issue_ref(key)
    if not (body or "").strip():
        raise InputError("comment body must not be empty")
    created = client.post(f"{API}/issue/{key}/comment", {"body": body}) or {}
    entry = _comment_entry(created, include_id=True)
    entry.update({"key": key, "url": client.browse_url(key)})
    return entry


def list_comments(client: JiraClient, key: str) -> list[dict]:
    """All comments on an issue, oldest first (Jira's order)."""
    key = _issue_ref(key)
    return [_comment_entry(c, include_id=True) for c in _raw_comments(client, key)]


def list_link_types(client: JiraClient) -> list[dict]:
    """Issue link types configured on this Jira."""
    data = client.get(f"{API}/issueLinkType") or {}
    return [
        {
            "id": str(item.get("id")) if item.get("id") is not None else None,
            "name": item.get("name"),
            "inward": item.get("inward"),
            "outward": item.get("outward"),
        }
        for item in data.get("issueLinkTypes") or []
    ]


def _resolve_link_type(client: JiraClient, link_type: str) -> str:
    wanted = (link_type or "").strip()
    if not wanted:
        raise InputError("a link type is required")
    try:
        known = list_link_types(client)
    except JiraError:
        return wanted
    names = [str(t["name"]) for t in known if t.get("name")]
    for name in names:
        if name.lower() == wanted.lower():
            return name
    if names:
        raise InputError(
            f"unknown link type {link_type!r}; available: {', '.join(sorted(names))}"
        )
    return wanted


def link_issues(
    client: JiraClient, from_key: str, to_key: str, link_type: str = "Relates"
) -> dict:
    """Link two issues. The outward description reads *from_key -> to_key*."""
    client.ensure_writable()
    if not from_key or not to_key:
        raise InputError("both issue keys are required")
    from_key = _issue_ref(from_key)
    to_key = _issue_ref(to_key)
    if from_key == to_key:
        raise InputError("cannot link an issue to itself")
    resolved = _resolve_link_type(client, link_type)
    client.post(
        f"{API}/issueLink",
        {
            "type": {"name": resolved},
            "inwardIssue": {"key": from_key},
            "outwardIssue": {"key": to_key},
        },
    )
    return {
        "from": from_key,
        "to": to_key,
        "type": resolved,
        "url": client.browse_url(from_key),
    }


def delete_issue(client: JiraClient, key: str) -> dict:
    """Delete an issue. Sub-tasks are not deleted (Jira refuses if any exist)."""
    client.ensure_writable()
    if not (key or "").strip():
        raise InputError("an issue key is required")
    key = _issue_ref(key)
    client.delete(f"{API}/issue/{key}")
    return {"key": key, "deleted": True}
