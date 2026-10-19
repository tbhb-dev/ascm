#!/usr/bin/env bash
# PreToolUse guard for the agent-commit skill, on the Bash tool. Once
# preflight has armed it, it refuses the ways around commit.sh: a direct
# `git commit`, and a whole-tree `git add`. It stands aside again as
# soon as HEAD moves.
#
# What it is, and isn't. It catches the spellings an agent types by
# habit, wherever they sit in the command: after `env` or `command`,
# inside `bash -c`, a here-document, or a quoted string, through a full
# path to git, and across a line continuation. It reads the command as
# text, with quotes and backslashes taken out, so it errs toward
# refusing: a note that says `git commit` on one line is refused too,
# for the short time the guard is armed. It doesn't follow a variable
# that expands to a subcommand, and a command built to dodge it can.
# That is the same honesty boundary as the review stamp. The rules
# themselves are in commit.sh, which every kind of session meets, and a
# hook in a skill's frontmatter doesn't fire inside a subagent or a
# headless run at all.
#
# Exit 2 refuses the command and shows the message to the agent.
set -uo pipefail
# No pathname expansion: the words of a command are read as text.
set -f

input=$(cat)

deny() {
  {
    echo "Blocked by the agent-commit skill guard."
    echo "$1"
  } >&2
  exit 2
}

# Whether preflight armed the worktree at $1 and its HEAD hasn't moved.
armed_at() {
  local git_dir mark head
  git_dir=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || return 1
  mark=$git_dir/agent-commit.head
  [ -f "$mark" ] || return 1
  head=$(git -C "$1" rev-parse HEAD 2>/dev/null || echo unborn)
  [ "$(cat "$mark")" = "$head" ]
}

# Whether any worktree of the repository at $1 is armed. A command can
# point at another worktree with `-C` or `cd`, so one armed worktree
# arms the guard for the whole repository.
armed_near() {
  local worktree
  [ -d "$1" ] || return 1
  while IFS= read -r worktree; do
    [ -n "$worktree" ] && armed_at "$worktree" && return 0
  done <<EOF
$(git -C "$1" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')
EOF
  return 1
}

# Without jq the command can't be read. An armed guard that can't read
# refuses.
if ! command -v jq >/dev/null 2>&1; then
  armed_near "$PWD" && deny "The guard needs jq to read the command, and jq isn't on PATH."
  exit 0
fi
command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null) || {
  armed_near "$PWD" && deny "The guard couldn't read the command it was given."
  exit 0
}
[ -n "$command" ] || exit 0
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
[ -n "$cwd" ] && [ -d "$cwd" ] || cwd=$PWD

# The command as plain text: continuation lines joined, and quotes and
# backslashes removed, so `"git" commit` and `git comm""it` read as
# what the shell would run.
text=$(printf '%s\n' "$command" |
  awk '{ while (sub(/\\$/, " ")) { if ((getline next_line) <= 0) break; $0 = $0 next_line } print }' |
  tr -d "\"'\\\\")

# The guard is armed when the session's directory is in an armed
# repository, or the command points at one.
armed=no
armed_near "$cwd" && armed=yes
if [ "$armed" = no ]; then
  while IFS= read -r target; do
    [ -n "$target" ] || continue
    case "$target" in
    /*) ;;
    *) target=$cwd/$target ;;
    esac
    armed_near "$target" && armed=yes
  done <<EOF
$(printf '%s\n' "$text" | grep -o -E '(^|[[:space:]])(-C|cd)[[:space:]]+[^[:space:];&|]+' | awk '{ print $2 }')
EOF
fi
[ "$armed" = yes ] || exit 0

# The word `git`, then anything up to a command separator, then a
# subcommand as a word of its own.
git_then='(^|[^A-Za-z0-9_-])git([[:space:]][^;&|]*)?[[:space:]]'
word_end='([^A-Za-z0-9_.-]|$)'

if printf '%s\n' "$text" | grep -E -q -- "${git_then}commit${word_end}"; then
  deny "Commit through bash .claude/skills/agent-commit/scripts/commit.sh, which takes the reviewed COMMIT_AGENTMSG. It never takes -m or --no-verify. A command that only mentions git commit is refused too while the guard is armed."
fi
if printf '%s\n' "$text" | grep -E -q -- "(^|[^A-Za-z0-9_-])git([[:space:]][^;&|]*)?[[:space:]]-c[[:space:]]*alias\\."; then
  deny "A git alias defined on the command line could stand for commit. Run the git subcommand by its own name."
fi

# After `git add` or `git stage`, each word that would stage the whole
# tree, or that the guard can't read as a named path.
while IFS= read -r words; do
  for word in $words; do
    case "$word" in
    -[A-Za-z]*A* | -A* | --a | --al | --all | . | ./ | :/ | '*' | *'$'* | *'`'*)
      deny "Stage named paths with git add -- <path>... A whole-tree add sweeps in files the review never saw."
      ;;
    esac
  done
done <<EOF
$(printf '%s\n' "$text" | grep -o -E -- "${git_then}(add|stage)([[:space:]][^;&|]*)?" | sed -E 's/^.*[[:space:]](add|stage)//')
EOF
exit 0
