"""Interactive terminal menu for jira-cli.

The menu is built for a manager who works by hand in a terminal: every screen is
numbered, ``q`` quits from the main menu, and ``b`` (or ``!back``) backs out of any
prompt, including a ``y/N`` question.  Every write shows a preview first and asks
``y/N``.

This module owns the main menu, the runner and the shared prompt helpers.  Each
section lives in its own ``jira_cli.cli.menu_<name>`` module, which imports those
helpers from here; the main menu imports a section only when it is chosen, so
there is no import cycle.

All I/O goes through :class:`MenuIO`, so the whole menu is testable offline by
passing a scripted implementation.  Service modules are imported lazily inside the
handlers, which keeps the import graph shallow and lets tests inject fakes through
``sys.modules``.

Server-controlled text (summaries, comments, names, error messages) is passed
through :mod:`jira_cli.textsafe` before it is shown, so an escape sequence in an
issue cannot rewrite a preview or reach the terminal.  Tables rendered by
``cli.output`` are already sanitised there and are shown as they are.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..redact import redact
from ..textsafe import terminal_safe, terminal_safe_line

_ROOT = (__package__ or "jira_cli.cli").rsplit(".", 1)[0]

try:  # pragma: no cover - exercised implicitly in both branches
    from ..errors import JiraError
except ImportError:  # pragma: no cover - core module not installed yet

    class JiraError(Exception):  # type: ignore[no-redef]
        """Fallback used only when ``jira_cli.errors`` cannot be imported."""


#: Answers that mean "go back one level" at any prompt.
BACK_WORDS = frozenset({"b", "!back"})
#: Answers that quit from the main menu.
QUIT_WORDS = frozenset({"q", "quit", "exit"})



class _Back(Exception):
    """Internal signal: the operator asked to go back one level."""


class _EndOfInput(BaseException):
    """Internal signal: the terminal's input is closed (Ctrl-D or end of a pipe).

    A ``BaseException`` on purpose, like ``KeyboardInterrupt``: no prompt and no
    section's ``except Exception`` may turn it into "back" and re-ask, because every
    further read would hit the same closed input.  Only :func:`run` catches it.
    """


# --------------------------------------------------------------------------- IO


class MenuIO:
    """Terminal I/O for the menu.  Subclass or replace it to script the menu."""

    def ask(self, prompt: str, default: str | None = None) -> str:
        """Read one line.  Returns the raw answer (``""`` when the user hit Enter)."""
        suffix = f" [{terminal_safe_line(default)}]" if default is not None else ""
        return _read_line(f"{terminal_safe(prompt)}{suffix}: ")

    def choose(self, title: str, options: Sequence[str]) -> int | None:
        """Show a numbered list and return the chosen index, or ``None`` to go back."""
        print(f"\n{terminal_safe_line(title)}")
        for number, option in enumerate(options, start=1):
            print(f"  {number:>2}. {terminal_safe_line(option)}")
        print("   b. back")
        while True:
            raw = _read_line("Choice: ").strip()
            if raw.casefold() in BACK_WORDS or not raw:
                return None
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            print("invalid choice")

    def confirm(self, prompt: str) -> bool:
        """Ask a yes/no question.  ``y`` is yes, ``b`` goes back, anything else is no."""
        answer = _read_line(f"{terminal_safe(prompt)} [y/N, b = back]: ").strip().casefold()
        if answer in BACK_WORDS:
            raise _Back
        return answer in ("y", "yes")

    def show(self, text: str) -> None:
        """Write a block of text to the operator.

        Text is passed through :func:`terminal_safe` here, so a call site that
        forgets to sanitise server text still cannot emit escape sequences.
        Stripping is idempotent: text that was already sanitised is unchanged.
        """
        print(terminal_safe(text))


def _read_line(prompt: str) -> str:
    """``input()``, with closed input raised as :class:`_EndOfInput`."""
    try:
        return input(prompt)
    except EOFError:
        print()
        raise _EndOfInput from None


def _ask(
    io: MenuIO,
    prompt: str,
    default: str | None = None,
    *,
    required: bool = False,
) -> str:
    """Ask for a line of text, translating back-words and Ctrl-C into :class:`_Back`."""
    while True:
        try:
            raw = io.ask(prompt, default)
        except (KeyboardInterrupt, EOFError):
            raise _Back from None
        text = (raw or "").strip()
        if not text and default is not None:
            text = str(default)
        if text.casefold() in BACK_WORDS:
            raise _Back
        if not text and required:
            io.show("a value is required (b to go back)")
            continue
        return text


def _choose(io: MenuIO, title: str, options: Sequence[str]) -> int:
    """Pick one option; going back or an out-of-range answer raises :class:`_Back`."""
    if not options:
        io.show(terminal_safe_line(f"{title}: nothing to choose from"))
        raise _Back
    try:
        index = io.choose(
            terminal_safe_line(title), [terminal_safe_line(option) for option in options]
        )
    except (KeyboardInterrupt, EOFError):
        raise _Back from None
    if not isinstance(index, int) or isinstance(index, bool):
        raise _Back
    if not 0 <= index < len(options):
        raise _Back
    return index


def _confirm(io: MenuIO, prompt: str) -> bool:
    """Ask ``y/N``.  ``b`` raises :class:`_Back`; Enter, ``n`` and Ctrl-C mean "no"."""
    try:
        return bool(io.confirm(prompt))
    except (KeyboardInterrupt, EOFError):
        return False


# ------------------------------------------------------------------- utilities


def _svc(name: str) -> Any:
    """Import a service module lazily (so fakes installed in ``sys.modules`` win)."""
    return importlib.import_module(f"{_ROOT}.services.{name}")


def _make_report(
    title: str,
    columns: list[str],
    rows: list[dict],
    *,
    summary: dict | None = None,
    meta: dict | None = None,
) -> dict:
    """Build the shared report shape, preferring ``jira_cli.report.make_report``."""
    try:
        report_mod = importlib.import_module(f"{_ROOT}.report")
        return report_mod.make_report(
            title, columns, rows, summary=summary, meta=meta
        )
    except (ImportError, AttributeError):
        # The core helper only normalises this dict; a local build keeps the menu
        # usable when jira_cli.report is unavailable.
        return {
            "title": title,
            "columns": columns,
            "rows": rows,
            "summary": summary or {},
            "meta": meta or {},
        }


def _plain_report(report: dict) -> str:
    """Minimal plain-text rendering, used when ``cli.output`` is unavailable."""
    columns = list(report.get("columns") or [])
    lines = [terminal_safe_line(report.get("title") or "report")]
    if columns:
        lines.append(" | ".join(terminal_safe_line(c) for c in columns))
    for row in report.get("rows") or []:
        lines.append(" | ".join(terminal_safe_line(row.get(c)) for c in columns))
    summary = report.get("summary") or {}
    if summary:
        lines.append(
            "summary: "
            + ", ".join(terminal_safe_line(f"{k}={v}") for k, v in summary.items())
        )
    if not report.get("rows"):
        lines.append("(no rows)")
    return "\n".join(lines)


def _render_report(report: dict) -> str:
    """Render a report through ``cli.output``, falling back to plain text."""
    try:
        output = importlib.import_module(f"{_ROOT}.cli.output")
        return output.render_report(report, "table")
    except (ImportError, AttributeError):
        return _plain_report(report)


def _render_rows(columns: list[str], rows: list[dict]) -> str:
    """Render a bare table through ``cli.output``, falling back to plain text."""
    try:
        output = importlib.import_module(f"{_ROOT}.cli.output")
        return output.render_table(columns, rows)
    except (ImportError, AttributeError):
        return _plain_report({"title": "", "columns": columns, "rows": rows})


_ISSUE_COLUMNS = ["key", "type", "status", "assignee", "priority", "due", "summary"]


def _issues_report(title: str, issues: list[dict], *, jql: str | None = None) -> dict:
    """Turn a list of normalised issues into the shared report shape."""
    rows = [{column: issue.get(column) for column in _ISSUE_COLUMNS} for issue in issues]
    meta = {"jql": [jql]} if jql else None
    return _make_report(
        title, _ISSUE_COLUMNS, rows, summary={"issues": len(rows)}, meta=meta
    )


def _issue_text(issue: dict) -> str:
    """Human-readable detail block for a single normalised issue.

    Every value comes from the server, so one-line fields are folded with
    :func:`terminal_safe_line` and multi-line bodies keep only their newlines.
    """
    line = terminal_safe_line
    lines = [
        f"{line(issue.get('key'))}  {line(issue.get('summary'))}",
        f"  url        {line(issue.get('url'))}",
    ]
    for label in ("type", "status", "priority", "assignee", "reporter", "due", "sprint", "epic", "parent"):
        value = issue.get(label)
        if value:
            lines.append(f"  {label:<10} {line(value)}")
    labels = issue.get("labels") or []
    if labels:
        lines.append("  labels     " + ", ".join(line(item) for item in labels))
    if issue.get("description"):
        lines.append("")
        lines.append(terminal_safe(issue["description"]))
    for comment in issue.get("comments") or []:
        lines.append(
            f"\n  -- {line(comment.get('author'))} ({line(comment.get('created'))})"
            f"\n  {terminal_safe(comment.get('body'))}"
        )
    return "\n".join(lines)


def _as_path(value: Any, fallback: str) -> Path:
    """Coerce a settings path (which may be missing) into a :class:`Path`."""
    return Path(str(value)) if value else Path(fallback)


def _split_list(text: str) -> list[str] | None:
    """Parse ``"A, B"`` into ``["A", "B"]``; empty input means "no filter"."""
    items = [part.strip() for part in text.split(",") if part.strip()]
    return items or None


# ------------------------------------------------------------------ session


@dataclass
class _Session:
    """Everything a handler needs, plus the little state the menu remembers."""

    client: Any
    settings: Any
    io: MenuIO
    last_report: dict | None = None
    last_key: str | None = None
    last_board: int | None = None
    last_jql: str | None = None
    #: Issue keys used in this session, newest first (kept by ``menu_actions``).
    recent_keys: list[str] = field(default_factory=list)

    def say(self, text: object) -> None:
        """Show text that may carry server data, with control characters removed."""
        self.io.show(terminal_safe(text))


# ------------------------------------------------------------- shared prompts


def _preview_and_confirm(session: _Session, title: str, lines: Sequence[str]) -> bool:
    """Show exactly what is about to be written and ask ``y/N``.

    Each line is folded to one terminal-safe line, so a value containing a
    newline or an escape sequence cannot fake or hide a preview line.
    """
    body = "\n".join(
        [terminal_safe_line(title), *(f"  {terminal_safe_line(line)}" for line in lines)]
    )
    session.io.show(body)
    if _confirm(session.io, "Proceed?"):
        return True
    session.io.show("cancelled; nothing was written")
    return False


def _pick_project(session: _Session) -> str:
    """Choose a project key from the server's project list."""
    projects = _svc("projects").list_projects(session.client)
    if not projects:
        return _ask(session.io, "Project key", required=True).upper()
    default = getattr(session.settings, "default_project", None)
    ordered = sorted(projects, key=lambda p: (p.get("key") != default, str(p.get("key"))))
    options = [f"{p.get('key')} - {p.get('name')}" for p in ordered]
    return str(ordered[_choose(session.io, "Project", options)].get("key"))


