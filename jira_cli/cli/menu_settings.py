"""Menu sections 13-15: saved filters, export of the last report, settings / doctor.

* **Saved filters** shows one numbered list — local filters first, then the
  server's favourite filters — and runs any of them by number or by name (names
  match case-insensitively).  Saving checks the JQL against the server with a
  ``maxResults=0`` count *before* the preview, so a typo is caught at the prompt
  instead of being stored.  Only local filters can be deleted.
* **Export last report** picks the format from a numbered list, re-asks on a bad
  answer, and rejects an unusable target (``.``, a directory, anything inside a
  ``.git`` directory) at the prompt, before the ``y/N``.  The preview shows the
  absolute path that will be written.  The offered default lives in the
  per-user data directory (:func:`default_export_dir`), never the working
  directory: run from inside a git checkout, ``./report.csv`` would sit where
  ``git add -A`` commits people and worklog data.
* **Settings / doctor** prints the settings (the secret is only ever described,
  never shown) and then runs exactly the checks ``jira doctor`` runs, by calling
  the same check functions.

Prompts follow the menu conventions: ``b`` backs out one step, input mistakes are
re-asked with a short reason, and only real server errors leave the section.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..errors import InputError, JiraError
from ..textsafe import terminal_safe_line
from .menu import (
    _ROOT,
    _as_path,
    _ask,
    _Back,
    _choose,
    _issues_report,
    _present_report,
    _preview_and_confirm,
    _render_rows,
    _Session,
    _svc,
)

#: Export formats in the order they are numbered on screen.
EXPORT_FORMATS = ("md", "csv", "json")

#: Names ``services.filters`` refuses even though they match its name pattern.
_RESERVED_FILTER_NAMES = (".", "..")

#: HTTP status Jira answers with when it cannot parse a JQL query.
_BAD_REQUEST = 400

LOCAL = "local"
SERVER = "server"


# ------------------------------------------------------- per-user data directory


def user_data_dir(environ: Mapping[str, str] | None = None, *, windows: bool | None = None) -> Path:
    """The per-user directory menu-written files default to (exports, plans).

    ``$XDG_DATA_HOME/jira-cli`` or ``~/.local/share/jira-cli``; on Windows
    ``%LOCALAPPDATA%\\jira-cli``.  A relative or empty variable is ignored,
    as the XDG spec requires, so the default can never fall back into the
    working directory.  Read at call time so tests can re-point it.
    """
    env = os.environ if environ is None else environ
    on_windows = os.name == "nt" if windows is None else windows
    name, fallback = (
        ("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        if on_windows
        else ("XDG_DATA_HOME", Path.home() / ".local" / "share")
    )
    raw = (env.get(name) or "").strip()
    base = Path(raw) if raw and Path(raw).is_absolute() else fallback
    return base / "jira-cli"


def default_export_dir() -> Path:
    """Where *Export* offers to write; created ``0700`` by the export writer."""
    return user_data_dir() / "exports"


def default_plan_file() -> Path:
    """Where *New plan file* offers to write the template."""
    return user_data_dir() / "plans" / "plan.yaml"


# ------------------------------------------------------------------ filters


@dataclass(frozen=True)
class _Filter:
    """One runnable filter, from the local file or the server's favourites."""

    source: str
    name: str
    jql: str
    description: str = ""


def run_filters(session: _Session) -> None:
    """Main-menu item 13: list, run, save and delete filters until ``b``."""
    server, server_note = _server_filters(session)
    actions: Sequence[tuple[str, Callable[[_Session, list[_Filter]], None]]] = (
        ("Run a filter", _run_filter),
        ("Save a new local filter", _save_filter),
        ("Delete a local filter", _delete_filter),
    )
    while True:
        filters = _local_filters(session) + server
        session.io.show(_filters_table(filters))
        if server_note:
            session.say(server_note)
        try:
            index = _choose(session.io, "Saved filters", [label for label, _ in actions])
        except _Back:
            return
        try:
            actions[index][1](session, filters)
        except _Back:
            continue  # backing out of an action returns to this list


def _filters_path(session: _Session) -> Path:
    return _as_path(getattr(session.settings, "filters_file", None), "filters.yaml")


def _local_filters(session: _Session) -> list[_Filter]:
    saved = _svc("filters").list_filters(_filters_path(session)) or {}
    return [
        _Filter(LOCAL, str(name), str(entry.get("jql") or ""), str(entry.get("description") or ""))
        for name, entry in saved.items()
        if isinstance(entry, dict) and entry.get("jql")
    ]


def _server_filters(session: _Session) -> tuple[list[_Filter], str | None]:
    """The server's favourite filters, plus a note when they could not be read.

    A failure here must not hide the local filters, so it becomes a one-line
    note under the list instead of ending the section.
    """
    try:
        favourites = _svc("filters").server_filters(session.client) or []
    except JiraError as exc:
        return [], f"server favourites unavailable: {exc.message or exc}"
    found = [
        _Filter(SERVER, str(item.get("name") or item.get("id") or "?"), str(item["jql"]))
        for item in favourites
        if isinstance(item, dict) and item.get("jql")
    ]
    return found, None


