"""Non-interactive command-line entry point for jira-cli.

Every capability the MCP server exposes has a hand-usable command here, so the
whole tool can be driven from bash with no menu and no agent:

    jira report team --team backend --since 14d --format md --out team.md
    jira issue create --project PROJ --summary "Fix login" --assignee alice --yes

Design notes
------------
* Service modules are imported at the top of this module, not lazily, so a
  test can monkeypatch ``main.issues`` and friends with fakes.
* The interactive menu and the MCP server are imported *inside* their handlers:
  running ``jira issue show`` must not pay for either.
* Writes always print a preview and then ask ``Proceed? [y/N]`` unless
  ``--yes`` was passed; a declined confirmation performs no request, prints
  ``Aborted, nothing changed.`` on stderr and exits 4.
* With ``--json`` stdout carries only JSON: previews, the ``Proceed?``
  prompt and notices go to stderr.
* Text that came from the server is passed through :mod:`jira_cli.textsafe`
  before it reaches the terminal; error lines and ``--debug`` tracebacks are
  also redacted.
* Exit codes are stable: 0 ok, 1 input/other failure, 2 configuration,
  3 authentication, 4 cancelled at the confirmation, 5 local file or I/O
  error (``OSError``), 130 interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .. import config as config_module
from ..client import JiraClient
from ..errors import AuthError, ConfigError, InputError, JiraError
from ..fields import FieldMap
from ..redact import redact
from ..services import (
    boards,
    export,
    filters,
    issues,
    people,
    plan,
    projects,
    reports,
    sprint_report,
    worklog,
)
from ..textsafe import terminal_safe, terminal_safe_line
from . import output

#: Exit codes, keyed by the error class they belong to.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_CANCELLED = 4
EXIT_IO = 5
EXIT_INTERRUPT = 130

#: Printed on stderr when the user declines (or cannot answer) a confirmation.
ABORTED_MESSAGE = "Aborted, nothing changed."

# json.dumps(ensure_ascii=False) keeps C1 controls (U+0080-U+009F, e.g. the
# 8-bit CSI U+009B) and bidi overrides as raw characters. They can only occur
# inside JSON strings, so rewriting them as \uXXXX keeps the document valid.
_JSON_UNSAFE_RE = re.compile("[\x7f-\x9f\u202a-\u202e\u2066-\u2069]")

#: Report kinds accepted by ``jira report <kind>``.
REPORT_KINDS = (
    "team",
    "person",
    "stale",
    "overdue",
    "incomplete",
    "unassigned",
    "workload",
    "worklog",
    "sprint",
    "velocity",
)

#: Columns used when a list of normalised issues is shown as a table.
ISSUE_COLUMNS = ("key", "type", "status", "assignee", "priority", "due", "summary")

_PROGRAM = "jira"


# --------------------------------------------------------------------- context


class Context:
    """Holds the parsed arguments and builds settings/client on first use.

    Lazy construction matters: ``jira plan new plan.yaml`` and ``jira filter
    list`` must work on a machine with no credentials configured yet.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._settings: Any = None
        self._client: Any = None
        self._teams: dict | None = None

    # -- resources ---------------------------------------------------------

    @property
    def settings(self) -> Any:
        """Settings loaded from the environment and the env file (once)."""
        if self._settings is None:
            env_file = Path(self.args.env_file) if self.args.env_file else None
            self._settings = config_module.load_settings(env_file=env_file)
        return self._settings

    @property
    def client(self) -> Any:
        """A :class:`~jira_cli.client.JiraClient` for the loaded settings."""
        if self._client is None:
            self._client = JiraClient(self.settings)
        return self._client

    @property
    def teams(self) -> dict:
        """Teams from ``settings.teams_file`` (``{}`` when the file is absent)."""
        if self._teams is None:
            self._teams = people.load_teams(Path(self.settings.teams_file))
        return self._teams

    # -- output ------------------------------------------------------------

    @property
    def as_json(self) -> bool:
        return bool(getattr(self.args, "json", False))

    @property
    def chatter(self) -> Any:
        """Stream for human-only text: stderr under ``--json``, else stdout."""
        return sys.stderr if self.as_json else sys.stdout

    def emit(self, payload: Any, text: str | None = None) -> None:
        """Print ``payload`` as JSON with ``--json``, otherwise ``text``.

        ``text`` usually embeds server values, so it is made terminal-safe.
        """
        if self.as_json:
            print(_json_text(payload))
        elif text is not None:
            print(terminal_safe(text))

    def emit_table(self, columns: Sequence[str], rows: Sequence[dict]) -> None:
        """Print a list of dicts as JSON or as an aligned table."""
        self.emit(list(rows), output.render_table(columns, list(rows)))

    def emit_issues(self, issue_list: Sequence[dict]) -> None:
        self.emit_table(ISSUE_COLUMNS, [dict(issue) for issue in issue_list])

    def emit_report(self, report: dict) -> None:
        """Print a report in the requested format and/or write it to ``--out``."""
        fmt = getattr(self.args, "format", None)
        out = getattr(self.args, "out", None)
        if self.as_json and fmt is None:
            fmt = "json"
        if out:
            written = export.write_report(report, Path(out), fmt)
            self.emit({"written": str(written)}, f"written: {written}")
            return
        print(output.render_report(report, fmt or "table"))

    def confirm(self, title: str, lines: Sequence[str]) -> bool:
        """Show exactly what is about to be written, then ask ``Proceed?``.

        Each preview line is folded to one terminal-safe line, so a value
        carrying a newline or an escape sequence cannot fake or erase a line
        of the preview. A decline prints :data:`ABORTED_MESSAGE` on stderr;
        handlers then return :data:`EXIT_CANCELLED`.
        """
        stream = self.chatter
        print(terminal_safe_line(title), file=stream)
        for line in lines:
            print(f"  {terminal_safe_line(line)}", file=stream)
        assume_yes = bool(getattr(self.args, "yes", False))
        # output.confirm echoes to stdout; keep stdout pure JSON under --json.
        with contextlib.redirect_stdout(stream):
            accepted = output.confirm("Proceed?", assume_yes)
        if accepted:
            return True
        print(ABORTED_MESSAGE, file=sys.stderr)
        return False


# ------------------------------------------------------------------- helpers


def _json_text(payload: Any) -> str:
    """``payload`` as indented JSON with C1 and bidi controls escaped."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return _JSON_UNSAFE_RE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def _split(value: str | None) -> list[str]:
    """``"a, b"`` -> ``["a", "b"]``; blank input is an empty list."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _int_list(value: str | None) -> list[int]:
    numbers = []
    for item in _split(value):
        if not item.lstrip("-").isdigit():
            raise InputError(f"not a number: {item}")
        numbers.append(int(item))
    return numbers


