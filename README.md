---
type: README
title: ascm
description: Agent source control management.
tags: [ascm, readme]
generated: { by: "codex:gpt-6.1-sol", at: "2026-10-18" }
---

# ascm

ascm (agent source control management) is one Go binary that runs coding agents' git and GitHub workflows (commit, PR, merge, review, issues, and comments). Each workflow starts as a draft, then moves through launched reviews and gates. It records content-bound receipts in git refs and writes verifiable attribution trailers. Every commit traces back to the agent sessions that wrote and reviewed it, including the session that merged it.

The command-line tool uses only the standard library. Go tool directives pin the analyzers, and `vendor/` holds their source dependencies. Commit `go.sum` with those generated sources. Run `mise run vendor` to refresh them and `mise run vendor-check` to check for drift.
