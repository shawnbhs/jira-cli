"""Tests for :mod:`jira_cli.config` — precedence, aliases, auth choice, safety.

``S105``/``S106`` are silenced for the whole file: the "passwords" below are
fixture strings for an offline test, which is exactly what those rules flag.
"""

# ruff: noqa: S105, S106

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jira_cli import config as config_mod  # noqa: E402
from jira_cli.config import (  # noqa: E402
    AUTH_BASIC,
    AUTH_PAT,
    Settings,
    describe_settings,
    load_settings,
    parse_env_text,
)
from jira_cli.errors import ConfigError  # noqa: E402

BASE = "https://jira.example.com"
PAT = "pat-token-value-0001"
PASSWORD = "basic-password-0002"


def write_env(tmp_path: Path, text: str, name: str = ".env") -> Path:
    """Write an env file with owner-only permissions and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# --------------------------------------------------------------------------- #
# env-file parser
# --------------------------------------------------------------------------- #


def test_parser_reads_plain_assignments():
    assert parse_env_text("JIRA_BASE_URL=https://jira.example.com\nJIRA_PAT=abc") == {
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_PAT": "abc",
    }


def test_parser_strips_matching_quotes():
    parsed = parse_env_text("A=\"one two\"\nB='three four'")
    assert parsed == {"A": "one two", "B": "three four"}


def test_parser_ignores_comment_lines_and_blank_lines():
    assert parse_env_text("# a comment\n\nA=1\n   # another\n") == {"A": "1"}


def test_parser_strips_trailing_comment_from_unquoted_value():
    assert parse_env_text("A=1 # why not")["A"] == "1"


def test_parser_keeps_hash_inside_quotes():
    assert parse_env_text('A="pa#ss"')["A"] == "pa#ss"


def test_parser_does_not_expand_variables():
    assert parse_env_text("A=$HOME/x\nB=${OTHER}") == {"A": "$HOME/x", "B": "${OTHER}"}


def test_parser_accepts_export_prefix():
    assert parse_env_text("export A=1")["A"] == "1"


def test_parser_skips_lines_without_equals():
    assert parse_env_text("garbage line\nA=1") == {"A": "1"}


# --------------------------------------------------------------------------- #
# precedence
# --------------------------------------------------------------------------- #


def test_process_env_overrides_env_file(tmp_path):
    env_file = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT=from-file-token\n")
    settings = load_settings(
        {"JIRA_PAT": "from-process-token"}, env_file=env_file
    )
    assert settings.secret == "from-process-token"


def test_env_file_supplies_values_absent_from_process_env(tmp_path):
    env_file = write_env(
        tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\nJIRA_DEFAULT_PROJECT=proj\n"
    )
    settings = load_settings({}, env_file=env_file)
    assert (settings.base_url, settings.secret, settings.default_project) == (
        BASE,
        PAT,
        "PROJ",
    )


def test_explicit_env_file_argument_beats_jira_cli_env_file(tmp_path):
    chosen = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT=chosen\n", "chosen.env")
    other = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT=other\n", "other.env")
    settings = load_settings({"JIRA_CLI_ENV_FILE": str(other)}, env_file=chosen)
    assert settings.secret == "chosen"


def test_jira_cli_env_file_is_used_when_no_argument_given(tmp_path):
    pointed = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\n", "pointed.env")
    settings = load_settings({"JIRA_CLI_ENV_FILE": str(pointed)})
    assert settings.secret == PAT


def test_config_dir_env_file_is_discovered(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    write_env(config_dir, f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\n")
    settings = load_settings({"JIRA_CONFIG_DIR": str(config_dir)})
    assert settings.secret == PAT
    assert settings.config_dir == config_dir


def test_missing_explicit_env_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="env file not found"):
        load_settings({}, env_file=tmp_path / "absent.env")


def test_legacy_jira_cli_config_env_is_discovered():
    """The previous version's ``~/.jira-cli/config.env`` still works untouched."""
    legacy = config_mod.LEGACY_ENV_FILE
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        f"JIRA_BASE_URL={BASE}\nJIRA_EMAIL=alice\nJIRA_API_TOKEN={PASSWORD}\n",
        encoding="utf-8",
    )
    os.chmod(legacy, 0o600)
    settings = load_settings({})
    assert (settings.auth_mode, settings.username, settings.secret) == (
        AUTH_BASIC,
        "alice",
        PASSWORD,
    )