def _selected_people(ctx: Context) -> list[str]:
    """Resolve ``--team`` / ``--users`` / ``--user`` into usernames."""
    args = ctx.args
    if getattr(args, "team", None):
        resolved = people.resolve_people(f"team:{args.team}", ctx.teams)
        if not resolved:
            raise InputError(f"team {args.team!r} has no members")
        return list(resolved)
    if getattr(args, "users", None):
        resolved = people.resolve_people(args.users, ctx.teams)
        if not resolved:
            raise InputError(f"no users matched {args.users!r}")
        return list(resolved)
    if getattr(args, "user", None):
        return [args.user]
    return []


def _selected_projects(ctx: Context) -> list[str] | None:
    """Projects from ``--projects``, else the team's projects, else ``None``."""
    explicit = _split(getattr(ctx.args, "projects", None))
    if explicit:
        return explicit
    team_name = getattr(ctx.args, "team", None)
    if team_name:
        entry = ctx.teams.get(team_name) or {}
        team_projects = [str(key) for key in entry.get("projects") or []]
        if team_projects:
            return team_projects
    return None


def _require_people(ctx: Context, kind: str) -> list[str]:
    usernames = _selected_people(ctx)
    if not usernames:
        raise InputError(
            f"report {kind} needs people: pass --team NAME, --users a,b or --user U"
        )
    return usernames


# ------------------------------------------------------------------- handlers
# Each handler takes the context and returns an exit code.


def cmd_me(ctx: Context) -> int:
    me = ctx.client.myself()
    ctx.emit(me, f"{me.get('name')}  {me.get('displayName')}  {me.get('emailAddress') or ''}".strip())
    return EXIT_OK


def cmd_config_show(ctx: Context) -> int:
    described = config_module.describe_settings(ctx.settings)
    text = "\n".join(f"  {key:<18} {value}" for key, value in sorted(described.items()))
    ctx.emit(described, text)
    return EXIT_OK


# -- doctor ---------------------------------------------------------------


def _env_file_path(ctx: Context) -> Path | None:
    """The env file the configuration would use, following the documented order."""
    if ctx.args.env_file:
        return Path(ctx.args.env_file)
    from_env = os.environ.get("JIRA_CLI_ENV_FILE")
    if from_env:
        return Path(from_env)
    try:
        return Path(ctx.settings.config_dir) / ".env"
    except (ConfigError, AttributeError):
        return None


def _check_settings(ctx: Context, record: Any) -> None:
    try:
        described = config_module.describe_settings(ctx.settings)
    except JiraError as exc:
        record("FAIL", "settings", str(exc))
        return
    record("PASS", "settings", f"base_url={described.get('base_url')}")
    for key in ("auth_mode", "username", "secret", "default_project", "verify_tls", "timeout"):
        if key in described:
            record("PASS", f"settings.{key}", str(described[key]))


def _check_env_file(ctx: Context, record: Any) -> None:
    path = _env_file_path(ctx)
    if path is None:
        record("WARN", "env-file", "no env file location could be determined")
        return
    if not path.exists():
        record("WARN", "env-file", f"{path} does not exist (environment variables only)")
        return
    record("PASS", "env-file", str(path))
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:  # pragma: no cover - unreadable parent directory
        record("WARN", "env-file permission", f"cannot stat {path}: {exc}")
        return
    if mode & 0o077:
        record("WARN", "env-file permission", f"{path} is mode {mode:04o}; use chmod 600")
    else:
        record("PASS", "env-file permission", f"{mode:04o}")


def _check_server(ctx: Context, record: Any) -> None:
    try:
        info = ctx.client.get("/rest/api/2/serverInfo") or {}
    except JiraError as exc:
        record("FAIL", "serverInfo", str(exc))
        return
    record(
        "PASS",
        "serverInfo",
        f"{info.get('version')} {info.get('deploymentType') or ''}".strip(),
    )


def _check_myself(ctx: Context, record: Any) -> None:
    try:
        me = ctx.client.myself() or {}
    except AuthError as exc:
        record("FAIL", "myself", f"{exc} (check JIRA_PAT / JIRA_USERNAME)")
        return
    except JiraError as exc:
        record("FAIL", "myself", str(exc))
        return
    record("PASS", "myself", f"{me.get('name')} ({me.get('displayName')})")


def _check_read_only(ctx: Context, record: Any) -> None:
    if getattr(ctx.settings, "read_only", False):
        record("WARN", "read_only", "JIRA_READ_ONLY is on; every write will be refused")
    else:
        record("PASS", "read_only", "off; writes are allowed")


def _check_fields(ctx: Context, record: Any) -> None:
    try:
        field_map = FieldMap(ctx.client)
        found = {
            "sprint": field_map.sprint,
            "epic_link": field_map.epic_link,
            "story_points": field_map.story_points,
        }
    except JiraError as exc:
        record("FAIL", "fields", str(exc))
        return
    for name, field_id in found.items():
        if field_id:
            record("PASS", f"field.{name}", str(field_id))
        else:
            record(
                "WARN",
                f"field.{name}",
                f"not found; set JIRA_FIELD_{name.upper()}=customfield_x",
            )


_DOCTOR_CHECKS = (
    _check_settings,
    _check_env_file,
    _check_server,
    _check_myself,
    _check_read_only,
    _check_fields,
)


def cmd_doctor(ctx: Context) -> int:
    """Run every configuration and connectivity check and report PASS/WARN/FAIL."""
    results: list[dict[str, str]] = []

    def record(status: str, check: str, detail: str) -> None:
        results.append({"status": status, "check": check, "detail": detail})
        if not ctx.as_json:
            tint = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[status]
            safe_detail = terminal_safe_line(detail)
            print(f"{output.color(status, tint):<6} {terminal_safe_line(check):<24} {safe_detail}")

    for check in _DOCTOR_CHECKS:
        try:
            check(ctx, record)
        except ConfigError as exc:
            record("FAIL", check.__name__.lstrip("_"), str(exc))
        except JiraError as exc:
            record("FAIL", check.__name__.lstrip("_"), str(exc))

    failures = [item for item in results if item["status"] == "FAIL"]
    warnings = [item for item in results if item["status"] == "WARN"]
    if ctx.as_json:
        print(
            _json_text(
                {
                    "ok": not failures,
                    "failures": len(failures),
                    "warnings": len(warnings),
                    "checks": results,
                }
            )
        )
    else:
        print(f"\n{len(failures)} failed, {len(warnings)} warnings, {len(results)} checks")
    return EXIT_ERROR if failures else EXIT_OK


# -- projects and boards --------------------------------------------------


def cmd_projects_list(ctx: Context) -> int:
    rows = projects.list_projects(ctx.client)
    ctx.emit_table(("key", "name", "lead", "type"), rows)
    return EXIT_OK


def cmd_projects_show(ctx: Context) -> int:
    project = projects.get_project(ctx.client, ctx.args.key)
    lines = [f"{project.get('key')}  {project.get('name')}", f"  lead {project.get('lead')}"]
    for label in ("issue_types", "components", "versions"):
        values = project.get(label) or []
        lines.append(f"  {label:<12} {', '.join(str(item) for item in values) or '-'}")
    ctx.emit(project, "\n".join(lines))
    return EXIT_OK


