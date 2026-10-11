#!/usr/bin/env bash
#
# install.sh — install jira-cli-manager for the current user.
#
# Does no sudo and writes nothing outside $HOME and this repository.
# Safe to run repeatedly: it upgrades the install and never overwrites an
# existing environment file.
#
#   ./install.sh                  install or upgrade
#   ./install.sh --import-legacy  also migrate ~/.jira-cli/config.env (1.x)
#   ./install.sh --uninstall      remove the launchers only (keeps .env)
#   ./install.sh --help
#
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_DIR="$REPO_DIR/.venv"
CONFIG_DIR="${JIRA_CONFIG_DIR:-$HOME/.config/jira-cli}"
ENV_FILE="$CONFIG_DIR/.env"
ENV_TEMPLATE="$REPO_DIR/.env.example"
BIN_DIR="$HOME/.local/bin"
LEGACY_CONFIG="$HOME/.jira-cli/config.env"
LAUNCHERS=("jira" "jira-mcp")

IMPORT_LEGACY=0
DO_UNINSTALL=0

say()  { printf '%s\n' "$*"; }
step() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --import-legacy) IMPORT_LEGACY=1 ;;
            --uninstall)     DO_UNINSTALL=1 ;;
            -h|--help)       usage; exit 0 ;;
            *)               die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done
}

# --- launcher removal ------------------------------------------------------
# Deliberately narrow: launchers only. The environment file holds the user's
# credential and is never touched by an uninstall.
remove_launchers() {
    local name path removed=0
    for name in "${LAUNCHERS[@]}"; do
        path="$BIN_DIR/$name"
        if [ -e "$path" ] || [ -L "$path" ]; then
            rm -f -- "$path"
            say "removed $path"
            removed=1
        fi
    done
    [ "$removed" -eq 1 ] || say "no launchers found in $BIN_DIR"
    say ""
    say "Kept, on purpose:"
    say "  $ENV_FILE         (your credential)"
    say "  $CONFIG_DIR/      (teams.yaml, filters.yaml)"
    say "  $VENV_DIR         (delete it by hand if you want the space back)"
}

# --- virtualenv ------------------------------------------------------------
ensure_venv() {
    if [ -x "$VENV_DIR/bin/python" ]; then
        step "using existing virtualenv at $VENV_DIR"
        return
    fi
    step "creating virtualenv at $VENV_DIR"
    local py
    py="$(command -v python3 || true)"
    [ -n "$py" ] || die "python3 not found on PATH"
    "$py" -m venv -- "$VENV_DIR"
    [ -x "$VENV_DIR/bin/python" ] || die "virtualenv creation failed: no $VENV_DIR/bin/python"
}

install_package() {
    step "installing jira-cli-manager into the virtualenv"
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/python" -m pip install --quiet --editable "$REPO_DIR"
}

# --- configuration ---------------------------------------------------------
ensure_config_dir() {
    step "preparing $CONFIG_DIR"
    mkdir -p -- "$CONFIG_DIR"
    chmod 700 -- "$CONFIG_DIR"
}

ensure_env_file() {
    if [ -e "$ENV_FILE" ]; then
        step "keeping existing $ENV_FILE (not overwritten)"
        chmod 600 -- "$ENV_FILE"
        return
    fi
    [ -f "$ENV_TEMPLATE" ] || die "template not found: $ENV_TEMPLATE"
    step "creating $ENV_FILE from .env.example"
    cp -- "$ENV_TEMPLATE" "$ENV_FILE"
    chmod 600 -- "$ENV_FILE"
}

