#!/usr/bin/env bash
# Run the independent review of PR_AGENTDESC.md against the branch it
# would publish, and stamp the draft when it passes.
#
# The script first takes one snapshot, in a directory of this run's own:
# a copy of the draft, then the commit at HEAD and the merge base with
# the base branch. The commits, the changed paths, the diff, and the
# template the reviewer reads all come from those two recorded commits,
# never from a live ref or the working tree. The stamp binds the copy's
# bytes, the head, and the merge base. Before stamping, the script
# confirms that the live draft, HEAD, and the merge base still match the
# snapshot, so the stamp can't cover bytes the reviewer never had, and
# publish.sh refuses when any of them has moved since.
#
# The reviewer is a fresh process with no shell. The diff is written as
# text, whatever the attributes and the configuration say.
#
# Past the round bound the review stays blocked. `--unblock` clears it,
# and it's for use on the planner's word alone.
#
# Usage: review.sh [--unblock]
# Exit: 0 stamped, 2 changes required, 3 blocked at the round bound,
#       4 no readable verdict, 5 the draft, HEAD, or the merge base
#       changed during the review.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=../../agent-commit/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../../agent-commit/scripts/lib.sh"
# shellcheck source-path=SCRIPTDIR source=draft.sh
. "$(dirname "${BASH_SOURCE[0]}")/draft.sh"

[ $# -le 1 ] || as_die "usage: review.sh [--unblock]"
case "${1:-}" in
"") ;;
--unblock)
  as_unblock pr
  exit 0
  ;;
*) as_die "usage: review.sh [--unblock]" ;;
esac

root=$(as_root)
cd "$root"
draft=$root/PR_AGENTDESC.md
[ -s "$draft" ] || as_die "PR_AGENTDESC.md is missing or empty; draft the description first"
in_progress=$(as_in_progress)
[ -z "$in_progress" ] || as_die "a $in_progress is in progress; finish or abort it before reviewing a pull request"
git symbolic-ref -q HEAD >/dev/null || as_die "HEAD is detached; check out the branch to review"
git rev-parse --verify --quiet HEAD >/dev/null || as_die "the branch has no commit yet"

# The snapshot. Everything the reviewer reads derives from the copy of
# the draft and these two commits.
inputs=$(as_new_inputs pr)
snapshot=$inputs/PR_AGENTDESC.md
cp "$draft" "$snapshot"
base=$(pr_field "$snapshot" base)
[ -n "$base" ] || as_die "the draft's frontmatter has no base:"
base_ref=$(pr_base_ref "$base")
head=$(git rev-parse HEAD)
mergebase=$(git merge-base "$base_ref" "$head") || as_die "no merge base between $base_ref and HEAD"

# The diff as text, whatever the attributes and the environment say: no
# external diff program, no text conversion, and no file shown as
# binary. A change that produced no diff at all means something hid it.
git log --reverse --pretty=format:'--- %h%n%B' "$mergebase..$head" >"$inputs/commits.txt"
git -c core.quotePath=false diff --no-ext-diff --no-textconv --no-color --no-renames --name-status "$mergebase" "$head" >"$inputs/changed-paths.txt"
git diff --text --no-ext-diff --no-textconv --no-color --no-renames "$mergebase" "$head" >"$inputs/branch.diff"
if [ -s "$inputs/changed-paths.txt" ] && [ ! -s "$inputs/branch.diff" ]; then
  as_die "review: the branch names changed paths, but its diff is empty; something is hiding the content"
fi
if git cat-file -e "$head:.github/pull_request_template.md" 2>/dev/null; then
  git show "$head:.github/pull_request_template.md" >"$inputs/pull_request_template.md"
fi

still_as_gathered() {
  [ "$(git rev-parse HEAD)" = "$head" ] && [ "$(git merge-base "$base_ref" HEAD)" = "$mergebase" ]
}

criteria=$(as_path "$(as_conf review_pr_criteria .claude/skills/review-pr-description/SKILL.md)")
as_review pr "$draft" "$snapshot" "$criteria" "$root" "$inputs" still_as_gathered \
  head "$head" mergebase "$mergebase"
