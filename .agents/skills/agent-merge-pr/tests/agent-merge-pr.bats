#!/usr/bin/env bats
# The agent-merge-pr scripts against a scratch repository and a bare
# remote, with a stand-in gh that serves canned JSON and records its
# calls, and a stand-in reviewer, readiness command, and mailbox. No
# test reaches a model, GitHub, or the network.
#
# A failed `[[ ]]` doesn't stop a test under the bash that macOS bundles,
# so each one ends in `|| false`. A `!` command never stops a test, so
# the tests count matches with `run` instead.

setup() {
  scripts=$(cd "$BATS_TEST_DIRNAME/../scripts" && pwd)
  tmp=$BATS_TEST_TMPDIR
  repo=$tmp/widgets
  origin=$tmp/origin.git
  fx=$tmp/gh
  mkdir -p "$fx" "$tmp/bin"

  # Nothing from the caller's git configuration.
  unset "${!GIT_CONFIG_@}" SSH_AUTH_SOCK GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
  # The project the hook command names: the checkout these tests are in.
  CLAUDE_PROJECT_DIR=$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)
  export CLAUDE_PROJECT_DIR
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export XDG_CONFIG_HOME=$tmp/xdg

  git init -q --bare -b main "$origin"
  mkdir -p "$repo"
  cd "$repo"
  git init -q -b main
  git config user.name "Test Agent"
  git config user.email "agent@example.com"
  git config commit.gpgsign false
  printf 'SQUASH_AGENTMSG\n.claude/worktrees/\n' >.gitignore
  printf 'one\n' >notes.txt
  mkdir owned
  printf 'rule\n' >owned/rule.txt
  printf 'repos:\n  - repo: local\n    hooks:\n      - id: lint\n      - id: spell\n' >.pre-commit-config.yaml
  mkdir -p .github/rulesets
  ruleset main lint >.github/rulesets/main.json
  git add -- .gitignore notes.txt owned/rule.txt .pre-commit-config.yaml .github/rulesets/main.json
  git commit -q -m "chore: seed the repository"
  git remote add origin "$origin"
  git push -q origin main
  git checkout -q -b feature
  printf 'two\n' >>notes.txt
  git commit -q -am "feat: add a second note" -m "The first note stood alone."
  git push -q origin feature
  head=$(git rev-parse HEAD)
  git push -q origin "$head:refs/pull/7/head"
  git checkout -q main
  git worktree add -q .claude/worktrees/feature feature

  write_pr
  install_gh
  printf '0\n' >"$tmp/ready-status"
  cat >"$tmp/ready" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$tmp/ready.calls"
exit "\$(cat "$tmp/ready-status")"
EOF
  cat >"$tmp/mail" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$tmp/mail.calls"
EOF
  chmod +x "$tmp/ready" "$tmp/mail"

  printf -- '---\nname: review\n---\n\nReview the draft in $ARGUMENTS.\n' >"$tmp/criteria.md"
  export PATH=$tmp/bin:$PATH
  export GH_FX=$fx GH_ORIGIN=$origin
  printf 'agent-account\n' >"$fx/login"
  export AGENT_SKILLS_REVIEW_SQUASH_CRITERIA=$tmp/criteria.md
  export AGENT_SKILLS_REVIEW_CMD="cat >'$tmp/prompt'; cat '$tmp/reply'"
  export AGENT_SKILLS_REVIEW_MAX_ROUNDS=2
  export AGENT_SKILLS_LINT_SQUASH_CMD=true
  export AGENT_SKILLS_GH_LOGIN=agent-account
  export AGENT_SKILLS_REQUIRED_REVIEWER=tbhb-agent-reviewer
  review_as tbhb-agent-reviewer APPROVED @head
  export AGENT_SKILLS_MERGE_READY_CMD=$tmp/ready
  export AGENT_SKILLS_MAIL_CMD=$tmp/mail
  export AGENT_SKILLS_MAIL_TO=planner
  export AGENT_SKILLS_MAIL_FROM=claude-test
  export AGENT_SKILLS_OWNER_PATHS='^owned/'
  export AGENT_SKILLS_MERGE_VERIFY_TRIES=1

  write_draft
}

# --- the stand-ins -----------------------------------------------------

# add_main_files <path> <source>...: commit files onto main on the remote,
# as a later commit on the base.
add_main_files() {
  local work=$tmp/main-work
  [ -d "$work" ] || git clone -q "$origin" "$work"
  git -C "$work" fetch -q origin main && git -C "$work" checkout -q -B main origin/main
  while [ $# -ge 2 ]; do
    mkdir -p "$(dirname "$work/$1")"
    cp "$2" "$work/$1"
    git -C "$work" add -- "$1"
    shift 2
  done
  git -C "$work" -c user.name=other -c user.email=other@example.com -c commit.gpgsign=false commit -q --allow-empty -m "build: change the base"
  git -C "$work" push -q origin main
}

# push_main_ruleset <check>...: make main's ruleset file require the
# checks.
push_main_ruleset() {
  ruleset main "$@" >"$tmp/main-ruleset.json"
  add_main_files .github/rulesets/main.json "$tmp/main-ruleset.json"
}

# ruleset <name> <check>...: an active ruleset on the default branch that
# requires the checks, as the repository stores it.
ruleset() {
  local name=$1
  shift
  jq -n --arg name "$name" '$ARGS.positional as $checks | {
    name: $name, target: "branch", enforcement: "active",
    conditions: {ref_name: {include: ["~DEFAULT_BRANCH"], exclude: []}},
    rules: [{type: "required_status_checks", parameters: {required_status_checks: [$checks[] | {context: ., integration_id: 15368}]}}]
  }' --args "$@"
}

# The pull request as GitHub would serve it.
write_pr() {
  jq -n --arg head "$head" '{
    number: 7,
    title: "feat: add a second note",
    body: "Why: the first note stood alone.",
    state: "OPEN",
    isDraft: false,
    mergeStateStatus: "CLEAN",
    baseRefName: "main",
    headRefName: "feature",
    headRefOid: $head,
    isCrossRepository: false,
    labels: [],
    statusCheckRollup: [
      {__typename: "CheckRun", name: "lint", workflowName: "ci", status: "COMPLETED", conclusion: "SUCCESS", startedAt: "2026-10-14T10:00:00Z"}
    ],
    commits: [{oid: $head, messageHeadline: "feat: add a second note", messageBody: "The first note stood alone."}],
    files: [{path: "notes.txt", additions: 1, deletions: 0}],
    mergeCommit: null
  }' >"$fx/pr-7.json"
}

# Start the reviews on the pull request: <login> <state> <commit>, where
# the commit @head means whatever head the pull request has when read.
review_as() {
  jq -n --arg u "$1" --arg s "$2" --arg c "$3" '[{user: {login: $u}, state: $s, commit_id: $c}]' >"$fx/reviews.json"
}

# Add one more review after the existing ones.
more_review() {
  jq --arg u "$1" --arg s "$2" --arg c "$3" '. + [{user: {login: $u}, state: $s, commit_id: $c}]' "$fx/reviews.json" >"$fx/edit.tmp"
  mv "$fx/edit.tmp" "$fx/reviews.json"
}

edit_pr() {
  jq "$1" "$fx/pr-7.json" >"$fx/edit.tmp"
  mv "$fx/edit.tmp" "$fx/pr-7.json"
}

