#!/usr/bin/env python3
"""Capture SANITIZED response shapes from a live Jira Server.

Read-only: every request is an HTTP GET. Nothing is written to the server and
nothing is written outside ``tests/fixtures/``.

The captured JSON is sanitized BEFORE it is written to disk:

* usernames / keys / display names / emails -> ``alice``, ``bob``, ``carol`` ...
  (consistent across every fixture, driven by one shared map)
* project keys -> ``PROJ``, ``PRJ2``, ``PRJ3`` ... (issue keys follow)
* hostnames and absolute URLs -> ``https://jira.example.com``
* summaries / descriptions / comments / worklog comments / goals -> ``Sample text N``
* board and sprint names -> ``Sample name N``
* project categories, components and versions -> ``Category N`` /
  ``Component N`` / ``Version N``
* changelog items: ``fromString`` / ``toString`` of user fields become the fake
  display name and ``from`` / ``to`` a synthetic ``JIRAUSER100nn`` key; free-text
  fields become ``Sample text N``; every other label is scrubbed like any string
* ``timeZone`` -> ``UTC``, ``locale`` -> ``en`` and every ISO timestamp offset
  -> ``+0000`` / ``+00:00`` (an offset geolocates the organisation)
* org-defined custom field names and project role names -> neutral
  ``Custom ...`` names (Atlassian-shipped names are kept)
* user names inside GreenHopper's HTML user-hover snippets -> fake display names
* avatar URLs dropped entirely
* any string containing a term from ``JIRA_CAPTURE_FORBIDDEN`` (comma-separated,
  case-insensitive; e.g. your company, client and product names) -> the term
  is replaced, and a whole-value match becomes ``Sample text N``
* ANY remaining string containing a non-ASCII character (e.g. Persian /
  Arabic script) is replaced with a ``Sample text N`` placeholder
* numeric ids are preserved on purpose - they carry the shape, not the secret

Usage (from the repo root, inside WSL):

    JIRA_CAPTURE_FORBIDDEN='acme,acme-bank' scripts/capture_shapes.py
    scripts/capture_shapes.py --scan     # re-run the leak scan only

Credentials come from ``$JIRA_CAPTURE_CONFIG``, else ``$JIRA_CLI_ENV_FILE``,
else ``~/.jira-cli/config.env``. Values are never printed. The scan counts must
all be 0 before any fixture is committed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError as exc:  # pragma: no cover
    sys.stderr.write("requests is required\n")
    raise SystemExit(2) from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _config_path() -> Path:
    """Credential file: explicit override, then the CLI's own env file, then home."""
    for var in ("JIRA_CAPTURE_CONFIG", "JIRA_CLI_ENV_FILE"):
        value = os.environ.get(var, "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / ".jira-cli" / "config.env"


def forbidden_terms(var: str = "JIRA_CAPTURE_FORBIDDEN") -> list[str]:
    """Comma-separated, case-insensitive terms that must never reach a fixture."""
    raw = os.environ.get(var, "")
    return sorted({t.strip() for t in raw.split(",") if t.strip()}, key=len, reverse=True)


CONFIG_PATH = _config_path()

FAKE_HOST = "jira.example.com"
FAKE_BASE = f"https://{FAKE_HOST}"
TIMEOUT = 20
MAX_REQUESTS = 55

ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,14})-(\d+)\b")
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,14}$")

# dict keys whose string value is free text
TEXT_KEYS = {
    "summary",
    "description",
    "body",
    "comment",
    "environment",
    "renderedBody",
    "goal",
    "statusEntryText",
    "text",
}
# dict keys whose string value identifies a person
USER_STR_KEYS = {
    "assignee",
    "assigneeKey",
    "assigneeName",
    "author",
    "authorKey",
    "authorName",
    "creator",
    "lead",
    "leadUserName",
    "owner",
    "ownerUserName",
    "reporter",
    "reporterKey",
    "reporterName",
    "updateAuthor",
    "user",
    "username",
    "userName",
    "displayName",
    "emailAddress",
}
DISPLAY_SUFFIXES = ("Name", "displayName")
# dict keys dropped outright
DROP_KEYS = {
    "avatarUrls",
    "avatarUrl",
    "avatarId",
    "avatar",
    "thumbnail",
    "priorityUrl",
    "typeUrl",
    "statusUrl",
    "iconUrl",
    "projectAvatarUrl",
}

