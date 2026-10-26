"""Plan workflow: draft a task list together, validate it, preview it, then bulk-create.

The plan is a plain YAML (or CSV) document a human and an AI agent can edit together.
Nothing is written to Jira until :func:`apply_plan` runs with ``dry_run=False``, and
every successful creation is recorded in a small state file immediately, so an
interrupted run can be resumed by simply running the same command again.

Plan shape (see :func:`new_plan_template` for the commented version)::

    defaults:
      project: PROJ
      board: 12
      sprint: active
      issue_type: Task
    issues:
      - ref: t1
        summary: "Design the import pipeline"
        assignee: alice
        estimate: 4h          # duration, "ask" (human must fill it) or "none"

Conventions used throughout this module:

* ``ref`` is a plan-local identifier. ``parent`` and ``links[].to`` may name either a
  ``ref`` (resolved to the key created for it) or an existing Jira key such as ``PROJ-12``.
* ``estimate: ask`` is a deliberate placeholder: validation reports it as an error so a
  plan can never be applied while an estimate is still unanswered.
* Core modules and sibling services are imported lazily inside functions. That keeps this
  module importable on its own and lets tests monkeypatch the service functions.
"""

from __future__ import annotations

import csv
import importlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from jira_cli.ids import validate_issue_key

#: An existing Jira issue key, e.g. ``PROJ-12``.

#: Estimate placeholder meaning "a human must fill this in before apply".
ASK = "ask"
#: Estimate placeholder meaning "deliberately no estimate".
NO_ESTIMATE = "none"

#: Who produced an estimate. ``agent`` marks a suggestion that still wants a human
#: glance; it is a warning, never an error, so an agreed plan can still be applied.
ESTIMATE_BY_AGENT = "agent"
ESTIMATE_BY_HUMAN = "human"
ESTIMATE_BY_VALUES = (ESTIMATE_BY_AGENT, ESTIMATE_BY_HUMAN)

_TOP_LEVEL_KEYS = frozenset(
    {"defaults", "issues", "name", "description", "version", "_source"}
)
_DEFAULT_KEYS = frozenset(
    {
        "project",
        "board",
        "sprint",
        "issue_type",
        "type",
        "priority",
        "labels",
        "assignee",
        "epic",
        "estimate",
        "estimate_by",
        "due",
        "components",
        "fix_versions",
    }
)
_ISSUE_KEYS = frozenset(
    {
        "ref",
        "summary",
        "description",
        "assignee",
        "estimate",
        "estimate_by",
        "due",
        "type",
        "issue_type",
        "epic",
        "parent",
        "links",
        "labels",
        "story_points",
        "project",
        "priority",
        "sprint",
        "board",
        "components",
        "fix_versions",
    }
)

#: Accepted CSV header, in the documented order. ``labels`` is semicolon separated.
#: ``estimate_by`` is accepted as an optional extra column.
CSV_COLUMNS = (
    "ref",
    "summary",
    "description",
    "assignee",
    "estimate",
    "due",
    "type",
    "epic",
    "parent",
    "labels",
    "story_points",
    "project",
    "estimate_by",
)

PREVIEW_COLUMNS = (
    "ref",
    "project",
    "type",
    "summary",
    "assignee",
    "estimate",
    "due",
    "parent",
    "sprint",
)


# --------------------------------------------------------------------------- #
# lazy core access
# --------------------------------------------------------------------------- #


def _mod(name: str):
    """Import ``jira_cli.<name>`` lazily (see module docstring for why)."""
    return importlib.import_module(f"jira_cli.{name}")


def _errs():
    return _mod("errors")


def _input_error(message: str) -> Exception:
    return _errs().InputError(message)


