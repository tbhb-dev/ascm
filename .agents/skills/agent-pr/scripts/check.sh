#!/usr/bin/env bash
# The mechanical checks on PR_AGENTDESC.md that run before anything is
# published. It runs every check and reports every finding, so one pass
# shows the whole list.
#
# The checks: the repository's description lint, the closing log entry
# D0054 asks for, a Related section that lists only issue numbers and
# decision ids, and the repository's own title check.
#
# Usage: check.sh
# Exit: 0 with `description checks pass`, 1 with the findings printed.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=../../agent-commit/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../../agent-commit/scripts/lib.sh"
# shellcheck source-path=SCRIPTDIR source=draft.sh
. "$(dirname "${BASH_SOURCE[0]}")/draft.sh"

[ $# -eq 0 ] || as_die "usage: check.sh"

root=$(as_root)
cd "$root"
draft=$root/PR_AGENTDESC.md
[ -s "$draft" ] || as_die "PR_AGENTDESC.md is missing or empty; draft the description first"

failed=0
finding() {
  printf '%s\n' "$*"
  failed=1
}

# An empty command is no lint at all, and never a reason to skip it.
lint=$(as_conf lint_pr_cmd "mise run lint-pr-description")
if [ -z "$lint" ]; then
  finding "lint_pr_cmd is empty; the description lint has to run"
elif ! output=$(bash -c "$lint" 2>&1); then
  [ -z "$output" ] || printf '%s\n' "$output"
  finding "the description lint fails; fix the findings above"
fi

if [ "$(as_conf pr_log_entry required)" = required ]; then
  last=$(awk 'NF { line = $0 } END { print line }' "$draft")
  if ! printf '%s\n' "$last" | grep -E -q -- '^Log entry: \* \*\*[^*]+\*\*: .+$'; then
    finding "the description must end with a line of the form: Log entry: * **Label**: text"
  fi
fi

pattern=$(as_conf pr_related_pattern '^(#[0-9]+|D[0-9]{4})$')
if [ -n "$pattern" ]; then
  related=$(awk '
    /^## Related[[:space:]]*$/ { inside = 1; next }
    inside && (/^## / || /^Log entry:/) { exit }
    inside && NF { print }
  ' "$draft")
  count=$(printf '%s\n' "$related" | grep -c . || true)
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    item=$(pr_trim "$line")
    case "$item" in
    "* "* | "- "*) item=$(pr_trim "${item#??}") ;;
    esac
    case "$item" in
    None | None.)
      [ "$count" -eq 1 ] || finding "Related lists issue numbers and decision ids only, or None; None can't sit beside another item: \"$line\""
      continue
      ;;
    esac
    IFS=',' read -r -a parts <<<"$item"
    for part in "${parts[@]}"; do
      if ! printf '%s\n' "$(pr_trim "$part")" | grep -E -q -- "$pattern"; then
        finding "Related lists issue numbers and decision ids only, or None: \"$line\""
        break
      fi
    done
  done <<<"$related"
fi

title_cmd=$(as_conf pr_title_check_cmd)
if [ -n "$title_cmd" ]; then
  base=$(pr_field "$draft" base)
  if [ -z "$base" ]; then
    finding "the draft's frontmatter has no base:, so the title check can't run"
  elif ! output=$(AS_BASE=$(pr_base_ref "$base") AS_HEAD=HEAD AS_TITLE=$(pr_title "$draft") bash -c "$title_cmd" 2>&1); then
    [ -z "$output" ] || printf '%s\n' "$output"
    finding "the title check fails; fix the title above"
  fi
fi

[ "$failed" -eq 0 ] || exit 1
echo "description checks pass"