def cmd_projects_create(ctx: Context) -> int:
    args = ctx.args
    if not ctx.confirm(
        "Create project",
        [
            f"key      {args.key}",
            f"name     {args.name}",
            f"lead     {args.lead}",
            f"template {args.template}",
        ],
    ):
        return EXIT_CANCELLED
    created = projects.create_project(
        ctx.client, args.key, args.name, args.lead, args.template, args.description
    )
    ctx.emit(created, f"created project {created.get('key')}  {created.get('url')}")
    return EXIT_OK


def cmd_projects_component(ctx: Context) -> int:
    args = ctx.args
    if not ctx.confirm("Create component", [f"project {args.project}", f"name {args.name}"]):
        return EXIT_CANCELLED
    created = projects.create_component(ctx.client, args.project, args.name, args.lead)
    ctx.emit(created, f"created component {created.get('name')}")
    return EXIT_OK


def cmd_projects_version(ctx: Context) -> int:
    args = ctx.args
    if not ctx.confirm("Create version", [f"project {args.project}", f"name {args.name}"]):
        return EXIT_CANCELLED
    created = projects.create_version(ctx.client, args.project, args.name, args.release_date)
    ctx.emit(created, f"created version {created.get('name')}")
    return EXIT_OK


def cmd_projects_types(ctx: Context) -> int:
    types = projects.list_issue_types(ctx.client, ctx.args.project)
    ctx.emit(types, "\n".join(str(item) for item in types))
    return EXIT_OK


def cmd_projects_priorities(ctx: Context) -> int:
    priorities = projects.list_priorities(ctx.client)
    ctx.emit(priorities, "\n".join(str(item) for item in priorities))
    return EXIT_OK


def cmd_projects_statuses(ctx: Context) -> int:
    statuses = projects.list_statuses(ctx.client, ctx.args.project)
    ctx.emit(statuses, "\n".join(str(item) for item in statuses))
    return EXIT_OK


def cmd_boards_list(ctx: Context) -> int:
    rows = boards.list_boards(ctx.client, ctx.args.project, ctx.args.type)
    ctx.emit_table(("id", "name", "type", "project"), rows)
    return EXIT_OK


def cmd_boards_show(ctx: Context) -> int:
    board = boards.get_board(ctx.client, ctx.args.board_id)
    ctx.emit(board, f"{board.get('id')}  {board.get('name')}  {board.get('type')}")
    return EXIT_OK


def cmd_boards_ensure(ctx: Context) -> int:
    args = ctx.args
    if not ctx.confirm(
        "Ensure board",
        [f"project {args.key}", f"type {args.type}", "an existing board is reused"],
    ):
        return EXIT_CANCELLED
    result = boards.ensure_board(ctx.client, args.key, args.type, args.name)
    board = result.get("board") or {}
    ctx.emit(
        result,
        f"board {board.get('name')} (id {board.get('id')}) "
        f"{'created' if result.get('created') else 'reused'}",
    )
    return EXIT_OK


def cmd_boards_issues(ctx: Context) -> int:
    found = boards.board_issues(ctx.client, ctx.args.board_id, ctx.args.jql, ctx.args.limit)
    ctx.emit_issues(found)
    return EXIT_OK


def cmd_sprints(ctx: Context) -> int:
    rows = boards.list_sprints(ctx.client, ctx.args.board_id, ctx.args.state)
    ctx.emit_table(("id", "name", "state", "start", "end", "goal"), rows)
    return EXIT_OK


def cmd_sprint_active(ctx: Context) -> int:
    sprint = boards.active_sprint(ctx.client, ctx.args.board_id)
    if sprint is None:
        ctx.emit(None, "no active sprint")
        return EXIT_OK
    ctx.emit(sprint, f"{sprint.get('id')}  {sprint.get('name')}  {sprint.get('state')}")
    return EXIT_OK


def cmd_sprint_create(ctx: Context) -> int:
    args = ctx.args
    if not ctx.confirm(
        "Create sprint",
        [
            f"board {args.board_id}",
            f"name  {args.name}",
            f"start {args.start or '-'}",
            f"end   {args.end or '-'}",
            f"goal  {args.goal or '-'}",
        ],
    ):
        return EXIT_CANCELLED
    sprint = boards.create_sprint(
        ctx.client, args.board_id, args.name, args.start, args.end, args.goal
    )
    ctx.emit(sprint, f"created sprint {sprint.get('name')} (id {sprint.get('id')})")
    return EXIT_OK


def cmd_sprint_add(ctx: Context) -> int:
    args = ctx.args
    keys = [key.upper() for key in args.keys]
    if not ctx.confirm(
        "Add issues to sprint", [f"sprint {args.sprint_id}", f"issues {', '.join(keys)}"]
    ):
        return EXIT_CANCELLED
    result = boards.add_to_sprint(ctx.client, args.sprint_id, keys)
    ctx.emit(result, f"{len(keys)} issue(s) added to sprint {args.sprint_id}")
    return EXIT_OK


def cmd_sprint_backlog(ctx: Context) -> int:
    keys = [key.upper() for key in ctx.args.keys]
    if not ctx.confirm("Move issues to backlog", [f"issues {', '.join(keys)}"]):
        return EXIT_CANCELLED
    result = boards.move_to_backlog(ctx.client, keys)
    ctx.emit(result, f"{len(keys)} issue(s) moved to the backlog")
    return EXIT_OK


# -- issues ---------------------------------------------------------------


def cmd_issue_show(ctx: Context) -> int:
    issue = issues.get_issue(
        ctx.client, ctx.args.key.upper(), comments=True, changelog=ctx.args.changelog
    )
    if ctx.as_json:
        ctx.emit(issue)
    else:
        output.print_issue(issue)
    return EXIT_OK


def cmd_issue_create(ctx: Context) -> int:
    args = ctx.args
    spec = issues.IssueSpec(
        project=args.project.upper(),
        summary=args.summary,
        issue_type=args.type,
        description=args.desc,
        assignee=args.assignee,
        priority=args.priority,
        labels=list(args.label or []),
        due_date=args.due,
        original_estimate=args.estimate,
        epic_key=args.epic,
        parent_key=args.parent,
        components=_split(args.components),
        fix_versions=_split(args.fix_versions),
        story_points=args.story_points,
        sprint_id=args.sprint,
    )
    preview = [
        f"project   {spec.project}",
        f"type      {spec.issue_type}",
        f"summary   {spec.summary}",
        f"assignee  {spec.assignee or '(unassigned)'}",
        f"priority  {spec.priority or '(server default)'}",
        f"estimate  {spec.original_estimate or '-'}",
        f"due       {spec.due_date or '-'}",
        f"epic      {spec.epic_key or '-'}",
        f"parent    {spec.parent_key or '-'}",
        f"sprint    {spec.sprint_id if spec.sprint_id is not None else '-'}",
        f"labels    {', '.join(spec.labels) or '-'}",
    ]
    if not ctx.confirm("Create issue", preview):
        return EXIT_CANCELLED
    created = issues.create_issue(ctx.client, spec)
    ctx.emit(created, f"created {created.get('key')}  {created.get('url')}")
    for warning in created.get("warnings") or []:
        print(f"warning: {terminal_safe_line(warning)}", file=sys.stderr)
    return EXIT_OK


