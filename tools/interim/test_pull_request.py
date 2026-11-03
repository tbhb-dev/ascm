# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the interim pull-request tooling."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from tools.interim import agent_identity
from tools.interim import pull_request as pr
from tools.interim.fake_tool import write_fake

# mutmut runs the tests from a copy of the source tree, so everything resolves
# against this file: the real repository root, or the mutants/ copy of it.
REAL = Path(pr.__file__).resolve().parents[2]
DATA = Path(__file__).parent / "testdata" / "pull_request"
FIXTURE = DATA / "passing-draft.md"
TEMPLATE = REAL / ".github" / "PULL_REQUEST_TEMPLATE.md"
SECTIONS = pr.load_sections()

H = "sha-256-64:"
SESSION = H + "a" * 16
SUBAGENT = H + "b" * 16
FALLBACK = (
    "LLM effort=medium;harness=claude-code:2.1.316;"
    "session=sha-256-64:71c761039ac2c7f9;surface=cli"
)
GEN_LINE = "Generated-by: LLM harness=claude-code;model=anthropic:m1"
GEN = "LLM harness=claude-code;model=anthropic:m1"
OTHER = "LLM harness=codex;model=openai:gpt-6.1-sol"

BASE = "# fix: x\n\n## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"
# Line numbers in BASE: title 1, Why 3, Behavior change 7, Verification 11.

FAKE = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
name = os.path.basename(__file__).removesuffix(".py")
spec = json.load(open(os.path.join(here, name + ".spec")))
draft = ".github/PULL_REQUEST_DRAFT.md"
record = {{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
    "stdin_tty": sys.stdin.isatty(),
    "draft": open(draft).read() if os.path.exists(draft) else None,
    "links": {{n: os.readlink(n) for n in os.listdir(".") if os.path.islink(n)}},
}}
with open(os.path.join(here, name + ".log"), "a") as log:
    log.write(json.dumps(record) + "\\n")
