# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the interim gh-pr guard hook."""

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tools.interim import guard_gh_pr as guard

DATA = Path(__file__).parent / "testdata"
SCRIPT = Path(guard.__file__)

CREATE = (
    "`gh pr create` is blocked. Write .github/PULL_REQUEST_DRAFT.md from "
    ".github/PULL_REQUEST_TEMPLATE.md, check it with `mise run pr -- check`, "
    "then open the PR with `mise run pr -- create`."
)
EDIT = (
    "`gh pr edit` is blocked. Update .github/PULL_REQUEST_DRAFT.md, "
    "then run `mise run pr -- edit --pr {n}`."
)
MERGE = (
    "`gh pr merge` is blocked. Once the PR is approved, run "
    "`mise run merge -- {n}` (add `--dry-run` to preview the squash message first)."
)

BLOCKED = [
    "gh pr create",
    "gh pr create --title x --body y",
    "gh pr edit 3",
    "gh pr merge 9 --squash",
    "  gh   pr   merge 9",
    "GH_CONFIG_DIR=x gh pr merge 1",
    "FOO=1 BAR='a b' gh pr merge 1",
    "env FOO=1 gh pr create",
    "env -i gh pr create",
    "command gh pr merge 1",
    "exec gh pr merge 1",
    "sudo -n gh pr merge 1",
    "nohup gh pr merge 1",
    "time gh pr merge 1",
    "/opt/homebrew/bin/gh pr merge 1",
    "cd a && gh pr merge 1",
    "cd a; gh pr merge 1",
    "false || gh pr merge 1",
    "echo x | gh pr merge 1",
    "echo x & gh pr merge 1",
    "(gh pr merge 5)",
    "( cd a && gh pr merge 5 )",
    "{ gh pr merge 5; }",
    "x=$(gh pr create)",
    'echo "$(gh pr merge 1)"',
    'echo "a $(echo b) $(gh pr merge 1)"',
    "echo `gh pr merge 1`",
    'echo "`gh pr merge 1`"',
    "echo $(echo $(gh pr merge 1))",
    "gh -R a/b pr edit 3",
    "gh --repo a/b pr edit 3",
    "gh --repo=a/b pr edit 3",
    "gh --hostname h pr edit 3",
    "gh -R a/b -R c/d pr merge 3",
    "bash -c 'gh pr merge 1'",
    'sh -c "gh pr merge 1"',
    "bash -lc 'cd x && gh pr merge 1'",
    "bash -e -c 'gh pr merge 1'",
    "/bin/bash -c 'bash -c \"gh pr merge 1\"'",
    "eval 'gh pr merge 1'",
    "eval gh pr merge 1",
    "xargs gh pr merge",
    "if true; then gh pr merge 1; fi",
    "for i in 1; do gh pr merge $i; done",
    "while false; do :; done; gh pr merge 1",
    "! gh pr merge 1",
    "echo hi\ngh pr create",
    "echo hi \\\n && gh pr create",
    "cat <<EOF\nbody\nEOF\ngh pr merge 1",
    "cat <<-'EOF'\n\tbody\n\tEOF\ngh pr merge 1",
    "cat <<EOF >out && gh pr merge 1\nbody\nEOF",
    "echo 'a' # c\ngh pr merge 1",
    "echo \\' ; gh pr merge 1",
    'echo "\\"" ; gh pr merge 1',
    "echo hi # gh pr view\ngh pr merge 1",
]

ALLOWED = [
    "",
    "ls",
    "gh pr view 1",
    "gh pr list",
    "gh pr checks 1",
    "gh pr diff 1",
    "gh pr status",
    "gh pr",
    "gh pr --help",
    "gh -R a/b",
    "gh -R",
    "gh api repos/a/b/pulls/1/merge -X PUT",
    "gh issue create",
    "mise run pr -- create",
    "mise run pr -- edit --pr 3",
    "mise run merge -- 3 --dry-run",
    "uv run python -m tools.interim.pull_request create",
    "uv run python -m tools.interim.squash_merge 3",
    "git commit -m 'docs: mention gh pr merge'",
    'git commit -m "docs: mention gh pr merge"',
    "git commit -m 'a' -m 'gh pr create'",
    "echo 'gh pr merge 1'",
    'echo "gh pr merge 1"',
    "grep -r 'gh pr create' docs",
    "echo gh pr merge # not run",
    "cat <<EOF\ngh pr merge 1\nEOF",
    "cat <<-EOF\n\tgh pr merge 1\n\tEOF",
    "git commit -m \"$(cat <<'EOF'\ndocs: gh pr merge note\n\ngh pr create\nEOF\n)\"",
    "cat <<EOF",
    "cat << EOF\ngh pr merge 1\nEOF",
    "bash script.sh gh pr merge",
    "bash -c",
    "bash -e",
    "bash --norc -c 'echo ok'",
    "eval",
    "env",
    "env -i",
    "echo $((1 + 2))",
    "gh pr view 1 && gh pr checks 1 | cat",
    "echo 'unterminated gh pr merge",
    "echo \\",
]