#: ISO-8601 time followed by a UTC offset (``+0330`` or ``+03:30``).
OFFSET_RE = re.compile(r"(T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)[+-]\d{2}(:?)\d{2}\b")
#: GreenHopper renders users as ``<a class="user-hover" rel="USERNAME">Display Name</a>``.
HOVER_RE = re.compile(r'(<a\b[^>]*\brel="([^"]*)"[^>]*>)([^<]*?)((?: \(Inactive\))?</a>)')
#: Changelog ``field`` values whose from/to identify a person.
USER_FIELDS = {"assignee", "reporter", "creator", "lead", "user", "Approvers", "CAB"}
#: Changelog ``field`` values whose from/to are free text.
TEXT_FIELDS = {"summary", "description", "environment", "Comment", "comment", "labels"}
#: ``self`` URL fragment -> placeholder prefix for named entities.
NAMED_ENTITIES = {
    "/projectCategory/": "Category",
    "/component/": "Component",
    "/version/": "Version",
}
#: Project roles every Jira Server/Service Desk install ships with.
STANDARD_ROLES = {
    "Administrators",
    "Developers",
    "Users",
    "viewer",
    "ADMIN",
    "USER",
    "Service Desk Team",
    "Service Desk Customers",
    "atlassian-addons-project-access",
    "Tempo Project Managers",
}
#: Custom-field type suffix -> neutral label for org-defined custom fields.
CUSTOM_KIND = {
    "userpicker": "user",
    "multiuserpicker": "users",
    "textarea": "text",
    "textfield": "text",
    "datepicker": "date",
    "datetime": "datetime",
    "float": "number",
    "multicheckboxes": "checkbox",
    "select": "select",
    "multiselect": "multiselect",
    "cascadingselect": "cascading select",
    "url": "url",
    "labels": "labels",
}
#: Custom field names Atlassian ships (Software, Service Management, Advanced
#: Roadmaps, Gantt); kept because code looks some of them up by name.
STANDARD_CUSTOM_FIELDS = {
    "Story Points", "Team", "Epic Name", "Epic Status", "Epic Colour", "Epic Link",
    "Sprint", "Rank", "Flagged", "Development", "Parent Link", "Target start",
    "Target end", "Original story points", "Baseline start date", "Baseline end date",
    "Task mode", "Task progress", "Risk consequence", "Risk probability", "Start date",
    "Time to resolution", "Time to first response", "Time to close after resolution",
    "Time to approve normal change", "CAB", "Impact", "Request participants", "Urgency",
    "Customer Request Type", "Operational categorization", "Organizations", "Source",
    "Pending reason", "Satisfaction", "Investigation reason", "Satisfaction date",
    "Groups", "Root cause", "Workaround", "Change type", "Linked major incidents",
    "Approvals", "Change managers", "Product categorization", "Change risk",
    "Change reason", "Approvers", "Change start date", "Change completion date",
    "Structure Index Monitor",
}

USER_POOL = [
    "alice",
    "bob",
    "carol",
    "dave",
    "erin",
    "frank",
    "grace",
    "heidi",
    "ivan",
    "judy",
    "ken",
    "lena",
    "mallory",
    "niaj",
    "olivia",
    "peggy",
    "rupert",
    "sybil",
    "trent",
    "victor",
]


# ---------------------------------------------------------------- config


def load_env(path: Path) -> dict[str, str]:
    """Tiny KEY=VALUE parser. Values are returned but never printed."""
    out: dict[str, str] = {}
    if not path.exists():
        raise SystemExit(f"config file not found: {path}")
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


# ---------------------------------------------------------------- sanitizer