def _clock_rates(settings: Any) -> tuple[float, float]:
    """Return ``(hours_per_day, days_per_week)`` from settings, with contract defaults."""
    hours = getattr(settings, "hours_per_day", None) or 8
    days = getattr(settings, "days_per_week", None) or 5
    return float(hours), float(days)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def _require_mapping(value: Any, what: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _input_error(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _check_str_list(value: Any, ref: str, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise _input_error(
            f"issue {ref!r}: field {field!r} must be a list of strings"
        )


def _is_blank(value: Any) -> bool:
    """True for ``None``, an empty string, or a whitespace-only string."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _check_issue_schema(issue: Any, position: int) -> None:
    """Validate one raw issue entry; messages always name the ref and the field."""
    if not isinstance(issue, dict):
        raise _input_error(
            f"issues[{position}] must be a mapping, got {type(issue).__name__}"
        )
    ref = issue.get("ref")
    label = str(ref) if ref not in (None, "") else f"issues[{position}]"
    if ref is not None and not isinstance(ref, (str, int)):
        raise _input_error(f"issue {label!r}: field 'ref' must be a string")

    unknown = sorted(set(issue) - _ISSUE_KEYS)
    if unknown:
        raise _input_error(
            f"issue {label!r}: unknown field(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(sorted(_ISSUE_KEYS))}"
        )

    for field in ("summary", "description", "assignee", "epic", "priority", "project"):
        value = issue.get(field)
        if value is not None and not isinstance(value, str):
            raise _input_error(f"issue {label!r}: field {field!r} must be a string")

    for field in ("labels", "components", "fix_versions"):
        if field in issue and issue[field] is not None:
            _check_str_list(issue[field], label, field)

    if issue.get("estimate") is not None and not isinstance(issue["estimate"], str):
        raise _input_error(
            f"issue {label!r}: field 'estimate' must be a string such as '4h', "
            f"'ask' or 'none'"
        )

    estimate_by = issue.get("estimate_by")
    if estimate_by is not None:
        if not isinstance(estimate_by, str):
            raise _input_error(
                f"issue {label!r}: field 'estimate_by' must be a string "
                f"({' or '.join(ESTIMATE_BY_VALUES)})"
            )
        if estimate_by.strip().lower() not in ESTIMATE_BY_VALUES:
            raise _input_error(
                f"issue {label!r}: field 'estimate_by' must be one of "
                f"{', '.join(ESTIMATE_BY_VALUES)}, got {estimate_by!r}"
            )

    if issue.get("story_points") is not None and not isinstance(
        issue["story_points"], (int, float)
    ):
        raise _input_error(f"issue {label!r}: field 'story_points' must be a number")

    if issue.get("parent") is not None and not isinstance(issue["parent"], (str, int)):
        raise _input_error(f"issue {label!r}: field 'parent' must be a ref or issue key")

    links = issue.get("links")
    if links is not None:
        if not isinstance(links, list):
            raise _input_error(
                f"issue {label!r}: field 'links' must be a list of "
                f"{{type: <name>, to: <ref or key>}} mappings"
            )
        for index, link in enumerate(links):
            if not isinstance(link, dict):
                raise _input_error(
                    f"issue {label!r}: field 'links[{index}]' must be a mapping "
                    f"with 'type' and 'to'"
                )
            extra = sorted(set(link) - {"type", "to"})
            if extra:
                raise _input_error(
                    f"issue {label!r}: field 'links[{index}]' has unknown "
                    f"key(s) {', '.join(extra)}"
                )
            for key in ("type", "to"):
                if not isinstance(link.get(key), (str, int)) or link.get(key) in (
                    None,
                    "",
                ):
                    raise _input_error(
                        f"issue {label!r}: field 'links[{index}].{key}' is required "
                        f"and must be a string"
                    )


def _check_plan_schema(plan: Any) -> dict:
    """Validate the overall plan document and return it unchanged."""
    if not isinstance(plan, dict):
        raise _input_error(
            f"plan must be a mapping with an 'issues' list, got {type(plan).__name__}"
        )
    unknown = sorted(set(plan) - _TOP_LEVEL_KEYS)
    if unknown:
        raise _input_error(
            f"plan: unknown top-level key(s) {', '.join(unknown)}; "
            f"allowed: defaults, issues, name, description, version"
        )

    defaults = _require_mapping(plan.get("defaults"), "plan: 'defaults'")
    unknown_defaults = sorted(set(defaults) - _DEFAULT_KEYS)
    if unknown_defaults:
        raise _input_error(
            f"plan: unknown key(s) in 'defaults': {', '.join(unknown_defaults)}; "
            f"allowed: {', '.join(sorted(_DEFAULT_KEYS))}"
        )
    if defaults.get("labels") is not None:
        _check_str_list(defaults["labels"], "defaults", "labels")
    default_estimate_by = defaults.get("estimate_by")
    if default_estimate_by is not None and (
        not isinstance(default_estimate_by, str)
        or default_estimate_by.strip().lower() not in ESTIMATE_BY_VALUES
    ):
        raise _input_error(
            f"plan: field 'defaults.estimate_by' must be one of "
            f"{', '.join(ESTIMATE_BY_VALUES)}, got {default_estimate_by!r}"
        )

    issues = plan.get("issues")
    if issues is None:
        raise _input_error("plan: 'issues' is required and must be a list")
    if not isinstance(issues, list):
        raise _input_error(
            f"plan: 'issues' must be a list, got {type(issues).__name__}"
        )
    for position, issue in enumerate(issues):
        _check_issue_schema(issue, position)
    return plan


def load_plan(path: Path) -> dict:
    """Load and schema-check a YAML plan file.

    Raises ``errors.InputError`` with a message naming the offending ref and field.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _input_error(f"plan file not found: {path}") from exc
    except OSError as exc:
        raise _input_error(f"cannot read plan file {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _input_error(f"plan file {path} is not valid YAML: {exc}") from exc

    plan = _check_plan_schema(raw)
    plan["_source"] = str(path)
    return plan


def plan_from_csv(path: Path) -> dict:
    """Load a plan from a CSV file with the columns documented in :data:`CSV_COLUMNS`.

    ``labels`` is semicolon separated. Empty cells are omitted entirely so plan
    ``defaults`` still apply to them.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise _input_error(f"plan file not found: {path}") from exc
    except OSError as exc:
        raise _input_error(f"cannot read plan file {path}: {exc}") from exc
    return plan_from_csv_text(text, source=path)


def plan_from_csv_text(text: str, *, source: Path | str) -> dict:
    """Build a plan from CSV ``text`` already read from ``source``.

    Lets a caller that must bound or vet the read itself (the MCP server) reuse
    the CSV mapping. ``source`` is recorded as ``_source`` and named in errors.
    """
    path = source
    text = text.removeprefix("\ufeff")
    reader = csv.DictReader(text.splitlines())
    header = [(name or "").strip() for name in (reader.fieldnames or [])]
    if not header:
        raise _input_error(f"CSV plan {path} has no header row")
    unknown = sorted({name for name in header if name and name not in CSV_COLUMNS})
    if unknown:
        raise _input_error(
            f"CSV plan {path}: unknown column(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(CSV_COLUMNS)}"
        )
    if "summary" not in header:
        raise _input_error(f"CSV plan {path}: a 'summary' column is required")

    issues: list[dict] = []
    for position, row in enumerate(reader):
        issue = _issue_from_csv_row(row, position)
        if issue is not None:
            issues.append(issue)

    plan = {"defaults": {}, "issues": issues, "_source": str(path)}
    return _check_plan_schema(plan)


def _issue_from_csv_row(row: dict, position: int) -> dict | None:
    """Convert one CSV row to an issue mapping, or ``None`` for a blank row."""
    cells = {
        key.strip(): (value or "").strip()
        for key, value in row.items()
        if isinstance(key, str)
    }
    if not any(cells.values()):
        return None

    issue: dict[str, Any] = {}
    for field in ("ref", "summary", "description", "assignee", "estimate", "due",
                  "type", "epic", "parent", "project", "estimate_by"):
        if cells.get(field):
            issue[field] = cells[field]

    if cells.get("labels"):
        issue["labels"] = [
            part.strip() for part in cells["labels"].split(";") if part.strip()
        ]

    points = cells.get("story_points")
    if points:
        try:
            issue["story_points"] = float(points)
        except ValueError as exc:
            label = issue.get("ref") or f"row {position + 2}"
            raise _input_error(
                f"issue {label!r}: field 'story_points' must be a number, "
                f"got {points!r}"
            ) from exc
    return issue


def new_plan_template() -> str:
    """Return a heavily commented YAML template explaining every field."""
    return '''\
# jira-cli plan file
# ------------------
# A plan is a draft task list. Nothing reaches Jira until you run:
#     jira plan apply <this file>
# Workflow:
#     jira plan validate <file>   # check refs, users, projects, estimates
#     jira plan preview  <file>   # table + total estimate per person
#     jira plan apply    <file> --dry-run
#     jira plan apply    <file>
# Applying writes <file-stem>.applied.json next to the plan. It maps each ref to the
# issue key that was created, so re-running the command skips what already exists.

# "defaults" are applied to every issue that does not set the field itself.
defaults:
  project: PROJ          # Jira project key (required, here or per issue)
  board: 12              # board id; only needed when "sprint: active" is used
  sprint: active         # "active" = the board's current active sprint,
                         #   or a numeric sprint id, or remove this line for backlog
  issue_type: Task       # default issue type name as it exists in the project
  priority: Medium       # default priority name
  labels: [ai]           # labels added to every issue (merged with per-issue labels)
  estimate_by: human     # who owns estimates by default: "human" or "agent"

# How estimating is meant to work:
#   1. the agent drafts the task list and leaves "estimate: ask" everywhere;
#   2. you answer per task with a duration, with "none", or by telling the agent
#      to estimate it, in which case the agent writes its guess AND
#      "estimate_by: agent" so the suggestion is flagged for your review;
#   3. validation fails while any estimate is still "ask", and warns for every
#      "estimate_by: agent" row, so nothing is created before you agreed.

issues:
  - ref: t1              # plan-local id, unique in this file. Referred to by
                         #   "parent" and "links.to" below. Never sent to Jira.
    summary: "Design the import pipeline"   # required, one line
    description: |        # optional, free text (Jira wiki markup)
      Decide the file format and the failure handling.
    assignee: alice       # Jira username (NOT an e-mail address). Omit to leave unassigned.
    estimate: 4h          # Original estimate. Three forms are allowed:
                          #   a duration -> "30m", "4h", "2d", "1w 2d 3h"
                          #   "ask"      -> a human must fill this in; validation
                          #                 FAILS while any estimate is still "ask",
                          #                 so a half-finished plan cannot be applied
                          #   "none"     -> deliberately no estimate
    estimate_by: human    # "human" (you set it) or "agent" (the agent suggested
                          #   it and validation will warn until you confirm)
    due: 2026-10-20       # YYYY-MM-DD, or a relative form: today, 3d, 2w, 1m
    type: Story           # overrides defaults.issue_type for this issue
    priority: High        # overrides defaults.priority
    labels: [backend]     # merged with defaults.labels
    story_points: 3       # number, written to the Story Points field
    epic: PROJ-12         # existing epic key to link this issue to

  - ref: t2
    summary: "Implement the CSV reader"
    description: "Stream rows, reject unknown columns."
    assignee: bob
    estimate: ask         # <- fill this in (or set "none") before applying
    parent: t1            # a ref from this plan, or an existing key like PROJ-12.
                          #   Setting "parent" creates this issue as a sub-task.
    links:                # issue links, created after all issues exist
      - type: Blocks      # link type name, e.g. Blocks, Relates, Duplicate
        to: t1            # a ref in this plan, or an existing issue key
'''


# --------------------------------------------------------------------------- #
# effective issues (defaults merged)
# --------------------------------------------------------------------------- #


_MISSING = object()


def _pick(issue: dict, defaults: dict, *names: str) -> Any:
    """First value present (not ``None``) for ``names``, issue before defaults."""
    for source in (issue, defaults):
        for name in names:
            value = source.get(name, _MISSING)
            if value is not _MISSING and value is not None:
                return value
    return None


def _merge_labels(issue: dict, defaults: dict) -> list[str]:
    merged: list[str] = []
    for label in list(defaults.get("labels") or []) + list(issue.get("labels") or []):
        if label not in merged:
            merged.append(label)
    return merged


def effective_issues(plan: dict) -> list[dict]:
    """Return one normalised dict per planned issue with ``defaults`` merged in.

    Refs that are absent in the source are given a stable ``auto<N>`` placeholder so
    every message and every state entry can name an issue.
    """
    defaults = _require_mapping(plan.get("defaults"), "plan: 'defaults'")
    issues = plan.get("issues") or []
    out: list[dict] = []
    for position, raw in enumerate(issues):
        raw = raw if isinstance(raw, dict) else {}
        ref = raw.get("ref")
        ref = str(ref) if ref not in (None, "") else f"auto{position + 1}"
        sprint = _pick(raw, defaults, "sprint")
        out.append(
            {
                "ref": ref,
                "position": position,
                "explicit_ref": raw.get("ref") not in (None, ""),
                "summary": raw.get("summary"),
                "description": raw.get("description"),
                "project": _pick(raw, defaults, "project"),
                "type": _pick(raw, defaults, "type", "issue_type") or "Task",
                "assignee": _pick(raw, defaults, "assignee"),
                "priority": _pick(raw, defaults, "priority"),
                "estimate": _pick(raw, defaults, "estimate"),
                "estimate_by": _pick(raw, defaults, "estimate_by"),
                "due": _pick(raw, defaults, "due"),
                "epic": _pick(raw, defaults, "epic"),
                "parent": raw.get("parent"),
                "labels": _merge_labels(raw, defaults),
                "components": list(_pick(raw, defaults, "components") or []),
                "fix_versions": list(_pick(raw, defaults, "fix_versions") or []),
                "story_points": raw.get("story_points"),
                "sprint": sprint,
                "board": _pick(raw, defaults, "board"),
                "links": list(raw.get("links") or []),
            }
        )
    return out


def _issue_key_or_none(value: Any) -> str | None:
    """The canonical issue key ``value`` names, or ``None`` when it is not one.

    Every key a plan sends to Jira (parent, epic, link target) passes through
    :func:`jira_cli.ids.validate_issue_key`, so a crafted value such as
    ``PROJ-1/../../project/PROJ`` can never reach a URL path.
    """
    if not isinstance(value, str):
        return None
    try:
        key = validate_issue_key(value)
    except Exception:  # noqa: BLE001 - InputError: simply "not a key"
        return None
    # Case-sensitive on purpose: a lower-case ``t-1`` stays a plan ref.
    return key if key == value.strip() else None


def _is_key(value: Any) -> bool:
    return _issue_key_or_none(value) is not None


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


class _Lookups:
    """Per-project / per-board Jira lookups, cached for one validation pass.

    Sibling services are imported here (not at module import time) so this module
    does not depend on their import order and tests can monkeypatch them.
    ``client=None`` disables every server lookup, so a plan can be checked locally
    with no configured connection.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._projects: dict[str, bool] = {}
        self._types: dict[str, list[str]] = {}
        self._assignable: dict[str, list[str]] = {}
        self._active_sprint: dict[int, dict | None] = {}
        self._board_types: dict[int, str | None] = {}

    @property
    def offline(self) -> bool:
        return self._client is None

    def project_exists(self, key: str) -> bool:
        if key not in self._projects:
            errs = _errs()
            try:
                self._client.get(f"/rest/api/2/project/{key}")
                self._projects[key] = True
            except errs.NotFoundError:
                self._projects[key] = False
        return self._projects[key]

    def issue_types(self, project: str) -> list[str]:
        if project not in self._types:
            projects = importlib.import_module("jira_cli.services.projects")
            self._types[project] = list(
                projects.list_issue_types(self._client, project) or []
            )
        return self._types[project]

    def assignable(self, project: str) -> list[str]:
        if project not in self._assignable:
            people = importlib.import_module("jira_cli.services.people")
            users = people.assignable_users(self._client, project) or []
            self._assignable[project] = [
                str(user.get("username")) for user in users if user.get("username")
            ]
        return self._assignable[project]

    def active_sprint(self, board_id: int) -> dict | None:
        if board_id not in self._active_sprint:
            boards = importlib.import_module("jira_cli.services.boards")
            self._active_sprint[board_id] = boards.active_sprint(self._client, board_id)
        return self._active_sprint[board_id]

    def board_type(self, board_id: int) -> str | None:
        """Return the board's lower-cased type (``scrum``/``kanban``), or ``None``.

        ``None`` means the type could not be determined; only ``AuthError``
        propagates, any other Jira failure is treated as "unknown".
        """
        if board_id not in self._board_types:
            errs = _errs()
            boards = importlib.import_module("jira_cli.services.boards")
            try:
                raw_type = (boards.get_board(self._client, board_id) or {}).get("type")
            except errs.AuthError:
                raise
            except errs.JiraError:
                raw_type = None
            self._board_types[board_id] = str(raw_type).strip().lower() if raw_type else None
        return self._board_types[board_id]


def _add(bucket: list[dict], ref: str | None, field: str, message: str) -> None:
    bucket.append({"ref": ref, "field": field, "message": message})


def _find_parent_cycles(issues: list[dict], known_refs: set[str]) -> list[list[str]]:
    """Return every multi-issue parent cycle, expressed in plan refs.

    Self-parenting (``parent`` == own ``ref``) is deliberately excluded: it is
    reported once, more precisely, by :func:`_validate_refs`.
    """
    parent_of = {}
    for issue in issues:
        parent = issue.get("parent")
        if isinstance(parent, (str, int)):
            parent = str(parent)
            if parent in known_refs and parent != issue["ref"]:
                parent_of[issue["ref"]] = parent

    cycles: list[list[str]] = []
    seen_cycle: set[frozenset[str]] = set()
    settled: set[str] = set()
    for start in parent_of:
        if start in settled:
            continue
        path: list[str] = []
        node: str | None = start
        while node is not None and node not in path and node not in settled:
            path.append(node)
            node = parent_of.get(node)
        if node is not None and node in path:
            cycle = path[path.index(node):]
            marker = frozenset(cycle)
            if marker not in seen_cycle:
                seen_cycle.add(marker)
                cycles.append(cycle)
        settled.update(path)
    return cycles


def _validate_estimate(
    issue: dict, settings: Any, errors: list[dict], warnings: list[dict]
) -> None:
    """Check one issue's estimate; 'ask' is an error, an agent guess is a warning."""
    estimate = issue["estimate"]
    estimate_by = issue.get("estimate_by")
    if isinstance(estimate_by, str) and estimate_by.strip().lower() == ESTIMATE_BY_AGENT:
        _add(
            warnings,
            issue["ref"],
            "estimate_by",
            f"estimate {str(estimate).strip()!r} was suggested by the agent "
            f"(estimate_by: agent) - confirm it or set estimate_by: human",
        )
    if _is_blank(estimate):
        return
    text = str(estimate).strip()
    if text.lower() == NO_ESTIMATE:
        return
    if text.lower() == ASK:
        _add(
            errors,
            issue["ref"],
            "estimate",
            "estimate is still 'ask': a human must set a duration (e.g. 4h) "
            "or 'none' before this plan can be applied",
        )
        return
    durations = _mod("durations")
    hours, days = _clock_rates(settings)
    try:
        durations.parse_duration(text, hours, days)
    except Exception as exc:  # noqa: BLE001 - InputError, surfaced as a plan error
        _add(errors, issue["ref"], "estimate", f"invalid duration {text!r}: {exc}")


def _validate_due(issue: dict, errors: list[dict]) -> None:
    due = issue["due"]
    if _is_blank(due):
        return
    durations = _mod("durations")
    try:
        durations.resolve_date(str(due).strip())
    except Exception as exc:  # noqa: BLE001 - InputError, surfaced as a plan error
        _add(errors, issue["ref"], "due", f"invalid due date {due!r}: {exc}")


def _validate_refs(
    issues: list[dict], known_refs: set[str], errors: list[dict]
) -> None:
    seen: set[str] = set()
    for issue in issues:
        ref = issue["ref"]
        if issue["explicit_ref"]:
            if ref in seen:
                _add(errors, ref, "ref", f"duplicate ref {ref!r}")
            seen.add(ref)

        if _is_blank(issue["summary"]):
            _add(errors, ref, "summary", "summary is required and cannot be empty")

        parent = issue.get("parent")
        if parent not in (None, ""):
            parent = str(parent)
            if parent == ref:
                _add(errors, ref, "parent", f"issue {ref!r} cannot be its own parent")
            elif parent not in known_refs and not _is_key(parent):
                _add(
                    errors,
                    ref,
                    "parent",
                    f"unknown parent {parent!r}: not a ref in this plan and not an "
                    f"issue key like PROJ-12",
                )

        for index, link in enumerate(issue["links"]):
            target = str(link.get("to"))
            if target == ref:
                _add(errors, ref, f"links[{index}].to", "an issue cannot link to itself")
            elif target not in known_refs and not _is_key(target):
                _add(
                    errors,
                    ref,
                    f"links[{index}].to",
                    f"unknown link target {target!r}: not a ref in this plan and not "
                    f"an issue key like PROJ-12",
                )

        epic = issue.get("epic")
        if not _is_blank(epic) and not _is_key(epic):
            _add(
                errors,
                ref,
                "epic",
                f"invalid epic {epic!r}: expected an existing issue key like PROJ-12",
            )

    for cycle in _find_parent_cycles(issues, known_refs):
        chain = " -> ".join(cycle + [cycle[0]])
        _add(errors, cycle[0], "parent", f"parent cycle: {chain}")


def _validate_against_jira(
    lookups: _Lookups, issue: dict, errors: list[dict], warnings: list[dict]
) -> None:
    """Project / issue type / assignee / sprint checks that need the server."""
    errs = _errs()
    ref = issue["ref"]
    project = issue["project"]

    if not project:
        _add(
            errors,
            ref,
            "project",
            "project is required (set it on the issue or in defaults.project)",
        )
        project_ok = False
    elif lookups.offline:
        project_ok = False
    else:
        try:
            project_ok = lookups.project_exists(str(project))
        except errs.AuthError:
            raise
        except errs.JiraError as exc:
            _add(warnings, ref, "project", f"could not verify project {project}: {exc}")
            project_ok = False
        else:
            if not project_ok:
                _add(errors, ref, "project", f"unknown project {project!r}")

    if project_ok:
        try:
            types = lookups.issue_types(str(project))
        except errs.AuthError:
            raise
        except errs.JiraError as exc:
            _add(warnings, ref, "type", f"could not list issue types: {exc}")
        else:
            wanted = str(issue["type"]).strip().lower()
            if types and wanted not in {t.strip().lower() for t in types}:
                _add(
                    errors,
                    ref,
                    "type",
                    f"issue type {issue['type']!r} does not exist in project "
                    f"{project}; available: {', '.join(types)}",
                )

    if issue["assignee"]:
        if project_ok:
            try:
                usernames = lookups.assignable(str(project))
            except errs.AuthError:
                raise
            except errs.JiraError as exc:
                _add(
                    warnings,
                    ref,
                    "assignee",
                    f"could not check assignable users: {exc}",
                )
            else:
                if str(issue["assignee"]) not in usernames:
                    _add(
                        errors,
                        ref,
                        "assignee",
                        f"user {issue['assignee']!r} is not assignable in project "
                        f"{project}",
                    )
    else:
        _add(warnings, ref, "assignee", "no assignee set")

    if _is_blank(issue["description"]):
        _add(warnings, ref, "description", "no description set")

    _validate_sprint(lookups, issue, errors, warnings)


def _validate_sprint(
    lookups: _Lookups, issue: dict, errors: list[dict], warnings: list[dict]
) -> None:
    sprint = issue["sprint"]
    if _is_blank(sprint):
        return
    errs = _errs()
    ref = issue["ref"]
    text = str(sprint).strip()

    if text.lower() != "active":
        try:
            int(text)
        except ValueError:
            _add(
                errors,
                ref,
                "sprint",
                f"sprint must be 'active' or a numeric sprint id, got {sprint!r}",
            )
        return

    board = issue["board"]
    if board is None:
        _add(
            errors,
            ref,
            "board",
            "sprint 'active' needs a board id (set 'board' on the issue or in "
            "defaults.board)",
        )
        return
    try:
        board_id = int(board)
    except (TypeError, ValueError):
        _add(errors, ref, "board", f"board must be a numeric id, got {board!r}")
        return
    if lookups.offline:
        return

    try:
        active = lookups.active_sprint(board_id)
    except errs.AuthError:
        raise
    except errs.JiraError as exc:
        if lookups.board_type(board_id) == "kanban":
            _add(errors, ref, "sprint", _kanban_sprint_message(board_id))
        else:
            _add(warnings, ref, "sprint", f"could not read the active sprint: {exc}")
        return
    if not active:
        if lookups.board_type(board_id) == "kanban":
            _add(errors, ref, "sprint", _kanban_sprint_message(board_id))
            return
        _add(
            errors,
            ref,
            "sprint",
            f"board {board_id} has no active sprint; start one, use a numeric sprint "
            f"id, or remove 'sprint' to create into the backlog",
        )


def _kanban_sprint_message(board_id: int) -> str:
    return (
        f"board {board_id} is kanban and has no sprints; remove 'sprint' or use a "
        f"scrum board"
    )


def validate_plan(client: Any, plan: dict) -> dict:
    """Check a plan locally and against Jira.

    Returns ``{"ok": bool, "errors": [{"ref","field","message"}], "warnings": [...]}``.
    ``AuthError`` propagates (the plan cannot be validated at all without access);
    other server failures become warnings so local problems are still reported.
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    try:
        _check_plan_schema(plan)
    except Exception as exc:  # noqa: BLE001 - report instead of raising
        _add(errors, None, "plan", str(exc))
        return {"ok": False, "errors": errors, "warnings": warnings}

    issues = effective_issues(plan)
    if not issues:
        _add(errors, None, "issues", "plan contains no issues")
        return {"ok": False, "errors": errors, "warnings": warnings}

    known_refs = {issue["ref"] for issue in issues}
    _validate_refs(issues, known_refs, errors)

    settings = getattr(client, "settings", None)
    lookups = _Lookups(client)
    for issue in issues:
        _validate_estimate(issue, settings, errors, warnings)
        _validate_due(issue, errors)
        if issue["story_points"] is not None and not isinstance(
            issue["story_points"], (int, float)
        ):
            _add(errors, issue["ref"], "story_points", "story_points must be a number")
        _validate_against_jira(lookups, issue, errors, warnings)

    return {"ok": not errors, "errors": errors, "warnings": warnings}


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #


def _estimate_seconds(issue: dict, settings: Any) -> int | None:
    """Seconds for an issue's estimate, or ``None`` when absent/unparseable."""
    estimate = issue["estimate"]
    if _is_blank(estimate):
        return None
    text = str(estimate).strip()
    if text.lower() in (ASK, NO_ESTIMATE):
        return None
    hours, days = _clock_rates(settings)
    try:
        return int(_mod("durations").parse_duration(text, hours, days))
    except Exception:  # noqa: BLE001 - preview must work on an invalid plan
        return None


def preview_plan(plan: dict, settings: Any = None) -> dict:
    """Return the report shape: one row per planned issue plus estimate totals."""
    report = _mod("report")
    durations = _mod("durations")
    hours, _days = _clock_rates(settings)

    issues = effective_issues(plan)
    rows: list[dict] = []
    total_seconds = 0
    unestimated: list[str] = []
    needs_answer: list[str] = []
    agent_estimated: list[str] = []
    per_assignee: dict[str, dict] = {}

    for issue in issues:
        seconds = _estimate_seconds(issue, settings)
        assignee = issue["assignee"] or "unassigned"
        bucket = per_assignee.setdefault(
            str(assignee), {"count": 0, "estimate_seconds": 0}
        )
        bucket["count"] += 1
        if seconds:
            total_seconds += seconds
            bucket["estimate_seconds"] += seconds
        else:
            unestimated.append(issue["ref"])

        estimate_text = "-" if _is_blank(issue["estimate"]) else str(
            issue["estimate"]
        ).strip()
        if estimate_text.lower() == ASK:
            needs_answer.append(issue["ref"])
        by = issue.get("estimate_by")
        if isinstance(by, str) and by.strip().lower() == ESTIMATE_BY_AGENT:
            agent_estimated.append(issue["ref"])
        rows.append(
            {
                "ref": issue["ref"],
                "project": issue["project"] or "-",
                "type": issue["type"],
                "summary": issue["summary"] or "",
                "assignee": issue["assignee"] or "-",
                "estimate": estimate_text,
                "due": "-" if _is_blank(issue["due"]) else str(issue["due"]).strip(),
                "parent": "-" if _is_blank(issue["parent"]) else str(
                    issue["parent"]
                ).strip(),
                "sprint": "-" if _is_blank(issue["sprint"]) else str(
                    issue["sprint"]
                ).strip(),
            }
        )

    for bucket in per_assignee.values():
        bucket["estimate"] = durations.format_duration(
            bucket["estimate_seconds"], hours
        )

    summary = {
        "issues": len(issues),
        "total_estimate": durations.format_duration(total_seconds, hours),
        "total_estimate_seconds": total_seconds,
        "without_estimate": len(unestimated),
        "needs_estimate_answer": needs_answer,
        "agent_estimated": agent_estimated,
        "per_assignee": per_assignee,
    }
    return report.make_report(
        "Plan preview",
        list(PREVIEW_COLUMNS),
        rows,
        summary=summary,
        meta={"source": plan.get("_source")},
    )


# --------------------------------------------------------------------------- #
# state file
# --------------------------------------------------------------------------- #


def default_state_path(plan: dict) -> Path | None:
    """``<plan-stem>.applied.json`` beside the plan, or ``None`` for an in-memory plan."""
    source = plan.get("_source")
    if not source:
        return None
    path = Path(source)
    return path.with_name(f"{path.stem}.applied.json")


def _write_atomic(path: Path, text: str, mode: int = 0o600) -> Path:
    """Write ``text`` to ``path`` atomically with ``mode`` permissions."""
    path = Path(path)
    if path.is_dir():
        raise _input_error(f"refusing to write over the directory {path}")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def _load_state(path: Path | None) -> dict:
    """Read the resume state, or a fresh empty one when there is no file yet."""
    if path is None or not Path(path).exists():
        return {"version": 1, "created": {}, "links": {}}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _input_error(f"cannot read plan state file {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("created"), dict):
        raise _input_error(
            f"plan state file {path} is corrupt: expected a 'created' mapping"
        )
    for ref, entry in data["created"].items():
        key = entry.get("key") if isinstance(entry, dict) else None
        if _issue_key_or_none(key) is None:
            raise _input_error(
                f"plan state file {path} is corrupt: ref {ref!r} has an invalid "
                f"issue key"
            )
    links = data.get("links")
    return {
        "version": data.get("version", 1),
        "created": dict(data["created"]),
        "links": dict(links) if isinstance(links, dict) else {},
    }


def _save_state(path: Path | None, state: dict) -> None:
    if path is None:
        return
    state["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_atomic(path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def save_plan(plan: dict, path: Path) -> Path:
    """Write the plan back to ``path`` atomically (internal ``_`` keys are dropped)."""
    body = {key: value for key, value in plan.items() if not key.startswith("_")}
    text = yaml.safe_dump(body, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return _write_atomic(Path(path), text)


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


def _topological_order(issues: list[dict]) -> list[dict]:
    """Plan order, except that an issue never precedes its in-plan parent."""
    by_ref = {issue["ref"]: issue for issue in issues}
    ordered: list[dict] = []
    placed: set[str] = set()

    def place(issue: dict, seen: frozenset[str]) -> None:
        ref = issue["ref"]
        if ref in placed or ref in seen:
            return
        parent = issue.get("parent")
        parent = str(parent) if parent not in (None, "") else None
        if parent in by_ref:
            place(by_ref[parent], seen | {ref})
        if ref not in placed:
            placed.add(ref)
            ordered.append(issue)

    for issue in issues:
        place(issue, frozenset())
    return ordered


def _resolve_sprint_id(
    client: Any, issue: dict, cache: dict[int, int | None]
) -> int | None:
    """Sprint id for an issue: a literal id, or the board's active sprint (cached)."""
    sprint = issue["sprint"]
    if _is_blank(sprint):
        return None
    text = str(sprint).strip()
    if text.lower() != "active":
        try:
            return int(text)
        except ValueError as exc:
            raise _input_error(
                f"issue {issue['ref']!r}: sprint must be 'active' or a numeric "
                f"sprint id, got {sprint!r}"
            ) from exc
    try:
        board_id = int(issue["board"])
    except (TypeError, ValueError) as exc:
        raise _input_error(
            f"issue {issue['ref']!r}: sprint 'active' needs a numeric board id, "
            f"got {issue['board']!r}"
        ) from exc
    if board_id not in cache:
        boards = importlib.import_module("jira_cli.services.boards")
        active = boards.active_sprint(client, board_id)
        cache[board_id] = int(active["id"]) if active else None
    sprint_id = cache[board_id]
    if sprint_id is None:
        raise _input_error(f"board {board_id} has no active sprint")
    return sprint_id


def _build_spec(
    client: Any,
    issue: dict,
    created: dict[str, dict],
    sprint_cache: dict[int, int | None],
) -> Any:
    """Build an ``issues.IssueSpec`` for one planned issue (links are added later)."""
    issues_mod = importlib.import_module("jira_cli.services.issues")
    durations = _mod("durations")

    parent_key = None
    parent = issue.get("parent")
    if parent not in (None, ""):
        parent = str(parent)
        parent_key = _issue_key_or_none(parent)
        if parent_key is None:
            entry = created.get(parent)
            if not entry:
                raise _input_error(
                    f"parent {parent!r} has no issue key yet (it was not created)"
                )
            parent_key = validate_issue_key(entry["key"], what="parent key")

    epic_key = None
    if not _is_blank(issue["epic"]):
        epic_key = validate_issue_key(issue["epic"], what="epic key")

    estimate = issue["estimate"]
    original_estimate = None
    if not _is_blank(estimate):
        text = str(estimate).strip()
        if text.lower() not in (ASK, NO_ESTIMATE):
            original_estimate = durations.to_jira(text)

    due_date = None
    if not _is_blank(issue["due"]):
        due_date = durations.resolve_date(str(issue["due"]).strip())

    return issues_mod.IssueSpec(
        project=str(issue["project"]),
        summary=str(issue["summary"]),
        issue_type=str(issue["type"]),
        description=issue["description"],
        assignee=issue["assignee"],
        priority=issue["priority"],
        labels=list(issue["labels"]),
        due_date=due_date,
        original_estimate=original_estimate,
        epic_key=epic_key,
        parent_key=parent_key,
        components=list(issue["components"]),
        fix_versions=list(issue["fix_versions"]),
        story_points=issue["story_points"],
        sprint_id=_resolve_sprint_id(client, issue, sprint_cache),
    )


def _link_id(source_ref: str, link_type: str, target_ref: str) -> str:
    """Stable identity of a planned link, used as the resume key in the state file."""
    return f"{source_ref}|{link_type}|{target_ref}"


def _apply_links(
    client: Any,
    issues: list[dict],
    created: dict[str, dict],
    state: dict,
    state_path: Path | None,
) -> tuple[list[dict], bool]:
    """Create the plan's issue links once every issue exists.

    Each successful link is recorded in the state file immediately, so a resumed
    run does not create the same link twice.
    """
    errs = _errs()
    issues_mod = importlib.import_module("jira_cli.services.issues")
    done: dict = state.setdefault("links", {})
    results: list[dict] = []
    stopped = False

    def resolve(target: str) -> str | None:
        key = _issue_key_or_none(target)
        if key is not None:
            return key
        entry = created.get(target)
        return _issue_key_or_none(entry.get("key")) if entry else None

    for issue in issues:
        if stopped:
            break
        source = resolve(issue["ref"])
        for link in issue["links"]:
            link_type = str(link.get("type"))
            target_ref = str(link.get("to"))
            record = {"from": issue["ref"], "to": target_ref, "type": link_type}
            marker = _link_id(issue["ref"], link_type, target_ref)
            if marker in done:
                results.append(
                    {**record, "ok": True, "skipped": "created in a previous run"}
                )
                continue
            target = resolve(target_ref)
            if source is None or target is None:
                missing = issue["ref"] if source is None else target_ref
                results.append(
                    {**record, "ok": False, "error": f"no issue key for {missing!r}"}
                )
                continue
            try:
                issues_mod.link_issues(client, source, target, link_type)
            except (errs.AuthError, errs.JiraPermissionError) as exc:
                results.append({**record, "ok": False, "error": str(exc)})
                stopped = True
                break
            except Exception as exc:  # noqa: BLE001 - collect and keep going
                results.append({**record, "ok": False, "error": str(exc)})
            else:
                done[marker] = {"from": source, "to": target, "type": link_type}
                _save_state(state_path, state)
                results.append({**record, "ok": True})
    return results, stopped


def apply_plan(
    client: Any,
    plan: dict,
    *,
    state_path: Path | None = None,
    dry_run: bool = True,
) -> dict:
    """Create every planned issue in Jira, parents first, resumably.

    The plan is validated first. With ``dry_run=True`` the planned order is returned
    and nothing is written; with ``dry_run=False`` a plan with validation errors is
    refused (``errors.InputError``).

    After each successful creation ``ref -> key`` is written to the state file
    (``state_path``, default ``<plan-stem>.applied.json``, mode 0600), so re-running
    skips refs that already exist. The run stops at the first ``AuthError`` or
    ``JiraPermissionError``; any other failure is collected and the run continues.
    """
    errs = _errs()
    validation = validate_plan(client, plan)
    issues = effective_issues(plan)
    order = [issue["ref"] for issue in _topological_order(issues)]

    if not validation["ok"]:
        if not dry_run:
            detail = "; ".join(
                f"{item['ref'] or 'plan'}.{item['field']}: {item['message']}"
                for item in validation["errors"][:5]
            )
            raise _input_error(
                f"plan has {len(validation['errors'])} validation error(s), "
                f"refusing to apply: {detail}"
            )
        return {
            "created": [],
            "skipped": [],
            "failed": [],
            "links": [],
            "planned": [],
            "order": order,
            "dry_run": True,
            "stopped": False,
            "ok": False,
            "validation": validation,
            "warnings": [],
            "state_path": None,
        }

    ordered = _topological_order(issues)
    if dry_run:
        planned = [
            {
                "ref": issue["ref"],
                "project": issue["project"],
                "type": issue["type"],
                "summary": issue["summary"],
                "parent": str(issue["parent"]) if issue["parent"] is not None else None,
            }
            for issue in ordered
        ]
        return {
            "created": [],
            "skipped": [],
            "failed": [],
            "links": [],
            "planned": planned,
            "order": order,
            "dry_run": True,
            "stopped": False,
            "ok": True,
            "validation": validation,
            "warnings": [],
            "state_path": str(state_path) if state_path else None,
        }

    resolved_state_path = Path(state_path) if state_path else default_state_path(plan)
    state = _load_state(resolved_state_path)
    created_map: dict[str, dict] = dict(state["created"])

    issues_mod = importlib.import_module("jira_cli.services.issues")
    created: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    warnings: list[str] = []
    if resolved_state_path is None:
        warnings.append(
            "no state file: this run cannot be resumed (pass state_path or load the "
            "plan from a file)"
        )
    stopped = False
    sprint_cache: dict[int, int | None] = {}

    for issue in ordered:
        ref = issue["ref"]
        if ref in created_map:
            entry = created_map[ref]
            skipped.append(
                {
                    "ref": ref,
                    "key": entry.get("key"),
                    "url": entry.get("url"),
                    "reason": "already created in a previous run",
                }
            )
            continue
        if stopped:
            failed.append({"ref": ref, "error": "not attempted: run stopped earlier"})
            continue

        try:
            spec = _build_spec(client, issue, created_map, sprint_cache)
            result = issues_mod.create_issue(client, spec)
        except (errs.AuthError, errs.JiraPermissionError) as exc:
            failed.append({"ref": ref, "error": str(exc)})
            stopped = True
            continue
        except Exception as exc:  # noqa: BLE001 - collect and keep going
            failed.append({"ref": ref, "error": str(exc)})
            continue

        key = (result or {}).get("key")
        if not key:
            failed.append(
                {"ref": ref, "error": "create_issue returned no issue key"}
            )
            continue
        entry = {"key": str(key), "url": (result or {}).get("url")}
        created_map[ref] = entry
        state["created"][ref] = entry
        _save_state(resolved_state_path, state)
        created.append(
            {
                "ref": ref,
                "key": entry["key"],
                "url": entry["url"],
                "warnings": list((result or {}).get("warnings") or []),
            }
        )

    links, links_stopped = (
        ([], False)
        if stopped
        else _apply_links(client, ordered, created_map, state, resolved_state_path)
    )

    return {
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "links": links,
        "planned": [],
        "order": order,
        "dry_run": False,
        "stopped": stopped or links_stopped,
        "ok": not failed and not stopped,
        "validation": validation,
        "warnings": warnings,
        "state_path": str(resolved_state_path) if resolved_state_path else None,
    }
