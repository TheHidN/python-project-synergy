#!/usr/bin/env bash
# Install the repository's git hooks into the current clone.
# Copies the enforcement hooks into .git/hooks/. Run from anywhere inside the repo.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A Linux-only repo (its build or tests can't run on macOS) has no local
# build/lint/test gate: CI is its gate. The commit hooks still install, and a
# gate installed earlier is removed; a pre-push hook that isn't ours is left alone.
hooks="pre-commit commit-msg pre-push"
if [ -f "$ROOT/.github/linux-only" ]; then
  hooks="pre-commit commit-msg"
  if grep -qs '^# Git pre-push hook (enforcement)' "$ROOT/.git/hooks/pre-push"; then
    rm -f "$ROOT/.git/hooks/pre-push"
  fi
  echo "Skipped the pre-push gate: .github/linux-only marks this repo as Linux-only (CI is its gate)."
fi
for h in $hooks; do
  cp "$SRC/$h" "$ROOT/.git/hooks/$h"
  chmod +x "$ROOT/.git/hooks/$h"
done

echo "Installed git hooks -> $ROOT/.git/hooks/ (${hooks// /, })"
