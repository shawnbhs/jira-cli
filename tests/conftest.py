"""Suite-wide isolation: no test may read the developer's real Jira config.

``jira_cli.config`` discovers credentials from ``$JIRA_CLI_ENV_FILE``,
``~/.config/jira-cli/.env`` and the legacy ``~/.jira-cli/config.env``, and the
two path constants are computed from ``Path.home()`` at import time. Setting
``HOME`` alone is therefore not enough, so the autouse fixture below also
re-points those module constants at a per-test temporary directory. The
public ``config.config_dir()`` reads ``DEFAULT_CONFIG_DIR`` at call time, so it
resolves inside the same temp dir (asserted in ``test_core_config.py``).

It also snapshots the redactor's registered literals and restores them after
each test, so a secret registered by one test cannot mask (or fail to mask)
text in another.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jira_cli import config as config_mod  # noqa: E402
from jira_cli import redact as redact_mod  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_jira_config(tmp_path_factory, monkeypatch):
    """Point every config location at a fresh temp dir and drop all ``JIRA_*`` vars.

    The directory comes from ``tmp_path_factory`` rather than ``tmp_path`` so
    tests that assert on the contents of their own ``tmp_path`` stay unaffected.
    """
    home = tmp_path_factory.mktemp("isolated-home")
    for name in list(os.environ):
        if name.startswith("JIRA_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("JIRA_CLI_ENV_FILE", str(home / "isolated-test.env"))
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_DIR", home / ".config" / "jira-cli")
    monkeypatch.setattr(config_mod, "LEGACY_ENV_FILE", home / ".jira-cli" / "config.env")
    monkeypatch.setattr(config_mod, "_warned_keys", set())
    return home


@pytest.fixture(autouse=True)
def restore_registered_secrets():
    """Forget any secret a test registers; keep import-time registrations."""
    before = redact_mod.registered_secrets()
    yield
    redact_mod.clear_secrets()
    for literal in before:
        redact_mod.register_secret(literal)
