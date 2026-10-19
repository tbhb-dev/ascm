#!/usr/bin/env bash
# The one sanctioned way for an agent to squash merge a pull request. It
# takes the pull request and the head oid the review stamped, and no
# message: the message is SQUASH_AGENTMSG, exactly as the independent
# review stamped it.
#
# In order, it refuses unless: the stamp covers the draft's bytes, the
# pull request, and the head oid, and the live head is that oid; the pull
# request is open and not a draft; its merge state is CLEAN; every check,
# judged by its latest run, succeeded; any readiness command
# says yes; no golden file or loosened gate is among the changes; and the
# commit-msg gates pass.
# It checks the stamp a second time after those commands have run, then
# moves the base of any open pull request that sits on this one's branch, and
# merges from its own copy of the reviewed message, pinned to the
# reviewed head. Afterward it confirms that the merge commit's message is
# the reviewed one, which it can't undo if it isn't, and tidies up.
#
# Usage: merge.sh <pr> <expected-head>
# Exit: 0 merged, 1 refused or failed.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

usage="usage: merge.sh <pr> <expected-head>"
[ $# -eq 2 ] || as_die "$usage"
pr=$1
expected=$2
case "$pr" in
"" | *[!0-9]*) as_die "$usage" ;;
esac
case "$expected" in
"" | *[!0-9a-f]*) as_die "$usage. The head has to be the full oid." ;;
esac
[ "${#expected}" -ge 40 ] || as_die "$usage. The head has to be the full oid."

am_need_tools
root=$(as_root)
cd "$root"
draft=$root/SQUASH_AGENTMSG
state=$(as_state_dir)
repo=$(as_repo_name)

# --- gate 1: the stamp, the draft, the pull request, and the head ------

[ -s "$draft" ] || as_die "SQUASH_AGENTMSG is missing or empty; draft the message first"
subject=$(head -n 1 "$draft")
[ "$(am_draft_number "$draft")" = "$pr" ] || as_die "the subject of SQUASH_AGENTMSG doesn't end in (#$pr)"

# Before the script reads the pull request, every pattern the gates and
# the cleanup use has to be one grep reads, or a check that matches
# nothing would pass.
bad_patterns=$(am_bad_patterns)
[ -z "$bad_patterns" ] || as_die "these settings are empty or aren't patterns grep can read: $(printf '%s' "$bad_patterns" | tr '\n' ' '); nothing was merged"

# The stamp binds the base too: retargeting a stacked pull request to a
# lower base keeps its head and adds commits to the squash, and a base
# that moved changes what the squash lands on.
require_reviewed() {
  local live live_base live_base_oid
  live_base=$(am_live "$pr" baseRefName) || as_die "couldn't read #$pr"
  live_base_oid=$(git ls-remote origin "refs/heads/$live_base" | cut -f1)
  [ -n "$live_base_oid" ] || as_die "couldn't read $live_base from origin"
  as_require_stamp squash "$draft" pr "$pr" head "$expected" base "$live_base" base_oid "$live_base_oid"
  live=$(am_live "$pr" headRefOid) || as_die "couldn't read #$pr"
  [ "$live" = "$expected" ] || as_die "the head of #$pr is $live, not the reviewed $expected; review it again"
}
require_reviewed

# gh acts as whoever is signed in, and a merge can't be undone, so the
# signed-in login has to be the configured agent account before
# anything else reads or writes for this merge.
# The agent account defaults to tbhb-agent. Only the environment or the
# machine's file can change it, never the repository's settings file.
want_login=$(as_conf gh_login tbhb-agent)
[ -n "$want_login" ] || as_die "gh_login isn't configured; it is empty in the environment or the machine's settings"
login=$(gh api user --jq .login 2>/dev/null) || as_die "gh couldn't say which account it is signed in as"
[ "$login" = "$want_login" ] || as_die "gh is signed in as ${login:-nobody}, not $want_login; nothing was merged"

# --- gate 2: open, and not a draft -------------------------------------

live_state=$(am_live "$pr" state) || as_die "couldn't read #$pr"
[ "$live_state" = OPEN ] || as_die "#$pr is $live_state, not OPEN"
live_draft=$(am_live "$pr" isDraft) || as_die "couldn't read #$pr"
[ "$live_draft" = false ] || as_die "#$pr is a draft; mark it ready first"