def _pick_priority(session: _Session) -> str | None:
    priorities = _svc("projects").list_priorities(session.client)
    options = ["(server default)"] + [str(p) for p in priorities]
    index = _choose(session.io, "Priority", options)
    return None if index == 0 else str(priorities[index - 1])


# ------------------------------------------------------- reports and exporting


def _present_report(
    session: _Session, report: dict, refresh: Callable[[], dict] | None = None
) -> None:
    """Show a report, then offer export / refresh / back until the operator leaves."""
    while True:
        session.last_report = report
        session.io.show(_render_report(report))
        try:
            answer = _ask(
                session.io, "e)xport md/csv/json, r)efresh, b)ack", default="b"
            ).casefold()
        except _Back:
            return
        if answer in ("e", "export"):
            _export_report(session, report)
        elif answer in ("r", "refresh"):
            if refresh is None:
                session.io.show("this report cannot be refreshed")
            else:
                report = refresh()
        else:
            session.io.show("type e, r or b")


def _export_report(session: _Session, report: dict) -> None:
    """Export ``report`` through the one shared export flow in ``menu_settings``.

    Kept under this name because section modules (``menu_find``) import it.
    """
    importlib.import_module(f"{_ROOT}.cli.menu_settings").export_report(session, report)


# ------------------------------------------------------------------- handlers


