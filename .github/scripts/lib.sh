#!/usr/bin/env bash
# Shared helpers for the repository git hooks.
# Works with BSD (macOS) and GNU tooling. Emoji detection uses perl (BSD grep
# lacks -P); text-pattern detection uses grep -E.

GOV_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOV_PATTERNS_FILE="${GOV_PATTERNS_FILE:-$GOV_SCRIPTS_DIR/text-hygiene-terms.txt}"
# Patterns a repo may use in its Markdown docs (one per line, copied exactly
# from the patterns file). Laid down only for ecosystems whose docs must name a
# product the list blocks, such as a tool whose documentation names the CLI it depends on.
# Found from the scripts dir (.github/scripts), so the cwd does not matter.
GOV_DOCS_ALLOW_FILE="${GOV_DOCS_ALLOW_FILE:-$GOV_SCRIPTS_DIR/../docs-allowed-terms.txt}"

# Build one extended-regex alternation from the patterns file. With a path to a
# Markdown doc, the patterns in the docs allow file are left out.
gov_text_regex() {
  case "${1:-}" in
    *.md)
      if [ -f "$GOV_DOCS_ALLOW_FILE" ]; then
        grep -vE '^[[:space:]]*(#|$)' "$GOV_PATTERNS_FILE" 2>/dev/null \
          | grep -vxF -f <(grep -vE '^[[:space:]]*(#|$)' "$GOV_DOCS_ALLOW_FILE") | paste -sd '|' -
        return
      fi ;;
  esac
  grep -vE '^[[:space:]]*(#|$)' "$GOV_PATTERNS_FILE" 2>/dev/null | paste -sd '|' -
}

# Read stdin; print offending lines for blocked phrases (empty if none).
# Commit messages and PR text come through here and always get the full list.
gov_find_text_issues() {
  local re
  re="$(gov_text_regex)"
  [ -z "$re" ] && return 0
  grep -inE "$re" || true
}

# gov_find_file_issues <path>: the same for a file in the repo, given by its path
# from the root, so a Markdown doc gets the repo's docs allowances.
gov_find_file_issues() {
  local re
  re="$(gov_text_regex "$1")"
  [ -z "$re" ] && return 0
  grep -inE -- "$re" "$1" || true
}

# Read stdin; print offending lines containing emoji/pictographs (empty if none).
gov_find_emoji() {
  perl -CSD -ne 'print "$.: $_" if /[\x{1F000}-\x{1FAFF}\x{2600}-\x{27BF}\x{2B00}-\x{2BFF}\x{FE00}-\x{FE0F}\x{1F1E6}-\x{1F1FF}]/'
}
