"""Settings loading: environment, env file, and nothing else.

Design rules that the rest of the package depends on:

* **Credentials come from the environment or an env file only.** They are never
  CLI arguments, never defaults in code, and never written back to disk.
* **Process environment beats the env file**, so a one-off
  ``JIRA_PAT=... jira me`` overrides the stored account without editing a file.
* **Switching accounts is an env-file edit.** Everything about who we are
  (``JIRA_AUTH_MODE``, ``JIRA_PAT``, ``JIRA_USERNAME``, ``JIRA_PASSWORD``)
  lives in that one file, so pointing the CLI at a different — e.g. admin —
  account needs no code change and no re-install.
* **Legacy names keep working.** The previous generation of this tool wrote
  ``~/.jira-cli/config.env`` with ``JIRA_EMAIL`` / ``JIRA_API_TOKEN`` /
  ``JIRA_PROJECT``; those are read when the modern name is absent.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import ConfigError
from .redact import register_secret

DEFAULT_TIMEOUT = 20.0
DEFAULT_HOURS_PER_DAY = 8.0
DEFAULT_DAYS_PER_WEEK = 5.0
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "jira-cli"
LEGACY_ENV_FILE = Path.home() / ".jira-cli" / "config.env"

AUTH_PAT = "pat"
AUTH_BASIC = "basic"
AUTH_MODES = (AUTH_PAT, AUTH_BASIC)

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}

# Env names whose value is a credential; registered with the redactor on load.
_SECRET_ENV_NAMES = ("JIRA_PAT", "JIRA_PASSWORD", "JIRA_API_TOKEN")

# Display-name -> env suffix for the four fields the contract lets you pin.
FIELD_ENV_PREFIX = "JIRA_FIELD_"
KNOWN_FIELD_KEYS = ("SPRINT", "EPIC_LINK", "EPIC_NAME", "STORY_POINTS")

# Keys of stderr warnings already printed by this process (paths, setting
# names). Security warnings are worth one line each, not one per load.
_warned_keys: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Print ``warning: <message>`` to stderr unless ``key`` already warned."""
    if key in _warned_keys:
        return
    _warned_keys.add(key)
    print(f"warning: {message}", file=sys.stderr)


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the jira-cli config directory (``$JIRA_CONFIG_DIR`` or the default).

    For callers that need a place on disk (e.g. a ``plans`` subfolder) without
    loading credentials. Only the process environment is consulted; code that
    already holds a :class:`Settings` should use ``settings.config_dir``, which
    also honours ``JIRA_CONFIG_DIR`` set inside the env file. The default is
    read at call time so test isolation that re-points ``DEFAULT_CONFIG_DIR``
    is respected. The directory is not created.
    """
    source = os.environ if env is None else env
    raw = (source.get("JIRA_CONFIG_DIR") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_DIR


@dataclass(frozen=True)
class Settings:
    """Everything the client and services need, resolved and validated once."""

    base_url: str
    auth_mode: str
    username: str
    secret: str = field(repr=False)
    default_project: str | None = None
    verify_tls: bool | str = True
    timeout: float = DEFAULT_TIMEOUT
    read_only: bool = False
    hours_per_day: float = DEFAULT_HOURS_PER_DAY
    days_per_week: float = DEFAULT_DAYS_PER_WEEK
    config_dir: Path = DEFAULT_CONFIG_DIR
    teams_file: Path = DEFAULT_CONFIG_DIR / "teams.yaml"
    filters_file: Path = DEFAULT_CONFIG_DIR / "filters.yaml"
    field_overrides: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.secret:
            register_secret(self.secret)

    def __repr__(self) -> str:
        # Explicit, because dataclass repr=False on one field is easy to lose
        # in a later edit and this is a security property, not a nicety.
        return (
            f"Settings(base_url={display_url(self.base_url)!r}, auth_mode={self.auth_mode!r}, "
            f"username={self.username!r}, secret=<hidden>, "
            f"default_project={self.default_project!r}, read_only={self.read_only!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def with_read_only(self, read_only: bool) -> Settings:
        """Return a copy with ``read_only`` flipped (used by the MCP server)."""
        return replace(self, read_only=read_only)


# --------------------------------------------------------------------------- #
# env file parsing
# --------------------------------------------------------------------------- #


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a dict.

    Supports ``#`` comments (whole-line and trailing on unquoted values), an
    optional ``export`` prefix, and single or double quoting. Performs **no**
    shell expansion: ``$HOME`` and ``${X}`` stay literal, and backslashes are
    not escape sequences. Malformed lines are skipped rather than fatal, so a
    stray line in a hand-edited file does not lock the user out.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # Trailing comment is only a comment outside quotes.
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at].rstrip()
            elif value.startswith("#"):
                value = ""
        values[key] = value
    return values


def _warn_if_env_file_unsafe(path: Path) -> None:
    """Warn once on stderr when an env file is exposed to other users.

    Two checks: group/world permission bits, and (POSIX only) an owner other
    than the current user — someone else could then rewrite the credentials
    or the base URL. Both only warn: refusing would lock users out of a
    working setup over a fixable ``chmod``/``chown``.
    """
    try:
        info = path.stat()
    except OSError:
        return
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        _warn_once(
            f"mode:{path}",
            f"{path} is readable by other users (mode {mode:04o}); "
            f"run: chmod 600 {path}",
        )
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        _warn_once(
            f"owner:{path}",
            f"{path} is owned by uid {info.st_uid}, not by the current user "
            f"(uid {getuid()}); another account can change these credentials",
        )


def read_env_file(path: Path) -> dict[str, str]:
    """Read and parse an env file. A missing file yields ``{}``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read env file {path}: {exc}") from exc
    _warn_if_env_file_unsafe(path)
    return parse_env_text(text)


