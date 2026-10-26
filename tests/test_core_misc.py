"""Tests for errors, redact, fields, durations and report."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jira_cli  # noqa: E402
from jira_cli import redact as redact_mod  # noqa: E402
from jira_cli.durations import (  # noqa: E402
    format_duration,
    parse_duration,
    resolve_date,
    to_jira,
)
from jira_cli.errors import (  # noqa: E402
    AuthError,
    ConfigError,
    InputError,
    JiraError,
    JiraPermissionError,
    NotFoundError,
    PartialResultError,
    ReadOnlyError,
)
from jira_cli.fields import FieldMap  # noqa: E402
from jira_cli.redact import MASK, redact, register_secret  # noqa: E402
from jira_cli.report import make_report  # noqa: E402

TODAY = date(2026, 10, 11)


@pytest.fixture(autouse=True)
def clean_registered_secrets():
    """Keep registered literals from leaking between tests."""
    previous = redact_mod.registered_secrets()
    redact_mod.clear_secrets()
    yield
    redact_mod.clear_secrets()
    for literal in previous:
        register_secret(literal)


# --------------------------------------------------------------------------- #
# package
# --------------------------------------------------------------------------- #


def test_version_is_the_contract_version():
    assert jira_cli.__version__ == "2.1.0"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cls",
    [
        AuthError,
        JiraPermissionError,
        NotFoundError,
        ReadOnlyError,
        PartialResultError,
        InputError,
        ConfigError,
    ],
)
def test_every_error_is_a_jira_error(cls):
    assert issubclass(cls, JiraError)


def test_jira_error_carries_status_message_and_url():
    error = JiraError("broken", status=500, url="https://jira.example.com/rest/api/2/x")
    assert error.status == 500
    assert error.message == "broken"
    assert "rest/api/2/x" in error.url
    assert "HTTP 500" in str(error)


def test_jira_error_without_status_omits_it_from_str():
    assert "HTTP" not in str(JiraError("local problem"))


def test_input_error_has_no_status():
    assert InputError("bad value").status is None


def test_config_error_has_no_status():
    assert ConfigError("missing JIRA_BASE_URL").status is None


def test_error_message_is_redacted_on_construction():
    error = JiraError("Authorization: Bearer abcdef123456")
    assert "abcdef123456" not in error.message
    assert "abcdef123456" not in str(error)
    assert "abcdef123456" not in repr(error)


def test_error_url_is_redacted():
    error = JiraError("nope", url="https://jira.example.com/x?token=abcdef123456")  # gitleaks:allow
    assert "abcdef123456" not in error.url


def test_a_registered_secret_is_redacted_inside_an_error():
    register_secret("my-live-token-999")
    error = JiraError("server said: my-live-token-999")
    assert "my-live-token-999" not in str(error)


def test_error_is_catchable_as_an_exception():
    with pytest.raises(JiraError):
        raise NotFoundError("gone", status=404)


# --------------------------------------------------------------------------- #
# redact
# --------------------------------------------------------------------------- #


def test_bearer_value_is_masked():
    assert redact("Authorization: Bearer abc123xyz789") == f"Authorization: Bearer {MASK}"


def test_basic_value_is_masked():
    assert "dXNlcjpwYXNz" not in redact("Authorization: Basic dXNlcjpwYXNz")


def test_bearer_is_matched_case_insensitively():
    assert "abc123xyz789" not in redact("authorization: bearer abc123xyz789")


@pytest.mark.parametrize(
    "name", ["JIRA_PAT", "JIRA_PASSWORD", "JIRA_API_TOKEN"]
)
def test_secret_assignments_are_masked(name):
    assert redact(f"{name}=super-secret-value") == f"{name}={MASK}"


def test_quoted_assignment_value_is_masked():
    assert "super-secret" not in redact('JIRA_PAT="super-secret"')


def test_export_prefixed_assignment_is_masked():
    assert "super-secret" not in redact("export JIRA_PASSWORD=super-secret")


def test_token_query_parameter_is_masked():
    assert "abc123" not in redact("https://jira.example.com/x?token=abc123&y=1")


def test_password_query_parameter_is_masked():
    assert "hunter2" not in redact("https://jira.example.com/x?password=hunter2")


def test_url_userinfo_password_is_masked():
    masked = redact("https://alice:hunter2@jira.example.com/rest")
    assert "hunter2" not in masked
    assert "alice" in masked


def test_registered_literal_is_masked_anywhere():
    register_secret("literal-token-abcdef")
    assert redact("oops literal-token-abcdef oops") == f"oops {MASK} oops"


def test_very_short_literals_are_not_registered():
    register_secret("abc")
    assert redact("abc stays") == "abc stays"


def test_registering_none_is_harmless():
    register_secret(None)
    assert redact("nothing to do") == "nothing to do"


def test_ordinary_text_is_untouched():
    text = "PROJ-12 moved to In Progress by alice"
    assert redact(text) == text


def test_redact_accepts_non_strings():
    assert redact(None) == ""
    assert redact(42) == "42"


def test_redact_handles_an_exception_object():
    assert "abc123xyz" not in redact(JiraError("Bearer abc123xyz"))


# --------------------------------------------------------------------------- #
# fields
# --------------------------------------------------------------------------- #


class FakeFieldClient:
    """Serves one ``/rest/api/2/field`` payload and counts the calls."""

    def __init__(self, payload=None, overrides=None, raises=None):
        self.payload = payload if payload is not None else []
        self.raises = raises
        self.calls = 0

        class _Settings:
            field_overrides = dict(overrides or {})

        self.settings = _Settings()

    def get(self, path, params=None):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.payload


FIELD_PAYLOAD = [
    {"id": "summary", "name": "Summary"},
    {"id": "customfield_10109", "name": "Sprint"},
    {"id": "customfield_10108", "name": "Epic Link"},
    {"id": "customfield_10104", "name": "Epic Name"},
    {"id": "customfield_10110", "name": "Story Points"},
]


def test_named_properties_resolve_to_their_field_id():
    fmap = FieldMap(FakeFieldClient(FIELD_PAYLOAD))
    assert fmap.sprint == "customfield_10109"
    assert fmap.epic_link == "customfield_10108"
    assert fmap.epic_name == "customfield_10104"
    assert fmap.story_points == "customfield_10110"


def test_lookup_is_case_insensitive():
    fmap = FieldMap(FakeFieldClient(FIELD_PAYLOAD))
    assert fmap.id("story points") == "customfield_10110"
    assert fmap.id("STORY POINTS") == "customfield_10110"


def test_field_list_is_fetched_only_once():
    client = FakeFieldClient(FIELD_PAYLOAD)
    fmap = FieldMap(client)
    assert fmap.sprint is not None
    assert fmap.epic_link is not None
    fmap.id("Summary")
    assert client.calls == 1


def test_unknown_field_name_returns_none():
    assert FieldMap(FakeFieldClient(FIELD_PAYLOAD)).id("Nonexistent") is None


def test_empty_field_name_returns_none():
    assert FieldMap(FakeFieldClient(FIELD_PAYLOAD)).id("  ") is None


def test_override_wins_over_the_server_and_skips_the_fetch():
    client = FakeFieldClient(FIELD_PAYLOAD, overrides={"SPRINT": "customfield_999"})
    assert FieldMap(client).sprint == "customfield_999"
    assert client.calls == 0


def test_override_can_be_given_with_a_display_name_key():
    client = FakeFieldClient(FIELD_PAYLOAD, overrides={"Story Points": "customfield_7"})
    assert FieldMap(client).story_points == "customfield_7"


def test_a_failed_field_fetch_degrades_to_no_fields():
    client = FakeFieldClient(raises=JiraError("field list forbidden", status=403))
    fmap = FieldMap(client)
    assert fmap.sprint is None
    assert fmap.story_points is None
    assert client.calls == 1  # the failure is cached, not retried per lookup


def test_malformed_field_entries_are_skipped():
    fmap = FieldMap(FakeFieldClient(["junk", {"name": "No Id"}, {"id": "x", "name": "Ok"}]))
    assert fmap.id("Ok") == "x"
    assert fmap.id("No Id") is None


def test_repr_does_not_blow_up_before_loading():
    assert "unloaded" in repr(FieldMap(FakeFieldClient(FIELD_PAYLOAD)))


# --------------------------------------------------------------------------- #
# durations: parse
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30m", 1800),
        ("90m", 5400),
        ("1h", 3600),
        ("1.5h", 5400),
        ("2d", 2 * 8 * 3600),
        ("1w", 5 * 8 * 3600),
        ("1w 2d 3h 30m", (5 + 2) * 8 * 3600 + 3 * 3600 + 1800),
        ("1d2h", 8 * 3600 + 2 * 3600),
        ("  3h   ", 3 * 3600),
        ("45s", 45),
    ],
)
def test_parse_duration_reads_jira_expressions(text, seconds):
    assert parse_duration(text) == seconds


def test_parse_duration_is_case_insensitive():
    assert parse_duration("2D 3H") == 2 * 8 * 3600 + 3 * 3600


def test_parse_duration_uses_custom_day_and_week_rates():
    assert parse_duration("1d", hours_per_day=6) == 6 * 3600
    assert parse_duration("1w", hours_per_day=6, days_per_week=4) == 4 * 6 * 3600


@pytest.mark.parametrize("text", ["", "   ", None])
def test_parse_duration_requires_text(text):
    with pytest.raises(InputError, match="required"):
        parse_duration(text)


@pytest.mark.parametrize("text", ["soon", "3", "3 potatoes", "3hx", "h", "-2h"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(InputError):
        parse_duration(text)


def test_parse_duration_rejects_zero():
    with pytest.raises(InputError, match="greater than zero"):
        parse_duration("0h")


def test_parse_duration_rejects_non_positive_rates():
    with pytest.raises(InputError, match="positive"):
        parse_duration("1d", hours_per_day=0)


# --------------------------------------------------------------------------- #
# durations: format
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, "-"),
        (0, "-"),
        (-5, "-"),
        (2700, "45m"),
        (3600, "1h"),
        (8 * 3600, "1d"),
        (8 * 3600 + 2 * 3600, "1d 2h"),
        (8 * 3600 + 2 * 3600 + 1800, "1d 2h 30m"),
        (5 * 8 * 3600, "5d"),
    ],
)
def test_format_duration_renders_compactly(seconds, text):
    assert format_duration(seconds, hours_per_day=8) == text


def test_format_duration_honours_a_custom_day_length():
    assert format_duration(6 * 3600, hours_per_day=6) == "1d"


def test_format_duration_tolerates_a_non_numeric_value():
    assert format_duration("lots") == "-"


def test_format_duration_keeps_sub_minute_values_visible():
    assert format_duration(30) == "30s"


@pytest.mark.parametrize("text", ["1.5h", "90m", "2d", "1w 2d 3h 30m"])
def test_parse_then_format_round_trips(text):
    assert parse_duration(format_duration(parse_duration(text))) == parse_duration(text)


# --------------------------------------------------------------------------- #
# durations: to_jira
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "normalised"),
    [("90m", "1h 30m"), ("1.5h", "1h 30m"), ("2d", "2d"), ("1w", "5d"), ("3h", "3h")],
)
def test_to_jira_normalises(text, normalised):
    assert to_jira(text) == normalised


def test_to_jira_validates():
    with pytest.raises(InputError):
        to_jira("whenever")


# --------------------------------------------------------------------------- #
# durations: resolve_date
# --------------------------------------------------------------------------- #


def test_resolve_date_passes_an_explicit_date_through():
    assert resolve_date("2026-01-31", TODAY) == "2026-01-31"


def test_resolve_date_understands_today_and_yesterday():
    assert resolve_date("today", TODAY) == "2026-10-11"
    assert resolve_date("yesterday", TODAY) == "2026-10-10"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1d", "2026-10-10"),
        ("7d", "2026-10-04"),
        ("2w", "2026-09-27"),
        ("1m", "2026-09-11"),
        ("0d", "2026-10-11"),
    ],
)
def test_resolve_date_counts_relative_ages_backwards(text, expected):
    assert resolve_date(text, TODAY) == expected


def test_resolve_date_is_case_insensitive():
    assert resolve_date("TODAY", TODAY) == "2026-10-11"
    assert resolve_date("7D", TODAY) == "2026-10-04"


def test_resolve_date_defaults_to_the_real_today():
    assert resolve_date("today") == date.today().isoformat()


@pytest.mark.parametrize("text", ["", "   ", None])
def test_resolve_date_requires_text(text):
    with pytest.raises(InputError, match="required"):
        resolve_date(text)


@pytest.mark.parametrize("text", ["tomorrow", "last week", "2026/10/11", "10-11-2026", "7y"])
def test_resolve_date_rejects_unknown_expressions(text):
    with pytest.raises(InputError):
        resolve_date(text, TODAY)


def test_resolve_date_rejects_an_impossible_calendar_date():
    with pytest.raises(InputError, match="not a real date"):
        resolve_date("2026-02-30", TODAY)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def test_make_report_has_the_contract_keys():
    report = make_report("Title", ["key"], [{"key": "PROJ-1"}])
    assert set(report) == {"title", "columns", "rows", "summary", "meta"}


def test_make_report_keeps_column_order():
    report = make_report("T", ["b", "a"], [{"a": 1, "b": 2}])
    assert report["columns"] == ["b", "a"]
    assert list(report["rows"][0]) == ["b", "a"]


def test_missing_cells_become_none():
    assert make_report("T", ["a", "b"], [{"a": 1}])["rows"][0] == {"a": 1, "b": None}


def test_extra_row_keys_are_dropped():
    assert make_report("T", ["a"], [{"a": 1, "zzz": 2}])["rows"][0] == {"a": 1}


def test_generated_at_is_always_present_and_utc():
    meta = make_report("T", [], [])["meta"]
    assert meta["generated_at"].endswith("Z")


def test_caller_meta_is_merged_and_can_carry_jql():
    report = make_report("T", [], [], meta={"jql": ["project = PROJ"]})
    assert report["meta"]["jql"] == ["project = PROJ"]
    assert "generated_at" in report["meta"]


def test_summary_defaults_to_an_empty_dict():
    assert make_report("T", [], [])["summary"] == {}


def test_summary_is_passed_through():
    assert make_report("T", [], [], summary={"issues": 3})["summary"] == {"issues": 3}


def test_scalars_survive_unchanged():
    row = {"s": "x", "i": 1, "f": 1.5, "b": True, "n": None}
    assert make_report("T", list(row), [row])["rows"][0] == row


def test_lists_are_flattened_to_comma_joined_text():
    report = make_report("T", ["labels"], [{"labels": ["a", "b"]}])
    assert report["rows"][0]["labels"] == "a, b"


def test_dicts_are_flattened_to_key_value_text():
    report = make_report("T", ["who"], [{"who": {"name": "alice"}}])
    assert report["rows"][0]["who"] == "name=alice"


def test_an_object_cell_is_stringified():
    assert isinstance(make_report("T", ["p"], [{"p": Path("x")}])["rows"][0]["p"], str)


def test_a_non_dict_row_becomes_an_empty_row():
    assert make_report("T", ["a"], ["junk"])["rows"][0] == {"a": None}


def test_no_rows_and_no_columns_is_valid():
    report = make_report("Empty", [], [])
    assert report["rows"] == []
    assert report["columns"] == []


def test_report_is_json_serialisable():
    import json

    report = make_report("T", ["a"], [{"a": ["x", "y"]}], summary={"n": 1})
    assert json.loads(json.dumps(report))["rows"][0]["a"] == "x, y"
