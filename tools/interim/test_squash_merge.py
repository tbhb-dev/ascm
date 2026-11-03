# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the interim squash-merge script."""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tools.interim import agent_identity
from tools.interim import pull_request as pr
from tools.interim import squash_merge as sm
from tools.interim.fake_tool import write_fake

FIXTURE = Path(__file__).parent / "testdata" / "pull_request" / "passing-draft.md"
HARNESS_ENV = {"CLAUDE_CODE_SESSION_ID": "raw-session"}
GEN = (
    "Generated-by: LLM harness=claude-code:2.1.0;model=anthropic:m1;"
    "session=sha-256-64:aaaaaaaaaaaaaaaa"
)
ASSIST = "Assisted-by: LLM harness=codex;model=openai:gpt-6.1-sol"

GH = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(here, "gh.spec")))
kind = sys.argv[2] if sys.argv[1] == "pr" else "?"
with open(os.path.join(here, "gh.log"), "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\\n")
sys.stdout.write(spec[kind + "_out"])
sys.stderr.write(spec[kind + "_err"])
sys.exit(spec[kind + "_rc"])
"""
COG = """import sys
text = sys.stdin.read()
if "BAD" in text:
    sys.stderr.write("not conventional")
    sys.exit(1)
"""


@dataclass
class Tools:
    """Fake gh and cog on an isolated PATH."""

    bin: Path

    def gh(self, **spec: object) -> None:
        full: dict[str, object] = {
            "view_rc": 0,
            "view_out": "",
            "view_err": "",
            "merge_rc": 0,
            "merge_out": "merged\n",
            "merge_err": "",
        }
        (self.bin / "gh.spec").write_text(json.dumps(full | spec))

    def view(self, **fields: object) -> None:
        data: dict[str, object] = {
            "title": "feat(cli): add the stamp subcommand",
            "body": FIXTURE.read_text().split("\n# ", 1)[1].split("\n", 1)[1],
            "headRefOid": "abc123",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
        }
        self.gh(view_out=json.dumps(data | fields))

    def calls(self) -> list[dict[str, object]]:
        log = self.bin / "gh.log"
        return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tools:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "git").symlink_to(shutil.which("git") or "git")
    for name, body in (("gh", GH), ("cog", COG)):
        write_fake(bin_dir / name, body.format())
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    prefixes = ("CLAUDE", "CODEX", "ANTIGRAVITY", "GIT_")
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(prefixes)]))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{git_dir}")
    made = Tools(bin_dir)
    made.view()
    return made


def message_of(body: str, title: str = "fix: x", number: int = 7) -> str:
    return sm.build_message(title, number, body).text()


def read_back(message: str) -> str:
    done = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


# ------------------------------------------------------------------ wrapping

WORD = st.text(
    st.characters(categories=("L", "N", "P", "S"), exclude_characters="`"),
    min_size=1,
    max_size=90,
)
SPAN = st.builds(
    "`{}`".format,
    st.text("ab -=()", min_size=1, max_size=30).filter(
        lambda text: text.strip() == text and text
    ),
)
TEXT = st.lists(st.one_of(WORD, SPAN), min_size=1, max_size=60).map(" ".join)
PREFIX = st.sampled_from([("", ""), ("- ", "  "), ("12. ", "    "), ("  * ", "    ")])


@given(text=TEXT, prefix=PREFIX)
def test_wrap_keeps_every_token_in_order_within_the_limit(
    text: str, prefix: tuple[str, str]
) -> None:
    lines = sm.wrap(text, *prefix)
    tokens = [match[0] for match in sm.TOKEN.finditer(text)]
    bare = [
        line.removeprefix(prefix[0] if i == 0 else prefix[1])
        for i, line in enumerate(lines)
    ]
    assert [m[0] for m in sm.TOKEN.finditer("\n".join(bare))] == tokens
    for line in lines:
        assert len(line) <= sm.WIDTH or len(sm.TOKEN.findall(line)) == 1
    assert lines[0].startswith(prefix[0])
    assert all(line.startswith(prefix[1]) for line in lines[1:])


def test_wrap_never_splits_a_long_token() -> None:
    url = "https://example.com/" + "a" * 100
    assert sm.wrap(f"see {url} now", "", "") == ["see", url, "now"]


def test_wrap_keeps_a_code_span_whole() -> None:
    text = "x" * 60 + " `a long span here` tail"
    assert sm.wrap(text, "", "") == ["x" * 60, "`a long span here` tail"]


# ------------------------------------------------------------------- render


def test_sections_become_plain_headings_with_blank_lines_between() -> None:
    body = "## Why\n\nBecause.\n\nTwice.\n\n## Risk\n\nNone here.\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nBecause.\n\nTwice.\n\nRisk\nNone here.\n"
    )


def test_comments_and_empty_sections_vanish() -> None:
    body = (
        "<!-- guidance\nacross lines -->\n\n## Why\n\n<!-- Required -->\n\n"
        "## Decisions\n\n<!-- Optional -->\n\n## Risk\n\nReal text. <!-- x -->\n"
    )
    assert message_of(body) == "fix: x (#7)\n\nRisk\nReal text.\n"


def test_preamble_text_comes_first() -> None:
    assert (
        message_of("Lead in.\n\n## Why\n\nw\n") == "fix: x (#7)\n\nLead in.\n\nWhy\nw\n"
    )


def test_prose_wraps_and_joins_soft_breaks() -> None:
    words = " ".join(["word"] * 40)
    out = message_of(f"## Why\n\n{words[:50]}\n{words[50:]}\n").splitlines()
    assert max(map(len, out)) <= sm.WIDTH
    assert " ".join(out[3:]).split() == ["word"] * 40


def test_list_items_keep_markers_with_a_hanging_indent() -> None:
    items = "- " + "alpha " * 20 + "\n  more\n2. second\n  * nested\n"
    out = message_of(f"## Verification\n\n{items}").splitlines()
    assert out[3].startswith("- alpha")
    assert out[4].startswith("  alpha") or out[4].startswith("  more")
    assert any(line.startswith("2. second") for line in out)
    assert "  * nested" in out
    assert all(len(line) <= sm.WIDTH for line in out)


def test_fenced_code_is_indented_not_wrapped() -> None:
    long = "$ " + "x" * 100
    body = f"## Verification\n\nRan:\n\n```console\n{long}\n\nok\n```\n\nDone.\n"
    assert message_of(body) == (
        f"fix: x (#7)\n\nVerification\nRan:\n\n    {long}\n\n    ok\n\nDone.\n"
    )


def test_a_longer_fence_ignores_a_shorter_inner_one() -> None:
    body = "## Why\n\n````md\n```\ninner\n```\n````\n"
    assert message_of(body) == "fix: x (#7)\n\nWhy\n    ```\n    inner\n    ```\n"


def test_other_fence_characters_do_not_close() -> None:
    body = "## Why\n\n~~~\n```\n~~~\n"
    assert message_of(body) == "fix: x (#7)\n\nWhy\n    ```\n"


def test_links_become_numbered_references() -> None:
    body = (
        "## Why\n\nSee [the issue](https://x.test/1) and "
        "[the spec](https://x.test/2) and [again](https://x.test/1).\n"
        "Bare https://x.test/3 and <https://x.test/4> and "
        "[https://x.test/5](https://x.test/5).\n"
    )
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nSee the issue [1] and the spec [2] and again [1]. "
        "Bare https://x.test/3\nand https://x.test/4 and https://x.test/5.\n\n"
        "[1]: https://x.test/1\n[2]: https://x.test/2\n"
    )


def test_emphasis_goes_and_inline_code_stays() -> None:
    body = "## Why\n\nA **strong** and *soft* word, `*raw* [a](b)` and 2 * 3 * 4.\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nA strong and soft word, `*raw* [a](b)` and 2 * 3 * 4.\n"
    )


def test_a_nested_heading_is_kept_as_text() -> None:
    assert message_of("## Why\n\n### Sub\n\nx\n") == "fix: x (#7)\n\nWhy\nSub\n\nx\n"


def test_an_empty_body_is_a_subject_only() -> None:
    assert message_of("") == "fix: x (#7)\n"
    assert message_of("<!-- only -->\n") == "fix: x (#7)\n"


@given(text=TEXT)
def test_rendered_prose_is_within_the_limit(text: str) -> None:
    text = text.replace("\\", "").replace("*", "").replace("_", "").replace("[", "")
    out = sm.render([pr.Line(1, text, text, code=False)], {})
    for line in "\n".join(out).splitlines():
        assert len(line) <= sm.WIDTH or len(sm.TOKEN.findall(line)) == 1


# ------------------------------------------------------------------ trailers


def test_final_trailer_block_is_kept_verbatim_and_last() -> None:
    body = f"## Why\n\nw\n\n{GEN}\n{ASSIST}\n"
    message = message_of(body)
    assert message.endswith(f"\n\n{GEN}\n{ASSIST}\n")
    assert read_back(message) == f"{GEN}\n{ASSIST}\n"


def test_trailers_are_never_wrapped() -> None:
    long = GEN + ";surface=" + "x" * 90
    message = message_of(f"## Why\n\nw\n\n{long}\n")
    assert message.splitlines()[-1] == long


def test_trailers_alone_after_the_subject() -> None:
    assert message_of(f"{GEN}\n") == f"fix: x (#7)\n\n{GEN}\n"


def test_a_trailer_inside_a_paragraph_is_not_a_trailer_block() -> None:
    with pytest.raises(SystemExit, match="remove this line"):
        message_of(f"## Why\n\nw\n{GEN}\n\nmore\n")


def test_a_trailer_in_the_middle_is_refused() -> None:
    with pytest.raises(SystemExit, match="remove this line"):
        message_of(f"## Why\n\n{GEN}\n\nw\n")


def test_an_invalid_trailer_value_is_refused() -> None:
    with pytest.raises(SystemExit, match="invalid trailer"):
        message_of("## Why\n\nw\n\nGenerated-by: nonsense\n")


@pytest.mark.parametrize("key", ["Signed-off-by", "Co-authored-by", "signed-off-by"])
def test_human_trailers_in_the_body_are_refused(key: str) -> None:
    with pytest.raises(SystemExit, match="do not belong in the description"):
        message_of(f"## Why\n\nw\n\n{key}: A <a@b.c>\n")


def test_a_trailer_git_cannot_read_back_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    done = subprocess.CompletedProcess([], 0, stdout="other\n", stderr="")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return done

    def same(name: str) -> str:
        return name

    monkeypatch.setattr(pr, "run", fake_run)
    monkeypatch.setattr(pr, "find_tool", same)
    with pytest.raises(SystemExit, match="did not survive"):
        sm.check_round_trip("m", [GEN])


# ------------------------------------------------------------------- subject


@pytest.mark.usefixtures("tools")
def test_subject_gets_the_pr_number() -> None:
    assert (
        sm.build_message("feat(cli): add x", 12, "").subject == "feat(cli): add x (#12)"
    )


@pytest.mark.usefixtures("tools")
def test_subject_over_72_with_the_suffix_fails() -> None:
    title = "feat: " + "a" * (72 - len("feat: ") - len(" (#5)") + 1)
    assert len(title) + len(" (#5)") == sm.WIDTH + 1
    with pytest.raises(SystemExit, match="characters; the limit is 72"):
        sm.build_message(title, 5, "")


@pytest.mark.usefixtures("tools")
def test_subject_of_exactly_72_passes() -> None:
    title = "feat: " + "a" * (72 - len("feat: ") - len(" (#5)"))
    assert len(sm.build_message(title, 5, "").subject) == sm.WIDTH


@pytest.mark.usefixtures("tools")
def test_subject_that_fails_cog_fails() -> None:
    with pytest.raises(SystemExit, match="cog verify"):
        sm.build_message("BAD title", 5, "")


def test_an_unclosed_fence_is_refused() -> None:
    with pytest.raises(SystemExit, match="never closed"):
        message_of("## Why\n\n```\ncode\n")


# ------------------------------------------------------------- fixture body


def test_the_passing_draft_becomes_a_clean_message(tools: Tools) -> None:
    body = json.loads((tools.bin / "gh.spec").read_text())["view_out"]
    request = sm.fetch(1)
    assert json.loads(body)["body"] == request.body
    message = sm.build_message(request.title, 12, request.body).text()
    assert message == (
        "feat(cli): add the stamp subcommand (#12)\n\n"
        "Why\n"
        "Running `ascm stamp` on a tree with a read-only file stops partway. The\n"
        "command leaves some files stamped and others not, and prints nothing\n"
        "that says which. See the issue [1].\n\n"
        "Behavior change\n"
        "The command now checks every target before it writes one. A read-only\n"
        "file fails the whole run with an exit code of 1 and a list of the\n"
        "blocked paths. A run that passes the check stamps every file.\n\n"
        "Verification\n"
        "Ran the new unit tests, then stamped a tree with one read-only file:\n\n"
        "    $ go test ./internal/stamp/...\n"
        "    ok  \tgithub.com/tbhb-dev/ascm/internal/stamp\t0.412s\n"
        "    $ ascm stamp ./fixture\n"
        "    error: 1 file is read-only: fixture/a.txt\n\n"
        "No file in the tree changed after the failure.\n\n"
        "[1]: https://github.com/tbhb-dev/ascm/issues/12\n"
    )


# ------------------------------------------------------------------- the CLI


def run_main(*argv: str) -> tuple[int | str | None, str]:
    try:
        return sm.main(list(argv)), ""
    except SystemExit as exc:
        return exc.code, ""


def test_dry_run_prints_the_command_and_message_and_merges_nothing(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_main("12", "--dry-run")
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(
        "gh pr merge 12 --squash --subject 'feat(cli): add the stamp subcommand (#12)' "
        "--body-file - --match-head-commit abc123\n\n"
        "feat(cli): add the stamp subcommand (#12)\n\nWhy\n"
    )
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


def test_merge_passes_subject_body_and_head(tools: Tools) -> None:
    assert run_main("12")[0] == 0
    merge = tools.calls()[1]
    assert merge["argv"] == [
        "pr", "merge", "12", "--squash",
        "--subject", "feat(cli): add the stamp subcommand (#12)",
        "--body-file", "-", "--match-head-commit", "abc123",
    ]  # fmt: skip
    assert str(merge["stdin"]).startswith("Why\nRunning `ascm stamp`")
    assert str(merge["stdin"]).endswith(
        "[1]: https://github.com/tbhb-dev/ascm/issues/12\n"
    )


def test_merge_reports_gh_output_and_exit_code(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    tools.view()
    spec = json.loads((tools.bin / "gh.spec").read_text())
    tools.gh(**{**spec, "merge_rc": 4, "merge_out": "o\n", "merge_err": "e\n"})
    assert run_main("3")[0] == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("o\n", "e\n")


def test_a_body_without_sections_merges_with_the_subject_only(tools: Tools) -> None:
    tools.view(body="")
    assert run_main("3")[0] == 0
    assert tools.calls()[1]["stdin"] == ""


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"isDraft": True}, "draft"),
        ({"state": "MERGED"}, "merged"),
        ({"mergeable": "CONFLICTING"}, "mergeable=CONFLICTING"),
        ({"mergeStateStatus": "BLOCKED"}, "mergeStateStatus=BLOCKED"),
        ({"mergeStateStatus": "BEHIND"}, "BEHIND"),
        (
            {
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [
                    {"name": "lint", "conclusion": "FAILURE"},
                    {"context": "ci/x", "state": "ERROR"},
                    {"name": "ok", "conclusion": "SUCCESS"},
                ],
            },
            "failing checks: lint, ci/x",
        ),
    ],
)
def test_unmergeable_prs_are_refused_with_gh_s_reason(
    tools: Tools, fields: dict[str, object], reason: str
) -> None:
    tools.view(**fields)
    code, _ = run_main("3")
    assert isinstance(code, str)
    assert code.startswith("not merging PR #3: ")
    assert reason in code
    assert len(tools.calls()) == 1


def test_a_clean_pr_with_odd_rollup_entries_is_fine() -> None:
    assert sm.failing_checks("nope") == []
    assert sm.failing_checks(["x", {"conclusion": "SUCCESS"}]) == []


def test_gh_view_failure_is_reported(tools: Tools) -> None:
    tools.gh(view_rc=1, view_err="no\nsuch pr\n")
    assert run_main("3")[0] == "gh pr view failed: no such pr"


def test_gh_view_garbage_is_reported(tools: Tools) -> None:
    tools.gh(view_out="<html>")
    code = run_main("3")[0]
    assert isinstance(code, str)
    assert code.startswith("gh pr view returned something that is not JSON")


def test_missing_fields_default_to_empty_and_refuse(tools: Tools) -> None:
    tools.gh(view_out="{}")
    code = run_main("3")[0]
    assert isinstance(code, str)
    assert "the PR is " in code


def test_null_body_is_empty(tools: Tools) -> None:
    tools.view(body=None)
    assert run_main("3")[0] == 0


# ------------------------------------------------------------------ the agent


def test_there_is_no_signoff_flag() -> None:
    with pytest.raises(SystemExit):
        sm.main(["12", "--dry-run", "--signoff"])


@pytest.mark.parametrize(
    "env",
    [
        HARNESS_ENV,
        {"CODEX_SESSION_ID": "s"},
        {"ANTIGRAVITY_CONVERSATION_ID": "c"},
    ],
)
def test_an_agent_environment_changes_nothing(
    tools: Tools, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main("12")[0] == 0
    plain = tools.calls()
    (tools.bin / "gh.log").unlink()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert run_main("12")[0] == 0
    assert tools.calls() == plain
    assert [call["argv"][1] for call in plain] == ["view", "merge"]  # type: ignore[index]


def test_a_dry_run_is_the_same_in_an_agent_environment(
    tools: Tools, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main("12", "--dry-run")[0] == 0
    plain = capsys.readouterr().out
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "raw")
    assert run_main("12", "--dry-run")[0] == 0
    assert capsys.readouterr().out == plain
    assert [call["argv"][1] for call in tools.calls()] == ["view", "view"]  # type: ignore[index]


def test_the_script_never_adds_human_trailers(tools: Tools) -> None:
    assert run_main("12")[0] == 0
    stdin = str(tools.calls()[1]["stdin"])
    assert not re.search(r"Signed-off-by|Co-authored-by", stdin, re.IGNORECASE)


@pytest.mark.usefixtures("tools")
def test_module_main_guard() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "tools.interim.squash_merge", "3", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        cwd=pr.ROOT,
        env={**os.environ, "PATH": os.environ["PATH"]},
    )
    assert done.returncode == 0
    assert "gh pr merge 3" in done.stdout


def test_the_fallback_identity_block_is_kept_and_passes_the_check() -> None:
    block = (
        "Generated-by: LLM effort=medium;harness=claude-code:2.1.316;"
        "session=sha-256-64:71c761039ac2c7f9;surface=cli"
    )
    message = message_of(f"## Why\n\nw\n\n{block}\n")
    assert message.endswith(f"\n\n{block}\n")
    assert read_back(message) == f"{block}\n"
    assert agent_identity.check_message(message) == []


def test_one_line_paragraphs_still_wrap_at_72() -> None:
    sentence = " ".join(["word"] * 60)
    out = message_of(f"## Why\n\n{sentence}\n\n- {sentence}\n").splitlines()
    assert len(out) > 8
    assert all(len(line) <= sm.WIDTH for line in out)
    assert out[3].startswith("word word")
    assert any(line.startswith("  word") for line in out)
