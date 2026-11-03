---
base: main
draft: true
labels:
  - enhancement
reviewers:
  - tbhb
assignees:
  - "@me"
---

# feat(cli): add the stamp subcommand

## Why

Running `ascm stamp` on a tree with a read-only file stops partway. The command leaves some files stamped and others not, and prints nothing that says which. See [the issue](https://github.com/tbhb-dev/ascm/issues/12).

## Behavior change

The command now checks every target before it writes one. A read-only file fails the whole run with an exit code of 1 and a list of the blocked paths. A run that passes the check stamps every file.

## Verification

Ran the new unit tests, then stamped a tree with one read-only file:

```console
$ go test ./internal/stamp/...
ok  	github.com/tbhb-dev/ascm/internal/stamp	0.412s
$ ascm stamp ./fixture
error: 1 file is read-only: fixture/a.txt
```

No file in the tree changed after the failure.