_EDIT_FLAGS = (
    ("summary", "summary"),
    ("desc", "description"),
    ("priority", "priority"),
    ("due", "due_date"),
    ("estimate", "original_estimate"),
    ("remaining", "remaining_estimate"),
    ("story_points", "story_points"),
)


def cmd_issue_edit(ctx: Context) -> int:
    args = ctx.args
    changes: dict[str, Any] = {}
    for flag, field_name in _EDIT_FLAGS:
        value = getattr(args, flag, None)
        if value is not None:
            changes[field_name] = value
    if args.label:
        changes["labels"] = list(args.label)
    if args.components is not None:
        changes["components"] = _split(args.components)
    if args.fix_versions is not None:
        changes["fix_versions"] = _split(args.fix_versions)
    if not changes:
        raise InputError("nothing to change: pass at least one field flag")
    key = args.key.upper()
    if not ctx.confirm(f"Edit {key}", [f"{name} -> {value}" for name, value in changes.items()]):
        return EXIT_CANCELLED
    result = issues.update_fields(ctx.client, key, changes)
    ctx.emit(result, f"{key} updated")
    return EXIT_OK


def cmd_issue_assign(ctx: Context) -> int:
    key = ctx.args.key.upper()
    username = None if ctx.args.user.casefold() in ("none", "-", "unassigned") else ctx.args.user
    if not ctx.confirm(f"Assign {key}", [f"assignee -> {username or '(unassigned)'}"]):
        return EXIT_CANCELLED
    result = issues.assign(ctx.client, key, username)
    ctx.emit(result, f"{key} assigned to {username or '(unassigned)'}")
    return EXIT_OK


def cmd_issue_transitions(ctx: Context) -> int:
    rows = issues.list_transitions(ctx.client, ctx.args.key.upper())
    ctx.emit_table(("id", "name", "to"), rows)
    return EXIT_OK


def cmd_issue_move(ctx: Context) -> int:
    args = ctx.args
    key = args.key.upper()
    if not ctx.confirm(
        f"Move {key}", [f"to      {args.status}", f"comment {args.comment or '-'}"]
    ):
        return EXIT_CANCELLED
    result = issues.transition(ctx.client, key, args.status, args.comment)
    ctx.emit(result, f"{key} moved to {args.status}")
    return EXIT_OK


def cmd_issue_comment(ctx: Context) -> int:
    key = ctx.args.key.upper()
    if not ctx.confirm(f"Comment on {key}", [ctx.args.text]):
        return EXIT_CANCELLED
    result = issues.add_comment(ctx.client, key, ctx.args.text)
    ctx.emit(result, f"comment added to {key}")
    return EXIT_OK


def cmd_issue_comments(ctx: Context) -> int:
    rows = issues.list_comments(ctx.client, ctx.args.key.upper())
    ctx.emit_table(("author", "created", "body"), rows)
    return EXIT_OK


def cmd_issue_link(ctx: Context) -> int:
    args = ctx.args
    from_key, to_key = args.from_key.upper(), args.to_key.upper()
    if not ctx.confirm("Link issues", [f"{from_key} {args.type} {to_key}"]):
        return EXIT_CANCELLED
    result = issues.link_issues(ctx.client, from_key, to_key, args.type)
    ctx.emit(result, f"{from_key} {args.type} {to_key}")
    return EXIT_OK


def cmd_issue_link_types(ctx: Context) -> int:
    rows = issues.list_link_types(ctx.client)
    normalised = [row if isinstance(row, dict) else {"name": row} for row in rows]
    ctx.emit_table(("name", "inward", "outward"), normalised)
    return EXIT_OK


def cmd_issue_delete(ctx: Context) -> int:
    key = ctx.args.key.upper()
    if not ctx.confirm(f"Delete {key}", ["this cannot be undone"]):
        return EXIT_CANCELLED
    result = issues.delete_issue(ctx.client, key)
    ctx.emit(result, f"{key} deleted")
    return EXIT_OK


def cmd_issue_worklog(ctx: Context) -> int:
    args = ctx.args
    key = args.key.upper()
    if not ctx.confirm(
        f"Log work on {key}",
        [f"time    {args.time}", f"started {args.started or 'now'}", f"comment {args.comment or '-'}"],
    ):
        return EXIT_CANCELLED
    result = worklog.add_worklog(ctx.client, key, args.time, args.started, args.comment)
    ctx.emit(result, f"{args.time} logged on {key}")
    return EXIT_OK


def cmd_issue_worklogs(ctx: Context) -> int:
    rows = worklog.issue_worklogs(ctx.client, ctx.args.key.upper())
    ctx.emit_table(("id", "author", "started", "seconds", "comment"), rows)
    return EXIT_OK


def cmd_issue_estimate(ctx: Context) -> int:
    args = ctx.args
    if args.original is None and args.remaining is None:
        raise InputError("pass --original and/or --remaining")
    key = args.key.upper()
    if not ctx.confirm(
        f"Set estimates on {key}",
        [f"original  {args.original or '(unchanged)'}", f"remaining {args.remaining or '(unchanged)'}"],
    ):
        return EXIT_CANCELLED
    result = worklog.set_estimate(ctx.client, key, args.original, args.remaining)
    ctx.emit(result, f"estimates updated on {key}")
    return EXIT_OK


def cmd_search(ctx: Context) -> int:
    found = issues.search_issues(ctx.client, ctx.args.jql, ctx.args.limit)
    ctx.emit_issues(found)
    return EXIT_OK


def cmd_mine(ctx: Context) -> int:
    found = issues.my_issues(ctx.client, include_done=ctx.args.all, limit=ctx.args.limit)
    ctx.emit_issues(found)
    return EXIT_OK


# -- users and teams ------------------------------------------------------


def cmd_users_search(ctx: Context) -> int:
    rows = people.search_users(ctx.client, ctx.args.query, ctx.args.limit)
    ctx.emit_table(("username", "display_name", "email", "active"), rows)
    return EXIT_OK


def cmd_users_assignable(ctx: Context) -> int:
    rows = people.assignable_users(ctx.client, ctx.args.project.upper(), ctx.args.query or "")
    ctx.emit_table(("username", "display_name", "email", "active"), rows)
    return EXIT_OK


def cmd_team_list(ctx: Context) -> int:
    rows = [
        {
            "team": name,
            "members": ", ".join(entry.get("members") or []),
            "projects": ", ".join(entry.get("projects") or []),
            "boards": ", ".join(str(item) for item in entry.get("boards") or []),
        }
        for name, entry in sorted(ctx.teams.items())
    ]
    ctx.emit_table(("team", "members", "projects", "boards"), rows)
    return EXIT_OK


