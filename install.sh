#!/usr/bin/env bash
# Project Synergy installer. Safe to run again. It never starts Claude, never edits your Claude
# settings, never overwrites an existing file of yours, and needs no network.
set -euo pipefail

NO_TMUX=0
COMMAND_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-tmux) NO_TMUX=1 ;;
    --command-only) COMMAND_ONLY=1 ;;
    -h|--help)
      echo "Usage: ./install.sh [--no-tmux] [--command-only]"
      echo "  --no-tmux       do not create the managed tmux session (you can run: synergy tmux ensure)"
      echo "  --command-only  only install the synergy command for an existing setup; no config, rules, registry or tmux changes"
      exit 0 ;;
    *) echo "error: unknown option $arg (try --help)" >&2; exit 2 ;;
  esac
done

REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${SYNERGY_PYTHON:-python3}"
BIN_DIR="${SYNERGY_BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${SYNERGY_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/synergy}"
STATE_DIR="${SYNERGY_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/synergy}"
OS_RELEASE="${SYNERGY_OS_RELEASE:-/etc/os-release}"
WRAPPER="$BIN_DIR/synergy"
MARK="# synergy-wrapper: created by project-synergy install.sh"

say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*"; }
die()  { printf 'error: %s\nNothing was installed or changed.\n' "$*" >&2; exit 1; }

say "Project Synergy installer"

# 1. Operating system
[ "$(uname -s)" = "Linux" ] || die "Project Synergy runs on Linux (this is $(uname -s))."
OS_ID=""
if [ -r "$OS_RELEASE" ]; then OS_ID="$(sed -n 's/^ID=//p' "$OS_RELEASE" | tr -d '"' | head -1)"; fi
case "$OS_ID" in
  ubuntu|debian) say "ok: $OS_ID (supported)" ;;
  *) warn "${OS_ID:-this Linux} is untested. Ubuntu and Debian are the tested systems; it may still work." ;;
esac

# 2. Python 3.11 or later (standard library only)
command -v "$PYTHON" >/dev/null 2>&1 || die "Python 3 was not found. Install Python 3.11 or later (on Ubuntu or Debian: apt install python3, as an administrator)."
PYVER="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')" || die "could not run $PYTHON."
PYMAJ="${PYVER%%.*}"; PYREST="${PYVER#*.}"; PYMIN="${PYREST%%.*}"
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]; }; then
  die "Python 3.11 or later is needed; found $PYVER."
fi
"$PYTHON" -c 'import sqlite3, tomllib' 2>/dev/null || die "this Python lacks sqlite3 or tomllib; use a standard Python 3.11 or later."
say "ok: Python $PYVER"

# 3. Other tools (warnings only)
command -v git >/dev/null 2>&1 && say "ok: git" || warn "git was not found; install it to use worktrees (on Ubuntu or Debian: apt install git, as an administrator)."
HAVE_TMUX=0
command -v tmux >/dev/null 2>&1 && { HAVE_TMUX=1; say "ok: tmux"; } || warn "tmux was not found; install it for managed workspaces (on Ubuntu or Debian: apt install tmux, as an administrator)."
command -v claude >/dev/null 2>&1 && say "ok: the claude command was found" || warn "The claude command was not found. Install Anthropic's Claude command-line tool and sign in yourself; Project Synergy never installs it or signs in for you."

# 4. Refuse before changing anything if the command name is taken by something else
if [ -e "$WRAPPER" ] && ! grep -qF "$MARK" "$WRAPPER" 2>/dev/null; then
  die "$WRAPPER exists and was not created by this installer; move it aside or set SYNERGY_BIN_DIR."
fi

if [ "$COMMAND_ONLY" -eq 1 ] && [ ! -e "$CONFIG_DIR/config.toml" ]; then
  die "--command-only needs an existing setup ($CONFIG_DIR/config.toml was not found); run ./install.sh without it first."
fi

# 5. Folders (private) and the command
mkdir -p -m 700 "$CONFIG_DIR" "$STATE_DIR"
mkdir -p "$BIN_DIR"
PYABS="$(command -v "$PYTHON")"
WANT="#!/usr/bin/env bash
$MARK
exec env PYTHONPATH=\"$REPO/src\${PYTHONPATH:+:\$PYTHONPATH}\" PYTHONDONTWRITEBYTECODE=1 \"$PYABS\" -m project_synergy \"\$@\""
if [ -e "$WRAPPER" ] && [ "$(cat "$WRAPPER")" = "$WANT" ]; then
  say "command: already installed at $WRAPPER"
else
  printf '%s\n' "$WANT" > "$WRAPPER.new.$$"
  chmod 755 "$WRAPPER.new.$$"
  mv -f "$WRAPPER.new.$$" "$WRAPPER"
  say "command: installed $WRAPPER"
fi

if [ "$COMMAND_ONLY" -eq 1 ]; then
  say ""
  say "Done (command only). Your configuration, rules and registry were not touched."
  say "Open a new terminal (or run: hash -r) and try:  synergy status"
  exit 0
fi

# 6. Operating rules
RULES="$CONFIG_DIR/rules/synergy-rules.md"
mkdir -p -m 700 "$CONFIG_DIR/rules"
if [ -e "$RULES" ]; then say "rules: already installed (not changed)"; else cp "$REPO/rules/synergy-rules.md" "$RULES"; chmod 644 "$RULES"; say "rules: installed $RULES"; fi

# 7. Configuration (never overwritten)
if [ -e "$CONFIG_DIR/config.toml" ]; then
  say "config: $CONFIG_DIR/config.toml already exists (not changed)"
else
  cp "$REPO/config.example.toml" "$CONFIG_DIR/config.toml"
  chmod 600 "$CONFIG_DIR/config.toml"
  printf '\n[onboarding]\nrules_files = ["%s"]\n' "$RULES" >> "$CONFIG_DIR/config.toml"
  say "config: created $CONFIG_DIR/config.toml"
fi

# 8. Registry, the first operators and their tokens (automatic; no token is shown)
"$WRAPPER" init --rules-file "$RULES"

# 9. Managed tmux workspace
if [ "$NO_TMUX" -eq 1 ]; then
  say "tmux: skipped (--no-tmux). Create it later with: synergy tmux ensure"
elif [ "$HAVE_TMUX" -eq 1 ]; then
  "$WRAPPER" tmux ensure
else
  warn "tmux: skipped because tmux is not installed."
fi

# 10. What to do next
say ""
say "Done. Your Claude settings were not changed."
say "Next steps:"
say "  1. Make sure $BIN_DIR is on your PATH, then run:  synergy status"
say "  2. Start your first operator with its launcher (it starts the claude command as that operator):"
for launcher in "$STATE_DIR"/launchers/*.sh; do [ -e "$launcher" ] && say "       bash $launcher"; done
say "  3. Each operator reads its packet and acknowledges it; check with:  synergy onboard --status OPS1"
say "  4. Add another operator any time:  synergy onboard OPS3"
say "Tokens are created and used automatically; you never copy or paste one. They stop mistakes between operators,"
say "not a hostile program running as your own Linux user (see docs/tokens.md)."