# The owner-review label asks for the owner's review by name.
owner_label=$(as_conf owner_review_label owner-review)
[ -n "$owner_label" ] || as_die "owner_review_label is empty; the label that sends a pull request to the owner has to be named"
gh pr view "$pr" --json labels >"$state/merge.labels" || as_die "couldn't read the labels of #$pr"
label_names=$(jq -r '.labels | if type == "array" then .[].name else error("no labels") end' "$state/merge.labels") ||
  as_die "couldn't read the labels of #$pr"
if printf '%s\n' "$label_names" | grep -q -x -F -- "$owner_label"; then
  as_mail blocker "$pr" "needs-owner: #$pr in $repo has the $owner_label label, so the owner reviews and merges it. Nothing was merged."
  as_die "#$pr has the $owner_label label; the owner reviews and merges it. Nothing was merged."
fi

# --- gate 2b: the required reviewer approved the current head ----------

reviewer=$(as_conf required_reviewer)
[ -n "$reviewer" ] || as_die "required_reviewer is empty; the account whose approval a merge needs has to be named"
verdict=$(am_reviewer_verdict "$pr" "$expected" "$reviewer") || as_die "couldn't read the reviews of #$pr"
[ "$verdict" = ok ] || as_die "#$pr lacks an approval from $reviewer on its head: $verdict. Nothing was merged."

# --- gate 3: the merge state -------------------------------------------

merge_state=$(am_live "$pr" mergeStateStatus) || as_die "couldn't read #$pr"
[ "$merge_state" = CLEAN ] || as_die "the merge state of #$pr is ${merge_state:-unknown}, not CLEAN. BEHIND or DIRTY goes to the rebase skill, BLOCKED to whoever holds the requirement."

# --- gate 4: every check, by its latest run ----------------------------

checks=$state/merge.checks
gh pr view "$pr" --json statusCheckRollup >"$checks" || as_die "couldn't read the checks of #$pr"
checks_status=0
verdicts=$(am_judge_checks "$checks") || checks_status=$?
case "$checks_status" in
0) ;;
1)
  printf '%s\n' "$verdicts" | grep -v '^ok' | awk -F'\t' '{ printf "%-8s %s (%s)\n", $1, $2, $3 }' >&2
  as_die "#$pr has a failing check, judged by its latest run. Hand it to fix-pr."
  ;;
2)
  printf '%s\n' "$verdicts" | grep -v '^ok' | awk -F'\t' '{ printf "%-8s %s (%s)\n", $1, $2, $3 }' >&2
  as_die "#$pr has a check that hasn't finished. Wait through watch-pr."
  ;;
4) as_die "#$pr has no checks at all, so nothing shows it passing" ;;
*) as_die "the checks of #$pr couldn't be read" ;;
esac
# Every check the base branch requires has to be among them, with a
# passing latest run. A check that never ran is no evidence.
live_base=$(am_live "$pr" baseRefName) || as_die "couldn't read #$pr"
git fetch --quiet origin "+refs/heads/$live_base:refs/remotes/origin/$live_base" >/dev/null 2>&1 || as_die "couldn't fetch $live_base"
live_base_oid=$(git rev-parse --verify --quiet "refs/remotes/origin/$live_base") || as_die "couldn't read $live_base"
default_branch=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) || as_die "couldn't read the default branch"
required=$(am_required_checks "$live_base" "$live_base_oid" "$default_branch") || as_die "couldn't read the checks $live_base requires"
[ -n "$required" ] || as_die "no required checks are known for $live_base: neither its rulesets on GitHub nor the ruleset files at $live_base list one"
printf '%s\n' "$verdicts" >"$state/merge.verdicts"
printf '%s\n' "$required" >"$state/merge.required"
unmet=$(am_unmet_checks "$state/merge.verdicts" "$state/merge.required")
[ -z "$unmet" ] || as_die "#$pr lacks a passing latest run of the required checks: $(printf '%s' "$unmet" | tr '\n' ' ')"

# --- gate 5: the readiness command -------------------------------------

ready=$(am_ready_cmd)
# An empty merge_ready_cmd adds no check beyond the required checks and the
# CLEAN merge state above.
if [ -n "$ready" ]; then
  [ -x "$ready" ] || as_die "merge_ready_cmd names $ready, which isn't executable"
  "$ready" "$root" "$pr" "$expected" || as_die "the readiness command says #$pr isn't ready to merge"
