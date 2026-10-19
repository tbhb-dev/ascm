---
type: Repository Guide
title: Agent guidance
description: Repository layout and contributor commands for ascm.
tags: [ascm, agents]
generated: { by: "codex:gpt-6.1-sol", at: "2026-10-18" }
---

# Agent guidance

## What this is

ascm (agent source control management) is one Go binary that runs coding agents' git and GitHub workflows (commit, PR, merge, review, issues, and comments). Each workflow starts as a draft, then moves through launched reviews and gates. It records content-bound receipts in git refs and writes verifiable attribution trailers. Every commit traces back to the agent sessions that wrote and reviewed it, including the session that merged it.

P1 sets up development tools and draft docs. It has no Go source or module yet.

## Layout

`docs/` holds the OKF docs bundle. `scripts/okf-check.py` checks its format and the Markdown frontmatter across the repository.

`.devtools/` and `.config/mise/conf.d/` hold the devtools v0.12.0 payload. The `vendir` tool owns both directories. Edit repository settings in the root configuration files.

`.agents/skills/agent-*` holds the local agent skills. APM deploys skills and rules to `.agents/` and `.claude/` from `apm.yml`.

## Commands

Read `mise.toml` before trusting it. In each fresh worktree, run `mise trust`, then `mise run bootstrap` to sync the payload and install the tools and hooks.

| Command | Purpose |
| --- | --- |
| `mise run devtools:apm-sync` | Deploy agent tooling |
| `mise run lint` | Check source gates |
| `mise run check` | Check docs and test the local skills |
| `mise run test-skills` | Run the local skills' bats suites |
| `mise run lint-skills` | Check skill prose and scripts |
| `mise run check-docs` | Check the docs bundle and repository Markdown |
| `mise run devtools:prek-all` | Run applicable hooks |

## Go toolchain

The shared payload pins Go 1.27.2. Use `mise exec -- go version` to inspect it. P2 adds the module `github.com/tbhb-dev/ascm` with `go 1.27` and `toolchain go1.27.2`. P2 also adds local build and test tasks.

The root configuration includes the shared Go task file. P1's lint and check aggregates don't invoke its tasks.

## Conventions

Start Markdown files with OKF frontmatter. The docs index declares `okf_version: "0.2"`. Draft docs record `status: draft` and their generation metadata.

Keep each prose paragraph on one line. Run `mise run lint-draft <file>` on a draft before copying it to its final path. Vale checks prose, cspell checks spelling, and rumdl checks Markdown structure. The guard-markdown hook checks paragraph wrapping.

Change the source configuration and regenerate outputs through vendir or APM. Keep local private values in the ignored `mise.local.toml`. Never commit drafts or build outputs.

## Identities

Sign commits as `tbhb-agent`. End each commit message with an `Assisted-by` trailer followed by a `Signed-off-by` trailer. Run `mise run lint-commit-msg` on `COMMIT_AGENTMSG` before committing through the hooks.

Use the agent GitHub account for git network operations and GitHub commands. Post PR reviews only through `gh-reviewer`, which uses `tbhb-agent-reviewer`.
