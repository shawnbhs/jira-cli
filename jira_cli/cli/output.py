"""Rendering helpers for the non-interactive jira-cli commands.

Everything here is pure text: no service module is imported at module import
time, so this module stays cheap and safe to import from both
:mod:`jira_cli.cli.main` and :mod:`jira_cli.cli.menu`.

Every human-readable string that may carry server-controlled text (issue
summaries, comments, display names, report cells) is passed through
:mod:`jira_cli.textsafe` before it is printed, so an escape sequence planted
in Jira cannot recolour, erase or rewrite the terminal, set the clipboard
(OSC 52) or reorder a line with bidi overrides.  JSON output keeps the data
intact but escapes C1 and bidi characters as ``\\uXXXX``.

Width handling is deliberate.  Jira summaries routinely mix Latin, CJK and
right-to-left scripts, and ``len()`` is the wrong measure for all but the
first: CJK ideographs occupy two terminal cells and combining marks occupy
none.  :func:`display_width` measures cells, and every pad/truncate decision
in this module is made in cells so that a table stays aligned.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from ..textsafe import terminal_safe, terminal_safe_line

#: Appended to a value that had to be cut to fit its column (one cell wide).
ELLIPSIS = "\u2026"

#: Printed between two table columns.
COLUMN_GAP = "  "

#: A column is never squeezed below this many cells.
MIN_COLUMN_WIDTH = 3

#: Used when the terminal size cannot be determined (not a tty, CI, pipe).
FALLBACK_TERMINAL_WIDTH = 100

#: Report formats :func:`render_report` understands.
REPORT_FORMATS = ("table", "md", "csv", "json")

#: Characters ``json.dumps(..., ensure_ascii=False)`` leaves raw but a
#: terminal may act on: DEL, the C1 block (U+0080-U+009F, including the
#: 8-bit CSI U+009B) and the bidi embedding/override/isolate controls.
#: ``json.dumps`` already escapes every C0 control, so newlines and the
#: indentation outside strings are untouched by this pattern.
_JSON_UNSAFE_RE = re.compile("[\x7f-\x9f\u202a-\u202e\u2066-\u2069]")

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
}

#: ``None`` means "decide from the environment"; ``True``/``False`` force it.
_color_override: bool | None = None


# --------------------------------------------------------------------- colour


def set_color(enabled: bool | None) -> None:
    """Force colour on (``True``), off (``False``) or auto-detect (``None``)."""
    global _color_override
    _color_override = enabled


def colors_enabled() -> bool:
    """True when ANSI colour may be written to stdout right now."""
    if _color_override is not None:
        return _color_override
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(sys.stdout, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):  # closed or detached stream
        return False


def color(text: str, name: str) -> str:
    """Wrap ``text`` in the ANSI sequence ``name``, or return it unchanged.

    Colour is dropped when stdout is not a terminal, when ``NO_COLOR`` is set,
    or when ``--no-color`` called :func:`set_color` with ``False``.
    """
    sequence = _ANSI.get(name)
    if sequence is None or not colors_enabled():
        return text
    return f"{sequence}{text}{_ANSI['reset']}"


# ----------------------------------------------------------------- json safety


def escape_json_controls(text: str) -> str:
    """Escape terminal-active characters left raw in JSON text as ``\\uXXXX``.

    These characters can only occur inside JSON string literals, where the
    ``\\uXXXX`` form is equivalent, so ``json.loads`` returns the same data.
    """
    return _JSON_UNSAFE_RE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def dumps_json(value: Any, *, indent: int | None = 2) -> str:
    """``json.dumps`` that keeps readable Unicode but never emits raw C1/bidi."""
    return escape_json_controls(json.dumps(value, ensure_ascii=False, indent=indent, default=str))


# ---------------------------------------------------------------------- width


def display_width(text: str) -> int:
    """Number of terminal cells ``text`` occupies.

    Wide and full-width East Asian characters count as two cells; combining
    marks (including the Arabic/Persian diacritics) count as zero.
    """
    width = 0
    for char in str(text):
        if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate(text: str, width: int) -> str:
    """Cut ``text`` to at most ``width`` cells, marking the cut with an ellipsis."""
    text = str(text)
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return ELLIPSIS
    budget = width - 1  # one cell reserved for the ellipsis
    taken: list[str] = []
    used = 0
    for char in text:
        char_width = display_width(char)
        if used + char_width > budget:
            break
        taken.append(char)
        used += char_width
    return "".join(taken) + ELLIPSIS


def pad(text: str, width: int) -> str:
    """Left-align ``text`` in a field of ``width`` cells (measured, not counted)."""
    return str(text) + " " * max(0, width - display_width(text))


def terminal_width(fallback: int = FALLBACK_TERMINAL_WIDTH) -> int:
    """Usable terminal width, falling back to a sane default when unknown."""
    try:
        columns = shutil.get_terminal_size(fallback=(fallback, 24)).columns
    except (ValueError, OSError):  # pragma: no cover - platform dependent
        return fallback
    return columns if columns and columns > 20 else fallback


# ---------------------------------------------------------------------- cells


def format_cell(value: Any) -> str:
    """Render one report value as a single-line, terminal-safe string.

    ``None`` becomes ``-``.  Control characters (escape sequences, carriage
    returns, bidi overrides) are removed and newlines fold to spaces, so a
    cell can neither fake an extra row nor drive the terminal.
    """
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(format_cell(item) for item in value)
    if isinstance(value, Mapping):
        return ", ".join(
            f"{terminal_safe_line(key)}={format_cell(item)}" for key, item in value.items()
        )
    return terminal_safe_line(str(value).replace("\r\n", " ").replace("\r", " "))


def _matrix(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    """Turn row dicts into a list of string cells, one list per row."""
    return [[format_cell(row.get(column)) for column in columns] for row in rows]


def _column_widths(
    headers: Sequence[str], body: Sequence[Sequence[str]], max_width: int
) -> list[int]:
    """Natural column widths, shrunk from the widest until the table fits."""
    widths = [display_width(header) for header in headers]
    for cells in body:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], display_width(cell))
    if not widths:
        return widths
    gaps = len(COLUMN_GAP) * (len(widths) - 1)
    while sum(widths) + gaps > max_width:
        widest = max(range(len(widths)), key=lambda i: widths[i])
        if widths[widest] <= MIN_COLUMN_WIDTH:
            break  # nothing left to give; the row will simply overflow
        widths[widest] -= 1
    return widths


def render_table(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    max_width: int | None = None,
) -> str:
    """Render ``rows`` as an aligned plain-text table.

    ``columns`` are the dict keys to show, in order.  ``max_width`` defaults to
    the terminal width; columns are squeezed (widest first) and values are
    truncated with an ellipsis so the output never wraps.
    """
    columns = list(columns)
    if not columns:
        return "(no columns)"
    body = _matrix(columns, list(rows))
    headers = [terminal_safe_line(column) for column in columns]
    limit = max_width if max_width and max_width > 0 else terminal_width()
    widths = _column_widths(headers, body, limit)

    lines = [
        COLUMN_GAP.join(
            pad(truncate(header, width), width)
            for header, width in zip(headers, widths, strict=False)
        ).rstrip()
    ]
    lines.append(COLUMN_GAP.join("-" * width for width in widths))
    for cells in body:
        lines.append(
            COLUMN_GAP.join(
                pad(truncate(cell, width), width)
                for cell, width in zip(cells, widths, strict=False)
            ).rstrip()
        )
    if not body:
        lines.append("(no rows)")
    return "\n".join(lines)


# --------------------------------------------------------------------- reports


def _summary_lines(summary: Mapping[str, Any]) -> list[str]:
    return [
        f"  {terminal_safe_line(key):<22} {format_cell(value)}" for key, value in summary.items()
    ]


def report_to_table(report: Mapping[str, Any], max_width: int | None = None) -> str:
    """Human-readable rendering of the shared report shape."""
    columns = list(report.get("columns") or [])
    rows = list(report.get("rows") or [])
    parts = [terminal_safe_line(report.get("title") or "report"), ""]
    parts.append(render_table(columns, rows, max_width) if columns else "(no columns)")
    summary = report.get("summary") or {}
    if summary:
        parts += ["", "summary:", *_summary_lines(summary)]
    meta = report.get("meta") or {}
    jql = meta.get("jql") if isinstance(meta, Mapping) else None
    for query in jql or []:
        if query:
            parts.append(f"jql: {terminal_safe_line(query)}")
    return "\n".join(parts)


def report_to_markdown(report: Mapping[str, Any]) -> str:
    """Markdown rendering, used when ``services.export`` is unavailable."""
    columns = list(report.get("columns") or [])
    rows = list(report.get("rows") or [])
    lines = [f"# {terminal_safe_line(report.get('title') or 'report')}", ""]
    if columns:
        headers = [terminal_safe_line(column).replace("|", "\\|") for column in columns]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows:
            cells = [format_cell(row.get(column)).replace("|", "\\|") for column in columns]
            lines.append("| " + " | ".join(cells) + " |")
    summary = report.get("summary") or {}
    if summary:
        lines += ["", "## Summary", ""]
        lines += [
            f"- **{terminal_safe_line(key)}**: {format_cell(value)}"
            for key, value in summary.items()
        ]
    return "\n".join(lines) + "\n"


def report_to_csv(report: Mapping[str, Any]) -> str:
    """CSV rendering, used when ``services.export`` is unavailable."""
    columns = list(report.get("columns") or [])
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([terminal_safe_line(column) for column in columns])
    for row in report.get("rows") or []:
        writer.writerow([format_cell(row.get(column)) for column in columns])
    return buffer.getvalue()


def report_to_json(report: Mapping[str, Any]) -> str:
    """JSON rendering of the whole report document (C1/bidi escaped)."""
    return dumps_json(report)


def _export_module() -> Any | None:
    """``jira_cli.services.export`` when it is importable, else ``None``."""
    import importlib

    root = (__package__ or "jira_cli.cli").rsplit(".", 1)[0]
    try:
        return importlib.import_module(f"{root}.services.export")
    except ImportError:
        return None


def render_report(
    report: Mapping[str, Any], fmt: str = "table", max_width: int | None = None
) -> str:
    """Render a report in ``fmt`` (``table``, ``md``, ``csv`` or ``json``).

    ``md``/``csv``/``json`` prefer ``services.export`` so that a file written
    with ``--out`` and text printed to stdout are byte-identical; the local
    renderers above are the fallback when that module is absent.  Whatever
    renderer runs, the returned text is terminal-safe: ``md``/``csv`` lose
    control characters and ``json`` has C1/bidi characters escaped.  (For a
    report with no control characters the text is unchanged.)
    """
    fmt = (fmt or "table").lower()
    if fmt not in REPORT_FORMATS:
        raise ValueError(f"unknown report format: {fmt}")
    if fmt == "table":
        return report_to_table(report, max_width)
    export = _export_module()
    function = {"md": "to_markdown", "csv": "to_csv", "json": "to_json"}[fmt]
    renderer = getattr(export, function, None) if export is not None else None
    if not callable(renderer):
        renderer = {"md": report_to_markdown, "csv": report_to_csv, "json": report_to_json}[fmt]
    text = str(renderer(report))
    if fmt == "json":
        return escape_json_controls(text)
    # CSV uses CRLF row endings by convention; keep them, drop every other CR.
    return "\r\n".join(terminal_safe(part) for part in text.split("\r\n"))


# ---------------------------------------------------------------------- issues

_ISSUE_FIELDS = (
    ("type", "type"),
    ("status", "status"),
    ("priority", "priority"),
    ("assignee", "assignee"),
    ("reporter", "reporter"),
    ("project", "project"),
    ("sprint", "sprint"),
    ("epic", "epic"),
    ("parent", "parent"),
    ("due", "due"),
    ("created", "created"),
    ("updated", "updated"),
    ("resolved", "resolved"),
    ("story_points", "story points"),
)

_ISSUE_DURATIONS = (
    ("estimate_seconds", "estimate"),
    ("remaining_seconds", "remaining"),
    ("spent_seconds", "spent"),
)


def _duration(seconds: Any) -> str:
    """Format a second count, preferring ``jira_cli.durations.format_duration``."""
    if not seconds:
        return "-"
    import importlib

    root = (__package__ or "jira_cli.cli").rsplit(".", 1)[0]
    try:
        durations = importlib.import_module(f"{root}.durations")
        return str(durations.format_duration(int(seconds)))
    except (ImportError, AttributeError, TypeError, ValueError):
        return f"{int(seconds) // 3600}h"


def issue_text(issue: Mapping[str, Any]) -> str:
    """Detail block for one normalised issue, as :func:`print_issue` prints it."""
    heading = f"{terminal_safe_line(str(issue.get('key')))}  {terminal_safe_line(str(issue.get('summary')))}"
    url = terminal_safe_line(issue.get("url") or "-")
    lines = [color(heading, "bold"), f"  {'url':<14} {url}"]
    for key, label in _ISSUE_FIELDS:
        value = issue.get(key)
        if value not in (None, "", []):
            lines.append(f"  {label:<14} {format_cell(value)}")
    for key, label in _ISSUE_DURATIONS:
        if issue.get(key):
            lines.append(f"  {label:<14} {_duration(issue.get(key))}")
    labels = issue.get("labels") or []
    if labels:
        lines.append(f"  {'labels':<14} {', '.join(terminal_safe_line(item) for item in labels)}")
    if issue.get("description"):
        lines += ["", terminal_safe(issue["description"])]
    comments = issue.get("comments") or []
    if comments:
        lines += ["", color(f"comments ({len(comments)})", "bold")]
        for comment in comments:
            author = terminal_safe_line(str(comment.get("author")))
            created = terminal_safe_line(str(comment.get("created")))
            lines.append(f"  -- {author} ({created})")
            for line in terminal_safe(comment.get("body") or "").splitlines() or [""]:
                lines.append(f"     {line}")
    return "\n".join(lines)


def print_issue(issue: Mapping[str, Any], stream: Any = None) -> None:
    """Print one normalised issue as a readable detail block."""
    print(issue_text(issue), file=stream or sys.stdout)


# ------------------------------------------------------------------ confirming


def confirm(prompt: str, assume_yes: bool = False) -> bool:
    """Ask ``prompt`` and return True only on an explicit yes.

    With ``assume_yes`` the question is echoed and answered ``y`` so that a
    non-interactive run still records what it agreed to.  A closed stdin, EOF
    or Ctrl-C all mean "no": a write is never performed on a guess.
    """
    question = f"{terminal_safe(prompt)} [y/N]: "
    if assume_yes:
        print(f"{question}y")
        return True
    try:
        answer = input(question)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().casefold() in ("y", "yes")