fi

# --- gate 6: golden files, loosened gates, and a note on owner paths ----

base=$(am_live "$pr" baseRefName) || as_die "couldn't read #$pr"
head_ref=$(am_live "$pr" headRefName) || as_die "couldn't read #$pr"
paths=$state/merge.paths
am_changed_paths "$pr" "$base" "$expected" "$paths" ||
  as_die "couldn't read the paths #$pr changes; the owner-path check can't run without them"

# A golden file changes only on the owner's review of its baseline diff,
# which no agent merge replaces.
golden=$(as_conf golden_paths)
[ -n "$golden" ] || as_die "golden_paths is empty; the paths of golden files have to be named"
golden_status=0
golden_hits=$(grep -E -- "$golden" "$paths" 2>/dev/null) || golden_status=$?
case "$golden_status" in
0)
  as_mail blocker "$pr" "needs-owner: #$pr in $repo changes golden files ($(printf '%s' "$golden_hits" | tr '\n' ' ')), so the owner reviews and merges it. Nothing was merged."
  as_die "#$pr changes golden files: $(printf '%s' "$golden_hits" | tr '\n' ' '). The owner reviews and merges it. Nothing was merged."
  ;;
1) ;;
*) as_die "golden_paths isn't a pattern grep can read: $golden" ;;
esac

# A change that loosens a gate goes to the owner. This catches what a script can see in the diff; the author
# and the reviewers flag the rest.
loosening=$state/merge.loosening
am_loosening "$base" "$expected" "$loosening" || as_die "couldn't read the diff of #$pr for gate changes, or loosening_markers or gate_config_paths is empty"
if [ -s "$loosening" ]; then
  cat "$loosening" >&2
  as_mail blocker "$pr" "needs-owner: #$pr in $repo loosens a gate, so the owner reviews and merges it. Nothing was merged. $(tr '\n' ' ' <"$loosening")"
  as_die "#$pr loosens a gate, as listed above. The owner reviews and merges it. Nothing was merged."
fi
# Owner paths don't stop a merge. The reviewer's approval gates it, and the
# report lists the owner paths the pull request changes.
owner_note=""
if as_owner_path "$paths"; then
  owner_hits=$(grep -E -- "$(as_conf owner_paths)" "$paths" | tr '\n' ' ')
  owner_note="Owner paths changed: ${owner_hits% }."
  echo "$owner_note"
fi

# --- gate 7: the commit-msg gates --------------------------------------

lint=$(as_conf lint_squash_cmd "mise run lint-squash-msg")
[ -n "$lint" ] || as_die "lint_squash_cmd is empty; the commit-msg gates have to run"
if ! bash -c "$lint" >"$state/merge.lint" 2>&1; then
  cat "$state/merge.lint" >&2
  as_die "the commit-msg gates fail on SQUASH_AGENTMSG; fix the message and review it again"
fi

# --- the stamp, a second time, and our own copy ------------------------

# The readiness command and the lint are other programs, and either could have rewritten the draft. Check again,
# and merge from a copy of the bytes the stamp covers.
require_reviewed
message=$state/merge.message
cp "$draft" "$message"
[ "$(as_digest "$message")" = "$(as_stamp_value squash draft)" ] || as_die "SQUASH_AGENTMSG changed while it was being copied; review it again"
reviewed_digest=$(as_digest "$message")
body=$state/merge.body
awk 'NR > 1' "$message" | awk 'NF { seen = 1 } seen' | git stripspace >"$body"
[ -s "$body" ] || as_die "the draft has a subject and no body; a merge message says why"
wanted=$(printf '%s\n\n%s\n' "$subject" "$(cat "$body")" | git stripspace)

# --- the pull requests stacked on this one -----------------------------

# Whether the head branch lives in a fork, read before anything moves.
# A fork's branch is never a base in this repository: a pull request
# here whose base shares its name is based on another branch.
cross=$(am_live "$pr" isCrossRepository) || as_die "couldn't tell whether #$pr comes from a fork; nothing was merged"
case "$cross" in
true | false) ;;
*) as_die "couldn't tell whether #$pr comes from a fork; nothing was merged" ;;
esac