def run_find(text: str) -> guard.Match | None:
    """Scan one command."""
    return guard.find(text)


@pytest.mark.parametrize("text", BLOCKED)
def test_blocked(text: str) -> None:
    assert run_find(text) is not None


@pytest.mark.parametrize("text", ALLOWED)
def test_allowed(text: str) -> None:
    assert run_find(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gh pr create --title x", CREATE),
        ("gh pr edit 12", EDIT.format(n=12)),
        ("gh pr edit --title x", EDIT.format(n="<N>")),
        ("gh -R a/b pr edit '#34' --title x", EDIT.format(n=34)),
        ("gh pr edit https://github.com/a/b/pull/56", EDIT.format(n=56)),
        ("gh pr merge 7 --squash", MERGE.format(n=7)),
        ("gh pr merge --auto 8", MERGE.format(n=8)),
        ("gh pr merge", MERGE.format(n="<N>")),
        ("gh pr merge feature-branch", MERGE.format(n="<N>")),
    ],
)
def test_hint(text: str, expected: str) -> None:
    found = run_find(text)
    assert found is not None
    assert guard.hint(found) == expected


def test_first_match_wins() -> None:
    found = run_find("gh pr merge 1; gh pr create")
    assert found is not None
    assert found.sub == "merge"


def test_scanner_backtick_nested_in_dollar_paren_restores_quotes() -> None:
    # The quote opened before `$(` is restored at `)`, so the text after it is
    # still quoted and is not a command.
    assert run_find('echo "$(true) ; gh pr merge 1"') is None


def test_unbalanced_close_paren_is_harmless() -> None:
    assert run_find(") gh pr merge 1") is not None


def test_heredoc_without_closing_line_swallows_the_rest() -> None:
    assert run_find("cat <<EOF\ngh pr merge 1") is None


def test_here_string_is_not_a_heredoc() -> None:
    assert run_find("cat <<< word\ngh pr merge 1") is not None


def test_two_heredocs_on_one_line() -> None:
    text = "cat <<A <<B\nx\nA\ny\nB\ngh pr merge 1"
    assert run_find(text) is not None
    assert run_find("cat <<A <<B\nx\nA\ngh pr merge 1\nB\n") is None


# --- payloads and output contracts -----------------------------------------

CLAUDE_PAYLOAD = json.loads((DATA / "claude-pretooluse-bash-main.json").read_text())
CODEX_PAYLOAD = json.loads((DATA / "codex-pretooluse-bash.json").read_text())
AGY_PAYLOAD = json.loads((DATA / "agy-pretooluse-run-command.json").read_text())
AGY_WRITE = json.loads((DATA / "agy-pretooluse-write.json").read_text())


