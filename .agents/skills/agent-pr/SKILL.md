---
name: agent-pr
description: >-
  Open or update a pull request as an autonomous agent, with no operator step. Draft the title, the properties, and the template's sections in PR_AGENTDESC.md, run the mechanical checks, get an independent review that stamps the exact bytes, and publish through a script. That script pushes the branch. It also sets the draft state the description gives, and reports the number and the head. Use this whenever an agent session, a subagent, or a headless run in this repository has a branch to open, to update, or to move between draft and ready. The operator's own interactive sessions use the `pr` skill instead.
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: "${CLAUDE_PROJECT_DIR}/.claude/skills/agent-pr/scripts/guard.sh"
---

# Open a pull request as an agent

This skill publishes a pull request without asking anyone. Its authority is the independent review and the gates in `publish.sh`. Each check the `pr` skill runs still runs here. Those checks are the template check and the prose lint, then an independent review of the exact bytes. The operator's confirmation is the one step this skill removes.

Scripts define each rule. In a peer session, in a subagent, and in a headless run, the skill works the same. The guard hook in this file's frontmatter is a rail for the session that loaded the skill. It doesn't fire inside a subagent, and nothing depends on it. It reads each command as text and refuses one that mentions a `gh pr` verb that changes a pull request, even inside a quoted note. It also refuses a `gh api` call that writes to a `/pulls` path.

`PR_AGENTDESC.md` is the one source for the pull request. Its frontmatter gives the base branch, the labels, and whether the pull request is a draft, and `publish.sh` makes GitHub match each of them. To mark a pull request ready, set `draft: false` and publish again.

## Steps

Work through these in order. Stop at the first one that fails, and report what failed.

1. Run the preflight.
2. Draft `PR_AGENTDESC.md`.
3. Run the checks on the draft.
4. Get the independent review.
5. Publish.
6. Report.

## Step 1: preflight

```bash
bash .claude/skills/agent-pr/scripts/preflight.sh
```

It prints the branch, the base, the commits that would merge, any open pull request for the branch, and the signed-in account. A fresh worktree has no Vale styles, because `.vale/` isn't tracked, so run `vale sync` (or `mise run install-tools`) there before the checks in step 3. Stop when the branch has nothing to merge, when a rebase, a merge, a cherry-pick, or a revert is in progress, when you stand on a detached `HEAD`. Report the gap and stop.

Commit everything first, through the `agent-commit` skill. The review reads the branch as committed, and a commit made after the review voids it.

## Step 2: draft the description

When the file doesn't exist yet, start from the scaffold:

```bash
bash .claude/skills/pr/scripts/scaffold-description.sh main
```

Then write the file yourself. When this session has loaded the installed `pr` skill too, that skill's hook refuses a direct edit of `PR_AGENTDESC.md`. In that session, draft through `Skill(write-pr-description)`.

- **Frontmatter.** `validate-description.sh` requires `base`, `draft`, and `labels`, and `labels` can't be empty. Use labels the repository has: `gh label list` shows them, and `publish.sh` refuses a name that isn't there. Labels come from the repository's existing list, and a name the list lacks is one to leave out, since adding a label is a separate change. The `owner-review` label asks for the owner's review by name, and touching an owner path doesn't by itself call for it.
- **Title.** One `# <type>(<scope>): <description>` heading. It becomes the squash subject, so it follows the repository's commit rules: the type is the highest-ranked type among the branch's commits, and the scope is the one scope the commits use.
- **Sections.** Fill each section the template lists, in its order. Say what the branch does and why. Give the commands that verified it, and state the real risk.
- **Related.** List issue numbers, such as `#12`, or write `None`. Put a pull request's number in the prose of another section.
- **Leave out** counts of anything, a restated diff, and how the work came about: requests, review rounds, sessions, and models.

## Step 3: run the checks

```bash
bash .claude/skills/agent-pr/scripts/check.sh
```

It runs the repository's description lint, then checks the log line, the Related section, and the title against the commits. Fix what it reports and run it again. Do this before the review, because the review signs exact bytes and any later edit voids it.

## Step 4: the independent review

```bash
bash .claude/skills/agent-pr/scripts/review.sh
```