def test_config_dir_env_file_beats_the_legacy_file(tmp_path):
    legacy = config_mod.LEGACY_ENV_FILE
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(f"JIRA_BASE_URL={BASE}\nJIRA_PAT=legacy-token-1\n", encoding="utf-8")
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    write_env(config_dir, f"JIRA_BASE_URL={BASE}\nJIRA_PAT=modern-token-2\n")
    settings = load_settings({"JIRA_CONFIG_DIR": str(config_dir)})
    assert settings.secret == "modern-token-2"


# --------------------------------------------------------------------------- #
# legacy aliases — the shape ~/.jira-cli/config.env actually has
# --------------------------------------------------------------------------- #


def test_legacy_aliases_are_read_when_modern_names_are_absent(tmp_path):
    env_file = write_env(
        tmp_path,
        f"JIRA_BASE_URL={BASE}\n"
        "JIRA_EMAIL=alice\n"
        f"JIRA_API_TOKEN={PASSWORD}\n"
        "JIRA_PROJECT=proj\n",
    )
    settings = load_settings({}, env_file=env_file)
    assert settings.auth_mode == AUTH_BASIC
    assert settings.username == "alice"
    assert settings.secret == PASSWORD
    assert settings.default_project == "PROJ"


def test_modern_names_win_over_legacy_aliases(tmp_path):
    env_file = write_env(
        tmp_path,
        f"JIRA_BASE_URL={BASE}\n"
        "JIRA_EMAIL=alice\nJIRA_USERNAME=bob\n"
        f"JIRA_API_TOKEN=legacy-secret\nJIRA_PASSWORD={PASSWORD}\n"
        "JIRA_PROJECT=OLD\nJIRA_DEFAULT_PROJECT=NEW\n",
    )
    settings = load_settings({}, env_file=env_file)
    assert (settings.username, settings.secret, settings.default_project) == (
        "bob",
        PASSWORD,
        "NEW",
    )


# --------------------------------------------------------------------------- #
# auth mode selection
# --------------------------------------------------------------------------- #


def test_pat_is_chosen_automatically_when_present():
    settings = load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT}, env_file=None)
    assert settings.auth_mode == AUTH_PAT
    assert settings.secret == PAT


def test_basic_is_chosen_when_no_pat_is_present():
    settings = load_settings(
        {"JIRA_BASE_URL": BASE, "JIRA_USERNAME": "alice", "JIRA_PASSWORD": PASSWORD}
    )
    assert settings.auth_mode == AUTH_BASIC


def test_explicit_basic_mode_ignores_a_present_pat():
    settings = load_settings(
        {
            "JIRA_BASE_URL": BASE,
            "JIRA_AUTH_MODE": "basic",
            "JIRA_PAT": PAT,
            "JIRA_USERNAME": "alice",
            "JIRA_PASSWORD": PASSWORD,
        }
    )
    assert settings.auth_mode == AUTH_BASIC
    assert settings.secret == PASSWORD


def test_pat_mode_allows_an_empty_username():
    settings = load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT})
    assert settings.username == ""


def test_pat_mode_without_a_pat_is_a_config_error():
    with pytest.raises(ConfigError, match="needs JIRA_PAT"):
        load_settings({"JIRA_BASE_URL": BASE, "JIRA_AUTH_MODE": "pat"})


