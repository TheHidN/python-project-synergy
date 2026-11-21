#!/usr/bin/env bash
# Project Synergy uninstaller. It moves the `synergy` command aside and keeps all your data
# (configuration, registry, tokens, packets). It never removes files and never touches tmux.
set -euo pipefail

BIN_DIR="${SYNERGY_BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${SYNERGY_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/synergy}"
STATE_DIR="${SYNERGY_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/synergy}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
WRAPPER="$BIN_DIR/synergy"
MARK="# synergy-wrapper: created by project-synergy install.sh"
YES=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    -h|--help) echo "Usage: ./uninstall.sh [--yes]"; exit 0 ;;
    *) echo "error: unknown option $arg" >&2; exit 2 ;;
  esac
done

[ -e "$WRAPPER" ] || { echo "Nothing to do: $WRAPPER does not exist."; exit 0; }
grep -qF "$MARK" "$WRAPPER" 2>/dev/null || { echo "error: $WRAPPER was not created by this installer; leaving it alone." >&2; exit 1; }

echo "This will move $WRAPPER into a backup folder under $DATA_DIR."
echo "It keeps: $CONFIG_DIR and $STATE_DIR (your configuration, registry and tokens)."
if [ "$YES" -ne 1 ]; then
  if [ -t 0 ]; then
    read -r -p "Continue? [y/N] " answer
    [ "$answer" = "y" ] || { echo "Nothing was changed."; exit 1; }
  else
    echo "Nothing was changed. Run ./uninstall.sh --yes to confirm."
    exit 1
  fi
fi

BACKUP="$DATA_DIR/synergy-uninstalled-$(date +%Y%m%d%H%M%S)"
mkdir -p "$BACKUP"
mv "$WRAPPER" "$BACKUP/synergy"
echo "Moved the command to $BACKUP/synergy."
echo "kept: $CONFIG_DIR and $STATE_DIR (delete them yourself only if you are sure you no longer need them)."
echo "If a managed tmux session is running it was not touched."
