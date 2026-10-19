#!/usr/bin/env bash
# The one sanctioned way for an agent to commit. It takes no message and
# no flag but --amend: the message is COMMIT_AGENTMSG, exactly as the
# independent review stamped it.
#
# In order, it refuses unless: SSH signing names a key, the
# repository's own hooks are installed, the draft and a staged change
# exist, the stamp covers the draft's bytes, the staged tree, the commit
# at HEAD, and the mode, and the commit-msg gates pass. It checks the
# stamp a second time after the lint has run, and commits from its own copy of the
# reviewed message, always signed. If the commit that results has
# another tree, message, or parents than the reviewed ones, or no good
# signature from the configured key, it moves the branch back, but only
# the branch it started on and only off the commit this run made. It
# reports a commit that a hook already pushed, which a local move
# doesn't retract.
#
# Usage: commit.sh [--amend]
# Exit: 0 committed, 1 refused, 75 held (signing failed): the index and
#       the draft stay as they are.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

mode=normal
[ $# -le 1 ] || as_die "usage: commit.sh [--amend]"
case "${1:-}" in
"") ;;
--amend) mode=amend ;;
*) as_die "usage: commit.sh [--amend]" ;;
esac

root=$(as_root)
cd "$root"
draft=$root/COMMIT_AGENTMSG
state=$(as_state_dir)

[ -f "$draft" ] && [ -s "$draft" ] || as_die "COMMIT_AGENTMSG is missing or empty; draft the message first"
in_progress=$(as_in_progress)
[ -z "$in_progress" ] || as_die "a $in_progress is in progress; finish or abort it first. A commit made now would take its other parents or its author."
ref=$(git symbolic-ref -q HEAD) || as_die "HEAD is detached; check out a branch before committing"
start=$(git rev-parse --verify --quiet HEAD) || as_die "the branch has no commit yet; make the first commit by hand"

# Every commit is signed with SSH, by the key the configuration names,
# and runs the repository's own hooks.
[ "$(git config --get gpg.format || true)" = ssh ] || as_die "commits here are signed with SSH, and gpg.format isn't ssh"
key=$(as_signing_key)
[ -n "$key" ] || as_die "user.signingkey names no public key this can read; set it to the key or to a key file with a .pub beside it"
[ -n "$(as_conf hook_marker "")" ] && [ -n "$(as_conf required_hooks "commit-msg pre-commit post-commit")" ] ||
  as_die "hook_marker and required_hooks have to be set; the commit runs the repository's own hooks"
missing=$(as_missing_hooks | tr '\n' ' ')
[ -z "$missing" ] || as_die "these hooks aren't installed as this repository's own: ${missing% }. Install them (mise run devtools:prek-install), then run commit.sh again."

# The paths of the commit that will exist, one a line in a file. An
# amend replaces the commit at HEAD, so its paths are everything against
# the parent of HEAD, and not just what was staged since.
manifest=$state/commit.paths
if [ "$mode" = amend ]; then
  parent=$(git rev-parse --verify --quiet "$start~1" || git hash-object -t tree /dev/null)
  as_paths "$manifest" --cached "$parent"
else
  as_paths "$manifest" --cached
  [ -s "$manifest" ] || as_die "nothing is staged; stage the change with git add -- <paths>"
fi

# The stamp has to match everything the review read.
require_reviewed() {
  as_require_stamp commit "$draft" tree "$(git write-tree)" head "$(git rev-parse HEAD)" mode "$mode"
}
require_reviewed
tree=$(git write-tree)

lint=$(as_conf lint_commit_cmd "mise run lint-commit-msg")
[ -n "$lint" ] || as_die "lint_commit_cmd is empty; the commit-msg gates have to run"
if ! bash -c "$lint" >"$state/commit.lint" 2>&1; then
  cat "$state/commit.lint" >&2
  as_die "the commit-msg gates fail on COMMIT_AGENTMSG; fix the message and review it again"
fi

# The lint is another program, and it could have rewritten the draft or
# the index. Check again, and commit
# from a copy this script made of the bytes the stamp covers.
require_reviewed
message=$state/commit.message
cp "$draft" "$message"
[ "$(as_digest "$message")" = "$(as_stamp_value commit draft)" ] || as_die "COMMIT_AGENTMSG changed while it was being copied; review it again"

set -- -c commit.gpgsign=true -c commit.cleanup=whitespace commit -F "$message"
[ "$mode" = amend ] && set -- "$@" --amend
log=$state/commit.log
# The branch's reflog says which commit git commit itself wrote. Hooks
# run inside the commit, so HEAD read afterward may already be theirs.
reflog_before=$(git log -g --format=%H "$ref" 2>/dev/null | wc -l)
status=0
command git "$@" >"$log" 2>&1 || status=$?
cat "$log"
if [ "$status" -ne 0 ]; then
  if as_signing_refused "$log"; then
    # The pre-commit hooks ran before the signer did, and one of them
    # may have changed the index or the draft.
    if [ "$(git write-tree)" = "$tree" ] && [ "$(as_digest "$draft")" = "$(as_stamp_value commit draft)" ]; then
      as_mail blocker "" "signing-refused: signing failed for a commit in $(as_repo_name) on $(git branch --show-current). The change is staged and the reviewed message is in COMMIT_AGENTMSG; nothing was committed."
      echo "HELD: signing failed. The staged change and the reviewed draft are unchanged. Wait for word that signing works, then run commit.sh again." >&2
    else
      as_mail blocker "" "signing-refused: signing failed for a commit in $(as_repo_name) on $(git branch --show-current), and the hooks changed the index or the draft before it did, so the stamp no longer covers them. Nothing was committed. The change needs a fresh review once signing works."
      echo "HELD: signing failed, and the hooks changed the index or the draft before it did. The stamp no longer covers what is staged. Once signing works, review the change again with review.sh, then run commit.sh." >&2
    fi
    exit 75
  fi
  as_die "git commit failed; the change is still staged"
