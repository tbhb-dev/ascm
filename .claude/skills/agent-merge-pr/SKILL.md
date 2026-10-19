---
name: agent-merge-pr
description: >-
  Squash merge a pull request as an autonomous agent, with no operator step. Draft the squash message in SQUASH_AGENTMSG, pass the commit-msg checks, get an independent review that stamps the exact bytes and the pull request's head commit, and merge through a script that pins the merge to the reviewed head, and reads the live pull request and the checks again before it merges. Use this whenever an agent session, a subagent, or a headless run in this repository has an approved pull request to land. The operator's own interactive sessions use the `merge-pr` skill instead.
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: "${CLAUDE_PROJECT_DIR}/.claude/skills/agent-merge-pr/scripts/guard.sh"
---

# Merge a pull request as an agent

This skill merges without asking anyone. Its authority is the independent review and the gates in `merge.sh`. Every check the `merge-pr` skill runs still runs here. Those checks are the commit-msg lint and an independent review of the exact bytes, then the state of the pull request. The operator's confirmation is the only step removed. A merge can't be undone, so this skill adds checks the operator's version doesn't have. It re-reads the pull request each time and pins the merge to the head commit that the review read.

Scripts define each rule. The skill works the same in a peer session, in a subagent, and in a headless run. The guard hook is a rail for the session that loaded this skill. It doesn't fire inside a subagent, and nothing depends on it. It reads each command as text and refuses `gh pr merge` in any spelling, and a `gh api` call that writes to a merge path, even inside a quoted note.

## Steps

Work through these in order. Stop at the first one that fails, and report what failed.

1. Run the preflight.
2. Draft `SQUASH_AGENTMSG`.
3. Lint the draft.
4. Get the independent review.
5. Merge.
6. Report.

## Step 1: preflight

```bash
bash .claude/skills/agent-merge-pr/scripts/preflight.sh <pr>
```

It prints the pull request's state, base, head commit, draft flag, and merge state. Each check shows the verdict of its latest run, because a re-run leaves the superseded run in the list. The preflight also prints the required reviewer's verdict and the readiness answer. After those comes a briefing with the title, the published description, every commit message the squash collapses, the list of changed files, and the sign-off line. It never fails, and it leaves the repository as it found it.

Stop when it reports a draft pull request, a failing or running check, or a missing precondition. A refusal isn't yours to work around: report `needs-owner` and stop. A failing check goes to `fix-pr`, and a running one to `watch-pr`.

The installed `squash-message.sh` doesn't serve here. It overwrites `SQUASH_AGENTMSG` with a skeleton and calls `gh auth status` to decide whether it can run, so an unattended run can't rely on it.

When the preflight says `SQUASH_AGENTMSG` isn't ignored, report it and stop. This skill leaves `.gitignore` alone.

## Step 2: draft the message

Write `SQUASH_AGENTMSG` at the repository root.

- **Subject.** The pull request title, a space, and `(#<pr>)`. The preflight prints it. The title follows the repository's commit rules: its type is the highest-ranked type among the commits, and its scope is the pull request's one scope. When the title describes one commit rather than the branch, fix the title on the pull request first. `merge.sh` reads the number back to confirm that the draft belongs to this pull request.
- **Body.** Say why the branch exists, from the description's reasons and from the reasons in the commit bodies. The squash is the last place a commit body appears, so a reason the description never states belongs in the message. Trust the commits over a description that predates them. Leave out what the diff already shows, verification notes, counts of anything, how the work came about, and Markdown. Write one account of the branch instead of one paragraph for each commit. Plain text, wrapped at 72 characters.
- **Closing references.** Add a `Closes #<n>` line only for an issue this branch closes. A pull request, a document, or a reference in another repository never goes there.
- **Trailers.** `Assisted-by: <harness>:<model> <tool>` first, then the `Signed-off-by:` line the preflight printed. Never credit a model through `Co-authored-by`.

## Step 3: lint the draft

```bash
mise run lint-squash-msg
```

Fix what it reports and run it again. `review.sh` and `merge.sh` run the same command from the `lint_squash_cmd` setting, and an empty setting refuses both. Lint before the review, because the review signs exact bytes and a later edit voids the stamp.

## Step 4: the independent review

```bash
bash .claude/skills/agent-merge-pr/scripts/review.sh <pr>
```

The script lints the draft, then takes one snapshot of it: a copy of the draft and the pull request's head commit, read once. Its title, description, and per-file change counts come from the one response that reported that head. Every commit message and the diff come from git, from the merge base with the base branch to that head, whatever the checkout has out. A fresh reviewer process on the model the configuration gives reads them as files. It has no shell, and its tools only read and search. You don't relay its verdict. Instead, the script reads the reply and stamps the draft only when the whole reply is the passing verdict. A stamp covers the draft's bytes, the pull request number, the head commit, and the base branch with its commit, and the script re-reads the head before it writes one.

- **Exit 0.** Stamped. Go to step 5 without touching the draft.
- **Exit 2.** Changes required, with the findings printed. Fix each one, lint again, then run this step again. A finding you think is wrong about the commits still blocks. Each finding cites one instance, so after the fix, read the rest of the message for the same fault.
- **Exit 3.** Blocked. The review has returned findings more times than the bound allows. The script stamped nothing and reported a `blocker` with the last findings. Stop. Each later review exits 3 as well, until `review.sh <pr> --unblock` clears the bound. Run that only on the owner's word.
- **Exit 4 or 5.** The reply had no verdict, or the draft or the head changed while the review ran. Run the step again.

