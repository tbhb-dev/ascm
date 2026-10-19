---
type: README
title: ascm
description: Agent source control management.
tags: [ascm, readme]
generated: { by: "codex:gpt-6.1-sol", at: "2026-10-18" }
---

# ascm

Plans for ascm (agent source control management) call for one Go binary to run coding agents' git and GitHub workflows (commit, PR, merge, review, issues, and comments). Each planned workflow starts as a draft, then moves through launched reviews and gates. The design calls for content-bound receipts in git refs and verifiable attribution trailers that trace every commit back to the agent sessions that wrote and reviewed it, including the session that merged it. Today the binary prints only usage and build metadata.

The command-line tool uses only the standard library. Go tool directives pin the analyzers, and `vendor/` holds their source dependencies. Commit `go.sum` with those generated sources. Run `mise run vendor` to refresh them and `mise run vendor-check` to check for drift.
