#!/usr/bin/env bash
# What an agent needs before it drafts a pull request: where it stands,
# what would land, whether a pull request is already open, and the gh login.
# It writes nothing, and it never fails because gh is unavailable.
#
# Usage: preflight.sh
set -uo pipefail
# shellcheck source-path=SCRIPTDIR source=../../agent-commit/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../../agent-commit/scripts/lib.sh"
# shellcheck source-path=SCRIPTDIR source=draft.sh
. "$(dirname "${BASH_SOURCE[0]}")/draft.sh"

root=$(as_root) || exit 1
cd "$root" || exit 1
branch=$(git branch --show-current || true)

echo "== worktree =="
echo "root: $root"
echo "repository: $(as_repo_name)"
echo "branch: ${branch:-DETACHED}"
in_progress=$(as_in_progress)
[ -z "$in_progress" ] || echo "IN PROGRESS: a $in_progress. Stop and finish that first."

echo
echo "== base =="
default=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)
default=${default#origin/}
default=${default:-main}
base_ref=$(pr_base_ref "$default")
echo "default branch: $default"
if [ "$base_ref" = "origin/$default" ]; then
  counts=$(git rev-list --left-right --count "$base_ref...HEAD" 2>/dev/null || true)
  if [ -n "$counts" ]; then
    echo "behind $base_ref: $(echo "$counts" | cut -f1)"
    echo "ahead of $base_ref: $(echo "$counts" | cut -f2)"
  fi
else
  echo "no origin/$default; comparing against the local $default"
fi
[ "$branch" = "$default" ] && echo "ON THE DEFAULT BRANCH: switch to a topic branch first."

echo
echo "== commits that would land =="
landing=$(git log --reverse --format='%h %s' "$base_ref..HEAD" 2>/dev/null || true)
if [ -n "$landing" ]; then
  echo "$landing"
else
  echo "nothing to open a pull request for"
fi

echo
echo "== existing pull request =="
if [ -z "$branch" ]; then
  echo "none (detached HEAD)"
elif listing=$(gh pr list --head "$branch" --state open --json number,isDraft,url 2>/dev/null); then
  existing=$(printf '%s' "$listing" | jq -r '.[] | "#\(.number) " + (if .isDraft then "draft" else "ready for review" end) + " " + .url' 2>/dev/null || true)
  if [ -n "$existing" ]; then
    echo "$existing"
  else
    echo "none"
  fi
else
  echo "gh unavailable"
fi

echo
echo "== draft =="
if [ -s PR_AGENTDESC.md ]; then
  echo "PR_AGENTDESC.md exists; read it before overwriting it"
else
  echo "no PR_AGENTDESC.md. Scaffold one: bash .claude/skills/pr/scripts/scaffold-description.sh <base>"
fi

echo
echo "== report =="
mail_cmd=$(as_path "$(as_conf mail_cmd)")
mail_to=$(as_conf mail_to)
if [ -n "$mail_cmd" ] && [ -x "$mail_cmd" ] && [ -n "$mail_to" ]; then
  echo "mailbox: configured, reports go to $mail_to"
else
  echo "mailbox: not configured; publish.sh prints its report"
fi
exit 0
