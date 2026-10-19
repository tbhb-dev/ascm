#!/usr/bin/env bash
# The one sanctioned way for an agent to open or update a pull request.
# It takes no argument: the pull request is PR_AGENTDESC.md, exactly as
# the independent review stamped it.
#
# In order, it refuses unless: the draft exists, no merge, cherry-pick,
# revert, or rebase is under way, the branch is neither the base nor
# detached, the mechanical checks pass, the stamp covers the draft's
# bytes, HEAD, and the merge base, and gh is signed in. It then makes its own
# copy of the reviewed draft, checks that gh knows the copy's labels and
# milestone, and checks the stamp a second time, because the lint is
# another program and could have rewritten the draft or moved HEAD. Every property of the pull request comes from the
# copy: title, body, base, labels, milestone, and whether it is a draft.
# The draft's reviewers and assignees are ignored.
#
# After the push it checks that HEAD is still the reviewed commit and
# that origin holds it, because a pre-push hook is another program too.
# Then it opens the pull request, or brings the open one up to date. A
# report goes to the planner at the end.
#
# The push is a fast-forward of what origin holds, or nothing. The
# skill opens pull requests and doesn't rewrite history, so a branch
# whose remote has commits HEAD lacks is refused with a blocker. A branch
# rebased on purpose goes up only with --rewrite, which the caller passes
# on the planner's word: then the push leases on the exact remote commit
# read just before it, and the report names it as a rewrite.
#
# gh acts as whoever is signed in. Before any write the signed-in login
# has to be the agent account, gh_login (tbhb-agent unless the environment
# or the machine's file says otherwise), so the skill never acts as the
# owner.
#
# Usage: publish.sh [--rewrite]
# Exit: 0 published, 1 refused.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=../../agent-commit/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../../agent-commit/scripts/lib.sh"
# shellcheck source-path=SCRIPTDIR source=draft.sh
. "$(dirname "${BASH_SOURCE[0]}")/draft.sh"

# The agent reads this output, so the operator's preferences must not
# change its shape, and gh must not wait on a prompt. The unset keeps
# GH_REPO from sending gh at another repository.
export GH_PAGER=cat GH_PROMPT_DISABLED=1
unset GH_REPO GH_HOST

rewrite=no
case "$#:${1:-}" in
0:) ;;
1:--rewrite) rewrite=yes ;;
*) as_die "usage: publish.sh [--rewrite]" ;;
esac

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(as_root)
cd "$root"
draft=$root/PR_AGENTDESC.md
state=$(as_state_dir)

[ -s "$draft" ] || as_die "PR_AGENTDESC.md is missing or empty; draft the description first"
in_progress=$(as_in_progress)
[ -z "$in_progress" ] || as_die "a $in_progress is in progress; finish or abort it first. A pull request opened now would publish a branch the review never saw."
branch=$(git branch --show-current)
[ -n "$branch" ] || as_die "HEAD is detached; check out the branch to publish"
base=$(pr_field "$draft" base)
[ -n "$base" ] || as_die "the draft's frontmatter has no base:"
[ "$branch" != "$base" ] || as_die "$branch is the base branch; publish a topic branch"
base_ref=$(pr_base_ref "$base")
git merge-base "$base_ref" HEAD >/dev/null || as_die "no merge base between $base_ref and HEAD"

# The stamp has to match everything the review read.
require_reviewed() {
  as_require_stamp pr "$draft" head "$(git rev-parse HEAD)" mergebase "$(git merge-base "$base_ref" HEAD)"
}

bash "$here/check.sh" || as_die "the description checks fail; fix the findings and review again"
require_reviewed

command -v gh >/dev/null 2>&1 || as_die "gh is not installed"
gh auth status >/dev/null 2>&1 || as_die "gh is not authenticated; run gh auth login"
# The agent account defaults to tbhb-agent. Only the environment or the
# machine's file can change it, never the repository's settings file.
want_login=$(as_conf gh_login tbhb-agent)
[ -n "$want_login" ] || as_die "gh_login isn't configured; it is empty in the environment or the machine's settings"
login=$(gh api user --jq .login 2>/dev/null) || as_die "gh couldn't say which account it is signed in as"
[ "$login" = "$want_login" ] || as_die "gh is signed in as ${login:-nobody}, not $want_login; nothing was pushed"