def cmd_team_show(ctx: Context) -> int:
    entry = ctx.teams.get(ctx.args.name)
    if entry is None:
        raise InputError(f"no such team: {ctx.args.name}")
    text = "\n".join(
        [
            ctx.args.name,
            f"  members  {', '.join(entry.get('members') or []) or '-'}",
            f"  projects {', '.join(entry.get('projects') or []) or '-'}",
            f"  boards   {', '.join(str(item) for item in entry.get('boards') or []) or '-'}",
        ]
    )
    ctx.emit(entry, text)
    return EXIT_OK


def cmd_team_set(ctx: Context) -> int:
    args = ctx.args
    members = _split(args.members)
    if not members:
        raise InputError("--members needs at least one username")
    team_projects = _split(args.projects) or None
    team_boards = _int_list(args.boards) or None
    if not ctx.confirm(
        f"Save team {args.name}",
        [
            f"members  {', '.join(members)}",
            f"projects {', '.join(team_projects or []) or '-'}",
            f"boards   {', '.join(str(item) for item in team_boards or []) or '-'}",
        ],
    ):
        return EXIT_CANCELLED
    result = people.save_team(
        Path(ctx.settings.teams_file), args.name, members, team_projects, team_boards
    )
    ctx.emit(result, f"team {args.name} saved")
    return EXIT_OK


def cmd_team_remove(ctx: Context) -> int:
    if not ctx.confirm("Remove team", [ctx.args.name]):
        return EXIT_CANCELLED
    removed = people.remove_team(Path(ctx.settings.teams_file), ctx.args.name)
    ctx.emit(
        {"removed": bool(removed)},
        f"team {ctx.args.name} removed" if removed else f"no such team: {ctx.args.name}",
    )
    return EXIT_OK


# -- reports --------------------------------------------------------------


def _report_team(ctx: Context) -> dict:
    return reports.team_dashboard(
        ctx.client, _require_people(ctx, "team"), _selected_projects(ctx), ctx.args.since
    )


def _report_person(ctx: Context) -> dict:
    usernames = _require_people(ctx, "person")
    return reports.person_activity(
        ctx.client,
        usernames[0],
        since=ctx.args.since,
        until=ctx.args.until,
        projects=_selected_projects(ctx),
    )


def _report_stale(ctx: Context) -> dict:
    return reports.stale_issues(
        ctx.client,
        days=ctx.args.days,
        projects=_selected_projects(ctx),
        usernames=_selected_people(ctx) or None,
    )


def _report_overdue(ctx: Context) -> dict:
    return reports.overdue_issues(
        ctx.client,
        projects=_selected_projects(ctx),
        usernames=_selected_people(ctx) or None,
    )


def _report_incomplete(ctx: Context) -> dict:
    return reports.incomplete_issues(
        ctx.client,
        projects=_selected_projects(ctx),
        usernames=_selected_people(ctx) or None,
    )


def _report_unassigned(ctx: Context) -> dict:
    selected = _selected_projects(ctx)
    if not selected:
        raise InputError("report unassigned needs --projects X,Y (or a team with projects)")
    return reports.unassigned_issues(ctx.client, selected)


def _report_workload(ctx: Context) -> dict:
    return reports.workload(
        ctx.client, _require_people(ctx, "workload"), _selected_projects(ctx)
    )


def _report_worklog(ctx: Context) -> dict:
    return worklog.worklog_report(
        ctx.client,
        _require_people(ctx, "worklog"),
        since=ctx.args.since,
        until=ctx.args.until,
        projects=_selected_projects(ctx),
    )


def _report_sprint(ctx: Context) -> dict:
    if ctx.args.board is None:
        raise InputError("report sprint needs --board ID")
    return sprint_report.sprint_report(ctx.client, ctx.args.board, ctx.args.sprint)


def _report_velocity(ctx: Context) -> dict:
    if ctx.args.board is None:
        raise InputError("report velocity needs --board ID")
    return sprint_report.velocity(ctx.client, ctx.args.board, last=ctx.args.last)


_REPORT_BUILDERS = {
    "team": _report_team,
    "person": _report_person,
    "stale": _report_stale,
    "overdue": _report_overdue,
    "incomplete": _report_incomplete,
    "unassigned": _report_unassigned,
    "workload": _report_workload,
    "worklog": _report_worklog,
    "sprint": _report_sprint,
    "velocity": _report_velocity,
}


def cmd_report(ctx: Context) -> int:
    report = _REPORT_BUILDERS[ctx.args.kind](ctx)
    ctx.emit_report(report)
    return EXIT_OK


def cmd_export(ctx: Context) -> int:
    """Re-render a report document saved earlier as JSON into another format."""
    source = Path(ctx.args.source)
    try:
        report = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{source} is not a JSON report: {exc}") from exc
    if not isinstance(report, dict) or "columns" not in report:
        raise InputError(f"{source} does not look like a report document")
    if ctx.args.out:
        written = export.write_report(report, Path(ctx.args.out), ctx.args.format)
        ctx.emit({"written": str(written)}, f"written: {written}")
        return EXIT_OK
    print(output.render_report(report, ctx.args.format or "table"))
    return EXIT_OK


# -- filters --------------------------------------------------------------


def _filters_path(ctx: Context) -> Path:
    return Path(ctx.settings.filters_file)


def cmd_filter_list(ctx: Context) -> int:
    saved = filters.list_filters(_filters_path(ctx)) or {}
    rows = [
        {
            "name": name,
            "jql": entry.get("jql") if isinstance(entry, dict) else entry,
            "description": entry.get("description") if isinstance(entry, dict) else None,
        }
        for name, entry in sorted(saved.items())
    ]
    ctx.emit_table(("name", "jql", "description"), rows)
    return EXIT_OK


def cmd_filter_save(ctx: Context) -> int:
    args = ctx.args
    if not ctx.confirm("Save filter", [f"name {args.name}", f"jql  {args.jql}"]):
        return EXIT_CANCELLED
    result = filters.save_filter(_filters_path(ctx), args.name, args.jql, args.description)
    ctx.emit(result, f"filter {args.name} saved")
    return EXIT_OK


def cmd_filter_run(ctx: Context) -> int:
    entry = filters.get_filter(_filters_path(ctx), ctx.args.name)
    jql = entry.get("jql") if isinstance(entry, dict) else entry
    if not jql:
        raise InputError(f"filter {ctx.args.name!r} has no JQL")
    found = issues.search_issues(ctx.client, str(jql), ctx.args.limit)
    ctx.emit_issues(found)
    return EXIT_OK


def cmd_filter_delete(ctx: Context) -> int:
    if not ctx.confirm("Delete filter", [ctx.args.name]):
        return EXIT_CANCELLED
    removed = filters.delete_filter(_filters_path(ctx), ctx.args.name)
    ctx.emit(
        {"removed": bool(removed)},
        f"filter {ctx.args.name} deleted" if removed else f"no such filter: {ctx.args.name}",
    )
    return EXIT_OK


def cmd_filter_server(ctx: Context) -> int:
    rows = filters.server_filters(ctx.client)
    ctx.emit_table(("id", "name", "jql", "owner"), rows)
    return EXIT_OK


