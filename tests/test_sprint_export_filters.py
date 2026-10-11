"""Tests for ``services.sprint_report``, ``services.export`` and ``services.filters``.

Entirely offline. The real core modules (``errors``, ``report``, ``fields``)
are imported; only the HTTP client is faked. Nothing here touches
``sys.modules``, so no state leaks into other test files.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import pytest

from jira_cli.errors import InputError, JiraError
from jira_cli.services import export, filters, people, sprint_report

# ----------------------------------------------------------------- fake client


class FakeClient:
    """A ``JiraClient`` stand-in driven by a routing table of canned responses.

    ``routes`` maps a request path to either a payload or an exception
    instance (which is raised). ``paginate`` reads the same table and returns
    the list under ``items_key``.

    The real ``FieldMap`` discovers custom field ids from ``/rest/api/2/field``,
    so tests that need story points route that path.
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, dict]] = []

    def _resolve(self, verb, path, params):
        self.calls.append((verb, path, dict(params or {})))
        if path not in self.routes:
            raise JiraError(f"no route for {path}", status=404)
        payload = self.routes[path]
        if isinstance(payload, BaseException):
            raise payload
        return payload

    def get(self, path, params=None):
        return self._resolve("GET", path, params)

    def paginate(self, path, params=None, *, items_key="values", **_kwargs):
        payload = self._resolve("PAGINATE", path, params)
        return list((payload or {}).get(items_key) or [])

    def params_for(self, path):
        """Every params dict this client was called with for ``path``."""
        return [params for _verb, called, params in self.calls if called == path]


def _greenhopper_issue(key, summary="Thing", points=None, status="Done"):
    issue = {
        "key": key,
        "summary": summary,
        "typeName": "Story",
        "statusName": status,
        "assignee": "alice",
    }
    if points is not None:
        issue["estimateStatistic"] = {"statFieldValue": {"value": points}}
    return issue


def _greenhopper_payload():
    return {
        "contents": {
            "completedIssues": [
                _greenhopper_issue("PROJ-1", "Done thing", 3),
                _greenhopper_issue("PROJ-2", "Late done thing", 2),
            ],
            "issuesNotCompletedInCurrentSprint": [
                _greenhopper_issue("PROJ-3", "Unfinished", 5, status="In Progress")
            ],
            "puntedIssues": [
                _greenhopper_issue("PROJ-4", "Dropped", 1, status="To Do")
            ],
            "issueKeysAddedDuringSprint": {"PROJ-2": True},
        },
        "sprint": {
            "id": 42,
            "name": "Sprint 7",
            "state": "ACTIVE",
            "startDate": "2026-10-01T09:00:00.000Z",
            "endDate": "2026-10-15T09:00:00.000Z",
            "goal": "ship it",
        },
    }


def _agile_issue(key, status_key="done", points=None):
    fields = {
        "summary": f"Summary of {key}",
        "issuetype": {"name": "Task"},
        "status": {"name": status_key.title(), "statusCategory": {"key": status_key}},
        "assignee": {"name": "bob", "displayName": "Bob"},
    }
    if points is not None:
        fields["customfield_10110"] = points
    return {"key": key, "fields": fields}


# ====================================================================== sprint


