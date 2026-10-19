#!/usr/bin/env bash
# Run the independent review of COMMIT_AGENTMSG against the staged
# change, and stamp the draft when it passes.
#
# The script first takes one snapshot: the staged tree, the commit at
# HEAD, and a copy of the draft, in a directory of this run's own. The
# diff the reviewer reads comes from that tree and that commit, never
# from the live index. The stamp binds the copy's bytes, the tree, the
# commit, and whether the review read an amend. Before stamping, the
# script confirms that the live draft, index, and HEAD still match the
# snapshot, so the stamp can't cover bytes the reviewer never had.
#
# The reviewer is a fresh process with no shell. For an amend, the diff
# is the amended commit's whole content, against the parent of HEAD.
#
# Past the round bound the review stays blocked. `--unblock` clears it,
# and it's for use on the planner's word alone.
#
# Usage: review.sh [--amend | --unblock]
# Exit: 0 stamped, 2 changes required, 3 blocked at the round bound,
#       4 no readable verdict, 5 the draft, index, or HEAD changed
#       during the review.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

mode=normal
arguments_suffix=""
[ $# -le 1 ] || as_die "usage: review.sh [--amend | --unblock]"
case "${1:-}" in
"") ;;
--amend)
  mode=amend
  arguments_suffix=" --amend"
  ;;
--unblock)
  as_unblock commit
  exit 0
  ;;
*) as_die "usage: review.sh [--amend | --unblock]" ;;
esac

root=$(as_root)
cd "$root"
draft=$root/COMMIT_AGENTMSG
[ -f "$draft" ] && [ -s "$draft" ] || as_die "COMMIT_AGENTMSG is missing or empty; draft the message first"
in_progress=$(as_in_progress)
[ -z "$in_progress" ] || as_die "a $in_progress is in progress; finish or abort it before reviewing a commit"
git rev-parse --verify --quiet HEAD >/dev/null || as_die "the branch has no commit yet; make the first commit by hand"

# The snapshot. Everything the reviewer reads derives from these three.
tree=$(git write-tree)
head=$(git rev-parse HEAD)
inputs=$(as_new_inputs commit)
snapshot=$inputs/COMMIT_AGENTMSG
cp "$draft" "$snapshot"

against=$head
if [ "$mode" = amend ]; then
  # The first commit of a history has no parent, so its amend reads
  # against the empty tree.
  against=$(git rev-parse --verify --quiet "$head~1" || git hash-object -t tree /dev/null)
fi
# The diff as text, whatever the attributes and the environment say: no
# external diff program, no text conversion, and no file shown as
# binary. A change that produced no diff at all means something hid it.
git -c core.quotePath=false diff --no-ext-diff --no-textconv --no-color --no-renames --name-status "$against" "$tree" >"$inputs/staged-paths.txt"
git diff --text --no-ext-diff --no-textconv --no-color --no-renames "$against" "$tree" >"$inputs/staged.diff"
if [ -s "$inputs/staged-paths.txt" ] && [ ! -s "$inputs/staged.diff" ]; then
  as_die "review: the staged change names paths, but its diff is empty; something is hiding the content"
fi
git log -5 --pretty=format:'%h %s' "$head" >"$inputs/recent-subjects.txt"

still_as_gathered() {
  [ "$(git write-tree)" = "$tree" ] && [ "$(git rev-parse HEAD)" = "$head" ]
}

criteria=$(as_path "$(as_conf review_commit_criteria .claude/skills/review-commit-message/SKILL.md)")
as_review commit "$draft" "$snapshot" "$criteria" "$root$arguments_suffix" "$inputs" still_as_gathered \
  tree "$tree" head "$head" mode "$mode"
