#!/usr/bin/env bash
# Shared by the scripts of the agent-merge-pr skill, on top of the
# agent-commit library: the checks judged by their latest run, the
# changed paths of a pull request, and small readers of the live state.
#
# Sourced, never run. Written for bash 3.2, which macOS still bundles.

# The environment the installed merge-pr scripts also pin: nothing the
# operator's preferences set may change what gh prints or where it points.
unset CDPATH GH_REPO GH_HOST GREP_OPTIONS
export GH_PAGER=cat GH_PROMPT_DISABLED=1

AM_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=../../agent-commit/scripts/lib.sh
. "$AM_DIR/../../agent-commit/scripts/lib.sh"

# The readiness command is a judge, so the repository's own settings file
# can't name it. The library lists the keys it keeps out of that file, and
# this adds the ones these scripts read.
AS_MACHINE_KEYS="$AS_MACHINE_KEYS merge_ready_cmd "

am_need_tools() {
  command -v gh >/dev/null 2>&1 || as_die "gh isn't installed"
  command -v jq >/dev/null 2>&1 || as_die "jq isn't installed"
}

# am_live <pr> <field>: the live value of one field of the pull request,
# read fresh. A boolean prints as true or false. Fails when gh does.
am_live() {
  local json
  json=$(gh pr view "$1" --json "$2") || return 1
  printf '%s' "$json" | jq -r --arg field "$2" '.[$field] | if . == null then "" else tostring end'
}

