"""Rendering of board summaries and one board's overview (cli/board_view.py).

All inputs are hand-built dicts in the shapes ``services.boards`` produces;
nothing here touches the network.
"""

from __future__ import annotations

import pytest

from jira_cli.cli import board_view
from jira_cli.cli.output import display_width
from jira_cli.errors import InputError

PERSIAN_TITLE = "متن‌های نمونه طولانی برای آزمون عرض ستون"
ESCAPE = "\x1b]52;c;ZWNobyBwd25lZA==\x07"


def _issue(key, summary="Fix login", status="In Progress", assignee="Alice", reporter="Bob"):
    return {
        "key": key,
        "summary": summary,
        "status": status,
        "type": "Task",
        "priority": "Medium",
        "assignee": assignee,
        "assignee_username": assignee and assignee.lower(),
        "reporter": reporter,
        "reporter_username": reporter and reporter.lower(),
        "updated": "2026-10-20T10:00:00.000+0000",
    }


def _overview(*, board_type="kanban", backlog=True, unmapped=0, sprints=None, columns=None, total=9):
    if columns is None:
        columns = [
            {
                "name": "Selected for Development",
                "statuses": ["Selected for Development"],
                "count": 1,
                "issues": [_issue("PROJ-1", status="Selected for Development")],
            },
            {
                "name": "In Progress",
                "statuses": ["In Progress"],
                "count": 2,
                "issues": [_issue("PROJ-2"), _issue("PROJ-3", summary=PERSIAN_TITLE)],
            },
            {"name": "Done", "statuses": ["Done"], "count": 0, "issues": []},
        ]
    return {
        "board": {"id": 7, "name": "PROJ board", "type": board_type, "project": "PROJ"},
        "sprints": sprints or [],
        "columns": columns,
        "backlog": (
            {
                "count": 2,
                "issues": [
                    _issue("PROJ-4", status="Open", assignee=None),
                    _issue("PROJ-5", status="Reopened"),
                ],
            }
            if backlog
            else None
        ),
        "unmapped": {
            "count": unmapped,
            "issues": [_issue(f"PROJ-{90 + n}", status="Weird") for n in range(unmapped)],
        },
        "total": total,
    }


def _summary_row(**overrides):
    row = {
        "id": 1,
        "name": "PROJ board",
        "type": "kanban",
        "project": "PROJ",
        "total": 49,
        "backlog": 12,
        "columns": [
            {"name": "Selected for Development", "count": 5},
            {"name": "In Progress", "count": 3},
            {"name": "Test", "count": 1},
            {"name": "Done", "count": 40},
        ],
        "sprint": None,
        "error": None,
    }
    row.update(overrides)
    return row


def _widest_line(text):
    return max(display_width(line) for line in text.splitlines())


# ------------------------------------------------------------ summaries


def test_summary_shows_numbered_rows_with_compact_column_counts():
    text = board_view.render_board_summaries([_summary_row()], width=140)

    header, _rule, row = text.splitlines()
    assert header.split() == ["#", "Board", "Type", "Project", "Total", "Backlog", "Columns"]
    assert row.startswith("1  PROJ board")
    assert "Selected for Development 5 · In Progress 3 · Test 1 · Done 40" in row


def test_summary_abbreviates_column_names_to_fit_width_without_wrapping():
    text = board_view.render_board_summaries([_summary_row()], width=80)

    assert _widest_line(text) <= 80
    assert len(text.splitlines()) == 3
    row = text.splitlines()[2]
    assert "Selected for Development" not in row
    assert "Done 40" in row


def test_scrum_summary_keeps_type_short_and_names_the_sprint_after_columns():
    rows = [
        _summary_row(type="scrum", sprint="Sprint 12"),
        _summary_row(id=2, type="scrum", sprint=None),
    ]

    text = board_view.render_board_summaries(rows, width=160)

    header, _rule, first, second = text.splitlines()
    assert header.split() == ["#", "Board", "Type", "Project", "Total", "Backlog", "Columns", "Sprint"]
    assert "scrum (" not in text
    assert first.endswith("Done 40  Sprint 12")
    assert second.endswith("Done 40  -")


def test_long_persian_sprint_name_is_cut_to_24_cells_and_never_squeezes_column_counts():
    sprint = "متن نمونه طولانی متن نمونه طولانی متن نمونه طولانی"
    rows = [_summary_row(type="scrum", sprint=sprint), _summary_row(id=2)]

    text = board_view.render_board_summaries(rows, width=160)

    first = text.splitlines()[2]
    assert "Selected for Development 5 · In Progress 3 · Test 1 · Done 40" in first
    sprint_cell = first.split("Done 40", 1)[1].strip()
    assert sprint_cell.endswith("…")
    assert display_width(sprint_cell) <= 24
    assert _widest_line(text) <= 160