sys.stdout.write(spec["stdout"])
sys.stderr.write(spec["stderr"])
sys.exit(spec["rc"])
"""


@dataclass
class World:
    """A temp repository, a bin directory of fake tools, and an isolated PATH."""

    root: Path
    bin: Path

    def fake(
        self, name: str, *, rc: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        write_fake(self.bin / name, FAKE.format())
        spec = {"rc": rc, "stdout": stdout, "stderr": stderr}
        (self.bin / f"{name}.spec").write_text(json.dumps(spec))

    def calls(self, name: str) -> list[dict[str, object]]:
        log = self.bin / f"{name}.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def commit(self, message: str) -> None:
        self.git("commit", "--allow-empty", "-q", "-m", message)


def link_git(directory: Path) -> None:
    directory.mkdir()
    (directory / "git").symlink_to(shutil.which("git") or "git")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """A repository at ROOT with the real schema and sections and stub configs."""
    root = tmp_path / "repo"
    tools_dir = tmp_path / "bin"
    tools_dir.mkdir()
    link_git(tmp_path / "git_dir")
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    prefixes = ("CLAUDE", "CODEX", "ANTIGRAVITY", "GIT_", "ASCM_")
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(prefixes)]))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PATH", f"{tools_dir}{os.pathsep}{tmp_path / 'git_dir'}")
    (root / ".github").mkdir(parents=True)
    shutil.copytree(REAL / ".github" / "schemas", root / ".github" / "schemas")
    shutil.copy(REAL / pr.SECTIONS, root / pr.SECTIONS)
    for name in pr.CONFIGS:
        (root / name).write_text("")
    monkeypatch.setattr(pr, "ROOT", root)
    # The script runs inside the repository whose branch it posts.
    monkeypatch.chdir(root)
    made = World(root, tools_dir)
    made.git("init", "-q", "-b", "main")
    made.git("config", "user.email", "t@example.com")
    made.git("config", "user.name", "T")
    made.git("config", "commit.gpgsign", "false")
    made.commit("init")
    made.fake("cog")
    made.fake("vale", stdout="{}")
    made.fake("cspell")
    made.fake("rumdl")
    made.fake("gh", stdout="https://github.com/tbhb-dev/ascm/pull/1\n")
    return made


def no_lint(_rendered: str, _first: int) -> list[pr.Problem]:
    return []


def no_title_problems(_title: str) -> list[str]:
    return []


def structure(text: str) -> list[pr.Problem]:
    """Check a draft without the external tools."""
    with (
        patch.object(pr, "lint", no_lint),
        patch.object(pr, "title_problems", no_title_problems),
    ):
        return pr.check_text(text).problems


# ----------------------------------------------------------- source of truth


def test_sections_file_matches_template_guidance() -> None:
    template = TEMPLATE.read_text()
    headings = re.findall(r"^## (.+)$", template, re.MULTILINE)
    assert headings == [section.heading for section in SECTIONS]
    guidance = re.findall(
        r"^## .+\n\n<!-- (Required|Optional), at most (\d+) words",
        template,
        re.MULTILINE,
    )
    assert [(g[0] == "Required", int(g[1])) for g in guidance] == [
        (section.required, section.max_words) for section in SECTIONS
    ]


def test_template_has_no_provenance_section() -> None:
    assert "Provenance" not in TEMPLATE.read_text().split("## Why")[1]


def test_limits() -> None:
    assert {s.heading: s.max_words for s in SECTIONS} == {
        "Why": 60,
        "Behavior change": 120,
        "Decisions": 120,
        "Deviations from the task": 80,
        "Needs human judgment": 100,
        "Risk": 80,
        "Verification": 150,
        "Human steering": 40,
    }
    assert [s.heading for s in SECTIONS if s.required] == [
        "Why",
        "Behavior change",
        "Verification",
    ]


def test_schema_is_valid_and_closed() -> None:
    schema = pr.load_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert pr.schema_default(schema, "base") == "main"
    assert pr.schema_default(schema, "draft") is False
    properties = cast("dict[str, dict[str, str]]", schema["properties"])
    assert all(prop.get("description") for prop in properties.values())


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"base": "release/1.x", "draft": True},
        {"labels": ["bug", "good first issue"], "reviewers": ["tbhb", "org/team"]},
        {"assignees": ["@me", "tbhb"], "milestone": "v1 beta"},
    ],
)
def test_schema_accepts(meta: dict[str, object]) -> None:
    assert Draft202012Validator(pr.load_schema()).is_valid(meta)


@pytest.mark.parametrize(
    "meta",
    [
        {"title": "x"},
        {"base": ""},
        {"base": "-x"},
        {"draft": "yes"},
        {"labels": ["a,b"]},
        {"labels": [" a"]},
        {"labels": ["a", "a"]},
        {"labels": ["x" * 51]},
        {"reviewers": ["bad name"]},
        {"reviewers": ["-x"]},
        {"assignees": ["@you"]},
        {"milestone": " v1"},
        {"milestone": ""},
    ],
)
def test_schema_rejects(meta: dict[str, object]) -> None:
    assert not Draft202012Validator(pr.load_schema()).is_valid(meta)


# -------------------------------------------------------------- word counting


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("", 0),
        ("one two  three\nfour", 4),
        ("- a\n- b", 2),
        ("-- ... *", 0),
        ("`code words` plain", 1),
        ("``a ` b`` plain", 1),
        ("[link text](https://example.com/a b) after", 3),
        ("![alt text](img.png)", 2),
        ("[text][ref] more\n\n[ref]: https://example.com", 2),
        ("<https://example.com> see https://example.com/x now", 2),
        ("a <!-- hidden words --> b", 2),
        ("a <!-- never closed\nhidden", 1),
        ("co-op don't 3rd", 3),
    ],
)
def test_count_words(text: str, count: int) -> None:
    assert pr.count_words(text) == count


WORD = st.text(alphabet="abcdefghij", min_size=1, max_size=6)


@given(st.lists(WORD, max_size=30))
def test_count_words_counts_words(words: list[str]) -> None:
    assert pr.count_words(" ".join(words)) == len(words)


@given(st.lists(WORD, max_size=10), WORD, WORD)
def test_free_text_adds_nothing(words: list[str], inner: str, url: str) -> None:
    text = " ".join(words)
    free = [
        f"`{inner} {inner}`",
        f"<!-- {inner} -->",
        f"<https://example.com/{url}>",
        f"https://example.com/{url}",
    ]
    assert pr.count_words(text + " " + " ".join(free)) == len(words)
    assert pr.count_words(f"[{inner}](https://example.com/{url})") == 1


# ----------------------------------------------------------- lines and fences


def test_fence_flags() -> None:
    raw = ["a", "```py", "x", "```", "b", "~~~", "y", "```", "~~~~", "c"]
    flags, unclosed = pr.fence_flags(raw)
    assert flags == [False, True, True, True, False, True, True, True, True, False]
    assert unclosed is None


@pytest.mark.parametrize(
    ("raw", "unclosed"),
    [
        (["a", "```", "x"], 1),
        (["```", "x", "``` info", "y"], 0),
        (["````", "x", "```", "y"], 0),
        (["   ```", "x", "   ```"], None),
        (["    ```", "x"], None),
        (["~~~", "```", "~~~"], None),
    ],
)
def test_fence_unclosed(raw: list[str], unclosed: int | None) -> None:
    assert pr.fence_flags(raw)[1] == unclosed


def test_strip_comments_keeps_line_numbers() -> None:
    texts = ["a <!-- x", "y --> b", "c", "<!-- z -->"]
    clean, starts = pr.strip_comments(texts)
    assert clean == ["a ", " b", "c", ""]
    assert starts == [0, 3]
    assert pr.strip_comments([]) == ([], [])
    assert pr.strip_comments(["a <!-- open", "b"])[0] == ["a ", ""]


def test_scan_reports_comments_and_fences() -> None:
    lines, problems = pr.scan(["a", "<!-- c -->", "```", "<!-- no -->", "x"], 10)
    assert problems == [
        (11, "HTML comment left in the description"),
        (12, "code fence is never closed"),
    ]
    assert [(line.no, line.text, line.code) for line in lines] == [
        (10, "a", False),
        (11, "", False),
        (12, "", True),
        (13, "", True),
        (14, "", True),
    ]
    assert lines[1].raw == "<!-- c -->"


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("# T", (1, "T")),
        ("## Two words ", (2, "Two words")),
        ("###### six", (6, "six")),
        ("   ## indented", (2, "indented")),
        ("    ## code", None),
        ("#nospace", None),
        ("####### seven", None),
        ("plain", None),
        ("#", None),
    ],
)
def test_heading_of(text: str, found: tuple[int, str] | None) -> None:
    assert pr.heading_of(pr.Line(1, text, text, code=False)) == found


def test_heading_of_ignores_code() -> None:
    assert pr.heading_of(pr.Line(1, "## x", "", code=True)) is None
    assert pr.heading_of(pr.Line(1, "## x", "## x", code=True)) is None


@given(st.lists(st.sampled_from(["a", "", "x y", "<!-- c -->"]), max_size=12))
def test_scan_keeps_one_line_per_input(raw: list[str]) -> None:
    lines, _ = pr.scan(raw, 5)
    assert [line.no for line in lines] == list(range(5, 5 + len(raw)))
    assert [line.raw for line in lines] == raw


def sections_text(names: list[str], bodies: list[str]) -> str:
    return "".join(f"## {n}\n\n{b}\n\n" for n, b in zip(names, bodies, strict=True))


@given(
    st.lists(
        st.tuples(st.integers(0, len(SECTIONS) - 1), st.integers(1, 40)),
        unique_by=lambda pair: pair[0],
        max_size=len(SECTIONS),
    )
)
def test_block_problems_follow_the_scheme(chosen: list[tuple[int, int]]) -> None:
    chosen = sorted(chosen)
    names = [SECTIONS[index].heading for index, _ in chosen]
    bodies = [" ".join(["word"] * count) for _, count in chosen]
    lines, _ = pr.scan(sections_text(names, bodies).splitlines(), 1)
    _, blocks, _ = pr.split_blocks(lines)
    assert [block.heading for block in blocks] == names
    found = pr.block_problems(blocks, SECTIONS)
    over = [
        n
        for (index, count), n in zip(chosen, names, strict=True)
        if count > (SECTIONS[index].max_words or 0)
    ]
    assert [text.split("'")[1] for _, text in found] == over


@given(st.permutations(range(len(SECTIONS))))
def test_block_problems_flag_any_other_order(order: list[int]) -> None:
    names = [SECTIONS[index].heading for index in order]
    lines, _ = pr.scan(sections_text(names, ["x"] * len(names)).splitlines(), 1)
    _, blocks, _ = pr.split_blocks(lines)
    found = pr.block_problems(blocks, SECTIONS)
    assert (len(found) > 0) == (order != sorted(order))


# ---------------------------------------------------------- structure checks


def test_minimal_draft_passes() -> None:
    assert structure(BASE) == []


def test_fixture_structure_passes() -> None:
    assert structure(FIXTURE.read_text()) == []


def replace(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


WHY_61 = " ".join(["word"] * 61)
WHY_60 = " ".join(["word"] * 60)
CASES: list[tuple[str, str, list[pr.Problem]]] = [
    ("empty", "", [(1, "the first line after the frontmatter must be the H1 title")]),
    (
        "section first",
        "## Why\n\nw\n",
        [(1, "the first line after the frontmatter must be the H1 title")],
    ),
    (
        "code first",
        "```\nx\n```\n" + BASE,
        [(1, "the first line after the frontmatter must be the H1 title")],
    ),
    (
        "missing why",
        replace(BASE, "## Why\n\nw\n\n", ""),
        [(1, "missing required section 'Why'")],
    ),
    (
        "missing verification",
        replace(BASE, "\n## Verification\n\nv\n", ""),
        [(1, "missing required section 'Verification'")],
    ),
    (
        "unknown",
        BASE + "\n## Provenance\n\nx\n",
        [
            (
                15,
                "unknown section 'Provenance'; use: "
                + ", ".join(s.heading for s in SECTIONS),
            )
        ],
    ),
    (
        "out of order",
        BASE + "\n## Risk\n\nx\n",
        [(15, "section 'Risk' is duplicated or out of order")],
    ),
    (
        "duplicate",
        BASE + "\n## Verification\n\nagain\n",
        [(15, "section 'Verification' is duplicated or out of order")],
    ),
    (
        "empty section",
        BASE + "\n## Human steering\n",
        [(15, "section 'Human steering' is empty")],
    ),
    (
        "comment only section",
        BASE + "\n## Human steering\n\n<!-- later -->\n",
        [
            (17, "HTML comment left in the description"),
            (15, "section 'Human steering' is empty"),
        ],
    ),
    (
        "comment",
        replace(BASE, "\nw\n", "\nw <!-- hi -->\n"),
        [(5, "HTML comment left in the description")],
    ),
    (
        "second h1",
        BASE + "\n# Again\n",
        [(15, "only one H1 is allowed: the title")],
    ),
    (
        "stray text",
        replace(BASE, "# fix: x\n\n", "# fix: x\n\nstray words\n\n"),
        [(3, "text before the first section; put it under a heading")],
    ),
    (
        "unclosed fence",
        BASE + "\n```\ncode\n",
        [(15, "code fence is never closed")],
    ),
    (
        "limit",
        replace(BASE, "\nw\n", f"\n{WHY_61}\n"),
        [(3, "section 'Why' has 61 words (limit 60)")],
    ),
    (
        "trailer",
        BASE + "\nGenerated-by: LLM harness=claude-code\n",
        [(15, "the script writes Generated-by and Assisted-by; remove this line")],
    ),
    (
        "assisted trailer",
        BASE + "Assisted-by: LLM harness=codex\n",
        [(14, "the script writes Generated-by and Assisted-by; remove this line")],
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"), [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES]
)
def test_structure_problems(text: str, expected: list[pr.Problem]) -> None:
    assert sorted(structure(text)) == sorted(expected)


def test_limit_boundary() -> None:
    assert structure(replace(BASE, "\nw\n", f"\n{WHY_60}\n")) == []


def test_code_and_links_are_free() -> None:
    text = replace(
        BASE,
        "\nw\n",
        f"\n{WHY_60}\n\n```\n{WHY_61}\n```\n\n`{WHY_61}` https://example.com/a\n",
    )
    assert structure(text) == []


def test_code_only_section_is_not_empty() -> None:
    assert structure(replace(BASE, "\nv\n", "\n```\n$ go test\n```\n")) == []


def test_fenced_lines_are_not_headings_comments_or_trailers() -> None:
    fenced = "\n```\n## Not a section\n<!-- not a comment -->\nAssisted-by: x\n```\n"
    assert structure(BASE + fenced) == []


def test_h3_and_indented_headings() -> None:
    text = replace(BASE, "\nw\n", "\n### Detail\n\nw\n")
    assert structure(text) == []
    assert structure(replace(BASE, "## Why", "   ## Why")) == []
    assert structure(replace(BASE, "## Why", "    ## Why")) == [
        (1, "missing required section 'Why'"),
        (3, "text before the first section; put it under a heading"),
    ]


def test_title_may_follow_blank_lines() -> None:
    assert structure("\n\n" + BASE) == []
    assert structure("---\nbase: main\n---\n\n" + BASE) == []


@pytest.mark.parametrize("key", ["Signed-off-by", "Co-authored-by", "co-authored-by"])
@pytest.mark.parametrize("env", [{}, {"CLAUDE_CODE_SESSION_ID": "raw"}])
def test_human_trailers_are_rejected_for_everyone(
    key: str, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    text = BASE + f"\n{key}: A <a@example.com>\n"
    assert structure(text) == [(15, pr.HUMAN_TRAILER_MESSAGE)]


# ------------------------------------------------------------- frontmatter


def test_frontmatter_errors_point_at_the_key() -> None:
    text = "---\nbase: main\ndraft: maybe\nlabels: [a]\n---\n" + BASE
    found = structure(text)
    assert [line for line, _ in found] == [3]
    assert found[0][1].startswith("frontmatter $.draft: ")


def test_frontmatter_unknown_key_is_line_one() -> None:
    found = structure("---\ndraft: true\ntitle: x\n---\n" + BASE)
    assert [line for line, _ in found] == [1]
    assert "title" in found[0][1]


def test_frontmatter_errors_are_reported_per_key() -> None:
    found = structure("---\nlabels: [a, a]\ndraft: 1\n---\n" + BASE)
    assert sorted((line, text.split(":")[0]) for line, text in found) == [
        (2, "frontmatter $.labels"),
        (3, "frontmatter $.draft"),
    ]


def test_frontmatter_invalid_yaml() -> None:
    found = structure("---\nlabels: [a\n---\n" + BASE)
    assert [line for line, _ in found] == [1]
    assert found[0][1].startswith("frontmatter: invalid YAML")
    assert (
        structure("---\nbase: main\n")[0][1] == "frontmatter: unterminated frontmatter"
    )


def test_frontmatter_must_be_a_mapping() -> None:
    found = structure("---\n- a\n---\n" + BASE)
    assert [line for line, _ in found] == [1]


def test_empty_frontmatter_block_is_fine() -> None:
    assert structure("---\n---\n" + BASE) == []


def test_line_numbers_follow_the_frontmatter() -> None:
    text = "---\nbase: main\n---\n\n" + replace(BASE, "\nw\n", "\nw <!-- c -->\n")
    assert structure(text) == [(9, "HTML comment left in the description")]


# -------------------------------------------------------------- tool lookup


def test_find_tool_prefers_path(world: World) -> None:
    assert pr.find_tool("vale") == str(world.bin / "vale")


def test_find_tool_falls_back_to_mise(world: World, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "absent_tool").write_text("")
    world.fake("mise", stdout=f"{elsewhere / 'absent_tool'}\n")
    assert pr.find_tool("absent_tool") == str(elsewhere / "absent_tool")
    assert world.calls("mise")[0]["argv"] == ["which", "absent_tool"]


def test_find_tool_missing(world: World) -> None:
    with pytest.raises(
        SystemExit, match=r"absent_tool not found on PATH or through `mise which`"
    ):
        pr.find_tool("absent_tool")
    world.fake("mise", rc=1, stderr="not installed")
    with pytest.raises(SystemExit, match="absent_tool not found"):
        pr.find_tool("absent_tool")
    world.fake("mise", rc=0, stdout="\n")
    with pytest.raises(SystemExit, match="absent_tool not found"):
        pr.find_tool("absent_tool")


# ------------------------------------------------------------------- title


def test_title_goes_to_cog_on_stdin(world: World) -> None:
    assert pr.title_problems("feat: x") == []
    call = world.calls("cog")[0]
    assert call["argv"] == ["verify", "--file", "-"]
    assert call["stdin"] == "feat: x"
    assert call["cwd"] == str(world.root.resolve())


MISSING = (
    "title is not a Conventional Commit header (cog verify): Error: Missing commit type"
)


def test_title_cog_failure_is_reported(world: World) -> None:
    world.fake("cog", rc=1, stderr="Error: Missing\n\n  commit type\n")
    assert pr.title_problems("Add thing") == [MISSING]


@pytest.mark.usefixtures("world")
def test_title_length() -> None:
    assert pr.title_problems("feat: " + "x" * 66) == []
    assert pr.title_problems("feat: " + "x" * 67) == [
        "title is 73 characters; the limit is 72"
    ]


def test_title_problem_lands_on_the_title_line(world: World) -> None:
    world.fake("cog", rc=1, stderr="bad")
    found = pr.check_text("---\nbase: main\n---\n\n" + BASE).problems
    assert found == [(5, "title is not a Conventional Commit header (cog verify): bad")]


@pytest.mark.skipif(shutil.which("cog") is None, reason="cog is not installed")
@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("feat(cli)!: add x", True),
        ("fix: y", True),
        ("Add thing", False),
        ("wip: x", False),
    ],
)
def test_real_cog(title: str, *, ok: bool) -> None:
    assert (pr.title_problems(title) == []) is ok


# -------------------------------------------------------------------- lint


def lint_checked(world: World, text: str) -> list[pr.Problem]:
    del world
    return pr.check_text(text).problems


def test_lint_runs_each_tool_in_a_mirrored_tree(world: World) -> None:
    text = "---\nbase: main\n---\n\n" + BASE
    assert lint_checked(world, text) == []
    rendered = text.split("---\n\n", 1)[1]
    for name, argv in (
        ("vale", ["--output=JSON", ".github/PULL_REQUEST_DRAFT.md"]),
        (
            "cspell",
            [
                "--config",
                ".cspell.jsonc",
                "--no-summary",
                "--no-progress",
                ".github/PULL_REQUEST_DRAFT.md",
            ],
        ),
        ("rumdl", ["check", ".github/PULL_REQUEST_DRAFT.md"]),
    ):
        (call,) = world.calls(name)
        assert call["argv"] == argv
        assert call["draft"] == rendered
        assert call["stdin"] == ""
        assert call["links"] == {
            config: str(world.root / config) for config in pr.CONFIGS
        }
        assert call["cwd"] != str(world.root)


def test_lint_maps_lines_back_to_the_draft(world: World) -> None:
    world.fake(
        "vale",
        rc=1,
        stdout=json.dumps(
            {
                "f": [
                    {"Check": "Google.We", "Message": "no we", "Line": 3},
                    {
                        "Check": "project.HeadingTitleCase",
                        "Message": "Title",
                        "Line": 1,
                    },
                    {
                        "Check": "project.HeadingTitleCase",
                        "Message": "Title",
                        "Line": 7,
                    },
                ]
            }
        ),
    )
    world.fake(
        "cspell",
        rc=1,
        stdout=".github/PULL_REQUEST_DRAFT.md:5:2 - Unknown word (zzz)\n",
    )
    world.fake(
        "rumdl",
        rc=1,
        stdout=(
            ".github/PULL_REQUEST_DRAFT.md:9:1: [MD022] spacing [*]\n\n"
            "Issues: Found 1 issue\n"
        ),
    )
    found = lint_checked(world, "---\nbase: main\n---\n\n" + BASE)
    assert found == [
        (7, "vale Google.We: no we"),
        (9, "cspell: Unknown word (zzz)"),
        (11, "vale project.HeadingTitleCase: Title"),
        (13, "rumdl: [MD022] spacing [*]"),
    ]


def test_cspell_and_rumdl_lines_ignore_other_output() -> None:
    parse = pr.parse_lines(pr.CSPELL_LINE, "cspell")
    assert parse("noise\nCSpell: Files checked: 1\n") == []
    assert parse("a.md:3:4 - Unknown word (x)") == [(3, "cspell: Unknown word (x)")]


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr"),
    [
        (2, "", "Configuration Error: bad"),
        (1, "not json", ""),
        (3, "", "killed"),
    ],
)
def test_lint_tool_failure_is_a_problem(
    world: World, rc: int, stdout: str, stderr: str
) -> None:
    world.fake("vale", rc=rc, stdout=stdout, stderr=stderr)
    found = lint_checked(world, BASE)
    message = stderr or stdout
    assert found == [(1, f"vale failed (exit {rc}): {message}")]


def test_lint_failure_message_prefers_stderr(world: World) -> None:
    world.fake("cspell", rc=2, stdout="out", stderr="err  text")
    assert lint_checked(world, BASE) == [(1, "cspell failed (exit 2): err text")]


def test_lint_failure_with_findings_reports_the_failure_only(world: World) -> None:
    world.fake("rumdl", rc=2, stdout="a.md:3:1: [MD001] x\n", stderr="boom")
    assert lint_checked(world, BASE) == [(1, "rumdl failed (exit 2): boom")]


def test_vale_parse_handles_bad_json() -> None:
    assert pr.parse_vale("{") == []
    assert pr.parse_vale("{}") == []


def test_lint_needs_every_tool_and_config(world: World) -> None:
    (world.bin / "rumdl").unlink()
    with pytest.raises(SystemExit, match="rumdl not found"):
        lint_checked(world, BASE)
    world.fake("rumdl")
    (world.root / ".rumdl.toml").unlink()
    with pytest.raises(SystemExit, match=r"missing lint config \.rumdl\.toml in "):
        lint_checked(world, BASE)


def test_lint_skipped_without_a_title(world: World) -> None:
    assert lint_checked(world, "## Why\n\nw\n")
    assert world.calls("vale") == []
    assert world.calls("cog") == []


def real_lint_ready() -> bool:
    names = ("vale", "cspell", "rumdl", "cog")
    configs = all((REAL / name).exists() for name in pr.CONFIGS)
    styles = (REAL / ".vale" / "Google").is_dir()
    return configs and styles and all(shutil.which(name) for name in names)


@pytest.mark.skipif(
    not real_lint_ready(), reason="linters or their configs are missing"
)
class TestRealLinters:
    @pytest.fixture(autouse=True)
    def _real_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pr, "ROOT", REAL)

    def test_fixture_passes(self) -> None:
        # The title is a lowercase Conventional Commit header, which the
        # sentence-case rule would flag if it were not filtered.
        assert pr.check_text(FIXTURE.read_text()).problems == []

    def test_findings_map_to_draft_lines(self) -> None:
        # cspell:ignore recieve
        text = FIXTURE.read_text()
        text = replace(text, "Running `ascm stamp`", "Running ascm recieve")
        text = replace(text, "## Why\n\n", "## Why\n")
        found = pr.check_text(text).problems
        assert any(line == 15 and "recieve" in why for line, why in found)
        assert any(line == 14 and "rumdl" in why for line, why in found)


# ------------------------------------------------------------------ trailers


def value(**attrs: str) -> str:
    return agent_identity.format_value(attrs)


def test_identity_key_ignores_effort_surface_and_version() -> None:
    one = value(effort="low", harness="claude-code:1.0", model="a:m", surface="cli")
    two = value(effort="high", harness="claude-code:2.0", model="a:m")
    assert pr.identity_key(one) == pr.identity_key(two)
    assert pr.identity_key(one) == ("claude-code", "a:m", "", "")
    full = value(harness="codex", model="a:m", session=SESSION, subagent=SUBAGENT)
    assert pr.identity_key(full) == ("codex", "a:m", SESSION, SUBAGENT)
    assert pr.identity_key(value(harness="codex")) == ("codex", "", "", "")


def test_merge_dedupes_and_orders() -> None:
    a = value(harness="claude-code", model="a:m1")
    b = value(harness="codex", model="a:m2")
    found = [("Assisted-by", a), ("Generated-by", b), ("Assisted-by", a)]
    assert pr.merge_trailers(found, None) == [("Generated-by", b), ("Assisted-by", a)]


def test_generated_by_wins_whichever_comes_first() -> None:
    low = value(effort="low", harness="claude-code", model="a:m")
    high = value(effort="high", harness="claude-code", model="a:m")
    assert pr.merge_trailers([("Assisted-by", low), ("Generated-by", high)], None) == [
        ("Generated-by", high)
    ]
    assert pr.merge_trailers([("Generated-by", high), ("Assisted-by", low)], None) == [
        ("Generated-by", high)
    ]
    assert pr.merge_trailers([("Generated-by", high), ("Generated-by", low)], None) == [
        ("Generated-by", high)
    ]
    assert pr.merge_trailers([("Assisted-by", high), ("Assisted-by", low)], None) == [
        ("Assisted-by", high)
    ]


def test_posting_agent_is_added_as_assisted_once() -> None:
    a = value(harness="claude-code", model="a:m")
    me = value(harness="codex", model="o:m")
    assert pr.merge_trailers([("Generated-by", a)], me) == [
        ("Generated-by", a),
        ("Assisted-by", me),
    ]
    assert pr.merge_trailers([], me) == [("Assisted-by", me)]
    assert pr.merge_trailers([("Generated-by", me)], me) == [("Generated-by", me)]
    assert pr.merge_trailers([("Assisted-by", me)], me) == [("Assisted-by", me)]
    assert pr.merge_trailers([], None) == []


def identity(harness: str, model: str, session: str) -> str:
    extra = {"session": session} if session else {}
    return value(harness=harness, model=model, **extra)


IDENTITIES = st.builds(
    identity,
    st.sampled_from(["claude-code", "codex"]),
    st.sampled_from(["a:m1", "a:m2"]),
    st.sampled_from(["", SESSION, SUBAGENT]),
)
TRAILERS = st.lists(
    st.tuples(st.sampled_from(["Generated-by", "Assisted-by"]), IDENTITIES)
)


@given(TRAILERS, st.none() | IDENTITIES)
def test_merge_properties(found: list[tuple[str, str]], posting: str | None) -> None:
    merged = pr.merge_trailers(found, posting)
    keys = [pr.identity_key(v) for _, v in merged]
    assert len(keys) == len(set(keys))
    wanted = {pr.identity_key(v) for _, v in found}
    if posting is not None:
        wanted.add(pr.identity_key(posting))
    assert set(keys) == wanted
    roles = [k for k, _ in merged]
    assert roles == sorted(roles, key=lambda role: role != "Generated-by")
    generated = {pr.identity_key(v) for k, v in found if k == "Generated-by"}
    for (role, _), key in zip(merged, keys, strict=True):
        assert (role == "Generated-by") == (key in generated)
    assert not agent_identity.check_message("\n".join(f"{k}: {v}" for k, v in merged))


AGENT = {
    "CLAUDE_CODE_SESSION_ID": "raw-session",
    "CLAUDE_CODE_EXECPATH": "/x/2.1.0",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_EFFORT": "high",
}
POSTING = (
    "LLM effort=high;harness=claude-code:2.1.0;"
    f"session={agent_identity.sha256_64('raw-session')};surface=cli"
)


def no_ancestry(*_args: object, **_kwargs: object) -> list[str]:
    return []


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(agent_identity, "ancestry", no_ancestry)
    return AGENT


def test_posting_value(world: World, agent: dict[str, str]) -> None:
    del world
    assert pr.posting_value(agent) == POSTING
    assert pr.posting_value({}) is None


def test_branch_trailers_are_in_commit_order(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}\nAssisted-by: {OTHER}")
    world.commit("plain")
    world.commit(f"three\n\nSigned-off-by: T <t@example.com>\nGenerated-by: {OTHER}")
    assert pr.branch_trailers("main") == [
        ("Generated-by", GEN),
        ("Assisted-by", OTHER),
        ("Generated-by", OTHER),
    ]


def test_branch_trailers_stop_at_the_merge_base(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"mine\n\nGenerated-by: {GEN}")
    world.git("switch", "-q", "main")
    world.commit(f"theirs\n\nGenerated-by: {OTHER}")
    world.git("switch", "-q", "topic")
    assert pr.branch_trailers("main") == [("Generated-by", GEN)]
    assert pr.branch_trailers("topic") == []


def test_branch_trailers_prefer_the_remote_base(world: World) -> None:
    world.commit(f"upstream\n\nGenerated-by: {OTHER}")
    world.git("update-ref", "refs/remotes/origin/main", "HEAD")
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"mine\n\nGenerated-by: {GEN}")
    world.git("branch", "-f", "main", "HEAD~2")
    assert pr.branch_trailers("main") == [("Generated-by", GEN)]


def test_branch_trailers_need_a_known_base(world: World) -> None:
    del world
    with pytest.raises(
        SystemExit, match=r"cannot list the commits in nope\.\.HEAD; is nope fetched"
    ):
        pr.branch_trailers("nope")


def checked_for(text: str = BASE) -> pr.Checked:
    return pr.check_text(text)


def test_description_without_trailers_is_the_body(world: World) -> None:
    del world
    got = pr.description(checked_for(), {})
    assert got == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"


def test_description_appends_a_trailer_paragraph(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    got = pr.description(checked_for(), agent)
    assert got.endswith(f"\nv\n\nGenerated-by: {GEN}\nAssisted-by: {POSTING}\n")
    parsed = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=got,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert parsed == f"Generated-by: {GEN}\nAssisted-by: {POSTING}\n"
    assert "Signed-off-by" not in got
    assert "raw-session" not in got


def test_human_posting_adds_only_branch_trailers(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    assert pr.description(checked_for(), {}).endswith("v\n")
    world.commit(f"one\n\nAssisted-by: {GEN}")
    got = pr.description(checked_for(), {})
    assert got.endswith(f"v\n\nAssisted-by: {GEN}\n")
    assert got.count("Assisted-by") == 1


def test_posting_agent_already_on_the_branch_is_not_repeated(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {POSTING}")
    got = pr.description(checked_for(), agent)
    assert got.endswith(f"v\n\nGenerated-by: {POSTING}\n")


def test_description_uses_the_frontmatter_base(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    world.git("branch", "dev", "main")
    world.git("switch", "-q", "dev")
    world.commit(f"dev\n\nGenerated-by: {OTHER}")
    world.git("switch", "-q", "-c", "topic2")
    world.commit(f"two\n\nAssisted-by: {GEN}")
    text = "---\nbase: dev\n---\n" + BASE
    got = pr.description(checked_for(text), {})
    assert got.endswith(f"v\n\nAssisted-by: {GEN}\n")
    assert OTHER not in got


@pytest.mark.parametrize(
    "bad", ["LLM model=a:m", "claude-code:opus", "LLM harness=nope"]
)
def test_invalid_branch_trailers_stop_the_description(world: World, bad: str) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {bad}")
    with pytest.raises(SystemExit, match=r"invalid LLM trailer: .*"):
        pr.description(checked_for(), {})


def test_invalid_trailer_message_names_the_value(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit("one\n\nGenerated-by: LLM model=a:m")
    with pytest.raises(SystemExit) as info:
        pr.description(checked_for(), {})
    assert str(info.value) == "invalid LLM trailer: LLM model=a:m: harness required"


# -------------------------------------------------------------- gh and the CLI


def run_main(*argv: str, env: dict[str, str] | None = None) -> int:
    return pr.main(list(argv), env or {})


def write_draft(world: World, text: str = BASE) -> Path:
    path = world.root / ".github" / "PULL_REQUEST_DRAFT.md"
    path.write_text(text)
    os.chdir(world.root)  # the default draft path is relative to the cwd
    return path


def test_check_prints_ok(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    write_draft(world)
    assert run_main("check") == 0
    assert capsys.readouterr().out == "OK: the draft conforms\n"


def test_check_prints_problems_with_paths(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_draft(world, replace(BASE, "\nw\n", "\nw <!-- c -->\n"))
    assert run_main("check", str(path)) == 1
    assert capsys.readouterr().out == (
        f"{path}:5: HTML comment left in the description\n"
    )


def test_check_defaults_to_the_draft_path(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    del world
    assert run_main("check") == 1
    out = capsys.readouterr().out
    assert out == (
        ".github/PULL_REQUEST_DRAFT.md:1: "
        "cannot read the draft: No such file or directory\n"
    )


def test_create_runs_gh_with_the_frontmatter(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    meta = (
        "---\nbase: dev\ndraft: true\nlabels: [bug, good first issue]\n"
        "reviewers: [tbhb, org/team]\nassignees: ['@me']\nmilestone: v1\n---\n"
    )
    world.git("branch", "dev")
    write_draft(world, meta + BASE)
    assert run_main("create") == 0
    (call,) = world.calls("gh")
    assert call["argv"] == [
        "pr", "create", "--title", "fix: x", "--body-file", "-",
        "--base", "dev", "--draft",
        "--label", "bug", "--label", "good first issue",
        "--reviewer", "tbhb", "--reviewer", "org/team",
        "--assignee", "@me", "--milestone", "v1",
    ]  # fmt: skip
    assert (
        call["stdin"]
        == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    assert call["stdin_tty"] is False
    assert capsys.readouterr().out == "https://github.com/tbhb-dev/ascm/pull/1\n"


def test_create_defaults(world: World) -> None:
    write_draft(world)
    assert run_main("create") == 0
    (call,) = world.calls("gh")
    assert call["argv"] == [
        "pr",
        "create",
        "--title",
        "fix: x",
        "--body-file",
        "-",
        "--base",
        "main",
    ]


def test_create_passes_gh_errors_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    message = "aborted: you must first push the current branch to a remote\n"
    world.fake("gh", rc=1, stderr=message)
    write_draft(world)
    assert run_main("create") == 1
    captured = capsys.readouterr()
    assert captured.err == message
    assert captured.out == ""
    assert len(world.calls("gh")) == 1
    assert world.git("status", "--porcelain", "--ignored").count("push") == 0


def test_create_stops_on_problems(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    write_draft(world, "## Why\n\nw\n")
    assert run_main("create") == 1
    assert world.calls("gh") == []
    assert ":1: the first line" in capsys.readouterr().out


def test_dry_run_prints_argv_and_body(
    world: World, capsys: pytest.CaptureFixture[str], agent: dict[str, str]
) -> None:
    (world.bin / "gh").unlink()
    write_draft(world, "---\nlabels: [bug]\n---\n" + BASE)
    assert run_main("create", "--dry-run", env=agent) == 0
    out = capsys.readouterr().out
    assert out == (
        "gh pr create --title 'fix: x' --body-file - --base main --label bug\n\n"
        "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n\n"
        f"Assisted-by: {POSTING}\n"
    )


def test_create_needs_gh(world: World) -> None:
    (world.bin / "gh").unlink()
    write_draft(world)
    with pytest.raises(SystemExit, match="gh not found"):
        run_main("create")


def test_edit(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    write_draft(
        world, "---\nlabels: [bug, docs]\ndraft: true\nbase: main\n---\n" + BASE
    )
    assert run_main("edit", "--pr", "7") == 0
    (call,) = world.calls("gh")
    assert call["argv"] == [
        "pr", "edit", "7", "--title", "fix: x", "--body-file", "-",
        "--add-label", "bug", "--add-label", "docs",
    ]  # fmt: skip
    assert (
        call["stdin"]
        == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    assert capsys.readouterr().out == "https://github.com/tbhb-dev/ascm/pull/1\n"


def test_edit_without_labels_and_with_agent(
    world: World, agent: dict[str, str]
) -> None:
    write_draft(world)
    assert run_main("edit", "--pr", "7", env=agent) == 0
    (call,) = world.calls("gh")
    assert call["argv"] == ["pr", "edit", "7", "--title", "fix: x", "--body-file", "-"]
    assert str(call["stdin"]).endswith(f"v\n\nAssisted-by: {POSTING}\n")


def test_edit_requires_a_pr_number(world: World) -> None:
    del world
    with pytest.raises(SystemExit) as info:
        run_main("edit")
    assert info.value.code == 2


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as info:
        run_main()
    assert info.value.code == 2


def test_main_defaults_to_the_process_environment(
    world: World, monkeypatch: pytest.MonkeyPatch, agent: dict[str, str]
) -> None:
    write_draft(world)
    for key, val in agent.items():
        monkeypatch.setenv(key, val)
    assert pr.main(["create", "--dry-run"]) == 0
    assert pr.main(["check"]) == 0


def test_create_with_an_unreadable_path(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main("create", str(world.root)) == 1
    assert "cannot read the draft: Is a directory" in capsys.readouterr().out


def test_gh_args_without_metadata(world: World) -> None:
    del world
    assert pr.gh_args({}) == ["--base", "main"]
    assert pr.gh_args({"draft": False}) == ["--base", "main"]


def test_run_gives_the_child_no_terminal(world: World) -> None:
    del world
    done = pr.run(
        [
            str(Path(sys.executable)),
            "-c",
            "import sys;print(sys.stdin.isatty(), repr(sys.stdin.read()))",
        ]
    )
    assert done.stdout == "False ''\n"
    done = pr.run(
        [sys.executable, "-c", "import sys;print(sys.stdin.read())"], stdin="hi"
    )
    assert done.stdout == "hi\n"


def test_run_uses_the_given_directory(tmp_path: Path) -> None:
    done = pr.run([sys.executable, "-c", "import os;print(os.getcwd())"], cwd=tmp_path)
    assert done.stdout.strip() == str(tmp_path.resolve())


def test_module_runs_as_a_script(world: World) -> None:
    write_draft(world)
    done = subprocess.run(
        [sys.executable, "-m", "tools.interim.pull_request", "check"],
        cwd=REAL,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REAL)},
    )
    assert done.returncode in {0, 1}


# ------------------------------------------------------- mutation-killing pins


def test_sections_without_a_word_limit(world: World) -> None:
    (world.root / pr.SECTIONS).write_text(
        '[[section]]\nheading = "A"\nrequired = true\n\n'
        '[[section]]\nheading = "B"\nrequired = false\nmax_words = 5\n'
    )
    assert pr.load_sections() == [
        pr.Section("A", required=True, max_words=None),
        pr.Section("B", required=False, max_words=5),
    ]


def test_unknown_section_does_not_hide_later_problems() -> None:
    text = "# fix: x\n\n## Nope\n\nx\n\n## Why\n\n"
    got = structure(text)
    known = (
        "Why, Behavior change, Decisions, Deviations from the task, "
        "Needs human judgment, Risk, Verification, Human steering"
    )
    assert (3, f"unknown section 'Nope'; use: {known}") in got
    assert (7, "section 'Why' is empty") in got


def test_code_lines_do_not_hide_later_trailers() -> None:
    text = BASE.replace("v\n", "v\n\n```\ncode\n```\n\nGenerated-by: LLM x\n")
    assert (
        19,
        "the script writes Generated-by and Assisted-by; remove this line",
    ) in structure(text)


def test_trailer_right_after_the_title_is_found() -> None:
    text = "# fix: x\nGenerated-by: LLM x\n\n## Why\n\nw\n"
    assert (
        2,
        "the script writes Generated-by and Assisted-by; remove this line",
    ) in structure(text)


def test_a_non_title_first_line_is_reported_where_it_is() -> None:
    assert (
        2,
        "the first line after the frontmatter must be the H1 title",
    ) in structure("\nplain\n")


def test_quoted_frontmatter_key_points_at_line_one() -> None:
    # The key is not found by a plain prefix, so the error falls back to line 1,
    # even though the same key appears as plain text further down.
    text = '---\n"base": 3\n---\n' + BASE + "\nbase: x\n"
    found = [line for line, why in structure(text) if why.startswith("frontmatter")]
    assert found == [1]


def test_body_of_keeps_spaces_and_edge_letters() -> None:
    assert pr.body_of(["# T", "", "## Why", "x  ", ""]) == "## Why\nx  \n"
    assert pr.body_of(["# T", "", "Xylophone X", ""]) == "Xylophone X\n"
    assert pr.body_of(["# T", "## Why", "x"]) == "## Why\nx\n"


def test_identity_key_splits_on_the_first_equals() -> None:
    value = "LLM harness=claude-code:1;model=a=b;session=s"
    assert pr.identity_key(value) == ("claude-code", "a=b", "s", "")


def test_posting_agent_is_added_to_an_empty_branch(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    got = pr.description(checked_for(), agent)
    assert got.endswith(f"v\n\nAssisted-by: {POSTING}\n")


def test_every_invalid_trailer_is_listed(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(
        "one\n\nGenerated-by: LLM harness=nope\nAssisted-by: LLM harness=nope2"
    )
    with pytest.raises(SystemExit) as info:
        pr.description(checked_for(), {})
    assert "; " in str(info.value)
    assert str(info.value).count("LLM harness=") == 2


@pytest.mark.usefixtures("world")
def test_branch_git_calls_run_in_the_current_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path | None] = []
    real = agent_identity.git

    def spy(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        seen.append(cwd)
        return real(*args, cwd=cwd)

    monkeypatch.setattr(agent_identity, "git", spy)
    pr.branch_trailers("main")
    assert seen == [None, None]


def test_help_describes_the_script(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run_main("--help")
    assert "\n\nCheck a PR draft and open the PR.\n\n" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["edit"])
def test_pr_number_must_be_an_integer(command: str) -> None:
    with pytest.raises(SystemExit) as info:
        run_main(command, "--pr", "seven")
    assert info.value.code == 2


# ---------------------------------------------------------------------- hook

PATCH = "*** Begin Patch\n*** {verb}: {path}\n*** End Patch\n"
DRAFT_REL = ".github/PULL_REQUEST_DRAFT.md"
LINT_PROBLEM = "pull request hook skipped"


def run_hook(monkeypatch: pytest.MonkeyPatch, harness: str, payload: object) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    return pr.main(["hook", harness], {})


def hook_payloads(root: Path) -> dict[str, tuple[str, dict[str, object]]]:
    """(harness, payload) pairs that all write the draft."""
    sub = root / "sub"
    absolute = str(root / DRAFT_REL)
    return {
        "claude-relative": (
            "claude",
            {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(root)},
        ),
        "claude-absolute": (
            "claude",
            {"tool_input": {"file_path": absolute}, "cwd": str(sub)},
        ),
        "claude-from-subdir": (
            "claude",
            {"tool_input": {"file_path": f"../{DRAFT_REL}"}, "cwd": str(sub)},
        ),
        "claude-no-cwd": ("claude", {"tool_input": {"file_path": absolute}}),
        "codex-add": (
            "codex",
            {
                "tool_name": "apply_patch",
                "cwd": str(root),
                "tool_input": {
                    "command": PATCH.format(verb="Add File", path=DRAFT_REL)
                },
            },
        ),
        "codex-move": (
            "codex",
            {
                "cwd": str(sub),
                "tool_input": {
                    "command": PATCH.format(verb="Update File", path="a.md")
                    + f"*** Move to: ../{DRAFT_REL}\n"
                },
            },
        ),
        "agy-dict": (
            "antigravity",
            {
                "cwd": str(root),
                "toolCall": {"name": "write_to_file", "args": {"TargetFile": absolute}},
                "error": "",
            },
        ),
        "agy-json": (
            "antigravity",
            {
                "cwd": str(root),
                "toolName": "replace_file_content",
                "toolInput": json.dumps({"AbsolutePath": absolute}),
            },
        ),
    }


HOOK_NAMES = list(hook_payloads(Path("/r")))


@pytest.fixture
def hooked(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    (world.root / "sub").mkdir()
    monkeypatch.chdir(world.root)
    return world


def write_hook_draft(world: World, text: str) -> None:
    (world.root / DRAFT_REL).write_text(text)


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_hook_clean_draft_is_silent(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())[name]
    if name == "claude-no-cwd":
        monkeypatch.chdir(hooked.root / "sub")
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert len(hooked.calls("vale")) == 1


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_hook_feeds_problems_back_on_stderr(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    write_hook_draft(hooked, "## Why\n\nw\n")
    harness, payload = hook_payloads(hooked.root.resolve())[name]
    if name == "claude-no-cwd":
        monkeypatch.chdir(hooked.root / "sub")
    assert run_hook(monkeypatch, harness, payload) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"{DRAFT_REL} has problems. Fix them and save the file again.\n"
        f"{DRAFT_REL}:1: the first line after the frontmatter must be the H1 title\n"
    )


def test_hook_reports_lint_findings_with_draft_lines(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hooked.fake("cspell", rc=1, stdout=f"{DRAFT_REL}:3:1 - Unknown word (zz)\n")
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 2
    assert f"{DRAFT_REL}:3: cspell: Unknown word (zz)\n" in capsys.readouterr().err


UNRELATED: list[tuple[str, dict[str, object]]] = [
    ("claude", {"tool_input": {"file_path": "README.md"}}),
    ("claude", {"tool_input": {"file_path": "sub/.github/PULL_REQUEST_DRAFT.md"}}),
    ("claude", {"tool_input": {"file_path": ""}}),
    ("claude", {"tool_input": {"file_path": 3}}),
    ("claude", {"tool_input": {"command": "ls"}}),
    ("claude", {}),
    ("codex", {"tool_input": {"command": "ls .github"}}),
    ("codex", {"tool_input": {"command": 7}}),
    (
        "codex",
        {"tool_input": {"command": PATCH.format(verb="Add File", path="a.md")}},
    ),
    (
        "antigravity",
        {"toolCall": {"name": "run_command", "args": {"TargetFile": DRAFT_REL}}},
    ),
    (
        "antigravity",
        {"toolCall": {"name": "write_file", "args": {"TargetFile": "a.md"}}},
    ),
    ("antigravity", {"toolCall": {"name": "write_file", "args": "not json"}}),
    ("antigravity", {}),
    # A Claude-shaped payload to another harness touches nothing.
    ("codex", {"tool_input": {"file_path": DRAFT_REL}}),
    ("antigravity", {"tool_input": {"file_path": DRAFT_REL}}),
]


@pytest.mark.parametrize(("harness", "payload"), UNRELATED)
def test_hook_ignores_other_tool_calls(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: str,
    payload: dict[str, object],
) -> None:
    write_hook_draft(hooked, "broken")
    payload = {"cwd": str(hooked.root.resolve()), **payload}
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    for name in ("vale", "cspell", "rumdl", "cog"):
        assert hooked.calls(name) == []


@pytest.mark.parametrize("stdin", ["", "not json", "[1]", "null"])
def test_hook_ignores_unreadable_payloads(
    hooked: World, monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    write_hook_draft(hooked, "broken")
    assert run_hook(monkeypatch, "claude", stdin) == 0
    assert hooked.calls("vale") == []


def test_hook_with_a_missing_draft_file_reports_it(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 2
    assert (
        f"{DRAFT_REL}:1: cannot read the draft: No such file or directory\n"
        in capsys.readouterr().err
    )


def test_hook_fails_open_when_a_tool_is_missing(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (hooked.bin / "vale").unlink()
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{LINT_PROBLEM}: vale not found")


def boom(*_args: object, **_kwargs: object) -> pr.Checked:
    message = "boom"
    raise RuntimeError(message)


def test_hook_fails_open_on_a_crash(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pr, "load_checked", boom)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
    assert capsys.readouterr().err == f"{LINT_PROBLEM}: boom\n"


def test_hook_rejects_an_unknown_harness(hooked: World) -> None:
    del hooked
    with pytest.raises(SystemExit) as info:
        pr.main(["hook", "vim"], {})
    assert info.value.code == 2


def test_hook_ignores_trailers_and_the_branch(
    hooked: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooked.git("switch", "-q", "-c", "topic")
    hooked.commit("one\n\nGenerated-by: LLM nonsense")
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0


# ------------------------------------------------------------ frontmatter parse


def test_parse_frontmatter_splits_block_and_body() -> None:
    assert pr.parse_frontmatter("---\nbase: dev\n---\nbody\n") == (
        {"base": "dev"},
        "body\n",
    )
    assert pr.parse_frontmatter("---\nbase: dev\n---  \nbody") == (
        {"base": "dev"},
        "body",
    )
    assert pr.parse_frontmatter("---\n---\nbody") == (None, "body")
    assert pr.parse_frontmatter("plain\n") == (None, "plain\n")


def test_parse_frontmatter_keeps_dates_as_text() -> None:
    data, _ = pr.parse_frontmatter("---\nmilestone: 2024-01-02\n---\n")
    assert data == {"milestone": "2024-01-02"}


def test_parse_frontmatter_errors() -> None:
    with pytest.raises(pr.FrontmatterError, match=r"^unterminated frontmatter$"):
        pr.parse_frontmatter("---\nbase: x\n")
    with pytest.raises(pr.FrontmatterError) as info:
        pr.parse_frontmatter("---\nbase: [\n---\n")
    message = str(info.value)
    assert message.startswith("invalid YAML: ")
    assert "\n" not in message


def test_cog_runs_in_root_wherever_the_script_is_run(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "outside").mkdir()
    monkeypatch.chdir(tmp_path / "outside")
    assert pr.title_problems("fix: x") == []
    assert world.calls("cog")[-1]["cwd"] == str(world.root.resolve())


def test_branch_trailers_come_from_the_current_repository_not_root(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The script lives in one repo and posts a branch of another."""
    other = tmp_path / "other"
    other.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "o@example.com"),
        ("config", "user.name", "O"),
        ("config", "commit.gpgsign", "false"),
        ("commit", "--allow-empty", "-q", "-m", "init"),
        ("update-ref", "refs/remotes/origin/main", "HEAD"),
        ("commit", "--allow-empty", "-q", "-m", f"feat: x\n\nGenerated-by: {FALLBACK}"),
    ):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
    monkeypatch.chdir(other)
    assert world.root == pr.ROOT
    assert pr.branch_trailers("main") == [("Generated-by", FALLBACK)]