fi

# A hook can rewrite the index or the message during the commit, and it
# can move HEAD or the branch. What follows judges the commit git wrote,
# found in the reflog of the branch this run started on: the oldest
# commit entry since the commit began, the one git commit itself wrote. The
# message comes from the commit object itself, since settings such as
# log.showSignature add lines to what `git log` prints.
tip=$(git rev-parse "$ref")
made=""
new_entries=$(($(git log -g --format=%H "$ref" 2>/dev/null | wc -l) - reflog_before))
if [ "$new_entries" -ge 1 ]; then
  made=$(git log -g --format='%H %gs' "$ref" | head -n "$new_entries" |
    grep -E '^[0-9a-f]+ commit( \(amend\))?: ' | tail -n 1 | cut -d' ' -f1 || true)
fi
# The one parent line a commit by this run can have: the start for a
# new commit, and the start's own parents for an amend.
if [ "$mode" = amend ]; then
  expected_parents=$(git rev-list --parents -n 1 "$start" | cut -s -d' ' -f2-)
else
  expected_parents=$start
fi
made_parents=""
[ -z "$made" ] || made_parents=$(git rev-list --parents -n 1 "$made" | cut -s -d' ' -f2-)
why=""
if [ -z "$made" ]; then
  why="the branch's reflog shows no commit by this run, so this run can't tell which commit is its own"
elif [ "$tip" = "$start" ]; then
  why="the branch is where it started, so a hook removed the commit this run made"
elif [ "$tip" != "$made" ]; then
  why="a hook moved the branch from the commit this run made, $made, to $tip"
elif [ "$made_parents" != "$expected_parents" ]; then
  why="its parents aren't the ones this run committed on, so a hook replaced it"
elif [ "$(git rev-parse "$made^{tree}")" != "$tree" ]; then
  why="a hook changed the index, and the commit's tree isn't the reviewed tree"
elif [ "$(git cat-file commit "$made" | sed '1,/^$/d' | git stripspace)" != "$(git stripspace <"$message")" ]; then
  why="a hook changed the message, and the commit's message isn't the reviewed message"
elif ! as_signed_by "$made" "$key" "$state/commit.signers"; then
  why="the commit has no good signature from the configured key"
elif [ "$(git symbolic-ref -q HEAD || true)" != "$ref" ]; then
  why="a hook moved HEAD off ${ref#refs/heads/} during the commit"
fi

if [ -n "$why" ]; then
  # Move the branch back only when that is certain to undo this run's
  # commit and nothing else: HEAD still names the branch, the branch's
  # tip is exactly the commit git wrote for this run, on the expected
  # parents, and it still is when the update happens. Otherwise change
  # nothing.
  outcome="It was NOT taken back, because the branch or HEAD isn't where this run left it. Nothing was changed. ${ref#refs/heads/} is at $tip."
  if [ -n "$made" ] &&
    [ "$tip" = "$made" ] &&
    [ "$made_parents" = "$expected_parents" ] &&
    [ "$(git symbolic-ref -q HEAD || true)" = "$ref" ] &&
    git update-ref "$ref" "$start" "$made" 2>/dev/null; then
    outcome="${ref#refs/heads/} was moved back to $start. The index and the working tree are as the hook left them."
  fi
  # A hook may have pushed the commit already, and moving the local
  # branch doesn't retract that. Only a remote-tracking ref can show it.
  remotes=""
  [ -z "$made" ] || remotes=$(git for-each-ref --contains "$made" --format='%(refname:short)' refs/remotes 2>/dev/null | tr '\n' ' ')
  if [ -n "$remotes" ]; then
    outcome="$outcome The commit is already on ${remotes% }, and a local change doesn't retract it: that needs recovery by hand."
  else
    outcome="$outcome No remote-tracking ref has the commit. A hook could still have pushed it somewhere this check can't see."
  fi
  as_mail blocker "" "commit-not-as-reviewed: in $(as_repo_name), commit ${made:-unknown} on ${ref#refs/heads/} isn't the reviewed commit: $why. $outcome"
  as_die "The commit ${made:-unknown} isn't the reviewed commit: $why. $outcome Review the state before going on."
fi

as_clear commit
_as_forget commit lint log message paths signers
[ -f "$(as_git_dir)/agent-commit.head" ] && unlink "$(as_git_dir)/agent-commit.head"
echo "committed: $made"