class TestSprintReportGreenhopper:
    def test_uses_greenhopper_and_records_the_method(self):
        client = FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: _greenhopper_payload()})

        report = sprint_report.sprint_report(client, 7, 42)

        assert report["meta"]["method"] == "greenhopper"
        assert report["meta"]["sprint_id"] == 42
        assert report["meta"]["sprint"]["name"] == "Sprint 7"
        assert report["title"] == "Sprint report: Sprint 7"

    def test_sends_rapid_view_and_sprint_id(self):
        client = FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: _greenhopper_payload()})

        sprint_report.sprint_report(client, 7, 42)

        assert client.params_for(sprint_report.GREENHOPPER_SPRINT_REPORT) == [
            {"rapidViewId": 7, "sprintId": 42}
        ]

    def test_categorises_completed_not_completed_and_removed(self):
        client = FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: _greenhopper_payload()})

        rows = {row["key"]: row for row in sprint_report.sprint_report(client, 7, 42)["rows"]}

        assert rows["PROJ-1"]["category"] == "completed"
        assert rows["PROJ-3"]["category"] == "not_completed"
        assert rows["PROJ-4"]["category"] == "removed"

    def test_marks_issues_added_after_sprint_start(self):
        client = FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: _greenhopper_payload()})

        rows = {row["key"]: row for row in sprint_report.sprint_report(client, 7, 42)["rows"]}

        assert rows["PROJ-2"]["added_after_start"] is True
        assert rows["PROJ-1"]["added_after_start"] is False

    def test_summary_excludes_late_additions_from_the_commitment(self):
        client = FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: _greenhopper_payload()})

        summary = sprint_report.sprint_report(client, 7, 42)["summary"]

        # Four issues, one of which arrived mid-sprint.
        assert summary["committed"] == 3
        assert summary["completed"] == 2
        assert summary["not_completed"] == 1
        assert summary["removed"] == 1
        assert summary["added_after_start"] == 1
        assert summary["completion_pct"] == pytest.approx(66.7)

    def test_summary_carries_story_points_when_the_board_estimates(self):
        client = FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: _greenhopper_payload()})

        summary = sprint_report.sprint_report(client, 7, 42)["summary"]

        assert summary["points_committed"] == pytest.approx(9.0)  # 3 + 5 + 1
        assert summary["points_completed"] == pytest.approx(5.0)  # 3 + 2
        assert summary["points_added_after_start"] == pytest.approx(2.0)

    def test_summary_omits_points_when_no_issue_is_estimated(self):
        payload = {
            "contents": {
                "completedIssues": [_greenhopper_issue("PROJ-1")],
                "issuesNotCompletedInCurrentSprint": [],
                "puntedIssues": [],
                "issueKeysAddedDuringSprint": {},
            },
            "sprint": {"id": 1, "name": "S1"},
        }
        client = FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: payload})

        summary = sprint_report.sprint_report(client, 7, 1)["summary"]

        assert "points_committed" not in summary
        assert summary["completion_pct"] == pytest.approx(100.0)

    def test_empty_sprint_does_not_divide_by_zero(self):
        payload = {"contents": {}, "sprint": {"id": 9, "name": "Empty"}}
        client = FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: payload})

        report = sprint_report.sprint_report(client, 7, 9)

        assert report["rows"] == []
        assert report["summary"]["completion_pct"] == 0.0


class TestSprintReportDefaultSprint:
    def test_defaults_to_the_boards_active_sprint(self):
        client = FakeClient(
            {
                "/rest/agile/1.0/board/7/sprint": {"values": [{"id": 42, "name": "Sprint 7"}]},
                sprint_report.GREENHOPPER_SPRINT_REPORT: _greenhopper_payload(),
            }
        )

        report = sprint_report.sprint_report(client, 7)

        assert report["meta"]["sprint_id"] == 42
        assert client.params_for("/rest/agile/1.0/board/7/sprint") == [{"state": "active"}]

    def test_no_active_sprint_raises_input_error(self):
        client = FakeClient({"/rest/agile/1.0/board/7/sprint": {"values": []}})

        with pytest.raises(InputError):
            sprint_report.sprint_report(client, 7)


class TestSprintReportAgileFallback:
    def _client(self):
        return FakeClient(
            {
                sprint_report.GREENHOPPER_SPRINT_REPORT: JiraError("no greenhopper", status=404),
                "/rest/api/2/field": [
                    {"id": "summary", "name": "Summary"},
                    {"id": "customfield_10110", "name": "Story Points"},
                ],
                "/rest/agile/1.0/sprint/42/issue": {
                    "issues": [
                        _agile_issue("PROJ-1", "done", 3),
                        _agile_issue("PROJ-3", "indeterminate", 5),
                    ]
                },
                "/rest/agile/1.0/sprint/42": {"id": 42, "name": "Sprint 7", "state": "closed"},
            }
        )

    def test_falls_back_to_the_agile_api_and_says_so(self):
        report = sprint_report.sprint_report(self._client(), 7, 42)

        assert report["meta"]["method"] == "agile"
        assert "no greenhopper" in report["meta"]["fallback_reason"]

    def test_categorises_by_status_category(self):
        rows = {row["key"]: row for row in sprint_report.sprint_report(self._client(), 7, 42)["rows"]}

        assert rows["PROJ-1"]["category"] == "completed"
        assert rows["PROJ-3"]["category"] == "not_completed"

    def test_cannot_detect_removals_or_late_additions(self):
        summary = sprint_report.sprint_report(self._client(), 7, 42)["summary"]

        assert summary["removed"] == 0
        assert summary["added_after_start"] == 0
        assert summary["committed"] == 2

    def test_requests_the_story_points_field_explicitly(self):
        client = self._client()

        report = sprint_report.sprint_report(client, 7, 42)

        requested = client.params_for("/rest/agile/1.0/sprint/42/issue")[0]["fields"]
        assert "customfield_10110" in requested
        assert report["summary"]["points_completed"] == pytest.approx(3.0)

    def test_report_shape_matches_the_greenhopper_one(self):
        agile = sprint_report.sprint_report(self._client(), 7, 42)
        green = sprint_report.sprint_report(
            FakeClient({sprint_report.GREENHOPPER_SPRINT_REPORT: _greenhopper_payload()}), 7, 42
        )

        assert agile["columns"] == green["columns"]
        assert set(agile["rows"][0]) == set(green["rows"][0])