# Deleting this branch would close every open pull request in this
# repository whose base it is. Each one gets this pull request's base
# first. A pull request from a fork has no such children here.
if [ "$cross" = false ]; then
  # gh lists 30 by default. Ask for more than can exist, and refuse when
  # the list comes back full, since one left out would close when the
  # branch goes.
  child_limit=500
  children=$(gh pr list --base "$head_ref" --state open --limit "$child_limit" --json number | jq -r '.[].number') ||
    as_die "couldn't list the pull requests based on $head_ref"
  [ "$(printf '%s\n' "$children" | awk 'NF' | wc -l)" -lt "$child_limit" ] ||
    as_die "$child_limit or more pull requests are based on $head_ref, too many to move with certainty; nothing was merged"
  for child in $children; do
    [ "$child" != "$pr" ] || continue
    gh pr edit "$child" --base "$base" >/dev/null || as_die "couldn't move the base of #$child to $base; nothing was merged"
    echo "moved the base of #$child from $head_ref to $base"
  done
fi

# --- the merge ---------------------------------------------------------

status=0
gh pr merge "$pr" --squash --subject "$subject" --body-file "$body" --match-head-commit "$expected" || status=$?
tries=$(as_conf merge_verify_tries 5)
case "$tries" in
"" | *[!0-9]* | 0) tries=5 ;;
esac
live_state=""
oid=""
try=0
while [ "$try" -lt "$tries" ]; do
  try=$((try + 1))
  live_state=$(am_live "$pr" state) || live_state=unknown
  oid=$(gh pr view "$pr" --json mergeCommit 2>/dev/null | jq -r '.mergeCommit.oid // empty' 2>/dev/null || true)
  [ "$live_state" = MERGED ] && [ -n "$oid" ] && break
  [ "$try" -ge "$tries" ] || sleep 2
done
# A merge can land on GitHub with its response lost on the way back, so
# a failed call proves nothing alone. Only a read that shows the pull
# request still open counts as not merged. Any other state, an
# unreadable one included, is unconfirmed: the planner hears, and
# nothing is cleaned up.
if [ "$live_state" != MERGED ]; then
  if [ "$status" -ne 0 ] && [ "$live_state" = OPEN ]; then
    as_die "gh pr merge failed (exit $status) and #$pr is still open; nothing was merged"
  fi
  if [ "$status" -ne 0 ]; then
    as_mail blocker "$pr" "merge-unconfirmed: gh pr merge for #$pr in $repo failed (exit $status), and the pull request then read as $live_state. It may have merged with the response lost. Look at it before anything else runs; nothing was cleaned up."
    as_die "gh pr merge failed (exit $status) and #$pr reads as $live_state, so whether it merged is unconfirmed. Nothing was cleaned up. The planner has the report."
  fi
  as_mail blocker "$pr" "merge-unconfirmed: gh pr merge for #$pr in $repo reported success, but the pull request is $live_state. Look at it before anything else runs."
  as_die "gh pr merge reported success and #$pr is $live_state; the planner has the report"
fi

# A merge can't be undone, so what landed is checked against what was
# reviewed.
why=""
if [ -z "$oid" ]; then
  why="GitHub names no merge commit"
elif ! git fetch --quiet origin "+refs/heads/$base:refs/remotes/origin/$base" >/dev/null 2>&1; then
  why="the base branch couldn't be fetched"
elif ! git cat-file -e "$oid^{commit}" 2>/dev/null; then
  why="the merge commit $oid isn't on the fetched base branch"
elif ! git merge-base --is-ancestor "$oid" "refs/remotes/origin/$base" 2>/dev/null; then
  why="the merge commit $oid isn't on $base"
elif [ "$(git cat-file commit "$oid" | sed '1,/^$/d' | git stripspace)" != "$wanted" ]; then
  why="the merge commit's message isn't the reviewed message"
fi
if [ -n "$why" ]; then
  as_mail blocker "$pr" "merged-not-as-reviewed: #$pr in $repo is merged, and $why. The merge can't be undone; the branch and the worktree are left as they are."
  as_die "#$pr is merged, and $why. Nothing was cleaned up. The planner has the report."
fi
echo "merged: #$pr as $oid"

# --- tidy up -----------------------------------------------------------