# -- plans ----------------------------------------------------------------


def cmd_plan_new(ctx: Context) -> int:
    path = Path(ctx.args.file)
    if path.exists():
        raise InputError(f"{path} already exists; delete it or choose another file")
    if not ctx.confirm("Write plan template", [f"file {path}"]):
        return EXIT_CANCELLED
    path.write_text(plan.new_plan_template(), encoding="utf-8")
    ctx.emit({"path": str(path)}, f"written: {path}")
    return EXIT_OK


def cmd_plan_validate(ctx: Context) -> int:
    loaded = plan.load_plan(Path(ctx.args.file))
    result = plan.validate_plan(ctx.client, loaded)
    if ctx.as_json:
        ctx.emit(result)
    else:
        print("plan is valid" if result.get("ok") else "plan has errors")
        for problem in list(result.get("errors") or []) + list(result.get("warnings") or []):
            detail = f"{problem.get('ref')}: {problem.get('field')} {problem.get('message')}"
            print(f"  {terminal_safe_line(detail)}")
    return EXIT_OK if result.get("ok") else EXIT_ERROR


def cmd_plan_preview(ctx: Context) -> int:
    loaded = plan.load_plan(Path(ctx.args.file))
    ctx.emit_report(plan.preview_plan(loaded, ctx.settings))
    return EXIT_OK


def cmd_plan_apply(ctx: Context) -> int:
    args = ctx.args
    path = Path(args.file)
    loaded = plan.load_plan(path)
    state_path = Path(args.state) if args.state else None
    dry = plan.apply_plan(ctx.client, loaded, state_path=state_path, dry_run=True)
    if args.dry_run:
        ctx.emit(dry, output.render_table(("ref", "key", "url"), list(dry.get("created") or [])))
        return EXIT_OK
    lines = [
        f"would create {len(dry.get('created') or [])}",
        f"would skip   {len(dry.get('skipped') or [])}",
        f"failed       {len(dry.get('failed') or [])}",
    ]
    if not ctx.confirm(f"Apply plan {path} for real", lines):
        return EXIT_CANCELLED
    result = plan.apply_plan(ctx.client, loaded, state_path=state_path, dry_run=False)
    ctx.emit(
        result,
        f"created {len(result.get('created') or [])}, "
        f"skipped {len(result.get('skipped') or [])}, "
        f"failed {len(result.get('failed') or [])}",
    )
    return EXIT_ERROR if result.get("failed") else EXIT_OK


# -- menu and MCP ---------------------------------------------------------


def cmd_menu(ctx: Context) -> int:
    """Hand over to the interactive menu (imported here to keep startup cheap)."""
    from . import menu

    return int(menu.run(ctx.client, ctx.settings) or EXIT_OK)


def cmd_mcp(ctx: Context) -> int:
    """Run the MCP server over stdio (imported here: the CLI never needs mcp)."""
    from .. import mcp_server

    return int(mcp_server.main() or EXIT_OK)


# -------------------------------------------------------------------- parser

HANDLERS: dict[str, Any] = {
    "me": cmd_me,
    "doctor": cmd_doctor,
    "config.show": cmd_config_show,
    "projects.list": cmd_projects_list,
    "projects.show": cmd_projects_show,
    "projects.create": cmd_projects_create,
    "projects.component": cmd_projects_component,
    "projects.version": cmd_projects_version,
    "projects.types": cmd_projects_types,
    "projects.priorities": cmd_projects_priorities,
    "projects.statuses": cmd_projects_statuses,
    "boards.list": cmd_boards_list,
    "boards.show": cmd_boards_show,
    "boards.ensure": cmd_boards_ensure,
    "boards.issues": cmd_boards_issues,
    "sprints": cmd_sprints,
    "sprint.active": cmd_sprint_active,
    "sprint.create": cmd_sprint_create,
    "sprint.add": cmd_sprint_add,
    "sprint.backlog": cmd_sprint_backlog,
    "issue.show": cmd_issue_show,
    "issue.create": cmd_issue_create,
    "issue.edit": cmd_issue_edit,
    "issue.assign": cmd_issue_assign,
    "issue.transitions": cmd_issue_transitions,
    "issue.move": cmd_issue_move,
    "issue.comment": cmd_issue_comment,
    "issue.comments": cmd_issue_comments,
    "issue.link": cmd_issue_link,
    "issue.linktypes": cmd_issue_link_types,
    "issue.delete": cmd_issue_delete,
    "issue.worklog": cmd_issue_worklog,
    "issue.worklogs": cmd_issue_worklogs,
    "issue.estimate": cmd_issue_estimate,
    "search": cmd_search,
    "mine": cmd_mine,
    "users.search": cmd_users_search,
    "users.assignable": cmd_users_assignable,
    "team.list": cmd_team_list,
    "team.show": cmd_team_show,
    "team.set": cmd_team_set,
    "team.remove": cmd_team_remove,
    "report": cmd_report,
    "export": cmd_export,
    "filter.list": cmd_filter_list,
    "filter.save": cmd_filter_save,
    "filter.run": cmd_filter_run,
    "filter.delete": cmd_filter_delete,
    "filter.server": cmd_filter_server,
    "plan.new": cmd_plan_new,
    "plan.validate": cmd_plan_validate,
    "plan.preview": cmd_plan_preview,
    "plan.apply": cmd_plan_apply,
    "mcp": cmd_mcp,
}


def _add_report_parser(sub: Any) -> None:
    parser = sub.add_parser("report", help="run a report (table/md/csv/json, optional --out)")
    parser.add_argument("kind", choices=REPORT_KINDS, help="which report to run")
    parser.add_argument("--team", help="team name from teams.yaml")
    parser.add_argument("--users", help="comma separated usernames, or team:NAME")
    parser.add_argument("--user", help="a single username")
    parser.add_argument("--projects", help="comma separated project keys")
    parser.add_argument("--since", default="7d", help="start of the window (7d, 2w, YYYY-MM-DD)")
    parser.add_argument("--until", help="end of the window (default: today)")
    parser.add_argument("--days", type=int, default=7, help="staleness threshold in days")
    parser.add_argument("--board", type=int, help="board id (sprint, velocity)")
    parser.add_argument("--sprint", type=int, help="sprint id (default: the active sprint)")
    parser.add_argument("--last", type=int, default=5, help="how many sprints for velocity")
    parser.add_argument("--format", choices=output.REPORT_FORMATS, help="output format")
    parser.add_argument("--out", help="write the report to this file instead of stdout")