def with_command(payload: dict[str, object], text: str) -> dict[str, object]:
    """A recorded payload with its shell command replaced."""
    if "toolCall" in payload:
        call = cast("dict[str, object]", payload["toolCall"])
        args = cast("dict[str, object]", call["args"])
        return {**payload, "toolCall": {**call, "args": {**args, "CommandLine": text}}}
    inputs = cast("dict[str, object]", payload["tool_input"])
    return {**payload, "tool_input": {**inputs, "command": text}}


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: str,
) -> tuple[int, str, str]:
    """Run ``main`` on stdin; return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = guard.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD])
def test_claude_and_codex_deny_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, "cd x && gh pr create"))
    code, out, err = run_main(monkeypatch, capsys, stdin)
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": CREATE,
        }
    }


def test_antigravity_deny_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin = json.dumps(with_command(AGY_PAYLOAD, "gh pr merge 4"))
    code, out, err = run_main(monkeypatch, capsys, stdin)
    assert (code, err) == (0, "")
    assert json.loads(out) == {"decision": "deny", "reason": MERGE.format(n=4)}


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD, AGY_PAYLOAD])
def test_recorded_payloads_allow_their_own_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    code, out, err = run_main(monkeypatch, capsys, json.dumps(payload))
    assert (code, out, err) == (0, "", "")


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD, AGY_PAYLOAD])
def test_gh_pr_view_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, "gh pr view 1"))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def test_agy_non_command_tool_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(monkeypatch, capsys, json.dumps(AGY_WRITE)) == (0, "", "")


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "null",
        "[1]",
        "{}",
        '{"tool_input": null}',
        '{"tool_input": {"command": 7}}',
        '{"tool_input": []}',
        '{"toolCall": null}',
        '{"toolCall": {"args": null}}',
        '{"toolCall": {"args": {"CommandLine": 7}}}',
        '{"toolCall": {"args": "gh pr merge 1"}}',
    ],
)
def test_malformed_input_fails_open_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def test_internal_error_fails_open_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_text: str) -> guard.Match | None:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(guard, "find", boom)
    stdin = json.dumps(with_command(CLAUDE_PAYLOAD, "gh pr merge 1"))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


@pytest.mark.parametrize(
    ("payload", "key"),
    [
        (with_command(CLAUDE_PAYLOAD, "gh pr merge 1"), "hookSpecificOutput"),
        (with_command(AGY_PAYLOAD, "gh pr merge 1"), "decision"),
    ],
)
def test_script_runs_standalone(payload: dict[str, object], key: str) -> None:
    done = subprocess.run(  # the interpreter running this test
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert key in json.loads(done.stdout)


# --- properties ------------------------------------------------------------

WORD = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Nd"), whitelist_characters="-_./"
    ),
    min_size=1,
    max_size=8,
).filter(lambda w: not w.startswith(("-", "#")) and "=" not in w)
SEP = st.sampled_from(["; ", " && ", " || ", " | ", " & ", "\n", " ; "])
SUB = st.sampled_from(guard.SUBCOMMANDS)
LEAD = st.sampled_from(
    ["", "FOO=1 ", "env ", "env -i A=b ", "sudo ", "time ", "command ", "! ", "{ "]
)
GH = st.sampled_from(["gh", "/usr/bin/gh", "gh -R a/b", "gh --repo a/b", "gh -q"])


@given(before=st.lists(WORD, max_size=3), sep=SEP, lead=LEAD, gh=GH, sub=SUB)
def test_property_gh_pr_after_any_boundary_is_blocked(
    before: list[str], sep: str, lead: str, gh: str, sub: str
) -> None:
    prefix = " ".join(["echo", *before]) + sep
    found = run_find(f"{prefix}{lead}{gh} pr {sub} 5")
    assert found is not None
    assert found.sub == sub


@given(sub=SUB, wrapper=st.sampled_from(["bash -c", "sh -c", "zsh -lc", "eval"]))
@pytest.mark.parametrize("quote", ["'", '"'])
def test_property_shell_wrappers_are_scanned(
    sub: str, wrapper: str, quote: str
) -> None:
    found = run_find(f"{wrapper} {quote}cd x; gh pr {sub} 5{quote}")
    assert found is not None
    assert found.sub == sub


@given(words=st.lists(WORD, max_size=4), sub=SUB, quote=st.sampled_from(["'", '"']))
def test_property_quoted_text_of_a_plain_command_is_not_run(
    words: list[str], sub: str, quote: str
) -> None:
    arg = f"{quote}{' '.join(words)} gh pr {sub} 5{quote}"
    assert run_find(f"git commit -m {arg}") is None
    assert run_find(f"echo {arg}") is None


@given(
    verb=st.sampled_from(["view", "list", "checks", "diff", "status", "ready"]),
    gh=GH,
)
def test_property_other_pr_subcommands_are_allowed(verb: str, gh: str) -> None:
    assert run_find(f"{gh} pr {verb} 5") is None


@given(st.text(max_size=80))
def test_property_scanner_never_raises(text: str) -> None:
    run_find(text)


@given(st.text(max_size=80))
def test_property_main_is_silent_without_a_match(text: str) -> None:
    payload = json.dumps({"tool_input": {"command": text}})
    found = run_find(text)
    out = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(out):
        assert guard.main() == 0
    assert (out.getvalue() != "") == (found is not None)


# --- harness configs -------------------------------------------------------

ROOT = SCRIPT.parents[2]


def guard_groups(path: str, *keys: str) -> list[dict[str, object]]:
    """The PreToolUse groups in a checked-in harness config that run the guard."""
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    for key in keys:
        data = data[key]
    groups = cast("list[dict[str, object]]", data["PreToolUse"])
    return [
        g
        for g in groups
        if any(
            "guard_gh_pr.py" in h["command"]
            for h in cast("list[dict[str, str]]", g["hooks"])
        )
    ]


@pytest.mark.parametrize(
    ("path", "keys", "matcher"),
    [
        (".claude/settings.json", ("hooks",), "Bash"),
        (".codex/hooks.json", ("hooks",), "^Bash$"),
        (".agents/hooks.json", ("pull-request",), "run_command"),
    ],
)
def test_every_harness_config_runs_the_guard(
    path: str, keys: tuple[str, ...], matcher: str
) -> None:
    (group,) = guard_groups(path, *keys)
    assert group["matcher"] == matcher
    (handler,) = cast("list[dict[str, str]]", group["hooks"])
    assert handler["command"].startswith(
        "uv run --no-project --quiet --python '>=3.15' python "
    )