# --- the reviewed copy --------------------------------------------------
#
# Everything published comes from this copy, whose digest is the one the
# stamp recorded. The working file can change again before the push.

copy=$state/pr.draft
require_reviewed
cp "$draft" "$copy"
[ "$(as_digest "$copy")" = "$(as_stamp_value pr draft)" ] || as_die "PR_AGENTDESC.md changed while it was being copied; review it again"
[ "$(pr_field "$copy" base)" = "$base" ] || as_die "the draft's base changed after the review; review it again"

is_draft=$(pr_field "$copy" draft)
case "$is_draft" in
true | false) ;;
*) as_die "the draft's frontmatter needs draft: true or draft: false" ;;
esac
labels=$(pr_sequence "$(pr_field "$copy" labels)")
milestone=$(pr_field "$copy" milestone)
[ -n "$(pr_title "$copy")" ] || as_die "the draft has no level 1 heading to use as the title"
[ -n "$(pr_body "$copy")" ] || as_die "the description has a title but no body"

# gh adds labels and the milestone as it opens the pull request, so an
# unknown one fails after the pull request exists. Resolving them first
# turns that into a clean refusal.
known_labels=$(gh label list --limit 200 --json name --jq '.[].name') || as_die "gh could not list the repository's labels"
while IFS= read -r want; do
  [ -n "$want" ] || continue
  printf '%s\n' "$known_labels" | grep -q -x -F -- "$want" || as_die "no label named \"$want\" in this repository"
done <<EOF
$labels
EOF
if [ -n "$milestone" ]; then
  known_milestones=$(gh api 'repos/{owner}/{repo}/milestones' --jq '.[].title') || as_die "gh could not list the repository's milestones"
  printf '%s\n' "$known_milestones" | grep -q -x -F -- "$milestone" || as_die "no milestone named \"$milestone\" in this repository"
fi

# Every issue or pull request the description cites has to exist. A
# number nobody opened is an invented claim a machine can settle, and the
# check on Related reads only its form.
pr_body "$copy" >"$state/pr.body-refs"
for ref in $(grep -o '#[0-9][0-9]*' "$state/pr.body-refs" | sort -u || true); do
  gh api "repos/{owner}/{repo}/issues/${ref#\#}" --jq .number >/dev/null 2>&1 ||
    as_die "the description cites $ref, which isn't an issue or pull request here"
done

# --- push ---------------------------------------------------------------

require_reviewed
head=$(git rev-parse HEAD)
# What origin holds now, read from origin itself and not from a
# remote-tracking ref that may be stale.
was=$(git ls-remote origin "refs/heads/$branch" | cut -f1) || as_die "couldn't read $branch from origin; nothing was published"
rewrote=""
if [ -z "$was" ] || [ "$was" = "$head" ] || git merge-base --is-ancestor "$was" HEAD 2>/dev/null; then
  git push --set-upstream origin "HEAD:refs/heads/$branch" || as_die "git push failed; nothing was published"
elif [ "$rewrite" = yes ]; then
  # A rewrite replaces only a commit this checkout has seen, and never
  # one HEAD is merely behind.
  git cat-file -e "$was^{commit}" 2>/dev/null ||
    as_die "origin has $branch at $was, a commit this checkout has never fetched. Nothing was pushed; fetch and look at it first."
  ! git merge-base --is-ancestor HEAD "$was" ||
    as_die "HEAD is behind origin's $branch at $was; a rewrite would drop its commits. Nothing was pushed."
  git push --set-upstream --force-with-lease="refs/heads/$branch:$was" origin "HEAD:refs/heads/$branch" ||
    as_die "the rewrite push failed; origin may have moved past $was. Nothing was published."
  rewrote=" The push rewrote $branch on origin, replacing $was."
  echo "REWRITE: $branch on origin went from $was to $head."