def _add_issue_parsers(sub: Any) -> None:
    issue = sub.add_parser("issue", help="create, inspect and change issues")
    issue_sub = issue.add_subparsers(dest="subcommand")

    show = issue_sub.add_parser("show", help="show one issue with its comments")
    show.add_argument("key")
    show.add_argument("--changelog", action="store_true", help="include the change history")

    create = issue_sub.add_parser("create", help="create an issue")
    create.add_argument("--project", required=True)
    create.add_argument("--summary", required=True)
    create.add_argument("--type", default="Task")
    create.add_argument("--assignee")
    create.add_argument("--priority")
    create.add_argument("--estimate", help="original estimate, e.g. 3h or 2d")
    create.add_argument("--due", help="due date, YYYY-MM-DD")
    create.add_argument("--label", action="append", help="repeatable")
    create.add_argument("--epic", help="epic issue key")
    create.add_argument("--parent", help="parent key (creates a sub-task)")
    create.add_argument("--desc", help="description text")
    create.add_argument("--components", help="comma separated component names")
    create.add_argument("--fix-versions", dest="fix_versions", help="comma separated versions")
    create.add_argument("--story-points", dest="story_points", type=float)
    create.add_argument("--sprint", type=int, help="sprint id to add the new issue to")

    edit = issue_sub.add_parser("edit", help="change fields on an existing issue")
    edit.add_argument("key")
    edit.add_argument("--summary")
    edit.add_argument("--desc")
    edit.add_argument("--priority")
    edit.add_argument("--due")
    edit.add_argument("--estimate")
    edit.add_argument("--remaining")
    edit.add_argument("--label", action="append", help="repeatable; replaces all labels")
    edit.add_argument("--components", help="comma separated component names")
    edit.add_argument("--fix-versions", dest="fix_versions", help="comma separated versions")
    edit.add_argument("--story-points", dest="story_points", type=float)

    assign = issue_sub.add_parser("assign", help="assign an issue (USER or none)")
    assign.add_argument("key")
    assign.add_argument("user")

    transitions = issue_sub.add_parser("transitions", help="list available transitions")
    transitions.add_argument("key")

    move = issue_sub.add_parser("move", help="transition an issue to a status")
    move.add_argument("key")
    move.add_argument("status")
    move.add_argument("--comment")

    comment = issue_sub.add_parser("comment", help="add a comment")
    comment.add_argument("key")
    comment.add_argument("text")

    comments = issue_sub.add_parser("comments", help="list comments")
    comments.add_argument("key")

    link = issue_sub.add_parser("link", help="link two issues")
    link.add_argument("from_key", metavar="FROM")
    link.add_argument("to_key", metavar="TO")
    link.add_argument("--type", default="Relates")

    issue_sub.add_parser("linktypes", help="list the server's link types")

    delete = issue_sub.add_parser("delete", help="delete an issue")
    delete.add_argument("key")

    worklog_parser = issue_sub.add_parser("worklog", help="log work on an issue")
    worklog_parser.add_argument("key")
    worklog_parser.add_argument("time", help="time spent, e.g. '2h 30m'")
    worklog_parser.add_argument("--comment")
    worklog_parser.add_argument("--started", help="YYYY-MM-DD (default: now)")

    worklogs = issue_sub.add_parser("worklogs", help="list the worklogs of an issue")
    worklogs.add_argument("key")

    estimate = issue_sub.add_parser("estimate", help="set the original/remaining estimate")
    estimate.add_argument("key")
    estimate.add_argument("--original")
    estimate.add_argument("--remaining")


def _add_project_parsers(sub: Any) -> None:
    parser = sub.add_parser("projects", help="projects, their metadata and creation")
    project_sub = parser.add_subparsers(dest="subcommand")

    project_sub.add_parser("list", help="list every visible project")

    show = project_sub.add_parser("show", help="show one project")
    show.add_argument("key")

    create = project_sub.add_parser("create", help="create a project")
    create.add_argument("key")
    create.add_argument("name")
    create.add_argument("--lead", required=True)
    create.add_argument("--template", default="kanban", choices=("kanban", "scrum", "basic"))
    create.add_argument("--description")

    component = project_sub.add_parser("component", help="create a component")
    component.add_argument("project")
    component.add_argument("name")
    component.add_argument("--lead")

    version = project_sub.add_parser("version", help="create a version")
    version.add_argument("project")
    version.add_argument("name")
    version.add_argument("--release-date", dest="release_date")

    types = project_sub.add_parser("types", help="list a project's issue types")
    types.add_argument("project")

    project_sub.add_parser("priorities", help="list the server's priorities")

    statuses = project_sub.add_parser("statuses", help="list a project's statuses")
    statuses.add_argument("project")


def _add_board_parsers(sub: Any) -> None:
    parser = sub.add_parser("boards", help="agile boards")
    board_sub = parser.add_subparsers(dest="subcommand")

    listing = board_sub.add_parser("list", help="list boards")
    listing.add_argument("--project")
    listing.add_argument("--type", choices=("kanban", "scrum"))

    show = board_sub.add_parser("show", help="show one board")
    show.add_argument("board_id", type=int, metavar="BOARD_ID")

    ensure = board_sub.add_parser("ensure", help="reuse or create a project's board")
    ensure.add_argument("key", metavar="PROJECT")
    ensure.add_argument("--type", default="kanban", choices=("kanban", "scrum"))
    ensure.add_argument("--name")

    board_issues = board_sub.add_parser("issues", help="issues on a board")
    board_issues.add_argument("board_id", type=int, metavar="BOARD_ID")
    board_issues.add_argument("--jql")
    board_issues.add_argument("--limit", type=int, default=200)

    sprints = sub.add_parser("sprints", help="list the sprints of a board")
    sprints.add_argument("board_id", type=int, metavar="BOARD_ID")
    sprints.add_argument("--state", default="active,future")

    sprint = sub.add_parser("sprint", help="create sprints and move issues in and out")
    sprint_sub = sprint.add_subparsers(dest="subcommand")

    active = sprint_sub.add_parser("active", help="show the active sprint of a board")
    active.add_argument("board_id", type=int, metavar="BOARD_ID")

    create = sprint_sub.add_parser("create", help="create a sprint")
    create.add_argument("board_id", type=int, metavar="BOARD_ID")
    create.add_argument("name")
    create.add_argument("--start")
    create.add_argument("--end")
    create.add_argument("--goal")

    add = sprint_sub.add_parser("add", help="add issues to a sprint")
    add.add_argument("sprint_id", type=int, metavar="SPRINT_ID")
    add.add_argument("keys", nargs="+", metavar="KEY")

    backlog = sprint_sub.add_parser("backlog", help="move issues back to the backlog")
    backlog.add_argument("keys", nargs="+", metavar="KEY")


def _add_people_parsers(sub: Any) -> None:
    users = sub.add_parser("users", help="find users on the server")
    users_sub = users.add_subparsers(dest="subcommand")

    search = users_sub.add_parser("search", help="search users by name or email")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)

    assignable = users_sub.add_parser("assignable", help="users assignable in a project")
    assignable.add_argument("project")
    assignable.add_argument("query", nargs="?")

    team = sub.add_parser("team", help="local teams (members, projects, boards)")
    team_sub = team.add_subparsers(dest="subcommand")

    team_sub.add_parser("list", help="list local teams")

    show = team_sub.add_parser("show", help="show one team")
    show.add_argument("name")

    team_set = team_sub.add_parser("set", help="create or replace a team")
    team_set.add_argument("name")
    team_set.add_argument("--members", required=True, help="comma separated usernames")
    team_set.add_argument("--projects", help="comma separated project keys")
    team_set.add_argument("--boards", help="comma separated board ids")

    remove = team_sub.add_parser("remove", help="remove a team")
    remove.add_argument("name")


