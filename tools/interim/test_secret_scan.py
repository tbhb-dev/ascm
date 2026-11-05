# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the interim secret scan, against the pinned gitleaks."""

import shutil
from pathlib import Path

import pytest

from tools.interim import secret_scan
from tools.interim.fake_tool import PRIVATE_CONFIG, write_fake, write_private_config

# A committed config with one rule of its own and none of gitleaks' defaults,
# so each finding below comes from exactly one of the two configs.
COMMITTED = """[[rules]]
id = "committed-rule"
description = "Test committed rule"
regex = '''committed-secret-[0-9]{8}'''
"""
CANARY_HIT = "tbhb-canary-0123456789abcdef"
COMMITTED_HIT = "committed-secret-12345678"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository root with the committed config, and HOME with the private one."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_private_config(home)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitleaks.toml").write_text(COMMITTED)
    return repo


def gitleaks() -> str:
    found = shutil.which("gitleaks")
    assert found, "the pinned gitleaks must be on PATH"
    return found


def scan(text: str, root: Path, tool: str | None = None) -> None:
    secret_scan.scan(text, "the text", root=root, gitleaks=tool or gitleaks())


def test_clean_text_passes(root: Path) -> None:
    scan("fix: nothing secret here\n\nJust prose.\n", root)


@pytest.mark.parametrize("hit", [CANARY_HIT, COMMITTED_HIT])
def test_a_finding_from_either_config_is_refused_and_redacted(
    root: Path, hit: str
) -> None:
    with pytest.raises(SystemExit) as info:
        scan(f"## Why\n\nThe value is {hit}.\n", root)
    message = str(info.value)
    assert message.startswith("refusing the text: gitleaks flags it or cannot scan it")
    assert hit not in message


def test_the_private_config_path_is_under_home(root: Path) -> None:
    del root
    assert secret_scan.private_config() == (
        Path.home() / ".config" / "gitleaks" / "private.toml"
    )


def test_a_missing_private_config_names_the_path_and_the_fix(root: Path) -> None:
    secret_scan.private_config().unlink()
    with pytest.raises(SystemExit) as info:
        scan("clean\n", root)
    assert str(info.value) == (
        "cannot scan the text for secrets: the private gitleaks config "
        f"{secret_scan.private_config()} cannot be read (No such file or "
        "directory). Run `tbhbdev host upgrade` (step gitleaks-config), "
        "which writes it."
    )


def test_a_private_config_without_the_canary_is_refused(root: Path) -> None:
    write_private_config(Path.home(), PRIVATE_CONFIG.replace("tbhb-private", "x"))
    with pytest.raises(SystemExit, match="has no rule with the id tbhb-private-canary"):
        scan("clean\n", root)


def test_a_missing_committed_config_is_refused(root: Path) -> None:
    (root / ".gitleaks.toml").unlink()
    with pytest.raises(SystemExit, match=r"\.gitleaks\.toml does not exist"):
        scan("clean\n", root)


def test_gitleaks_failing_is_refused_with_its_output(
    root: Path, tmp_path: Path
) -> None:
    fake = tmp_path / "gitleaks"
    write_fake(fake, "import sys\nsys.stderr.write('bad  config\\n')\nsys.exit(2)\n")
    with pytest.raises(SystemExit) as info:
        scan("clean\n", root, str(fake))
    assert str(info.value).endswith("cannot scan it: bad config")


def test_gitleaks_runs_from_the_root_with_the_private_config(
    root: Path, tmp_path: Path
) -> None:
    fake = tmp_path / "gitleaks"
    log = tmp_path / "log"
    body = (
        "import os, sys\n"
        f"open({str(log)!r}, 'w').write("
        "repr((sys.argv[1:], os.getcwd(), sys.stdin.read())))\n"
    )
    write_fake(fake, body)
    scan("text\n", root, str(fake))
    argv = ["stdin", "--redact", "--no-banner", "--config"]
    argv.append(str(secret_scan.private_config()))
    assert log.read_text() == repr((argv, str(root), "text\n"))