def test_basic_mode_without_a_username_is_a_config_error():
    with pytest.raises(ConfigError, match="JIRA_USERNAME"):
        load_settings({"JIRA_BASE_URL": BASE, "JIRA_PASSWORD": PASSWORD})


def test_basic_mode_without_a_password_is_a_config_error():
    with pytest.raises(ConfigError, match="JIRA_PASSWORD"):
        load_settings({"JIRA_BASE_URL": BASE, "JIRA_USERNAME": "alice"})


def test_unknown_auth_mode_is_a_config_error():
    with pytest.raises(ConfigError, match="JIRA_AUTH_MODE"):
        load_settings(
            {"JIRA_BASE_URL": BASE, "JIRA_AUTH_MODE": "oauth", "JIRA_PAT": PAT}
        )


def test_switching_account_needs_only_an_env_file_edit(tmp_path):
    """A different (e.g. admin) account is a file edit, not a code change."""
    env_file = write_env(
        tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_USERNAME=alice\nJIRA_PAT=alice-token-001\n"
    )
    first = load_settings({}, env_file=env_file)

    env_file.write_text(
        f"JIRA_BASE_URL={BASE}\nJIRA_USERNAME=bob\nJIRA_PAT=bob-admin-token-002\n",
        encoding="utf-8",
    )
    second = load_settings({}, env_file=env_file)

    assert (first.username, second.username) == ("alice", "bob")
    assert second.secret == "bob-admin-token-002"


# --------------------------------------------------------------------------- #
# base url and scalar coercion
# --------------------------------------------------------------------------- #


def test_missing_base_url_is_a_config_error():
    with pytest.raises(ConfigError, match="JIRA_BASE_URL"):
        load_settings({"JIRA_PAT": PAT})


def test_nothing_configured_anywhere_is_a_config_error_naming_jira_base_url():
    """Regression: with no JIRA_* vars and no env file, loading must fail."""
    with pytest.raises(ConfigError, match="Set JIRA_BASE_URL"):
        load_settings()


def test_missing_base_url_error_names_the_places_that_were_searched():
    with pytest.raises(ConfigError) as caught:
        load_settings({})
    assert str(config_mod.LEGACY_ENV_FILE) in str(caught.value)


def test_missing_base_url_error_names_the_env_file_that_was_read(tmp_path):
    env_file = write_env(tmp_path, f"JIRA_PAT={PAT}\n")
    with pytest.raises(ConfigError, match="JIRA_BASE_URL") as caught:
        load_settings({}, env_file=env_file)
    assert str(env_file) in str(caught.value)


def test_base_url_without_any_credentials_is_a_config_error():
    with pytest.raises(ConfigError, match="no Jira credentials found"):
        load_settings({"JIRA_BASE_URL": BASE})


def test_missing_credentials_error_says_which_variables_to_set():
    with pytest.raises(ConfigError) as caught:
        load_settings({"JIRA_BASE_URL": BASE})
    message = str(caught.value)
    assert "JIRA_PAT" in message
    assert "JIRA_USERNAME" in message
    assert "JIRA_PASSWORD" in message


def test_blank_credentials_count_as_missing():
    with pytest.raises(ConfigError, match="no Jira credentials found"):
        load_settings(
            {"JIRA_BASE_URL": BASE, "JIRA_PAT": "  ", "JIRA_USERNAME": "", "JIRA_PASSWORD": ""}
        )


def test_suite_isolation_points_discovery_away_from_the_real_home(tmp_path_factory):
    """Guard for tests/conftest.py: no test may see a developer's real config."""
    sandbox = tmp_path_factory.getbasetemp()
    home = Path(os.environ["HOME"])
    assert home.is_relative_to(sandbox)
    assert config_mod.LEGACY_ENV_FILE.is_relative_to(home)
    assert config_mod.DEFAULT_CONFIG_DIR.is_relative_to(home)
    assert Path(os.environ["JIRA_CLI_ENV_FILE"]).is_relative_to(home)
    assert sorted(k for k in os.environ if k.startswith("JIRA_")) == ["JIRA_CLI_ENV_FILE"]
    assert config_mod.config_dir().is_relative_to(home)