else
  as_mail blocker "" "non-fast-forward: origin has $branch at $was, which HEAD $head doesn't contain, in $(as_repo_name). Nothing was pushed. A rebased branch goes up only with publish.sh --rewrite, on the planner's word."
  as_die "origin has $branch at $was, which HEAD doesn't contain. Nothing was pushed. If the branch was rebased on purpose, publish with --rewrite on the planner's word; otherwise bring in the remote commits first."
fi

# A pre-push hook, or a server that rewrites a ref, could leave something
# other than the reviewed commit on the branch.
[ "$(git rev-parse HEAD)" = "$head" ] || as_die "HEAD moved during the push; origin has $branch as it stood, but no pull request was touched. Review again."
remote=$(git ls-remote origin "refs/heads/$branch" | cut -f1)
[ "$remote" = "$head" ] || as_die "origin has ${remote:-no $branch} after the push, not the reviewed $head; no pull request was touched"

# --- open or update -----------------------------------------------------

title=$(pr_title "$copy")
body_file=$state/pr.body.md
pr_body "$copy" >"$body_file"
open_number=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number // empty') || as_die "gh could not list pull requests for $branch"

if [ -z "$open_number" ]; then
  set -- --base "$base" --head "$branch" --title "$title" --body-file "$body_file"
  if [ "$is_draft" = true ]; then
    set -- "$@" --draft
  fi
  while IFS= read -r item; do
    [ -n "$item" ] || continue
    set -- "$@" --label "$item"
  done <<EOF
$labels
EOF
  if [ -n "$milestone" ]; then
    set -- "$@" --milestone "$milestone"
  fi
  gh pr create "$@" || as_die "gh pr create failed; the branch is pushed, so run publish.sh again"
  number=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number // empty') || as_die "gh could not list pull requests for $branch"
  [ -n "$number" ] || as_die "the pull request opened but gh doesn't list it; check the repository"
else
  number=$open_number
  current=$(gh pr view "$number" --json baseRefName,isDraft,labels,milestone)
  current_labels=$(printf '%s' "$current" | jq -r '.labels[].name')
  set -- --base "$base" --title "$title" --body-file "$body_file"
  while IFS= read -r item; do
    [ -n "$item" ] || continue
    printf '%s\n' "$current_labels" | grep -q -x -F -- "$item" || set -- "$@" --add-label "$item"
  done <<EOF
$labels
EOF
  while IFS= read -r item; do
    [ -n "$item" ] || continue
    printf '%s\n' "$labels" | grep -q -x -F -- "$item" || set -- "$@" --remove-label "$item"
  done <<EOF
$current_labels
EOF
  if [ -n "$milestone" ]; then
    set -- "$@" --milestone "$milestone"
  elif [ -n "$(printf '%s' "$current" | jq -r '.milestone.title // empty')" ]; then
    set -- "$@" --remove-milestone
  fi
  gh pr edit "$number" "$@" || as_die "gh pr edit failed; the branch is pushed, so run publish.sh again"
  current_draft=$(printf '%s' "$current" | jq -r '.isDraft')
  if [ "$is_draft" = true ] && [ "$current_draft" = false ]; then
    gh pr ready "$number" --undo || as_die "gh pr ready --undo failed; run publish.sh again"
  elif [ "$is_draft" = false ] && [ "$current_draft" = true ]; then
    gh pr ready "$number" || as_die "gh pr ready failed; run publish.sh again"
  fi
fi

# --- report -------------------------------------------------------------

url=$(gh pr view "$number" --json url --jq .url)
if [ "$is_draft" = true ]; then
  kind="open as a draft"
else
  kind="open and ready for review"
fi
as_mail fixed "$number" "#$number $title is $kind: head $head on base $base.$rewrote"
as_clear pr
_as_forget pr draft body.md paths body-refs
echo "published: $url at $head"