# A gh that serves $GH_FX/pr-<n>.json, filtered to the fields asked for,
# records every call, and merges into the bare remote.
install_gh() {
  cat >"$tmp/bin/gh" <<'EOF'
#!/usr/bin/env bash
fx=$GH_FX
printf '%s\n' "$*" >>"$fx/calls"
edit() {
  jq "$1" "$fx/pr-$2.json" >"$fx/edit.tmp"
  mv "$fx/edit.tmp" "$fx/pr-$2.json"
}
case "$1" in
auth) exit 0 ;;
api)
  if [ "$2" = user ]; then
    cat "$fx/login"
    exit
  fi
  case "$2" in
  repos/*/pulls/*/reviews)
    [ ! -f "$fx/fail-reviews" ] || exit 1
    jq --arg h "$(jq -r .headRefOid "$fx/pr-7.json")" 'map(if .commit_id == "@head" then .commit_id = $h else . end)' "$fx/reviews.json"
    exit
    ;;
  repos/*/rules/branches/*)
    [ ! -f "$fx/fail-rules" ] || exit 1
    if [ -f "$fx/rules.json" ]; then jq -r '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context' "$fx/rules.json"; fi
    exit
    ;;
  esac
  [ ! -f "$fx/fail-delete" ] || exit 1
  all="$*"
  ref=${all##*git/refs/}
  git --git-dir="$GH_ORIGIN" update-ref -d "refs/$ref"
  exit
  ;;
esac
case "$1 $2" in
"pr view")
  n=$3
  shift 3
  fields=""
  while [ $# -gt 0 ]; do
    case "$1" in --json) fields=$2; shift 2 ;; *) shift ;; esac
  done
  [ -f "$fx/pr-$n.json" ] || { echo "no pull request #$n" >&2; exit 1; }
  [ ! -f "$fx/fail-view" ] || exit 1
  jq --arg f "$fields" '($f | split(",")) as $k | with_entries(select(.key as $x | $k | index($x)))' "$fx/pr-$n.json"
  ;;
"repo view") echo main ;;
"pr list")
  shift 2
  base="" limit=30
  while [ $# -gt 0 ]; do
    case "$1" in --base) base=$2; shift 2 ;; --limit) limit=$2; shift 2 ;; *) shift ;; esac
  done
  if [ -f "$fx/children-$base.json" ]; then jq --argjson n "$limit" '.[:$n]' "$fx/children-$base.json"; else echo '[]'; fi
  ;;
"pr edit")
  n=$3
  shift 3
  [ ! -f "$fx/fail-edit" ] || exit 1
  while [ $# -gt 0 ]; do
    case "$1" in --base) edit ".baseRefName = \"$2\"" "$n"; shift 2 ;; *) shift ;; esac
  done
  ;;
"pr merge")
  n=$3
  shift 3
  subject="" body_file="" match=""
  while [ $# -gt 0 ]; do
    case "$1" in
    --subject) subject=$2; shift 2 ;;
    --body-file) body_file=$2; shift 2 ;;
    --match-head-commit) match=$2; shift 2 ;;
    *) shift ;;
    esac
  done
  cp "$body_file" "$fx/body-file-seen"
  [ ! -f "$fx/fail-merge" ] || { echo "merge refused" >&2; exit 1; }
  [ "$match" = "$(jq -r .headRefOid "$fx/pr-$n.json")" ] || { echo "Head branch was modified" >&2; exit 1; }
  [ ! -f "$fx/no-merge" ] || exit 0
  message=$(printf '%s\n\n%s\n' "$subject" "$(cat "$body_file")")
  [ ! -f "$fx/message-suffix" ] || message=$message$(cat "$fx/message-suffix")
  tree=$(git --git-dir="$GH_ORIGIN" rev-parse 'main^{tree}')
  parent=$(git --git-dir="$GH_ORIGIN" rev-parse main)
  new=$(printf '%s\n' "$message" | GIT_AUTHOR_NAME=gh GIT_AUTHOR_EMAIL=gh@example.com GIT_COMMITTER_NAME=gh GIT_COMMITTER_EMAIL=gh@example.com git --git-dir="$GH_ORIGIN" commit-tree "$tree" -p "$parent")
  git --git-dir="$GH_ORIGIN" update-ref refs/heads/main "$new"
  edit ".state = \"MERGED\" | .mergeCommit = {oid: \"$new\"}" "$n"
  [ ! -f "$fx/wrong-oid" ] || edit ".mergeCommit = {oid: \"$match\"}" "$n"
  [ ! -f "$fx/fail-view-after-merge" ] || touch "$fx/fail-view"
  [ ! -f "$fx/fail-after" ] || { echo "reply lost" >&2; exit 1; }
  # Someone pushes to the head branch just after the merge lands.
  if [ -f "$fx/push-after-merge" ]; then
    late=$(printf 'late work\n' | GIT_AUTHOR_NAME=other GIT_AUTHOR_EMAIL=other@example.com GIT_COMMITTER_NAME=other GIT_COMMITTER_EMAIL=other@example.com git --git-dir="$GH_ORIGIN" commit-tree "$match^{tree}" -p "$match")
    git --git-dir="$GH_ORIGIN" update-ref "refs/heads/$(jq -r .headRefName "$fx/pr-$n.json")" "$late"
    printf '%s' "$late" >"$fx/late"
  fi
  ;;
*) echo "stand-in gh: unexpected call: $*" >&2; exit 1 ;;
esac
EOF
  chmod +x "$tmp/bin/gh"
}

write_draft() {
  printf 'feat: add a second note (#7)\n\nThe first note stood alone, so a reader opening the file had no\ncontext for the one beside it.\n\nAssisted-by: claude:test claude-code\nSigned-off-by: Test Agent <agent@example.com>\n' >SQUASH_AGENTMSG
}

reply() {
  printf '%s\n' "$@" >"$tmp/reply"
}

review() {
  run bash "$scripts/review.sh" "${@:-7}"
}

merge() {
  run bash "$scripts/merge.sh" 7 "${1:-$head}"
}

# Review the draft and expect the stamp.
stamped() {
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 0 ]
}

# The pull request changes a path in the feature worktree and the head
# moves with it, as GitHub would serve it.
advance_feature() {
  mkdir -p "$(dirname "$repo/.claude/worktrees/feature/$1")"
  printf '%b' "${2:-x\\n}" >"$repo/.claude/worktrees/feature/$1"
  git -C "$repo/.claude/worktrees/feature" add -- "$1"
  git -C "$repo/.claude/worktrees/feature" commit -q -m "feat: change $1"
  head=$(git -C "$repo/.claude/worktrees/feature" rev-parse HEAD)
  git -C "$repo/.claude/worktrees/feature" push -q origin feature "$head:refs/pull/7/head"
  edit_pr ".headRefOid = \"$head\""
}

gh_calls() {
  cat "$fx/calls" 2>/dev/null || true
}

merged_calls() {
  if [ -f "$fx/calls" ]; then grep -c -- "^pr merge" "$fx/calls" || true; else echo 0; fi
}

origin_has() {
  git --git-dir="$origin" rev-parse --verify --quiet "$1" >/dev/null
}

state_dir() {
  echo "$(git rev-parse --absolute-git-dir)/agent-skills"
}

# --- the checks, judged by their latest run ----------------------------

judge() {
  printf '%s' "$1" >"$tmp/rollup.json"
  run bash -c ". '$scripts/common.sh'; am_judge_checks '$tmp/rollup.json'"
}


# The guard's hook command, exactly as SKILL.md declares it. A script
# that lost its executable bit fails here as it would in a session.
hook_command() {
  sed -n 's/^ *command: "\(.*\)"$/\1/p' "$BATS_TEST_DIRNAME/../SKILL.md" | head -n 1
}

@test "a failure that a later run of the same check replaced passes" {
  judge '[
    {"name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T11:00:00Z"}]'
  [ "$status" -eq 0 ]
  [[ $output == *"ok"*"lint"* ]] || false
  [[ $output != *"fail"* ]] || false
}

@test "the latest run is found by its time, whatever its place in the list" {
  judge '[
    {"name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T11:00:00Z"},
    {"name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"}]'
  [ "$status" -eq 0 ]
}

@test "a success that a later run of the same check failed refuses" {
  judge '[
    {"name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T11:00:00Z"}]'
  [ "$status" -eq 1 ]
  [[ $output == *"fail"*"lint"* ]] || false
}

@test "two different checks with one failing refuse" {
  judge '[
    {"name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"test","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T09:00:00Z"}]'
  [ "$status" -eq 1 ]
}

@test "a latest run that is pending refuses, whatever it follows" {
  for pending in '"status":"IN_PROGRESS"' '"status":"QUEUED"' '"status":"WAITING"' '"status":"COMPLETED"'; do
    judge "[
      {\"name\":\"lint\",\"status\":\"COMPLETED\",\"conclusion\":\"SUCCESS\",\"startedAt\":\"2026-10-14T10:00:00Z\"},
      {\"name\":\"lint\",$pending,\"startedAt\":\"2026-10-14T11:00:00Z\"}]"
    [ "$status" -eq 2 ] || { echo "not refused as pending: $pending"; false; }
  done
}

@test "a re-run that hasn't started yet counts as the latest" {
  judge '[
    {"name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"lint","status":"QUEUED","startedAt":"0001-01-01T00:00:00Z"}]'
  [ "$status" -eq 2 ]
  judge '[
    {"name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"lint","status":"QUEUED"}]'
  [ "$status" -eq 2 ]
}

@test "a failure outranks a pending check" {
  judge '[
    {"name":"lint","status":"IN_PROGRESS","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"test","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"}]'
  [ "$status" -eq 1 ]
}

@test "neutral and skipped runs pass, and every other conclusion refuses" {
  judge '[
    {"name":"a","status":"COMPLETED","conclusion":"NEUTRAL","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"b","status":"COMPLETED","conclusion":"SKIPPED","startedAt":"2026-10-14T10:00:00Z"}]'
  [ "$status" -eq 0 ]
  for conclusion in FAILURE CANCELLED TIMED_OUT ACTION_REQUIRED STALE STARTUP_FAILURE; do
    judge "[{\"name\":\"a\",\"status\":\"COMPLETED\",\"conclusion\":\"$conclusion\",\"startedAt\":\"2026-10-14T10:00:00Z\"}]"
    [ "$status" -eq 1 ] || { echo "not refused: $conclusion"; false; }
  done
}

@test "a commit status is judged by its latest state, by creation time" {
  judge '[
    {"__typename":"StatusContext","context":"ci/build","state":"FAILURE","createdAt":"2026-10-14T10:00:00Z"},
    {"__typename":"StatusContext","context":"ci/build","state":"SUCCESS","createdAt":"2026-10-14T11:00:00Z"}]'
  [ "$status" -eq 0 ]
  judge '[
    {"__typename":"StatusContext","context":"ci/build","state":"SUCCESS","createdAt":"2026-10-14T10:00:00Z"},
    {"__typename":"StatusContext","context":"ci/build","state":"PENDING","createdAt":"2026-10-14T11:00:00Z"}]'
  [ "$status" -eq 2 ]
  judge '[
    {"__typename":"StatusContext","context":"ci/build","state":"SUCCESS","startedAt":"2026-10-14T10:00:00Z"},
    {"__typename":"StatusContext","context":"ci/build","state":"ERROR","startedAt":"2026-10-14T11:00:00Z"}]'
  [ "$status" -eq 1 ]
}

@test "the same check name in two workflows is judged in each" {
  judge '[
    {"name":"build","workflowName":"ci","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"build","workflowName":"release","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T11:00:00Z"}]'
  [ "$status" -eq 1 ]
}

@test "runs that tie on time are judged by the worse one" {
  judge '[
    {"name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"}]'
  [ "$status" -eq 1 ]
  judge '[
    {"name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"},
    {"name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T10:00:00Z"}]'
  [ "$status" -eq 1 ]
}

@test "no checks at all, or a missing rollup, is no evidence and refuses" {
  judge '[]'
  [ "$status" -eq 4 ]
  judge '{"statusCheckRollup":[]}'
  [ "$status" -eq 4 ]
  judge '{"statusCheckRollup":null}'
  [ "$status" -eq 3 ]
  judge '{}'
  [ "$status" -eq 3 ]
  judge '{"statusCheckRollup":[{"name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"}]}'
  [ "$status" -eq 1 ]
  judge 'not json'
  [ "$status" -eq 3 ]
  judge '["a string"]'
  [ "$status" -eq 3 ]
}

# --- review ------------------------------------------------------------

@test "a passing review stamps the draft with the pull request and its head" {
  stamped
  stamp=$(state_dir)/squash.stamp
  [ "$(sed -n 's/^pr //p' "$stamp")" = 7 ]
  [ "$(sed -n 's/^head //p' "$stamp")" = "$head" ]
  [ "$(sed -n 's/^draft //p' "$stamp")" = "$(shasum -a 256 SQUASH_AGENTMSG | cut -d' ' -f1)" ]
}

@test "the reviewer gets the criteria with the repository filled in" {
  stamped
  run grep -c -F "Review the draft in $(git rev-parse --show-toplevel)." "$tmp/prompt"
  [ "$output" = 1 ]
  run grep -c -F "name: review" "$tmp/prompt"
  [ "$output" = 0 ]
}

@test "the reviewer is given the draft, the title, the commits, the diffstat, and the diff as files" {
  stamped
  inputs=$(echo "$(state_dir)"/squash.inputs.*)
  run grep -c -F "$inputs/commits.txt" "$tmp/prompt"
  [ "$output" = 1 ]
  cmp -s SQUASH_AGENTMSG "$inputs/SQUASH_AGENTMSG"
  run cat "$inputs/pull-request.txt"
  [[ $output == *"title: feat: add a second note"* ]] || false
  [[ $output == *"base: main"* ]] || false
  [[ $output == *"head oid: $head"* ]] || false
  run cat "$inputs/commits.txt"
  [[ $output == *"--- $head"* ]] || false
  [[ $output == *"The first note stood alone."* ]] || false
  run cat "$inputs/diffstat.txt"
  [[ $output == *"1+ 0- notes.txt"* ]] || false
  run cat "$inputs/description.md"
  [[ $output == *"Why: the first note stood alone."* ]] || false
  # The change itself, at the recorded head.
  run grep -c -F "$inputs/change.diff" "$tmp/prompt"
  [ "$output" = 1 ]
  run cat "$inputs/change.diff"
  [[ $output == *"+two"* ]] || false
  run cat "$inputs/changed-paths.txt"
  [ "$output" = "$(printf 'M\tnotes.txt')" ]
}

@test "the reviewer reads the change at the pull request's head, not the checkout's" {
  # The checkout sits on the base branch, which doesn't have the change.
  git checkout -q main
  stamped
  inputs=$(echo "$(state_dir)"/squash.inputs.*)
  run cat "$inputs/change.diff"
  [[ $output == *"+two"* ]] || false
  run cat "$inputs/commits.txt"
  [[ $output == *"--- $head"* ]] || false
}

@test "the review reads the pull request in one request, so its parts describe one head" {
  stamped
  run grep -c -- "^pr view 7 --json title,body" "$fx/calls"
  [ "$output" = 1 ]
}

@test "the lint runs before the review, and a failing lint stops it" {
  export AGENT_SKILLS_LINT_SQUASH_CMD="echo 'subject too long'; exit 1"
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 1 ]
  [[ $output == *"subject too long"* ]] || false
  [ ! -e "$tmp/prompt" ]
}

@test "an empty lint command stops the review" {
  export AGENT_SKILLS_LINT_SQUASH_CMD=
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 1 ]
  [[ $output == *"lint_squash_cmd is empty"* ]] || false
}

@test "a draft that names another pull request, or none, is refused" {
  printf 'feat: add a second note (#8)\n\nA reason.\n' >SQUASH_AGENTMSG
  review
  [ "$status" -eq 1 ]
  [[ $output == *"doesn't end in (#7)"* ]] || false
  printf 'feat: add a second note\n\nA reason.\n' >SQUASH_AGENTMSG
  review
  [ "$status" -eq 1 ]
  : >SQUASH_AGENTMSG
  review
  [ "$status" -eq 1 ]
  [[ $output == *"missing or empty"* ]] || false
}

@test "a pull request that isn't open is not reviewed" {
  edit_pr '.state = "CLOSED"'
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 1 ]
  [[ $output == *"CLOSED, not OPEN"* ]] || false
}

@test "a head that moves while the review runs is not stamped" {
  export AGENT_SKILLS_REVIEW_CMD="cat >/dev/null; jq '.headRefOid = \"0123456789abcdef0123456789abcdef01234567\"' '$fx/pr-7.json' >'$fx/moved.json'; mv '$fx/moved.json' '$fx/pr-7.json'; echo 'VERDICT: PASS'"
  review
  [ "$status" -eq 5 ]
  [ ! -e "$(state_dir)/squash.stamp" ]
}

@test "a draft that changes while the review runs is not stamped" {
  export AGENT_SKILLS_REVIEW_CMD="cat >/dev/null; printf 'edited\n' >>SQUASH_AGENTMSG; echo 'VERDICT: PASS'"
  review
  [ "$status" -eq 5 ]
  merge
  [ "$status" -eq 1 ]
}

@test "only a reply that is the passing verdict alone stamps the draft" {
  for text in "Looks fine to me." "VERDICT: PASS and well done" "  VERDICT: PASS"; do
    reply "$text"
    review
    [ "$status" -eq 4 ]
  done
  reply "VERDICT: PASS" "One more thought: the body could be shorter."
  review
  [ "$status" -eq 4 ]
  printf 'VERDICT: PASS\000 -- no, CHANGES REQUIRED\n' >"$tmp/reply"
  review
  [ "$status" -eq 4 ]
  [ ! -e "$(state_dir)/squash.stamp" ]
  merge
  [ "$status" -eq 1 ]
}

@test "findings leave no stamp, and the merge refuses" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [coverage] The second commit's reason is gone."
  review
  [ "$status" -eq 2 ]
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"no independent review"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "past the bound, findings block, the planner hears, and the block is sticky until cleared" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [form] The subject ends in a period."
  review
  [ "$status" -eq 2 ]
  review
  [ "$status" -eq 2 ]
  review
  [ "$status" -eq 3 ]
  [[ $output == *"BLOCKED"* ]] || false
  run grep -c -- "--kind blocker" "$tmp/mail.calls"
  [ "$output" = 1 ]
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 3 ]
  [[ $output == *"Wait for word"* ]] || false
  [ ! -e "$(state_dir)/squash.stamp" ]
  review 7 --unblock
  [ "$status" -eq 0 ]
  review
  [ "$status" -eq 0 ]
  review --unblock
  [ "$status" -eq 0 ]
}

# --- the merge ---------------------------------------------------------

@test "a reviewed pull request merges: pinned to the stamped head, with the reviewed message, and tidied" {
  stamped
  merge
  [ "$status" -eq 0 ]
  sha=$(git --git-dir="$origin" rev-parse main)
  [[ $output == *"merged: #7 as $sha"* ]] || false
  # The merge went through gh once, squash, on the stamped head.
  [ "$(merged_calls)" = 1 ]
  run grep -c -F -- "pr merge 7 --squash --subject feat: add a second note (#7) --body-file $(state_dir)/merge.body --match-head-commit $head" "$fx/calls"
  [ "$output" = 1 ]
  # The commit on the base branch holds the reviewed message.
  run git --git-dir="$origin" log -1 --format=%B main
  [[ ${lines[0]} == "feat: add a second note (#7)" ]] || false
  [[ $output == *"context for the one beside it."* ]] || false
  [[ $output == *"Signed-off-by: Test Agent <agent@example.com>"* ]] || false
  # No other merge route, and no CI run touched.
  run grep -c -E -- "--admin|--auto|^run |^workflow " "$fx/calls"
  [ "$output" = 0 ]
  # The remote branch, the worktree, the local branch, and the state are gone.
  run origin_has refs/heads/feature
  [ "$status" -ne 0 ]
  [ ! -e .claude/worktrees/feature ]
  run git rev-parse --verify --quiet refs/heads/feature
  [ "$status" -ne 0 ]
  [ ! -e SQUASH_AGENTMSG ]
  [ ! -e "$(state_dir)/squash.stamp" ]
  run bash -c "ls '$(state_dir)' | wc -l"
  [ "$((output))" -eq 0 ]
  # The main checkout moved to the merge.
  [ "$(git rev-parse HEAD)" = "$sha" ]
  # The planner hears the number and the full sha.
  run grep -c -- "--kind fixed" "$tmp/mail.calls"
  [ "$output" = 1 ]
  run grep -c -F -- "merged #7 in widgets as $sha" "$tmp/mail.calls"
  [ "$output" = 1 ]
  run grep -c -F -- "--pr 7" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "the merge hands gh its own copy of the body, not the draft" {
  stamped
  merge
  [ "$status" -eq 0 ]
  [ "$(head -n 1 "$fx/body-file-seen")" = "The first note stood alone, so a reader opening the file had no" ]
  run grep -c -F "feat: add a second note" "$fx/body-file-seen"
  [ "$output" = 0 ]
  run tail -n 2 "$fx/body-file-seen"
  [[ ${lines[1]} == "Signed-off-by: Test Agent <agent@example.com>" ]] || false
}

@test "the merge takes exactly its pull request and the full head" {
  stamped
  run bash "$scripts/merge.sh"
  [ "$status" -eq 1 ]
  run bash "$scripts/merge.sh" 7
  [ "$status" -eq 1 ]
  run bash "$scripts/merge.sh" 7 "${head:0:7}"
  [ "$status" -eq 1 ]
  [[ $output == *"full oid"* ]] || false
  run bash "$scripts/merge.sh" 7 "$head" --admin
  [ "$status" -eq 1 ]
  [ "$(merged_calls)" = 0 ]
}

@test "the merge refuses without a review" {
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"no independent review"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a draft edited after its review is refused" {
  stamped
  printf 'feat: add a second note (#7)\n\nA different reason.\n' >SQUASH_AGENTMSG
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"changed after its review"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "the stamp is checked first, ahead of the state of the pull request" {
  edit_pr '.isDraft = true'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"no independent review"* ]] || false
  stamped
  edit_pr '.headRefOid = "0123456789abcdef0123456789abcdef01234567"'
  merge
  [[ $output == *"not the reviewed $head"* ]] || false
}

@test "a draft that names another pull request in its subject is refused with that reason" {
  stamped
  printf 'feat: add a second note (#8)\n\nA reason.\n' >SQUASH_AGENTMSG
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"doesn't end in (#7)"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a draft with a subject and no body is refused" {
  printf 'feat: add a second note (#7)\n' >SQUASH_AGENTMSG
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"subject and no body"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a stamp made for another pull request is refused" {
  stamped
  sed 's/^pr 7$/pr 8/' "$(state_dir)/squash.stamp" >"$tmp/stamp.edit"
  mv "$tmp/stamp.edit" "$(state_dir)/squash.stamp"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"the pr changed after the review"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a head that isn't the one the caller expected is refused" {
  stamped
  merge 0123456789abcdef0123456789abcdef01234567
  [ "$status" -eq 1 ]
  [[ $output == *"the head changed after the review"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a head that moved after the review is refused" {
  stamped
  edit_pr '.headRefOid = "0123456789abcdef0123456789abcdef01234567"'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"not the reviewed $head"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a pull request that isn't open, or is a draft, is refused" {
  stamped
  for value in MERGED CLOSED; do
    edit_pr ".state = \"$value\""
    merge
    [ "$status" -eq 1 ]
    [[ $output == *"is $value, not OPEN"* ]] || false
  done
  edit_pr '.state = "OPEN" | .isDraft = true'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"is a draft"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a merge state other than CLEAN is refused" {
  stamped
  for value in BEHIND BLOCKED DIRTY UNSTABLE HAS_HOOKS UNKNOWN DRAFT; do
    edit_pr ".mergeStateStatus = \"$value\""
    merge
    [ "$status" -eq 1 ] || { echo "not refused: $value"; false; }
    [[ $output == *"not CLEAN"* ]] || false
  done
  [ "$(merged_calls)" = 0 ]
}

@test "a check rollup that can't be read refuses the merge" {
  stamped
  edit_pr '.statusCheckRollup = ["a string"]'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"couldn't be read"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a check whose latest run failed, or hasn't finished, is refused" {
  stamped
  edit_pr '.statusCheckRollup = [
    {"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T10:00:00Z"},
    {"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T11:00:00Z"}]'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"failing check"* ]] || false
  edit_pr '.statusCheckRollup = [
    {"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T10:00:00Z"},
    {"__typename":"CheckRun","name":"test","status":"IN_PROGRESS","startedAt":"2026-10-14T11:00:00Z"}]'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"hasn't finished"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a failure that a later run replaced does not stop the merge" {
  stamped
  edit_pr '.statusCheckRollup = [
    {"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"},
    {"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T11:00:00Z"}]'
  merge
  [ "$status" -eq 0 ]
}

@test "the checks are read from the rollup, never re-run, cancelled, or deleted" {
  stamped
  merge
  [ "$status" -eq 0 ]
  run grep -c -E "^(run|workflow|api -X POST|api -X PUT)" "$fx/calls"
  [ "$output" = 0 ]
}

@test "a pull request that changes a golden file goes to the owner, whatever the grant says" {
  export AGENT_SKILLS_GOLDEN_PATHS='(^|/)tests/golden/'
  advance_feature tests/golden/frame-001.txt
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"#7 changes golden files: tests/golden/frame-001.txt"* ]] || false
  run grep -c "^pr merge" "$fx/calls"
  [ "$output" = 0 ]
  run grep -c "needs-owner: #7 in widgets changes golden files" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "the repository's golden-file pattern matches no path, because it has no golden files" {
  unset AGENT_SKILLS_GOLDEN_PATHS
  for path in tests/golden/a.png crates/avirt-core/src/lib.rs notes.txt docs/index.md; do
    ! printf '%s\n' "$path" | grep -q -E -- "$(bash -c ". '$scripts/common.sh'; as_conf golden_paths")" || { echo "golden by mistake: $path"; false; }
  done
}

@test "an empty or broken golden-file pattern refuses the merge" {
  stamped
  AGENT_SKILLS_GOLDEN_PATHS="" run bash "$scripts/merge.sh" 7 "$head"
  [ "$status" -eq 1 ]
  [[ $output == *"aren't patterns grep can read: golden_paths"* ]] || false
  AGENT_SKILLS_GOLDEN_PATHS="(" run bash "$scripts/merge.sh" 7 "$head"
  [ "$status" -eq 1 ]
  [[ $output == *"aren't patterns grep can read: golden_paths"* ]] || false
  run grep -c "^pr merge" "$fx/calls"
  [ "$output" = 0 ]
}

@test "a pull request that adds a suppression goes to the owner" {
  advance_feature tools/check.py 'import os  # noqa: F401\n'
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"tools/check.py: adds a suppression: import os  # noqa: F401"* ]] || false
  [[ $output == *"#7 loosens a gate"* ]] || false
  run grep -c "^pr merge" "$fx/calls"
  [ "$output" = 0 ]
  run grep -c "needs-owner: #7 in widgets loosens a gate" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a pull request that widens a lint exclusion or drops a hook goes to the owner" {
  advance_feature .rumdl.toml 'exclude = ["notes"]\n'
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *'.rumdl.toml: adds an exclusion: exclude = ["notes"]'* ]] || false

  printf 'repos:\n  - repo: local\n    hooks:\n      - id: lint\n' >"$repo/.claude/worktrees/feature/.pre-commit-config.yaml"
  git -C "$repo/.claude/worktrees/feature" commit -q -am "build: drop the spell hook"
  head=$(git -C "$repo/.claude/worktrees/feature" rev-parse HEAD)
  git -C "$repo/.claude/worktrees/feature" push -q origin feature "$head:refs/pull/7/head"
  edit_pr ".headRefOid = \"$head\""
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *".pre-commit-config.yaml: removes a hook or task:       - id: spell"* ]] || false
}

@test "the loosening markers read Go suppressions and coverage floors, and nothing ordinary" {
  # ascm adaptation: assert Go suppressions and coverage threshold edits
  unset AGENT_SKILLS_LOOSENING_MARKERS
  markers=$(bash -c ". '$scripts/common.sh'; as_conf loosening_markers")
  for line in '//nolint:errcheck' '//go:build ignore' '//go:build linux && ignore' 't.Skip("pending")' 't.SkipNow()' '// #nosec G104' '//lint:ignore U1000 reason' '  file: 70' '  package: 75' '  total: 80'; do
    printf '%s\n' "$line" | tr '[:upper:]' '[:lower:]' | grep -q -E -- "$markers" || { echo "not read as a suppression: $line"; false; }
  done
  for line in 'allowed := true' 'import "io"' '//go:build linux' 'func expectOK() {}'; do
    ! printf '%s\n' "$line" | tr '[:upper:]' '[:lower:]' | grep -q -E -- "$markers" || { echo "read as a suppression by mistake: $line"; false; }
  done
}

@test "an approval from the required reviewer on the current head lets the merge go" {
  stamped
  more_review someone-else COMMENTED @head
  merge
  [ "$status" -eq 0 ]
  [ "$(merged_calls)" = 1 ]
}

@test "an approval of an older head is refused" {
  review_as tbhb-agent-reviewer APPROVED 0123456789abcdef0123456789abcdef01234567
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"is for 0123456789ab, not the current head"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "changes requested after an approval is refused" {
  more_review tbhb-agent-reviewer CHANGES_REQUESTED @head
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"requests changes"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "an approval after a change request lets the merge go" {
  review_as tbhb-agent-reviewer CHANGES_REQUESTED @head
  more_review tbhb-agent-reviewer APPROVED @head
  stamped
  merge
  [ "$status" -eq 0 ]
}

@test "a dismissed review is refused" {
  review_as tbhb-agent-reviewer DISMISSED @head
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"was dismissed"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "an approval only from someone else is refused" {
  review_as someone-else APPROVED @head
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"no approving or blocking review from tbhb-agent-reviewer"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "an empty required reviewer refuses every merge" {
  stamped
  export AGENT_SKILLS_REQUIRED_REVIEWER=
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"required_reviewer is empty"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "reviews that can't be read refuse the merge" {
  stamped
  : >"$fx/fail-reviews"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"couldn't read the reviews"* ]] || false
}

@test "preflight reports the required reviewer's verdict" {
  run bash "$scripts/preflight.sh" 7
  [[ $output == *"tbhb-agent-reviewer: approved the current head"* ]] || false
  review_as tbhb-agent-reviewer DISMISSED @head
  run bash "$scripts/preflight.sh" 7
  [[ $output == *"tbhb-agent-reviewer: NOT APPROVED, the latest review from tbhb-agent-reviewer was dismissed"* ]] || false
  export AGENT_SKILLS_REQUIRED_REVIEWER=
  run bash "$scripts/preflight.sh" 7
  [[ $output == *"required_reviewer: NOT CONFIGURED"* ]] || false
}

@test "an ordinary change has nothing a loosening check reads" {
  advance_feature tools/check.py 'import os\nprint(os.sep)\n'
  stamped
  merge
  [ "$status" -eq 0 ]
  [[ $output != *"loosens a gate"* ]] || false
}

@test "a pull request with no checks at all doesn't merge" {
  stamped
  edit_pr '.statusCheckRollup = []'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"#7 has no checks at all"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a required check with no run among the checks refuses the merge" {
  push_main_ruleset lint private-lists
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"lacks a passing latest run of the required checks: private-lists"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "the required checks come from the ruleset files as the base has them, by their real names" {
  push_main_ruleset private-lists lint "lint-codeowners / lint-codeowners"
  edit_pr '.statusCheckRollup = [
    {__typename: "CheckRun", name: "private-lists", workflowName: "ci", status: "COMPLETED", conclusion: "SUCCESS", startedAt: "2026-10-14T10:00:00Z"},
    {__typename: "CheckRun", name: "lint", workflowName: "ci", status: "COMPLETED", conclusion: "SUCCESS", startedAt: "2026-10-14T10:00:00Z"},
    {__typename: "CheckRun", name: "lint-codeowners / lint-codeowners", workflowName: "lint-codeowners", status: "COMPLETED", conclusion: "SUCCESS", startedAt: "2026-10-14T10:00:00Z"}]'
  stamped
  merge
  [ "$status" -eq 0 ]
}

@test "a pull request that drops a required check from its own ruleset still needs it" {
  push_main_ruleset lint private-lists
  # The branch's own copy requires only lint.
  mkdir -p "$repo/.claude/worktrees/feature/.github/rulesets"
  ruleset main-as-the-branch-has-it lint >"$repo/.claude/worktrees/feature/.github/rulesets/main.json"
  git -C "$repo/.claude/worktrees/feature" add -- .github/rulesets/main.json
  git -C "$repo/.claude/worktrees/feature" commit -q -m "build: drop a required check"
  head=$(git -C "$repo/.claude/worktrees/feature" rev-parse HEAD)
  git -C "$repo/.claude/worktrees/feature" push -q origin feature "$head:refs/pull/7/head"
  edit_pr ".headRefOid = \"$head\""
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"required checks: private-lists"* ]] || false
}

@test "a ruleset for another branch, or one that's disabled, requires nothing here" {
  push_main_ruleset lint
  jq '.conditions.ref_name.include = ["refs/heads/release/*"] | .rules[0].parameters.required_status_checks += [{context: "release-only"}]' \
    <(ruleset release lint) >"$tmp/release.json"
  jq '.enforcement = "disabled" | .rules[0].parameters.required_status_checks += [{context: "disabled-only"}]' \
    <(ruleset off lint) >"$tmp/off.json"
  add_main_files .github/rulesets/release.json "$tmp/release.json" .github/rulesets/off.json "$tmp/off.json"
  stamped
  merge
  [ "$status" -eq 0 ]
}

@test "a skipped or neutral run of a required check doesn't pass it" {
  for conclusion in SKIPPED NEUTRAL; do
    edit_pr ".statusCheckRollup = [{__typename: \"CheckRun\", name: \"lint\", workflowName: \"ci\", status: \"COMPLETED\", conclusion: \"$conclusion\", startedAt: \"2026-10-14T10:00:00Z\"}]"
    stamped
    merge
    [ "$status" -eq 1 ] || { echo "passed on $conclusion"; false; }
    [[ $output == *"required checks: lint"* ]] || false
  done
  [ "$(merged_calls)" = 0 ]
}

@test "required checks that can't be read, or that nothing names, refuse the merge" {
  stamped
  touch "$fx/fail-rules"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"couldn't read the checks main requires"* ]] || false
  unlink "$fx/fail-rules"
  push_main_ruleset
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"no required checks are known for main"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a required check whose latest run failed refuses, though an older run passed" {
  stamped
  edit_pr '.statusCheckRollup = [
    {__typename: "CheckRun", name: "lint", workflowName: "ci", status: "COMPLETED", conclusion: "SUCCESS", startedAt: "2026-10-14T10:00:00Z"},
    {__typename: "CheckRun", name: "lint", workflowName: "ci", status: "COMPLETED", conclusion: "FAILURE", startedAt: "2026-10-14T11:00:00Z"}]'
  merge
  [ "$status" -eq 1 ]
  [ "$(merged_calls)" = 0 ]
}

@test "a pull request moved to another base after its review doesn't merge" {
  git push -q origin "$(git rev-parse main):refs/heads/release"
  stamped
  edit_pr '.baseRefName = "release"'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"the base changed after the review (reviewed main, now release)"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a base branch that moved after the review sends the message back to review" {
  stamped
  moved=$(printf 'elsewhere\n' | GIT_AUTHOR_NAME=other GIT_AUTHOR_EMAIL=other@example.com GIT_COMMITTER_NAME=other GIT_COMMITTER_EMAIL=other@example.com git --git-dir="$origin" commit-tree "main^{tree}" -p main)
  git --git-dir="$origin" update-ref refs/heads/main "$moved"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"the base_oid changed after the review"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "every stacked pull request moves, past the 30 gh lists by default" {
  jq -n '[range(100; 131) | {number: .}]' >"$fx/children-feature.json"
  for n in $(seq 100 130); do
    jq -n --argjson n "$n" '{number: $n, baseRefName: "feature", state: "OPEN"}' >"$fx/pr-$n.json"
  done
  stamped
  merge
  [ "$status" -eq 0 ]
  for n in $(seq 100 130); do
    [ "$(jq -r .baseRefName "$fx/pr-$n.json")" = main ] || { echo "not moved: #$n"; false; }
  done
}

@test "a list of stacked pull requests that comes back full refuses the merge" {
  jq -n '[range(1000; 1500) | {number: .}]' >"$fx/children-feature.json"
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"500 or more pull requests are based on feature"* ]] || false
  [ "$(merged_calls)" = 0 ]
  run origin_has refs/heads/feature
  [ "$status" -eq 0 ]
}

@test "a merge whose response is lost and whose state can't be read is reported unconfirmed" {
  stamped
  # GitHub merges, the reply never arrives, and every read after it fails.
  touch "$fx/fail-after" "$fx/fail-view-after-merge"
  export AGENT_SKILLS_MERGE_VERIFY_TRIES=1
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"whether it merged is unconfirmed"* ]] || false
  [[ $output != *"nothing was merged"* ]] || false
  run grep -c "merge-unconfirmed: gh pr merge for #7 in widgets failed" "$tmp/mail.calls"
  [ "$output" = 1 ]
  # Nothing was cleaned up.
  origin_has refs/heads/feature
  [ -d .claude/worktrees/feature ]
}

@test "a failed merge on a pull request that reads as still open is reported as not merged" {
  stamped
  touch "$fx/fail-merge"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"#7 is still open; nothing was merged"* ]] || false
  [ ! -e "$tmp/mail.calls" ] || [ "$(grep -c "merge-unconfirmed" "$tmp/mail.calls")" = 0 ]
}

@test "a worktree holding local configuration stays after the merge" {
  printf 'SECRET=1\n' >"$repo/.claude/worktrees/feature/.env"
  printf '.env\n.venv/\n' >>"$(git -C "$repo/.claude/worktrees/feature" rev-parse --git-path info/exclude)"
  stamped
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"holds ignored files that aren't caches, so it stays: .env"* ]] || false
  [ -f "$repo/.claude/worktrees/feature/.env" ]
  git rev-parse --verify --quiet refs/heads/feature >/dev/null
}

@test "the agent account defaults to tbhb-agent" {
  unset AGENT_SKILLS_GH_LOGIN
  printf 'the-owner\n' >"$fx/login"
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"gh is signed in as the-owner, not tbhb-agent"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a worktree holding only caches goes after the merge" {
  # ascm adaptation: clean Go binaries and coverage outputs after merge
  mkdir -p "$repo/.claude/worktrees/feature/bin" "$repo/.claude/worktrees/feature/apm_modules/x"
  printf 'x\n' >"$repo/.claude/worktrees/feature/bin/x"
  printf 'mode: set\n' >"$repo/.claude/worktrees/feature/coverage.out"
  printf 'x\n' >"$repo/.claude/worktrees/feature/apm_modules/x/y"
  printf 'docs: a draft already committed\n' >"$repo/.claude/worktrees/feature/COMMIT_AGENTMSG"
  printf 'bin/\ncoverage.out\napm_modules/\nCOMMIT_AGENTMSG\n' >>"$(git -C "$repo/.claude/worktrees/feature" rev-parse --git-path info/exclude)"
  stamped
  merge
  [ "$status" -eq 0 ]
  [ ! -e .claude/worktrees/feature ]
}

@test "a broken disposable-files pattern keeps the worktree and stops the merge before it starts" {
  printf 'SECRET=1\n' >"$repo/.claude/worktrees/feature/.env"
  printf '.env\n' >>"$(git -C "$repo/.claude/worktrees/feature" rev-parse --git-path info/exclude)"
  stamped
  AGENT_SKILLS_DISPOSABLE_IGNORED='(' run bash "$scripts/merge.sh" 7 "$head"
  [ "$status" -eq 1 ]
  [[ $output == *"aren't patterns grep can read: disposable_ignored"* ]] || false
  [ "$(merged_calls)" = 0 ]
  [ -f "$repo/.claude/worktrees/feature/.env" ]
}

@test "a broken pattern in the cleanup check keeps the worktree" {
  printf 'SECRET=1\n' >"$repo/.claude/worktrees/feature/.env"
  printf '.env\n' >>"$(git -C "$repo/.claude/worktrees/feature" rev-parse --git-path info/exclude)"
  run bash -c ". '$scripts/common.sh'; AGENT_SKILLS_DISPOSABLE_IGNORED='(' am_kept_ignored '$repo/.claude/worktrees/feature'"
  [ "$status" -ne 0 ]
}

@test "each pattern setting that grep can't read is named, and preflight reports it" {
  for key in OWNER_PATHS GOLDEN_PATHS LOOSENING_MARKERS GATE_CONFIG_PATHS DISPOSABLE_IGNORED; do
    run env "AGENT_SKILLS_$key=(" bash -c ". '$scripts/common.sh'; am_bad_patterns"
    [ "$output" = "$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')" ] || { echo "not named: $key"; false; }
  done
  run bash -c ". '$scripts/common.sh'; am_bad_patterns"
  [ -z "$output" ]
  AGENT_SKILLS_GOLDEN_PATHS='(' run bash "$scripts/preflight.sh" 7
  [[ $output == *"BROKEN: these settings are empty or aren't patterns grep can read, and merge.sh refuses: golden_paths"* ]] || false
}

@test "a pull request from a fork moves no other pull request" {
  # A pull request in this repository based on a branch that happens to
  # share the fork branch's name.
  printf '[{"number": 9}]' >"$fx/children-feature.json"
  jq -n '{number: 9, baseRefName: "feature", state: "OPEN"}' >"$fx/pr-9.json"
  edit_pr '.isCrossRepository = true'
  stamped
  merge
  [ "$status" -eq 0 ]
  [ "$(jq -r .baseRefName "$fx/pr-9.json")" = feature ]
  run grep -c -- "--base main" "$fx/calls"
  [ "$output" = 0 ]
}

@test "a pull request whose fork status can't be read moves nothing and doesn't merge" {
  printf '[{"number": 9}]' >"$fx/children-feature.json"
  jq -n '{number: 9, baseRefName: "feature", state: "OPEN"}' >"$fx/pr-9.json"
  edit_pr 'del(.isCrossRepository)'
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"couldn't tell whether #7 comes from a fork"* ]] || false
  [ "$(jq -r .baseRefName "$fx/pr-9.json")" = feature ]
  [ "$(merged_calls)" = 0 ]
}

@test "a pull request with the owner-review label goes to the owner" {
  stamped
  edit_pr '.labels = [{"name": "enhancement"}, {"name": "owner-review"}]'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"#7 has the owner-review label; the owner reviews and merges it"* ]] || false
  run grep -c "^pr merge" "$fx/calls"
  [ "$output" = 0 ]
  run grep -c "needs-owner" "$tmp/mail.calls"
  [ "$output" = 1 ]
  edit_pr '.labels = [{"name": "enhancement"}]'
  merge
  [ "$status" -eq 0 ]
}

@test "labels that can't be read refuse the merge" {
  stamped
  edit_pr 'del(.labels)'
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"couldn't read the labels of #7"* ]] || false
  run grep -c "^pr merge" "$fx/calls"
  [ "$output" = 0 ]
}

@test "a merge refuses unless gh is signed in as the configured account" {
  stamped
  printf 'the-owner\n' >"$GH_FX/login"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"gh is signed in as the-owner, not agent-account; nothing was merged"* ]] || false
  run grep -c "^pr merge" "$fx/calls"
  [ "$output" = 0 ]
  printf 'agent-account\n' >"$GH_FX/login"
  AGENT_SKILLS_GH_LOGIN="" run bash "$scripts/merge.sh" 7 "$head"
  [ "$status" -eq 1 ]
  [[ $output == *"gh_login isn't configured"* ]] || false
  run grep -c "^pr merge" "$fx/calls"
  [ "$output" = 0 ]
}

@test "no readiness command adds no check" {
  stamped
  export AGENT_SKILLS_MERGE_READY_CMD=
  merge
  [ "$status" -eq 0 ]
  [ ! -e "$tmp/ready.calls" ]
}

@test "a readiness command that is missing or says no refuses" {
  stamped
  export AGENT_SKILLS_MERGE_READY_CMD=$tmp/missing
  merge
  [ "$status" -eq 1 ]
  export AGENT_SKILLS_MERGE_READY_CMD=$tmp/ready
  printf '1\n' >"$tmp/ready-status"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"readiness command says"* ]] || false
}

@test "the readiness command is asked with the repository root, the pull request, and the head" {
  stamped
  merge
  [ "$status" -eq 0 ]
  [ "$(cat "$tmp/ready.calls")" = "$(git rev-parse --show-toplevel) 7 $head" ]
}

@test "a pull request that changes owner paths merges, and the report names every one" {
  advance_feature owned/rule.txt
  advance_feature owned/other.txt
  stamped
  merge
  [ "$status" -eq 0 ]
  [ "$(merged_calls)" = 1 ]
  [[ $output == *"Owner paths changed: "* ]] || false
  [[ $output == *"owned/rule.txt"* ]] || false
  [[ $output == *"owned/other.txt"* ]] || false
  [[ $output != *"notes.txt"* ]] || false
  run grep -F -- "--kind fixed" "$tmp/mail.calls"
  [[ $output == *"Owner paths changed: owned/other.txt owned/rule.txt."* ]] || false
  run grep -c -- "--kind blocker" "$tmp/mail.calls"
  [ "$output" = 0 ]
}

@test "a rename out of an owner path counts as touching it" {
  git -C "$repo/.claude/worktrees/feature" mv owned/rule.txt moved.txt
  git -C "$repo/.claude/worktrees/feature" commit -q -m "feat: move it out"
  head=$(git -C "$repo/.claude/worktrees/feature" rev-parse HEAD)
  git -C "$repo/.claude/worktrees/feature" push -q origin feature "$head:refs/pull/7/head"
  edit_pr ".headRefOid = \"$head\""
  stamped
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"Owner paths changed: owned/rule.txt."* ]] || false
}

@test "an ordinary pull request says nothing about owner paths" {
  stamped
  merge
  [ "$status" -eq 0 ]
  [[ $output != *owner* ]] || false
  run grep -c -i "owner" "$tmp/mail.calls"
  [ "$output" = 0 ]
}

@test "the owner-review label still refuses a merge that touches an owner path" {
  advance_feature owned/rule.txt
  edit_pr '.labels = [{name: "owner-review"}]'
  stamped
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"owner-review label"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "paths that can't be read refuse the merge" {
  stamped
  git --git-dir="$origin" update-ref -d refs/pull/7/head
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"couldn't read the paths"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "failing commit-msg gates stop the merge" {
  stamped
  export AGENT_SKILLS_LINT_SQUASH_CMD="echo 'trailers out of order'; exit 1"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"trailers out of order"* ]] || false
  [ "$(merged_calls)" = 0 ]
  export AGENT_SKILLS_LINT_SQUASH_CMD=
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"lint_squash_cmd is empty"* ]] || false
}

@test "a draft that the lint command rewrites is not merged" {
  stamped
  export AGENT_SKILLS_LINT_SQUASH_CMD="printf 'feat: something else (#7)\n\nNo one reviewed this.\n' >SQUASH_AGENTMSG"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"changed after its review"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "a head that moves while the gates run is refused by the second check" {
  stamped
  cat >"$tmp/ready" <<EOF
#!/usr/bin/env bash
jq '.headRefOid = "0123456789abcdef0123456789abcdef01234567"' "$fx/pr-7.json" >"$fx/moved.json"
mv "$fx/moved.json" "$fx/pr-7.json"
EOF
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"not the reviewed $head"* ]] || false
  [ "$(merged_calls)" = 0 ]
}

@test "the gates run in order, each reading the live state" {
  advance_feature tools/check.py 'import os  # noqa: F401\n'
  stamped
  edit_pr '.isDraft = true | .mergeStateStatus = "BEHIND"'
  printf '1\n' >"$tmp/ready-status"
  merge
  [[ $output == *"is a draft"* ]] || false
  edit_pr '.isDraft = false'
  merge
  [[ $output == *"not CLEAN"* ]] || false
  edit_pr '.mergeStateStatus = "CLEAN"'
  merge
  [[ $output == *"readiness command"* ]] || false
  printf '0\n' >"$tmp/ready-status"
  export AGENT_SKILLS_LINT_SQUASH_CMD="touch '$tmp/linted'; exit 1"
  merge
  [[ $output == *"loosens a gate"* ]] || false
  [ ! -e "$tmp/linted" ]
  [ "$(merged_calls)" = 0 ]
}

@test "a pull request stacked on this one gets the new base before the merge" {
  stamped
  printf '[{"number":8},{"number":9}]\n' >"$fx/children-feature.json"
  jq -n '{number: 8, baseRefName: "feature"}' >"$fx/pr-8.json"
  jq -n '{number: 9, baseRefName: "feature"}' >"$fx/pr-9.json"
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"moved the base of #8 from feature to main"* ]] || false
  [ "$(jq -r .baseRefName "$fx/pr-8.json")" = main ]
  [ "$(jq -r .baseRefName "$fx/pr-9.json")" = main ]
  edit_line=$(grep -n -- "^pr edit 8 --base main" "$fx/calls" | cut -d: -f1)
  merge_line=$(grep -n -- "^pr merge 7" "$fx/calls" | cut -d: -f1)
  [ "$edit_line" -lt "$merge_line" ]
}

@test "a merge with nothing stacked on it moves no base" {
  stamped
  merge
  [ "$status" -eq 0 ]
  run grep -c -- "^pr edit" "$fx/calls"
  [ "$output" = 0 ]
}

@test "a pull request whose base can't be moved stops the merge" {
  stamped
  printf '[{"number":8}]\n' >"$fx/children-feature.json"
  jq -n '{number: 8, baseRefName: "feature"}' >"$fx/pr-8.json"
  touch "$fx/fail-edit"
  merge
  [ "$status" -eq 1 ]
  [ "$(merged_calls)" = 0 ]
}

@test "the main checkout moves to the merge only on the base branch with a clean tree" {
  stamped
  git checkout -q -b elsewhere
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"main checkout isn't on main"* ]] || false
  [ "$(git rev-parse HEAD)" != "$(git --git-dir="$origin" rev-parse main)" ]
}

@test "a main checkout with changes is not fast-forwarded" {
  stamped
  printf 'edit\n' >>notes.txt
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"main checkout has changes"* ]] || false
  [ "$(git rev-parse HEAD)" != "$(git --git-dir="$origin" rev-parse main)" ]
  run git diff --name-only
  [ "$output" = notes.txt ]
}

@test "a main checkout that has diverged is left alone and the merge still succeeds" {
  stamped
  printf 'local\n' >local.txt
  git add -- local.txt
  git commit -q -m "chore: a local commit"
  local_head=$(git rev-parse HEAD)
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"couldn't fast-forward"* ]] || false
  [ "$(git rev-parse HEAD)" = "$local_head" ]
}

@test "a worktree with changes is kept, and so is its branch" {
  stamped
  printf 'unsaved\n' >.claude/worktrees/feature/scratch.txt
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"worktree of feature has changes"* ]] || false
  [ -e .claude/worktrees/feature/scratch.txt ]
  run git rev-parse --verify --quiet refs/heads/feature
  [ "$status" -eq 0 ]
}

@test "the worktree the merge runs in is kept, and the main checkout still moves" {
  cd .claude/worktrees/feature
  write_draft
  stamped
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"this is the worktree of feature"* ]] || false
  [ -e "$repo/.claude/worktrees/feature" ]
  [ "$(git -C "$repo" rev-parse HEAD)" = "$(git --git-dir="$origin" rev-parse main)" ]
  run git -C "$repo" rev-parse --verify --quiet refs/heads/feature
  [ "$status" -eq 0 ]
}

@test "a worktree outside .claude/worktrees is kept" {
  git worktree remove .claude/worktrees/feature
  git worktree add -q "$tmp/elsewhere" feature
  stamped
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"isn't under .claude/worktrees/"* ]] || false
  [ -e "$tmp/elsewhere" ]
}

@test "a local branch that has commits beyond the reviewed head is kept" {
  printf 'more\n' >.claude/worktrees/feature/more.txt
  git -C .claude/worktrees/feature add -- more.txt
  git -C .claude/worktrees/feature commit -q -m "feat: not pushed"
  stamped
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"isn't at the reviewed head"* ]] || false
  [ -e .claude/worktrees/feature ]
}

@test "a merged branch with no worktree loses its local branch" {
  git worktree remove .claude/worktrees/feature
  stamped
  merge
  [ "$status" -eq 0 ]
  run git rev-parse --verify --quiet refs/heads/feature
  [ "$status" -ne 0 ]
}

@test "a branch from a fork is not deleted from the remote" {
  edit_pr '.isCrossRepository = true'
  stamped
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"in a fork"* ]] || false
  run grep -c -- "^api -X DELETE" "$fx/calls"
  [ "$output" = 0 ]
  origin_has refs/heads/feature
}

@test "a remote branch that can't be deleted is reported and the merge is still done" {
  stamped
  # A remote that refuses every delete, as a ruleset would.
  printf '#!/bin/sh\nwhile read -r old new ref; do\n  [ "$new" != 0000000000000000000000000000000000000000 ] || exit 1\ndone\n' >"$origin/hooks/pre-receive"
  chmod +x "$origin/hooks/pre-receive"
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"couldn't delete origin/feature"* ]] || false
  origin_has refs/heads/feature
  run grep -c -- "--kind fixed" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a push to the head branch after the merge keeps the remote branch" {
  stamped
  touch "$fx/push-after-merge"
  merge
  [ "$status" -eq 0 ]
  [[ $output == *"couldn't delete origin/feature: it no longer points at the merged head $head"* ]] || false
  [ "$(git --git-dir="$origin" rev-parse refs/heads/feature)" = "$(cat "$fx/late")" ]
  run grep -c "origin/feature was left in place" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a merge commit whose message isn't the reviewed one is reported, and nothing is cleaned up" {
  stamped
  printf '\n\nCo-authored-by: Someone <s@example.com>' >"$fx/message-suffix"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"isn't the reviewed message"* ]] || false
  run grep -c "merged-not-as-reviewed" "$tmp/mail.calls"
  [ "$output" = 1 ]
  run grep -c -- "--kind fixed" "$tmp/mail.calls"
  [ "$output" = 0 ]
  origin_has refs/heads/feature
  [ -e .claude/worktrees/feature ]
  [ -s SQUASH_AGENTMSG ]
}

@test "a merge commit that isn't on the base branch is reported" {
  stamped
  touch "$fx/wrong-oid"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"isn't on main"* ]] || false
  run grep -c "merged-not-as-reviewed" "$tmp/mail.calls"
  [ "$output" = 1 ]
  origin_has refs/heads/feature
}

@test "a merge that gh reports and GitHub doesn't show is reported" {
  stamped
  touch "$fx/no-merge"
  merge
  [ "$status" -eq 1 ]
  run grep -c "merge-unconfirmed" "$tmp/mail.calls"
  [ "$output" = 1 ]
  origin_has refs/heads/feature
}

@test "a merge that gh refuses leaves everything in place" {
  stamped
  touch "$fx/fail-merge"
  merge
  [ "$status" -eq 1 ]
  [[ $output == *"nothing was merged"* ]] || false
  origin_has refs/heads/feature
  [ -e .claude/worktrees/feature ]
  [ -s SQUASH_AGENTMSG ]
  [ -e "$(state_dir)/squash.stamp" ]
}

@test "a merge that landed but whose reply failed is still verified and finished" {
  stamped
  touch "$fx/fail-after"
  merge
  [ "$status" -eq 0 ]
  run grep -c -- "--kind fixed" "$tmp/mail.calls"
  [ "$output" = 1 ]
  run origin_has refs/heads/feature
  [ "$status" -ne 0 ]
}

# --- the library ---------------------------------------------------------

@test "the repository's settings file can't name the readiness command" {
  cp -R "$scripts/../../agent-commit" "$tmp/agent-commit"
  cp -R "$scripts/.." "$tmp/agent-merge-pr"
  printf 'merge_ready_cmd = %s\nlint_squash_cmd = from-the-file\n' "$tmp/always-yes" >"$tmp/agent-commit/agent-skills.conf"
  unset AGENT_SKILLS_MERGE_READY_CMD AGENT_SKILLS_LINT_SQUASH_CMD
  run bash -c ". '$tmp/agent-merge-pr/scripts/common.sh'; as_conf merge_ready_cmd unset; as_conf lint_squash_cmd unset"
  [ "$output" = "$(printf 'unset\nfrom-the-file')" ]
  mkdir -p "$XDG_CONFIG_HOME/agent-skills"
  printf 'merge_ready_cmd = %s\n' "$tmp/ready" >"$XDG_CONFIG_HOME/agent-skills/widgets.conf"
  run bash -c ". '$tmp/agent-merge-pr/scripts/common.sh'; as_conf merge_ready_cmd unset"
  [ "$output" = "$tmp/ready" ]
}

@test "the repository's own settings name the lint and the criteria" {
  unset AGENT_SKILLS_LINT_SQUASH_CMD AGENT_SKILLS_REVIEW_SQUASH_CRITERIA
  run bash -c ". '$scripts/common.sh'; as_conf lint_squash_cmd; as_conf review_squash_criteria"
  [ "${lines[0]}" = "mise run lint-squash-msg" ]
  [ "${lines[1]}" = ".claude/skills/review-squash-message/SKILL.md" ]
}

@test "the scripts suppress no shellcheck rule, and never reach for an admin or auto merge or a CI run" {
  run grep -c "shellcheck disable" "$scripts"/*.sh
  [ "$status" -eq 1 ]
  run grep -E -- "--admin|--auto|--delete-branch|gh run |gh workflow|rm -f|--no-verify" "$scripts/merge.sh" "$scripts/review.sh" "$scripts/preflight.sh" "$scripts/common.sh"
  [ "$status" -eq 1 ]
}

# --- preflight -----------------------------------------------------------

@test "preflight prints the state, each check by its latest run, and the answers" {
  edit_pr '.statusCheckRollup = [
    {"__typename":"CheckRun","name":"lint","workflowName":"ci","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T10:00:00Z"},
    {"__typename":"CheckRun","name":"lint","workflowName":"ci","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2026-10-14T11:00:00Z"},
    {"__typename":"CheckRun","name":"test","workflowName":"ci","status":"IN_PROGRESS","startedAt":"2026-10-14T11:00:00Z"}]'
  run bash "$scripts/preflight.sh" 7
  [ "$status" -eq 0 ]
  [[ $output == *"state: OPEN"* ]] || false
  [[ $output == *"base: main"* ]] || false
  [[ $output == *"head oid: $head"* ]] || false
  [[ $output == *"draft: false"* ]] || false
  [[ $output == *"mergeStateStatus: CLEAN"* ]] || false
  [[ $output == *"ok"*"ci / lint (COMPLETED/SUCCESS)"* ]] || false
  [[ $output == *"pending"*"ci / test"* ]] || false
  [[ $output == *"checks: still running"* ]] || false
  [[ $output == *"merge_ready_cmd: ready"* ]] || false
  [[ $output == *"SQUASH_AGENTMSG is ignored: ok"* ]] || false
  [ "$(cat "$tmp/ready.calls")" = "$(git rev-parse --show-toplevel) 7 $head" ]
}

@test "preflight prints the briefing the message is written from" {
  run bash "$scripts/preflight.sh" 7
  [[ $output == *"subject: feat: add a second note (#7)"* ]] || false
  [[ $output == *"Why: the first note stood alone."* ]] || false
  [[ $output == *"--- $head"* ]] || false
  [[ $output == *"The first note stood alone."* ]] || false
  [[ $output == *"1+ 0- notes.txt"* ]] || false
  [[ $output == *"Signed-off-by: Test Agent <agent@example.com>"* ]] || false
}

@test "preflight lists the owner paths the pull request touches" {
  advance_feature owned/rule.txt
  run bash "$scripts/preflight.sh" 7
  [[ $output == *"owner path touched: yes"* ]] || false
  [[ $output == *"owned/rule.txt"* ]] || false
}

@test "preflight reports a readiness that says no, an unset one, and a failing check" {
  printf '1\n' >"$tmp/ready-status"
  edit_pr '.statusCheckRollup = [{"name":"lint","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2026-10-14T11:00:00Z"}]'
  run bash "$scripts/preflight.sh" 7
  [ "$status" -eq 0 ]
  [[ $output == *"merge_ready_cmd: NOT READY (exit 1)"* ]] || false
  [[ $output == *"checks: FAILING"* ]] || false
  export AGENT_SKILLS_MERGE_READY_CMD=
  run bash "$scripts/preflight.sh" 7
  [[ $output == *"merge_ready_cmd: not configured"* ]] || false
}

@test "preflight never fails, whatever is missing" {
  run bash "$scripts/preflight.sh"
  [ "$status" -eq 0 ]
  [[ $output == *"usage"* ]] || false
  run bash "$scripts/preflight.sh" 99
  [ "$status" -eq 0 ]
  [[ $output == *"couldn't read #99"* ]] || false
  touch "$fx/fail-view"
  run bash "$scripts/preflight.sh" 7
  [ "$status" -eq 0 ]
  unlink "$fx/fail-view"
  git --git-dir="$origin" update-ref -d refs/pull/7/head
  run bash "$scripts/preflight.sh" 7
  [ "$status" -eq 0 ]
  [[ $output == *"UNREADABLE"* ]] || false
  mkdir "$tmp/without-gh"
  for tool in cat sed awk grep tr dirname git jq; do
    ln -s "$(command -v "$tool")" "$tmp/without-gh/$tool"
  done
  run env PATH="$tmp/without-gh" /bin/bash "$scripts/preflight.sh" 7
  [ "$status" -eq 0 ]
  [[ $output == *"MISSING: gh and jq"* ]] || false
}

@test "preflight says when SQUASH_AGENTMSG isn't ignored" {
  printf '.claude/worktrees/\n' >.gitignore
  run bash "$scripts/preflight.sh" 7
  [[ $output == *"MISSING: SQUASH_AGENTMSG isn't in .gitignore"* ]] || false
}

# --- the guard -----------------------------------------------------------

guard() {
  run bash -c "printf '%s' \"\$1\" | $(hook_command)" _ "$(jq -n --arg c "$1" '{tool_input: {command: $c}}')"
}

# Each line is one command the guard has to refuse.
@test "the guard refuses a gh pr merge however it is spelled" {
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 2 ] || { echo "not refused: $command"; false; }
  done <<'EOF'
gh pr merge 7
gh pr merge 7 --squash
gh pr merge --auto 7
gh pr merge 7 --admin
gh  pr   merge 7
gh -R tbhb/widgets pr merge 7
gh --repo tbhb/widgets pr merge 7
GH_REPO=tbhb/widgets gh pr merge 7
command gh pr merge 7
env gh pr merge 7
/opt/homebrew/bin/gh pr merge 7
\gh pr merge 7
"gh" pr merge 7
gh "pr" "merge" 7
gh pr mer""ge 7
gh pr merge
cd x && gh pr merge 7
echo hi |xargs gh pr merge
{ gh pr merge 7; }
if true; then gh pr merge 7; fi
x=$(gh pr merge 7)
x=`gh pr merge 7`
bash -c 'gh pr merge 7'
eval "gh pr merge 7"
time gh pr merge 7
! gh pr merge 7
gh alias set m 'pr merge'
gh alias import aliases.yml
EOF
}

@test "the guard refuses a merge across a continuation, in a here-document, and after one" {
  guard "$(printf 'gh pr \\\n  merge 7')"
  [ "$status" -eq 2 ]
  guard "$(printf 'bash <<EOF\ngh pr merge 7\nEOF')"
  [ "$status" -eq 2 ]
  guard "$(printf 'cat >a <<-EOF\n\tbody\n\tEOF\ngh pr merge 7')"
  [ "$status" -eq 2 ]
}

@test "the guard refuses a gh api call that writes to a merge path" {
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 2 ] || { echo "not refused: $command"; false; }
  done <<'EOF'
gh api -X PUT repos/tbhb/widgets/pulls/7/merge
gh api --method PUT repos/tbhb/widgets/pulls/7/merge
gh api --method=PUT repos/tbhb/widgets/pulls/7/merge
gh api repos/tbhb/widgets/pulls/7/merge -X PUT -f merge_method=squash
gh api repos/tbhb/widgets/pulls/7/merge -f merge_method=squash
gh api -X PUT /repos/tbhb/widgets/pulls/7/merge
gh api "repos/tbhb/widgets/pulls/7/merge" --method PUT
gh api repos/tbhb/widgets/pulls/7/merge --input body.json
gh api -X POST repos/tbhb/widgets/merges -f base=main -f head=feature
gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: "x"}) { clientMutationId } }'
gh api graphql -f query='mutation { enablePullRequestAutoMerge(input: {pullRequestId: "x"}) { clientMutationId } }'
gh -R tbhb/widgets api repos/tbhb/widgets/pulls/7/merge -X PUT
env GH_TOKEN=x gh api -X PUT repos/tbhb/widgets/pulls/7/merge
gh api repos/tbhb/widgets/pulls/7 && gh api -X PUT repos/tbhb/widgets/pulls/7/merge
EOF
}

@test "the guard allows reading, the script, and everyday commands" {
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 0 ] || { echo "refused: $command"; false; }
  done <<'EOF'
gh pr view 7
gh pr view 7 --json state
gh pr list --state open
gh pr checks 7
gh pr diff 7
gh pr create --title x --body y
gh pr edit 7 --title x
gh pr ready 7
gh pr close 7
gh api repos/tbhb/widgets/pulls/7
gh api repos/tbhb/widgets/pulls/7/files
gh api -X DELETE repos/tbhb/widgets/git/refs/heads/feature
gh api -X GET repos/tbhb/widgets/pulls/7/merge
gh api --method GET repos/tbhb/widgets/pulls/7/merge
gh api --method=GET repos/tbhb/widgets/pulls/7/merge
gh run list
git merge --ff-only main
git merge-base main HEAD
git log --oneline | grep merge
bash .claude/skills/agent-merge-pr/scripts/review.sh 7
bash .claude/skills/agent-merge-pr/scripts/preflight.sh 7
bash .claude/skills/agent-merge-pr/scripts/merge.sh 7 0123456789abcdef0123456789abcdef01234567
mise run lint-squash-msg
gh alias list
EOF
}

@test "the guard also refuses a command that only mentions the merge" {
  guard 'echo "then run gh pr merge 7"'
  [ "$status" -eq 2 ]
  [[ $output == *"only mentions the merge is refused too"* ]] || false
  [[ $output == *"scripts/merge.sh"* ]] || false
}

@test "the guard needs no arming and no skill state" {
  cd "$tmp"
  guard "gh pr merge 7"
  [ "$status" -eq 2 ]
  cd "$repo"
  guard "gh pr merge 7"
  [ "$status" -eq 2 ]
}

@test "a guard that can't read its input refuses a merge, and lets the rest through" {
  mkdir "$tmp/without-jq"
  for tool in git cat sed awk grep tr dirname bash; do
    ln -s "$(command -v "$tool")" "$tmp/without-jq/$tool"
  done
  run env PATH="$tmp/without-jq" /bin/bash -c "printf '%s' '{\"tool_input\":{\"command\":\"gh pr merge 7\"}}' | $(hook_command)"
  [ "$status" -eq 2 ]
  [[ $output == *"needs jq"* ]] || false
  run env PATH="$tmp/without-jq" /bin/bash -c "printf '%s' '{\"tool_input\":{\"command\":\"git status\"}}' | $(hook_command)"
  [ "$status" -eq 0 ]
  run bash -c "printf '%s' 'not json at all, gh pr merge' | $(hook_command)"
  [ "$status" -eq 2 ]
  run bash -c "printf '%s' 'not json at all' | $(hook_command)"
  [ "$status" -eq 0 ]
}
