"""Menu section 10 - plan files: write a template, validate, preview and apply.

A plan file is a YAML document describing issues to create (see
``docs/PLAN_FORMAT.md``).  This section never writes to Jira before the operator
has seen exactly what would be created and answered ``y``:

* the plan file is picked from a numbered list (recently used files first, then
  ``*.yaml`` / ``*.yml`` in the current directory and in the per-user plans
  directory) or typed; a missing or broken file is reported and the path is
  asked again instead of leaving the section;
* a new template defaults to the per-user plans directory, never the working
  directory, so a plan naming assignees is not left where ``git add -A`` would
  commit it; it is created ``0600`` (its directory ``0700``);
* *Apply* always runs a dry run first, renders the issues the dry run plans to
  create, and only offers the real apply when the plan has no errors;
* story points that are not finite numbers (``.nan``, ``.inf`` in YAML) are
  rejected here, because the service would only fail once the request is sent;
* validation errors and warnings are listed under separate headings, so a
  warning such as "could not read the active sprint" cannot be missed.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from ..errors import InputError
from ..textsafe import terminal_safe_line
from .menu import (
    _ask,
    _choose,
    _confirm,
    _present_report,
    _preview_and_confirm,
    _render_rows,
    _Session,
    _svc,
)
from .menu_settings import default_plan_file

_ACTIONS = ("New plan file", "Validate", "Preview", "Apply (dry run first)")
_PLAN_PATTERNS = ("*.yaml", "*.yml")
#: Mode of a new plan file: it names assignees, so it is private by default.
_FILE_MODE = 0o600
#: Upper bound on files offered in the picker; more than this is typed instead.
_MAX_LISTED = 30
_TYPE_PATH = "Type a path..."
#: Session attribute holding the plan files used in this session, newest first.
_RECENT_ATTR = "recent_plan_files"
#: Columns ``apply_plan(dry_run=True)`` returns under ``planned``.
_DRY_RUN_COLUMNS = ["ref", "project", "type", "summary", "parent"]
_PLANNED_COLUMNS = [*_DRY_RUN_COLUMNS, "assignee", "sprint"]


def run(session: _Session) -> None:
    """Main-menu handler for *Plan files*."""
    index = _choose(session.io, "Plan files", list(_ACTIONS))
    plan_mod = _svc("plan")
    if index == 0:
        _write_plan_template(session, plan_mod, _ask_new_plan_path(session))
        return
    path, plan = _load_plan(session, plan_mod)
    if index == 1:
        _validate(session, plan_mod, plan)
    elif index == 2:
        _preview(session, plan_mod, plan)
    else:
        _apply(session, plan_mod, plan, path)


# ------------------------------------------------------------------ choosing a file


def _recent_files(session: _Session) -> list[Path]:
    return list(getattr(session, _RECENT_ATTR, None) or [])


def _remember(session: _Session, path: Path) -> None:
    """Put ``path`` at the front of this session's recent plan files."""
    resolved = path.resolve()
    recent = [p for p in _recent_files(session) if p != resolved]
    setattr(session, _RECENT_ATTR, [resolved, *recent][:10])


def _candidate_files(session: _Session) -> list[Path]:
    """Recent plan files that still exist, then YAML files in the current
    directory, then those in the per-user plans directory."""
    found: list[Path] = [p for p in _recent_files(session) if p.is_file()]
    for directory in (Path.cwd(), default_plan_file().parent):
        for path in _yaml_files(directory):
            if path not in found:
                found.append(path)
    return found[:_MAX_LISTED]


def _yaml_files(directory: Path) -> list[Path]:
    """``*.yaml`` / ``*.yml`` files directly in ``directory``, sorted."""
    if not directory.is_dir():
        return []
    return sorted({p.resolve() for pattern in _PLAN_PATTERNS for p in directory.glob(pattern) if p.is_file()})


def _label(path: Path, recent: list[Path]) -> str:
    try:
        shown = str(path.relative_to(Path.cwd().resolve()))
    except ValueError:
        shown = str(path)
    return f"{shown}  (recent)" if path in recent else shown


