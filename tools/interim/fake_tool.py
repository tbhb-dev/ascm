# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Fake command-line tools for the interim tooling's tests."""

import shlex
import shutil
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def write_fake(script: Path, body: str) -> None:
    """A fake tool: a /bin/sh stub that runs ``body`` (saved beside it) in Python.

    Not a ``#!<python>`` shebang: under Bazel on Linux the interpreter path can
    pass the kernel's shebang length limit, or be a script itself, and exec
    then fails with ENOEXEC.
    """
    source = script.with_name(script.name + ".py")
    source.write_text(body)
    python, path = shlex.quote(sys.executable), shlex.quote(str(source))
    script.write_text(f'#!/bin/sh\nexec {python} {path} "$@"\n')
    script.chmod(0o755)


# tbhb_rules' pinned git is a /bin/sh wrapper that finds its archive with
# readlink and dirname and checks the kernel with uname, so a PATH that
# holds git must hold these too.
GIT_TOOLS = ("git", "readlink", "dirname", "uname")


def link_git(directory: Path) -> None:
    """A new directory for PATH that holds git and the tools it runs."""
    directory.mkdir()
    for name in GIT_TOOLS:
        (directory / name).symlink_to(shutil.which(name) or name)
