"""Terminal rendering for board summaries and a single board's overview.

Pure text: this module only formats dicts that ``services`` already built, so
it never touches the network.  Every string that came from the server (board,
column, sprint and person names, issue titles, error messages) goes through
:func:`jira_cli.textsafe.terminal_safe_line` before it is measured or printed.

Widths are measured in terminal cells (:func:`output.display_width`), never
with ``len()``, so Persian and CJK titles keep the columns aligned.  The rule
everywhere is *abbreviate, don't wrap*: names are shortened with an ellipsis
until a line fits.  The one exception is the header count strip, which may
spill onto extra lines when even abbreviated names cannot fit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import InputError
from ..textsafe import terminal_safe_line
from .output import COLUMN_GAP, ELLIPSIS, display_width, render_table, terminal_width, truncate

#: Separates columns in the summary table's compact "Columns" cell.
SUMMARY_SEPARATOR = " · "

#: Fallback separator when even abbreviated names do not fit: dropping the
#: spaces is cheaper than dropping a count.
COMPACT_SEPARATOR = "·"

#: Separates segments of a board header's count strip.
STRIP_SEPARATOR = " │ "

#: Separates the parts of a board header's title line.
TITLE_SEPARATOR = "  ·  "

#: Abbreviation never cuts a column name below this many cells (ellipsis
#: included) in a one-line summary; past that the whole line is cut instead.
_MIN_NAME_WIDTH = 4

#: The header strip stops abbreviating here and wraps instead: below this,
#: column names stop being recognisable.
_STRIP_MIN_NAME_WIDTH = 10

#: A board name in the summary table is never squeezed below this.
_MIN_BOARD_WIDTH = 12

#: Board names give up cells down to this so every column count fits in full.
_SOFT_BOARD_WIDTH = 24

#: The summary "Columns" cell is given at least this many cells.
_MIN_COLUMNS_WIDTH = 24

#: Summary "Type" holds only the board type ("scrum" / "kanban"), never a sprint.
_MAX_TYPE_WIDTH = len("kanban")

#: A multi-project board's "A,B,C" project cell is cut here.
_MAX_PROJECT_WIDTH = 16

#: The optional summary "Sprint" column never grows past this many cells ...
_MAX_SPRINT_WIDTH = 24

#: ... and is dropped rather than squeezed below this.
_MIN_SPRINT_WIDTH = 8

COLUMNS = "Columns"
SPRINT = "Sprint"

ALL_COLUMNS = "All columns"


def _safe(value: Any) -> str:
    return "" if value is None else terminal_safe_line(str(value))


def _width(width: int | None) -> int:
    return width if width and width > 0 else terminal_width()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# ------------------------------------------------------------- count strips


def _abbreviate(name: str, cap: int) -> str:
    """``truncate`` that never leaves a space dangling before the ellipsis."""
    cut = truncate(name, cap)
    if cut.endswith(ELLIPSIS) and cut != name:
        cut = cut[:-1].rstrip() + ELLIPSIS
    return cut


def _join_counts(parts: Sequence[tuple[str, Any]], separator: str) -> str:
    return separator.join(f"{name} {count}" for name, count in parts)


def _abbreviate_counts(
    parts: Sequence[tuple[str, Any]], separator: str, budget: int, floor: int
) -> tuple[list[tuple[str, Any]], bool]:
    """Shorten the longest names first until the joined line fits ``budget``.

    Short names (``Done``, ``Test``) stay readable because only the current
    longest name is cut each round.  Returns the parts and whether they fit.
    """
    names = [name for name, _ in parts]
    counts = [count for _, count in parts]
    caps = [display_width(name) for name in names]

    def joined() -> str:
        cut = [_abbreviate(name, cap) for name, cap in zip(names, caps, strict=True)]
        return _join_counts(list(zip(cut, counts, strict=True)), separator)

    while display_width(joined()) > budget:
        longest = max(range(len(caps)), key=lambda i: caps[i])
        if caps[longest] <= floor:
            break
        caps[longest] -= 1
    cut = [_abbreviate(name, cap) for name, cap in zip(names, caps, strict=True)]
    return list(zip(cut, counts, strict=True)), display_width(joined()) <= budget


def _fit_counts(parts: Sequence[tuple[str, Any]], separator: str, budget: int) -> str:
    """One line of counts within ``budget`` cells; abbreviates, never wraps."""
    short, fits = _abbreviate_counts(parts, separator, budget, _MIN_NAME_WIDTH)
    if not fits:
        separator = COMPACT_SEPARATOR
        short, _ = _abbreviate_counts(parts, separator, budget, _MIN_NAME_WIDTH)
    return truncate(_join_counts(short, separator), budget)


def _wrap_counts(parts: Sequence[tuple[str, Any]], separator: str, budget: int) -> list[str]:
    """Count strip lines: abbreviate first, wrap only when that is not enough."""
    short, fits = _abbreviate_counts(parts, separator, budget, _STRIP_MIN_NAME_WIDTH)
    if fits:
        return [_join_counts(short, separator)]
    lines: list[str] = []
    current = ""
    for name, count in short:
        segment = truncate(f"{name} {count}", budget)
        candidate = f"{current}{separator}{segment}" if current else segment
        if current and display_width(candidate) > budget:
            lines.append(current)
            candidate = segment
        current = candidate
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------- board summaries


def _summary_cells(index: int, row: Mapping[str, Any]) -> dict[str, str]:
    failed = bool(row.get("error"))
    backlog = row.get("backlog")
    return {
        "#": str(index),
        "Board": _safe(row.get("name")) or f"board {row.get('id')}",
        "Type": truncate(_safe(row.get("type")) or "-", _MAX_TYPE_WIDTH),
        "Project": truncate(_safe(row.get("project")) or "-", _MAX_PROJECT_WIDTH),
        "Total": "-" if failed else str(row.get("total", 0)),
        "Backlog": "-" if failed or backlog is None else str(backlog),
    }


def _sprint_cell(row: Mapping[str, Any]) -> str:
    """Active sprint name(s) of a scrum board; blank for kanban and failed rows."""
    if row.get("error") or _safe(row.get("type")) != "scrum":
        return ""
    return _safe(row.get("sprint")) or "-"


def _columns_cell(row: Mapping[str, Any], budget: int) -> str:
    if row.get("error"):
        return truncate(f"error: {_safe(row.get('error'))}", budget)
    parts = [(_safe(col.get("name")), col.get("count", 0)) for col in row.get("columns") or []]
    if not parts:
        return "-"
    return _fit_counts(parts, SUMMARY_SEPARATOR, budget)


def _columns_need(row: Mapping[str, Any]) -> int:
    """Cells a row's Columns cell needs with every name still recognisable.

    Names are cut to no fewer than ``_STRIP_MIN_NAME_WIDTH`` cells; that is the
    most the optional Sprint column may take from them.  Error rows may be cut
    freely and need nothing.
    """
    if row.get("error"):
        return 0
    parts = [(_safe(col.get("name")), col.get("count", 0)) for col in row.get("columns") or []]
    if not parts:
        return 1
    short, _ = _abbreviate_counts(parts, SUMMARY_SEPARATOR, 0, _STRIP_MIN_NAME_WIDTH)
    return display_width(_join_counts(short, SUMMARY_SEPARATOR))


def _sprint_width(sprints: Sequence[str], room: int) -> int:
    """Cells for the optional Sprint column; ``0`` drops it.

    The column only takes what is left after Columns got its full width, so a
    sprint name is cut (or the column dropped) before any count is.
    """
    if not any(sprints):
        return 0
    wanted = min(_MAX_SPRINT_WIDTH, max(display_width(SPRINT), *map(display_width, sprints)))
    if room < min(wanted, _MIN_SPRINT_WIDTH):
        return 0
    return min(wanted, room)


def render_board_summaries(rows: list[dict], *, width: int | None = None) -> str:
    """Numbered table of boards: type, project, totals and per-column counts.

    The ``Columns`` cell gets whatever width the other columns leave; column
    names in it are abbreviated (never wrapped) until the line fits.  Scrum
    boards add a trailing ``Sprint`` column (at most 24 cells) only when the
    line still has room once every column count fits in full.
    """
    if not rows:
        return "(no boards)"
    limit = _width(width)
    fixed = [_summary_cells(index, row) for index, row in enumerate(rows, start=1)]
    headers = list(fixed[0])
    widths = {
        header: max(display_width(header), *(display_width(cells[header]) for cells in fixed))
        for header in headers
    }
    gaps = len(COLUMN_GAP) * len(headers)  # one more column ("Columns") follows
    spare = limit - sum(widths.values()) - gaps
    columns_full = max(
        display_width(COLUMNS),
        max(
            (display_width(_columns_cell(row, limit)) for row in rows if not row.get("error")),
            default=0,
        ),
    )
    # Board names yield first: to _SOFT_BOARD_WIDTH so every count fits in
    # full, and on down to _MIN_BOARD_WIDTH only to keep a usable Columns cell.
    for floor, wanted in ((_SOFT_BOARD_WIDTH, columns_full), (_MIN_BOARD_WIDTH, _MIN_COLUMNS_WIDTH)):
        if spare < wanted:
            board_width = max(min(floor, widths["Board"]), widths["Board"] - (wanted - spare))
            spare += widths["Board"] - board_width
            widths["Board"] = board_width
    sprints = [_sprint_cell(row) for row in rows]
    columns_need = max(display_width(COLUMNS), *(_columns_need(row) for row in rows))
    sprint_width = _sprint_width(sprints, spare - columns_need - len(COLUMN_GAP))
    if sprint_width:
        spare -= sprint_width + len(COLUMN_GAP)
    budget = max(spare, len(COLUMNS))
    table_rows = []
    for cells, row, sprint in zip(fixed, rows, sprints, strict=True):
        cells["Board"] = truncate(cells["Board"], widths["Board"])
        cells[COLUMNS] = _columns_cell(row, budget)
        cells[SPRINT] = _abbreviate(sprint, sprint_width)
        table_rows.append(cells)
    shown = [*headers, COLUMNS, SPRINT] if sprint_width else [*headers, COLUMNS]
    return render_table(shown, table_rows, max_width=limit)


# ------------------------------------------------------------- board header


def _sprint_line(sprint: Mapping[str, Any]) -> str:
    details = [_safe(sprint.get("state"))] if sprint.get("state") else []
    end_date = _safe(sprint.get("end_date"))
    if end_date:
        details.append(f"ends {end_date[:10]}")  # ISO timestamp -> date
    suffix = f" ({', '.join(details)})" if details else ""
    return f"sprint: {_safe(sprint.get('name')) or '-'}{suffix}"


def _title_line(board: Mapping[str, Any], total: Any, limit: int) -> str:
    rest = [_safe(board.get("type")) or "board"]
    if board.get("project"):
        rest.append(f"project {_safe(board.get('project'))}")
    rest.append(_plural(int(total or 0), "issue"))
    tail = TITLE_SEPARATOR + TITLE_SEPARATOR.join(rest)
    name = _safe(board.get("name")) or f"board {board.get('id')}"
    name = truncate(name, max(_MIN_BOARD_WIDTH, limit - display_width(tail)))
    return truncate(name + tail, limit)


def _strip_parts(overview: Mapping[str, Any]) -> list[tuple[str, Any]]:
    parts: list[tuple[str, Any]] = []
    backlog = overview.get("backlog")
    if backlog is not None:
        parts.append(("Backlog", backlog.get("count", 0)))
    for column in overview.get("columns") or []:
        parts.append((_safe(column.get("name")), column.get("count", 0)))
    unmapped = _unmapped_count(overview)
    if unmapped > 0:
        parts.append(("Unmapped", unmapped))
    return parts


def render_board_header(overview: dict, *, width: int | None = None) -> str:
    """Title line, sprint line(s) for scrum boards, then a count strip.

    The strip lists Backlog (when the board has one), every column in board
    order, and Unmapped only when it holds issues.
    """
    limit = _width(width)
    board = overview.get("board") or {}
    lines = [_title_line(board, overview.get("total"), limit)]
    if board.get("type") == "scrum":
        sprints = overview.get("sprints") or []
        if sprints:
            lines.extend(truncate(_sprint_line(sprint), limit) for sprint in sprints)
        else:
            lines.append("sprint: no active sprint")
    parts = _strip_parts(overview)
    if parts:
        lines.extend(_wrap_counts(parts, STRIP_SEPARATOR, limit))
    return "\n".join(lines)


# ---------------------------------------------------------- column sections


def _unmapped_count(overview: Mapping[str, Any]) -> int:
    unmapped = overview.get("unmapped") or {}
    return int(unmapped.get("count") or 0)


def _sections(overview: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any], bool]]:
    """``(name, section, is_mixed)`` in picker order; mixed sections may show Status."""
    sections: list[tuple[str, Mapping[str, Any], bool]] = []
    if overview.get("backlog") is not None:
        sections.append(("Backlog", overview["backlog"], True))
    for column in overview.get("columns") or []:
        sections.append((_safe(column.get("name")), column, False))
    if _unmapped_count(overview) > 0:
        sections.append(("Unmapped", overview["unmapped"], True))
    return sections


def column_choices(overview: dict) -> list[str]:
    """Picker labels, e.g. ``["Backlog (12)", "To Do (5)", ..., "All columns"]``.

    The index of a label is the ``choice_index`` :func:`render_column` takes.
    """
    labels = [f"{name} ({section.get('count', 0)})" for name, section, _ in _sections(overview)]
    return [*labels, ALL_COLUMNS]


def _issue_cells(issue: Mapping[str, Any], person_cap: int, status_cap: int) -> dict[str, str]:
    return {
        "Key": _safe(issue.get("key")) or "-",
        "Title": _safe(issue.get("summary")),
        "Assignee": truncate(_safe(issue.get("assignee")) or "unassigned", person_cap),
        "Reporter": truncate(_safe(issue.get("reporter")) or "-", person_cap),
        "Status": truncate(_safe(issue.get("status")) or "-", status_cap),
    }


def _render_section(
    name: str,
    section: Mapping[str, Any],
    *,
    may_show_status: bool,
    limit: int,
    max_rows: int | None,
) -> str:
    count = int(section.get("count") or 0)
    issues = list(section.get("issues") or [])
    lines = [truncate(f"{name} — {_plural(count, 'issue')}", limit)]
    if not issues:
        lines.append("(no issues)")
        return "\n".join(lines)
    shown = issues[:max_rows] if max_rows is not None and max_rows >= 0 else issues
    # Narrow person/status columns so the title, the useful part, keeps room.
    person_cap = max(10, limit // 6)
    status_cap = max(8, limit // 8)
    rows = [_issue_cells(issue, person_cap, status_cap) for issue in shown]
    columns = ["Key", "Title", "Assignee", "Reporter"]
    if may_show_status and len({issue.get("status") for issue in issues}) > 1:
        columns.append("Status")
    lines.append(render_table(columns, rows, max_width=limit))
    if section.get("truncated") or len(shown) < max(count, len(issues)):
        lines.append(f"(showing first {len(shown)} of {max(count, len(issues))})")
    return "\n".join(lines)


def render_column(
    overview: dict,
    choice_index: int,
    *,
    width: int | None = None,
    max_rows: int | None = None,
) -> str:
    """Issues of the section picked by ``choice_index`` (see :func:`column_choices`).

    The last choice, ``All columns``, renders every section in board order.
    ``Status`` is shown only for Backlog/Unmapped (and those sections under
    All columns) when their issues are not all in one status.
    """
    sections = _sections(overview)
    if not 0 <= choice_index <= len(sections):
        raise InputError(f"no board column at choice {choice_index}")
    limit = _width(width)
    picked = sections if choice_index == len(sections) else [sections[choice_index]]
    return "\n\n".join(
        _render_section(name, section, may_show_status=mixed, limit=limit, max_rows=max_rows)
        for name, section, mixed in picked
    )