# ==================================================================== velocity


class TestVelocity:
    def test_uses_the_greenhopper_velocity_chart(self):
        client = FakeClient(
            {
                sprint_report.GREENHOPPER_VELOCITY: {
                    "sprints": [
                        {"id": 1, "name": "S1", "state": "CLOSED"},
                        {"id": 2, "name": "S2", "state": "CLOSED"},
                    ],
                    "velocityStatEntries": {
                        "1": {"estimated": {"value": 10}, "completed": {"value": 8}},
                        "2": {"estimated": {"value": 12}, "completed": {"value": 12}},
                    },
                }
            }
        )

        report = sprint_report.velocity(client, 7, last=5)

        assert report["meta"]["method"] == "greenhopper"
        assert [row["sprint"] for row in report["rows"]] == ["S1", "S2"]
        assert report["summary"]["avg_completed"] == pytest.approx(10.0)

    def test_keeps_only_the_last_n_sprints(self):
        client = FakeClient(
            {
                sprint_report.GREENHOPPER_VELOCITY: {
                    "sprints": [{"id": i, "name": f"S{i}"} for i in range(1, 6)],
                    "velocityStatEntries": {},
                }
            }
        )

        report = sprint_report.velocity(client, 7, last=2)

        assert [row["sprint"] for row in report["rows"]] == ["S4", "S5"]

    def test_falls_back_to_counting_issues_in_closed_sprints(self):
        client = FakeClient(
            {
                sprint_report.GREENHOPPER_VELOCITY: JiraError("no chart", status=403),
                "/rest/agile/1.0/board/7/sprint": {
                    "values": [{"id": 3, "name": "S3", "state": "closed"}]
                },
                "/rest/agile/1.0/sprint/3/issue": {
                    "issues": [
                        _agile_issue("PROJ-1", "done"),
                        _agile_issue("PROJ-2", "done"),
                        _agile_issue("PROJ-3", "new"),
                    ]
                },
            }
        )

        report = sprint_report.velocity(client, 7, last=5)

        assert report["meta"]["method"] == "agile"
        assert report["rows"] == [
            {"sprint_id": 3, "sprint": "S3", "state": "closed", "committed": 3, "completed": 2}
        ]

    @pytest.mark.parametrize("bad", [0, -1, 1.5, "3", True])
    def test_rejects_a_non_positive_sprint_count(self, bad):
        with pytest.raises(InputError):
            sprint_report.velocity(FakeClient(), 7, last=bad)

    def test_no_sprints_yields_zero_averages(self):
        client = FakeClient(
            {sprint_report.GREENHOPPER_VELOCITY: {"sprints": [], "velocityStatEntries": {}}}
        )

        summary = sprint_report.velocity(client, 7)["summary"]

        assert summary == {
            "sprints": 0,
            "total_committed": 0,
            "total_completed": 0,
            "avg_committed": 0.0,
            "avg_completed": 0.0,
        }


# ====================================================================== export


def _report(**overrides):
    base = {
        "title": "Sprint report: Sprint 7",
        "columns": ["key", "summary", "points"],
        "rows": [
            {"key": "PROJ-1", "summary": "Normal row", "points": 3},
            {"key": "PROJ-2", "summary": "Has | pipe", "points": None},
        ],
        "summary": {"committed": 2, "completed": 1},
        "meta": {"generated_at": "2026-10-11T00:00:00+00:00"},
    }
    base.update(overrides)
    return base


