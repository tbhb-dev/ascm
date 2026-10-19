#!/usr/bin/env bash
# What an agent needs before it drafts a commit: where it stands, what
# is staged, the message rules, and the signing and hook setup.
# It writes one thing: the mark that arms
# guard.sh until HEAD moves.
#
# Usage: preflight.sh
set -uo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

root=$(as_root) || exit 1
cd "$root" || exit 1
git_dir=$(as_git_dir)

echo "== worktree =="
echo "root: $root"
echo "repository: $(as_repo_name)"
echo "branch: $(git branch --show-current || true)"
for marker in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD; do
  [ -e "$git_dir/$marker" ] && echo "IN PROGRESS: $marker. Stop and finish that first."
done

printf '%s' "$(git rev-parse HEAD 2>/dev/null || echo unborn)" >"$git_dir/agent-commit.head"

echo
echo "== preconditions =="
if git check-ignore -q COMMIT_AGENTMSG; then echo "COMMIT_AGENTMSG is ignored: ok"; else echo "MISSING: COMMIT_AGENTMSG isn't in .gitignore"; fi
criteria=$(as_path "$(as_conf review_commit_criteria .claude/skills/review-commit-message/SKILL.md)")
if [ -f "$criteria" ]; then echo "review criteria: ok"; else echo "MISSING: review criteria at $criteria"; fi
echo "reviewer: $(as_conf review_cmd "claude -p, model $(as_conf review_model UNSET), effort $(as_conf review_effort UNSET)")"
echo "review rounds allowed: $(as_conf review_max_rounds 3)"
[ -s COMMIT_AGENTMSG ] && echo "a draft is already here; read it before overwriting it"
if [ "$(git config --get gpg.format || true)" = ssh ] && [ -n "$(as_signing_key)" ]; then
  echo "signing: ssh, with the configured key"
else
  echo "MISSING: SSH signing with a readable key; commit.sh refuses without it"
fi
if [ -z "$(as_conf hook_marker "")" ] || [ -z "$(as_conf required_hooks "commit-msg pre-commit post-commit")" ]; then
  echo "MISSING: hook_marker or required_hooks is empty; commit.sh refuses until both are set"
else
  missing=$(as_missing_hooks | tr '\n' ' ')
  if [ -z "$missing" ]; then echo "hooks: installed"; else echo "MISSING: hooks not installed as this repository's own: ${missing% }. Run mise run devtools:prek-install."; fi
fi

echo
echo "== staged =="
git diff --cached --stat
echo
echo "== unstaged and untracked =="
git status --short | grep -v '^[MADRC] ' || true

echo
echo "== recent subjects =="
git log -8 --format='%h %s' 2>/dev/null || true

echo
echo "== trailers =="
echo "Assisted-by: <harness>:<model> <tool>   (before Signed-off-by)"
# The identity git will commit as. git var honours the GIT_COMMITTER_*
# variables a launcher sets to pick the agent's identity, where
# user.name and user.email alone would name whoever owns the config.
ident=$(git var GIT_COMMITTER_IDENT 2>/dev/null | sed -E 's/ [0-9]+ [-+][0-9]{4}$//' || true)
echo "Signed-off-by: ${ident:-UNSET: git has no committer identity here}"
