---
type: Repository Guide
title: Agent guidance
description: Repository layout and contributor commands for ascm.
tags: [ascm, agents]
generated: { by: "codex:gpt-6.1-sol", at: "2026-10-18" }
---

# Agent guidance

## What this is

Plans for ascm (agent source control management) call for one Go binary to run coding agents' git and GitHub workflows (commit, PR, merge, review, issues, and comments). Each planned workflow starts as a draft, then moves through launched reviews and gates. The design calls for content-bound receipts in git refs and verifiable attribution trailers that trace every commit back to the agent sessions that wrote and reviewed it, including the session that merged it. Today the binary prints only usage and build metadata.

The workflow design remains a draft.

## Layout

`cmd/ascm/` holds the command-line entry point and its tests. `internal/buildmeta/` resolves version metadata from linker flags or runtime build info.

`docs/` holds the OKF docs bundle. `scripts/okf-check.py` checks its format and the Markdown frontmatter across the repository.

`.devtools/` and `.config/mise/conf.d/` hold the devtools v0.12.0 payload. The `vendir` tool owns both directories. Edit repository settings in the root configuration files.

`.agents/skills/agent-*` holds the local agent skills. APM deploys skills and rules to `.agents/` and `.claude/` from `apm.yml`.

## Commands

Read `mise.toml` before trusting it. In each fresh worktree, run `mise trust`, then `mise run bootstrap` to sync the payload and install the tools and hooks.

| Command | Purpose |
| --- | --- |
| `mise run devtools:apm-sync` | Deploy agent tooling |
| `mise run lint` | Check source gates |
| `mise run check` | Check docs, local skills, and Go coverage |
| `mise run build` | Build `bin/ascm` with version metadata |
| `mise run test-go` | Run the Go tests |
| `mise run test-go-race` | Run the Go tests with the race detector |
| `mise run cover-go` | Measure coverage and enforce its floors |
| `mise run cover-go-check` | Check the existing coverage profile |
| `mise run lint-go-all` | Run the Go analysis gates |
| `mise run lint-go-fmt` | Check Go formatting |
| `mise run vuln` | Scan reachable Go vulnerabilities |
| `mise run devtools:gomodscan` | Scan module dependencies |
| `mise run mutate-go` | Run mutation testing |
| `mise run test-skills` | Run the local skills' bats suites |
| `mise run lint-skills` | Check skill prose and scripts |
| `mise run check-docs` | Check the docs bundle and repository Markdown |
| `mise run devtools:prek-all` | Run applicable hooks |

## Go toolchain

The shared payload pins Go 1.27.2. Use `mise exec -- go version` to inspect it. Module `github.com/tbhb-dev/ascm` declares `go 1.27` and `toolchain go1.27.2`. Its packages use only the standard library.

Root configuration includes the shared Go task file and exposes local build and test tasks. Go tool directives declare the analyzer dependencies in the module. `DEVTOOLS_GO_MODULES_MODE=vendor` loads them from the tracked `vendor/` tree. Commit `go.sum` with those generated sources. Run `mise run vendor` to refresh them and `mise run vendor-check` to check for drift.

## Conventions

Start Markdown files with OKF frontmatter. The docs index declares `okf_version: "0.2"`. Draft docs record `status: draft` and their generation metadata.

Keep each prose paragraph on one line. Run `mise run lint-draft <file>` on a draft before copying it to its final path. Vale checks prose, cspell checks spelling, and rumdl checks Markdown structure. The guard-markdown hook checks paragraph wrapping.

Change the source configuration and regenerate outputs through vendir or APM. Keep local private values in the ignored `mise.local.toml`. Never commit drafts or build outputs.

## Identities

Sign commits as `tbhb-agent`. End each commit message with an `Assisted-by` trailer followed by a `Signed-off-by` trailer. Run `mise run lint-commit-msg` on `COMMIT_AGENTMSG` before committing through the hooks.

Use the agent GitHub account for git network operations and GitHub commands. Post PR reviews only through `gh-reviewer`, which uses `tbhb-agent-reviewer`.

## Workflows

`ci.yml` runs on pull requests, pushes to `main`, and manual dispatch. It checks lint on Linux and runs Go tests on Linux and macOS. Separate jobs enforce the coverage floors, scan reachable vulnerabilities, and check payload and Go vendor drift. The `test` summary requires every matrix slot to pass.

`security.yml` runs on pull requests and pushes to `main`. It also runs daily at 06:26 Coordinated Universal Time (UTC) and on manual dispatch. `gitleaks` scans history through the PR head on pull requests. The Go scanners upload reports for reachable vulnerabilities and dependencies in `vendor/`.

`scheduled.yml` runs daily at 04:17 UTC and on manual dispatch. It runs Go mutation testing and uploads the coverage profile.

`codeql.yml` analyzes Go on pull requests and pushes to `main`. It also runs on Sundays at 03:39 UTC and on manual dispatch.

`lint-codeowners.yml` runs on pull requests only and calls the shared CODEOWNERS check.

`lint-workflows.yml` runs when a pull request or a push to `main` changes a workflow. Manual dispatch runs it too.

`renovate-config-validator.yaml` runs when a pull request or a push to `main` changes a Renovate configuration. It also supports manual dispatch.

`renovate.yaml` runs hourly on weekdays and every two hours on weekends, at minute 11. Manual dispatch can select debug logging. It uses the Renovate App credentials to update dependencies.