class Sanitizer:
    """Replaces every identifying value with a stable fake."""

    def __init__(self, real_host: str, real_user: str, real_email: str,
                 forbidden: list[str] | None = None) -> None:
        self.real_host = real_host.lower()
        self.real_user = real_user
        self.real_email = real_email
        self.forbidden = forbidden if forbidden is not None else forbidden_terms()
        self._users: dict[str, str] = {}
        self._projects: dict[str, str] = {}
        self._texts: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._entities: dict[tuple[str, str], str] = {}
        self._fields: dict[str, str] = {}
        self._roles: dict[str, str] = {}
        if real_user:
            self._users[real_user.lower()] = "alice"
        if real_email:
            self._users[real_email.lower()] = "alice"

    # -- maps -------------------------------------------------------

    def user(self, original: Any) -> str:
        if not isinstance(original, str) or not original.strip():
            return "alice"
        key = original.strip().lower()
        if key not in self._users:
            idx = len({v for v in self._users.values()})
            self._users[key] = (
                USER_POOL[idx] if idx < len(USER_POOL) else f"user{idx + 1}"
            )
        return self._users[key]

    def alias_user(self, fake: str, *identifiers: Any) -> None:
        """Bind every identifier of one person (name, key, email, display) to ``fake``."""
        for ident in identifiers:
            if isinstance(ident, str) and ident.strip():
                self._users.setdefault(ident.strip().lower(), fake)

    def user_key(self, original: Any) -> str:
        """Synthetic Server user key (``JIRAUSER100nn``) for a changelog id."""
        fake = self.user(original)
        idx = USER_POOL.index(fake) if fake in USER_POOL else len(USER_POOL)
        return f"JIRAUSER{10000 + idx}"

    def entity(self, prefix: str, original: Any) -> str:
        """``Category N`` / ``Component N`` / ``Version N`` for an org-chosen name."""
        key = (prefix, str(original))
        if key not in self._entities:
            n = sum(1 for p, _ in self._entities if p == prefix) + 1
            self._entities[key] = f"{prefix} {n}"
        return self._entities[key]

    def custom_field(self, field_id: str, name: str, schema: dict) -> str:
        """Neutral name for an org-defined custom field; Atlassian names are kept."""
        if name in STANDARD_CUSTOM_FIELDS:
            return name
        if field_id not in self._fields:
            kind = CUSTOM_KIND.get(str(schema.get("custom", "")).rsplit(":", 1)[-1], "value")
            self._fields[field_id] = f"Custom {kind} field {len(self._fields) + 1}"
        return self._fields[field_id]

    def role(self, name: str) -> str:
        """Neutral name for an org-defined project role; shipped roles are kept."""
        if name in STANDARD_ROLES:
            return name
        if name not in self._roles:
            self._roles[name] = f"Custom Role {chr(ord('A') + len(self._roles) % 26)}"
        return self._roles[name]

    def project(self, original: Any) -> str:
        if not isinstance(original, str) or not original.strip():
            return "PROJ"
        key = original.strip().upper()
        if key not in self._projects:
            n = len(self._projects)
            self._projects[key] = "PROJ" if n == 0 else f"PRJ{n + 1}"
        return self._projects[key]

    def text(self, original: Any) -> str:
        if original is None:
            return None  # type: ignore[return-value]
        key = str(original)
        if key not in self._texts:
            self._texts[key] = f"Sample text {len(self._texts) + 1}"
        return self._texts[key]

    def name(self, original: Any) -> str:
        key = str(original)
        if key not in self._names:
            self._names[key] = f"Sample name {len(self._names) + 1}"
        return self._names[key]

    # -- scalars ----------------------------------------------------

    def _host(self, s: str) -> str:
        if self.real_host and self.real_host in s.lower():
            pattern = re.compile(
                r"https?://" + re.escape(self.real_host) + r"(:\d+)?", re.I
            )
            s = pattern.sub(FAKE_BASE, s)
            s = re.sub(re.escape(self.real_host), FAKE_HOST, s, flags=re.I)
        return s

    def _sprint_string(self, s: str) -> str:
        """Jira Server returns the Sprint custom field as a Java toString blob.

        Field values are unquoted and may themselves contain commas, so each
        free-text field is replaced from its ``key=`` up to the *next known key*
        rather than up to the next comma.
        """
        known = (
            "id",
            "rapidViewId",
            "state",
            "name",
            "startDate",
            "endDate",
            "completeDate",
            "activatedDate",
            "sequence",
            "goal",
            "autoStartStop",
            "synced",
            "boardName",
            "incompleteIssuesDestinationId",
        )
        nxt = r"(?=,(?:" + "|".join(known) + r")=|\]$)"
        tok = r"(?:(?!,(?:" + "|".join(known) + r")=)(?!\]$).)*"
        for field, repl in (
            ("name", "Sample Sprint"),
            ("goal", "Sample text"),
            ("boardName", "Sample Board"),
        ):
            s = re.sub(
                r"(?<=\b" + field + r"=)" + tok + nxt, repl, s, flags=re.S
            )
        return s

    def _people(self, s: str) -> str:
        for original, fake in sorted(
            self._users.items(), key=lambda kv: -len(kv[0])
        ):
            if len(original) < 3:
                continue
            s = re.sub(re.escape(original), fake, s, flags=re.I)
        s = EMAIL_RE.sub(lambda m: f"{self.user(m.group(0))}@example.com", s)
        return s

    def _keys(self, s: str) -> str:
        s = ISSUE_KEY_RE.sub(lambda m: f"{self.project(m.group(1))}-{m.group(2)}", s)
        # bare project keys already discovered (e.g. inside a JQL string)
        for original, fake in sorted(
            self._projects.items(), key=lambda kv: -len(kv[0])
        ):
            s = re.sub(r"\b" + re.escape(original) + r"\b", fake, s)
        return s

    def _hover(self, s: str) -> str:
        """Replace the display name inside GreenHopper's user-hover anchors."""
        def repl(m: re.Match[str]) -> str:
            fake = self.user(m.group(2))
            anchor = m.group(1).replace(m.group(2), fake)
            return anchor + fake.capitalize() + m.group(4)

        return HOVER_RE.sub(repl, s)

    def _forbidden(self, s: str) -> str:
        """Scrub caller-supplied forbidden terms (company, client, product names)."""
        for term in self.forbidden:
            if term.lower() == s.strip().lower():
                return self.text(s)
            s = re.sub(re.escape(term), "Example", s, flags=re.I)
        return s

    def string(self, s: str, key: str | None = None) -> str:
        if key == "timeZone":
            return "UTC"
        if key == "locale":
            return "en"
        if "com.atlassian.greenhopper.service.sprint.Sprint@" in s:
            s = self._sprint_string(s)
        s = self._host(s)
        s = OFFSET_RE.sub(lambda m: m.group(1) + "+00" + m.group(2) + "00", s)
        if "user-hover" in s:
            s = self._hover(s)
        if key == "key" and PROJECT_KEY_RE.match(s):
            return self.project(s)
        if key in ("projectKey", "projectKeyOrId") and PROJECT_KEY_RE.match(s):
            return self.project(s)
        s = self._keys(s)
        s = self._people(s)
        s = self._forbidden(s)
        if not s.isascii():
            return self.text(s)
        return s

    # -- containers -------------------------------------------------

    @staticmethod
    def _is_user(d: dict) -> bool:
        return "displayName" in d or "emailAddress" in d

    @staticmethod
    def _entity_prefix(d: dict) -> str | None:
        self_url = d.get("self")
        if not isinstance(self_url, str) or "name" not in d:
            return None
        for fragment, prefix in NAMED_ENTITIES.items():
            if fragment in self_url:
                return prefix
        return None

    @staticmethod
    def _is_custom_field(d: dict) -> bool:
        schema = d.get("schema")
        return (
            isinstance(schema, dict)
            and bool(schema.get("custom"))
            and isinstance(d.get("name"), str)
            and ("clauseNames" in d or "fieldId" in d)
        )

    @staticmethod
    def _is_changelog_item(d: dict) -> bool:
        return "field" in d and ("toString" in d or "fromString" in d)

    def _entity_dict(self, d: dict, prefix: str) -> dict:
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in DROP_KEYS:
                continue
            if k == "name" and isinstance(v, str):
                out[k] = self.entity(prefix, v)
            elif k == "description" and isinstance(v, str):
                out[k] = self.text(v)
            else:
                out[k] = self.walk(v, k)
        return out

    def _custom_field_dict(self, d: dict) -> dict:
        old = d["name"]
        new = self.custom_field(str(d.get("id") or d.get("fieldId")), old, d["schema"])
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k == "name":
                out[k] = new
            elif k == "clauseNames" and isinstance(v, list):
                out[k] = [new if c == old else self.walk(c, k) for c in v]
            else:
                out[k] = self.walk(v, k)
        return out

    def _changelog_item(self, d: dict) -> dict:
        field = d.get("field")
        if field in USER_FIELDS:
            # one person per side: bind the internal key and the label together
            for ident, label in (("from", "fromString"), ("to", "toString")):
                if d.get(ident) or d.get(label):
                    fake = self.user(d.get(ident) or d.get(label))
                    self.alias_user(fake, d.get(ident), d.get(label))
        out: dict[str, Any] = {}
        for k, v in d.items():
            if not isinstance(v, str):
                out[k] = self.walk(v, k)
            elif field in USER_FIELDS and k in ("from", "to"):
                out[k] = self.user_key(v)
            elif field in USER_FIELDS and k in ("fromString", "toString"):
                out[k] = self.user(v).capitalize()
            elif field in TEXT_FIELDS and k in ("fromString", "toString"):
                out[k] = self.text(v)
            else:
                out[k] = self.string(v, k)
        return out

    @staticmethod
    def _is_project(d: dict) -> bool:
        self_url = d.get("self")
        if isinstance(self_url, str) and "/project/" in self_url:
            return True
        return "projectTypeKey" in d and "key" in d

    @staticmethod
    def _is_board_or_sprint(d: dict) -> bool:
        self_url = d.get("self")
        if isinstance(self_url, str) and (
            "/board/" in self_url or "/sprint/" in self_url
        ):
            return True
        if any(k in d for k in ("originBoardId", "rapidViewId", "stateKey", "boardName")):
            return True
        return "state" in d and "startDate" in d

    @staticmethod
    def _is_filter(d: dict) -> bool:
        self_url = d.get("self")
        if isinstance(self_url, str) and "/filter/" in self_url:
            return True
        return "jql" in d and "name" in d

    def _filter_dict(self, d: dict) -> dict:
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in DROP_KEYS:
                continue
            if k in ("name", "description") and isinstance(v, str):
                out[k] = self.text(v)
            else:
                out[k] = self.walk(v, k)
        return out

    def _user_dict(self, d: dict) -> dict:
        ident = (
            d.get("name")
            or d.get("key")
            or d.get("emailAddress")
            or d.get("displayName")
        )
        fake = self.user(ident)
        self.alias_user(
            fake, d.get("name"), d.get("key"), d.get("emailAddress"), d.get("displayName")
        )
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in DROP_KEYS:
                continue
            if k in ("name", "key") and isinstance(v, str):
                out[k] = fake
            elif k == "displayName":
                out[k] = fake.capitalize()
            elif k == "emailAddress":
                out[k] = f"{fake}@example.com"
            elif k == "self" and isinstance(v, str):
                out[k] = re.sub(
                    r"(username|key)=[^&]*", r"\1=" + fake, self._host(v)
                )
            else:
                out[k] = self.walk(v, k)
        return out

    def _project_dict(self, d: dict) -> dict:
        fake = self.project(d.get("key") or d.get("projectKey") or "")
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in DROP_KEYS:
                continue
            if k in ("key", "projectKey") and isinstance(v, str):
                out[k] = fake
            elif k == "name" and isinstance(v, str):
                out[k] = f"Project {fake}"
            else:
                out[k] = self.walk(v, k)
        return out

    def _agile_dict(self, d: dict) -> dict:
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in DROP_KEYS:
                continue
            if k in ("name", "boardName") and isinstance(v, str):
                out[k] = self.name(v)
            elif k == "goal" and isinstance(v, str):
                out[k] = self.text(v)
            else:
                out[k] = self.walk(v, k)
        return out

    def walk(self, obj: Any, key: str | None = None) -> Any:
        if isinstance(obj, dict):
            if self._is_user(obj):
                return self._user_dict(obj)
            prefix = self._entity_prefix(obj)
            if prefix:
                return self._entity_dict(obj, prefix)
            if self._is_custom_field(obj):
                return self._custom_field_dict(obj)
            if self._is_changelog_item(obj):
                return self._changelog_item(obj)
            if self._is_project(obj):
                return self._project_dict(obj)
            if self._is_board_or_sprint(obj):
                return self._agile_dict(obj)
            if self._is_filter(obj):
                return self._filter_dict(obj)
            if key == "roles":
                return {self.role(str(k)): self.walk(v, k) for k, v in obj.items()}
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if k in DROP_KEYS:
                    continue
                kk = self.string(k, None) if not str(k).isascii() else k
                if k in TEXT_KEYS and isinstance(v, str):
                    out[kk] = self.text(v)
                elif k in USER_STR_KEYS and isinstance(v, str):
                    fake = self.user(v)
                    if k == "emailAddress":
                        out[kk] = f"{fake}@example.com"
                    elif any(k.endswith(s) for s in DISPLAY_SUFFIXES):
                        out[kk] = fake.capitalize()
                    else:
                        out[kk] = fake
                else:
                    out[kk] = self.walk(v, k)
            return out
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            return self.string(obj, key)
        return obj


