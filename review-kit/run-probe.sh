#!/usr/bin/env bash
# Run a probe script (a Python file inside the work folder) in the sandbox. The code is importable through
# the CODE_SRC environment variable; only the work folder is writable; there is no network and no access to
# your home folder, registry or tokens.
# Usage: run-probe.sh --code CODE_DIR --work WORK_DIR SCRIPT   (SCRIPT is relative to the work folder)
set -euo pipefail

HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE=""
WORK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --code) CODE="${2:-}"; shift 2 ;;
    --work) WORK="${2:-}"; shift 2 ;;
    --) shift; break ;;
    -*) echo "error: unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
SCRIPT="${1:-}"
[ -n "$CODE" ] && [ -n "$WORK" ] && [ -n "$SCRIPT" ] || { echo "usage: run-probe.sh --code CODE_DIR --work WORK_DIR SCRIPT" >&2; exit 2; }
case "$SCRIPT" in
  /*|*..*) echo "error: the script must be a plain relative path inside the work folder." >&2; exit 2 ;;
esac
WORK_ABS="$(cd -P "$WORK" && pwd)"
[ -f "$WORK_ABS/$SCRIPT" ] || { echo "error: $SCRIPT was not found in the work folder." >&2; exit 2; }

exec bash "$HERE/run-isolated.sh" --code "$CODE" --work "$WORK_ABS" -- python3 -I "$SCRIPT" "$WORK_ABS"
