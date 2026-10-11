"""Server-controlled text must never drive the terminal (review finding F1).

Every human-readable renderer in :mod:`jira_cli.cli.output` strips control
characters; the JSON renderer keeps the data but escapes C1 and bidi
characters so they are never emitted raw.
"""

from __future__ import annotations

import json

import pytest

from jira_cli.cli import output

OSC52_CLIPBOARD = "\x1b]52;c;ZWNobyBwd25lZA==\x07"
CSI_CLEAR_LINE = "\x1b[2K"
CR_REWRITE = "harmless\rrm -rf ~"
CSI_8BIT = "\x9b31m"
BIDI_OVERRIDE = "\u202eexe.txt\u202c"

PAYLOADS = {
    "osc52": OSC52_CLIPBOARD,
    "csi_clear_line": CSI_CLEAR_LINE,
    "carriage_return": CR_REWRITE,
    "csi_8bit": CSI_8BIT,
    "bidi_override": BIDI_OVERRIDE,
}

#: No raw character from any payload may survive into rendered text.
FORBIDDEN = ("\x1b", "\x07", "\r", "\x9b", "\u202e", "\u202c")

PERSIAN = "\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627"


def _assert_inert(text: str) -> None:
    leaked = [repr(char) for char in FORBIDDEN if char in text]
    assert not leaked, f"raw control characters reached output: {leaked}"


def _issue(payload: str) -> dict:
    return {
        "key": f"PROJ-1{payload}",
        "summary": f"summary {payload}",
        "url": f"https://jira.example.com/browse/PROJ-1{payload}",
        "status": f"Open{payload}",
        "assignee": f"alice{payload}",
        "labels": [f"label{payload}"],
        "description": f"line one {payload}\nline two",
        "comments": [
            {"author": f"bob{payload}", "created": "2026-01-01", "body": f"hi {payload}\nbye"}
        ],
    }


def _report(payload: str) -> dict:
    return {
        "title": f"title {payload}",
        "columns": ["key", f"col{payload}"],
        "rows": [{"key": f"PROJ-1{payload}", f"col{payload}": {"k": f"v{payload}"}}],
        "summary": {f"total{payload}": f"1{payload}"},
        "meta": {"jql": [f"project = PROJ{payload}"]},
    }


@pytest.fixture(autouse=True)
def _no_color():
    output.set_color(False)
    yield
    output.set_color(None)


@pytest.mark.parametrize("payload", PAYLOADS.values(), ids=PAYLOADS.keys())
def test_print_issue_writes_no_raw_control_characters(payload, capsys):
    output.print_issue(_issue(payload))
    captured = capsys.readouterr()
    _assert_inert(captured.out + captured.err)


@pytest.mark.parametrize("payload", PAYLOADS.values(), ids=PAYLOADS.keys())
def test_render_table_cells_and_headers_are_inert(payload):
    rendered = output.render_table(
        ["key", f"head{payload}"], [{"key": f"PROJ-1{payload}", f"head{payload}": payload}],
        max_width=200,
    )
    _assert_inert(rendered)


@pytest.mark.parametrize("fmt", ["table", "md", "csv"])
@pytest.mark.parametrize("payload", PAYLOADS.values(), ids=PAYLOADS.keys())
def test_text_report_formats_are_inert(payload, fmt):
    rendered = output.render_report(_report(payload), fmt, max_width=200)
    # CSV keeps its CRLF row terminator; only a CR inside a value is unsafe.
    _assert_inert(rendered.replace("\r\n", "\n"))


@pytest.mark.parametrize("fmt", ["md", "csv"])
def test_local_fallback_renderers_are_inert(fmt, monkeypatch):
    monkeypatch.setattr(output, "_export_module", lambda: None)
    rendered = output.render_report(_report(OSC52_CLIPBOARD + CR_REWRITE), fmt)
    _assert_inert(rendered)


@pytest.mark.parametrize("payload", PAYLOADS.values(), ids=PAYLOADS.keys())
def test_json_report_is_inert_but_round_trips(payload):
    report = _report(payload)
    rendered = output.render_report(report, "json")
    _assert_inert(rendered)
    assert json.loads(rendered) == report


def test_json_fallback_escapes_8bit_csi_and_bidi_as_unicode_escapes(monkeypatch):
    monkeypatch.setattr(output, "_export_module", lambda: None)
    rendered = output.render_report({"title": CSI_8BIT + BIDI_OVERRIDE}, "json")
    assert "\\u009b" in rendered
    assert "\\u202e" in rendered


def test_dumps_json_keeps_persian_readable_and_round_trips():
    value = {"summary": PERSIAN, "danger": "\x1b]52;c;x\x07\x9b"}
    rendered = output.dumps_json(value)
    assert PERSIAN in rendered
    _assert_inert(rendered)
    assert json.loads(rendered) == value


def test_confirm_prompt_is_inert(capsys):
    output.confirm(f"Delete {OSC52_CLIPBOARD}{CR_REWRITE}?", assume_yes=True)
    _assert_inert(capsys.readouterr().out)


def test_issue_text_preserves_persian_and_body_newlines():
    issue = {
        "key": "PROJ-1",
        "summary": PERSIAN,
        "description": f"{PERSIAN}\nsecond line\n\tindented",
        "comments": [{"author": "alice", "created": "2026-01-01", "body": "a\nb"}],
    }
    text = output.issue_text(issue)
    assert f"PROJ-1  {PERSIAN}" in text
    assert f"{PERSIAN}\nsecond line\n\tindented" in text
    assert "     a\n     b" in text


def test_payload_text_around_the_control_characters_survives():
    text = output.issue_text({"key": "PROJ-1", "summary": f"before{CSI_CLEAR_LINE}after"})
    assert "before[2Kafter" in text


def test_table_cell_folds_newlines_so_rows_cannot_be_faked():
    rendered = output.render_table(["summary"], [{"summary": "one\ntwo\tthree"}], max_width=60)
    assert rendered.splitlines()[2] == "one two three"


def test_color_still_emits_its_own_ansi_after_sanitising():
    output.set_color(True)
    text = output.issue_text({"key": "PROJ-1", "summary": f"x{OSC52_CLIPBOARD}"})
    heading = text.splitlines()[0]
    assert heading.startswith("\x1b[1m") and heading.endswith("\x1b[0m")
    assert "\x1b]" not in heading
    assert "\x07" not in heading