def _add_filter_parsers(sub: Any) -> None:
    parser = sub.add_parser("filter", help="saved JQL filters")
    filter_sub = parser.add_subparsers(dest="subcommand")

    filter_sub.add_parser("list", help="list saved filters")

    save = filter_sub.add_parser("save", help="save a filter")
    save.add_argument("name")
    save.add_argument("jql")
    save.add_argument("--description")

    run = filter_sub.add_parser("run", help="run a saved filter")
    run.add_argument("name")
    run.add_argument("--limit", type=int, default=50)

    delete = filter_sub.add_parser("delete", help="delete a saved filter")
    delete.add_argument("name")

    filter_sub.add_parser("server", help="list the server's favourite filters")


def _add_plan_parsers(sub: Any) -> None:
    parser = sub.add_parser("plan", help="draft a task list in a file, then create it")
    plan_sub = parser.add_subparsers(dest="subcommand")

    for name, help_text in (
        ("new", "write a commented plan template"),
        ("validate", "check a plan against the server"),
        ("preview", "show what the plan would create"),
    ):
        child = plan_sub.add_parser(name, help=help_text)
        child.add_argument("file")
        if name == "preview":
            child.add_argument("--format", choices=output.REPORT_FORMATS)
            child.add_argument("--out")

    apply_parser = plan_sub.add_parser("apply", help="create the issues in a plan")
    apply_parser.add_argument("file")
    apply_parser.add_argument(
        "--dry-run", action="store_true", help="show what would happen and stop"
    )
    apply_parser.add_argument("--state", help="path of the ref->key state file")


def build_parser() -> argparse.ArgumentParser:
    """Build the whole command tree.  Every MCP capability has a command here."""
    parser = argparse.ArgumentParser(
        prog=_PROGRAM,
        description="Jira from the command line: issues, sprints, teams, reports and plans.",
    )
    parser.add_argument("--env-file", help="read configuration from this env file")
    parser.add_argument("--json", action="store_true", help="print raw JSON instead of text")
    parser.add_argument("--no-color", action="store_true", help="never emit ANSI colour")
    parser.add_argument("--yes", action="store_true", help="skip every confirmation prompt")
    parser.add_argument("--debug", action="store_true", help="print tracebacks on failure")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("me", help="show the authenticated user")
    sub.add_parser("doctor", help="check configuration and connectivity")
    sub.add_parser("mcp", help="run the MCP server over stdio")

    config_parser = sub.add_parser("config", help="inspect the resolved configuration")
    config_sub = config_parser.add_subparsers(dest="subcommand")
    config_sub.add_parser("show", help="print the settings (secrets redacted)")

    _add_project_parsers(sub)
    _add_board_parsers(sub)
    _add_issue_parsers(sub)
    _add_people_parsers(sub)
    _add_report_parser(sub)
    _add_filter_parsers(sub)
    _add_plan_parsers(sub)

    search = sub.add_parser("search", help="run a JQL query")
    search.add_argument("jql")
    search.add_argument("--limit", type=int, default=50)

    mine = sub.add_parser("mine", help="issues assigned to you")
    mine.add_argument("--all", action="store_true", help="include resolved issues")
    mine.add_argument("--limit", type=int, default=200)

    export_parser = sub.add_parser("export", help="re-render a saved JSON report")
    export_parser.add_argument("source", metavar="FILE")
    export_parser.add_argument("--format", choices=output.REPORT_FORMATS)
    export_parser.add_argument("--out")

    return parser


def _handler_key(args: argparse.Namespace) -> str:
    """Turn the parsed namespace into a key into :data:`HANDLERS`."""
    command = getattr(args, "command", None) or ""
    subcommand = getattr(args, "subcommand", None)
    return f"{command}.{subcommand}" if subcommand else command


# ----------------------------------------------------------------------- main


def _debug_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "debug", False)) or os.environ.get("JIRA_CLI_DEBUG") == "1"


def _fail(message: str, code: int, hint: str | None = None) -> int:
    """Print one redacted, terminal-safe ``error:`` line (plus hint) on stderr."""
    print(f"error: {terminal_safe_line(redact(message))}", file=sys.stderr)
    if hint:
        print(terminal_safe_line(hint), file=sys.stderr)
    return code


def _debug_dump(exc: BaseException) -> None:
    """Print ``exc``'s traceback, chained causes included, redacted and safe.

    ``traceback.print_exc`` would print ``__cause__``/``__context__`` messages
    verbatim (a wrapped ``requests`` error quotes the full URL, for example),
    so the whole text is formatted first and scrubbed as one block.
    """
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(terminal_safe(redact(text)), file=sys.stderr, end="")


def _os_error_message(exc: OSError) -> str:
    """``"<strerror>: <filename>"`` when the OS gave both, else ``str(exc)``."""
    if exc.strerror and exc.filename is not None:
        return f"{exc.strerror}: {exc.filename}"
    if exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return the process exit code.

    Never raises for a failure inside a command: every exception becomes one
    redacted ``error:`` line on stderr and a stable exit code. The traceback
    is printed (redacted) only with ``--debug`` or ``JIRA_CLI_DEBUG=1``.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    output.set_color(False if args.no_color else None)

    ctx = Context(args)
    key = _handler_key(args)
    if not key:
        handler = cmd_menu
    elif key in HANDLERS:
        handler = HANDLERS[key]
    else:
        # A group was named without a subcommand ("jira issue").
        parser.print_help(sys.stderr)
        return _fail(f"incomplete command: {key.replace('.', ' ').strip()}", EXIT_ERROR)

    debug = _debug_enabled(args)
    try:
        return int(handler(ctx))
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return EXIT_INTERRUPT
    except Exception as exc:  # noqa: BLE001 - the CLI's last-resort boundary
        code, message, hint = _classify_failure(exc, debug)
        if debug:
            _debug_dump(exc)
        return _fail(message, code, hint)


def _classify_failure(exc: Exception, debug: bool) -> tuple[int, str, str | None]:
    """Map an exception to ``(exit code, message, hint)`` for :func:`_fail`.

    Unknown exception types report only their class name: their message is
    unvetted and may carry anything (the traceback is there for ``--debug``).
    """
    if isinstance(exc, ConfigError):
        return EXIT_CONFIG, str(exc), None
    if isinstance(exc, AuthError):
        return EXIT_AUTH, str(exc), "run: jira doctor"
    if isinstance(exc, JiraError):
        return EXIT_ERROR, str(exc), None
    if isinstance(exc, OSError):
        return EXIT_IO, _os_error_message(exc), None
    hint = None if debug else "rerun with --debug for details"
    return EXIT_ERROR, f"unexpected {type(exc).__name__}", hint


if __name__ == "__main__":  # pragma: no cover - console_scripts is the real entry
    raise SystemExit(main())