def test_base_url_without_a_scheme_is_a_config_error():
    with pytest.raises(ConfigError, match="http"):
        load_settings({"JIRA_BASE_URL": "jira.example.com", "JIRA_PAT": PAT})


def test_ftp_base_url_is_a_config_error():
    with pytest.raises(ConfigError, match="https://"):
        load_settings({"JIRA_BASE_URL": "ftp://jira.example.com", "JIRA_PAT": PAT})


def test_plain_http_base_url_is_refused_by_default(capsys):
    with pytest.raises(ConfigError, match="JIRA_ALLOW_HTTP=1"):
        load_settings({"JIRA_BASE_URL": "http://jira.example.com", "JIRA_PAT": PAT})
    assert PAT not in capsys.readouterr().err


def test_plain_http_is_refused_when_allow_http_is_off():
    with pytest.raises(ConfigError, match="plain http://"):
        load_settings(
            {
                "JIRA_BASE_URL": "http://jira.example.com",
                "JIRA_PAT": PAT,
                "JIRA_ALLOW_HTTP": "0",
            }
        )


def test_uppercase_http_scheme_is_still_refused():
    with pytest.raises(ConfigError, match="plain http://"):
        load_settings({"JIRA_BASE_URL": "HTTP://jira.example.com", "JIRA_PAT": PAT})


def test_plain_http_is_accepted_with_allow_http_and_warns_once(capsys):
    env = {
        "JIRA_BASE_URL": "http://jira.example.com",
        "JIRA_PAT": PAT,
        "JIRA_ALLOW_HTTP": "1",
    }
    settings = load_settings(env)
    load_settings(env)
    err = capsys.readouterr().err
    assert settings.base_url == "http://jira.example.com"
    assert err.count("unencrypted") == 1
    assert PAT not in err


def test_allow_http_can_come_from_the_env_file(tmp_path):
    env_file = write_env(
        tmp_path, f"JIRA_BASE_URL=http://jira.example.com\nJIRA_PAT={PAT}\nJIRA_ALLOW_HTTP=1\n"
    )
    assert load_settings({}, env_file=env_file).base_url == "http://jira.example.com"


def test_non_boolean_allow_http_is_a_config_error():
    with pytest.raises(ConfigError, match="JIRA_ALLOW_HTTP"):
        load_settings(
            {
                "JIRA_BASE_URL": "http://jira.example.com",
                "JIRA_PAT": PAT,
                "JIRA_ALLOW_HTTP": "sure",
            }
        )


def test_https_base_url_prints_nothing_on_stderr(capsys):
    load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT})
    assert capsys.readouterr().err == ""


def test_trailing_slash_is_stripped_from_base_url():
    settings = load_settings({"JIRA_BASE_URL": BASE + "/", "JIRA_PAT": PAT})
    assert settings.base_url == BASE


def test_defaults_match_the_contract():
    settings = load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT})
    assert settings.timeout == 20
    assert settings.read_only is False
    assert settings.hours_per_day == 8
    assert settings.days_per_week == 5
    assert settings.verify_tls is True
    assert settings.config_dir == config_mod.DEFAULT_CONFIG_DIR
    assert settings.teams_file == config_mod.DEFAULT_CONFIG_DIR / "teams.yaml"
    assert settings.filters_file == config_mod.DEFAULT_CONFIG_DIR / "filters.yaml"
    assert settings.field_overrides == {}


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_read_only_truthy_values(raw):
    settings = load_settings(
        {"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_READ_ONLY": raw}
    )
    assert settings.read_only is True


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off"])
def test_read_only_falsy_values(raw):
    settings = load_settings(
        {"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_READ_ONLY": raw}
    )
    assert settings.read_only is False


