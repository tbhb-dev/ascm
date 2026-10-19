"""Check that a directory is a conformant OKF v0.2 bundle (SPEC.md section 11).

Usage: uv run --no-project --with pyyaml python scripts/okf-check.py docs
       uv run --no-project --with pyyaml python scripts/okf-check.py --repo .

Checks: every non-reserved .md file has a parseable YAML frontmatter block with a non-empty
`type`; `index.md` has no frontmatter except `okf_version` at the bundle root, and its
entries have the `* [Title](link) - description` form; concept id segments are valid; a
`status` is draft, stable or deprecated; `generated` has `by`; relative links resolve.

With `--repo ROOT`, every Markdown file under ROOT outside `docs/` and the build, dependency, log and
hidden directories needs the same frontmatter (`type`, valid `status` and `generated`) unless git
ignores the file, and none of them needs a place in a docs index.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps timestamps as the text the author wrote."""


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
INDEX_ENTRY = re.compile(r"^\* \[[^\]]+\]\([^)]+\)( - .+)?$")
SKIP_DIRS = {"docs", "target", "node_modules", "apm_modules", "tmp", "logs"}
LINK = re.compile(r"\]\((\./[^)#]+|[A-Za-z0-9_-]+\.md)\)")


def parse(text: str) -> tuple[dict | None, str]:
    """Return (frontmatter, body); frontmatter is None when the file has none."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = yaml.load("\n".join(lines[1:i]), Loader=_Loader) or {}
            if not isinstance(fm, dict):
                raise ValueError("frontmatter must be a YAML mapping")
            return fm, "\n".join(lines[i + 1 :])
    raise ValueError("unterminated YAML frontmatter block")


def check_frontmatter(rel: Path, fm: dict) -> list[str]:
    errors = []
    if not fm.get("type"):
        errors.append(f"{rel}: missing required frontmatter key: type")
    if fm.get("status") not in (None, "draft", "stable", "deprecated"):
        errors.append(f"{rel}: status must be draft, stable or deprecated")
    generated = fm.get("generated")
    if generated is not None and not (isinstance(generated, dict) and generated.get("by")):
        errors.append(f"{rel}: generated.by is required within generated")
    return errors


def repo_markdown(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        found.extend(Path(dirpath) / f for f in sorted(filenames) if f.endswith(".md"))
    ignored = ignored_by_git(root, found)
    return [md for md in found if md not in ignored]


def ignored_by_git(root: Path, paths: list[Path]) -> set[Path]:
    """The paths that .gitignore covers, so an ignored draft such as PR_AGENTDESC.md is never checked."""
    if not paths:
        return set()
    done = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
        input="\0".join(str(p.relative_to(root)) for p in paths),
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 1 means none is ignored. Any other failure, such as a directory outside a repository, ignores nothing.
    if done.returncode not in (0, 1):
        return set()
    return {root / name for name in done.stdout.split("\0") if name}


def check_repo(root: Path) -> list[str]:
    errors = []
    for md in repo_markdown(root):
        rel = md.relative_to(root)
        try:
            fm, _ = parse(md.read_text(encoding="utf-8"))
        except (ValueError, yaml.YAMLError) as e:
            errors.append(f"{rel}: {e}")
            continue
        if fm is None:
            errors.append(f"{rel}: no frontmatter")
            continue
        errors.extend(check_frontmatter(rel, fm))
    return errors


def check(root: Path) -> list[str]:
    errors = []
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        for seg in rel.with_suffix("").parts:
            if not SEGMENT.fullmatch(seg):
                errors.append(f"{rel}: invalid concept id segment {seg!r}")
        try:
            fm, body = parse(md.read_text(encoding="utf-8"))
        except (ValueError, yaml.YAMLError) as e:
            errors.append(f"{rel}: {e}")
            continue
        if md.name == "log.md":
            continue
        if md.name == "index.md":
            allowed = {"okf_version"} if md.parent == root else set()
            extra = set(fm or {}) - allowed
            if extra:
                errors.append(f"{rel}: index.md frontmatter not allowed: {sorted(extra)}")
            for line in body.splitlines():
                if line.startswith("* [") and not INDEX_ENTRY.match(line):
                    errors.append(f"{rel}: malformed index entry: {line}")
            continue
        if fm is None:
            errors.append(f"{rel}: no frontmatter")
            continue
        errors.extend(check_frontmatter(rel, fm))
        for target in LINK.findall(body):
            if not (md.parent / target).exists():
                errors.append(f"{rel}: broken link {target}")
    return errors


def main() -> int:
    if sys.argv[1:2] == ["--repo"]:
        root = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
        errors = check_repo(root)
        for e in errors:
            print(e)
        if not errors:
            print(f"OK: {len(repo_markdown(root))} files outside docs/ have OKF frontmatter")
        return 1 if errors else 0
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "docs")
    errors = check(root)
    for e in errors:
        print(e)
    if not errors:
        print(f"OK: {len(list(root.rglob('*.md')))} files conform to OKF v0.2")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
