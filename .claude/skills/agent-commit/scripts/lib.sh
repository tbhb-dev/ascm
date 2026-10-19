#!/usr/bin/env bash
# Shared by the agent-commit, agent-pr, and agent-merge-pr skills: the
# configuration lookup, the independent
# review and its stamp, and the report to the planner.
#
# Sourced, never run. Written for bash 3.2, which macOS still bundles.
#
# Configuration comes from three places, first hit wins:
#   1. the environment, AGENT_SKILLS_<KEY> in capitals
#   2. the machine's file, ~/.config/agent-skills/<repository>.conf
#   3. the repository's file, agent-skills.conf beside this skill
# Machine paths belong in the first two. The machine's file is optional.
# Each file holds `key = value` lines, with `#` comments.

# Before anything else: a CDPATH would make `cd` print a path, and the
# directory below would come back as two lines.
unset CDPATH GREP_OPTIONS
export LC_ALL=C
AS_LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

as_die() {
  echo "$*" >&2
  exit 1
}

as_root() {
  git rev-parse --show-toplevel
}

as_git_dir() {
  git rev-parse --absolute-git-dir
}

# The name the machine's settings file is keyed by: the directory of
# the main checkout, whichever worktree the caller stands in.
as_repo_name() {
  local common
  common=$(git rev-parse --path-format=absolute --git-common-dir)
  basename "$(dirname "$common")"
}

as_state_dir() {
  local dir
  dir=$(as_git_dir)/agent-skills
  mkdir -p "$dir"
  echo "$dir"
}

as_digest() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

_as_conf_file() {
  # Print the value of key $2 in file $1, or fail when the key is absent.
  [ -f "$1" ] || return 1
  awk -v key="$2" '
    /^[ \t]*#/ || !/=/ { next }
    {
      k = $0
      sub(/=.*/, "", k)
      gsub(/^[ \t]+|[ \t]+$/, "", k)
      if (k != key) next
      v = $0
      sub(/^[^=]*=/, "", v)
      gsub(/^[ \t]+|[ \t]+$/, "", v)
      print v
      found = 1
      exit
    }
    END { exit found ? 0 : 1 }
  ' "$1"
}

# Keys the repository's own settings file can't set. The reviewer decides
# for the skill, and the agent account says who may act. A commit to the
# repository could otherwise replace the judge of the next one, or the
# account its own publish accepts. They come from the environment or the
# machine's file alone.
AS_MACHINE_KEYS=" review_cmd gh_login "

# as_conf <key> [default]: the configured value, or the default.
as_conf() {
  local key=$1 default=${2:-} env_name value
  env_name=AGENT_SKILLS_$(printf '%s' "$key" | tr '[:lower:]' '[:upper:]')
  if [ -n "${!env_name+set}" ]; then
    printf '%s\n' "${!env_name}"
    return 0
  fi
  if value=$(_as_conf_file "${XDG_CONFIG_HOME:-$HOME/.config}/agent-skills/$(as_repo_name).conf" "$key"); then
    printf '%s\n' "$value"
    return 0
  fi
  case "$AS_MACHINE_KEYS" in
  *" $key "*) ;;
  *)
    if value=$(_as_conf_file "$AS_LIB_DIR/../agent-skills.conf" "$key"); then
      printf '%s\n' "$value"
      return 0
    fi
    ;;
  esac
  printf '%s\n' "$default"
}

