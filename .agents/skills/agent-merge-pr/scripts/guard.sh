#!/usr/bin/env bash
# PreToolUse guard for the agent-merge-pr skill, on the Bash tool. It is
# always active while the skill is loaded: it refuses a direct
# `gh pr merge`, and a `gh api` call that writes to a merge path, and
# points at merge.sh, which holds every gate.
#
# What it is, and isn't. It reads the command as text, with quotes and
# backslashes taken out and line continuations joined, so it catches the
# spellings an agent types by habit: after `env` or `command`, inside
# `bash -c` or a here-document, through a global flag such as
# `-R owner/repo`. It errs toward refusing, so a note that says
# `gh pr merge` is refused too. It doesn't follow a variable that expands
# to a subcommand, and a command built to dodge it can. The rules
# themselves are in merge.sh, which every kind of session meets, and a
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
    echo "Blocked by the agent-merge-pr skill guard."
    echo "$1"
  } >&2
  exit 2
}

by_script="Merge through bash .claude/skills/agent-merge-pr/scripts/merge.sh <pr> <head>, which takes the reviewed SQUASH_AGENTMSG and checks every gate. A command that only mentions the merge is refused too."

# Without jq the command can't be read. The guard is always active, so
# it can't refuse every command; it refuses one whose input mentions a
# merge.
if ! command -v jq >/dev/null 2>&1; then
  case "$input" in
  *merge* | *Merge*) deny "The guard needs jq to read the command, and jq isn't on PATH. $by_script" ;;
  esac
  exit 0
fi
command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null) || {
  case "$input" in
  *merge* | *Merge*) deny "The guard couldn't read the command it was given. $by_script" ;;
  esac
  exit 0
}
[ -n "$command" ] || exit 0

# The command as plain text: continuation lines joined, and quotes and
# backslashes removed, so `"gh" pr merge` and `gh pr mer""ge` read as
# what the shell would run.
text=$(printf '%s\n' "$command" |
  awk '{ while (sub(/\\$/, " ")) { if ((getline next_line) <= 0) break; $0 = $0 next_line } print }' |
  tr -d "\"'\\\\")

# The word `gh`, then anything up to a command separator, then a
# subcommand as a word of its own.
gh_then='(^|[^A-Za-z0-9_-])gh([[:space:]][^;&|]*)?[[:space:]]'
word_end='([^A-Za-z0-9_.-]|$)'

if printf '%s\n' "$text" | grep -E -q -- "${gh_then}pr[[:space:]]+merge${word_end}"; then
  deny "$by_script"
fi
if printf '%s\n' "$text" | grep -E -q -- "${gh_then}alias[[:space:]]+(set|import)${word_end}"; then
  deny "A gh alias could stand for pr merge. Run the gh subcommand by its own name."
fi

# A gh api call: the same command, up to its separator. It is refused
# when it names a merge path or a merge mutation and doesn't say, with
# an explicit method, that it only reads.
while IFS= read -r call; do
  [ -n "$call" ] || continue
  if printf '%s\n' "$call" | grep -E -q -- "(/merges?|mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest)${word_end}"; then
    if ! printf '%s\n' "$call" | grep -E -q -- '(^|[[:space:]])(-X|--method)(=|[[:space:]]*)GET([[:space:]]|$)'; then
      deny "A gh api call that writes to a merge path merges around merge.sh. $by_script"
    fi
  fi
done <<CALLS
$(printf '%s\n' "$text" | grep -o -E -- "${gh_then}api[[:space:]][^;&|]*")
CALLS
exit 0