def test_sprint_column_may_shorten_long_column_names_but_keeps_every_count():
    rows = [_summary_row(type="scrum", sprint="Sprint 12")]

    text = board_view.render_board_summaries(rows, width=110)

    header, _rule, row = text.splitlines()
    assert header.split()[-1] == "Sprint"
    assert row.endswith("Sprint 12")
    assert " 5 · " in row and " 3 · " in row
    assert "Test 1 · Done 40  Sprint 12" in row
    assert "Selected for Development" not in row
    assert _widest_line(text) <= 110


def test_sprint_column_is_dropped_before_names_become_unrecognisable():
    rows = [_summary_row(type="scrum", sprint="Sprint 12")]

    text = board_view.render_board_summaries(rows, width=90)

    header, _rule, row = text.splitlines()
    assert "Sprint" not in header
    assert row.endswith("Done 40")
    assert _widest_line(text) <= 90


def test_long_board_name_yields_cells_so_every_column_count_fits_in_full():
    rows = [_summary_row(name="A very long board name that goes on and on and on " * 2)]

    text = board_view.render_board_summaries(rows, width=140)

    row = text.splitlines()[2]
    assert "Selected for Development 5 · In Progress 3 · Test 1 · Done 40" in row
    assert "…" in row.split("kanban")[0]
    assert _widest_line(text) <= 140


def test_kanban_only_summary_has_no_sprint_column():
    text = board_view.render_board_summaries([_summary_row(), _summary_row(id=2)], width=200)

    assert text.splitlines()[0].split()[-1] == "Columns"


def test_summary_type_cell_never_grows_past_kanban_width():
    text = board_view.render_board_summaries([_summary_row(type="something-much-longer")], width=160)

    type_cell = text.splitlines()[2].split()[3]
    assert display_width(type_cell) <= len("kanban")


def test_summary_shows_looked_up_project_and_cuts_a_long_multi_project_list():
    rows = [
        _summary_row(project="ALPHA,BRAVO,CHARLIE,DELTA"),
        _summary_row(id=2, project=None),
    ]

    text = board_view.render_board_summaries(rows, width=160)

    first, second = text.splitlines()[2:]
    assert "ALPHA,BRAVO,CHA…" in first
    assert second.split()[4] == "-"


def test_summary_shows_dash_for_board_without_backlog():
    text = board_view.render_board_summaries([_summary_row(backlog=None)], width=140)

    assert text.splitlines()[2].split()[6] == "-"


def test_summary_error_row_reports_error_in_columns_cell():
    text = board_view.render_board_summaries(
        [_summary_row(error="permission denied", columns=[], total=0, backlog=None)], width=120
    )

    assert "error: permission denied" in text.splitlines()[2]


def test_summary_neutralises_escape_sequences_in_server_text():
    text = board_view.render_board_summaries(
        [_summary_row(name=f"evil{ESCAPE}", error=f"boom{ESCAPE}")], width=200
    )

    assert "\x1b" not in text
    assert "\x07" not in text


def test_summary_of_no_boards_says_so():
    assert board_view.render_board_summaries([], width=80) == "(no boards)"


# ---------------------------------------------------------------- header


def test_kanban_header_has_title_and_count_strip_in_board_order():
    text = board_view.render_board_header(_overview(), width=140)

    title, strip = text.splitlines()
    assert title == "PROJ board  ·  kanban  ·  project PROJ  ·  9 issues"
    assert strip == "Backlog 2 │ Selected for Development 1 │ In Progress 2 │ Done 0"


def test_scrum_header_adds_sprint_line_with_end_date():
    sprints = [{"id": 3, "name": "Sprint 12", "state": "active", "end_date": "2026-11-02T12:00:00.000Z"}]

    lines = board_view.render_board_header(
        _overview(board_type="scrum", sprints=sprints), width=140
    ).splitlines()

    assert lines[1] == "sprint: Sprint 12 (active, ends 2026-11-02)"


def test_scrum_header_without_sprint_says_no_active_sprint():
    text = board_view.render_board_header(_overview(board_type="scrum"), width=140)

    assert "sprint: no active sprint" in text.splitlines()


def test_header_shows_unmapped_only_when_it_holds_issues():
    assert "Unmapped" not in board_view.render_board_header(_overview(unmapped=0), width=140)
    assert "│ Unmapped 3" in board_view.render_board_header(_overview(unmapped=3), width=140)


def test_header_strip_abbreviates_to_fit_narrow_width():
    text = board_view.render_board_header(_overview(), width=60)

    assert _widest_line(text) <= 60
    assert len(text.splitlines()) == 2