def _ask_plan_path(session: _Session) -> Path:
    """Pick an existing plan file from a numbered list, or type its path."""
    files = _candidate_files(session)
    if files:
        recent = _recent_files(session)
        options = [_label(path, recent) for path in files] + [_TYPE_PATH]
        index = _choose(session.io, "Plan file", options)
        if index < len(files):
            return files[index]
    while True:
        path = Path(_ask(session.io, "Plan file path", required=True)).expanduser()
        if path.is_file():
            return path
        reason = "is not a file" if path.exists() else "does not exist"
        session.say(f"{path} {reason}; type another path (b to go back)")


def _load_plan(session: _Session, plan_mod: Any) -> tuple[Path, dict]:
    """Ask for a plan file until one loads; a bad file re-asks instead of leaving."""
    while True:
        path = _ask_plan_path(session)
        try:
            plan = plan_mod.load_plan(path)
        except InputError as exc:
            session.say(f"error: {exc}")
            continue
        _remember(session, path)
        return path, plan


def _ask_new_plan_path(session: _Session) -> Path:
    """Ask where to write a new template.

    The default is :func:`default_plan_file`, shown in full; its directory is
    created on write.  Any other path needs an existing parent directory.
    """
    default = default_plan_file()
    while True:
        path = Path(_ask(session.io, "New plan file", default=str(default))).expanduser()
        if path.exists() and not path.is_file():
            session.say(f"{path} exists and is not a file; type another path")
            continue
        in_default_dir = path.resolve().parent == default.parent.resolve()
        if not in_default_dir and not path.resolve().parent.is_dir():
            session.say(f"directory {path.resolve().parent} does not exist; type another path")
            continue
        return path


def _write_plan_template(session: _Session, plan_mod: Any, path: Path) -> None:
    """Write a new plan template; an existing file is kept unless the operator says so.

    The preview names the absolute path, and the overwrite when there is one, so
    neither can be missed.
    """
    overwrite = path.exists()
    if overwrite and not _confirm(session.io, f"{terminal_safe_line(path)} already exists. Overwrite it?"):
        session.say(f"kept existing {path}; nothing was written")
        return
    template = plan_mod.new_plan_template()
    lines = [f"file {path.resolve()}"] + (["OVERWRITE existing file"] if overwrite else [])
    if not _preview_and_confirm(session, "Write plan template", lines):
        return
    # The plans directory is made 0700 by the same helper the export writer uses.
    _svc("export")._make_private_dirs(path.resolve().parent)
    # O_EXCL refuses a file that appeared since the check; O_TRUNC only after an
    # explicit yes.  A new file is 0600; an overwritten one keeps its mode.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    with os.fdopen(os.open(path, flags, _FILE_MODE), "w", encoding="utf-8") as handle:
        handle.write(template)
    _remember(session, path)
    session.say(f"written: {path.resolve()}")


# ------------------------------------------------------------------ problems


def _story_point_errors(plan_mod: Any, plan: dict) -> list[dict]:
    """Story points the service would accept as numbers but cannot send.

    ``validate_plan`` only checks the type, so YAML ``.nan``/``.inf`` pass and the
    apply later dies serialising the request.  Negative values make no sense either.
    """
    errors = []
    for issue in plan_mod.effective_issues(plan):
        points = issue.get("story_points")
        if not isinstance(points, (int, float)) or isinstance(points, bool):
            continue  # missing, or a non-number the service already reports
        if not math.isfinite(points) or points < 0:
            errors.append(
                {
                    "ref": issue.get("ref"),
                    "field": "story_points",
                    "message": f"story_points must be a finite number >= 0, got {points}",
                }
            )
    return errors


def _problem_line(marker: str, problem: dict) -> str:
    where = f"{problem.get('ref') or 'plan'}.{problem.get('field')}"
    return f"  {marker} {where}: {problem.get('message')}"


def _show_problems(session: _Session, errors: list[dict], warnings: list[dict]) -> None:
    """List errors and warnings under their own headings, errors first."""
    if errors:
        session.io.show(f"ERRORS ({len(errors)}) - the plan cannot be applied:")
        for problem in errors:
            session.say(_problem_line("x", problem))
    if warnings:
        session.io.show(f"WARNINGS ({len(warnings)}) - read these before applying:")
        for problem in warnings:
            session.say(_problem_line("!", problem))


