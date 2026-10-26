"""Regression tests for output and id safety.

* A CSV cell whose formula character hides behind a control character
  must stay inert on BOTH the ``--out`` file path and the stdout path.
* Server labels shown by the menu, and anything ``MenuIO`` prints, must
  not carry terminal escape sequences.
* ``sprint_report`` refuses a non-integer board/sprint id before any
  request is made.
* Issue keys accept ASCII digits only.
"""

from __future__ import annotations

import pytest

from jira_cli import ids
from jira_cli.cli import menu, menu_actions, output
from jira_cli.errors import InputError
from jira_cli.services import export, sprint_report
from jira_cli.textsafe import csv_safe_cell

#: Formula payloads whose first character is a control, DEL or bidi character.
HIDDEN_FORMULAS = {
    "esc": '\x1b=HYPERLINK("http://example.com/?x="&A1,"click")',
    "nul": "\x00=1+1",
    "del": "\x7f@SUM(1+1)",
    "rlo": "\u202e=cmd|' /C calc'!A0",
    "c1_csi": "\x9b+1+1",
    "cr": "\r-1+1",
    "leading_space": " =1+1",
}

RAW_CONTROLS = ("\x00", "\x1b", "\x7f", "\x9b", "\u202e", "\r")


def _report(value: str) -> dict:
    return {"title": "t", "columns": ["key", "summary"], "rows": [{"key": "ABC-1", "summary": value}]}


def _summary_cell(csv_text: str) -> str:
    import csv
    import io

    rows = list(csv.reader(io.StringIO(csv_text, newline="")))
    return rows[1][1]


# ------------------------------------------------------------------ CSV formula escaping


@pytest.mark.parametrize("payload", HIDDEN_FORMULAS.values(), ids=HIDDEN_FORMULAS.keys())
def test_stdout_csv_cell_with_hidden_formula_is_quoted(payload):
    cell = _summary_cell(output.render_report(_report(payload), "csv"))
    assert cell.startswith("'")
    assert not any(char in cell for char in RAW_CONTROLS)


@pytest.mark.parametrize("payload", HIDDEN_FORMULAS.values(), ids=HIDDEN_FORMULAS.keys())
def test_file_csv_cell_with_hidden_formula_is_quoted_and_clean(payload):
    cell = _summary_cell(export.to_csv(_report(payload)))
    assert cell.startswith("'")
    assert not any(char in cell for char in RAW_CONTROLS)


@pytest.mark.parametrize("payload", HIDDEN_FORMULAS.values(), ids=HIDDEN_FORMULAS.keys())
def test_fallback_csv_renderer_quotes_hidden_formula(payload):
    cell = _summary_cell(output.report_to_csv(_report(payload)))
    assert cell.startswith("'")


def test_stdout_csv_and_file_csv_are_identical_for_hostile_values():
    report = {
        "title": "t",
        "columns": ["key", "summary"],
        "rows": [{"key": f"ABC-{n}", "summary": v} for n, v in enumerate(HIDDEN_FORMULAS.values(), 1)],
    }
    assert output.render_report(report, "csv") == export.to_csv(report)


def test_csv_header_with_hidden_formula_is_quoted():
    report = {"title": "t", "columns": ["\x1b=1+1"], "rows": [{"\x1b=1+1": "x"}]}
    header = output.render_report(report, "csv").split("\r\n", 1)[0]
    assert header == "'=1+1"


def test_csv_keeps_plain_text_and_newlines_inside_a_cell():
    assert _summary_cell(export.to_csv(_report("line one\nline two"))) == "line one\nline two"
    assert csv_safe_cell("Fix login page") == "Fix login page"


def test_csv_safe_cell_is_idempotent_on_clean_values():
    once = csv_safe_cell("\x1b=1+1")
    assert once == "'=1+1"
    assert csv_safe_cell(once) == once


# ------------------------------------------------------------------ terminal-safe labels

EVIL_LABEL = "ok\x1b]52;c;ZWNobyBwd25lZA==\x07\x1b[2K\x1b[1A"