def _filters_table(filters: list[_Filter]) -> str:
    if not filters:
        return "no saved filters and no server favourites"
    rows = [
        {"#": number, "source": item.source, "name": item.name, "jql": item.jql}
        for number, item in enumerate(filters, start=1)
    ]
    return _render_rows(["#", "source", "name", "jql"], rows)


def _find_filter(filters: list[_Filter], answer: str) -> _Filter | None:
    """Look a filter up by its list number or, case-insensitively, by name."""
    if answer.isdigit():
        number = int(answer)
        return filters[number - 1] if 1 <= number <= len(filters) else None
    wanted = answer.casefold()
    return next((item for item in filters if item.name.casefold() == wanted), None)


def _ask_filter(session: _Session, filters: list[_Filter], prompt: str) -> _Filter:
    """Ask until the answer names a listed filter; ``b`` backs out."""
    while True:
        answer = _ask(session.io, prompt, required=True)
        found = _find_filter(filters, answer)
        if found is not None:
            return found
        session.say(f"no filter {answer!r}; type a number 1-{len(filters)} or a name (b to go back)")


def _run_filter(session: _Session, filters: list[_Filter]) -> None:
    if not filters:
        session.io.show("there is no filter to run; save one first")
        return
    chosen = _ask_filter(session, filters, "Run which filter (number or name)")

    def build() -> dict:
        issues = _svc("issues").search_issues(session.client, chosen.jql)
        return _issues_report(f"Filter: {chosen.name}", issues, jql=chosen.jql)

    _present_report(session, build(), refresh=build)


def _save_filter(session: _Session, filters: list[_Filter]) -> None:
    local = [item for item in filters if item.source == LOCAL]
    name, replaced = _ask_new_filter_name(session, local)
    jql, matches = _ask_checked_jql(session, replaced.jql if replaced else None)
    description = _ask(session.io, "Description (blank = none)")
    path = _filters_path(session)
    lines = [
        f"name        {name}",
        f"jql         {jql}",
        f"matches     {matches} issues now",
        f"description {description or '(none)'}",
        f"file        {Path(path).expanduser().absolute()}",
    ]
    if replaced:
        lines.append(f"replaces    the saved filter {replaced.name} ({replaced.jql})")
    if not _preview_and_confirm(session, "Save filter", lines):
        return
    _svc("filters").save_filter(path, name, jql, description or None)
    session.say(f"filter {name} saved")


def _ask_new_filter_name(session: _Session, local: list[_Filter]) -> tuple[str, _Filter | None]:
    """Ask for a valid filter name.

    Returns the name to store and the local filter it replaces, if any.  A name
    that differs only in case from a saved one reuses the saved spelling, so the
    file never holds two filters a user cannot tell apart.
    """
    pattern = _svc("filters").NAME_PATTERN
    while True:
        name = _ask(session.io, "Filter name", required=True)
        if not pattern.match(name) or name in _RESERVED_FILTER_NAMES:
            session.io.show("a filter name is 1-64 letters, digits, '_', '.' or '-' (b to go back)")
            continue
        wanted = name.casefold()
        existing = next((item for item in local if item.name.casefold() == wanted), None)
        return (existing.name if existing else name), existing


def _ask_checked_jql(session: _Session, default: str | None) -> tuple[str, int]:
    """Ask for JQL until the server accepts it; returns it with its match count.

    The check is a ``maxResults=0`` search (``client.count``), a read that is
    allowed in read-only mode.  Only "the server could not parse this" is
    re-asked; any other failure (auth, network) is a real error and propagates.
    """
    while True:
        jql = _ask(session.io, "JQL", default=default, required=True)
        try:
            return jql, int(session.client.count(jql))
        except InputError as exc:
            session.say(f"{exc.message}; try again (b to go back)")
        except JiraError as exc:
            if exc.status != _BAD_REQUEST:
                raise
            session.say(f"the server rejected this JQL: {exc.message}; try again (b to go back)")


def _delete_filter(session: _Session, filters: list[_Filter]) -> None:
    if not any(item.source == LOCAL for item in filters):
        session.io.show("there are no local filters to delete")
        return
    while True:
        chosen = _ask_filter(session, filters, "Delete which local filter (number or name)")
        if chosen.source == LOCAL:
            break
        session.say(f"{chosen.name} is a server favourite; only local filters can be deleted here")
    lines = [f"name {chosen.name}", f"jql  {chosen.jql}"]
    if not _preview_and_confirm(session, "Delete local filter", lines):
        return
    _svc("filters").delete_filter(_filters_path(session), chosen.name)
    session.say(f"filter {chosen.name} deleted")


# ------------------------------------------------------------------- export


def run_export_last(session: _Session) -> None:
    """Main-menu item 14: export the report shown last."""
    if not session.last_report:
        session.io.show("no report to export yet; run a report first")
        return
    export_report(session, session.last_report)


