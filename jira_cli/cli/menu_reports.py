"""Menu sections for the read-only reports: team dashboard, person activity,
problems and workload/worklogs.

Every value is picked or validated at its own prompt, so nothing the operator
typed is lost to a late error:

* people come from the shared numbered picker (``menu_people``), so an unknown
  or misspelt username can no longer reach a JQL query;
* projects come from the shared project picker, where ``all`` really means all
  projects;
* since/until/days are checked right where they are typed and asked again with
  a short reason.

``b`` at any prompt goes back one step (to the previous prompt); ``b`` at the
first prompt leaves the section.  Before each report a one-line header says
which projects and date window it covers.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from ..durations import resolve_date
from ..errors import InputError, JiraError
from ..textsafe import terminal_safe_line
from .menu import _ROOT, _ask, _Back, _choose, _present_report, _Session, _svc

DATE_FORMATS = "7d, 2w, 1m, today, yesterday, YYYY-MM-DD"
DEFAULT_SINCE = "7d"
DEFAULT_STALE_DAYS = 7
MAX_STALE_DAYS = 3650
#: Names listed in the header before it switches to a count.
_HEADER_MAX_NAMES = 5
#: Longest server error shown in the one-line person-activity failure message.
_ERROR_CHARS = 240


def _today() -> date:
    """Reference date for date validation (tests replace it)."""
    return date.today()


def _people_menu():
    """The shared people/project pickers, imported lazily so tests can fake them."""
    return importlib.import_module(f"{_ROOT}.cli.menu_people")


# ------------------------------------------------------------------ answers


@dataclass
class _Scope:
    """What a report covers; filled in step by step by the prompts."""

    projects: list[str] | None = None
    people: list[str] | None = None
    since: str | None = None
    until: str | None = None
    days: int | None = None

    @property
    def focus_project(self) -> str | None:
        """First chosen project, used to put its members first in the people picker."""
        return self.projects[0] if self.projects else None

    def header(self) -> str:
        """One line naming the projects, window and people this report covers."""
        parts = ["projects: " + (", ".join(self.projects) if self.projects else "all projects")]
        if self.since:
            parts.append(f"from {self.since} to {self.until or 'today'}")
        if self.days is not None:
            parts.append(f"not updated for {self.days}+ days")
        if self.people:
            if len(self.people) <= _HEADER_MAX_NAMES:
                parts.append("people: " + ", ".join(self.people))
            else:
                parts.append(f"people: {len(self.people)}")
        return terminal_safe_line("Covers " + " | ".join(parts))


def _dedupe_casefold(names: Sequence[str]) -> list[str]:
    """Drop blanks and case-insensitive repeats, keeping the first spelling seen.

    Jira usernames are case-insensitive, so ``bob`` and ``BOB`` would otherwise
    become two rows that count the same issues twice.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        text = str(name or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            unique.append(text)
    return unique


def _run_steps(steps: Sequence[Callable[[], None]], start: int = 0) -> None:
    """Run prompts in order; ``b`` re-runs the previous prompt, ``b`` at the first leaves."""
    index = start
    while index < len(steps):
        try:
            steps[index]()
        except _Back:
            if index == 0:
                raise
            index -= 1
            continue
        index += 1


# ------------------------------------------------------------------ prompts


def _ask_projects(session: _Session, scope: _Scope) -> None:
    chosen = _people_menu().ask_projects(session, "Projects")
    scope.projects = _dedupe_casefold(chosen or []) or None


def _ask_people(session: _Session, scope: _Scope) -> None:
    while True:
        chosen = _people_menu().ask_people(session, "Users", project=scope.focus_project)
        scope.people = _dedupe_casefold(chosen or [])
        if scope.people:
            return
        session.io.show("pick at least one person (b to go back)")


def _ask_person(session: _Session, scope: _Scope) -> None:
    username = _people_menu().ask_person(session, "Person", project=scope.focus_project)
    scope.people = [str(username)]


def _read_date(session: _Session, prompt: str, default: str | None) -> str:
    """Ask for a date expression until it resolves to ``YYYY-MM-DD``.

    Returns ``""`` only when the prompt has no default and the answer is blank.
    """
    while True:
        text = _ask(session.io, prompt, default=default)
        if not text:
            return ""
        try:
            return resolve_date(text, today=_today())
        except InputError as exc:
            session.io.show(terminal_safe_line(f"{exc.message} - try again"))


def _ask_since(session: _Session, scope: _Scope) -> None:
    while True:
        day = _read_date(session, f"Since ({DATE_FORMATS})", DEFAULT_SINCE)
        if day > _today().isoformat():
            session.io.show(f"since {day} is in the future; pick today or earlier")
            continue
        scope.since = day
        return


def _ask_until(session: _Session, scope: _Scope) -> None:
    while True:
        day = _read_date(session, f"Until ({DATE_FORMATS}; blank = today)", None)
        if day and scope.since and day < scope.since:
            session.io.show(f"until {day} is before since {scope.since}; pick a later date")
            continue
        scope.until = day or None
        return


def _ask_days(session: _Session, scope: _Scope) -> None:
    while True:
        text = _ask(
            session.io,
            f"Not updated for how many days (1-{MAX_STALE_DAYS})",
            default=str(DEFAULT_STALE_DAYS),
        )
        digits = text.casefold().removesuffix("d").strip()
        if not digits.isdecimal():
            session.io.show("days must be a whole number such as 7 or 30")
            continue
        days = int(digits)
        if not 1 <= days <= MAX_STALE_DAYS:
            session.io.show(f"days must be between 1 and {MAX_STALE_DAYS}")
            continue
        scope.days = days
        return


def _with_header(session: _Session, scope: _Scope, build: Callable[[], dict]) -> Callable[[], dict]:
    """Wrap ``build`` so the scope header is shown before every (re)run."""

    def run() -> dict:
        session.io.show(scope.header())
        return build()

    return run


def _present(session: _Session, scope: _Scope, build: Callable[[], dict]) -> None:
    runner = _with_header(session, scope, build)
    _present_report(session, runner(), refresh=runner)


def _ask_scope(session: _Session, *extra: Callable[[_Session, _Scope], None]) -> _Scope:
    """Projects first, then ``extra`` prompts in order, with one-step back."""
    scope = _Scope()
    _run_steps(
        [lambda: _ask_projects(session, scope)] + [lambda step=step: step(session, scope) for step in extra]
    )
    return scope


def _pick_then_ask(
    session: _Session,
    title: str,
    options: Sequence[str],
    prompts_for: Callable[[int], Sequence[Callable[[_Session, _Scope], None]]],
) -> tuple[int, _Scope]:
    """Choose a sub-report, then ask its prompts; ``b`` at the first prompt re-shows the list."""
    while True:
        index = _choose(session.io, title, list(options))
        try:
            return index, _ask_scope(session, *prompts_for(index))
        except _Back:
            continue


# ------------------------------------------------------------------ sections


def run_team_dashboard(session: _Session) -> None:
    """Menu item: per-person counts for a team over a since-window."""
    scope = _ask_scope(session, _ask_people, _ask_since)

    def build() -> dict:
        return _svc("reports").team_dashboard(
            session.client, scope.people, projects=scope.projects, since=scope.since
        )

    _present(session, scope, build)


def run_person_activity(session: _Session) -> None:
    """Menu item: what one person did in a window.

    When the server rejects the query for the chosen person, the error is shown
    on one line and the person picker is offered again; projects and dates are
    kept.
    """
    scope = _Scope()
    steps = [
        lambda: _ask_projects(session, scope),
        lambda: _ask_person(session, scope),
        lambda: _ask_since(session, scope),
        lambda: _ask_until(session, scope),
    ]
    person_step = 1
    start = 0
    while True:
        _run_steps(steps, start)

        def build() -> dict:
            return _svc("reports").person_activity(
                session.client,
                scope.people[0],
                since=scope.since,
                until=scope.until,
                projects=scope.projects,
            )

        runner = _with_header(session, scope, build)
        try:
            report = runner()
        except JiraError as exc:
            message = terminal_safe_line(str(exc))[:_ERROR_CHARS]
            session.io.show(f"error: {message}")
            session.io.show("pick another person (b to go back)")
            start = person_step
            continue
        _present_report(session, report, refresh=runner)
        return


_PROBLEMS = ("Stale issues", "Overdue issues", "Incomplete issues", "Unassigned issues")


def run_problems(session: _Session) -> None:
    """Menu item: stale, overdue, incomplete or unassigned issues."""
    index, scope = _pick_then_ask(session, "Problems", _PROBLEMS, lambda i: [_ask_days] if i == 0 else [])
    reports = _svc("reports")
    client = session.client
    builders: dict[int, Callable[[], dict]] = {
        0: lambda: reports.stale_issues(client, days=scope.days, projects=scope.projects),
        1: lambda: reports.overdue_issues(client, projects=scope.projects),
        2: lambda: reports.incomplete_issues(client, projects=scope.projects),
        3: lambda: reports.unassigned_issues(client, scope.projects),
    }
    _present(session, scope, builders[index])


def run_workload(session: _Session) -> None:
    """Menu item: open workload per person, or hours logged in a window."""
    index, scope = _pick_then_ask(
        session,
        "Workload and worklogs",
        ("Workload", "Worklog report"),
        lambda i: [_ask_people] if i == 0 else [_ask_people, _ask_since, _ask_until],
    )
    if index == 0:

        def build() -> dict:
            return _svc("reports").workload(session.client, scope.people, projects=scope.projects)

    else:

        def build() -> dict:
            return _svc("worklog").worklog_report(
                session.client,
                scope.people,
                since=scope.since,
                until=scope.until,
                projects=scope.projects,
            )

    _present(session, scope, build)


__all__ = [
    "run_person_activity",
    "run_problems",
    "run_team_dashboard",
    "run_workload",
]
