#!/usr/bin/env bash
# Run the code's unit tests inside the sandbox. One simple command a reviewer can use without a prompt.
# Usage: run-tests.sh --code CODE_DIR --work WORK_DIR
set -euo pipefail

HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE=""
WORK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --code) CODE="${2:-}"; shift 2 ;;
    --work) WORK="${2:-}"; shift 2 ;;
    *) echo "error: unknown option $1" >&2; exit 2 ;;
  esac
done
[ -n "$CODE" ] && [ -n "$WORK" ] || { echo "usage: run-tests.sh --code CODE_DIR --work WORK_DIR" >&2; exit 2; }

exec bash "$HERE/run-isolated.sh" --code "$CODE" --work "$WORK" -- \
  sh -c 'cd "$CODE_SRC/.." && PYTHONPATH="$CODE_SRC" python3 -m unittest discover -s tests -p "test_*.py"'