# A configured path may start with ~ or be relative to the repository.
as_path() {
  local home_prefix
  home_prefix='~'/
  case "$1" in
  "") ;;
  "$home_prefix"*) printf '%s\n' "$HOME/${1#??}" ;;
  /*) printf '%s\n' "$1" ;;
  *) printf '%s\n' "$(as_root)/$1" ;;
  esac
}

# as_owner_path <file>: succeed when a path in the file matches the
# repository's owner-path pattern, an extended regular expression. The
# file holds one path a line. It is a file, and not a pipe, because a
# reader that stops at its first match would kill the writer, and the
# failed pipe would read as no match. A pattern grep can't compile
# stops the run: it must not read as no owner path.
as_owner_path() {
  local pattern status
  pattern=$(as_conf owner_paths)
  [ -n "$pattern" ] || return 1
  status=0
  grep -E -- "$pattern" "$1" >/dev/null 2>&1 || status=$?
  case "$status" in
  0 | 1) return "$status" ;;
  *) as_die "owner_paths isn't a pattern grep can read: $pattern" ;;
  esac
}

# as_paths <file> <git diff arguments>: write the paths a diff names to
# the file, one a line and unquoted. Git quotes a name with a byte
# outside ASCII unless told not to, and a quoted name would never match
# an owner path.
as_paths() {
  local file=$1
  shift
  git -c core.quotePath=false diff --name-only --no-renames -z "$@" | tr '\0' '\n' | sort >"$file"
}

# as_in_progress: print the name of a merge, cherry-pick, revert, or
# rebase that is under way, or nothing.
as_in_progress() {
  local git_dir marker
  git_dir=$(as_git_dir)
  for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
    [ -e "$git_dir/$marker" ] && echo "$marker" && return 0
  done
  return 0
}

# as_mail <kind> <pr-or-empty> <body>: report to the planner's mailbox. A
# repository without a mailbox prints the report instead. A failed send
# never undoes the work it reports, so this always succeeds.
as_mail() {
  local kind=$1 pr=$2 body=$3 cmd to from
  cmd=$(as_path "$(as_conf mail_cmd)")
  to=$(as_conf mail_to)
  from=$(as_conf mail_from "${AGENT_MAIL_AGENT:-}")
  if [ -z "$cmd" ] || [ ! -x "$cmd" ] || [ -z "$to" ]; then
    printf 'REPORT (%s, no mailbox configured):\n%s\n' "$kind" "$body"
    return 0
  fi
  set -- send --to "$to" --kind "$kind" --body "$body"
  [ -n "$from" ] && set -- "$@" --from "$from"
  [ -n "$pr" ] && set -- "$@" --pr "$pr"
  if ! "$cmd" "$@"; then
    printf 'REPORT NOT SENT (%s). Send it by hand:\n%s\n' "$kind" "$body" >&2
  fi
  return 0
}

# Whether git's output says signing failed. The session's environment
# decides how a commit signs. A failure means that path is down, and no
# retry helps until someone restores it.
as_signing_refused() {
  grep -E -q "^(error: gpg failed to sign the data|fatal: failed to write commit object)" "$1"
}

# as_signing_key: print the public key that signs commits here, as the
# key type and the key, or nothing when the configuration names none
# this can read. user.signingkey is either the key itself, with or
# without a `key::` prefix, or the path of a key file whose public half
# sits beside it with a `.pub` suffix.
as_signing_key() {
  local key
  key=$(git config --get user.signingkey || true)
  case "$key" in
  "") return 0 ;;
  key::*) key=${key#key::} ;;
  ssh-* | ecdsa-* | sk-*) ;;
  *)
    [ "${key#\~/}" = "$key" ] || key=$HOME/${key#\~/}
    case "$key" in *.pub) ;; *) key=$key.pub ;; esac
    [ -f "$key" ] || return 0
    key=$(head -n 1 "$key")
    ;;
  esac
  printf '%s\n' "$key" | awk 'NF >= 2 { print $1, $2 }'
}

# as_signed_by <commit> <key> <file>: succeed when the commit has a
# good SSH signature from the key. The file is scratch space for the
# allowed-signers list. Verification runs ssh-keygen itself, whatever
# program the configuration signs with.
as_signed_by() {
  printf 'agent-commit %s\n' "$2" >"$3"
  git -c gpg.ssh.program=ssh-keygen -c gpg.ssh.allowedSignersFile="$3" \
    verify-commit "$1" >/dev/null 2>&1
}

# as_missing_hooks: print each hook the configuration requires that
# isn't installed as this repository's own, one a line. A hook counts
# when git would run it and it holds the configured marker, the line
# the repository's hook manager writes into each hook it installs.
as_missing_hooks() {
  local dir hook marker
  dir=$(git rev-parse --git-path hooks)
  marker=$(as_conf hook_marker "")
  for hook in $(as_conf required_hooks "commit-msg pre-commit post-commit"); do
    if [ ! -x "$dir/$hook" ] || ! grep -q -F -- "$marker" "$dir/$hook"; then
      echo "$hook"
    fi
  done
}

# --- the independent review -------------------------------------------
#
# A review is a fresh agent process that reads the draft cold. It runs
# the same way from a peer session, a subagent, and a headless run,
# because it's a child process and depends on no harness hook. The
# caller never handles the verdict: this code reads it and writes the
# stamp, which binds the verdict to the exact bytes reviewed.
#
# The reviewer has no shell and can't write. The calling script gathers
# what the review needs into files, and the reviewer's whole tool set
# reads and searches. Text planted in a draft or a diff has no tool to
# act through.
#
# The stamp is an honesty boundary, as the installed skills' stamp is. A
# process running as the same user can write the file by hand. What it
# buys is that the sanctioned path can't publish unreviewed bytes by
# accident, and the cross-vendor review of the pull request is the check
# an author can't write for itself.

# as_review_prompt <criteria-file> <arguments> <inputs-dir>: the
# installed review skill's own instructions, without their frontmatter,
# with the arguments it expects filled in, and with the gathered inputs
# listed in place of the commands it would run.
as_review_prompt() {
  awk -v args="$2" '
    NR == 1 && /^---$/ { front = 1; next }
    front && /^---$/ { front = 0; next }
    front { next }
    {
      while ((i = index($0, "$ARGUMENTS")) > 0) {
        $0 = substr($0, 1, i - 1) args substr($0, i + 10)
      }
      print
    }
  ' "$1"
  cat <<EOF

## Inputs for this run

You run as a separate process with no shell. You can't run the commands
this skill lists, and you don't need to: their output is already in the
files below. Read every one of them before you judge anything. You may
also read and search other files in the repository for context.

$(find "$3" -type f | sort)

## Reply format for this run

A script reads your reply, and no hook does. Reply with the verdict
block alone, as plain text: no code fence, no heading, and no sentence
before or after it. A passing reply is the single line VERDICT: PASS and
nothing else. Any other reply that isn't a findings list counts as no
verdict at all.
EOF
}

# as_run_reviewer: read the prompt on standard input, print the reply.
# `review_cmd` replaces the default, a headless Claude run on the model
# and effort the configuration names. No harness default is ever used.
# The default reviewer's whole tool set is Read, Grep, and Glob.
as_run_reviewer() {
  local custom model effort
  custom=$(as_conf review_cmd)
  if [ -n "$custom" ]; then
    bash -c "$custom"
    return
  fi
  model=$(as_conf review_model)
  effort=$(as_conf review_effort)
  [ -n "$model" ] && [ -n "$effort" ] || as_die "review_model and review_effort must both be set; no default model is used"
  claude -p --model "$model" --effort "$effort" --no-session-persistence \
    --tools Read Grep Glob --allowedTools Read Grep Glob
}

# as_verdict <reply-file>: print `pass`, `changes`, or `unreadable`.
#
# The format is strict, because a reply can quote the draft or the diff,
# and either can contain the words of a passing verdict. A pass is a
# reply whose only non-blank line is the verdict. Changes are a reply
# whose first non-blank line says so. Anything else is no verdict.
as_verdict() {
  # A NUL or another control byte can end a line early for one awk and
  # not for another. A reply with one is no verdict.
  if [ "$(tr -d '\000-\010\013\014\016-\037' <"$1" | wc -c)" -ne "$(wc -c <"$1")" ]; then
    echo unreadable
    return 0
  fi
  awk '
    {
      sub(/\r$/, "")
      sub(/[ \t]+$/, "")
      if ($0 == "") next
      line[++n] = $0
    }
    END {
      if (n == 1 && line[1] == "VERDICT: PASS") print "pass"
      else if (n >= 1 && line[1] == "VERDICT: CHANGES REQUIRED") print "changes"
      else print "unreadable"
    }
  ' "$1"
}

# as_review <kind> <draft> <snapshot> <criteria-file> <arguments> <inputs-dir> <still-current> [binding...]
#
# Review a snapshot and stamp it on a pass. The caller gathers one
# consistent snapshot into the inputs directory first: a copy of the
# draft, and whatever else the review reads. The stamp's digest comes
# from that copy, the bytes the reviewer was given, and never from the
# live draft. Each binding is a `name value` pair the stamp records
# beside the digest, such as the staged tree, taken when the snapshot
# was. `still-current` is a function of the caller's that succeeds
# while the live state still matches those bindings. Before anything is
# stamped, the live draft has to match the snapshot and that function
# has to succeed. Findings always block. Past the round bound the
# review stops and reports to the planner, and nothing is stamped.
#   0  stamped: the review passed
#   2  changes required; revise and review again
#   3  blocked: findings came back more times than the bound allows
#   4  the reply had no verdict a script could read
#   5  the draft changed while the review ran
as_review() {
  local kind=$1 draft=$2 snapshot=$3 criteria=$4 arguments=$5 inputs=$6 still_current=$7
  shift 7
  local state reply rounds_file rounds max before verdict
  state=$(as_state_dir)
  reply=$state/$kind.review
  rounds_file=$state/$kind.rounds
  max=$(as_conf review_max_rounds 3)

  [ -s "$draft" ] || as_die "review: $draft is missing or empty"
  [ -f "$criteria" ] || as_die "review: no review criteria at $criteria"
  [ -d "$inputs" ] || as_die "review: no gathered inputs at $inputs"
  [ -s "$snapshot" ] || as_die "review: no snapshot of the draft at $snapshot"
  _as_forget "$kind" stamp
  before=$(as_digest "$snapshot")
  case "$max" in
  "" | *[!0-9]*) as_die "review: review_max_rounds isn't a number: $max" ;;
  esac
  # A blocked review stays blocked until someone clears it. A count that
  # isn't a number reads as past the bound.
  rounds=0
  [ -f "$rounds_file" ] && rounds=$(cat "$rounds_file")
  case "$rounds" in
  "" | *[!0-9]*) rounds=$((max + 1)) ;;
  esac
  if [ "$rounds" -gt "$max" ]; then
    echo "review: BLOCKED. Findings came back $rounds times, past the bound of $max, and the planner has the last ones. Wait for word before reviewing this draft again."
    return 3
  fi

  as_review_prompt "$criteria" "$arguments" "$inputs" | as_run_reviewer >"$reply" || {
    echo "review: the reviewer failed to run" >&2
    return 4
  }
  if [ "$(as_digest "$draft")" != "$before" ] || [ "$(as_digest "$snapshot")" != "$before" ]; then
    echo "review: $(basename "$draft") isn't the text the reviewer was given; review it again" >&2
    return 5
  fi
  if ! "$still_current"; then
    echo "review: what the review read changed while it ran; review it again" >&2
    return 5
  fi

  verdict=$(as_verdict "$reply")
  case "$verdict" in
  pass)
    _as_stamp "$state/$kind.stamp" "$before" "$@"
    echo "VERDICT: PASS (stamped)"
    return 0
    ;;
  changes)
    cat "$reply"
    rounds=$((rounds + 1))
    echo "$rounds" >"$rounds_file"
    echo
    if [ "$rounds" -le "$max" ]; then
      echo "review: finding round $rounds of $max. Fix the findings and review again."
      return 2
    fi
    echo "review: BLOCKED. Findings came back $rounds times, past the bound of $max. Nothing is stamped."
    as_mail blocker "" "review-bound: the independent review of $(basename "$draft") in $(as_repo_name) on $(git branch --show-current) returned findings $rounds times, past the bound of $max. Nothing was stamped. The last findings:

$(cat "$reply")"
    return 3
    ;;
  *)
    cat "$reply"
    echo "review: no verdict a script can read; nothing stamped" >&2
    return 4
    ;;
  esac
}

_as_stamp() {
  local stamp=$1 digest=$2
  shift 2
  # The bindings come first and each is one line. The reviewer's name
  # is free text from the configuration, so it goes last, on one line,
  # where it can't pass for a binding.
  {
    echo "draft $digest"
    while [ $# -ge 2 ]; do
      printf '%s %s\n' "$1" "$(printf '%s' "$2" | tr '\n\r' '  ')"
      shift 2
    done
    echo "at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "reviewer $(as_conf review_cmd "claude:$(as_conf review_model):$(as_conf review_effort)" | tr '\n\r' '  ')"
  } >"$stamp"
}

# _as_forget <kind> <suffix>...: delete a kind's named state files. The
# directory comes from git, and each name is spelled out here.
_as_forget() {
  local dir kind=$1 suffix
  shift
  dir=$(as_state_dir)
  [ -n "$dir" ] && [ -n "$kind" ] || return 0
  for suffix in "$@"; do
    [ -f "$dir/$kind.$suffix" ] && unlink "$dir/$kind.$suffix"
  done
  return 0
}

# as_stamp_value <kind> <name>: the value a stamp recorded, or nothing.
as_stamp_value() {
  local stamp
  stamp=$(as_state_dir)/$1.stamp
  [ -f "$stamp" ] || return 0
  sed -n "s/^$2 //p" "$stamp" | head -n 1
}

# as_require_stamp <kind> <draft> [name value]...: fail unless the stamp
# covers these exact bytes and each named value still matches.
as_require_stamp() {
  local kind=$1 draft=$2 stamp recorded
  shift 2
  stamp=$(as_state_dir)/$kind.stamp
  [ -f "$stamp" ] || as_die "no independent review has cleared $(basename "$draft"); run the review step first"
  recorded=$(as_stamp_value "$kind" draft)
  [ "$recorded" = "$(as_digest "$draft")" ] || as_die "$(basename "$draft") changed after its review; review it again"
  while [ $# -ge 2 ]; do
    recorded=$(as_stamp_value "$kind" "$1")
    [ "$recorded" = "$2" ] || as_die "the $1 changed after the review (reviewed ${recorded:-nothing}, now $2); review again"
    shift 2
  done
}

# as_new_inputs <kind>: make and print a fresh directory for one review's
# gathered inputs. Each review gets its own, so one run can't overwrite
# what another's reviewer is reading.
as_new_inputs() {
  mktemp -d "$(as_state_dir)/$1.inputs.XXXXXX"
}

# as_unblock <kind>: clear a review the bound stopped, on the planner's
# word that the draft may be reviewed again.
as_unblock() {
  _as_forget "$1" rounds
  echo "the review bound for $1 is cleared"
}

# as_clear <kind>: drop a kind's review state once its work has landed,
# with every inputs directory the kind's reviews left.
as_clear() {
  local dir file
  _as_forget "$1" stamp review rounds
  for dir in "$(as_state_dir)/$1".inputs.*; do
    # A real directory of this state directory, never a link to another.
    [ -d "$dir" ] && [ ! -L "$dir" ] || continue
    for file in "$dir"/*; do
      [ -f "$file" ] && [ ! -L "$file" ] && unlink "$file"
    done
    rmdir "$dir" 2>/dev/null || true
  done
}
