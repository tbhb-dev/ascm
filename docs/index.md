---
okf_version: "0.2"
---

# ascm

ascm (agent source control management) is one Go binary that runs coding agents' git and GitHub workflows (commit, PR, merge, review, issues, and comments). Each workflow starts as a draft, then moves through launched reviews and gates. It records content-bound receipts in git refs and writes verifiable attribution trailers. Every commit traces back to the agent sessions that wrote and reviewed it, including the session that merged it.

# Architecture overview

* [Architecture overview](architecture.md) - Draft summary of the ledger and workflow layers.

# Command reference

* [Command reference](cli.md) - Draft scope of the command-line tool.