def test_header_strip_wraps_only_when_abbreviation_cannot_fit():
    columns = [{"name": f"Stage number {n}", "statuses": [], "count": n, "issues": []} for n in range(12)]

    text = board_view.render_board_header(_overview(columns=columns), width=60)

    assert len(text.splitlines()) > 2
    assert _widest_line(text) <= 60
    assert "11" in text.splitlines()[-1]


# --------------------------------------------------------------- choices


def test_choices_list_backlog_columns_then_all():
    assert board_view.column_choices(_overview()) == [
        "Backlog (2)",
        "Selected for Development (1)",
        "In Progress (2)",
        "Done (0)",
        "All columns",
    ]


def test_choices_without_backlog_start_at_first_column_and_include_unmapped():
    choices = board_view.column_choices(_overview(backlog=False, unmapped=1))

    assert choices[0] == "Selected for Development (1)"
    assert choices[-2:] == ["Unmapped (1)", "All columns"]


def test_choice_index_selects_same_section_on_board_without_backlog():
    text = board_view.render_column(_overview(backlog=False), 1, width=100)

    assert text.splitlines()[0] == "In Progress — 2 issues"


def test_out_of_range_choice_is_an_input_error():
    with pytest.raises(InputError):
        board_view.render_column(_overview(), 5, width=100)


# ---------------------------------------------------------------- column


def test_column_table_has_key_title_assignee_reporter_and_no_status():
    text = board_view.render_column(_overview(), 2, width=100)

    lines = text.splitlines()
    assert lines[0] == "In Progress — 2 issues"
    assert lines[1].split() == ["Key", "Title", "Assignee", "Reporter"]


@pytest.mark.parametrize("width", [80, 140])
def test_column_with_persian_title_stays_aligned_within_width(width):
    text = board_view.render_column(_overview(), 2, width=width)

    table = text.splitlines()[1:]
    assert all(display_width(line) <= width for line in table)
    header, _rule, *rows = table
    assignee_cell = header.index("Assignee")
    for row in rows:
        before = row.split("Alice")[0]
        assert display_width(before) == assignee_cell


def test_long_title_is_cut_with_ellipsis_at_narrow_width():
    text = board_view.render_column(_overview(), 2, width=60)

    assert "…" in text
    assert PERSIAN_TITLE not in text


def test_backlog_shows_status_when_statuses_differ_and_unassigned():
    text = board_view.render_column(_overview(), 0, width=120)

    lines = text.splitlines()
    assert lines[1].split() == ["Key", "Title", "Assignee", "Reporter", "Status"]
    assert "unassigned" in lines[3]
    assert "Reopened" in lines[4]


def test_empty_column_says_no_issues():
    assert board_view.render_column(_overview(), 3, width=100) == "Done — 0 issues\n(no issues)"


def test_truncated_column_has_showing_first_footer():
    columns = [
        {
            "name": "To Do",
            "statuses": ["To Do"],
            "count": 250,
            "issues": [_issue("PROJ-1"), _issue("PROJ-2")],
            "truncated": True,
        }
    ]

    text = board_view.render_column(_overview(columns=columns, backlog=False), 0, width=100)

    assert text.splitlines()[0] == "To Do — 250 issues"
    assert text.splitlines()[-1] == "(showing first 2 of 250)"


def test_max_rows_limits_rows_and_adds_footer():
    text = board_view.render_column(_overview(), 2, width=100, max_rows=1)

    assert "PROJ-3" not in text
    assert text.splitlines()[-1] == "(showing first 1 of 2)"


def test_all_columns_renders_every_section_in_order():
    overview = _overview(unmapped=1)
    all_index = len(board_view.column_choices(overview)) - 1

    text = board_view.render_column(overview, all_index, width=100)

    headings = [line for line in text.splitlines() if " — " in line]
    assert headings == [
        "Backlog — 2 issues",
        "Selected for Development — 1 issue",
        "In Progress — 2 issues",
        "Done — 0 issues",
        "Unmapped — 1 issue",
    ]


def test_column_neutralises_escape_sequences_in_issue_text():
    columns = [
        {
            "name": f"Col{ESCAPE}",
            "statuses": [],
            "count": 1,
            "issues": [_issue("PROJ-1", summary=f"hi{ESCAPE}\nfake row", assignee=f"a{ESCAPE}")],
        }
    ]

    text = board_view.render_column(_overview(columns=columns, backlog=False), 0, width=120)

    assert "\x1b" not in text
    assert "\x07" not in text
    assert len(text.splitlines()) == 4  # heading, header, rule, one row