The bound is three finding rounds unless the configuration says otherwise. Findings block whatever their tags. When a finding gives `fix:` text, apply it verbatim.

## Step 5: merge

```bash
bash .claude/skills/agent-merge-pr/scripts/merge.sh <pr> <head>
```

Pass the head commit the review stamped, in full, as the preflight printed it. At each condition below, the script re-reads the live pull request, and it refuses unless every one holds. They run in this order:

1. The stamp covers the draft's bytes and this pull request. The head it records matches the one you passed and the one the pull request has now. The base branch and its commit match the ones the review read, so a pull request moved to another base, or a base that moved since, goes back to review.
2. `gh` signed in as the agent's own account, the one `gh_login` names, `tbhb-agent` unless the environment or the machine's file says otherwise. A merge can't be undone, and `gh` acts as whichever account holds the session.
3. An open pull request that isn't a draft, without the `owner-review` label. That label sends a pull request to the owner.
4. An approval from the account `required_reviewer` names. Its latest approving, change-requesting, or dismissing review has to be an approval, and the review has to be on the pull request's current head. A change request, a dismissal, no review, an approval of an older head, or an empty `required_reviewer` refuses the merge. A comment or a pending review doesn't count.
5. A merge state of `CLEAN`. `BEHIND` and `DIRTY` go to the `rebase` skill.
6. Every check, judged by its latest run alone, succeeded or was neutral or skipped. A run that hasn't finished refuses, and so does a pull request with no checks at all. Each check the base branch requires has a latest run that succeeded, since a skipped or neutral run never did the check's work. The required checks come from the rulesets GitHub applies and from the ruleset files in `ruleset_dir` as the base commit has them, and when neither lists a check the merge refuses. The script never re-runs, cancels, or deletes a run.
7. A readiness command, `merge_ready_cmd`, that exits 0 when run as `<cmd> <repo-root> <pr> <head>`. With no command set, as here, this step passes, and the required checks and the merge state still apply.
8. No golden file among the changed paths, by the `golden_paths` pattern. A golden file changes only on the owner's review of its baseline diff.
9. No loosened gate in the diff that the script detects. It looks for a suppression marker on an added line (`loosening_markers`). In a gate or lint configuration file (`gate_config_paths`), it also looks for an added exclusion and a removed hook or lint task. That check is a backstop. A loosened gate it can't see is the author's and the reviewers' to flag, and such a pull request goes to the owner.
10. Owner paths, by the `owner_paths` pattern, don't stop the merge. Commits supply the changed paths, and a rename counts under both names. The script lists the owner paths in its output and in the report, and the reviewer's approval is the gate.
11. A passing commit-msg lint.

After those commands, the script checks the stamp a second time. Any open pull request that sits on this one's branch then gets this one's base. Next the script merges from its own copy of the reviewed body, pinned to the reviewed head with `--match-head-commit`, and never with `--admin` or `--auto`. Last, it confirms that GitHub shows the pull request as merged and that the merge commit has the reviewed message. Then it tidies up:

- It deletes the remote head branch, unless the branch is in a fork or no longer points at the merged head. A push that landed after the merge stays, and the report says so.
- Your main checkout moves forward to the merge, but only while it's on the base branch with no changes to tracked files.
- Its worktree and local branch go too, but only when the worktree is under `.claude/worktrees/` with no changes, you aren't standing in it, and the branch is still at the reviewed head. An ignored file keeps the worktree too, unless the `disposable_ignored` pattern lists it as a cache or a consumed draft, so local configuration such as `.env` survives.
- Finally, it removes the draft and the review state, then reports `fixed` with the pull request number, the full merge commit id, and any owner paths the pull request changed.

The exit code says how it went.

- **Exit 0.** Merged. It prints the full id of the merge commit.
- **Exit 1.** Refused or failed, with the reason. A refusal for a golden file or a loosened gate went out as a `blocker` report. Don't retry it. A merge that ends with another message than the reviewed one, or that GitHub doesn't show as merged, reports a `blocker` and leaves everything in place.

## Step 6: report

Say which pull request merged and the id of its merge commit. List what the script left in place and why, such as a worktree with changes or a checkout on another branch. The report goes to the mailbox the configuration names. With none configured, as here, the script prints it.

## Beside the `merge-pr` skill

Both skills can load in one session. The installed guard refuses any `gh pr` verb that changes a pull request, and points at its own script. Follow this skill's script. `merge.sh` runs `gh` inside a script, where neither guard looks, so it works whichever guards are live.

## Configuration

`.claude/skills/agent-commit/agent-skills.conf` holds the repository's settings, shared with the other agent skills. Merging reads `review_squash_criteria`, `lint_squash_cmd`, and `merge_verify_tries`, plus the reviewer's model and effort, the round bound, the mailbox (empty here, so reports print), and the owner-path pattern. The environment and an optional `~/.config/agent-skills/<repository>.conf` override any key. The readiness command (`merge_ready_cmd`), the agent's GitHub account (`gh_login`, default `tbhb-agent`), and a replacement reviewer (`review_cmd`) come from those two places alone. None works from the repository's file. Before it reads the pull request, `merge.sh` checks that each pattern setting is one `grep` reads, and refuses when one is empty or broken, because a pattern that doesn't match anything would let its check pass. Preflight reports a broken one too.

## Tests

`bats .claude/skills/agent-merge-pr/tests` runs the scripts against a scratch repository with a bare remote. The tests use stand-ins for `gh`, the reviewer, the readiness command, and the mailbox. Run them directly: `mise run test` runs `cargo test` and doesn't include them.