def _checked(session: _Session, plan_mod: Any, plan: dict) -> tuple[list[dict], list[dict]]:
    """Run the service validation plus the local story-point check."""
    result = plan_mod.validate_plan(session.client, plan)
    errors = list(result.get("errors") or []) + _story_point_errors(plan_mod, plan)
    return errors, list(result.get("warnings") or [])


# ------------------------------------------------------------------ actions


def _validate(session: _Session, plan_mod: Any, plan: dict) -> None:
    errors, warnings = _checked(session, plan_mod, plan)
    if errors:
        session.io.show(f"plan has {len(errors)} error(s)")
    elif warnings:
        session.io.show(f"plan is valid, with {len(warnings)} warning(s)")
    else:
        session.io.show("plan is valid")
    _show_problems(session, errors, warnings)


def _preview(session: _Session, plan_mod: Any, plan: dict) -> None:
    """Show the estimate preview; it works on invalid plans, so flag bad points first."""
    _show_problems(session, _story_point_errors(plan_mod, plan), [])
    _present_report(session, plan_mod.preview_plan(plan, session.settings))


def _planned_rows(plan_mod: Any, plan: dict, planned: list[dict]) -> list[dict]:
    """The dry run's planned issues, with assignee and sprint from the plan itself."""
    by_ref = {issue.get("ref"): issue for issue in plan_mod.effective_issues(plan)}
    rows = []
    for item in planned:
        issue = by_ref.get(item.get("ref"), {})
        sprint = issue.get("sprint")
        board = issue.get("board")
        if sprint is not None and board is not None:
            sprint = f"{sprint} (board {board})"
        rows.append(
            {
                **{column: item.get(column) for column in _DRY_RUN_COLUMNS},
                "assignee": issue.get("assignee") or "(unassigned)",
                "sprint": sprint if sprint is not None else "(backlog)",
            }
        )
    return rows


def _state_line(plan_mod: Any, plan: dict) -> list[str]:
    state = plan_mod.default_state_path(plan)
    if state is not None and Path(state).exists():
        return [f"state file {state} exists: refs created earlier will be skipped"]
    return []


def _apply(session: _Session, plan_mod: Any, plan: dict, path: Path) -> None:
    """Dry run, show what would be created, and apply only on an explicit yes."""
    dry = plan_mod.apply_plan(session.client, plan, dry_run=True)
    validation = dry.get("validation") or {}
    errors = list(validation.get("errors") or []) + _story_point_errors(plan_mod, plan)
    warnings = list(validation.get("warnings") or [])
    if not dry.get("ok") or errors:
        session.io.show(f"dry run: plan has {len(errors)} error(s); nothing can be applied")
        _show_problems(session, errors, warnings)
        session.io.show("fix the plan file and run Apply again")
        return

    planned = list(dry.get("planned") or [])
    if not planned:
        session.io.show("dry run: the plan creates no issues; nothing to apply")
        return
    session.io.show(f"dry run: would create {len(planned)} issue(s), in this order")
    session.io.show(_render_rows(_PLANNED_COLUMNS, _planned_rows(plan_mod, plan, planned)))
    _show_problems(session, [], warnings)

    lines = [f"file         {path.resolve()}", f"would create {len(planned)} issue(s)"]
    lines += _state_line(plan_mod, plan)
    if warnings:
        lines.append(f"WARNING      {len(warnings)} warning(s) listed above")
    if not _preview_and_confirm(session, "Apply plan for real", lines):
        return
    _report_result(session, plan_mod.apply_plan(session.client, plan, dry_run=False))


def _report_result(session: _Session, result: dict) -> None:
    created = list(result.get("created") or [])
    skipped = list(result.get("skipped") or [])
    failed = list(result.get("failed") or [])
    if created:
        session.io.show(_render_rows(["ref", "key", "url"], created))
    if failed:
        session.io.show(_render_rows(["ref", "error"], failed))
    for warning in result.get("warnings") or []:
        session.say(f"warning: {warning}")
    session.say(f"created {len(created)}, skipped {len(skipped)}, failed {len(failed)}")
    if result.get("stopped"):
        session.io.show("the run stopped early; fix the problem and apply again to resume")