class TestToMarkdown:
    def test_starts_with_the_title_as_a_heading(self):
        assert to_lines(export.to_markdown(_report()))[0] == "# Sprint report: Sprint 7"

    def test_summary_becomes_a_bullet_list(self):
        text = export.to_markdown(_report())

        assert "- **committed**: 2" in text
        assert "- **completed**: 1" in text

    def test_table_header_and_separator_match_the_columns(self):
        lines = to_lines(export.to_markdown(_report()))

        assert "| key | summary | points |" in lines
        assert "| --- | --- | --- |" in lines

    def test_escapes_pipes_so_the_table_keeps_its_columns(self):
        lines = [line for line in to_lines(export.to_markdown(_report())) if "PROJ-2" in line]

        assert lines == [r"| PROJ-2 | Has \| pipe |  |"]

    def test_flattens_newlines_inside_a_cell(self):
        report = _report(rows=[{"key": "PROJ-9", "summary": "two\nlines", "points": 1}])

        line = [ln for ln in to_lines(export.to_markdown(report)) if "PROJ-9" in ln][0]

        assert line == "| PROJ-9 | two lines | 1 |"

    def test_renders_none_as_an_empty_cell(self):
        report = _report(rows=[{"key": "PROJ-9", "summary": None, "points": None}])

        assert "| PROJ-9 |  |  |" in to_lines(export.to_markdown(report))

    def test_ends_with_the_generation_timestamp(self):
        text = export.to_markdown(_report())

        assert "_generated_at: 2026-10-11T00:00:00+00:00_" in text

    def test_report_with_no_rows_still_renders_a_header(self):
        text = export.to_markdown(_report(rows=[]))

        assert "| key | summary | points |" in text


def to_lines(text: str) -> list[str]:
    """Split rendered text into lines, dropping the blank spacers."""
    return [line for line in text.splitlines() if line.strip()]


class TestToCsv:
    def test_header_row_comes_from_the_columns(self):
        rows = list(csv.reader(io.StringIO(export.to_csv(_report()))))

        assert rows[0] == ["key", "summary", "points"]

    def test_one_row_per_report_entry(self):
        rows = list(csv.reader(io.StringIO(export.to_csv(_report()))))

        assert rows[1] == ["PROJ-1", "Normal row", "3"]
        assert len(rows) == 3

    def test_keeps_a_pipe_and_quotes_an_embedded_comma(self):
        report = _report(rows=[{"key": "PROJ-9", "summary": "a, b | c", "points": 1}])

        text = export.to_csv(report)

        assert '"a, b | c"' in text
        assert list(csv.reader(io.StringIO(text)))[1] == ["PROJ-9", "a, b | c", "1"]

    @pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
    def test_neutralises_a_leading_formula_character(self, prefix):
        report = _report(rows=[{"key": "PROJ-9", "summary": f"{prefix}cmd|'calc'", "points": 1}])

        cell = list(csv.reader(io.StringIO(export.to_csv(report))))[1][1]

        assert cell.startswith("'")

    def test_leaves_an_ordinary_cell_untouched(self):
        cell = list(csv.reader(io.StringIO(export.to_csv(_report()))))[1][1]

        assert cell == "Normal row"

    def test_neutralises_a_formula_in_a_column_header(self):
        report = _report(columns=["=HYPERLINK(\"x\")"], rows=[])

        header = list(csv.reader(io.StringIO(export.to_csv(report))))[0][0]

        assert header == "'=HYPERLINK(\"x\")"

    def test_neutralises_a_negative_number_rendered_from_an_int(self):
        report = _report(rows=[{"key": "PROJ-9", "summary": "x", "points": -3}])

        assert list(csv.reader(io.StringIO(export.to_csv(report))))[1][2] == "'-3"

    def test_a_formula_character_after_the_first_position_is_left_alone(self):
        report = _report(rows=[{"key": "PROJ-9", "summary": "a=b+c", "points": 1}])

        assert list(csv.reader(io.StringIO(export.to_csv(report))))[1][1] == "a=b+c"