def _menu_my_issues(session: _Session) -> None:
    include_done = _confirm(session.io, "Include resolved issues?")

    def build() -> dict:
        issues = _svc("issues").my_issues(session.client, include_done=include_done)
        return _issues_report("My issues", issues)

    _present_report(session, build(), refresh=build)


def _section(module: str, function: str = "run") -> Callable[[_Session], None]:
    """A main-menu handler that runs ``jira_cli.cli.<module>.<function>(session)``.

    The section module is imported on first use, not here: every section imports
    its shared helpers from this module, so a top-level import would be a cycle.
    """

    def handler(session: _Session) -> None:
        section = importlib.import_module(f"{_ROOT}.cli.{module}")
        getattr(section, function)(session)

    handler.__qualname__ = handler.__name__ = f"{module}.{function}"
    return handler


# ------------------------------------------------------------------ main menu

_MAIN_MENU: list[tuple[str, str, Callable[[_Session], None]]] = [
    ("1", "My issues", _menu_my_issues),
    ("2", "Team dashboard", _section("menu_reports", "run_team_dashboard")),
    ("3", "Person activity", _section("menu_reports", "run_person_activity")),
    ("4", "Problems (stale / overdue / incomplete / unassigned)", _section("menu_reports", "run_problems")),
    ("5", "Workload and worklogs", _section("menu_reports", "run_workload")),
    ("6", "Sprint report and velocity", _section("menu_sprint")),
    ("7", "Boards overview", _section("menu_boards", "run_overview")),
    ("8", "Find issue (key, JQL or saved filter)", _section("menu_find")),
    ("9", "Create issue (guided)", _section("menu_create")),
    ("10", "Issue actions", _section("menu_actions")),
    ("11", "Plan files", _section("menu_plan")),
    ("12", "Teams", _section("menu_teams")),
    ("13", "Projects and boards", _section("menu_boards")),
    ("14", "Saved filters", _section("menu_settings", "run_filters")),
    ("15", "Export last report", _section("menu_settings", "run_export_last")),
    ("16", "Settings / doctor", _section("menu_settings", "run_settings")),
    ("17", "Project members", _section("menu_members")),
]

