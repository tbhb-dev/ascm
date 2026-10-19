---
okf_version: "0.2"
---

# ascm

Plans for ascm (agent source control management) call for one Go binary to run coding agents' git and GitHub workflows (commit, PR, merge, review, issues, and comments). Each planned workflow starts as a draft, then moves through launched reviews and gates. The design calls for content-bound receipts in git refs and verifiable attribution trailers that trace every commit back to the agent sessions that wrote and reviewed it, including the session that merged it. Today the binary prints only usage and build metadata.

# Architecture overview

* [Architecture overview](architecture.md) - Draft summary of the planned ledger and workflow layers.

# Command reference

* [Command reference](cli.md) - Draft scope of the command-line tool.
