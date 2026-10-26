"""People directory: who is in which project, in which role, and who is admin.

Built for pickers that should not make anyone type a username from memory.
Membership is read from project roles (``services.members.list_roles``); a
group that holds a role is expanded to its members through::

    GET /rest/api/2/group/member?groupname=G&includeInactiveUsers=false

(paginated with ``startAt``/``maxResults``/``isLast``/``values``; Jira Server 8+).

Everything here is read-only. A project or group that cannot be read is
recorded in the report (``errors`` / ``unexpanded_groups``) instead of failing
the whole directory, because one locked-down project must not hide everyone
else.

A person appears only when they hold at least one role in the projects read;
Jira-admin group members with no role in those projects are not listed.
"""

from __future__ import annotations

import difflib
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ..errors import InputError, JiraError, NotFoundError
from ..redact import redact
from . import members
from . import projects as projects_svc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import JiraClient

__all__ = [
    "JIRA_ADMIN_GROUPS",
    "build_directory",
    "build_directory_report",
    "directory_meta",
    "find_people",
    "suggest",
]

#: Membership in either group makes someone a Jira (instance) administrator.
JIRA_ADMIN_GROUPS = ("jira-administrators", "jira-system-administrators")
_ADMIN_ROLE_NAMES = frozenset({"administrators", "admin"})
_GROUP_MEMBER_PATH = "/rest/api/2/group/member"
_GROUP_PAGE_SIZE = 50
_MAX_GROUP_PAGES = 2000
"""Stop walking a group after this many pages; a server that never says
``isLast`` must not keep the directory looping forever."""
MAX_GROUP_MEMBERS = 2000
"""Total group members read across ALL groups of one directory build.  Group
expansion is otherwise unbounded (every group of every project, page by page);
once the budget is spent further members are not fetched and the report says
so in ``truncated``/``notes``."""
MAX_PROJECTS = 500
"""Projects read when ``projects`` is omitted (``None`` = every visible
project).  Keys are read in sorted order; the rest are skipped and reported."""


class _Budget:
    """Thread-safe count of group members still allowed to be fetched."""

    def __init__(self, total: int) -> None:
        self._left = max(0, int(total))
        self._lock = threading.Lock()

    def take(self, wanted: int) -> int:
        """Reserve up to ``wanted`` members; 0 means the budget is spent."""
        with self._lock:
            granted = min(wanted, self._left)
            self._left -= granted
            return granted

    def give_back(self, unused: int) -> None:
        with self._lock:
            self._left += max(0, unused)


def _describe(exc: BaseException) -> str:
    """Error text safe to show: JiraError is already redacted; others are redacted here."""
    if isinstance(exc, JiraError):
        return str(exc)
    return redact(f"{type(exc).__name__}: {exc}")


def _fetch_group_members(
    client: JiraClient, group: str, budget: _Budget | None = None
) -> tuple[list[dict], bool]:
    """Active members of ``group`` as ``{"username", "display_name", "active"}``.

    Returns ``(members, truncated)``; ``truncated`` is true when ``budget`` ran
    out (or the page guard tripped) before Jira said the group was complete.
    """
    budget = budget if budget is not None else _Budget(MAX_GROUP_MEMBERS)
    found: list[dict] = []
    start_at = 0
    for _ in range(_MAX_GROUP_PAGES):
        want = budget.take(_GROUP_PAGE_SIZE)
        if want <= 0:
            return found, True
        params = {
            "groupname": group,
            "includeInactiveUsers": "false",
            "startAt": start_at,
            "maxResults": want,
        }
        page = client.get(_GROUP_MEMBER_PATH, params) or {}
        raw_values = page.get("values") if isinstance(page, dict) else None
        page_slice = raw_values[:want] if isinstance(raw_values, list) else []
        values = [v for v in page_slice if isinstance(v, dict) and v.get("name")]
        budget.give_back(want - len(values))
        for raw in values:
            active = raw.get("active")
            found.append(
                {
                    "username": str(raw["name"]),
                    "display_name": raw.get("displayName"),
                    "active": active if isinstance(active, bool) else None,
                }
            )
        if not page_slice or (isinstance(page, dict) and page.get("isLast", True)):
            return found, False
        start_at += len(page_slice)
    return found, True