def export_report(session: _Session, report: dict) -> None:
    """Ask for a format and a file, preview the absolute target, then write."""
    try:
        fmt = _ask_format(session)
        target = _ask_target(session, report, fmt)
    except _Back:
        return
    lines = [
        f"title  {report.get('title')}",
        f"rows   {len(report.get('rows') or [])}",
        f"format {fmt}",
        f"file   {target}",
    ]
    if target.exists():
        lines.append("note   the existing file is replaced")
    if not _preview_and_confirm(session, "Export report", lines):
        return
    written = _svc("export").write_report(report, target, fmt)
    session.say(f"written: {written}")


def _ask_format(session: _Session) -> str:
    """Numbered format pick; accepts the number or the name, re-asks otherwise."""
    numbered = "\n".join(f"  {n}. {fmt}" for n, fmt in enumerate(EXPORT_FORMATS, start=1))
    session.io.show(f"Format\n{numbered}")
    while True:
        answer = _ask(session.io, "Format (number or name)", default=EXPORT_FORMATS[0]).casefold()
        if answer.isdigit() and 1 <= int(answer) <= len(EXPORT_FORMATS):
            return EXPORT_FORMATS[int(answer) - 1]
        if answer in EXPORT_FORMATS:
            return answer
        session.say(
            f"unknown format {answer!r}; type 1-{len(EXPORT_FORMATS)} or "
            f"{'/'.join(EXPORT_FORMATS)} (b to go back)"
        )


def _ask_target(session: _Session, report: dict, fmt: str) -> Path:
    """Ask for the output file until it is one ``write_report`` will accept.

    The rules (not ``.``, not a directory, never inside ``.git``) live in
    ``services.export``; they are applied here through its own check so the
    prompt and the writer can never disagree.  The default is an absolute
    path in :func:`default_export_dir`, shown in full at the prompt; a typed
    relative path still means the working directory.
    """
    export = _svc("export")
    stem = export.safe_filename(str(report.get("title") or "report").lower())
    default = default_export_dir() / f"{stem}.{fmt}"
    while True:
        raw = _ask(session.io, "Output file", default=str(default))
        try:
            return export._check_target(Path(raw).expanduser())
        except InputError as exc:
            session.say(f"{exc.message}; choose another file (b to go back)")


# ------------------------------------------------------------ settings / doctor


def run_settings(session: _Session) -> None:
    """Main-menu item 15: show the settings, then run the ``jira doctor`` checks."""
    session.io.show(_settings_text(session.settings))
    session.io.show("\nDoctor (same checks as `jira doctor`; reads only)")
    session.io.show(doctor_text(run_doctor_checks(session)))


def _settings_text(settings: Any) -> str:
    """Settings as ``describe_settings`` reports them: the secret is never shown."""
    described = importlib.import_module(f"{_ROOT}.config").describe_settings(settings, local=True)
    width = max((len(str(key)) for key in described), default=0)
    lines = ["Settings"]
    lines += [
        f"  {terminal_safe_line(key):<{width}}  {terminal_safe_line(value)}"
        for key, value in described.items()
    ]
    return "\n".join(lines)


def run_doctor_checks(session: _Session) -> list[dict[str, str]]:
    """Run every check of ``jira doctor`` and return ``[{status, check, detail}]``.

    The checks are ``cli.main._DOCTOR_CHECKS`` themselves; this only supplies the
    small context they read (``settings``, ``client``, ``args.env_file``).  The
    menu is not told about ``--env-file``, so the env file is located the way
    ``jira doctor`` does without that flag.  A check that raises is recorded as
    FAIL, exactly as ``cmd_doctor`` does.
    """
    checks = importlib.import_module(f"{_ROOT}.cli.main")._DOCTOR_CHECKS
    context = SimpleNamespace(
        settings=session.settings, client=session.client, args=SimpleNamespace(env_file=None)
    )
    results: list[dict[str, str]] = []

    def record(status: str, check: str, detail: str) -> None:
        results.append({"status": status, "check": check, "detail": detail})

    for check in checks:
        try:
            check(context, record)
        except JiraError as exc:
            record("FAIL", check.__name__.lstrip("_"), str(exc))
    return results


def doctor_text(results: list[dict[str, str]]) -> str:
    """PASS/WARN/FAIL lines plus a one-line total, all terminal-safe."""
    lines = [
        f"  {item['status']:<5} {terminal_safe_line(item['check']):<24} {terminal_safe_line(item['detail'])}"
        for item in results
    ]
    failed = sum(item["status"] == "FAIL" for item in results)
    warned = sum(item["status"] == "WARN" for item in results)
    lines.append(f"{failed} failed, {warned} warnings, {len(results)} checks")
    return "\n".join(lines)


__all__ = [
    "EXPORT_FORMATS",
    "doctor_text",
    "export_report",
    "run_doctor_checks",
    "run_export_last",
    "run_filters",
    "run_settings",
]