class TestToJson:
    def test_round_trips_the_whole_report(self):
        assert json.loads(export.to_json(_report())) == _report()

    def test_is_indented_by_two_spaces(self):
        assert '\n  "title":' in export.to_json(_report())

    def test_keeps_non_ascii_characters_as_themselves(self):
        text = export.to_json(_report(title="Sprint \u00fcber"))

        assert "\u00fcber" in text
        assert "\\u00fc" not in text


class TestWriteReport:
    def test_infers_markdown_from_the_suffix(self, tmp_path):
        path = export.write_report(_report(), tmp_path / "out.md")

        assert path.read_text(encoding="utf-8").startswith("# Sprint report")

    def test_infers_csv_from_the_suffix(self, tmp_path):
        path = export.write_report(_report(), tmp_path / "out.csv")

        assert path.read_text(encoding="utf-8").startswith("key,summary,points")

    def test_infers_json_from_the_suffix(self, tmp_path):
        path = export.write_report(_report(), tmp_path / "out.json")

        assert json.loads(path.read_text(encoding="utf-8"))["title"]

    def test_explicit_format_overrides_the_suffix(self, tmp_path):
        path = export.write_report(_report(), tmp_path / "out.txt", "json")

        assert json.loads(path.read_text(encoding="utf-8"))["title"]

    def test_creates_missing_parent_directories(self, tmp_path):
        path = export.write_report(_report(), tmp_path / "deep" / "nested" / "out.md")

        assert path.is_file()

    def test_overwrites_an_existing_report(self, tmp_path):
        target = tmp_path / "out.json"
        target.write_text("stale" * 500, encoding="utf-8")

        export.write_report(_report(), target)

        assert json.loads(target.read_text(encoding="utf-8"))["title"]

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        export.write_report(_report(), tmp_path / "out.md")

        assert [p.name for p in tmp_path.iterdir()] == ["out.md"]

    def test_unknown_format_is_rejected(self, tmp_path):
        with pytest.raises(InputError):
            export.write_report(_report(), tmp_path / "out.md", "xlsx")

    def test_unguessable_suffix_is_rejected(self, tmp_path):
        with pytest.raises(InputError):
            export.write_report(_report(), tmp_path / "out.txt")

    def test_refuses_to_write_over_a_directory(self, tmp_path):
        (tmp_path / "somewhere.md").mkdir()

        with pytest.raises(InputError):
            export.write_report(_report(), tmp_path / "somewhere.md")

    def test_refuses_to_write_inside_a_git_directory(self, tmp_path):
        with pytest.raises(InputError):
            export.write_report(_report(), tmp_path / ".git" / "out.md")

    def test_rejects_a_report_that_is_not_a_dict(self, tmp_path):
        with pytest.raises(InputError):
            export.write_report(["not", "a", "report"], tmp_path / "out.md")

    @pytest.mark.parametrize("git_dir", [".GIT", ".Git", ".git.", ".git "])
    def test_refuses_a_git_directory_in_any_case_or_windows_alias(self, tmp_path, git_dir):
        with pytest.raises(InputError):
            export.write_report(_report(), tmp_path / git_dir / "out.md")

        assert not (tmp_path / git_dir).exists()

    def test_a_name_that_merely_contains_git_is_allowed(self, tmp_path):
        path = export.write_report(_report(), tmp_path / ".github" / "out.md")

        assert path.is_file()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_report_file_is_owner_only(self, tmp_path):
        path = export.write_report(_report(), tmp_path / "out.md")

        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_overwritten_report_becomes_owner_only(self, tmp_path):
        target = tmp_path / "out.md"
        target.write_text("old", encoding="utf-8")
        target.chmod(0o644)

        export.write_report(_report(), target)

        assert target.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_created_directories_are_owner_only(self, tmp_path):
        export.write_report(_report(), tmp_path / "a" / "b" / "out.md")

        assert (tmp_path / "a").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "a" / "b").stat().st_mode & 0o777 == 0o700

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_existing_directories_keep_their_mode(self, tmp_path):
        tmp_path.chmod(0o755)

        export.write_report(_report(), tmp_path / "out.md")

        assert tmp_path.stat().st_mode & 0o777 == 0o755

    def test_base_dir_allows_a_target_inside_it(self, tmp_path):
        path = export.write_report(_report(), tmp_path / "sub" / "out.md", base_dir=tmp_path)

        assert path.is_file()

    def test_base_dir_refuses_dot_dot_escape(self, tmp_path):
        base = tmp_path / "reports"
        base.mkdir()

        with pytest.raises(InputError):
            export.write_report(_report(), base / ".." / "escaped.md", base_dir=base)

        assert not (tmp_path / "escaped.md").exists()

    @pytest.mark.skipif(os.name != "posix", reason="symlinks")
    def test_base_dir_refuses_a_symlink_that_leads_outside(self, tmp_path):
        base = tmp_path / "reports"
        outside = tmp_path / "outside"
        base.mkdir()
        outside.mkdir()
        (base / "link").symlink_to(outside, target_is_directory=True)

        with pytest.raises(InputError):
            export.write_report(_report(), base / "link" / "out.md", base_dir=base)

        assert list(outside.iterdir()) == []


