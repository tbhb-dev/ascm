---
name: agent-commit
description: >-
  Commit as an autonomous agent, with no operator step. Group the change into one atomic commit, draft a Conventional Commits message in COMMIT_AGENTMSG, clear the prose and commit-msg gates, get an independent review that stamps the exact bytes, and commit through a script that signs. Use this whenever an agent session, a subagent, or a headless run in this repository has work to commit. The operator's own interactive sessions use the `commit` skill instead.
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: "${CLAUDE_PROJECT_DIR}/.claude/skills/agent-commit/scripts/guard.sh"
---

# Commit as an agent

This skill commits without asking anyone. Its authority is the independent review and the gates in `commit.sh`. Every check the `commit` skill runs still runs here. Those checks are the prose lint and the commit-msg hooks, then an independent review of the exact bytes and a signed commit. The operator's confirmation is the only step removed.

Scripts define each rule. In a peer session, in a subagent, and in a headless run, the skill works the same. The guard hook is a rail for the session that loaded this skill. It doesn't fire inside a subagent, and nothing depends on it. While armed, it reads each command as text and refuses one that mentions `git commit` or a whole-tree `git add`, even inside a quoted note.

## Steps

Work through these in order. Stop at the first one that fails, and report what failed.

1. Run the preflight.
2. Group the change and stage it.
3. Draft `COMMIT_AGENTMSG`.
4. Run the checks on the draft.
5. Get the independent review.
6. Commit.
7. Report.

## Step 1: preflight

```bash
bash .claude/skills/agent-commit/scripts/preflight.sh
```

It prints the worktree state, the staged paths, and the trailer identity. It also activates the guard hook. A fresh worktree has no Vale styles, because `.vale/` isn't tracked, so run `vale sync` (or `mise run install-tools`) there before any lint task. Stop when it reports a rebase, merge, or cherry-pick in progress, or a missing precondition. Report the gap and stop.

## Step 2: group and stage

A commit contains one logical change that a reader can state in a sentence and revert as a unit. Put a change and its tests in one commit, unless the repository's commit rules put them in separate types. Split unrelated work into separate commits, and run this skill once for each.

Stage named paths, and never the whole tree:

```bash
git add -- path/one path/two
git diff --cached --stat
```

## Step 3: draft the message

Write `COMMIT_AGENTMSG` at the repository root.

- **Subject.** `<type>(<scope>): <description>`, imperative, lowercase after the colon, no trailing period, 72 characters at most.
- **Type and scope.** The type is the Conventional Commits type that fits the change. The scope is the one area the staged paths change, such as a crate, `docs`, `workloads`, or `tooling`. `git log --oneline` shows the scopes already in use, so reuse one before inventing another.
- **Body.** Say why the change exists. Leave out what the diff already shows, counts of anything, and how the work came about: requests, review rounds, sessions, and models. Plain text, wrapped at 72 characters. Backticks around a literal identifier are fine.
- **Trailers.** `Assisted-by: <harness>:<model> <tool>` first, then the `Signed-off-by:` line the preflight printed. Never credit a model through `Co-authored-by`.

## Step 4: run the checks

```bash
mise run lint-commit-msg
```

Fix what it reports and run it again. It runs the hooks the commit itself runs: trailers, `commitlint`, the prose rules, spelling, and whatever the repository adds, such as the scope check. Do this before the review, because the review signs exact bytes and any later edit voids it.

## Step 5: the independent review

```bash
bash .claude/skills/agent-commit/scripts/review.sh
```

Add `--amend` when the commit replaces the one at `HEAD`. The script takes one snapshot of the draft, the staged tree, and the commit at `HEAD`, and writes the draft and the diff from that snapshot to files. It then starts a fresh reviewer process on the model the configuration gives. That reviewer has no shell, and its tools only read and search. You don't relay its verdict: `review.sh` reads the reply, and it stamps the draft only when the whole reply is the passing verdict. Its stamp covers the draft's bytes, the staged tree, the commit at `HEAD`, and whether the review read an amend.

- **Exit 0.** Stamped. Go to step 6 without touching the draft or the index.
- **Exit 2.** Changes required, with the findings printed. Fix each one, run step 4 again, then run this step again. A finding you think is wrong about the diff still blocks. Leave that text unchanged, and explain why when the bound stops the review.
- **Exit 3.** Blocked. The review has returned findings more times than the bound allows. The script stamped nothing and reported a `blocker` with the last findings. Stop, and leave everything staged. Each later review exits 3 as well, until `review.sh --unblock` clears the bound. Run that only on the owner's word.
- **Exit 4 or 5.** The reply had no verdict, or the draft, the index, or `HEAD` changed while the review ran. Run the step again.

The bound is three finding rounds unless the configuration says otherwise. Findings block whatever their tags, and no path commits with one open. When a finding gives `fix:` text, apply it verbatim, since a reworded fix often draws a new finding.

## Step 6: commit

```bash
bash .claude/skills/agent-commit/scripts/commit.sh
```

Add `--amend` to match step 5. The script refuses unless commits here sign with SSH, by a key the configuration names, and git would run the repository's own hooks. A stamp that doesn't match everything the review read is a refusal too. Next come the commit-msg hooks, once more. After that the script checks the stamp a second time, and commits its own copy of the reviewed message, always signed, with the session's signing configuration. If a hook changes the tree, the message, or the parents during the commit, or the commit has no good signature from that key, the script moves the branch back to where it started. That move applies only to the branch the run started on, and only off the commit this run made. In any other state the script leaves the branch alone, and either way a `blocker` report goes out. A commit that a hook already pushed is still on the remote, and the report says so. It never takes a message, and it never skips a hook.

- **Exit 0.** Committed. It prints the full id of the commit.
- **Exit 1.** Refused, with the reason. Don't retry it before you fix the cause.
- **Exit 75.** Held because signing failed. Don't remove anything from the index, don't redraft, and don't look for another way to sign. Wait for word that the cause no longer applies, then run `commit.sh` again. When the hooks changed the index or the draft before signing failed, the script says so: the stamp no longer covers them, so run step 5 again first.

## Step 7: report

Say which commit succeeded, and list every group still uncommitted. A commit on its own doesn't send mail, because the pull request reports for the branch through the `agent-pr` skill. A report that `commit.sh` does send, such as a `blocker`, goes to the mailbox the configuration names. This repository names none. The script prints the report instead, and you copy it into your reply.

## Beside the `commit` skill

Both skills can load in one session. Each guard refuses a direct `git commit` and points at its own script, so the message you see depends on which skill armed first. Follow this skill's script. `commit.sh` runs git inside a script, where neither guard looks, so it works whichever guards are live.

## Configuration

`.claude/skills/agent-commit/agent-skills.conf` contains the repository's settings. It sets the reviewer's model and effort, the round bound, and the owner-path pattern. An empty mailbox setting makes reports print. The environment and an optional `~/.config/agent-skills/<repository>.conf` override any key. A replacement reviewer, `review_cmd`, comes from those two places alone, and the scripts ignore it in the repository's file.

## Tests

`bats .claude/skills/agent-commit/tests` runs the scripts against a scratch repository, with a stand-in reviewer. Run it directly: `mise run test` runs `cargo test` and doesn't include it.
