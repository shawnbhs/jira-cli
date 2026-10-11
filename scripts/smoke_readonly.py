#!/usr/bin/env python3
"""LIVE read-only smoke test for jira-cli.

Calls every READ function in the package exactly once against the real server
and prints **PASS/FAIL plus a count per function**. It never prints issue data,
user data, hostnames, tokens or any response body.

WRITE SAFETY
------------
* No function in the ``WRITES`` set below is ever called.
* ``JIRA_READ_ONLY=1`` is forced into the environment before settings load, so
  even an accidental write path raises ``ReadOnlyError`` instead of mutating.
* Nothing is written to disk: team/filter/plan helpers are skipped because they
  touch the user's config directory.

This script is for AFTER integration - it imports ``jira_cli``, which must be
importable (repo root on ``sys.path`` or the package installed).

Usage (inside WSL, from the repo root):

    JIRA_CLI_ENV_FILE=~/.config/jira-cli/.env scripts/smoke_readonly.py
    scripts/smoke_readonly.py --list      # list the checks, call nothing

Exit code: 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Belt and braces: make the client refuse writes before anything is imported.
os.environ["JIRA_READ_ONLY"] = "1"

# Functions that MUTATE. Listed so the set is auditable and so a reviewer can
# grep for it; this script must never reference any of them.
WRITES = frozenset(
    {
        "create_issue",
        "update_fields",
        "assign",
        "transition",
        "add_comment",
        "link_issues",
        "delete_issue",
        "create_project",
        "create_component",
        "create_version",
        "create_sprint",
        "add_to_sprint",
        "move_to_backlog",
        "ensure_board",
        "add_worklog",
        "set_estimate",
        "save_team",
        "remove_team",
        "save_filter",
        "delete_filter",
        "apply_plan",
        "save_plan",
        "write_report",
    }
)


class Runner:
    def __init__(self, list_only: bool = False) -> None:
        self.list_only = list_only
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    @staticmethod
    def _count(value: object) -> str:
        """A SAFE description of a result: shape only, never content."""
        if value is None:
            return "none"
        if isinstance(value, bool):
            return f"bool={value}"
        if isinstance(value, (int, float)):
            return f"n={value}"
        if isinstance(value, str):
            return f"len={len(value)}"
        if isinstance(value, dict):
            return f"keys={len(value)}"
        if isinstance(value, (list, tuple, set)):
            return f"items={len(value)}"
        return type(value).__name__

    def check(self, name: str, fn, *args, **kwargs) -> object:
        if name in WRITES:  # pragma: no cover - defensive
            raise AssertionError(f"{name} is a WRITE function; refusing to call it")
        if self.list_only:
            print(f"  LIST {name}")
            self.skipped += 1
            return None
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self.failed += 1
            # type name only - an exception message can carry server text
            print(f"  FAIL {name:38s} {type(exc).__name__}")
            if os.environ.get("JIRA_SMOKE_DEBUG"):
                traceback.print_exc()
            return None
        self.passed += 1
        print(f"  PASS {name:38s} {self._count(result)}")
        return result

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"  SKIP {name:38s} {why}")

    def summary(self) -> int:
        print(
            f"\nresult: {self.passed} passed, {self.failed} failed, "
            f"{self.skipped} skipped"
        )
        return 0 if self.failed == 0 else 1


def main(argv: list[str]) -> int:
    list_only = "--list" in argv
    r = Runner(list_only)

    from jira_cli import client as client_mod
    from jira_cli import config as config_mod
    from jira_cli import durations, fields, report
    from jira_cli.services import boards, export, filters, issues, people, projects, worklog
    from jira_cli.services import reports as reports_mod
    from jira_cli.services import sprint_report as sprint_mod

    # ---- pure/local (no network) -------------------------------------
    print("local (no network):")
    settings = r.check("config.load_settings", config_mod.load_settings)
    if settings is None and not list_only:
        print("cannot load settings; aborting")
        return r.summary()
    r.check("config.describe_settings", config_mod.describe_settings, settings)
    r.check("durations.parse_duration", durations.parse_duration, "1w 2d 3h 30m")
    r.check("durations.format_duration", durations.format_duration, 7200)
    r.check("durations.to_jira", durations.to_jira, "90m")
    r.check("durations.resolve_date", durations.resolve_date, "7d")
    r.check(
        "report.make_report",
        report.make_report,
        "Smoke",
        ["a"],
        [{"a": 1}],
    )
    r.check("export.to_markdown", export.to_markdown, report.make_report("S", ["a"], []))
    r.check("export.to_csv", export.to_csv, report.make_report("S", ["a"], []))
    r.check("export.to_json", export.to_json, report.make_report("S", ["a"], []))
    r.check("people.resolve_people", people.resolve_people, "alice,bob", {})
    r.skip("people.load_teams", "reads the user config dir")
    r.skip("filters.list_filters", "reads the user config dir")
    r.skip("filters.get_filter", "reads the user config dir")

    if list_only:
        pass

    # ---- live, read-only ---------------------------------------------
    print("\nlive (GET only, read_only forced on):")
    if list_only:
        c = None
    else:
        c = client_mod.JiraClient(settings)

    me = r.check("client.myself", c.myself) if c else None
    username = (me or {}).get("name") or ""
    default_project = getattr(settings, "default_project", None) or ""

    fmap = None
    if c:
        fmap = r.check("fields.FieldMap", fields.FieldMap, c)
        if fmap is not None:
            r.check("fields.FieldMap.id", fmap.id, "Sprint")

    if c:
        r.check("client.get(serverInfo)", c.get, "/rest/api/2/serverInfo")
        r.check("client.browse_url", c.browse_url, f"{default_project}-1")

    plist = r.check("projects.list_projects", projects.list_projects, c) if c else None
    key = default_project or ((plist or [{}])[0].get("key") if plist else "")

    if c and key:
        r.check("projects.get_project", projects.get_project, c, key)
        r.check("projects.list_issue_types", projects.list_issue_types, c, key)
        r.check("projects.list_statuses", projects.list_statuses, c, key)
    if c:
        r.check("projects.list_priorities", projects.list_priorities, c)

    blist = None
    if c:
        blist = r.check("boards.list_boards", boards.list_boards, c, key or None)
        if key:
            r.check("boards.board_for_project", boards.board_for_project, c, key)

    board_id = None
    if blist:
        board_id = (blist[0] or {}).get("id")
    if c and board_id:
        r.check("boards.get_board", boards.get_board, c, board_id)
        r.check("boards.list_sprints", boards.list_sprints, c, board_id)
        r.check("boards.active_sprint", boards.active_sprint, c, board_id)
        r.check("boards.board_issues", boards.board_issues, c, board_id, None, 3)
    elif not list_only:
        r.skip("boards.get_board", "no board reachable")

    found = None
    if c and key:
        found = r.check(
            "issues.search_issues",
            issues.search_issues,
            c,
            f"project = {key} ORDER BY updated DESC",
            3,
        )
    if c:
        r.check("issues.my_issues", issues.my_issues, c, False, 3)
        r.check("issues.list_link_types", issues.list_link_types, c)

    issue_key = (found or [{}])[0].get("key") if found else None
    if c and issue_key:
        r.check("issues.get_issue", issues.get_issue, c, issue_key)
        r.check("issues.list_comments", issues.list_comments, c, issue_key)
        r.check("issues.list_transitions", issues.list_transitions, c, issue_key)
        r.check("issues.normalize_issue", issues.normalize_issue, {"key": issue_key, "fields": {}}, c, fmap)
        r.check("worklog.issue_worklogs", worklog.issue_worklogs, c, issue_key)
    elif not list_only:
        r.skip("issues.get_issue", "no issue reachable")

    if c:
        r.check("people.search_users", people.search_users, c, "a", 5)
        if username:
            r.check("people.get_user", people.get_user, c, username)
        if key:
            r.check("people.assignable_users", people.assignable_users, c, key, "")

    if c and username:
        r.check(
            "worklog.user_worklogs",
            worklog.user_worklogs,
            c,
            [username],
            "7d",
            None,
            [key] if key else None,
        )
        r.check(
            "worklog.worklog_report",
            worklog.worklog_report,
            c,
            [username],
            "7d",
            None,
            [key] if key else None,
        )

    if c and username:
        users = [username]
        projs = [key] if key else None
        r.check("reports.team_dashboard", reports_mod.team_dashboard, c, users, projs, "7d")
        r.check("reports.person_activity", reports_mod.person_activity, c, username, "7d", None, projs)
        r.check("reports.workload", reports_mod.workload, c, users, projs)
    if c:
        projs = [key] if key else None
        r.check("reports.stale_issues", reports_mod.stale_issues, c, 7, projs, None)
        r.check("reports.overdue_issues", reports_mod.overdue_issues, c, projs, None)
        r.check("reports.incomplete_issues", reports_mod.incomplete_issues, c, projs, None)
        if key:
            r.check("reports.unassigned_issues", reports_mod.unassigned_issues, c, [key])

    if c and board_id:
        r.check("sprint_report.sprint_report", sprint_mod.sprint_report, c, board_id, None)
        r.check("sprint_report.velocity", sprint_mod.velocity, c, board_id, 3)
    if c:
        r.check("filters.server_filters", filters.server_filters, c)

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
