#!/usr/bin/env bash
# What an agent needs before it drafts a squash message and merges: the
# pull request's state, each check judged by its latest run, the
# readiness answer, and the briefing the message is
# written from. It never fails and it changes nothing but the
# remote-tracking refs it fetches to read the changed paths.
#
# Usage: preflight.sh <pr>
set -uo pipefail
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

section() { printf '\n== %s ==\n' "$1"; }

pr=${1:-}
case "$pr" in
"" | *[!0-9]*)
  echo "usage: preflight.sh <pr>. Pass the pull request number."
  exit 0
  ;;
esac

if ! command -v gh >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
  echo "MISSING: gh and jq have to be installed. merge.sh refuses without them."
  exit 0
fi
root=$(as_root 2>/dev/null) || {
  echo "MISSING: this isn't a git repository."
  exit 0
}
cd "$root" || exit 0
state=$(as_state_dir)

if ! json=$(gh pr view "$pr" --json number,title,body,state,isDraft,mergeStateStatus,baseRefName,headRefName,headRefOid,isCrossRepository,statusCheckRollup,commits,files 2>&1); then
  echo "MISSING: couldn't read #$pr: $json"
  exit 0
fi
field() { printf '%s' "$json" | jq -r --arg f "$1" '.[$f] | if . == null then "" else tostring end'; }
base=$(field baseRefName)
head_ref=$(field headRefName)
head=$(field headRefOid)

section "pull request"
echo "number: #$pr"
echo "title: $(field title)"
echo "state: $(field state)"
echo "draft: $(field isDraft)"
echo "base: $base"
echo "head branch: $head_ref"
echo "head oid: $head"
echo "mergeStateStatus: $(field mergeStateStatus)"
echo "from a fork: $(field isCrossRepository)"
echo "repository: $(as_repo_name)"

section "checks, each judged by its latest run"
checks=$state/merge.checks
printf '%s' "$json" | jq '.statusCheckRollup // []' >"$checks" 2>/dev/null
verdicts=$(am_judge_checks "$checks")
status=$?
if [ -z "$verdicts" ]; then
  echo "no checks reported"
else
  printf '%s\n' "$verdicts" | awk -F'\t' '{ printf "%-8s %s (%s)\n", $1, $2, $3 }'
fi
case "$status" in
0) echo "checks: every latest run passed" ;;
1) echo "checks: FAILING. merge.sh refuses; hand the pull request to fix-pr." ;;
2) echo "checks: still running. merge.sh refuses; wait through watch-pr." ;;
*) echo "checks: UNREADABLE. merge.sh refuses." ;;
esac
unlink "$checks"

section "changed paths"
paths=$state/merge.paths
owner=unknown
if am_changed_paths "$pr" "$base" "$head" "$paths"; then
  owner=no
  as_owner_path "$paths" && owner=yes
  echo "owner path touched: $owner"
  [ "$owner" = no ] || grep -E -- "$(as_conf owner_paths)" "$paths" | sed 's/^/  /'
else
  echo "UNREADABLE: the pull request's commits couldn't be fetched. merge.sh refuses."
fi
[ ! -f "$paths" ] || unlink "$paths"

section "patterns"
bad_patterns=$(am_bad_patterns)
if [ -z "$bad_patterns" ]; then
  echo "pattern settings: each one reads"
else
  echo "BROKEN: these settings are empty or aren't patterns grep can read, and merge.sh refuses: $(printf '%s' "$bad_patterns" | tr '\n' ' ')"
fi

section "reviewer approval"
reviewer=$(as_conf required_reviewer)
if [ -z "$reviewer" ]; then
  echo "required_reviewer: NOT CONFIGURED. merge.sh refuses on every pull request."
elif verdict=$(am_reviewer_verdict "$pr" "$head" "$reviewer"); then
  if [ "$verdict" = ok ]; then echo "$reviewer: approved the current head"; else echo "$reviewer: NOT APPROVED, $verdict. merge.sh refuses."; fi
else
  echo "$reviewer: UNREADABLE, the reviews couldn't be read. merge.sh refuses."
fi

section "readiness"
ready=$(am_ready_cmd)
if [ -z "$ready" ]; then
  echo "merge_ready_cmd: not configured; merge.sh adds no readiness check beyond the required checks and a CLEAN merge state"
else
  if "$ready" "$root" "$pr" "$head"; then
    echo "merge_ready_cmd: ready"
  else
    echo "merge_ready_cmd: NOT READY (exit $?). merge.sh refuses."
  fi
fi

section "preconditions"
if git check-ignore -q SQUASH_AGENTMSG; then echo "SQUASH_AGENTMSG is ignored: ok"; else echo "MISSING: SQUASH_AGENTMSG isn't in .gitignore. Report it; don't edit .gitignore from this skill."; fi
criteria=$(as_path "$(as_conf review_squash_criteria .claude/skills/review-squash-message/SKILL.md)")
if [ -f "$criteria" ]; then echo "review criteria: ok"; else echo "MISSING: review criteria at $criteria"; fi
echo "reviewer: $(as_conf review_cmd "claude -p, model $(as_conf review_model UNSET), effort $(as_conf review_effort UNSET)")"
echo "review rounds allowed: $(as_conf review_max_rounds 3)"
if [ -n "$(as_conf lint_squash_cmd "mise run lint-squash-msg")" ]; then echo "lint: $(as_conf lint_squash_cmd "mise run lint-squash-msg")"; else echo "MISSING: lint_squash_cmd is empty; review.sh and merge.sh refuse"; fi
if [ -s SQUASH_AGENTMSG ]; then
  echo "a draft is already here, for #$(am_draft_number SQUASH_AGENTMSG | grep . || echo unknown); read it before overwriting it"
fi

section "briefing: write SQUASH_AGENTMSG from this"
echo "subject: $(field title) (#$pr)"
echo
echo "description as published:"
printf '%s' "$json" | jq -r '.body // "(none)"'
echo
echo "commits being collapsed, in full:"
printf '%s' "$json" | jq -r '.commits[]? | "--- " + .oid + "\n" + .messageHeadline + (if (.messageBody // "") == "" then "" else "\n\n" + .messageBody end) + "\n"'
echo "diffstat:"
printf '%s' "$json" | jq -r '.files[]? | "\(.additions)+ \(.deletions)- \(.path)"'

section "trailers"
echo "Assisted-by: <harness>:<model> <tool>   (before Signed-off-by)"
ident=$(git var GIT_COMMITTER_IDENT 2>/dev/null | sed -E 's/ [0-9]+ [-+][0-9]{4}$//' || true)
echo "Signed-off-by: ${ident:-UNSET: git has no committer identity here}"
exit 0
