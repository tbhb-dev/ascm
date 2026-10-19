#!/usr/bin/env bash
# PreToolUse guard for the agent-pr skill, on the Bash tool. It refuses
# the ways around publish.sh: every `gh pr` verb that changes a pull
# request, and a `gh api` call that changes one through a /pulls path.
# The verbs that only read, list, view, diff, status, and checks, pass,
# and so does a read through `gh api`.
#
# What it is, and isn't. It catches the spellings an agent types by
# habit, wherever they sit in the command: after gh's global options, as
# in `gh -R owner/repo pr create`, after `env` or `command`, inside
# `bash -c`, a here-document, or a quoted string, through a full path to
# gh, and across a line continuation. It reads the command as text, with
# quotes and backslashes taken out, so it errs toward refusing: a note
# that says `gh pr create` on one line is refused too. It doesn't follow
# a variable that expands to a subcommand, and a command built to dodge
# it can. That is the same honesty boundary as the review stamp. The
# rules themselves are in publish.sh, which every kind of session meets,
# and a hook in a skill's frontmatter doesn't fire inside a subagent or a
# headless run at all. Unlike the agent-commit guard it needs no arming:
# nothing here ever calls gh pr directly. So it is always active, and a
# command it can't read is refused.
#
# Exit 2 refuses the command and shows the message to the agent.
set -uo pipefail
# No pathname expansion: the words of a command are read as text.
set -f

input=$(cat)

deny() {
  {
    echo "Blocked by the agent-pr skill guard."
    echo "$1"
  } >&2
  exit 2
}

command -v jq >/dev/null 2>&1 || deny "The guard needs jq to read the command, and jq isn't on PATH."
command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null) ||
  deny "The guard couldn't read the command it was given."
[ -n "$command" ] || exit 0

# The command as plain text: continuation lines joined, and quotes and
# backslashes removed, so `"gh" pr create` and `gh pr cre""ate` read as
# what the shell would run.
text=$(printf '%s\n' "$command" |
  awk '{ while (sub(/\\$/, " ")) { if ((getline next_line) <= 0) break; $0 = $0 next_line } print }' |
  tr -d "\"'\\\\")

# Each command, cut at a separator, that holds the word gh: its `pr`
# words and the verb after each, and its `api` call. This prints the
# first thing to refuse as `pr <verb>` or `api`, and nothing when all is
# well. The awk is plain POSIX, for the one macOS bundles.
found=$(printf '%s\n' "$text" | awk '
  function scan(segment,    words, count, start, index_, word, following, verb, api, pulls, mutating, field, get, value, has_method) {
    count = split(segment, words, /[[:space:]]+/)
    start = 0
    for (index_ = 1; index_ <= count; index_++) {
      if (words[index_] ~ gh_word) {
        start = index_
        break
      }
    }
    if (!start) return
    for (index_ = start + 1; index_ <= count; index_++) {
      word = words[index_]
      if (word == "pr") {
        following = words[index_ + 1]
        if (match(following, /^[A-Za-z][A-Za-z-]*/)) {
          verb = substr(following, 1, RLENGTH)
          if (verb != "list" && verb != "view" && verb != "diff" && verb != "status" && verb != "checks") {
            print "pr " verb
            exit
          }
        }
      }
      if (word == "api") api = 1
      if (index(tolower(word), "/pulls") > 0) pulls = 1
      # The method flag in each spelling: -X POST, -XPOST, -X=POST,
      # --method POST, and --method=POST, even inside a cluster of short
      # flags.
      has_method = 0
      if (word == "--method") {
        has_method = 1
        value = words[index_ + 1]
      } else if (word ~ /^--method=/) {
        has_method = 1
        value = substr(word, 10)
      } else if (word !~ /^--/ && match(word, /^-[A-Za-z]*X/)) {
        has_method = 1
        value = substr(word, RLENGTH + 1)
        sub(/^=/, "", value)
        if (value == "") value = words[index_ + 1]
      }
      if (has_method) {
        value = toupper(value)
        if (value ~ /^(POST|PATCH|PUT|DELETE)/) mutating = 1
        else if (value == "GET") get = 1
      }
      # Fields and an input file turn a call into a write unless the
      # method is named GET.
      if (word ~ /^-[fF]/ || word ~ /^--(field|raw-field|input)(=|$)/) field = 1
    }
    if (api && pulls && (mutating || (field && !get))) {
      print "api"
      exit
    }
  }
  BEGIN { gh_word = "(^|[^A-Za-z0-9_-])gh([^A-Za-z0-9_./-]|$)" }
  {
    count = split($0, parts, /&&|&[[:space:]]|&$|;|\|/)
    for (i = 1; i <= count; i++) scan(parts[i])
  }
')

case "$found" in
"") exit 0 ;;
"pr "*)
  deny "gh pr ${found#pr } skips the review. Open or update a pull request through bash .claude/skills/agent-pr/scripts/publish.sh, which takes the reviewed PR_AGENTDESC.md. A command that only mentions a gh pr verb that changes a pull request is refused too."
  ;;
*)
  deny "A gh api call that changes a pull request skips the review. Open or update a pull request through bash .claude/skills/agent-pr/scripts/publish.sh, which takes the reviewed PR_AGENTDESC.md."
  ;;
esac