class TestSafeFilename:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Sprint 7 report", "Sprint-7-report"),
            ("../../etc/passwd", "etc-passwd"),
            ("..", "report"),
            (".hidden", "hidden"),
            ("a\\b/c:d", "a-b-c-d"),
            ("-rf", "rf"),
            ("PROJ-12_v1.0", "PROJ-12_v1.0"),
            ("Sprint \u00fcber", "Sprint-ber"),
            ("", "report"),
            (None, "report"),
        ],
    )
    def test_keeps_only_safe_characters(self, raw, expected):
        assert export.safe_filename(raw) == expected

    def test_uses_the_given_default_when_nothing_is_left(self):
        assert export.safe_filename("///", default="export") == "export"

    def test_caps_the_length(self):
        assert len(export.safe_filename("x" * 500)) == 100

    def test_result_always_matches_the_allowed_alphabet(self):
        hostile = "=cmd|' /C calc'!A0 ..\\..\\ \x00\n\t PROJ-1"

        name = export.safe_filename(hostile)

        assert name and all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in name)
        assert name not in (".", "..") and not name.startswith((".", "-"))

    def test_a_safe_name_stays_inside_the_output_dir(self, tmp_path):
        name = export.safe_filename("../../outside") + ".md"

        path = export.write_report(_report(), tmp_path / name, base_dir=tmp_path)

        assert path == (tmp_path / "outside.md").resolve() or path == tmp_path / "outside.md"


# ===================================================================== filters


class TestListFilters:
    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert filters.list_filters(tmp_path / "absent.yaml") == {}

    def test_returns_jql_and_description_per_name(self, tmp_path):
        path = tmp_path / "filters.yaml"
        path.write_text(
            "mine:\n  jql: assignee = alice\n  description: my work\n", encoding="utf-8"
        )

        assert filters.list_filters(path) == {
            "mine": {"jql": "assignee = alice", "description": "my work"}
        }

    def test_accepts_the_legacy_plain_string_form(self, tmp_path):
        path = tmp_path / "filters.yaml"
        path.write_text("mine: assignee = alice\n", encoding="utf-8")

        assert filters.list_filters(path) == {
            "mine": {"jql": "assignee = alice", "description": ""}
        }

    def test_results_are_sorted_by_name(self, tmp_path):
        path = tmp_path / "filters.yaml"
        path.write_text("zeta: a = 1\nalpha: b = 2\n", encoding="utf-8")

        assert list(filters.list_filters(path)) == ["alpha", "zeta"]

    def test_empty_file_is_empty(self, tmp_path):
        path = tmp_path / "filters.yaml"
        path.write_text("", encoding="utf-8")

        assert filters.list_filters(path) == {}

    def test_a_yaml_list_is_rejected(self, tmp_path):
        path = tmp_path / "filters.yaml"
        path.write_text("- one\n- two\n", encoding="utf-8")

        with pytest.raises(InputError):
            filters.list_filters(path)

    def test_broken_yaml_is_rejected(self, tmp_path):
        path = tmp_path / "filters.yaml"
        path.write_text("mine: [unclosed\n", encoding="utf-8")

        with pytest.raises(InputError):
            filters.list_filters(path)

    def test_an_entry_without_jql_is_rejected(self, tmp_path):
        path = tmp_path / "filters.yaml"
        path.write_text("mine:\n  description: no jql here\n", encoding="utf-8")

        with pytest.raises(InputError):
            filters.list_filters(path)