def resolve_env_file(
    env: Mapping[str, str], env_file: Path | None, config_dir: Path
) -> Path | None:
    """Pick the env file to load.

    Precedence: explicit argument, then ``$JIRA_CLI_ENV_FILE``, then
    ``<config_dir>/.env``, then the legacy ``~/.jira-cli/config.env`` written
    by the previous version of this tool. Only existing files are returned,
    except an explicit argument, which is returned so the caller gets a clear
    error if it is missing.
    """
    if env_file is not None:
        return Path(env_file).expanduser()
    pointed = (env.get("JIRA_CLI_ENV_FILE") or "").strip()
    if pointed:
        return Path(pointed).expanduser()
    for candidate in (config_dir / ".env", LEGACY_ENV_FILE):
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# value coercion
# --------------------------------------------------------------------------- #


def _as_bool(value: str | None, default: bool, name: str) -> bool:
    if value is None:
        return default
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (1/0, true/false), got {value!r}")


def _as_float(value: str | None, default: float, name: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        number = float(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if number <= 0:
        raise ConfigError(f"{name} must be greater than zero, got {value!r}")
    return number


def _as_verify_tls(value: str | None) -> bool | str:
    """``True``/``False``, or a path to a CA bundle when it looks like one."""
    if value is None or not value.strip():
        return True
    text = value.strip()
    lowered = text.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return text


def display_url(url: str) -> str:
    """Return ``url`` with any ``user:password@`` part removed, for display.

    Defensive: :func:`load_settings` already refuses a base URL that carries
    credentials, but a ``Settings`` can be built by hand, so every place that
    shows the base URL goes through this.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    netloc, slash, tail = rest.partition("/")
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    return f"{scheme}{sep}{netloc}{slash}{tail}"


def _check_base_url_userinfo(base_url: str) -> None:
    """Refuse a base URL that embeds a username or password.

    The message never repeats the URL, so the password is not echoed back.
    """
    _, sep, rest = base_url.partition("://")
    netloc = rest.partition("/")[0] if sep else ""
    if "@" in netloc:
        raise ConfigError(
            "JIRA_BASE_URL must not contain a username or password "
            f"(got {display_url(base_url)!r} with credentials removed). Put "
            "credentials in JIRA_PAT, or JIRA_USERNAME + JIRA_PASSWORD"
        )


def _check_base_url_scheme(base_url: str, allow_http: bool) -> None:
    """Reject non-HTTP(S) URLs, and plain ``http://`` unless explicitly allowed.

    Over ``http://`` the PAT or password travels in cleartext, so it needs a
    deliberate ``JIRA_ALLOW_HTTP=1`` and is then still announced on stderr.
    """
    lowered = base_url.lower()
    if lowered.startswith("https://"):
        return
    if not lowered.startswith("http://"):
        raise ConfigError(
            f"JIRA_BASE_URL must start with https://, got {display_url(base_url)!r}"
        )
    if not allow_http:
        raise ConfigError(
            f"JIRA_BASE_URL uses plain http:// ({display_url(base_url)!r}), which sends your "
            "credentials unencrypted. Use https://, or set JIRA_ALLOW_HTTP=1 "
            "to accept the risk"
        )
    _warn_once(
        "allow_http",
        f"JIRA_ALLOW_HTTP=1: credentials for {display_url(base_url)} are sent "
        "unencrypted over http://",
    )


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


class _Lookup:
    """Layered env lookup: process environment wins over the env file."""

    def __init__(self, process: Mapping[str, str], file_values: Mapping[str, str]):
        self._layers = (process, file_values)

    def get(self, *names: str) -> str | None:
        """First non-empty value for any of ``names``, searched name by name.

        Names are tried in order, and for each name the process environment is
        consulted before the env file. That makes a legacy alias in the process
        environment lose to the modern name in the file only if the modern name
        is also absent from the process environment — which is the behaviour
        the contract asks for ("read legacy only if the new name is absent").
        """
        for name in names:
            for layer in self._layers:
                value = layer.get(name)
                if value is not None and str(value).strip():
                    return str(value)
        return None

    def field_overrides(self) -> dict[str, str]:
        """Collect ``JIRA_FIELD_<NAME>=customfield_x`` from both layers."""
        overrides: dict[str, str] = {}
        for layer in reversed(self._layers):  # process env applied last == wins
            for key, value in layer.items():
                if not key.startswith(FIELD_ENV_PREFIX) or not str(value).strip():
                    continue
                name = key[len(FIELD_ENV_PREFIX) :].strip().upper()
                if name:
                    overrides[name] = str(value).strip()
        return overrides


def _describe_source(chosen_file: Path | None, config_dir: Path) -> str:
    """Say where settings were looked for, for use in a ConfigError message."""
    if chosen_file is not None and chosen_file.is_file():
        return f"the env file {chosen_file} or the process environment"
    if chosen_file is not None:
        return (
            f"the env file {chosen_file} (it does not exist; it was named by "
            "$JIRA_CLI_ENV_FILE) or the process environment"
        )
    return (
        "an env file (none found; looked for $JIRA_CLI_ENV_FILE, "
        f"{config_dir / '.env'}, {LEGACY_ENV_FILE}) or the process environment"
    )


def _resolve_auth(lookup: _Lookup, source: str) -> tuple[str, str, str]:
    """Return ``(auth_mode, username, secret)``.

    ``pat`` is chosen when ``JIRA_AUTH_MODE`` says so, or when no mode is set
    but a ``JIRA_PAT`` exists. A PAT is sent as a bearer token in the
    ``Authorization`` header; basic auth sends username + password.
    ``source`` names where settings were looked for, so a missing-credential
    error tells the user where to put them.
    """
    pat = lookup.get("JIRA_PAT")
    password = lookup.get("JIRA_PASSWORD", "JIRA_API_TOKEN")
    username = lookup.get("JIRA_USERNAME", "JIRA_EMAIL") or ""

    requested = (lookup.get("JIRA_AUTH_MODE") or "").strip().lower()
    if requested and requested not in AUTH_MODES:
        raise ConfigError(
            f"JIRA_AUTH_MODE must be one of {', '.join(AUTH_MODES)}, got {requested!r}"
        )
    if not requested and not (pat or username or password):
        raise ConfigError(
            "no Jira credentials found. Set JIRA_PAT (personal access token), "
            f"or JIRA_USERNAME and JIRA_PASSWORD, in {source}"
        )
    mode = requested or (AUTH_PAT if pat else AUTH_BASIC)

    if mode == AUTH_PAT:
        if not pat:
            raise ConfigError(
                "auth mode 'pat' needs JIRA_PAT; set it in the env file "
                "(or switch JIRA_AUTH_MODE to basic)"
            )
        return AUTH_PAT, username, pat

    if not username:
        raise ConfigError("auth mode 'basic' needs JIRA_USERNAME (or legacy JIRA_EMAIL)")
    if not password:
        raise ConfigError(
            "auth mode 'basic' needs JIRA_PASSWORD (or legacy JIRA_API_TOKEN)"
        )
    return AUTH_BASIC, username, password


def load_settings(
    env: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> Settings:
    """Build :class:`Settings` from the environment and an env file.

    Args:
        env: Process environment to read. Defaults to ``os.environ``.
        env_file: Explicit env file. When ``None`` the file is discovered via
            ``$JIRA_CLI_ENV_FILE``, ``<config_dir>/.env``, then the legacy
            ``~/.jira-cli/config.env``.

    Raises:
        ConfigError: Required settings are missing or a value is malformed.
    """
    process = dict(os.environ if env is None else env)

    config_dir_raw = (process.get("JIRA_CONFIG_DIR") or "").strip()
    probe_dir = Path(config_dir_raw).expanduser() if config_dir_raw else DEFAULT_CONFIG_DIR

    chosen_file = resolve_env_file(process, env_file, probe_dir)
    file_values: dict[str, str] = {}
    if chosen_file is not None:
        if env_file is not None and not chosen_file.is_file():
            raise ConfigError(f"env file not found: {chosen_file}")
        file_values = read_env_file(chosen_file)

    lookup = _Lookup(process, file_values)
    source = _describe_source(chosen_file, probe_dir)

    base_url = (lookup.get("JIRA_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        raise ConfigError(
            "JIRA_BASE_URL is not set. Set JIRA_BASE_URL "
            f"(e.g. JIRA_BASE_URL=https://jira.example.com) in {source}"
        )
    _check_base_url_userinfo(base_url)
    _check_base_url_scheme(
        base_url, _as_bool(lookup.get("JIRA_ALLOW_HTTP"), False, "JIRA_ALLOW_HTTP")
    )

    for name in _SECRET_ENV_NAMES:
        register_secret(lookup.get(name))

    auth_mode, username, secret = _resolve_auth(lookup, source)

    config_dir = (
        Path(lookup.get("JIRA_CONFIG_DIR") or "").expanduser()
        if lookup.get("JIRA_CONFIG_DIR")
        else DEFAULT_CONFIG_DIR
    )
    default_project = lookup.get("JIRA_DEFAULT_PROJECT", "JIRA_PROJECT")
    verify_tls = _as_verify_tls(lookup.get("JIRA_VERIFY_TLS"))
    if verify_tls is False:
        _warn_once(
            "verify_tls",
            "JIRA_VERIFY_TLS is off: TLS certificates are NOT verified, so the "
            "Jira server's identity is unchecked and credentials can be "
            "intercepted. Prefer JIRA_VERIFY_TLS=/path/to/ca-bundle.pem",
        )

    return Settings(
        base_url=base_url,
        auth_mode=auth_mode,
        username=username,
        secret=secret,
        default_project=default_project.strip().upper() if default_project else None,
        verify_tls=verify_tls,
        timeout=_as_float(lookup.get("JIRA_TIMEOUT"), DEFAULT_TIMEOUT, "JIRA_TIMEOUT"),
        read_only=_as_bool(lookup.get("JIRA_READ_ONLY"), False, "JIRA_READ_ONLY"),
        hours_per_day=_as_float(
            lookup.get("JIRA_HOURS_PER_DAY"), DEFAULT_HOURS_PER_DAY, "JIRA_HOURS_PER_DAY"
        ),
        days_per_week=_as_float(
            lookup.get("JIRA_DAYS_PER_WEEK"), DEFAULT_DAYS_PER_WEEK, "JIRA_DAYS_PER_WEEK"
        ),
        config_dir=config_dir,
        teams_file=config_dir / "teams.yaml",
        filters_file=config_dir / "filters.yaml",
        field_overrides=lookup.field_overrides(),
    )


def describe_settings(s: Settings, *, local: bool = False) -> dict:
    """Return a dict that is safe to print.

    The secret is reported as ``set`` or ``not set`` only; its length is a
    hint about the credential type and is not shown. Filesystem paths
    (``config_dir``, ``teams_file``, ``filters_file``) reveal the OS user name
    and home layout, so they are included only when ``local`` is true: pass it
    from a terminal command shown to the local user, never from the MCP
    server, whose output goes to a model.
    """
    described = {
        "base_url": display_url(s.base_url),
        "auth_mode": s.auth_mode,
        "username": s.username or "(resolved from /myself)",
        "secret": "set" if s.secret else "not set",
        "default_project": s.default_project or "(none)",
        "verify_tls": s.verify_tls,
        "timeout": s.timeout,
        "read_only": s.read_only,
        "hours_per_day": s.hours_per_day,
        "days_per_week": s.days_per_week,
        "field_overrides": dict(s.field_overrides),
    }
    if local:
        described["config_dir"] = str(s.config_dir)
        described["teams_file"] = str(s.teams_file)
        described["filters_file"] = str(s.filters_file)
    return described


__all__ = [
    "display_url",
    "AUTH_BASIC",
    "AUTH_MODES",
    "AUTH_PAT",
    "DEFAULT_CONFIG_DIR",
    "KNOWN_FIELD_KEYS",
    "LEGACY_ENV_FILE",
    "Settings",
    "config_dir",
    "describe_settings",
    "load_settings",
    "parse_env_text",
    "read_env_file",
    "resolve_env_file",
]