# The remote branch goes only while it still points at the merged head.
# A push that landed after the merge holds work the merge doesn't have,
# so the delete leases on that head and leaves anything newer in place.
branch_note=""
if [ "$cross" = false ]; then
  if git push --quiet --force-with-lease="refs/heads/$head_ref:$expected" origin ":refs/heads/$head_ref" >"$state/merge.delete" 2>&1; then
    echo "deleted origin/$head_ref"
  else
    branch_note=" origin/$head_ref was left in place: it no longer points at the merged head $expected, or the remote refused the delete."
    echo "couldn't delete origin/$head_ref: it no longer points at the merged head $expected, or the remote refused the delete. It stays." >&2
  fi
else
  echo "the head branch is in a fork; it stays"
fi

main=$(git worktree list --porcelain | sed -n '1s/^worktree //p')
if [ -z "$main" ]; then
  echo "no main checkout found; nothing to fast-forward"
elif [ "$(git -C "$main" symbolic-ref -q --short HEAD || true)" != "$base" ]; then
  echo "the main checkout isn't on $base; it stays where it is"
elif [ -n "$(git -C "$main" status --porcelain --untracked-files=no)" ]; then
  echo "the main checkout has changes; it isn't fast-forwarded"
elif git -C "$main" merge --ff-only --quiet "$oid"; then
  echo "fast-forwarded the main checkout to $oid"
else
  echo "couldn't fast-forward the main checkout to $oid; it stays where it is" >&2
fi

# The local worktree of the merged branch, and the branch itself, go
# only when nothing of anyone's is in them: a clean worktree under
# .claude/worktrees/, not the one this runs in, with the branch where the
# review left it.
wt=""
cur=""
while IFS= read -r line; do
  case "$line" in
  "worktree "*) cur=${line#worktree } ;;
  "branch refs/heads/$head_ref") wt=$cur ;;
  esac
done <<LISTING
$(git worktree list --porcelain)
LISTING
local_tip=$(git rev-parse --verify --quiet "refs/heads/$head_ref" || true)
remove_branch=no
if [ -z "$local_tip" ]; then
  echo "no local branch $head_ref"
elif [ "$local_tip" != "$expected" ]; then
  echo "the local $head_ref isn't at the reviewed head; it stays"
elif [ -z "$wt" ]; then
  remove_branch=yes
elif [ "$wt" = "$main" ]; then
  echo "$head_ref is checked out in the main checkout; it stays"
else
  here=$(pwd -P)
  there=$(cd "$wt" && pwd -P) || there=$wt
  case "$wt/" in
  "$main"/.claude/worktrees/*) ;;
  *) there="" ;;
  esac
  if [ -z "$there" ]; then
    echo "the worktree of $head_ref isn't under .claude/worktrees/; it stays"
  elif [ "$here" = "$there" ]; then
    echo "this is the worktree of $head_ref; remove it from elsewhere"
  elif [ -n "$(git -C "$wt" status --porcelain)" ]; then
    echo "the worktree of $head_ref has changes; it stays"
  elif ! kept=$(am_kept_ignored "$wt"); then
    echo "couldn't list the ignored files in the worktree of $head_ref, or disposable_ignored is empty; it stays"
  elif [ -n "$kept" ]; then
    echo "the worktree of $head_ref holds ignored files that aren't caches, so it stays: $(printf '%s' "$kept" | tr '\n' ' ')"
  elif git worktree remove "$wt"; then
    echo "removed the worktree $wt"
    remove_branch=yes
  else
    echo "couldn't remove the worktree $wt; it stays" >&2
  fi
fi
if [ "$remove_branch" = yes ]; then
  if git branch -D "$head_ref" >/dev/null; then
    echo "deleted the local branch $head_ref"
  else
    echo "couldn't delete the local branch $head_ref" >&2
  fi
fi

# The draft belongs to the merge that just landed. It goes only if it is
# still the reviewed one.
[ "$(as_digest "$draft")" != "$reviewed_digest" ] || unlink "$draft"
as_clear squash
_as_forget merge checks paths body lint message delete labels loosening verdicts required required.raw rulesets ignored

report="merged #$pr in $repo as $oid.$branch_note"
[ -z "$owner_note" ] || report="$report $owner_note"
as_mail fixed "$pr" "$report"
echo "$report"