class _ScriptedIO:
    def __init__(self):
        self.shown: list[str] = []

    def show(self, text):
        self.shown.append(text)

    def choose(self, title, options):
        return 1  # "Remove labels"

    def ask(self, prompt, default=None):
        raise EOFError

    def confirm(self, prompt):
        return False


class _Session:
    def __init__(self):
        self.io = _ScriptedIO()
        self.client = None
        self.settings = None

    def say(self, text):
        self.io.show(text)


def test_remove_labels_list_strips_escape_sequences():
    session = _Session()
    with pytest.raises(menu._Back):  # the scripted IO ends input after the list
        menu_actions._labels_after(session, ["safe", EVIL_LABEL])
    listing = session.io.shown[0]
    assert "\x1b" not in listing and "\x07" not in listing
    assert "ok]52;c;" in listing and "safe" in listing


def test_menuio_show_strips_control_characters(capsys):
    menu.MenuIO().show("title\x1b]0;pwned\x07\nnext\x9b31m line")
    assert capsys.readouterr().out == "title]0;pwned\nnext31m line\n"


def test_menuio_show_leaves_clean_text_unchanged(capsys):
    text = "PROJ-1  \u0633\u0644\u0627\u0645\n\tindented"
    menu.MenuIO().show(text)
    assert capsys.readouterr().out == text + "\n"


def test_menuio_choose_strips_control_characters_from_title_and_options(capsys, monkeypatch):
    monkeypatch.setattr(menu, "_read_line", lambda prompt: "1")
    choice = menu.MenuIO().choose("Pick\x1b[2J", ["one\x1b]52;c;eA==\x07", "two"])
    out = capsys.readouterr().out
    assert choice == 0
    assert "\x1b" not in out and "\x07" not in out
    assert "Pick[2J" in out and "one]52;c;eA==" in out


# ------------------------------------------------------------------ sprint report ids


class _NoNetworkClient:
    def __init__(self, active_id=None):
        self.calls: list[str] = []
        self._active_id = active_id

    def get(self, path, params=None):
        self.calls.append(path)
        if path.endswith("/sprint") and self._active_id is not None:
            return {"values": [{"id": self._active_id}]}
        raise AssertionError(f"unexpected request {path}")

    def paginate(self, path, params=None, **kwargs):
        self.calls.append(path)
        raise AssertionError(f"unexpected request {path}")


@pytest.mark.parametrize("bad", ["ABC-1?deleteSubtasks=true", "1/../2", "0", -3, True, "\u0661\u0662"])
def test_sprint_report_rejects_non_integer_sprint_id_before_any_request(bad):
    client = _NoNetworkClient()
    with pytest.raises(InputError):
        sprint_report.sprint_report(client, 7, bad)
    assert client.calls == []


@pytest.mark.parametrize("bad", ["7/../1", "x", 0])
def test_sprint_report_rejects_bad_board_id_before_any_request(bad):
    client = _NoNetworkClient()
    with pytest.raises(InputError):
        sprint_report.sprint_report(client, bad, 42)
    assert client.calls == []


def test_velocity_rejects_bad_board_id_before_any_request():
    client = _NoNetworkClient()
    with pytest.raises(InputError):
        sprint_report.velocity(client, "7?x=1")
    assert client.calls == []


def test_sprint_report_rejects_malformed_discovered_sprint_id():
    client = _NoNetworkClient(active_id="1/../../issue/ABC-1")
    with pytest.raises(InputError):
        sprint_report.sprint_report(client, 7)
    assert client.calls == ["/rest/agile/1.0/board/7/sprint"]


# ------------------------------------------------------------------ ASCII issue keys


@pytest.mark.parametrize("bad", ["ABC-\u0661\u0662", "ABC-\uff11", "ABC-\u09e7"])
def test_issue_key_rejects_non_ascii_digits(bad):
    with pytest.raises(InputError):
        ids.validate_issue_key(bad)


def test_issue_key_still_accepts_ascii_key():
    assert ids.validate_issue_key(" abc-12 ") == "ABC-12"
    assert ids.ISSUE_KEY_RE.match("ABC-\u0661") is None