def test_same_identity_in_commit_and_posting_is_one_generated_by() -> None:
    """Generated-by wins over the posting agent's own Assisted-by."""
    assert pr.merge_trailers([("Generated-by", FALLBACK)], FALLBACK) == [
        ("Generated-by", FALLBACK)
    ]


def test_generated_by_precedes_assisted_by() -> None:
    assert pr.merge_trailers([("Assisted-by", OTHER), ("Generated-by", GEN)], None) == [
        ("Generated-by", GEN),
        ("Assisted-by", OTHER),
    ]


def test_description_for_one_identity_ends_in_one_generated_by(
    world: World, agent: dict[str, str]
) -> None:
    """Regression: a branch commit and the posting agent share an identity."""
    posting = pr.posting_value(agent)
    assert posting is not None
    world.git("update-ref", "refs/remotes/origin/main", "HEAD")
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"feat: x\n\nGenerated-by: {posting}")
    checked = pr.check_text(BASE)
    body = pr.description(checked, agent)
    assert body.endswith(f"\nGenerated-by: {posting}\n")
    assert "Assisted-by" not in body


@pytest.mark.parametrize(
    ("body", "lines"),
    [
        ("One line.\n\nNext paragraph.\n", []),
        ("First line\nsecond line\n", [1]),
        ("a\nb\nc\n\nd\ne\n", [1, 5]),
        ("- item one\n- item two\n", []),
        ("- item one\n  continues\n- item two\n", [1]),
        ("1. one\n2) two\n", []),
        ("Intro:\n- a\n- b\n", []),
        ("text\n```\ncode\nmore\n```\ntail\n", []),
        ("| a | b |\n| - | - |\n| 1 | 2 |\n", []),
        ("### Heading\ntext\n", []),
        ("text\n<!-- note -->\nmore\n", []),
        (f"text\n{GEN_LINE}\n", []),
        ("text\nSigned-off-by: A <a@b.c>\n", []),
    ],
)
def test_wrap_problems(body: str, lines: list[int]) -> None:
    scanned, _ = pr.scan(body.splitlines(), 1)
    assert pr.wrap_problems(scanned) == [(no, pr.WRAPPED) for no in lines]


def test_a_wrapped_draft_fails_the_check() -> None:
    text = BASE.replace("\nw\n", "\nline one\nline two\n")
    assert (5, pr.WRAPPED) in structure(text)


def test_the_passing_draft_has_no_wrapped_paragraphs() -> None:
    _, body = pr.parse_frontmatter(FIXTURE.read_text())
    scanned, _ = pr.scan(body.splitlines(), 1)
    assert pr.wrap_problems(scanned) == []


def test_rumdl_line_length_is_off_for_drafts() -> None:
    assert (
        "MD013" in (REAL / ".rumdl.toml").read_text().split("disable")[1].split("]")[0]
    )
