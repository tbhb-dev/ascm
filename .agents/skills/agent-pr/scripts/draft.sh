#!/usr/bin/env bash
# Shared by the agent-pr scripts: reads the parts of PR_AGENTDESC.md that
# review.sh, check.sh, and publish.sh all need. The frontmatter parser is
# line-based, as the installed pr skill's is, because the validator has
# already settled its shape.
#
# Sourced after lib.sh, never run. Written for bash 3.2.

# pr_fm_end <draft>: the line number of the closing --- of the
# frontmatter, or nothing when the draft has none.
pr_fm_end() {
  awk 'NR == 1 && $0 != "---" { exit } NR > 1 && $0 == "---" { print NR; exit }' "$1"
}

# pr_field <draft> <key>: the value of a frontmatter key, quotes removed.
pr_field() {
  local end
  end=$(pr_fm_end "$1")
  [ -n "$end" ] || return 0
  awk -v key="$2" -v last="$end" '
    NR >= last { exit }
    index($0, key ":") == 1 {
      value = substr($0, length(key) + 2)
      gsub(/^[ \t]+|[ \t]+$/, "", value)
      gsub(/^["\047]|["\047]$/, "", value)
      print value
      exit
    }
  ' "$1"
}

# pr_sequence <flow-sequence>: one entry per line from a YAML flow
# sequence such as [a, "b c"].
pr_sequence() {
  printf '%s' "$1" | sed 's/^\[//; s/\]$//' | tr ',' '\n' |
    sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//' |
    grep -v '^$' || true
}

# pr_title <draft>: the text of the first level 1 heading after the
# frontmatter.
pr_title() {
  local end
  end=$(pr_fm_end "$1")
  [ -n "$end" ] || return 0
  awk -v end="$end" 'NR > end && /^# / { print substr($0, 3); exit }' "$1"
}

# pr_body <draft>: everything after the title line. GitHub renders the
# title itself, and the frontmatter is this workflow's own bookkeeping,
# so neither belongs in what gets published.
pr_body() {
  local end
  end=$(pr_fm_end "$1")
  [ -n "$end" ] || return 0
  awk -v end="$end" '
    NR <= end { next }
    !titled && /^# / { titled = 1; next }
    titled && (NF || started) { started = 1; print }
  ' "$1"
}

# pr_base_ref <base>: origin's copy of the base branch when it exists,
# else the local branch.
pr_base_ref() {
  if git rev-parse --verify --quiet "refs/remotes/origin/$1" >/dev/null; then
    echo "origin/$1"
  else
    echo "$1"
  fi
}

# pr_trim <text>: the text without surrounding whitespace.
pr_trim() {
  local s=$1
  s=${s#"${s%%[![:space:]]*}"}
  s=${s%"${s##*[![:space:]]}"}
  printf '%s' "$s"
}
