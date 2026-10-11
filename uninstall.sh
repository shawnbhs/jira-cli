#!/usr/bin/env bash
#
# uninstall.sh — remove the jira-cli-manager launchers.
#
# Removes ~/.local/bin/jira and ~/.local/bin/jira-mcp and nothing else.
# Your credential (~/.config/jira-cli/.env) and your local configuration
# (teams.yaml, filters.yaml) are NEVER touched: reinstalling must not cost you
# a token, and an uninstall script must not be able to delete one.
#
# The repository virtualenv is left in place too; delete it yourself if you
# want the disk space back. Both extra steps are printed below.
#
#   ./uninstall.sh
#   ./uninstall.sh --help
#
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_DIR="$REPO_DIR/.venv"
CONFIG_DIR="${JIRA_CONFIG_DIR:-$HOME/.config/jira-cli}"
ENV_FILE="$CONFIG_DIR/.env"
BIN_DIR="$HOME/.local/bin"
LAUNCHERS=("jira" "jira-mcp")

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
    "")        ;;
    -h|--help) usage; exit 0 ;;
    *)         die "unknown option: $1 (try --help)" ;;
esac

removed=0
for name in "${LAUNCHERS[@]}"; do
    path="$BIN_DIR/$name"
    if [ -e "$path" ] || [ -L "$path" ]; then
        rm -f -- "$path"
        say "removed $path"
        removed=1
    fi
done

[ "$removed" -eq 1 ] || say "no launchers found in $BIN_DIR — nothing to do"

say ""
say "Kept, on purpose:"
say "  $ENV_FILE         (your credential)"
say "  $CONFIG_DIR/      (teams.yaml, filters.yaml)"
say "  $VENV_DIR         (the repository virtualenv)"
say ""
say "To remove those too, do it deliberately:"
say "  rm -rf -- \"$VENV_DIR\""
say "  rm -rf -- \"$CONFIG_DIR\"      # deletes your token: revoke it in Jira first"
say ""
say "Revoking a PAT: avatar -> Profile -> Personal Access Tokens -> Revoke."
