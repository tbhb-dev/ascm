# ascm

ascm (agent source control management) is a command-line tool, written in Go, for running coding agents' git and GitHub workflows with verifiable attribution. Each commit it makes ends with `Generated-by` and `Assisted-by` trailers naming the harness, model and session of every agent that wrote or helped with the change.

The repository also holds Bazel rules for the tool, plugins for Claude Code, Codex and Antigravity, and the docs.

The project is at the bootstrap stage. Today the binary prints only its usage and build metadata. Interim hooks under `tools/interim/` stamp the attribution trailers until ascm can do it itself.

## Development

Bazel pins and runs every gate. [mise](https://mise.jdx.dev) only installs `bazelisk` and `uv`, pinned in `mise.toml` and locked in `mise.lock`. Run every gate with:

```sh
bazelisk test //...
```

Run one gate by its target, such as `bazelisk test //:vale_test`. `bazelisk query 'tests(//...)'` lists them. `bazelisk run //:format` formats the tree in place. The git hooks call the pinned tools through `.bazel_env/bin`, which `bazelisk run //:bazel_env` builds.

The Bazel rules come from [tbhb-dev/rules](https://github.com/tbhb-dev/rules). To work against a local clone of it, add these lines to `.bazelrc.user`, which git ignores:

```text
common --override_module=tbhb_rules=../rules
common --override_module=tbhb_rules_python=../rules/python
```

The docs are in [docs/](docs/index.md). Their frontmatter rules are in [docs/reference/frontmatter.md](docs/reference/frontmatter.md).

## License

Apache-2.0. See `LICENSE`.