def test_non_boolean_read_only_is_a_config_error():
    with pytest.raises(ConfigError, match="boolean"):
        load_settings(
            {"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_READ_ONLY": "maybe"}
        )


def test_verify_tls_accepts_a_ca_bundle_path():
    settings = load_settings(
        {"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_VERIFY_TLS": "/etc/ca.pem"}
    )
    assert settings.verify_tls == "/etc/ca.pem"


def test_verify_tls_can_be_disabled_explicitly():
    settings = load_settings(
        {"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_VERIFY_TLS": "0"}
    )
    assert settings.verify_tls is False


def test_disabled_verify_tls_warns_on_stderr_exactly_once(capsys):
    env = {"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_VERIFY_TLS": "false"}
    load_settings(env)
    load_settings(env)
    err = capsys.readouterr().err
    assert err.count("JIRA_VERIFY_TLS is off") == 1
    assert "NOT verified" in err
    assert PAT not in err


def test_ca_bundle_verify_tls_does_not_warn(capsys):
    load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_VERIFY_TLS": "/etc/ca.pem"})
    assert capsys.readouterr().err == ""


def test_non_numeric_timeout_is_a_config_error():
    with pytest.raises(ConfigError, match="JIRA_TIMEOUT"):
        load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_TIMEOUT": "soon"})


def test_zero_timeout_is_a_config_error():
    with pytest.raises(ConfigError, match="greater than zero"):
        load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_TIMEOUT": "0"})


def test_field_overrides_are_collected_from_both_layers(tmp_path):
    env_file = write_env(
        tmp_path,
        f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\nJIRA_FIELD_SPRINT=customfield_1\n"
        "JIRA_FIELD_EPIC_LINK=customfield_2\n",
    )
    settings = load_settings({"JIRA_FIELD_SPRINT": "customfield_9"}, env_file=env_file)
    assert settings.field_overrides == {
        "SPRINT": "customfield_9",
        "EPIC_LINK": "customfield_2",
    }


# --------------------------------------------------------------------------- #
# permissions warning
# --------------------------------------------------------------------------- #


def test_group_readable_env_file_warns_on_stderr(tmp_path, capsys):
    env_file = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\n", "loose.env")
    os.chmod(env_file, 0o644)
    load_settings({}, env_file=env_file)
    captured = capsys.readouterr()
    assert "readable by other users" in captured.err
    assert PAT not in captured.err


def test_owner_only_env_file_does_not_warn(tmp_path, capsys):
    env_file = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\n", "tight.env")
    load_settings({}, env_file=env_file)
    assert capsys.readouterr().err == ""


def test_world_readable_env_file_still_loads_and_warns_once(tmp_path, capsys):
    """Loose permissions warn rather than fail, so nobody is locked out."""
    env_file = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\n", "open.env")
    os.chmod(env_file, 0o604)
    first = load_settings({}, env_file=env_file)
    load_settings({}, env_file=env_file)
    assert first.secret == PAT
    assert capsys.readouterr().err.count("readable by other users") == 1


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership check")
def test_env_file_owned_by_another_uid_warns(tmp_path, capsys, monkeypatch):
    env_file = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\n", "foreign.env")
    real_uid = os.getuid()
    monkeypatch.setattr(config_mod.os, "getuid", lambda: real_uid + 1)
    settings = load_settings({}, env_file=env_file)
    err = capsys.readouterr().err
    assert settings.secret == PAT
    assert "not by the current user" in err
    assert f"uid {real_uid}" in err
    assert PAT not in err


def test_env_file_owned_by_current_user_has_no_owner_warning(tmp_path, capsys):
    env_file = write_env(tmp_path, f"JIRA_BASE_URL={BASE}\nJIRA_PAT={PAT}\n", "mine.env")
    load_settings({}, env_file=env_file)
    assert "current user" not in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# config_dir() — public location for other modules (e.g. plans/)
# --------------------------------------------------------------------------- #


