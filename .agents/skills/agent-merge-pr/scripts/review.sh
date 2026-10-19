#!/usr/bin/env bash
# Run the independent review of SQUASH_AGENTMSG against the pull
# request's commits, and stamp the draft when it passes.
#
# The script lints the draft first, then takes one snapshot: a copy of
# the draft, and the pull request's head oid, read once. The title, the
# description, every commit message, and the diffstat come from the one
# response that named that oid, and go to files in a directory of this
# run's own. The stamp binds the copy's bytes, the pull request number,
# and the head oid. Before stamping, the script re-reads the head oid and
# confirms it hasn't moved.
#
# The reviewer is a fresh process with no shell. Past the round bound
# the review stays blocked; `--unblock` clears it, on the planner's word
# alone.
#
# Usage: review.sh <pr> [--unblock]
# Exit: 0 stamped, 2 changes required, 3 blocked at the round bound,
#       4 no readable verdict, 5 the draft or the head changed during
#       the review.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

usage="usage: review.sh <pr> [--unblock]"
pr=""
unblock=no
for arg in "$@"; do
  case "$arg" in
  --unblock) unblock=yes ;;
  -*) as_die "$usage" ;;
  *)
    [ -z "$pr" ] || as_die "$usage"
    pr=$arg
    ;;
  esac
done
if [ "$unblock" = yes ]; then
  as_unblock squash
  exit 0
fi
case "$pr" in
"" | *[!0-9]*) as_die "$usage" ;;
esac

am_need_tools
root=$(as_root)
cd "$root"
draft=$root/SQUASH_AGENTMSG
[ -s "$draft" ] || as_die "SQUASH_AGENTMSG is missing or empty; draft the message first"
[ "$(am_draft_number "$draft")" = "$pr" ] || as_die "the subject of SQUASH_AGENTMSG doesn't end in (#$pr)"

lint=$(as_conf lint_squash_cmd "mise run lint-squash-msg")
[ -n "$lint" ] || as_die "lint_squash_cmd is empty; the commit-msg gates have to run"
state=$(as_state_dir)
if ! bash -c "$lint" >"$state/merge.lint" 2>&1; then
  cat "$state/merge.lint" >&2
  as_die "the commit-msg gates fail on SQUASH_AGENTMSG; fix the message and review it again"
fi

# The snapshot. Everything the reviewer reads derives from this one
# response, which names the head oid it describes.
json=$(gh pr view "$pr" --json title,body,state,isDraft,baseRefName,headRefName,headRefOid,commits,files) ||
  as_die "couldn't read #$pr"
field() { printf '%s' "$json" | jq -r --arg f "$1" '.[$f] | if . == null then "" else tostring end'; }
[ "$(field state)" = OPEN ] || as_die "#$pr is $(field state), not OPEN"
head=$(field headRefOid)
[ -n "$head" ] || as_die "#$pr names no head oid"

inputs=$(as_new_inputs squash)
snapshot=$inputs/SQUASH_AGENTMSG
cp "$draft" "$snapshot"
{
  echo "number: #$pr"
  echo "title: $(field title)"
  echo "base: $(field baseRefName)"
  echo "head branch: $(field headRefName)"
  echo "head oid: $head"
  echo "draft: $(field isDraft)"
} >"$inputs/pull-request.txt"
printf '%s' "$json" | jq -r '.body // ""' >"$inputs/description.md"
printf '%s' "$json" | jq -r '.files[]? | "\(.additions)+ \(.deletions)- \(.path)"' >"$inputs/diffstat.txt"

# The change itself, from git at the recorded head, so the reviewer can
# check the message against what the pull request does. The commits and
# the diff run from the merge base with the base branch to that head,
# whatever the checkout has out, and the diff is text whatever the
# attributes say.
base=$(field baseRefName)
git fetch --quiet origin "+refs/heads/$base:refs/remotes/origin/$base" "refs/pull/$pr/head" >/dev/null 2>&1 ||
  as_die "couldn't fetch $base and the head of #$pr"
git rev-parse --verify --quiet "$head^{commit}" >/dev/null || as_die "the head $head of #$pr isn't on origin"
base_oid=$(git rev-parse --verify --quiet "refs/remotes/origin/$base") || as_die "couldn't read $base"
mergebase=$(git merge-base "$base_oid" "$head") || as_die "no merge base between $base and $head"
git log --reverse --format='--- %H%n%B' "$mergebase..$head" >"$inputs/commits.txt"
git -c core.quotePath=false diff --no-ext-diff --no-textconv --no-color --no-renames --name-status "$mergebase" "$head" >"$inputs/changed-paths.txt"
git diff --text --no-ext-diff --no-textconv --no-color --no-renames "$mergebase" "$head" >"$inputs/change.diff"
if [ -s "$inputs/changed-paths.txt" ] && [ ! -s "$inputs/change.diff" ]; then
  as_die "the change of #$pr names paths, but its diff is empty; something is hiding the content"
fi

still_as_gathered() {
  [ "$(am_live "$pr" headRefOid)" = "$head" ] &&
    [ "$(am_live "$pr" baseRefName)" = "$base" ] &&
    [ "$(git ls-remote origin "refs/heads/$base" | cut -f1)" = "$base_oid" ]
}

criteria=$(as_path "$(as_conf review_squash_criteria .claude/skills/review-squash-message/SKILL.md)")
as_review squash "$draft" "$snapshot" "$criteria" "$root" "$inputs" still_as_gathered \
  pr "$pr" head "$head" base "$base" base_oid "$base_oid"
