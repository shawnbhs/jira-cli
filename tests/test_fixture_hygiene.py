"""Fixture hygiene: the committed Jira fixtures must stay de-identified.

The fixtures under ``tests/fixtures`` were captured from a real server and then
sanitized. This repo is public, so any regression that reintroduces a real
person, organisation, host or location is a data leak. These checks are
deliberately blunt (whole-file text scans) so they also catch values hidden in
places a structural sanitizer may not know about.

Extra organisation-specific terms (company, client and product names) are
supplied at run time through ``JIRA_FIXTURE_FORBIDDEN`` (comma-separated,
case-insensitive) so the real words never have to be written into the repo.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FILES = sorted(p for p in FIXTURES.rglob("*") if p.is_file() and p.suffix in {".json", ".md"})

ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
ARABIC_ESCAPE = re.compile(r"\\u0[67][0-9a-fA-F]{2}|\\u(?:fb[5-9a-f]|fc|fd|fe[7-9a-f]|ff)[0-9a-fA-F]", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://([^/\s\"'<>:?#`,)\]]+)", re.I)
TEHRAN_OFFSET = re.compile(r"T\d{2}:\d{2}[^\"\s]*[+]0[34]:?30\b")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ids() -> list[str]:
    return [p.relative_to(FIXTURES).as_posix() for p in FILES]


def _forbidden_terms() -> list[str]:
    raw = os.environ.get("JIRA_FIXTURE_FORBIDDEN", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


# Read at import time: the autouse conftest fixture strips every ``JIRA_*``
# variable before each test runs, so a lookup inside the test would always miss.
FORBIDDEN_TERMS = _forbidden_terms()


def test_fixture_files_are_discovered() -> None:
    # Guards against the scans below silently passing on an empty set.
    names = {p.name for p in FILES}
    assert {"search_changelog.json", "project_detail.json", "SHAPES.md"} <= names


@pytest.mark.parametrize("path", FILES, ids=_ids())
def test_no_arabic_script(path: Path) -> None:
    text = _text(path)
    assert not ARABIC_SCRIPT.findall(text), f"{path.name}: Arabic-script characters"
    assert not ARABIC_ESCAPE.findall(text), f"{path.name}: escaped Arabic-script characters"


@pytest.mark.parametrize("path", FILES, ids=_ids())
def test_emails_use_example_domain(path: Path) -> None:
    bad = sorted({d for d in EMAIL.findall(_text(path)) if d.lower() != "example.com"})
    assert not bad, f"{path.name}: {len(bad)} non-example.com email domain(s)"


@pytest.mark.parametrize("path", FILES, ids=_ids())
def test_urls_use_example_hosts(path: Path) -> None:
    hosts = {h.lower() for h in URL.findall(_text(path))}
    bad = sorted(h for h in hosts if h != "example.com" and not h.endswith(".example.com"))
    assert not bad, f"{path.name}: URL hosts outside example.com: {bad}"


@pytest.mark.parametrize("path", FILES, ids=_ids())
def test_no_tehran_location(path: Path) -> None:
    text = _text(path)
    assert "asia/tehran" not in text.lower(), f"{path.name}: real time zone"
    assert not TEHRAN_OFFSET.findall(text), f"{path.name}: +03:30/+04:30 timestamp offset"


@pytest.mark.parametrize("path", FILES, ids=_ids())
def test_no_real_user_keys(path: Path) -> None:
    # Synthetic Server user keys are JIRAUSER10000..JIRAUSER10099.
    keys = set(re.findall(r"JIRAUSER(\d+)\b", _text(path)))
    bad = sorted(k for k in keys if not 10000 <= int(k) <= 10099)
    assert not bad, f"{path.name}: {len(bad)} non-synthetic JIRAUSER key(s)"


@pytest.mark.parametrize("path", FILES, ids=_ids())
def test_no_env_forbidden_terms(path: Path) -> None:
    terms = FORBIDDEN_TERMS
    if not terms:
        pytest.skip("JIRA_FIXTURE_FORBIDDEN not set")
    low = _text(path).lower()
    hits = sum(1 for t in terms if t.lower() in low)
    # Never echo the terms themselves: the failure message would leak them.
    assert hits == 0, f"{path.name}: {hits} forbidden term(s) from JIRA_FIXTURE_FORBIDDEN"


def test_scanners_detect_leaks() -> None:
    # The scans are only worth something if they can fail; prove each one bites.
    assert ARABIC_SCRIPT.search("name \u0639\u0644\u06cc")
    assert ARABIC_ESCAPE.search('"\\u0639"')
    assert EMAIL.findall("x a.b@corp.test y") == ["corp.test"]
    assert URL.findall('"self": "https://jira.corp.test:8443/rest"') == ["jira.corp.test"]
    assert TEHRAN_OFFSET.search('"2026-01-01T10:00:00.000+0330"')
    assert TEHRAN_OFFSET.search('"2026-01-01T10:00:00.000+04:30"')
    assert not TEHRAN_OFFSET.search('"2026-01-01T10:00:00.000+0000"')
