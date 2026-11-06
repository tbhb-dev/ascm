# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Interim secret scan of agent-written text. Replaced by ascm at M4.

The PR script scans a pull request's final title and body, and the merge
script its squash message, before gh posts them. The scan pipes the text
through ``gitleaks stdin --redact --no-banner`` under the host's private
config, ~/.config/gitleaks/private.toml, which extends the repository's
.gitleaks.toml. That is the combination tbhb_rules' ``gitleaks_test`` uses for
its private config: the private file begins with ``[extend] path =
".gitleaks.toml"``, gitleaks runs from the repository root so the path
resolves, and the private file must define the ``tbhb-private-canary`` rule.

There is no public-only fallback. A missing or unreadable private config, one
without the canary rule, or a missing .gitleaks.toml stops the script, and so
does any finding or gitleaks error. gitleaks redacts what it prints.
"""

import re
import subprocess  # nosec B404
from pathlib import Path

PRIVATE = Path(".config") / "gitleaks" / "private.toml"
COMMITTED = ".gitleaks.toml"
CANARY_ID = "tbhb-private-canary"
CANARY = re.compile(
    rf"""^[ \t]*id[ \t]*=[ \t]*["']{CANARY_ID}["'][ \t]*(?:#.*)?$""", re.MULTILINE
)
# The host step that writes the private config. Agents run upgrade, which
# refreshes it on every run; bootstrap is the operator's first run.
FETCH = "Run `tbhbdev host upgrade` (step gitleaks-config), which writes it."


def private_config() -> Path:
    """The host's private gitleaks config."""
    return Path.home() / PRIVATE


def scan(text: str, what: str, *, root: Path, gitleaks: str) -> None:
    """Refuse text that gitleaks flags under the private config.

    Args:
        text: The text to scan.
        what: What the text is, for the refusal message.
        root: The repository root, which holds .gitleaks.toml.
        gitleaks: The gitleaks executable.

    Raises:
        SystemExit: The private config or .gitleaks.toml is missing or wrong,
            or gitleaks reports a finding or fails.
    """
    config = private_config()
    try:
        private = config.read_text()
    except OSError as exc:
        message = (
            f"cannot scan {what} for secrets: the private gitleaks config "
            f"{config} cannot be read ({exc.strerror}). {FETCH}"
        )
        raise SystemExit(message) from None
    if not CANARY.search(private):
        message = (
            f"cannot scan {what} for secrets: {config} has no rule with the id "
            f"{CANARY_ID}, so it is not the private gitleaks config. {FETCH}"
        )
        raise SystemExit(message)
    if not (root / COMMITTED).is_file():
        message = f"cannot scan {what} for secrets: {root / COMMITTED} does not exist"
        raise SystemExit(message)
    argv = [gitleaks, "stdin", "--redact", "--no-banner", "--config", str(config)]
    # Argv is the found gitleaks and fixed flags; text goes on stdin.
    done = subprocess.run(  # nosec B603
        argv,
        capture_output=True,
        text=True,
        check=False,  # pragma: no mutate -- the default, spelled for the linter
        cwd=root,
        input=text,
    )
    if done.returncode != 0:
        output = " ".join((done.stdout + done.stderr).split())
        message = f"refusing {what}: gitleaks flags it or cannot scan it: {output}"
        raise SystemExit(message)