class _People:
    """Accumulates people keyed by case-folded username (Jira usernames are case-insensitive)."""

    def __init__(self) -> None:
        self._by_key: dict[str, dict[str, Any]] = {}

    def touch(self, username: str, display_name: Any, active: bool | None) -> dict[str, Any]:
        key = username.casefold()
        person = self._by_key.get(key)
        if person is None:
            person = {"username": username, "display_name": None, "active": None, "roles": {}}
            self._by_key[key] = person
        if display_name and not person["display_name"]:
            person["display_name"] = str(display_name)
        if active is not None and person["active"] is None:
            person["active"] = active
        return person

    def add_role(self, user: dict, project: str, label: str, role_name: str) -> None:
        username = str(user.get("username") or "").strip()
        if not username:
            return
        person = self.touch(username, user.get("display_name"), user.get("active"))
        person["roles"].setdefault(project, set()).add((label, role_name))

    def finish(self, admins: set[str], non_admin_value: bool | None) -> list[dict]:
        people = []
        for key, raw in self._by_key.items():
            roles: dict[str, set[tuple[str, str]]] = raw["roles"]
            people.append(
                {
                    "username": raw["username"],
                    "display_name": raw["display_name"] or raw["username"],
                    "active": raw["active"],
                    "projects": [
                        {"key": proj, "roles": sorted({label for label, _ in roles[proj]})}
                        for proj in sorted(roles)
                    ],
                    "project_admin": sorted(
                        proj
                        for proj, held in roles.items()
                        if any(name.casefold() in _ADMIN_ROLE_NAMES for _, name in held)
                    ),
                    "jira_admin": True if key in admins else non_admin_value,
                }
            )
        people.sort(key=lambda p: (p["display_name"].casefold(), p["username"].casefold()))
        return people


def _resolve_keys(client: JiraClient, requested: list[str] | None) -> tuple[list[str], list[dict]]:
    """Validated project keys plus errors for server-listed keys that fail validation.

    Raises:
        InputError: when a caller-supplied key is malformed (a typo should be loud).
    """
    if requested is not None:
        return sorted({projects_svc.validate_project_key(k) for k in requested}), []
    keys: set[str] = set()
    errors: list[dict] = []
    for project in projects_svc.list_projects(client):
        raw_key = project.get("key")
        try:
            keys.add(projects_svc.validate_project_key(raw_key))
        except JiraError as exc:
            errors.append({"project": str(raw_key), "error": _describe(exc)})
    return sorted(keys), errors


def _admin_lookup(expanded: dict[str, list[dict] | None], missing: set[str]) -> tuple[set[str], bool | None]:
    """Case-folded admin usernames, and the ``jira_admin`` value for everyone else.

    A group that answered 404 does not exist, so nobody is in it. A group that
    could not be read (403, error) makes "not an admin" unknowable -> ``None``.
    """
    admins: set[str] = set()
    all_known = True
    for group in JIRA_ADMIN_GROUPS:
        listed = expanded.get(group)
        if listed is None:
            all_known = all_known and group in missing
            continue
        admins.update(user["username"].casefold() for user in listed)
    return admins, (False if all_known else None)


def build_directory_report(
    client: JiraClient,
    projects: list[str] | None = None,
    *,
    expand_groups: bool = True,
    max_workers: int = 6,
) -> dict:
    """Read every role of every requested project and fold it into one people list.

    Args:
        client: Configured Jira client (only GET requests are made).
        projects: Project keys to read; ``None`` reads every visible project,
            up to :data:`MAX_PROJECTS` (the rest are skipped and noted).
        expand_groups: Expand role groups into their members.
        max_workers: Concurrent project/group reads.

    Returns:
        ``{"people": [...], "projects": [keys read], "unexpanded_groups":
        [{"group", "reason"}], "errors": [{"project", "error"}], "truncated":
        bool, "notes": [str]}``; ``truncated`` is true when the project cap or
        the :data:`MAX_GROUP_MEMBERS` budget cut the directory short. Each person is
        ``{"username", "display_name", "active", "projects": [{"key", "roles"}],
        "project_admin": [keys], "jira_admin": bool | None}``.

    Raises:
        InputError: a key in ``projects`` is malformed, or more than
            :data:`MAX_PROJECTS` keys were passed.
    """
    if projects is not None and len(projects) > MAX_PROJECTS:
        raise InputError(f"at most {MAX_PROJECTS} projects can be read at once, got {len(projects)}")
    keys, errors = _resolve_keys(client, projects)
    workers = max(1, int(max_workers))
    notes: list[str] = []
    if projects is None and len(keys) > MAX_PROJECTS:
        notes.append(
            f"truncated: {len(keys)} projects are visible; only the first "
            f"{MAX_PROJECTS} (by key) were read. Pass project keys to read others."
        )
        keys = keys[:MAX_PROJECTS]
    budget = _Budget(MAX_GROUP_MEMBERS)

    def read_project(key: str) -> tuple[str, list[dict] | None, str | None]:
        try:
            return key, members.list_roles(client, key), None
        except Exception as exc:  # noqa: BLE001 - one bad project must not hide the rest
            return key, None, _describe(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(read_project, keys))

    roles_by_project: dict[str, list[dict]] = {}
    for key, roles, error in results:
        if error is not None:
            errors.append({"project": key, "error": error})
        else:
            roles_by_project[key] = roles or []

    role_groups = sorted(
        {g for roles in roles_by_project.values() for r in roles for g in r.get("groups") or []}
    )
    wanted = list(dict.fromkeys((role_groups if expand_groups else []) + list(JIRA_ADMIN_GROUPS)))

    cut_groups: list[str] = []

    def read_group(group: str) -> tuple[str, list[dict] | None, BaseException | None]:
        try:
            listed, cut = _fetch_group_members(client, group, budget)
        except Exception as exc:  # noqa: BLE001 - an unreadable group is reported, not fatal
            return group, None, exc
        if cut:
            cut_groups.append(group)
        return group, listed, None

    # Admin groups first so the shared member budget is spent on them before
    # role groups; a truncated admin group makes "not an admin" unknowable.
    ordered = list(JIRA_ADMIN_GROUPS) + [g for g in wanted if g not in JIRA_ADMIN_GROUPS]
    group_results = [read_group(g) for g in ordered[: len(JIRA_ADMIN_GROUPS)]]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        group_results += list(pool.map(read_group, ordered[len(JIRA_ADMIN_GROUPS) :]))
    if cut_groups:
        notes.append(
            f"truncated: group expansion stopped at {MAX_GROUP_MEMBERS} members in "
            f"total; incomplete groups: {', '.join(sorted(cut_groups, key=str.casefold))}"
        )

    expanded: dict[str, list[dict] | None] = {}
    missing: set[str] = set()
    unexpanded: list[dict] = []
    for group, listed, exc in group_results:
        expanded[group] = listed
        if exc is None:
            continue
        if isinstance(exc, NotFoundError):
            missing.add(group)
        if group in role_groups:
            unexpanded.append({"group": group, "reason": _describe(exc)})

    people = _People()
    for key, roles in roles_by_project.items():
        for role in roles:
            name = str(role.get("name") or "")
            for user in role.get("users") or []:
                people.add_role(user, key, name, name)
            if not expand_groups:
                continue
            for group in role.get("groups") or []:
                for user in expanded.get(group) or []:
                    people.add_role(user, key, f"{name} (via group {group})", name)

    errors.sort(key=lambda e: e["project"])
    unexpanded.sort(key=lambda g: g["group"].casefold())
    admins, non_admin_value = _admin_lookup(expanded, missing)
    if any(group in cut_groups for group in JIRA_ADMIN_GROUPS):
        non_admin_value = None
    return {
        "people": people.finish(admins, non_admin_value),
        "projects": sorted(roles_by_project),
        "unexpanded_groups": unexpanded,
        "errors": errors,
        "truncated": bool(notes),
        "notes": notes,
    }


