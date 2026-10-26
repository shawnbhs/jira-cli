"""Tests for the numbered people picker (``jira_cli.cli.people_picker``).

Fully offline: people are plain dicts and the terminal is a scripted IO.
"""

from __future__ import annotations

import pytest

from jira_cli.cli import people_picker as pp
from jira_cli.cli.output import display_width

_INTERRUPT = object()
_EOF = object()


class ScriptedIO:
    """``ask`` answers come from a list; ``show`` output is recorded."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def ask(self, prompt, default=None):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("script exhausted")
        answer = self.answers.pop(0)
        if answer is _INTERRUPT:
            raise KeyboardInterrupt
        if answer is _EOF:
            raise EOFError
        return answer

    def show(self, text):
        self.shown.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.shown)


def person(username, name, *, active=True, projects=(), project_admin=(), jira_admin=False):
    return {
        "username": username,
        "display_name": name,
        "active": active,
        "projects": [{"key": key, "roles": list(roles)} for key, roles in projects],
        "project_admin": list(project_admin),
        "jira_admin": jira_admin,
    }


# Deliberately NOT alphabetical: the producer's order must be kept as-is.
PEOPLE = [
    person("carol", "Carol Jones", projects=[("PROJ", ["Developers"])]),
    person("alice", "Alice Smith", projects=[("PROJ", ["Developers"])], jira_admin=True),
    person("bob", "Bob Brown", projects=[("OPS", ["Users"])], project_admin=["OPS"]),
    person("al.white", "Al White", active=False, jira_admin=None),
]


# ------------------------------------------------------------------ rendering


def test_render_numbers_people_in_given_order():
    lines = pp.render_people(PEOPLE, width=120).splitlines()
    assert lines[0].split() == ["#", "Name", "Username", "Projects", "/", "roles", "Admin"]
    body = lines[2:]
    assert [line.split()[0] for line in body] == ["1", "2", "3", "4"]
    assert "carol" in body[0] and "alice" in body[1] and "al.white" in body[3]


def test_render_admin_cell_variants():
    text = pp.render_people(PEOPLE, width=120)
    lines = text.splitlines()[2:]
    assert lines[0].endswith("-")
    assert lines[1].endswith("Jira admin")
    assert lines[2].endswith("admin: OPS")
    assert lines[3].endswith("?")


def test_render_marks_inactive_people():
    text = pp.render_people(PEOPLE, width=120)
    assert "Al White (inactive)" in text
    assert "Carol Jones (inactive)" not in text


def test_render_projects_puts_focus_first_and_caps_at_three():
    many = [
        person(
            "alice",
            "Alice",
            projects=[
                ("AAA", ["Users"]),
                ("BBB", []),
                ("CCC", ["Devs"]),
                ("PROJ", ["Leads"]),
                ("EEE", ["X"]),
            ],
        )
    ]
    text = pp.render_people(many, focus_project="PROJ", width=200)
    assert "PROJ: Leads; AAA: Users; BBB; +2 more" in text


def test_render_joins_multiple_roles_in_one_project():
    one = [person("bob", "Bob", projects=[("PROJ", ["Developers", "Administrators"])])]
    assert "PROJ: Developers, Administrators" in pp.render_people(one, width=200)


def test_render_truncates_projects_cell_to_fit_width():
    wide = [person("alice", "Alice", projects=[("PROJ", ["Developers"] * 20)])]
    text = pp.render_people(wide, width=60)
    assert all(display_width(line) <= 60 for line in text.splitlines())
    assert "\u2026" in text.splitlines()[2]
    assert "alice" in text


def test_render_neutralises_terminal_escapes_in_display_name():
    evil = [person("mallory", "Mal\x1b[2J\x1b]52;c;ZXZpbA==\x07lory\u202e")]
    text = pp.render_people(evil, width=100)
    assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text
    assert "Mal" in text


def test_render_keeps_wide_and_persian_characters_aligned():
    mixed = [
        person("alice", "Alice"),
        person("wang", "\u738b\u5c0f\u660e"),  # CJK, two cells each
        person("user1", "\u0646\u0627\u0645 \u0646\u0645\u0648\u0646\u0647"),  # Persian placeholder
    ]
    lines = pp.render_people(mixed, width=100).splitlines()
    header_offset = display_width(lines[0][: lines[0].index("Username")])
    for line, username in zip(lines[2:], ["alice", "wang", "user1"], strict=True):
        assert display_width(line[: line.index(username)]) == header_offset


# -------------------------------------------------------------------- parsing


def test_parse_numbers_ranges_and_dedup_keep_first_order():
    names, unknown = pp.parse_selection("3, 1-2 3", PEOPLE, multi=True)
    assert names == ["bob", "carol", "alice"]
    assert unknown == []


def test_parse_all_selects_everyone_in_multi_mode():
    assert pp.parse_selection("all", PEOPLE, multi=True) == (
        ["carol", "alice", "bob", "al.white"],
        [],
    )


def test_parse_range_and_all_are_unknown_in_single_mode():
    assert pp.parse_selection("1-2", PEOPLE, multi=False) == ([], ["1-2"])
    assert pp.parse_selection("all", PEOPLE, multi=False) == ([], ["all"])


def test_parse_exact_username_and_display_name_case_insensitive():
    names, unknown = pp.parse_selection("AL.WHITE, alice smith", PEOPLE, multi=True)
    assert names == ["al.white", "alice"]
    assert unknown == []


def test_parse_partial_name_and_out_of_range_are_unknown():
    names, unknown = pp.parse_selection("ali 0 9 2-9", PEOPLE, multi=True)
    assert names == []
    assert unknown == ["ali", "0", "9", "2-9"]


def test_parse_team_token_uses_resolver_with_team_name():
    seen = []

    def resolver(team):
        seen.append(team)
        return ["bob", "carol"] if team == "core" else []

    names, unknown = pp.parse_selection("team:core team:nope", PEOPLE, multi=True, teams_resolver=resolver)
    assert names == ["bob", "carol"]
    assert unknown == ["team:nope"]
    assert seen == ["core", "nope"]


def test_parse_team_token_without_resolver_is_unknown():
    assert pp.parse_selection("team:core", PEOPLE, multi=True) == ([], ["team:core"])


def test_parse_single_mode_with_two_people_raises():
    with pytest.raises(ValueError, match="pick exactly one"):
        pp.parse_selection("1,2", PEOPLE, multi=False)


# -------------------------------------------------------------------- picking


def test_pick_returns_usernames_and_announces_selection():
    io = ScriptedIO(["1,3"])
    assert pp.pick_people(io, PEOPLE) == ["carol", "bob"]
    assert io.shown[-1] == "selected: carol, bob"
    assert io.prompts == [pp.MULTI_PROMPT]


def test_pick_single_mode_uses_single_prompt():
    io = ScriptedIO(["2"])
    assert pp.pick_people(io, PEOPLE, multi=False) == ["alice"]
    assert io.prompts == [pp.SINGLE_PROMPT]


def test_pick_filter_renumbers_and_numbers_follow_filtered_view():
    io = ScriptedIO(["/AL", "1"])
    assert pp.pick_people(io, PEOPLE) == ["alice"]
    filtered = io.shown[1].splitlines()
    assert filtered[0] == "People (2 of 4 people)"
    body = filtered[4:]
    assert [line.split()[0] for line in body] == ["1", "2"]
    assert "alice" in body[0] and "al.white" in body[1]


def test_pick_slash_alone_clears_filter():
    io = ScriptedIO(["/bob", "/", "1"])
    assert pp.pick_people(io, PEOPLE) == ["carol"]


def test_pick_filter_without_matches_keeps_view():
    io = ScriptedIO(["/nobody", "1"])
    assert pp.pick_people(io, PEOPLE) == ["carol"]
    assert "no matches for 'nobody'" in io.text


def test_pick_unknown_token_shows_suggestion_then_retries():
    def suggest(people, token):
        return [p for p in people if token in p["username"]]

    io = ScriptedIO(["al", "al.white"])
    assert pp.pick_people(io, PEOPLE, suggest=suggest) == ["al.white"]
    assert "not found: al" in io.text
    assert "did you mean: alice (Alice Smith), al.white (Al White)" in io.text


def test_pick_does_not_return_while_any_token_is_unknown():
    io = ScriptedIO(["1, ghost", "1"])
    assert pp.pick_people(io, PEOPLE) == ["carol"]
    assert "not found: ghost" in io.text


@pytest.mark.parametrize("answer", ["b", "B", "!back"])
def test_pick_back_returns_none(answer):
    assert pp.pick_people(ScriptedIO([answer]), PEOPLE) is pp.BACK


@pytest.mark.parametrize("signal", [_INTERRUPT, _EOF])
def test_pick_ctrl_c_or_eof_returns_none(signal):
    assert pp.pick_people(ScriptedIO([signal]), PEOPLE) is None


def test_pick_allow_none_zero_returns_empty_list():
    io = ScriptedIO(["0"])
    assert pp.pick_people(io, PEOPLE, allow_none=True, none_label="(unassigned)") == []
    assert "0. (unassigned)" in io.shown[0]


def test_pick_allow_none_enter_returns_empty_list_and_announces_none():
    io = ScriptedIO([""])
    assert pp.pick_people(io, PEOPLE, multi=False, allow_none=True, none_label="(unassigned)") == []
    assert io.shown[-1] == "selected: (unassigned)"
    assert "a choice is required" not in io.text


def test_pick_allow_none_prompt_says_enter_means_none_label():
    io = ScriptedIO(["2"])
    pp.pick_people(io, PEOPLE, multi=False, allow_none=True, none_label="(unassigned)")
    assert io.prompts == ["Pick a number, /text to filter, b to go back, Enter = (unassigned)"]


def test_pick_without_allow_none_prompt_has_no_enter_note():
    io = ScriptedIO(["2"])
    pp.pick_people(io, PEOPLE, multi=False)
    assert "Enter" not in io.prompts[0]


def test_pick_allow_none_with_no_people_enter_returns_empty_list():
    io = ScriptedIO([""])
    assert pp.pick_people(io, [], allow_none=True, none_label="(unassigned)") == []
    assert io.prompts == ["Username (Enter = (unassigned), b to go back)"]
    assert io.shown[-1] == "selected: (unassigned)"


def test_pick_with_no_people_enter_without_allow_none_reprompts():
    io = ScriptedIO(["", "alice"])
    assert pp.pick_people(io, []) == ["alice"]
    assert "a choice is required" in io.text


def test_pick_header_shows_title_count_and_multi_hint():
    io = ScriptedIO(["1"])
    pp.pick_people(io, PEOPLE, title="Watchers")
    lines = io.shown[0].splitlines()
    assert lines[0] == "Watchers (4 people)"
    assert lines[1] == "number(s), 1-3 ranges, all, /text to filter, username, b = back"


def test_pick_header_shows_single_hint_in_single_mode():
    io = ScriptedIO(["1"])
    pp.pick_people(io, PEOPLE, multi=False)
    assert io.shown[0].splitlines()[1] == "one number or username, /text to filter, b = back"


def test_pick_header_counts_one_person_in_singular():
    io = ScriptedIO(["1"])
    pp.pick_people(io, PEOPLE[:1])
    assert io.shown[0].splitlines()[0] == "People (1 person)"


def test_pick_zero_without_allow_none_is_not_found():
    io = ScriptedIO(["0", "2"])
    assert pp.pick_people(io, PEOPLE) == ["alice"]
    assert "not found: 0" in io.text


def test_pick_empty_answer_reprompts():
    io = ScriptedIO(["", "2"])
    assert pp.pick_people(io, PEOPLE) == ["alice"]
    assert "a choice is required" in io.text


def test_pick_single_mode_two_picks_shows_message_and_reprompts():
    io = ScriptedIO(["1,2", "2"])
    assert pp.pick_people(io, PEOPLE, multi=False) == ["alice"]
    assert "pick exactly one" in io.text


def test_pick_team_token():
    io = ScriptedIO(["team:core"])
    assert pp.pick_people(io, PEOPLE, teams_resolver=lambda team: ["bob"]) == ["bob"]


def test_pick_with_no_people_falls_back_to_free_text():
    io = ScriptedIO(["alice, bob"])
    assert pp.pick_people(io, []) == ["alice", "bob"]
    assert "no people to pick from" in io.text
    assert io.prompts == ["Username (b to go back)"]


def test_pick_with_no_people_back_returns_none():
    assert pp.pick_people(ScriptedIO(["b"]), []) is None


def test_pick_with_no_people_single_mode_rejects_two_names():
    io = ScriptedIO(["alice, bob", "alice"])
    assert pp.pick_people(io, [], multi=False) == ["alice"]
    assert "pick exactly one" in io.text
