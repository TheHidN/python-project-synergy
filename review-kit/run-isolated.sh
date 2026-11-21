#!/usr/bin/env bash
# Run one command inside a bubblewrap sandbox: read-only system, an empty home folder, no network, no
# session sockets, a scrubbed environment, the code folder read-only and ONE writable work folder.
# It fails closed: without bubblewrap it refuses to run anything.
#
# Usage: run-isolated.sh --code CODE_DIR --work WORK_DIR -- COMMAND [ARGS...]
set -euo pipefail

CODE=""
WORK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --code) CODE="${2:-}"; shift 2 ;;
    --work) WORK="${2:-}"; shift 2 ;;
    --) shift; break ;;
    *) echo "error: unknown option $1" >&2; exit 2 ;;
  esac
done
if [ -z "$CODE" ] || [ -z "$WORK" ] || [ $# -eq 0 ]; then
  echo "usage: run-isolated.sh --code CODE_DIR --work WORK_DIR -- COMMAND [ARGS...]" >&2
  exit 2
fi
KIT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REVIEW_ROOT="$(dirname "$KIT_DIR")"
inside() { # inside PATH ALLOWED_ROOT...: PATH must resolve to one of the roots or below it
  local resolved="$1"; shift
  local root
  for root in "$@"; do
    case "$resolved" in "$root"|"$root"/*) return 0 ;; esac
  done
  return 1
}
CODE_R="$(cd -P "$CODE" 2>/dev/null && pwd)" || { echo "error: the code folder $CODE does not exist." >&2; exit 2; }
WORK_R="$(cd -P "$WORK" 2>/dev/null && pwd)" || { echo "error: the work folder $WORK does not exist." >&2; exit 2; }
inside "$CODE_R" "$REVIEW_ROOT/code" "$REVIEW_ROOT/work" || { echo "error: the code folder is outside the review folder; refusing." >&2; exit 2; }
inside "$WORK_R" "$REVIEW_ROOT/work" || { echo "error: the work folder is outside the review folder's work area; refusing." >&2; exit 2; }
if ! command -v bwrap >/dev/null 2>&1; then
  echo "error: bubblewrap (bwrap) is not installed; refusing to run unsandboxed." >&2
  echo "On Ubuntu or Debian, an administrator can install it with: apt install bubblewrap" >&2
  exit 1
fi
CODE="$(cd -P "$CODE" && pwd)"
WORK="$(cd -P "$WORK" && pwd)"

exec bwrap \
  --ro-bind / / --dev /dev --proc /proc \
  --tmpfs /tmp --tmpfs /run --tmpfs "$HOME" \
  --ro-bind "$CODE" "$CODE" --bind "$WORK" "$WORK" \
  --remount-ro "$HOME" --remount-ro /run \
  --chdir "$WORK" \
  --unshare-all --die-with-parent --new-session --clearenv \
  --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv HOME "$WORK" --setenv TMPDIR /tmp \
  --setenv LANG C.UTF-8 --setenv PYTHONDONTWRITEBYTECODE 1 --setenv CODE_SRC "$CODE/src" \
  -- "$@"