The script first copies the draft and records the branch head and its merge base. A directory of this run's own then receives the commits, the changed paths, the diff as text, and the template, each read from the recorded commits. Last, the script starts a fresh reviewer process on the model the configuration gives. That reviewer has no shell. Its tools only read and search. You don't relay its verdict: `review.sh` reads the reply, and it stamps the draft only when the whole reply is the passing verdict. Its stamp covers the copy's bytes together with the branch head and its merge base.

- **Exit 0.** Stamped. Go to step 5 without editing the draft or committing.
- **Exit 2.** Changes required, with the findings printed. Fix each one, run step 3 again, then run this step again. A finding you think is wrong about the branch still blocks. Leave that text unchanged, and explain why when the bound stops the review.
- **Exit 3.** Blocked. The review has returned findings more times than the bound allows. The script stamped nothing and reported a `blocker` with the last findings. Stop, and publish nothing. Each later review exits 3 as well, until `review.sh --unblock` clears the bound. Run that only on the owner's word.
- **Exit 4 or 5.** The reply had no verdict, or the draft, the head, or the merge base changed while the review ran. Run the step again.

The bound is three finding rounds unless the configuration says otherwise. A round that a new commit or a rebase caused still counts, so settle the branch before the first review. Findings block whatever their tags, and no path publishes with one open. When a finding gives `fix:` text, apply it verbatim, since a reworded fix often draws a new finding.

## Step 5: publish

```bash
bash .claude/skills/agent-pr/scripts/publish.sh
```

The script refuses during a merge, a cherry-pick, a revert, or a rebase, and on a detached `HEAD`. It also refuses when a check fails, when the stamp doesn't match the draft, the head and the merge base, or when the signed-in `gh` account isn't the agent's own. Because those steps run other programs, a second stamp check comes before the push. After it, the script publishes from its own copy of the reviewed draft, as a new pull request or as an update to the open one, and an update sets the base, the title, the body, the labels, and the draft state to the description's values.

The push only ever moves the branch forward. When origin holds commits that `HEAD` doesn't contain, the script refuses and reports a `blocker`, because this skill opens pull requests and doesn't rewrite history. A branch whose history you rewrote on purpose takes `--rewrite`, on the owner's word alone. Even then the script refuses when `HEAD` is behind origin, or when origin holds a commit this checkout hasn't fetched. Otherwise it leases on the exact remote commit it read, and its report says it rewrote the branch.

- **Exit 0.** Published. The last line it prints has the pull request's URL and the full head, and the report states both.
- **Exit 1.** Refused, with the reason. Fix the cause before you retry.

## Step 6: report

`publish.sh` reports a `fixed` message with the pull request's number and its full 40-character head. With no mailbox configured, as here, it prints the report. Add nothing to that message by hand. In your own reply, give the URL and say whether the pull request is a draft.

A later change to the branch or the description repeats steps 3 to 5. The merge belongs to the `agent-merge-pr` skill.

## Beside the `pr` skill

Both skills can load in one session. Each hook refuses a direct `gh pr create`, `gh pr edit`, and `gh pr ready`, and each refusal points at that hook's own script. Follow this skill's script. `publish.sh` runs `gh` inside a script, where neither hook looks, so it works whichever hooks are active.

## Configuration

`.claude/skills/agent-commit/agent-skills.conf` contains the repository's settings for this skill too. Among them are the reviewer and the round bound, the description lint with its Related pattern and title check, and the mailbox, which is empty here so reports print. The environment and an optional `~/.config/agent-skills/<repository>.conf` override any key. A replacement reviewer, `review_cmd`, comes from those two places alone, and the scripts ignore it in the repository's file. Also, `gh_login`, the agent's GitHub account, defaults to `tbhb-agent` and changes only through the environment or the machine's file. If it's empty, `publish.sh` refuses before it writes anything. An empty `lint_pr_cmd` fails the checks, because the description lint has to run.

## Tests

`bats .claude/skills/agent-pr/tests` runs the scripts against a scratch repository, with a stand-in for `gh`, and the reviewer. Run it directly: `mise run test` runs `cargo test` and doesn't include it.