# ---------------------------------------------------------------- shapes


def shape(obj: Any, prefix: str = "", depth: int = 0, out: dict | None = None) -> dict:
    """Collect ``path -> type`` for the first sample of every branch."""
    if out is None:
        out = {}
    if depth > 5:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            out[path] = type(v).__name__
            shape(v, path, depth + 1, out)
    elif isinstance(obj, list):
        out[prefix or "<root>"] = f"list[{len(obj)}]"
        if obj:
            shape(obj[0], f"{prefix}[]", depth + 1, out)
    return out


# ---------------------------------------------------------------- capture


class Capture:
    def __init__(self, base: str, user: str, secret: str, san: Sanitizer) -> None:
        self.base = base.rstrip("/")
        self.san = san
        self.session = requests.Session()
        self.session.auth = (user, secret)
        self.session.headers.update({"Accept": "application/json"})
        self.count = 0
        self.meta: list[dict] = []
        self.raw: dict[str, Any] = {}

    def get(self, label: str, path: str, params: dict | None = None) -> Any:
        if self.count >= MAX_REQUESTS:
            raise SystemExit("request budget exhausted")
        self.count += 1
        url = self.base + path
        status: Any = None
        err = ""
        data: Any = None
        try:
            r = self.session.get(url, params=params, timeout=TIMEOUT)
            status = r.status_code
            if r.status_code < 400:
                try:
                    data = r.json()
                except ValueError:
                    err = "non-json body"
            else:
                err = f"http {r.status_code}"
        except requests.RequestException as exc:
            err = type(exc).__name__
        self.meta.append(
            {"label": label, "path": path, "status": status, "error": err}
        )
        print(f"  {label:34s} status={status} {err}")
        if data is not None:
            self.raw[label] = data
        return data

    def write(self, label: str, data: Any) -> Path:
        clean = self.san.walk(data)
        p = FIXTURES / f"{label}.json"
        p.write_text(
            json.dumps(clean, indent=2, ensure_ascii=True, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return p


# ---------------------------------------------------------------- scan


SURPRISES_MD = [
    "## SURPRISES (read before writing a parser)",
    "",
    "### 1. The Sprint custom field is a Java `toString()` blob, not an object",
    "",
    "On Jira **Server** the Sprint field (`customfield_10109` here) comes back as a",
    "`list[str]`, each element being a serialized Java object:",
    "",
    "```",
    "com.atlassian.greenhopper.service.sprint.Sprint@1a2b3c[id=123,rapidViewId=45,",
    "state=ACTIVE,name=Sample Sprint,startDate=2026-01-01T00:00:00.000Z,",
    "endDate=...,completeDate=<null>,activatedDate=...,sequence=123,goal=Sample text,",
    "autoStartStop=false,synced=false]",
    "```",
    "",
    "Implications:",
    "",
    "* It is a **list**, so an issue can carry several sprints; the *last* element is",
    "  the current one.",
    "* `completeDate=<null>` - the literal string `<null>`, not JSON `null`.",
    "* Parse with a regex over `key=value` pairs up to the next `,` or `]`. Values are",
    "  NOT quoted and may themselves contain `,` in free text (the name/goal), so a",
    "  naive `split(\",\")` corrupts names. Prefer named-key regexes",
    "  (`id=(\\d+)`, `state=([A-Z]+)`, `name=(.*?),startDate=`).",
    "* On Jira **Cloud** the same field is `list[dict]`. Code must handle both.",
    "* `/rest/agile/1.0/sprint/{id}` and the board sprint list DO return proper JSON",
    "  objects - prefer those whenever a sprint id is already known.",
    "",
    "### 2. Assignee / reporter keys are Server-shaped, not Cloud-shaped",
    "",
    "A user object here carries:",
    "",
    "```",
    "self, name, key, emailAddress, avatarUrls, displayName, active, timeZone, locale?",
    "```",
    "",
    "* `name` is the **username** - this is what `PUT /issue/{key}/assignee` wants as",
    "  `{\"name\": \"<username>\"}`. There is **no `accountId`** (Cloud only).",
    "* `key` is a separate, stable internal user key and is often *equal* to `name` on",
    "  older installs but must not be assumed equal.",
    "* `displayName` may be non-ASCII on the real server; never use it as an identifier.",
    "* Unassigned issues have `fields.assignee = null` (key present, value null).",
    "",
    "### 3. `statusCategory` keys are the stable signal, `name` is not",
    "",
    "`fields.status.statusCategory` is:",
    "",
    "```",
    "{self, id, key, colorName, name}",
    "```",
    "",
    "* `key` is one of exactly `new` | `indeterminate` | `done` (stable, API-level).",
    "* `name` is the localised label (`To Do` | `In Progress` | `Done`) and will change",
    "  with the instance language. **Branch on `key`, map to `name` only for display.**",
    "* `id` is 2 (new), 4 (indeterminate), 3 (done) on this server.",
    "* `colorName` is e.g. `blue-gray`, `yellow`, `green` - presentation only.",
    "",
    "### 4. `timetracking` keys come in pairs and may be entirely absent",
    "",
    "`fields.timetracking` is `{}` (empty dict) when nothing is tracked. When populated:",
    "",
    "```",
    "originalEstimate: str        e.g. \"1d 4h\"      (human)",
    "remainingEstimate: str       e.g. \"3h\"",
    "timeSpent: str               e.g. \"5h 30m\"",
    "originalEstimateSeconds: int",
    "remainingEstimateSeconds: int",
    "timeSpentSeconds: int",
    "```",
    "",
    "* The `*Seconds` variants are the ones to compute with; the string forms are",
    "  rendered with the instance's own hours-per-day / days-per-week settings.",
    "* The SAME numbers are ALSO exposed as sibling top-level fields",
    "  `fields.timeoriginalestimate`, `fields.timeestimate`, `fields.timespent`",
    "  (all `int|None`, seconds). They can disagree with `timetracking` when a field",
    "  configuration hides one of them - prefer `timetracking` and fall back.",
    "* `aggregatetimeoriginalestimate` / `aggregatetimeestimate` /",
    "  `aggregatetimespent` include sub-tasks.",
    "* Any of these keys is simply MISSING (not null) when `fields=` restricted the",
    "  response - always `.get()`.",
    "",
    "### 5. GreenHopper layout is flat, undocumented and not REST",
    "",
    "`/rest/greenhopper/1.0/rapid/charts/sprintreport?rapidViewId=&sprintId=`:",
    "",
    "```",
    "contents.completedIssues: list[dict]            # NOT normal issue objects",
    "contents.issuesNotCompletedInCurrentSprint: list[dict]",
    "contents.puntedIssues: list[dict]               # removed from the sprint",
    "contents.issuesCompletedInAnotherSprint: list[dict]",
    "contents.completedIssuesEstimateSum: {text: str, value: float|None}",
    "contents.incompletedIssuesEstimateSum: {text, value}",
    "contents.allIssuesEstimateSum: {text, value}",
    "contents.puntedIssuesEstimateSum: {text, value}",
    "contents.issueKeysAddedDuringSprint: {\"PROJ-1\": true, ...}   # dict, not list!",
    "sprint: {id, name, state, startDate, endDate, completeDate, ...}  # DISPLAY dates",
    "```",
    "",
    "Implications:",
    "",
    "* The per-issue dicts are a bespoke shape: `key, summary, typeName, typeId,",
    "  priorityName, priorityUrl, done (bool), assignee, assigneeName, assigneeKey,",
    "  avatarUrl, hasCustomUserAvatar, color, estimateStatistic, trackingStatistic,",
    "  statusId, statusName, statusUrl, fixVersions, projectId`. There is no `fields`",
    "  sub-object and no `self` URL.",
    "* `estimateStatistic.statFieldValue` is `{value: float}` or `{}` - a missing",
    "  estimate is an EMPTY DICT, not null.",
    "* `issueKeysAddedDuringSprint` is a **dict keyed by issue key** mapping to `true`.",
    "  Use `.keys()`; do not iterate it as a list.",
    "* `sprint.startDate` / `endDate` here are **localised display strings**",
    "  (e.g. `01/Jan/26 9:00 AM`), NOT ISO-8601. The ISO values live on",
    "  `/rest/agile/1.0/sprint/{id}`.",
    "* `*EstimateSum.value` is `None` when the board tracks issue count rather than",
    "  story points.",
    "",
    "`/rest/greenhopper/1.0/rapid/charts/velocity.json?rapidViewId=`:",
    "",
    "```",
    "sprints: list[{id, name, state, goal?, sequence, linkedPagesCount?}]",
    "velocityStatEntries: { \"<sprintId>\": {estimated: {text, value},",
    "                                      completed: {text, value}} }",
    "```",
    "",
    "* `velocityStatEntries` is a dict **keyed by the sprint id as a STRING**, while",
    "  `sprints[].id` is an `int`. Cast before lookup - this is the single most common",
    "  velocity bug.",
    "* `sprints` is ordered oldest-first on this server, but `sequence` is the",
    "  authoritative ordering key.",
    "* This endpoint is unversioned and unsupported by Atlassian; guard every access",
    "  and degrade gracefully rather than raising.",
    "",
    "### 6. Other things worth knowing",
    "",
    "* `/rest/api/2/project` (list form) does NOT include `lead`, `components` or",
    "  `versions`; only `/rest/api/2/project/{key}` does.",
    "* `/rest/agile/1.0/board` list items have **no `location.projectKey`** on this",
    "  server, so a project's board can only be found via",
    "  `?projectKeyOrId=<KEY>`.",
    "* `/rest/api/2/search` returns `{expand, startAt, maxResults, total, issues}`.",
    "  `total` is authoritative for counting (`maxResults=0`).",
    "* `expand=changelog` puts history at `issues[].changelog.{startAt,maxResults,",
    "  total,histories[]}` and each history is `{id, author, created, items[]}` with",
    "  items `{field, fieldtype, fieldId?, from, fromString, to, toString}`.",
    "  `from`/`to` are internal ids; `fromString`/`toString` are the labels.",
    "* `/rest/api/2/project/{key}/statuses` is a **list of issue types**, each with",
    "  its own `statuses` list - the same status appears once per issue type.",
    "* `/rest/api/2/field` entries are `{id, name, custom, orderable, navigable,",
    "  searchable, clauseNames[], schema{type,items?,custom?,customId?}}`.",
    "  System fields have no `schema.custom`.",
    "* Worklog: `/issue/{key}/worklog` -> `{startAt, maxResults, total, worklogs[]}`,",
    "  each `{self, author, updateAuthor, comment, created, updated, started,",
    "  timeSpent, timeSpentSeconds, id, issueId}`. `started` is the one that matters",
    "  for date filtering; `created` is when it was entered.",
    "",
]


def scan(real_host: str, real_user: str, real_project: str = "",
         san: Sanitizer | None = None) -> dict[str, int]:
    counts = {
        "forbidden_term": 0,
        "tehran_offset_or_zone": 0,
        "real_host": 0,
        "real_username": 0,
        "real_project_key": 0,
        "foreign_email": 0,
        "arabic": 0,
        "residual_original_value": 0,
    }
    host_re = re.compile(re.escape(real_host), re.I) if real_host else None
    user_re = (
        re.compile(r"\b" + re.escape(real_user) + r"\b", re.I) if real_user else None
    )
    proj_re = (
        re.compile(r"\b" + re.escape(real_project) + r"\b") if real_project else None
    )
    # \uXXXX escapes survive ensure_ascii=True, so scan the DECODED text too
    esc_re = re.compile(r"\\u0[67][0-9a-fA-F]{2}")
    # every original value the sanitizer replaced must be gone from the output.
    # Compared as WHOLE JSON string values, not substrings - a summary that
    # happens to be the word "Task" would otherwise match 500 unrelated places.
    originals: set[str] = set()
    if san is not None:
        for mapping in (san._users, san._projects, san._texts, san._names):
            for original in mapping:
                o = str(original).strip()
                if o and not o.startswith("Sample "):
                    originals.add(o.lower())

    def _values(obj: Any) -> Any:
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from _values(k)
                yield from _values(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _values(v)
        elif isinstance(obj, str):
            yield obj

    for p in sorted(FIXTURES.glob("*")):
        if p.is_dir():
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        if host_re:
            counts["real_host"] += len(host_re.findall(txt))
        if user_re:
            counts["real_username"] += len(user_re.findall(txt))
        if proj_re:
            counts["real_project_key"] += len(proj_re.findall(txt))
        counts["foreign_email"] += sum(
            1 for m in EMAIL_RE.findall(txt) if not m.lower().endswith("example.com")
        )
        low = txt.lower()
        counts["forbidden_term"] += sum(
            low.count(t.lower()) for t in forbidden_terms()
        )
        counts["tehran_offset_or_zone"] += len(
            re.findall(r"Asia/Tehran|[+]0[34]:?30\b", txt)
        )
        counts["arabic"] += len(ARABIC.findall(txt))
        # json.dumps(ensure_ascii=True) emits \u06XX for Arabic script, which the
        # literal-character regex above cannot see - count the escapes as well.
        counts["arabic"] += len(esc_re.findall(txt))
        if p.name != "SHAPES.md" and originals and p.suffix == ".json":
            try:
                parsed = json.loads(txt)
            except ValueError:  # pragma: no cover
                parsed = None
            if parsed is not None:
                for s in _values(parsed):
                    if s.strip().lower() in originals:
                        counts["residual_original_value"] += 1
    return counts


def write_shapes_md(meta: list[dict], fixtures: dict[str, Any]) -> Path:
    lines = [
        "# Jira Server response shapes (sanitized fixtures)",
        "",
        "Captured read-only (GET only) from a live **Jira Software Server 10.3.0**",
        "(`deploymentType: Server`) by `scripts/capture_shapes.py`, then sanitized:",
        "hosts -> `https://jira.example.com`, users -> `alice`/`bob`/`carol`, project keys ->",
        "`PROJ`/`PRJ2`, all free text -> `Sample text N`, avatars dropped, numeric ids kept.",
        "",
        "These fixtures are for OFFLINE unit tests. They carry the *shape* of the real",
        "server's responses; every identifying value has been replaced.",
        "",
        "## Endpoints",
        "",
        "| fixture | endpoint | status |",
        "|---|---|---|",
    ]
    for m in meta:
        lines.append(f"| `{m['label']}.json` | `{m['path']}` | {m['status']} |")
    lines.append("")
    lines.extend(SURPRISES_MD)
    lines.append("")
    lines.append("## Key fields and types")
    lines.append("")
    for label, data in fixtures.items():
        lines.append(f"### {label}")
        lines.append("")
        lines.append("```")
        for path, typ in list(shape(data).items())[:120]:
            lines.append(f"{path}: {typ}")
        lines.append("```")
        lines.append("")
    p = FIXTURES / "SHAPES.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    env = load_env(CONFIG_PATH)
    base = env.get("JIRA_BASE_URL", "").rstrip("/")
    user = env.get("JIRA_EMAIL", "")
    secret = env.get("JIRA_API_TOKEN", "")
    project = env.get("JIRA_PROJECT", "")
    if not (base and user and secret):
        raise SystemExit("config.env missing JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN")
    host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
    real_email = user if "@" in user else ""
    real_user = user.split("@")[0]

    san = Sanitizer(host, real_user, real_email)

    if "--scan" in argv:
        print("scan:", scan(host, real_user, project))
        return 0

    if "--shapes" in argv:
        # rebuild SHAPES.md from the fixtures already on disk - NO network
        meta_path = FIXTURES / "_capture_meta.json"
        meta = (
            json.loads(meta_path.read_text(encoding="utf-8"))
            if meta_path.exists()
            else {"endpoints": []}
        )
        written = {
            p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(FIXTURES.glob("*.json"))
            if not p.name.startswith("_")
        }
        md = write_shapes_md(meta.get("endpoints", []), written)
        print(f"wrote {md.name} from {len(written)} fixtures (no network)")
        print("leak scan (all must be 0):", scan(host, real_user, project))
        return 0

    cap = Capture(base, user, secret, san)

    print("capturing (GET only):")
    cap.get("serverinfo", "/rest/api/2/serverInfo")
    cap.get("myself", "/rest/api/2/myself")
    projects = cap.get("project_list", "/rest/api/2/project")
    key = project or (projects[0]["key"] if projects else "")
    cap.get("project_detail", f"/rest/api/2/project/{key}")

    # ---- createmeta: Jira 10 removed the legacy single-shot endpoint -------
    legacy = cap.get(
        "createmeta",
        "/rest/api/2/issue/createmeta",
        {"projectKeys": key, "expand": "projects.issuetypes.fields"},
    )
    if legacy is None:
        types = cap.get(
            "createmeta_issuetypes", f"/rest/api/2/issue/createmeta/{key}/issuetypes"
        )
        type_id = None
        if isinstance(types, dict) and types.get("values"):
            type_id = types["values"][0].get("id")
        elif isinstance(types, list) and types:
            type_id = types[0].get("id")
        if type_id:
            cap.get(
                "createmeta_fields",
                f"/rest/api/2/issue/createmeta/{key}/issuetypes/{type_id}",
                {"maxResults": 200},
            )

    # ---- boards and sprints ----------------------------------------------
    boards = cap.get("board_list", "/rest/agile/1.0/board", {"projectKeyOrId": key})
    scrum_board = None
    if boards and boards.get("values"):
        for b in boards["values"]:
            if b.get("type") == "scrum":
                scrum_board = b["id"]
                break
    sprint_id = None
    if scrum_board:
        sprints = cap.get(
            "board_sprints",
            f"/rest/agile/1.0/board/{scrum_board}/sprint",
            {"maxResults": 10},
        )
        if sprints and sprints.get("values"):
            sprint_id = sprints["values"][0]["id"]
    if sprint_id is None:
        # the project's own board is kanban (kanban boards 400 on /sprint), so
        # walk the instance's scrum boards until one actually has a sprint
        allscrum = cap.get(
            "scrum_board_lookup",
            "/rest/agile/1.0/board",
            {"type": "scrum", "maxResults": 20},
        )
        candidates = [
            b["id"] for b in (allscrum or {}).get("values", []) if b.get("id")
        ]
        for idx, cand in enumerate(candidates[:8]):
            label = "scrum_board_sprints" if idx == 0 else f"scrum_board_sprints_{idx}"
            sb = cap.get(
                label,
                f"/rest/agile/1.0/board/{cand}/sprint",
                {"maxResults": 10, "state": "active,closed,future"},
            )
            if sb and sb.get("values"):
                scrum_board = cand
                # prefer a closed sprint - sprintreport is richest there
                vals = sb["values"]
                closed = [s for s in vals if s.get("state") == "closed"]
                sprint_id = (closed or vals)[0]["id"]
                break
    if sprint_id:
        cap.get(
            "sprint_detail", f"/rest/agile/1.0/sprint/{sprint_id}"
        )
        cap.get(
            "sprint_issues", f"/rest/agile/1.0/sprint/{sprint_id}/issue", {"maxResults": 3}
        )

    # ---- search + one issue's worklog/comments ----------------------------
    # pick issues that actually HAVE worklogs so the worklog fixture is non-empty
    search = cap.get(
        "search_changelog",
        "/rest/api/2/search",
        {
            "jql": "worklogDate >= -30d ORDER BY updated DESC",
            "maxResults": 5,
            "expand": "changelog",
        },
    )
    if not (search and search.get("issues")):
        search = cap.get(
            "search_changelog",
            "/rest/api/2/search",
            {
                "jql": f"project = {key} ORDER BY updated DESC",
                "maxResults": 5,
                "expand": "changelog",
            },
        )
    issue_key = None
    if search and search.get("issues"):
        issue_key = search["issues"][0]["key"]
        for iss in search["issues"]:
            if ((iss.get("fields") or {}).get("timespent") or 0):
                issue_key = iss["key"]
                break
    if issue_key:
        cap.get("issue_worklog", f"/rest/api/2/issue/{issue_key}/worklog")
        cap.get("issue_comments", f"/rest/api/2/issue/{issue_key}/comment")

    # ---- metadata ---------------------------------------------------------
    cap.get("field_list", "/rest/api/2/field")
    cap.get("user_search", "/rest/api/2/user/search", {"username": "a", "maxResults": 5})
    cap.get(
        "user_assignable",
        "/rest/api/2/user/assignable/search",
        {"project": key, "maxResults": 5},
    )
    cap.get("priority", "/rest/api/2/priority")
    cap.get("project_statuses", f"/rest/api/2/project/{key}/statuses")
    cap.get("issue_link_types", "/rest/api/2/issueLinkType")
    cap.get("filter_favourite", "/rest/api/2/filter/favourite")
    if scrum_board and sprint_id:
        cap.get(
            "greenhopper_sprintreport",
            "/rest/greenhopper/1.0/rapid/charts/sprintreport",
            {"rapidViewId": scrum_board, "sprintId": sprint_id},
        )
    if scrum_board:
        cap.get(
            "greenhopper_velocity",
            "/rest/greenhopper/1.0/rapid/charts/velocity.json",
            {"rapidViewId": scrum_board},
        )

    print(f"requests made: {cap.count}")
    written: dict[str, Any] = {}
    for label, data in cap.raw.items():
        p = cap.write(label, data)
        written[label] = json.loads(p.read_text(encoding="utf-8"))
        print(f"  wrote {p.name} ({p.stat().st_size} bytes)")
    (FIXTURES / "_capture_meta.json").write_text(
        json.dumps(
            {
                "server": "Jira Software Server 10.3.0 (sanitized capture)",
                "requests": cap.count,
                "endpoints": [
                    {
                        "label": m["label"],
                        "path": san.string(m["path"]),
                        "status": m["status"],
                        "error": san.string(m["error"]),
                    }
                    for m in cap.meta
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    md = write_shapes_md(
        [
            {
                "label": m["label"],
                "path": san.string(m["path"]),
                "status": m["status"],
            }
            for m in cap.meta
        ],
        written,
    )
    print(f"  wrote {md.name}")
    print("leak scan (all must be 0):", scan(host, real_user, project, san))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