# am_reviewer_verdict <pr> <head> <login>: print "ok" when the latest
# decisive review by the login approved the pull request on the head
# commit, and otherwise print the reason a merge is refused. Comments and
# pending reviews decide nothing, so the latest approval, change request,
# or dismissal wins. Fails when gh or the reviews can't be read.
am_reviewer_verdict() {
  local pr=$1 head=$2 login=$3 reviews
  reviews=$(gh api "repos/{owner}/{repo}/pulls/$pr/reviews" --paginate | jq -s 'add // []') || return 1
  printf '%s' "$reviews" | jq -r --arg login "$login" --arg head "$head" '
    [.[] | select(.user.login == $login and (.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED"))]
    | last
    | if . == null then "no approving or blocking review from \($login)"
      elif .state == "CHANGES_REQUESTED" then "the latest review from \($login) requests changes"
      elif .state == "DISMISSED" then "the latest review from \($login) was dismissed"
      elif .commit_id != $head then "the approval from \($login) is for \(.commit_id[0:12]), not the current head \($head[0:12])"
      else "ok" end'
}

# am_draft_number <file>: the pull request number the first line names
# in its closing (#N), or nothing.
am_draft_number() {
  head -n 1 "$1" | sed -n 's/.*(#\([0-9][0-9]*\))[[:space:]]*$/\1/p'
}

# am_judge_checks <file>: judge the check rollup in the file, a JSON
# array or an object with a statusCheckRollup array, by the latest run
# of each check alone.
#
# A re-run leaves its predecessor in the rollup. Judging every entry
# would refuse a pull request over a failure that a later run replaced,
# and judging by position would let an old success hide a newer failure.
# Entries group by workflow and check name (a CheckRun's `name`, a
# StatusContext's `context`), the entry with the latest start time
# (`startedAt`, or `createdAt` for a status) stands for its group, and
# that entry alone is judged. A run that hasn't started has no start
# time, so it counts as the latest. On equal times the worse entry
# stands.
#
# Prints one line a check: `ok`, `pending`, or `fail`, the check's name
# with its workflow, its state, and its bare name, separated by tabs.
#   0  every latest run succeeded, or was neutral or skipped
#   1  a latest run failed
#   2  none failed, and a latest run hasn't finished
#   3  the rollup couldn't be read, or is missing or null
#   4  the rollup has no checks at all, so it's no evidence of passing
am_judge_checks() {
  local out
  out=$(jq -r '
    def is_status: (.__typename // "") == "StatusContext" or ((.name // null) == null and (.context // null) != null);
    def cname: ((if is_status then .context else .name end) // "");
    def verdict:
      if is_status then
        ((.state // "PENDING") | ascii_upcase | if . == "SUCCESS" then "ok" elif . == "PENDING" or . == "EXPECTED" then "pending" else "fail" end)
      elif ((.status // "COMPLETED") | ascii_upcase) != "COMPLETED" then "pending"
      else ((.conclusion // "") | ascii_upcase | if . == "SUCCESS" or . == "NEUTRAL" or . == "SKIPPED" then "ok" elif . == "" then "pending" else "fail" end)
      end;
    def rank: verdict | if . == "fail" then 2 elif . == "pending" then 1 else 0 end;
    def started:
      ((.startedAt // .createdAt // "") | if startswith("0001") then "" else . end) as $t
      | if $t == "" and verdict == "pending" then "9999-12-31T23:59:59Z" else $t end;
    def detail:
      if is_status then (.state // "PENDING")
      else ((.status // "COMPLETED") + (if .conclusion then "/" + .conclusion else "" end)) end;
    (if type == "array" then .
     elif type == "object" and (.statusCheckRollup | type) == "array" then .statusCheckRollup
     else error("no rollup") end)
    | group_by([(.workflowName // ""), cname])
    | map(sort_by([started, rank]) | last)
    | .[]
    | [verdict, (((.workflowName // "") | if . == "" then "" else . + " / " end) + cname), detail, cname]
    | @tsv
  ' "$1" 2>/dev/null) || {
    echo "unreadable	the check rollup	not JSON this can read"
    return 3
  }
  if [ -z "$out" ]; then
    echo "missing	any check	the rollup lists no checks"
    return 4
  fi
  printf '%s\n' "$out"
  printf '%s\n' "$out" | awk -F'\t' '
    $1 == "fail" { failed = 1 }
    $1 == "pending" { pending = 1 }
    END { exit failed ? 1 : (pending ? 2 : 0) }
  '
}

# am_required_checks <base> <base-oid> <default-branch>: print the checks
# the base branch requires, one name a line. They come from the rulesets
# GitHub applies to the branch, and from the ruleset files in the
# `ruleset_dir` directory as the base commit has them, so a pull request
# can't drop a requirement by editing its own copy. A file counts when
# it's an active branch ruleset whose conditions include the base and
# don't exclude it. Returns 1 when either source can't be read, and
# prints nothing when neither requires a check, which the caller
# refuses.
am_required_checks() {
  local base=$1 base_oid=$2 default=$3 dir file
  dir=$(as_conf ruleset_dir .github/rulesets)
  {
    gh api "repos/{owner}/{repo}/rules/branches/$base" \
      --jq '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context' || return 1
    git ls-tree --name-only "$base_oid" -- "$dir/" >"$(as_state_dir)/merge.rulesets" || return 1
    while IFS= read -r file; do
      case "$file" in *.json) ;; *) continue ;; esac
      git show "$base_oid:$file" | am_ruleset_checks "$base" "$default" || return 1
    done <"$(as_state_dir)/merge.rulesets"
  } >"$(as_state_dir)/merge.required.raw" || return 1
  sed 's/^[[:space:]]*//; s/[[:space:]]*$//' "$(as_state_dir)/merge.required.raw" | awk 'NF && !seen[$0]++'
}

# am_ruleset_checks <base> <default-branch>: read one ruleset as JSON on
# standard input, and print its required checks when it applies to the
# base branch.
am_ruleset_checks() {
  local base=$1 default=$2 rows kind value included=no excluded=no
  rows=$(jq -r '
    select(.target == "branch" and .enforcement == "active")
    | ((.conditions.ref_name.include // [])[] | "include\t" + .),
      ((.conditions.ref_name.exclude // [])[] | "exclude\t" + .),
      (.rules[]? | select(.type == "required_status_checks") | .parameters.required_status_checks[].context | "check\t" + .)
  ') || return 1
  while IFS=$'\t' read -r kind value; do
    case "$kind" in
    include) ! am_ref_matches "$value" "$base" "$default" || included=yes ;;
    exclude) ! am_ref_matches "$value" "$base" "$default" || excluded=yes ;;
    esac
  done <<EOF
$rows
EOF
  [ "$included" = yes ] && [ "$excluded" = no ] || return 0
  printf '%s\n' "$rows" | sed -n 's/^check\t//p'
}

# am_ref_matches <pattern> <branch> <default-branch>: succeed when a
# ruleset's ref pattern covers the branch.
am_ref_matches() {
  case "$1" in
  "~ALL") return 0 ;;
  "~DEFAULT_BRANCH") [ "$2" = "$3" ] ;;
  *)
    # A ruleset pattern is an fnmatch glob over the full ref: `**` crosses
    # a slash, and `*` and `?` don't.
    printf '%s\n' "refs/heads/$2" | grep -q -E -- "^$(printf '%s' "$1" |
      sed -e 's/[][\.^$+(){}|]/\\&/g' -e 's/\*\*/@@ANY@@/g' -e 's/\*/[^\/]*/g' -e 's/?/[^\/]/g' -e 's/@@ANY@@/.*/g')\$"
    ;;
  esac
}

# am_unmet_checks <judged> <required>: print each required check that
# has no passing latest run among the judged lines, one a line. A
# required check passes only on success: a skipped or neutral run never
# ran its job, so it's no evidence. It counts as met when every judged
# line with its bare name succeeded, and there is at least one.
am_unmet_checks() {
  awk -F'\t' 'NR == FNR { if ($1 == "ok" && $3 ~ /SUCCESS$/) seen[$4] = 1; else bad[$4] = 1; next }
    !($0 in seen) || ($0 in bad) { print }' "$1" "$2"
}

# am_changed_paths <pr> <base> <head> <file>: write the paths the pull
# request changes to the file, one a line, from the commits themselves:
# the base branch and the pull request's head ref, fetched, and the diff
# GitHub shows for it. A rename lists both names. Fails when the commits
# can't be read or the diff names no path, so a caller never mistakes an
# unreadable change for one that touches nothing.
am_changed_paths() {
  local pr=$1 base=$2 head=$3 file=$4
  git fetch --quiet origin "+refs/heads/$base:refs/remotes/origin/$base" "refs/pull/$pr/head" >/dev/null 2>&1 || return 1
  git rev-parse --verify --quiet "$head^{commit}" >/dev/null || return 1
  as_paths "$file" "refs/remotes/origin/$base...$head"
  [ -s "$file" ]
}

# am_loosening <base> <head> <file>: write to the file each line of the
# pull request's diff that loosens a gate in a way a script can see, one
# finding a line, or nothing. It reads three things:
#   - an added line anywhere with a suppression marker, such as a noqa,
#     a type or pyright ignore, a no-cover pragma, an eslint, cspell,
#     Vale, ShellCheck, rumdl, or markdownlint switch, a nolint, a C#
#     pragma or SuppressMessage;
#   - an added line in a gate or lint configuration file that names an
#     exclusion, an ignore, a disabled rule, a skip, or an allowance;
#   - a removed line in such a file that registered a hook or a lint
#     task.
# It's a backstop: a loosening it can't see is the author's and the
# reviewers' to flag. Returns 1 when the diff can't be read.
am_loosening() {
  local base=$1 head=$2 out=$3 markers configs
  markers=$(as_conf loosening_markers)
  configs=$(as_conf gate_config_paths)
  [ -n "$markers" ] && [ -n "$configs" ] || return 1
  git -c core.quotePath=false diff --no-ext-diff --no-textconv --text --unified=0 --no-color \
    "refs/remotes/origin/$base...$head" >"$out.diff" || return 1
  awk -v markers="$markers" -v configs="$configs" '
    /^--- / { old = substr($0, 5); sub(/^a\//, "", old); next }
    /^\+\+\+ / {
      file = substr($0, 5); sub(/^b\//, "", file)
      if (file == "/dev/null") file = old
      next
    }
    /^@@/ { next }
    /^\+/ {
      line = substr($0, 2)
      if (tolower(line) ~ markers) print file ": adds a suppression: " line
      else if (file ~ configs && tolower(line) ~ /exclude|ignore|disable|skip|allow/) print file ": adds an exclusion: " line
      next
    }
    /^-/ {
      line = substr($0, 2)
      if (file ~ configs && line ~ /^[[:space:]]*- id:|"mise run /) print file ": removes a hook or task: " line
    }
  ' "$out.diff" >"$out" || return 1
  unlink "$out.diff"
}

# am_kept_ignored <worktree>: print each ignored path in the worktree
# that isn't a disposable cache, one a line. git's cleanliness check
# and `git worktree remove` both pass over ignored files, and some of
# them are local configuration or work, such as an .env file, that
# nothing can regenerate. The `disposable_ignored` setting is the
# pattern for what may go. Returns 1 when the status can't be read or
# the setting is empty.
am_kept_ignored() {
  local disposable
  disposable=$(as_conf disposable_ignored)
  [ -n "$disposable" ] || return 1
  git -C "$1" -c core.quotePath=false status --porcelain --ignored=matching --untracked-files=normal >"$(as_state_dir)/merge.ignored" || return 1
  # grep exits 1 when every ignored path is disposable, and 2 when the
  # pattern is broken, which has to keep the worktree, never free it.
  local status=0
  sed -n 's/^!! //p' "$(as_state_dir)/merge.ignored" | grep -v -E -- "$disposable" || status=$?
  [ "$status" -le 1 ]
}

# am_bad_patterns: print the name of each pattern setting that is empty
# or isn't a pattern grep can read, one a line. A broken pattern would
# read as "nothing matches", which fails open for every check that uses
# one, so the merge checks them all before it does anything.
am_bad_patterns() {
  local key value status
  for key in owner_paths golden_paths loosening_markers gate_config_paths disposable_ignored; do
    value=$(as_conf "$key")
    status=0
    [ -n "$value" ] && { grep -E -- "$value" </dev/null >/dev/null 2>&1 || status=$?; } || status=2
    [ "$status" -le 1 ] || echo "$key"
  done
}

# am_ready_cmd: print the readiness command's path, or nothing.
am_ready_cmd() {
  as_path "$(as_conf merge_ready_cmd)"
}