def build_directory(
    client: JiraClient,
    projects: list[str] | None = None,
    *,
    expand_groups: bool = True,
    max_workers: int = 6,
) -> list[dict]:
    """The ``people`` list of :func:`build_directory_report`."""
    return build_directory_report(client, projects, expand_groups=expand_groups, max_workers=max_workers)[
        "people"
    ]


def directory_meta(people: list[dict]) -> dict:
    """Counts derivable from a people list alone.

    ``unexpanded_groups`` is always empty here: that fact lives only in the
    report from :func:`build_directory_report`.
    """
    keys = {p_entry["key"] for person in people for p_entry in person.get("projects") or []}
    return {"people": len(people), "projects": sorted(keys), "unexpanded_groups": []}


def _names_of(person: dict) -> tuple[str, str]:
    return str(person.get("username") or ""), str(person.get("display_name") or "")


def find_people(people: list[dict], text: str) -> list[dict]:
    """People whose username or display name contains ``text`` (case-insensitive)."""
    needle = (text or "").strip().casefold()
    return [p for p in people if any(needle in n.casefold() for n in _names_of(p))]


def _words(name: str) -> list[str]:
    for sep in "._-@+":
        name = name.replace(sep, " ")
    return [w for w in name.casefold().split() if w]


def suggest(people: list[dict], token: str, limit: int = 3) -> list[dict]:
    """Best "did you mean" candidates for an unknown ``token``.

    Substring matches come first (exact, then prefix, then anywhere), then
    fuzzy matches on the whole username / display name and on their words.
    """
    needle = (token or "").strip().casefold()
    if not needle or limit <= 0:
        return []

    def substring_rank(person: dict) -> int | None:
        names = [n.casefold() for n in _names_of(person)]
        words = [w for n in names for w in _words(n)]
        if needle in names or needle in words:
            return 0
        if any(n.startswith(needle) for n in names + words):
            return 1
        if any(needle in n for n in names):
            return 2
        return None

    ranked = sorted(((rank, i) for i, p in enumerate(people) if (rank := substring_rank(p)) is not None))
    chosen = [i for _, i in ranked]

    candidates: dict[str, list[int]] = {}
    for i, person in enumerate(people):
        for name in _names_of(person):
            for cand in [name.casefold(), *_words(name)]:
                candidates.setdefault(cand, []).append(i)
    for close in difflib.get_close_matches(needle, list(candidates), n=limit * 4, cutoff=0.6):
        for i in candidates[close]:
            if i not in chosen:
                chosen.append(i)
    return [people[i] for i in chosen[:limit]]
