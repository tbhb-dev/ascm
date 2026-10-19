---
type: Command Reference
title: Command reference
description: Draft reference for usage and version output.
tags: [ascm, commands]
status: draft
generated: { by: "codex:gpt-6.1-sol", at: "2026-10-18" }
---

# Command reference

The command-line tool supports these flags:

| Flag | Output | Exit code |
| --- | --- | --- |
| `--version` | Version, commit, and date on standard output | 0 |
| `-h`, `--help` | Usage on standard output | 0 |

No arguments, an unknown flag, any positional argument, a bare `--`, or only flags set to false (`--version=false`, `-h=false`) prints an error and usage on standard error and returns exit code 2.
