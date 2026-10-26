"""Tests for menu item 7, Find issue (``jira_cli.cli.menu_find``).

Fully offline: a scripted IO replaces the terminal and the issues / filters
services are fakes installed in ``sys.modules``.  Report rendering is the
real ``cli.output`` so the paging line is checked as the operator sees it.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from jira_cli.cli import menu, menu_actions, menu_find
from jira_cli.errors import InputError, JiraError, NotFoundError


class ScriptedIO:
    """Answers come from a script of ``(kind, value)``; prompts and defaults are recorded."""

    def __init__(self, script):
        self.script = list(script)
        self.shown: list[str] = []
        self.asked: list[tuple] = []

    def _answer(self, kind):
        if not self.script:
            raise AssertionError(f"script exhausted; menu asked for a {kind}")
        expected, value = self.script.pop(0)
        if expected != kind:
            raise AssertionError(f"script expected a {expected}, menu asked for a {kind}")
        return value

    def ask(self, prompt, default=None):
        self.asked.append(("ask", prompt, default))
        return self._answer("ask")

    def choose(self, title, options):
        self.asked.append(("choose", title, list(options)))
        return self._answer("choose")

    def confirm(self, prompt):
        self.asked.append(("confirm", prompt))
        return self._answer("confirm")

    def show(self, text):
        self.shown.append(str(text))

    @property
    def text(self):
        return "\n".join(self.shown)


@dataclass
class FakeSettings:
    base_url: str = "https://jira.example.com"
    default_project: str = "PROJ"
    filters_file: Path = Path("filters.yaml")


def _issue(number):
    key = f"PROJ-{number}"
    return {
        "key": key,
        "url": f"https://jira.example.com/browse/{key}",
        "summary": f"Issue {number}",
        "type": "Task",
        "status": "To Do",
        "assignee": "alice",
        "priority": "Medium",
        "due": None,
        "project": "PROJ",
        "labels": [],
        "comments": [],
    }


class FakeJira:
    """Fake client + issues/filters services sharing one recorded state."""

    def __init__(self):
        self.hits = [_issue(n) for n in range(1, 121)]
        self.searches: list[tuple[str, int]] = []
        self.local_filters: dict | Exception = {}
        self.server_favourites: list[dict] | Exception = []

    # client
    def count(self, jql):
        self._check(jql)
        return len(self.hits)

    def _check(self, jql):
        if jql == "bad jql":
            raise JiraError("Error in the JQL Query", status=400)
        if jql == "outage":
            raise JiraError("Service Unavailable", status=503)

    # issues service
    def search_issues(self, client, jql, limit=50):
        self._check(jql)
        self.searches.append((jql, limit))
        return self.hits[:limit]

    def get_issue(self, client, key, comments=True, changelog=False):
        number = int(key.split("-")[1])
        if not 1 <= number <= len(self.hits):
            raise NotFoundError("Issue Does Not Exist", status=404)
        return self.hits[number - 1]

    # filters service
    def list_filters(self, path):
        if isinstance(self.local_filters, Exception):
            raise self.local_filters
        return self.local_filters

    def server_filters(self, client):
        if isinstance(self.server_favourites, Exception):
            raise self.server_favourites
        return self.server_favourites


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setitem(
        sys.modules,
        "jira_cli.services.issues",
        _module("jira_cli.services.issues", search_issues=fake.search_issues, get_issue=fake.get_issue),
    )
    monkeypatch.setitem(
        sys.modules,
        "jira_cli.services.filters",
        _module(
            "jira_cli.services.filters", list_filters=fake.list_filters, server_filters=fake.server_filters
        ),
    )
    return fake


@pytest.fixture
def opened(monkeypatch):
    """Replace Issue actions with a recorder; returns the list of opened keys."""
    keys: list[str] = []
    monkeypatch.setattr(menu_actions, "actions_for", lambda session, key, **kw: keys.append(key))
    return keys


def _run(jira, script):
    session = menu._Session(client=jira, settings=FakeSettings(), io=ScriptedIO(script))
    menu._run_action(session, menu_find.run)
    assert not session.io.script, f"unused answers: {session.io.script}"
    return session


KEY, JQL, FILTER, RECENT = (("choose", n) for n in range(4))
BACK = ("choose", None)


# ------------------------------------------------------------------- modes


def test_find_starts_with_a_numbered_choice(jira):
    session = _run(jira, [BACK])
    title, options = session.io.asked[0][1:]
    assert title == "Find issue"
    assert options[:3] == [
        "Issue key or browse URL",
        "JQL search",
        "Saved filter (local or server favourite)",
    ]
    assert options[3] == "Recent issue (none yet)"


def test_pasted_browse_url_opens_the_issue_and_offers_actions(jira, opened):
    session = _run(
        jira, [KEY, ("ask", "https://jira.example.com/browse/PROJ-46?focusedCommentId=1"), ("ask", "a"), BACK]
    )
    assert "PROJ-46  Issue 46" in session.io.text
    assert opened == ["PROJ-46"]
    assert session.last_key == "PROJ-46"


def test_unknown_key_is_re_asked(jira, opened):
    session = _run(jira, [KEY, ("ask", "PROJ-99999"), ("ask", "PROJ-2"), ("ask", "b"), BACK])
    assert "PROJ-99999 does not exist" in session.io.text
    assert opened == []
    assert session.last_key == "PROJ-2"


# ------------------------------------------------------------------ paging


def test_jql_results_say_how_many_are_shown_and_page_forward(jira):
    session = _run(
        jira,
        [JQL, ("ask", "project = PROJ"), ("ask", "n"), ("ask", "n"), ("ask", "n"), ("ask", "b"), BACK],
    )
    assert "showing 1-50 of 120" in session.io.text
    assert "showing 51-100 of 120" in session.io.text
    assert "showing 101-120 of 120" in session.io.text
    assert "this is the last page" in session.io.text
    assert session.last_report["summary"]["showing"] == "showing 101-120 of 120"
    assert [row["#"] for row in session.last_report["rows"]][:2] == [101, 102]


def test_prompt_offers_next_page_only_when_there_is_more(jira):
    jira.hits = jira.hits[:3]
    session = _run(jira, [JQL, ("ask", "project = PROJ"), ("ask", "b"), BACK])
    prompts = [entry[1] for entry in session.io.asked if entry[0] == "ask"]
    assert "showing 1-3 of 3" in session.io.text
    assert "n)ext page" not in prompts[-1]


def test_bad_jql_shows_the_server_error_and_keeps_the_typed_query(jira):
    session = _run(jira, [JQL, ("ask", "bad jql"), ("ask", "project = PROJ"), ("ask", "b"), BACK])
    assert "error: Error in the JQL Query (HTTP 400)" in session.io.text
    second_jql_prompt = [entry for entry in session.io.asked if entry[:2] == ("ask", "JQL")][1]
    assert second_jql_prompt[2] == "bad jql"


def test_server_outage_is_not_mistaken_for_a_bad_query(jira):
    session = _run(jira, [JQL, ("ask", "outage")])
    assert "error: Service Unavailable (HTTP 503)" in session.io.text


def test_row_number_opens_issue_actions(jira, opened):
    session = _run(jira, [JQL, ("ask", "project = PROJ"), ("ask", "7"), ("ask", "99"), ("ask", "b"), BACK])
    assert opened == ["PROJ-7"]
    assert "row 99 is not on this page (rows 1-50)" in session.io.text


# ----------------------------------------------------------------- filters


def test_saved_filters_list_local_and_server_and_match_names_case_insensitively(jira):
    jira.local_filters = {"icsrecent": {"jql": "project = PROJ ORDER BY updated DESC", "description": ""}}
    jira.server_favourites = [{"id": "1", "name": "Team board", "jql": "project = PROJ AND status = Open"}]
    session = _run(jira, [FILTER, ("ask", "ICSRECENT"), ("ask", "b"), BACK])
    assert jira.searches[0][0] == "project = PROJ ORDER BY updated DESC"
    assert "Filter: icsrecent" in session.last_report["title"]


def test_filter_named_like_an_issue_key_is_picked_by_number(jira):
    jira.local_filters = {"sprint-12": {"jql": "sprint = 12", "description": ""}}
    _run(jira, [FILTER, ("ask", "1"), ("ask", "b"), BACK])
    assert jira.searches[0][0] == "sprint = 12"


def test_unknown_filter_name_is_re_asked_not_sent_as_jql(jira):
    jira.server_favourites = [{"id": "1", "name": "Team board", "jql": "project = PROJ"}]
    session = _run(jira, [FILTER, ("ask", "foobarbaz"), ("ask", "3"), ("ask", "1"), ("ask", "b"), BACK])
    assert "no saved filter named 'foobarbaz'" in session.io.text
    assert "pick a number from 1 to 1" in session.io.text
    assert [jql for jql, _ in jira.searches] == ["project = PROJ"]


def test_broken_filters_file_shows_the_real_error_and_still_offers_server_favourites(jira):
    jira.local_filters = InputError("filters.yaml must contain a mapping of filter name to filter")
    jira.server_favourites = [{"id": "1", "name": "Team board", "jql": "project = PROJ"}]
    session = _run(jira, [FILTER, ("ask", "1"), ("ask", "b"), BACK])
    assert "error: filters.yaml must contain a mapping" in session.io.text
    assert jira.searches[0][0] == "project = PROJ"


def test_no_filters_anywhere_says_so(jira):
    session = _run(jira, [FILTER, BACK])
    assert "no saved filters" in session.io.text


# ------------------------------------------------------------------ recent


def test_recent_lists_keys_used_earlier_in_the_session(jira, opened):
    session = _run(
        jira,
        [KEY, ("ask", "PROJ-3"), ("ask", "b"), RECENT, ("choose", 0), ("ask", "a"), BACK],
    )
    recent_title = [e for e in session.io.asked if e[0] == "choose" and e[1] == "Recent issues"]
    assert recent_title[0][2] == ["PROJ-3"]
    assert opened == ["PROJ-3"]


def test_recent_with_no_history_says_so(jira):
    session = _run(jira, [RECENT, BACK])
    assert "no issues used in this session yet" in session.io.text