_MAIN_ACTIONS = {key: handler for key, _label, handler in _MAIN_MENU}


def main_menu_text() -> str:
    """The main menu as it is shown to the operator."""
    lines = ["", "jira-cli - main menu", ""]
    lines += [f"  {key:>2}. {label}" for key, label, _handler in _MAIN_MENU]
    lines += ["", "   q. quit"]
    return "\n".join(lines)


def _main_choice(io: MenuIO) -> str:
    """Show the main menu and read one choice; Ctrl-C or closed input here means quit."""
    io.show(main_menu_text())
    try:
        raw = io.ask("Choice", None)
    except (KeyboardInterrupt, EOFError, _EndOfInput):
        return "q"
    return (raw or "").strip().casefold()


def _error_line(exc: Exception) -> str:
    """One redacted line describing ``exc`` for an operator, never a traceback."""
    if isinstance(exc, JiraError):
        return f"error: {redact(exc)}"
    if isinstance(exc, OSError):
        reason = exc.strerror or type(exc).__name__
        where = f": {exc.filename}" if exc.filename is not None else ""
        return f"error: {redact(f'{reason}{where}')}"
    return f"error: unexpected {type(exc).__name__}: {redact(exc)}"


def _run_action(session: _Session, handler: Callable[[_Session], None]) -> bool:
    """Run one menu action.  Any error ends the action, never the session.

    A failing action (a Jira error, an unwritable file, a bug in a service)
    prints one redacted ``error:`` line and returns to the main menu, so the
    operator keeps the session and the last report.  Ctrl-C is not an error:
    it is a ``BaseException`` and only backs out to the main menu.

    Returns ``False`` when the input is closed and the menu should quit.
    """
    try:
        handler(session)
    except _Back:
        pass
    except KeyboardInterrupt:
        # Ctrl-C outside a prompt (during a service call) also means "back to the menu".
        session.io.show("")
    except (EOFError, _EndOfInput):
        return False
    except Exception as exc:  # noqa: BLE001 - the menu must survive any action failure
        session.say(_error_line(exc))
    return True


def run(client: Any, settings: Any, io: MenuIO | None = None) -> int:
    """Run the interactive menu until the operator quits.  Returns the exit code."""
    session = _Session(client=client, settings=settings, io=io or MenuIO())
    while True:
        choice = _main_choice(session.io)
        if choice in QUIT_WORDS:
            return 0
        if not choice or choice in BACK_WORDS:
            continue
        handler = _MAIN_ACTIONS.get(choice)
        if handler is None:
            session.say(f"invalid choice: {choice}")
            continue
        if not _run_action(session, handler):
            return 0
