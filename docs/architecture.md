---
type: Architecture Overview
title: Architecture overview
description: Draft summary of the planned ledger and workflow layers.
tags: [ascm, architecture]
status: draft
generated: { by: "codex:gpt-6.1-sol", at: "2026-10-18" }
---

# Architecture overview

Plans for ascm call for two layers. In this design, `ledger` would import nothing else in the module, and `workflow` would build on `ledger` to run git and GitHub workflows.

The binary has neither layer yet. `cmd/ascm` prints only usage and build metadata, and there is no `ledger` or `workflow` package. Its dependency, `internal/buildmeta`, resolves linker metadata with a runtime build-info fallback.

The full architecture design is forthcoming.