class TestSaveFilter:
    def test_saved_filter_reads_back(self, tmp_path):
        path = tmp_path / "filters.yaml"

        filters.save_filter(path, "mine", "assignee = alice", "my work")

        assert filters.list_filters(path)["mine"] == {
            "jql": "assignee = alice",
            "description": "my work",
        }

    def test_reports_whether_it_created_or_replaced(self, tmp_path):
        path = tmp_path / "filters.yaml"

        assert filters.save_filter(path, "mine", "a = 1")["created"] is True
        assert filters.save_filter(path, "mine", "a = 2")["created"] is False
        assert filters.list_filters(path)["mine"]["jql"] == "a = 2"

    def test_keeps_the_other_filters(self, tmp_path):
        path = tmp_path / "filters.yaml"
        filters.save_filter(path, "one", "a = 1")

        filters.save_filter(path, "two", "b = 2")

        assert sorted(filters.list_filters(path)) == ["one", "two"]

    def test_rewrites_a_legacy_file_into_the_current_shape(self, tmp_path):
        path = tmp_path / "filters.yaml"
        path.write_text("old: a = 1\n", encoding="utf-8")

        filters.save_filter(path, "new", "b = 2")

        assert "jql: a = 1" in path.read_text(encoding="utf-8")

    def test_creates_the_config_directory(self, tmp_path):
        path = tmp_path / "config" / "filters.yaml"

        filters.save_filter(path, "mine", "a = 1")

        assert path.is_file()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
    def test_file_is_private_and_directory_is_owner_only(self, tmp_path):
        path = tmp_path / "config" / "filters.yaml"

        filters.save_filter(path, "mine", "a = 1")

        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700

    def test_description_defaults_to_empty(self, tmp_path):
        path = tmp_path / "filters.yaml"

        result = filters.save_filter(path, "mine", "a = 1")

        assert result["description"] == ""

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "has space", "slash/name", "..", "a" * 65, "semi;colon", "quote'", None],
    )
    def test_rejects_an_unsafe_name(self, tmp_path, bad):
        with pytest.raises(InputError):
            filters.save_filter(tmp_path / "filters.yaml", bad, "a = 1")

    @pytest.mark.parametrize("good", ["mine", "my-filter", "my_filter", "v1.2", "A1"])
    def test_accepts_a_safe_name(self, tmp_path, good):
        filters.save_filter(tmp_path / "filters.yaml", good, "a = 1")

        assert good in filters.list_filters(tmp_path / "filters.yaml")

    @pytest.mark.parametrize("bad", ["", "   ", None, 5])
    def test_rejects_an_empty_jql(self, tmp_path, bad):
        with pytest.raises(InputError):
            filters.save_filter(tmp_path / "filters.yaml", "mine", bad)

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        filters.save_filter(tmp_path / "filters.yaml", "mine", "a = 1")

        assert [p.name for p in tmp_path.iterdir()] == ["filters.yaml"]


class TestGetFilter:
    def test_returns_the_name_jql_and_description(self, tmp_path):
        path = tmp_path / "filters.yaml"
        filters.save_filter(path, "mine", "a = 1", "desc")

        assert filters.get_filter(path, "mine") == {
            "name": "mine",
            "jql": "a = 1",
            "description": "desc",
        }

    def test_unknown_name_raises_input_error(self, tmp_path):
        with pytest.raises(InputError):
            filters.get_filter(tmp_path / "filters.yaml", "absent")

    def test_unsafe_name_raises_input_error(self, tmp_path):
        with pytest.raises(InputError):
            filters.get_filter(tmp_path / "filters.yaml", "has space")


class TestDeleteFilter:
    def test_removes_the_filter_and_reports_true(self, tmp_path):
        path = tmp_path / "filters.yaml"
        filters.save_filter(path, "mine", "a = 1")

        assert filters.delete_filter(path, "mine") is True
        assert filters.list_filters(path) == {}

    def test_unknown_filter_reports_false(self, tmp_path):
        path = tmp_path / "filters.yaml"
        filters.save_filter(path, "mine", "a = 1")

        assert filters.delete_filter(path, "other") is False
        assert "mine" in filters.list_filters(path)

    def test_missing_file_reports_false(self, tmp_path):
        assert filters.delete_filter(tmp_path / "absent.yaml", "mine") is False

    def test_unsafe_name_raises_input_error(self, tmp_path):
        with pytest.raises(InputError):
            filters.delete_filter(tmp_path / "filters.yaml", "has space")