def test_config_dir_defaults_to_the_default_config_dir():
    assert config_mod.config_dir({}) == config_mod.DEFAULT_CONFIG_DIR


def test_config_dir_honours_jira_config_dir(tmp_path):
    assert config_mod.config_dir({"JIRA_CONFIG_DIR": str(tmp_path / "cfg")}) == tmp_path / "cfg"


def test_config_dir_reads_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CONFIG_DIR", str(tmp_path / "from-env"))
    assert config_mod.config_dir() == tmp_path / "from-env"


def test_config_dir_ignores_a_blank_jira_config_dir():
    assert config_mod.config_dir({"JIRA_CONFIG_DIR": "  "}) == config_mod.DEFAULT_CONFIG_DIR


def test_config_dir_matches_settings_config_dir(tmp_path):
    env = {"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT, "JIRA_CONFIG_DIR": str(tmp_path)}
    assert config_mod.config_dir(env) == load_settings(env).config_dir


def test_config_dir_does_not_create_the_directory(tmp_path):
    target = tmp_path / "not-yet"
    config_mod.config_dir({"JIRA_CONFIG_DIR": str(target)})
    assert not target.exists()


# --------------------------------------------------------------------------- #
# the secret never escapes
# --------------------------------------------------------------------------- #


def test_describe_settings_hides_the_secret_and_its_length():
    settings = load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT})
    described = describe_settings(settings)
    assert described["secret"] == "set"
    assert PAT not in repr(described)


def test_describe_settings_says_not_set_for_an_empty_secret():
    settings = Settings(
        base_url=BASE, auth_mode=AUTH_PAT, username="alice", secret="",
        default_project=None,
    )
    assert describe_settings(settings)["secret"] == "not set"


def test_secret_is_absent_from_repr_and_str():
    settings = load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT})
    assert PAT not in repr(settings)
    assert PAT not in str(settings)
    assert "secret=<hidden>" in repr(settings)


def test_loading_a_secret_registers_it_with_the_redactor():
    from jira_cli.redact import redact

    load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": "registered-token-xyz"})
    assert "registered-token-xyz" not in redact("leak: registered-token-xyz")


def test_settings_is_frozen():
    settings = load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT})
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.base_url = "https://evil.example.com"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# credentials embedded in JIRA_BASE_URL (J9 F3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://alice:hunter2pass@jira.example.com",
        "https://alice@jira.example.com",
        "https://alice:p@ss@jira.example.com/ctx",
    ],
)
def test_base_url_with_userinfo_is_refused_without_echoing_the_password(url):
    with pytest.raises(ConfigError) as info:
        load_settings({"JIRA_BASE_URL": url, "JIRA_PAT": PAT}, env_file=None)
    message = str(info.value)
    assert "username or password" in message
    assert "hunter2pass" not in message
    assert "p@ss" not in message
    assert "jira.example.com" in message


def test_display_url_strips_userinfo_and_keeps_the_rest():
    assert (
        config_mod.display_url("https://alice:p@ss@jira.example.com/ctx?a=1")
        == "https://jira.example.com/ctx?a=1"
    )
    assert config_mod.display_url("https://jira.example.com") == "https://jira.example.com"
    assert config_mod.display_url("not a url") == "not a url"


def test_hand_built_settings_never_display_userinfo():
    settings = config_mod.Settings(
        base_url="https://alice:hunter2pass@jira.example.com",
        auth_mode="pat",
        username="",
        secret="",
    )
    assert "hunter2pass" not in repr(settings)
    assert "hunter2pass" not in str(config_mod.describe_settings(settings))
    assert config_mod.describe_settings(settings)["base_url"] == "https://jira.example.com"


def test_plain_base_url_still_loads():
    settings = load_settings({"JIRA_BASE_URL": BASE, "JIRA_PAT": PAT}, env_file=None)
    assert settings.base_url == BASE.rstrip("/")