# Migrate 1.x keys into the new env file. Values are copied through a Python
# helper that reads and writes files only — no value is ever echoed, logged or
# passed on a command line.
import_legacy() {
    if [ ! -f "$LEGACY_CONFIG" ]; then
        warn "--import-legacy: no legacy config at $LEGACY_CONFIG, nothing to import"
        return
    fi
    step "importing keys from the 1.x config (values are not printed)"
    LEGACY_CONFIG="$LEGACY_CONFIG" ENV_FILE="$ENV_FILE" \
        "$VENV_DIR/bin/python" - <<'PY'
import os
import re
from pathlib import Path

LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
# 1.x names on the left, 2.x names on the right.
ALIASES = {
    "JIRA_EMAIL": "JIRA_USERNAME",
    "JIRA_API_TOKEN": "JIRA_PASSWORD",
    "JIRA_PROJECT": "JIRA_DEFAULT_PROJECT",
}
KNOWN = {
    "JIRA_BASE_URL", "JIRA_AUTH_MODE", "JIRA_PAT", "JIRA_USERNAME", "JIRA_PASSWORD",
    "JIRA_DEFAULT_PROJECT", "JIRA_VERIFY_TLS", "JIRA_TIMEOUT", "JIRA_READ_ONLY",
    "JIRA_HOURS_PER_DAY", "JIRA_DAYS_PER_WEEK", "JIRA_CONFIG_DIR",
    "JIRA_FIELD_SPRINT", "JIRA_FIELD_EPIC_LINK", "JIRA_FIELD_EPIC_NAME",
    "JIRA_FIELD_STORY_POINTS",
}


def unquote(raw: str) -> str:
    raw = raw.split(" #", 1)[0].strip() if not raw.startswith(("'", '"')) else raw
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def parse(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            continue
        key, value = ALIASES.get(m.group(1), m.group(1)), unquote(m.group(2))
        if key in KNOWN and value:
            found.setdefault(key, value)
    return found


legacy_path = Path(os.environ["LEGACY_CONFIG"])
env_path = Path(os.environ["ENV_FILE"])
legacy = parse(legacy_path)
if not legacy:
    print("  no recognised keys in the legacy config")
    raise SystemExit(0)

existing = parse(env_path) if env_path.exists() else {}
lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

# A PAT in the legacy file was stored under the basic-auth name; if the import
# brings a password and no PAT, make the auth mode explicit rather than guessed.
added, skipped = [], []
for key, value in legacy.items():
    if key in existing:
        skipped.append(key)
        continue
    lines.append(f"{key}={value}")
    added.append(key)

if added and "JIRA_AUTH_MODE" not in existing and "JIRA_AUTH_MODE" not in added:
    mode = "pat" if "JIRA_PAT" in legacy else "basic"
    lines.append(f"JIRA_AUTH_MODE={mode}")
    added.append("JIRA_AUTH_MODE")

if added:
    header = f"\n# --- imported from {legacy_path} ---"
    body = lines[: len(lines) - len(added)] + [header] + lines[len(lines) - len(added) :]
    env_path.write_text("\n".join(body) + "\n", encoding="utf-8")
    env_path.chmod(0o600)

print(f"  imported keys: {', '.join(sorted(added)) or 'none'}")
if skipped:
    print(f"  already set, left alone: {', '.join(sorted(skipped))}")
print("  (values were not displayed)")
PY
    chmod 600 -- "$ENV_FILE"
}

# --- launchers -------------------------------------------------------------
write_launchers() {
    step "writing launchers into $BIN_DIR"
    mkdir -p -- "$BIN_DIR"
    local name target
    for name in "${LAUNCHERS[@]}"; do
        target="$VENV_DIR/bin/$name"
        [ -x "$target" ] || die "entry point missing after install: $target"
        cat > "$BIN_DIR/$name" <<EOF
#!/usr/bin/env bash
# Generated by jira-cli-manager install.sh. Safe to delete.
set -euo pipefail
exec "$target" "\$@"
EOF
        chmod 755 -- "$BIN_DIR/$name"
        say "  $BIN_DIR/$name -> $target"
    done
}

check_path() {
    case ":${PATH:-}:" in
        *":$BIN_DIR:"*) return 0 ;;
    esac
    warn "$BIN_DIR is not on your PATH, so 'jira' will not be found yet."
    warn "Add this to your shell profile (~/.bashrc or ~/.zshrc) and reopen the shell:"
    warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
}

main() {
    parse_args "$@"

    if [ "$DO_UNINSTALL" -eq 1 ]; then
        step "removing launchers"
        remove_launchers
        exit 0
    fi

    ensure_venv
    install_package

    # Everything created while the credential is being handled is private
    # from birth: no window where the env file exists with group/world read
    # before the chmod below lands. The caller's umask is restored afterwards
    # so the launchers keep their normal 755 mode.
    local saved_umask
    saved_umask="$(umask)"
    umask 077
    ensure_config_dir
    ensure_env_file
    if [ "$IMPORT_LEGACY" -eq 1 ]; then
        import_legacy
    fi
    umask "$saved_umask"

    write_launchers
    check_path

    say ""
    say "Installed. Next:"
    say "  1. edit $ENV_FILE  (JIRA_BASE_URL and a Personal Access Token)"
    say "  2. jira doctor"
    say ""
    say "Creating a PAT: avatar -> Profile -> Personal Access Tokens -> Create token."
    say "See README.md for the walkthrough and docs/COMMANDS.md for every command."
}

main "$@"