class TestServerFilters:
    def test_normalises_the_favourite_filter_payload(self):
        client = FakeClient(
            {
                filters.SERVER_FAVOURITES: [
                    {
                        "id": 10100,
                        "name": "My open work",
                        "jql": "assignee = currentUser()",
                        "owner": {"name": "alice", "displayName": "Alice"},
                    }
                ]
            }
        )

        assert filters.server_filters(client) == [
            {
                "id": "10100",
                "name": "My open work",
                "jql": "assignee = currentUser()",
                "owner": "alice",
            }
        ]

    def test_falls_back_to_the_display_name_when_owner_has_no_username(self):
        client = FakeClient(
            {
                filters.SERVER_FAVOURITES: [
                    {"id": 1, "name": "f", "jql": "a = 1", "owner": {"displayName": "Bob"}}
                ]
            }
        )

        assert filters.server_filters(client)[0]["owner"] == "Bob"

    def test_tolerates_a_missing_owner(self):
        client = FakeClient({filters.SERVER_FAVOURITES: [{"id": 1, "name": "f", "jql": "a = 1"}]})

        assert filters.server_filters(client)[0]["owner"] is None

    def test_accepts_a_values_wrapped_payload(self):
        client = FakeClient(
            {filters.SERVER_FAVOURITES: {"values": [{"id": 2, "name": "g", "jql": "b = 2"}]}}
        )

        assert [f["name"] for f in filters.server_filters(client)] == ["g"]

    def test_no_favourites_is_an_empty_list(self):
        client = FakeClient({filters.SERVER_FAVOURITES: []})

        assert filters.server_filters(client) == []

    def test_requests_only_the_fixed_favourites_path(self):
        client = FakeClient({filters.SERVER_FAVOURITES: []})

        filters.server_filters(client)

        assert [path for _verb, path, _params in client.calls] == [
            "/rest/api/2/filter/favourite"
        ]


# ======================== people: user input travels as query params, not path


class TestPeopleUrlSafety:
    """A hostile username must never become part of the request path."""

    HOSTILE = "../../project/PROJ?x=1#frag"

    def test_search_users_sends_the_query_as_a_param(self):
        client = FakeClient({"/rest/api/2/user/search": []})

        people.search_users(client, self.HOSTILE)

        assert client.calls == [
            ("GET", "/rest/api/2/user/search", {"username": self.HOSTILE, "maxResults": 50})
        ]

    def test_get_user_sends_the_username_as_a_param(self):
        client = FakeClient({"/rest/api/2/user": {"name": "alice"}})

        people.get_user(client, self.HOSTILE)

        assert client.calls == [("GET", "/rest/api/2/user", {"username": self.HOSTILE})]

    def test_assignable_users_sends_project_and_query_as_params(self):
        client = FakeClient({"/rest/api/2/user/assignable/search": []})

        people.assignable_users(client, "PROJ/../x", self.HOSTILE)

        assert client.calls == [
            (
                "GET",
                "/rest/api/2/user/assignable/search",
                {"project": "PROJ/../x", "username": self.HOSTILE},
            )
        ]


# ====================================== the names cli/menu.py imports must exist


class TestMenuContract:
    @pytest.mark.parametrize(
        "module, name",
        [
            (export, "write_report"),
            (export, "to_markdown"),
            (export, "to_csv"),
            (export, "to_json"),
            (filters, "list_filters"),
            (filters, "save_filter"),
            (filters, "delete_filter"),
            (filters, "get_filter"),
            (filters, "server_filters"),
            (sprint_report, "sprint_report"),
            (sprint_report, "velocity"),
        ],
    )
    def test_public_name_is_callable(self, module, name):
        assert callable(getattr(module, name))

    def test_menu_can_read_both_filter_file_shapes(self, tmp_path):
        """``menu._resolve_jql`` accepts a dict entry or a bare string."""
        path = tmp_path / "filters.yaml"
        path.write_text("legacy: a = 1\n", encoding="utf-8")

        entry = filters.list_filters(path)["legacy"]

        assert isinstance(entry, dict) and entry["jql"] == "a = 1"

    def test_write_report_accepts_a_path_and_a_format_like_the_menu_calls_it(self, tmp_path):
        written = export.write_report(_report(), Path(tmp_path / "report-x.md"), "md")

        assert Path(written).is_file()
