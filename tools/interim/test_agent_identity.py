# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the interim agent-identity hooks."""

import hashlib
import io
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import jsonschema
import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from tools.interim import agent_identity as ai

if TYPE_CHECKING:
    from collections.abc import Callable

# mutmut runs the tests from a copy of the source tree, so fixtures resolve
# against this file rather than the working directory.
DATA = Path(__file__).parent / "testdata"
SCRIPT = Path(ai.__file__)

H = "sha-256-64:"
ROOT = H + "a" * 16
CHILD = H + "c" * 16
OTHER = H + "d" * 16
AGENT_VARS = (
    "GIT_",
    "PRE_COMMIT",
    "PREK",
    "CLAUDE",
    "CODEX",
    "ANTIGRAVITY",
    "ASCM_",
    "AI_AGENT",
)

type Json = dict[str, object]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No injected GIT_* or agent variables, no user git config, a temp HOME."""
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(AGENT_VARS)]))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.invalid")


def run_git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=hook_env(),
    )
    return done.stdout


def init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    run_git(path, "config", "commit.gpgsign", "false")
    return path.resolve()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = init_repo(tmp_path / "repo")
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory outside any repository, as the working directory."""
    path = tmp_path / "plain"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


def stage(repo: Path, name: str, text: str | None = None) -> None:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(name + "\n" if text is None else text, encoding="utf-8")
    run_git(repo, "add", "--", name)


def loaded(path: Path) -> Json:
    return cast("Json", json.loads(path.read_text(encoding="utf-8")))


def stdin_fixture(name: str, **overrides: object) -> Json:
    return {**loaded(DATA / name), **overrides}


def feed(monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def make_ident(
    sub: str | None = None,
    model: str | None = "anthropic:m",
    root: str = ROOT,
    effort: str | None = None,
    surface: str | None = None,
) -> ai.Ident:
    return ai.Ident(root, "claude-code", sub, model, effort, surface)


def make_receipt(
    path: str,
    who: ai.Ident,
    blob: str | None = None,
    ts: float = 1,
) -> Json:
    return {
        "root": who.root,
        "subagent": who.subagent,
        "harness": who.harness,
        "model": who.model,
        "effort": who.effort,
        "surface": who.surface,
        "path": path,
        "blob": blob,
        "ts": ts,
        "consumed": False,
    }


def committer_of(who: ai.Ident, root: ai.Ident | None = None) -> ai.Committer:
    return ai.Committer(ai.CLAUDE, who, root or who)


# ------------------------------------------------------------------ hashing


def test_hash_vectors() -> None:
    vectors = loaded(DATA / "hash-vectors.json")["vectors"]
    assert isinstance(vectors, list)
    assert len(vectors) >= 3
    for vector in cast("list[dict[str, str]]", vectors):
        raw = bytes.fromhex(vector["input_hex"]).decode()
        assert raw == vector["input_utf8"]
        assert ai.sha256_64(raw) == vector["expected"]


@given(st.text())
def test_sha256_64_shape(raw: str) -> None:
    hashed = ai.sha256_64(raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    assert hashed == f"sha-256-64:{digest[:16]}"
    assert ai.HASH_RE.fullmatch(hashed)
    assert ai.hx(hashed) == digest[:16]


def test_sha256_64_hashes_the_string_form() -> None:
    assert ai.sha256_64(12) == ai.sha256_64("12")


# ------------------------------------------------------------ small helpers


@pytest.mark.parametrize(
    ("value", "expected"),
    [("x", "x"), ("", None), (None, None), (3, None), (["x"], None)],
)
def test_as_str(value: object, expected: str | None) -> None:
    assert ai.as_str(value) == expected


def test_as_dict() -> None:
    assert ai.as_dict({"a": 1}) == {"a": 1}
    assert ai.as_dict({}) == {}
    assert ai.as_dict([1]) is None
    assert ai.as_dict("x") is None


def test_first_str_takes_first_nonempty_string() -> None:
    data: Json = {"a": "", "b": 3, "c": "yes", "d": "later"}
    assert ai.first_str(data, "a", "b", "c", "d") == "yes"
    assert ai.first_str(data, "a", "b", "missing") is None
    assert ai.first_str(data) is None


def test_read_json_defaults(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    listy = tmp_path / "list.json"
    listy.write_text("[1]", encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    fallback: Json = {"d": 1}
    assert ai.read_json(good, fallback) == {"a": 1}
    assert ai.read_json(listy, fallback) is fallback
    assert ai.read_json(bad, fallback) is fallback
    assert ai.read_json(tmp_path / "missing.json", fallback) is fallback
    assert ai.read_json(bad) is None
    assert ai.read_record(good) == {"a": 1}
    assert ai.read_record(bad) == {}
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert ai.read_record(empty) == {}


def test_write_json_is_atomic_and_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "r.json"
    ai.write_json(target, {"k": "v"})
    assert loaded(target) == {"k": "v"}
    ai.write_json(target, {"k": "w"})
    assert loaded(target) == {"k": "w"}
    assert [p.name for p in target.parent.iterdir()] == ["r.json"]


@given(
    st.dictionaries(
        st.text(max_size=8),
        st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8)),
    )
)
def test_json_round_trip(obj: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.json"
        ai.write_json(path, obj)
        assert ai.read_json(path) == obj


LOCK_PROBE = """
import fcntl, sys
with open(sys.argv[1], "a") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
"""


def lock_is_free(path: Path) -> bool:
    done = subprocess.run(
        [sys.executable, "-c", LOCK_PROBE, str(path)], check=False, env=hook_env()
    )
    return done.returncode == 0


def test_locked_holds_an_exclusive_lock_and_creates_the_store(
    tmp_path: Path,
) -> None:
    store = tmp_path / "x" / "ascm-interim"
    with ai.locked(store):
        assert not lock_is_free(store / "lock")
    assert lock_is_free(store / "lock")
    with ai.locked(store):  # the store exists now
        pass
    assert [p.name for p in store.iterdir()] == ["lock"]


RECORD_WRITER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import agent_identity as ai
for i in range(20):
    ai.write_record(sys.argv[3], {f"{sys.argv[4]}-{i}": "v"}, Path(sys.argv[2]))
"""


def test_concurrent_record_writers_lose_no_update(repo: Path) -> None:
    """Regression: unlocked read-modify-write lost most of these updates."""
    writers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                RECORD_WRITER,
                str(SCRIPT.parent),
                str(repo),
                ROOT,
                str(n),
            ],
            env=hook_env(),
        )
        for n in range(10)
    ]
    assert [w.wait() for w in writers] == [0] * 10
    assert len(ai.load_record(ROOT)) == 200


# ---------------------------------------------------- detection and values


def test_detect_innermost_wins() -> None:
    assert ai.detect({}) is None
    assert ai.detect({"CLAUDE_CODE_SESSION_ID": ""}) is None
    assert ai.detect({"CLAUDE_CODE_SESSION_ID": "a"}) == ("claude", "a")
    both = {"CLAUDE_CODE_SESSION_ID": "a", "CODEX_SESSION_ID": "b"}
    assert ai.detect(both) == ("codex", "b")
    all3 = {**both, "ANTIGRAVITY_CONVERSATION_ID": "c"}
    assert ai.detect(all3) == ("antigravity", "c")
    assert ai.detect({"ANTIGRAVITY_CONVERSATION_ID": "c"}) == ("antigravity", "c")


def test_version_per_harness() -> None:
    assert ai.version("claude", {"CLAUDE_CODE_EXECPATH": "/x/2.1.316"}) == "2.1.316"
    assert ai.version("claude", {}) is None
    assert ai.version("claude", {"CLAUDE_CODE_EXECPATH": ""}) is None
    assert ai.version("codex", {"CODEX_VERSION": "0.166.0"}) == "0.166.0"
    assert ai.version("codex", {"CODEX_VERSION": ""}) is None
    assert ai.version("codex", {}) is None
    assert ai.version("antigravity", {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}) == (
        "1.4.12"
    )
    assert ai.version("antigravity", {"ANTIGRAVITY_LS_VERSION": "1.4.12"}) == "1.4.12"
    assert ai.version("antigravity", {"ANTIGRAVITY_LS_VERSION": ""}) is None
    assert ai.version("antigravity", {}) is None
    assert ai.version("other", {"ANTIGRAVITY_LS_VERSION": "cli-1"}) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("High", "high"),
        ("  low  ", "low"),
        ("", None),
        ("   ", None),
        (None, None),
        ("a b", None),
        ("a;b", None),
        ("a,b", None),
        ("<synthetic>", None),
        (5, "5"),
    ],
)
def test_clean_value(value: object, expected: str | None) -> None:
    assert ai.clean_value(value) == expected


@given(st.text())
def test_clean_value_is_grammar_safe(text: str) -> None:
    cleaned = ai.clean_value(text)
    if cleaned is not None:
        assert cleaned
        assert cleaned == cleaned.strip().lower()
        assert not re.search(r"[\s;,]", cleaned)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sdk-cli", "sdk-cli"),
        ("CLI", "CLI"),
        ("a b", None),
        ("a=b", None),
        ("a;b", None),
        ("a,b", None),
        ("", None),
        (None, None),
        (3, None),
    ],
)
def test_surface_ok(value: object, expected: str | None) -> None:
    assert ai.surface_ok(value) == expected


def test_surface_per_harness() -> None:
    assert ai.surface("claude", {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) == "sdk-cli"
    assert ai.surface("claude", {"CLAUDE_CODE_ENTRYPOINT": "a b"}) is None
    assert ai.surface("claude", {}) is None
    assert ai.surface("codex", {"CLAUDE_CODE_ENTRYPOINT": "x"}) is None
    assert ai.surface("other", {"ANTIGRAVITY_LS_VERSION": "cli-1"}) is None
    assert ai.surface("antigravity", {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}) == "cli"
    assert ai.surface("antigravity", {"ANTIGRAVITY_LS_VERSION": "1.4.12"}) is None
    assert ai.surface("antigravity", {}) is None


def test_agy_model_table() -> None:
    assert ai.agy_model("claude-sonnet-5-5-low") == (
        "anthropic:claude-sonnet-5-5",
        "low",
    )
    assert ai.agy_model("gpt-oss-120b-medium") == ("openai:gpt-oss-120b", "medium")
    assert ai.agy_model("gemini-4-argon-high") == ("gcp.gemini:gemini-4-argon", "high")
    assert ai.agy_model("claude-opus-5-5-medium") == (
        "anthropic:claude-opus-5-5",
        "medium",
    )
    assert ai.agy_model("gemini-3.8-flash-low") == (
        "gcp.gemini:gemini-3.8-flash",
        "low",
    )
    assert len(ai.AGY_MODELS) == 13
    for key in (
        "gemini-4-argon-cyber-permissive",
        "gemini-3.8-flash-tiered",
        "gemini-3.8-flash",
        "gemini-4-argon-xhigh",
        "",
    ):
        assert ai.agy_model(key) == (key, None)


def test_provider_model() -> None:
    assert (
        ai.provider_model("claude", "claude-haiku-5-5") == "anthropic:claude-haiku-5-5"
    )
    assert ai.provider_model("codex", "gpt-5.6-sol") == "openai:gpt-5.6-sol"
    assert ai.provider_model("codex", "o3-mini") == "openai:o3-mini"
    assert ai.provider_model("codex", "codex-mini") == "openai:codex-mini"
    assert ai.provider_model("codex", "mystery") == "mystery"
    assert ai.provider_model("codex", "xgpt-5") == "xgpt-5"
    assert ai.provider_model("antigravity", "gemini-3.8-flash-medium") == (
        "gcp.gemini:gemini-3.8-flash"
    )
    assert ai.provider_model("antigravity", "gemini-3.8-flash-tiered") == (
        "gemini-3.8-flash-tiered"
    )
    assert ai.provider_model("claude", "<synthetic>") is None
    assert ai.provider_model("claude", "a b") is None
    assert ai.provider_model("claude", "a;b") is None
    assert ai.provider_model("claude", "a,b") is None
    assert ai.provider_model("claude", "") is None
    assert ai.provider_model("claude", None) is None
    assert ai.provider_model("other", "m") == "m"


def test_slugs_and_providers() -> None:
    assert ai.SLUGS == {
        "claude": "claude-code",
        "codex": "codex",
        "antigravity": "antigravity",
    }
    assert ai.PROVIDERS == {
        "claude": "anthropic",
        "codex": "openai",
        "antigravity": "gcp.gemini",
    }


def test_make_ident_omits_unknowns() -> None:
    assert ai.make_ident("claude", ROOT, ai.Seen()) == ai.Ident(ROOT, "claude-code")
    seen = ai.Seen("2.1.316", "m", "High", "cli")
    assert ai.make_ident("claude", ROOT, seen, CHILD) == ai.Ident(
        ROOT, "claude-code:2.1.316", CHILD, "anthropic:m", "high", "cli"
    )
    assert ai.make_ident("claude", ROOT, ai.Seen(effort="huge")).effort is None
    assert ai.make_ident("claude", ROOT, ai.Seen(surface="")).surface is None
    assert (
        ai.make_ident("claude", ROOT, ai.Seen(version="a b")).harness == "claude-code"
    )
    assert ai.make_ident("claude", ROOT, ai.Seen(model=5)).model is None
    assert ai.make_ident("claude", ROOT, ai.Seen(), "").subagent is None


def test_make_ident_agy_effort_only_from_table() -> None:
    known = ai.make_ident("antigravity", ROOT, ai.Seen(model="gemini-3.8-flash-low"))
    assert (known.model, known.effort) == ("gcp.gemini:gemini-3.8-flash", "low")
    explicit = ai.make_ident(
        "antigravity", ROOT, ai.Seen(model="gemini-3.8-flash-low", effort="high")
    )
    assert explicit.effort == "high"
    unknown = ai.make_ident(
        "antigravity", ROOT, ai.Seen(model="gemini-3.8-flash-tiered")
    )
    assert (unknown.model, unknown.effort) == ("gemini-3.8-flash-tiered", None)
    assert ai.make_ident("antigravity", ROOT, ai.Seen()).effort is None
    other = ai.make_ident("codex", ROOT, ai.Seen(model="gemini-3.8-flash-low"))
    assert other.effort is None


def test_ident_attrs_and_keys() -> None:
    who = ai.Ident(ROOT, "codex", CHILD, "openai:m", "low", "exec")
    assert who.attrs() == {
        "effort": "low",
        "harness": "codex",
        "model": "openai:m",
        "session": ROOT,
        "subagent": CHILD,
        "surface": "exec",
    }
    assert ai.Ident(ROOT, "codex").attrs() == {"harness": "codex", "session": ROOT}
    assert who.agent_key() == (ROOT, CHILD)
    receipt = make_receipt("p", who)
    assert ai.Ident.from_receipt(receipt) == who
    bare = ai.Ident.from_receipt({"root": ROOT, "harness": "codex"})
    assert bare == ai.Ident(ROOT, "codex")


def test_parse_stdin_fixtures() -> None:
    main = stdin_fixture("claude-pretooluse-bash-main.json")
    assert ai.parse_stdin(main) == ("session-raw", None, "high")
    codex = stdin_fixture("codex-sessionstart.json")
    assert ai.parse_stdin(codex) == (
        "01a19ebf-adec-7210-8ba8-550921c2b924",
        "gpt-5.6-sol",
        "high",
    )
    agy = stdin_fixture("agy-posttooluse-write.json")
    assert ai.parse_stdin(agy) == ("conv-raw", "gemini-3.8-flash-low", None)
    assert ai.parse_stdin({"sessionId": "s", "to_model": "m"}) == ("s", "m", None)
    assert ai.parse_stdin({"conversation_id": "c"}) == ("c", None, None)
    assert ai.parse_stdin({"effort": {"x": 1}}) == (None, None, None)
    assert ai.parse_stdin({"effort": {"level": "low"}})[2] == "low"
    assert ai.parse_stdin({}) == (None, None, None)


# ---------------------------------------------------------------- grammar

harnesses = st.sampled_from(sorted(ai.SLUGS.values())).flatmap(
    lambda slug: st.one_of(
        st.just(slug),
        st.from_regex(r"[0-9]\.[0-9]{1,2}", fullmatch=True).map(
            lambda ver: f"{slug}:{ver}"
        ),
    )
)
hashes = st.from_regex(r"sha-256-64:[0-9a-f]{16}", fullmatch=True)
attr_sets = st.fixed_dictionaries(
    {"harness": harnesses},
    optional={
        "effort": st.sampled_from(sorted(ai.EFFORTS)),
        "model": st.from_regex(r"[a-z0-9][a-z0-9.:_-]{0,20}", fullmatch=True),
        "session": hashes,
        "subagent": hashes,
        "surface": st.from_regex(r"[A-Za-z0-9_-]{1,10}", fullmatch=True),
    },
)


@given(attr_sets)
def test_grammar_round_trip(attrs: dict[str, str]) -> None:
    value = ai.format_value(attrs)
    assert value.startswith("LLM ")
    assert ai.check_value(value) is None
    pairs = [pair.split("=", 1) for pair in value.removeprefix("LLM ").split(";")]
    assert dict(pairs) == attrs
    keys = [key for key, _ in pairs]
    assert keys == [key for key in ai.ATTR_ORDER if key in attrs]


def test_format_order_and_omission() -> None:
    attrs = {
        "surface": "cli",
        "subagent": H + "b" * 16,
        "model": "anthropic:m",
        "harness": "claude-code:2.1.316",
        "effort": "high",
        "session": ROOT,
    }
    assert ai.format_value(attrs) == (
        "LLM effort=high;harness=claude-code:2.1.316;model=anthropic:m;"
        f"session={ROOT};subagent={H}{'b' * 16};surface=cli"
    )
    assert ai.format_value({"harness": "antigravity"}) == "LLM harness=antigravity"
    assert ai.format_value({"harness": "codex", "model": ""}) == "LLM harness=codex"
    assert ai.ATTR_ORDER == [
        "effort",
        "harness",
        "model",
        "session",
        "subagent",
        "surface",
    ]


@given(
    st.lists(st.from_regex(r"[a-z0-9.]{1,6}", fullmatch=True), min_size=1, max_size=6)
)
def test_command_candidates_recover_eval_operand(parts: list[str]) -> None:
    cmd = " && ".join(f"echo 'it'\"'\"'s {p}'" for p in parts)
    line = f"/bin/zsh -c source s && eval {shlex.quote(cmd)} < /dev/null && pwd"
    assert ai.command_candidates(line) == [cmd]


@given(st.text(max_size=40))
def test_cmd_hashes_cover_trimmed_form(cmd: str) -> None:
    got = ai.cmd_hashes(cmd)
    assert got == sorted(set(got))
    assert hashlib.sha256(cmd.encode()).hexdigest() in got
    assert hashlib.sha256(cmd.strip().encode()).hexdigest() in got
    assert len(got) == (1 if cmd == cmd.strip() else 2)


def test_command_candidates_from_real_shape() -> None:
    cmd = "cd x && echo 'it'\"'\"'s' && git commit -m \"a b\""
    quoted = cmd.replace("'", "'\"'\"'")
    line = (
        "/bin/zsh -c source /u/.claude/shell-snapshots/s.sh 2>/dev/null || true && "
        "setopt NO_EXTENDED_GLOB 2>/dev/null || true && "
        "{ \\builtin unalias -- 'which'; } >/dev/null 2>&1 || true && "
        f"eval '{quoted}' < /dev/null && pwd -P >| /tmp/claude-1a2b-cwd"
    )
    assert ai.command_candidates(line) == [cmd]
    assert ai.command_candidates("python3 x.py trailer m") == []
    assert ai.command_candidates("zsh -c 'unterminated") == []
    assert ai.command_candidates("eval") == []
    assert ai.command_candidates("a eval b eval c") == ["b", "c"]


# --------------------------------------------------------- git and the store


def test_git_reports_exit_code_and_trims_newlines(repo: Path, plain: Path) -> None:
    assert ai.git("rev-parse", "--show-toplevel", cwd=repo) == (0, str(repo))
    code, out = ai.git("rev-parse", "--git-dir", cwd=plain)
    assert code != 0
    assert out == ""
    code, out = ai.git("--version")
    assert code == 0
    assert out.startswith("git version")


def test_interim_dir_and_repo_info(
    repo: Path, plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    store = repo / ".git" / "ascm-interim"
    assert ai.interim_dir() == store
    assert ai.interim_dir(repo) == store
    assert ai.interim_dir(plain) is None
    assert ai.repo_info() == ai.Repo(repo, store)
    assert ai.repo_info(repo) == ai.Repo(repo, store)
    assert ai.repo_info(plain) is None


def test_worktrees_share_one_store(repo: Path, tmp_path: Path) -> None:
    stage(repo, "f")
    run_git(repo, "commit", "-q", "-m", "x")
    tree = tmp_path / "wt"
    run_git(repo, "worktree", "add", "-q", str(tree))
    info = ai.repo_info(tree)
    assert info == ai.Repo(tree.resolve(), repo / ".git" / "ascm-interim")
    assert ai.interim_dir(tree) == repo / ".git" / "ascm-interim"


def test_write_record_merges_without_clobbering(repo: Path) -> None:
    ai.write_record(ROOT, {"model": "m", "effort": None})
    ai.write_record(ROOT, {"effort": "low", "model": ""})
    assert ai.load_record(ROOT) == {"model": "m", "effort": "low"}
    path = repo / ".git" / "ascm-interim" / "sessions" / f"{'a' * 16}.json"
    assert loaded(path) == {"model": "m", "effort": "low"}


def test_records_honor_the_cwd_argument(repo: Path, plain: Path) -> None:
    # `plain` is the working directory; `repo` is only reachable by argument.
    ai.write_record(ROOT, {"model": "m"}, repo)
    assert ai.load_record(ROOT, repo) == {"model": "m"}
    assert ai.load_record(ROOT) == {}
    assert list(plain.iterdir()) == []


def test_records_outside_a_repository_are_noops(plain: Path) -> None:
    ai.write_record(ROOT, {"model": "m"})
    assert ai.load_record(ROOT) == {}
    assert list(plain.iterdir()) == []


# ------------------------------------------------------------------ Claude


def test_child_transcript_derived_and_authoritative() -> None:
    child = stdin_fixture("claude-pretooluse-bash-child.json")
    main = stdin_fixture("claude-pretooluse-bash-main.json")
    assert ai.child_transcript(child) == (
        "/nonexistent/session-raw/subagents/agent-agent-raw.jsonl"
    )
    assert ai.child_transcript({"agent_transcript_path": "/z"}) == "/z"
    assert ai.child_transcript({**child, "agent_transcript_path": "/z"}) == "/z"
    assert ai.child_transcript(main) is None
    assert ai.child_transcript({"agent_id": "a"}) is None
    assert ai.child_transcript({"transcript_path": "/t.jsonl"}) is None


def test_rewrite_is_hash_only_and_optional() -> None:
    tool_input: Json = {"command": "cd x", "description": "d"}
    assert ai.rewrite_output(CHILD, tool_input) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {
                "command": f"export ASCM_AGENT_ID={CHILD}; cd x",
                "description": "d",
            },
        }
    }
    assert ai.rewrite_output(CHILD, {}) is None
    assert ai.rewrite_output(CHILD, {"command": 3}) is None
    assert tool_input["command"] == "cd x"


def test_cwd_of() -> None:
    assert ai.cwd_of({"cwd": "/a"}) == Path("/a")
    assert ai.cwd_of({"cwd": ""}) is None
    assert ai.cwd_of({"cwd": 3}) is None
    assert ai.cwd_of({}) is None


def write_transcript(path: Path, *records: Json) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_read_tail_and_parse_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert ai.read_tail(path, 1000) == ["one", "two", "three"]
    assert ai.read_tail(tmp_path / "missing", 10) is None
    path.write_bytes(b"\xff\xfe ok\n")
    assert ai.read_tail(path, 100) == ["\ufffd\ufffd ok"]
    assert ai.parse_line('{"a": 1}') == {"a": 1}
    assert ai.parse_line("[1]") is None
    assert ai.parse_line("nope") is None
    assert ai.parse_line("") is None


def test_read_tail_keeps_only_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("aaaa\nbbbb\ncccc\n", encoding="utf-8")
    assert ai.read_tail(path, 5) == ["cccc"]
    assert ai.read_tail(path, 6) == ["", "cccc"]
    assert ai.read_tail(path, 7) == ["b", "cccc"]
    assert ai.read_tail(path, 14) == ["aaa", "bbbb", "cccc"]
    assert ai.read_tail(path, 15) == ["aaaa", "bbbb", "cccc"]
    assert ai.read_tail(path, 16) == ["aaaa", "bbbb", "cccc"]


def test_transcript_model_reads_the_last_real_model(tmp_path: Path) -> None:
    path = write_transcript(
        tmp_path / "t.jsonl",
        {"type": "user", "message": {"content": "x"}},
        {"type": "assistant", "message": {"model": "claude-sonnet-5-5"}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},
        {"type": "assistant", "message": {"model": ""}},
        {"type": "assistant", "message": "not an object"},
        {"type": "system"},
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    assert ai.transcript_model(str(path)) == "claude-sonnet-5-5"
    assert ai.transcript_model(str(tmp_path / "missing")) is None
    assert ai.transcript_model(None) is None
    assert ai.transcript_model(3) is None
    only = write_transcript(tmp_path / "o.jsonl", {"message": {"model": "<x>"}})
    assert ai.transcript_model(str(only)) is None


def test_transcript_model_only_reads_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_transcript(
        tmp_path / "t.jsonl",
        {"message": {"model": "old"}},
        {"message": {"model": "new"}},
    )
    monkeypatch.setattr(ai, "CLAUDE_TAIL", 40)
    assert ai.transcript_model(str(path)) == "new"
    path.write_text(
        json.dumps({"message": {"model": "old"}}) + "\n" + "x" * 100 + "\n",
        encoding="utf-8",
    )
    assert ai.transcript_model(str(path)) is None


def test_record_child_stores_hashes_only(repo: Path) -> None:
    transcript = write_transcript(
        repo / "session-raw.jsonl", {"message": {"model": "claude-opus-5-5"}}
    )
    child = write_transcript(
        repo / "session-raw" / "subagents" / "agent-agent-raw.jsonl",
        {"message": {"model": "claude-haiku-5-5"}},
    )
    data = stdin_fixture(
        "claude-pretooluse-bash-child.json",
        transcript_path=str(transcript),
        cwd=str(repo),
    )
    agent = ai.record_child(data, "high")
    assert agent == ai.sha256_64("agent-raw")
    record = ai.load_record(agent)
    assert record == {
        "harness": "claude-code",
        "root": ai.sha256_64("session-raw"),
        "agent_type": "beta",
        "model": "claude-haiku-5-5",
        "effort": "high",
        "transcript": str(child),
    }
    # File names and ids are hashes. (The transcript path is a filesystem path
    # the harness chose, and kept as is.)
    names = [p.name for p in (repo / ".git" / "ascm-interim").rglob("*.json")]
    assert names == [f"{ai.hx(agent)}.json"]


def test_command_matching_and_ambiguity(repo: Path) -> None:
    def line(cmd: str) -> str:
        return f"/bin/zsh -c source s && eval '{cmd}' < /dev/null && pwd -P >| /t"

    ai.record_command(ROOT, CHILD, "git commit -m x", repo)
    ai.record_command(ROOT, None, "ls", repo)
    match = ai.match_command_agent
    assert match(ROOT, [line("git commit -m x")], repo) == (CHILD, False)
    assert match(ROOT, [line("ls")], repo) == (None, False)
    assert match(ROOT, [line("other")], repo) == (None, False)
    assert match(ROOT, [], repo) == (None, False)
    # The harness may trim the command it runs.
    assert match(ROOT, [line("git commit -m x  ")], repo) == (CHILD, False)
    ai.record_command(ROOT, CHILD, "padded  ", repo)
    assert match(ROOT, [line("padded")], repo) == (CHILD, False)
    # Identical commands from two agents are ambiguous.
    ai.record_command(ROOT, OTHER, "git commit -m x", repo)
    assert match(ROOT, [line("git commit -m x")], repo) == (None, True)
    # The main agent and a subagent running the same command are ambiguous too.
    ai.record_command(ROOT, None, "shared", repo)
    ai.record_command(ROOT, CHILD, "shared", repo)
    assert match(ROOT, [line("shared")], repo) == (None, True)
    # Finished records stop matching, so sequential repeats are fine.
    ai.finish_command(ROOT, "git commit -m x", OTHER, repo)
    assert match(ROOT, [line("git commit -m x")], repo) == (CHILD, False)
    # Other sessions never match.
    assert match(H + "9" * 16, [line("git commit -m x")], repo) == (None, False)


def test_match_command_agent_window_and_defaults(
    repo: Path, plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = "sh -c eval 'ls'"
    ai.record_command(ROOT, CHILD, "ls", repo)
    assert ai.match_command_agent(ROOT, [line], repo) == (CHILD, False)
    far = time_of(repo) + ai.CMD_WINDOW
    assert ai.match_command_agent(ROOT, [line], repo, now=far - 1) == (CHILD, False)
    assert ai.match_command_agent(ROOT, [line], repo, now=far + 1) == (None, False)
    monkeypatch.setattr(ai.time, "time", lambda: far + 1)
    assert ai.match_command_agent(ROOT, [line], repo) == (None, False)
    monkeypatch.chdir(plain)
    assert ai.match_command_agent(ROOT, [line]) == (None, False)


def time_of(repo: Path) -> float:
    (path,) = (repo / ".git" / "ascm-interim" / "cmds" / ("a" * 16)).glob("*.json")
    return float(cast("float", loaded(path)["ts"]))


def pending(repo: Path) -> list[Json]:
    cmds = repo / ".git" / "ascm-interim" / "cmds"
    return [loaded(p) for p in sorted(cmds.rglob("*.json"))]


def test_record_command_stores_hashes_not_text(repo: Path) -> None:
    ai.record_command(ROOT, CHILD, "secret --token abc", repo)
    (rec,) = pending(repo)
    assert rec["agent"] == CHILD
    assert rec["cmd"] == ai.cmd_hashes("secret --token abc")
    assert rec["done"] is None
    assert isinstance(rec["ts"], float)
    assert "secret" not in json.dumps(rec)


def test_record_command_ignores_nonstrings_and_missing_repos(
    repo: Path, plain: Path
) -> None:
    ai.record_command(ROOT, CHILD, None, repo)
    ai.record_command(ROOT, CHILD, 5, repo)
    ai.record_command(ROOT, CHILD, "ls", plain)
    assert pending(repo) == []
    assert list(plain.iterdir()) == []


def test_finish_command_marks_only_the_newest_match(
    repo: Path, plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([100.0, 200.0, 300.0, 400.0, 500.0, 600.0])
    monkeypatch.setattr(ai.time, "time", lambda: next(clock))
    ai.record_command(ROOT, CHILD, "ls", repo)  # ts 100
    ai.record_command(ROOT, CHILD, "ls", repo)  # ts 200
    ai.record_command(ROOT, OTHER, "ls", repo)  # ts 300
    ai.record_command(ROOT, CHILD, "pwd", repo)  # ts 400
    ai.finish_command(ROOT, "ls", CHILD, repo)  # done at 500
    by_ts = {rec["ts"]: rec["done"] for rec in pending(repo)}
    assert by_ts == {100.0: None, 200.0: 500.0, 300.0: None, 400.0: None}
    ai.finish_command(ROOT, "ls", CHILD, repo)  # now the older one, done at 600
    by_ts = {rec["ts"]: rec["done"] for rec in pending(repo)}
    assert by_ts == {100.0: 600.0, 200.0: 500.0, 300.0: None, 400.0: None}
    ai.finish_command(ROOT, "ls", CHILD, repo)  # nothing left: no change
    assert {rec["ts"]: rec["done"] for rec in pending(repo)} == by_ts
    ai.finish_command(ROOT, "ls", None, repo)  # the main agent has none
    ai.finish_command(ROOT, "nope", OTHER, repo)
    ai.finish_command(ROOT, 7, OTHER, repo)
    ai.finish_command(ROOT, "ls", OTHER, plain)
    assert {rec["ts"]: rec["done"] for rec in pending(repo)} == by_ts


def test_finish_command_matches_the_main_agent(repo: Path) -> None:
    ai.record_command(ROOT, None, "ls", repo)
    ai.finish_command(ROOT, "ls", None, repo)
    (rec,) = pending(repo)
    assert isinstance(rec["done"], float)


def test_command_records_skip_done_and_unreadable(repo: Path) -> None:
    ai.record_command(ROOT, CHILD, "ls", repo)
    ai.record_command(ROOT, CHILD, "pwd", repo)
    ai.finish_command(ROOT, "pwd", CHILD, repo)
    folder = repo / ".git" / "ascm-interim" / "cmds" / ("a" * 16)
    (folder / "junk.json").write_text("{", encoding="utf-8")
    (folder / "empty.json").write_text("{}", encoding="utf-8")
    store = repo / ".git" / "ascm-interim"
    got = [rec for _, rec in ai.command_records(store, ROOT)]
    assert [rec["cmd"] for rec in got] == [ai.cmd_hashes("ls")]
    assert list(ai.command_records(store, OTHER)) == []


def returning[T](value: T) -> Callable[..., T]:
    """A stand-in that ignores its arguments and returns ``value``."""

    def fake(*_args: object, **_kwargs: object) -> T:
        return value

    return fake


class FakeRun:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_ps_row_parses_parent_and_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str], **kwargs: object) -> FakeRun:
        calls.append(argv)
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        return FakeRun(0, "  42 /bin/zsh -c  eval x \n")

    monkeypatch.setattr(ai.subprocess, "run", fake)
    assert ai.ps_row("7") == ("42", "/bin/zsh -c  eval x")
    assert calls == [["ps", "-ww", "-o", "ppid=,command=", "-p", "7"]]


def test_ps_row_none_when_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai.subprocess, "run", returning(FakeRun(1, "1 x\n")))
    assert ai.ps_row("7") is None
    monkeypatch.setattr(ai.subprocess, "run", returning(FakeRun(0, "\n")))
    assert ai.ps_row("7") is None
    monkeypatch.setattr(ai.subprocess, "run", returning(FakeRun(1, "")))
    assert ai.ps_row("7") is None


def test_ps_row_on_real_processes() -> None:
    row = ai.ps_row(str(os.getpid()))
    assert row is not None
    assert row[0].isdigit()
    # pytest-xdist retitles its workers ("[pytest-xdist running] ..."), so
    # check the command against what ps itself reports rather than "python".
    listed = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(os.getpid())],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    assert row[1] == listed
    assert ai.ps_row("999999999") is None


def fake_ps(monkeypatch: pytest.MonkeyPatch, rows: dict[str, tuple[str, str]]) -> None:
    monkeypatch.setattr(ai, "ps_row", rows.get)


def test_ancestry_walks_up_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ps(
        monkeypatch,
        {
            "10": ("20", "/bin/zsh -c eval a"),
            "20": ("30", "node tool"),
            "30": ("40", "/usr/local/bin/claude --flag"),
            "40": ("1", "login"),
        },
    )
    assert ai.ancestry(10) == [
        "/bin/zsh -c eval a",
        "node tool",
        "/usr/local/bin/claude --flag",
    ]
    assert ai.ancestry(30) == ["/usr/local/bin/claude --flag"]


def test_ancestry_stops_at_init_and_gaps_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ps(
        monkeypatch,
        {
            "10": ("20", "a"),
            "20": ("1", "b"),
            "30": ("0", "c"),
            "40": ("", "d"),
            "50": ("60", "e"),
            "60": ("50", "f"),
            "70": ("999", "g"),
            "": ("999", "after-empty"),
            "0": ("999", "after-zero"),
            "1": ("999", "after-one"),
        },
    )
    assert ai.ancestry(10) == ["a", "b"]
    assert ai.ancestry(30) == ["c"]
    assert ai.ancestry(40) == ["d"]
    assert ai.ancestry(70) == ["g"]
    assert ai.ancestry(50, limit=3) == ["e", "f", "e"]
    assert ai.ancestry(50, limit=0) == []
    assert len(ai.ancestry(50)) == ai.ANCESTRY_LIMIT


def ps_command(pid: int) -> str:
    done = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_ancestry_defaults_to_this_process() -> None:
    # Compared with ps, not a "python" substring: xdist workers retitle themselves.
    chain = ai.ancestry()
    assert chain[:2] == [ps_command(os.getpid()), ps_command(os.getppid())]


# ------------------------------------------------------------- edit receipts


def test_rel_to_repo(repo: Path, plain: Path, tmp_path: Path) -> None:
    (repo / "sub").mkdir()
    got = ai.rel_to_repo(str(repo / "sub" / "f.txt"), repo)
    assert got is not None
    assert got[0] == "sub/f.txt"
    assert got[1] == ai.repo_info(repo)
    assert ai.rel_to_repo("sub/f.txt", repo) == got
    deep = ai.rel_to_repo(str(repo / "new" / "deeper" / "g.txt"), repo)
    assert deep is not None
    assert deep[0] == "new/deeper/g.txt"
    assert ai.rel_to_repo(str(plain / "x"), plain) is None
    assert ai.rel_to_repo(str(tmp_path / "outside.txt")) is None
    for bad in (None, "", 5):
        assert ai.rel_to_repo(bad, repo) is None


def test_rel_to_repo_defaults_to_cwd(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    found = ai.rel_to_repo("a/b.txt")
    assert found is not None
    assert found[0] == "a/b.txt"


def test_rel_to_repo_rejects_paths_above_the_top(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = ai.Repo(elsewhere, elsewhere / ".git" / "ascm-interim")
    monkeypatch.setattr(ai, "repo_info", lambda _cwd=None: fake)
    assert ai.rel_to_repo(str(repo / "f"), repo) is None
    inside = ai.rel_to_repo(str(elsewhere / "f"), repo)
    assert inside is not None
    assert inside[0] == "f"


def test_rel_to_repo_follows_symlinked_directories(repo: Path, tmp_path: Path) -> None:
    (repo / "real").mkdir()
    (tmp_path / "link").symlink_to(repo / "real")
    found = ai.rel_to_repo(str(tmp_path / "link" / "f.txt"), repo)
    assert found is not None
    assert found[0] == "real/f.txt"


def test_blob_of(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    expected = run_git(repo, "hash-object", "f.txt").strip()
    assert ai.blob_of(repo, "f.txt") == expected
    assert ai.blob_of(repo, "missing.txt") is None
    (repo / "d").mkdir()
    assert ai.blob_of(repo, "d") is None
    monkeypatch.setattr(ai, "git", returning((1, "x")))
    assert ai.blob_of(repo, "f.txt") is None


def test_write_edit_stores_a_receipt(repo: Path, plain: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / "f.txt").write_text("hello\n", encoding="utf-8")
    who = make_ident(CHILD, "anthropic:m", ROOT, "low", "cli")
    ai.write_edit(who, str(repo / "sub" / "f.txt"), repo)
    ai.write_edit(who, str(repo / "gone.txt"), repo)
    ai.write_edit(who, str(plain / "elsewhere.txt"), plain)
    ai.write_edit(who, None, repo)
    store = ai.Repo(repo, repo / ".git" / "ascm-interim")
    receipts = {str(r["path"]): r for _, r in ai.load_edits(store, ROOT)}
    assert sorted(receipts) == ["gone.txt", "sub/f.txt"]
    kept = receipts["sub/f.txt"]
    assert kept["blob"] == run_git(repo, "hash-object", "sub/f.txt").strip()
    assert {k: kept[k] for k in ("root", "subagent", "harness", "model")} == {
        "root": ROOT,
        "subagent": CHILD,
        "harness": "claude-code",
        "model": "anthropic:m",
    }
    assert (kept["effort"], kept["surface"], kept["consumed"]) == ("low", "cli", False)
    assert isinstance(kept["ts"], float)
    assert receipts["gone.txt"]["blob"] is None
    assert ai.Ident.from_receipt(kept) == who


def test_diff_and_walk_paths() -> None:
    text = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n+++ /dev/null\n"
    assert ai.diff_paths(text) == ["x.txt", "/dev/null", "x.txt"]
    assert ai.diff_paths("+++ plain.txt\n") == ["plain.txt"]
    assert ai.diff_paths("++++ nope\n+++ \n") == []
    assert ai.diff_paths("") == []
    assert ai.walk_paths(text) == ai.diff_paths(text)
    assert ai.walk_paths(3) == []
    assert ai.walk_paths(None) == []
    nested = [
        {"filePath": "/r/a.py", "patch": [{"path": "p"}, {"file_path": "q"}]},
        "+++ b/z",
        {"other": 5, "filePath": ""},
    ]
    assert ai.walk_paths(nested) == ["/r/a.py", "p", "q", "z"]
    assert ai.walk_paths({"path": "a", "filePath": "b"}) == ["b", "a"]


def test_edit_paths_from_bash() -> None:
    assert ai.edit_paths_from_bash({}) == []
    assert ai.edit_paths_from_bash(None) == []
    assert ai.edit_paths_from_bash("text") == []
    assert ai.edit_paths_from_bash({"bashEditDiff": None}) == []
    diff = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n"
    assert ai.edit_paths_from_bash({"bashEditDiff": diff}) == ["x.txt"]
    assert ai.edit_paths_from_bash({"bashEditDiff": [{"filePath": "/r/a.py"}]}) == [
        "/r/a.py"
    ]
    assert ai.edit_paths_from_bash(
        {"bashEditDiff": "+++ /dev/null\n+++ a\n+++ a\n"}
    ) == ["a"]
    assert ai.edit_paths_from_bash({"bashEditDiff": ""}) == []


def test_load_edits_skips_consumed_and_unreadable(repo: Path) -> None:
    store = ai.Repo(repo, repo / ".git" / "ascm-interim")
    who = make_ident()
    folder = store.interim / "edits" / ("a" * 16)
    ai.write_json(folder / "keep.json", make_receipt("keep", who))
    ai.write_json(folder / "done.json", {**make_receipt("done", who), "consumed": True})
    (folder / "junk.json").write_text("{", encoding="utf-8")
    (folder / "empty.json").write_text("{}", encoding="utf-8")
    (folder / "note.txt").write_text("{}", encoding="utf-8")
    got = ai.load_edits(store, ROOT)
    assert [(p.name, r["path"]) for p, r in got] == [("keep.json", "keep")]
    assert ai.load_edits(store, OTHER) == []


# ------------------------------------------------------- Claude hook events

CLAUDE_HOOK_ENV = {
    "CLAUDE_CODE_EXECPATH": "/x/2.1.316",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
}
SESSION_HASH = ai.sha256_64("session-raw")
EVENT_FAIL = "PostToolUseFailure"
AGENT = ai.sha256_64("agent-raw")


def claude_data(
    repo: Path, name: str = "claude-pretooluse-bash-main.json", **kw: object
) -> Json:
    (repo / "session-raw.jsonl").write_text(
        json.dumps({"message": {"model": "claude-opus-5-5"}}) + "\n", encoding="utf-8"
    )
    return stdin_fixture(
        name, cwd=str(repo), transcript_path=str(repo / "session-raw.jsonl"), **kw
    )


def receipts_of(repo: Path, root: str) -> list[Json]:
    store = ai.Repo(repo, repo / ".git" / "ascm-interim")
    return sorted(
        (r for _, r in ai.load_edits(store, root)), key=lambda r: str(r["path"])
    )


def test_claude_ident_main_and_child(repo: Path) -> None:
    main = claude_data(repo)
    assert ai.claude_ident(main, CLAUDE_HOOK_ENV) == ai.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        None,
        "anthropic:claude-opus-5-5",
        "high",
        "cli",
    )
    write_transcript(
        repo / "session-raw" / "subagents" / "agent-agent-raw.jsonl",
        {"message": {"model": "claude-haiku-5-5"}},
    )
    child = claude_data(repo, "claude-pretooluse-bash-child.json")
    assert ai.claude_ident(child, CLAUDE_HOOK_ENV) == ai.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        AGENT,
        "anthropic:claude-haiku-5-5",
        "high",
        "cli",
    )


def test_claude_ident_falls_back_to_the_recorded_version(repo: Path) -> None:
    ai.write_record(SESSION_HASH, {"version": "9.9.9"}, repo)
    got = ai.claude_ident(claude_data(repo), {})
    assert got.harness == "claude-code:9.9.9"
    assert got.surface is None
    assert ai.claude_ident(claude_data(repo), CLAUDE_HOOK_ENV).harness == (
        "claude-code:2.1.316"
    )
    other = claude_data(repo, session_id="other")
    assert ai.claude_ident(other, {}).harness == "claude-code"


def test_tool_input_of() -> None:
    assert ai.tool_input_of({"tool_input": {"command": "x"}}) == {"command": "x"}
    assert ai.tool_input_of({"tool_input": "x"}) == {}
    assert ai.tool_input_of({}) == {}


def test_main_agent_events_record_the_session_and_commands(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ai.handle_claude(claude_data(repo), CLAUDE_HOOK_ENV)
    assert ai.load_record(SESSION_HASH) == {
        "harness": "claude-code",
        "version": "2.1.316",
        "model": "claude-opus-5-5",
        "effort": "high",
    }
    (rec,) = pending(repo)
    assert (rec["agent"], rec["done"]) == (None, None)
    assert rec["cmd"] == ai.cmd_hashes("cd x && git commit -m m")
    ai.handle_claude(claude_data(repo, hook_event_name=EVENT_FAIL), CLAUDE_HOOK_ENV)
    assert pending(repo) == [{**rec, "done": pending(repo)[0]["done"]}]
    assert isinstance(pending(repo)[0]["done"], float)
    assert receipts_of(repo, SESSION_HASH) == []
    assert capsys.readouterr().out == ""


def test_subagent_events_record_the_child_and_its_commands(repo: Path) -> None:
    data = claude_data(repo, "claude-pretooluse-bash-child.json")
    ai.handle_claude(data, CLAUDE_HOOK_ENV)
    assert ai.load_record(AGENT)["root"] == SESSION_HASH
    assert ai.load_record(SESSION_HASH) == {}
    (rec,) = pending(repo)
    assert rec["agent"] == AGENT


def test_pretooluse_rewrite_is_opt_in(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    child = claude_data(repo, "claude-pretooluse-bash-child.json")
    ai.handle_claude(child, CLAUDE_HOOK_ENV)
    ai.handle_claude(child, {**CLAUDE_HOOK_ENV, "ASCM_AGENT_REWRITE": "0"})
    assert capsys.readouterr().out == ""
    ai.handle_claude(child, {**CLAUDE_HOOK_ENV, "ASCM_AGENT_REWRITE": "1"})
    out = json.loads(capsys.readouterr().out)
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert (
        updated["command"] == f"export ASCM_AGENT_ID={AGENT}; cd x && git commit -m m"
    )
    assert "agent-raw" not in json.dumps(out)
    # The main agent is never rewritten, nor is a command-less tool input.
    main = claude_data(repo)
    ai.handle_claude(main, {"ASCM_AGENT_REWRITE": "1"})
    no_command = {**child, "tool_input": {"description": "d"}}
    ai.handle_claude(no_command, {"ASCM_AGENT_REWRITE": "1"})
    assert capsys.readouterr().out == ""


def test_bash_edit_diffs_become_receipts(repo: Path) -> None:
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    response = {"bashEditDiff": "diff --git a/x.txt b/x.txt\n+++ b/x.txt\n"}
    post = claude_data(repo, hook_event_name="PostToolUse", tool_response=response)
    ai.handle_claude(post, CLAUDE_HOOK_ENV)
    (got,) = receipts_of(repo, SESSION_HASH)
    assert (got["path"], got["subagent"], got["harness"]) == (
        "x.txt",
        None,
        "claude-code:2.1.316",
    )
    # Failures and pre events never write receipts; a response without paths neither.
    for event in ("PreToolUse", "PostToolUseFailure"):
        ai.handle_claude({**post, "hook_event_name": event}, CLAUDE_HOOK_ENV)
    ai.handle_claude({**post, "tool_response": {"x": 1}}, CLAUDE_HOOK_ENV)
    assert len(receipts_of(repo, SESSION_HASH)) == 1


def test_edit_tools_become_receipts(repo: Path) -> None:
    for name in ("a.txt", "b.ipynb", "c.txt"):
        (repo / name).write_text("x\n", encoding="utf-8")
    write = stdin_fixture("claude-posttooluse-write.json")
    base = {
        **write,
        "cwd": str(repo),
        "transcript_path": str(repo / "session-raw.jsonl"),
    }
    (repo / "session-raw.jsonl").write_text("", encoding="utf-8")
    ai.handle_claude({**base, "tool_input": {"file_path": "a.txt"}}, CLAUDE_HOOK_ENV)
    ai.handle_claude(
        {
            **base,
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "b.ipynb"},
        },
        CLAUDE_HOOK_ENV,
    )
    ai.handle_claude(
        {**base, "tool_name": "MultiEdit", "tool_input": {"file_path": "c.txt"}}, {}
    )
    # Not edits: wrong tool, wrong event, no path.
    ai.handle_claude(
        {**base, "tool_name": "Read", "tool_input": {"file_path": "r"}}, {}
    )
    ai.handle_claude(
        {**base, "hook_event_name": "PreToolUse", "tool_input": {"file_path": "p"}}, {}
    )
    ai.handle_claude({**base, "tool_input": {}}, {})
    ai.handle_claude({**base, "hook_event_name": "Stop", "tool_name": "Bash"}, {})
    assert [r["path"] for r in receipts_of(repo, SESSION_HASH)] == [
        "a.txt",
        "b.ipynb",
        "c.txt",
    ]
    first = receipts_of(repo, SESSION_HASH)[0]
    assert first["harness"] == "claude-code:2.1.316"
    assert first["surface"] == "cli"


# ------------------------------------------------------------ record entry


def test_cmd_record_claude_dispatch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ai.cmd_record("nonsense", {}) == 0
    feed(monkeypatch, claude_data(repo))
    assert ai.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    assert ai.load_record(SESSION_HASH)["harness"] == "claude-code"
    assert len(pending(repo)) == 1
    # Without a session ID nothing is recorded.
    feed(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert ai.cmd_record("claude", {}) == 0
    assert len(pending(repo)) == 1
    # session_id spellings are normalized.
    data = claude_data(repo)
    del data["session_id"]
    feed(monkeypatch, {**data, "sessionId": "session-raw"})
    assert ai.cmd_record("claude", CLAUDE_HOOK_ENV) == 0
    assert len(pending(repo)) == 2


def test_read_hook_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    feed(monkeypatch, '{"a": 1}')
    assert ai.read_hook_stdin() == {"a": 1}
    feed(monkeypatch, "")
    assert ai.read_hook_stdin() == {}
    feed(monkeypatch, "[1]")
    assert ai.read_hook_stdin() == {}
    feed(monkeypatch, "{")
    assert ai.read_hook_stdin() == {}


# ------------------------------------------------------------------- Codex

ROOT_THREAD = "01a19ebf-adec-7210-8ba8-550921c2b924"
CHILD_THREAD = "01a19ebf-e15a-7612-a93f-033e919906de"
OTHER_THREAD = "01a19ebf-ffff-7000-8000-0123456789ab"


def write_rollout(home: Path, thread: str, lines: list[object]) -> Path:
    folder = home / "sessions" / "2026" / "11" / "02"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-11-02T19-12-17-{thread}.jsonl"
    text = ""
    for line in lines:
        text += (line if isinstance(line, str) else json.dumps(line)) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def turn(model: str | None, effort: str | None) -> Json:
    return {"type": "turn_context", "payload": {"model": model, "effort": effort}}


def meta(**payload: object) -> Json:
    return {"type": "session_meta", "payload": payload}


def patch_paths_of(path: Path, offset: int = 0, skip: int = 0) -> list[str]:
    return ai.rollout_patch_edits(path, offset, skip)[0]


def patch_call(*paths: str) -> Json:
    body = "*** Begin Patch\n" + "".join(f"*** Add File: {p}\n+x\n" for p in paths)
    patch = json.dumps(body + "*** End Patch")
    code = f"const patch = {patch};\nawait tools.apply_patch(patch);"
    payload: Json = {"type": "custom_tool_call", "name": "exec", "input": code}
    return {"type": "response_item", "payload": payload}


def test_codex_home(tmp_path: Path) -> None:
    assert ai.codex_home({"CODEX_HOME": "/x/y"}) == Path("/x/y")
    assert ai.codex_home({}) == tmp_path / "home" / ".codex"
    assert ai.codex_home({"CODEX_HOME": ""}) == tmp_path / "home" / ".codex"


def test_codex_rollout_lookup(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {"CODEX_HOME": str(home)}
    assert ai.codex_rollout(CHILD_THREAD, env) is None
    first = write_rollout(home, CHILD_THREAD, [turn("gpt-5.6-sol", "low")])
    assert ai.codex_rollout(CHILD_THREAD, env) == first
    later = home / "sessions" / "2026" / "11" / "03"
    later.mkdir(parents=True)
    newer = later / f"rollout-2026-11-03T01-00-00-{CHILD_THREAD}.jsonl"
    newer.write_text("{}\n", encoding="utf-8")
    os.utime(first, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))
    assert ai.codex_rollout(CHILD_THREAD, env) == newer
    os.utime(first, (3_000, 3_000))
    assert ai.codex_rollout(CHILD_THREAD, env) == first
    for bad in ("../../x", "", "short", "x" * 20, "a/b" + "0" * 10, "0" * 65):
        assert ai.codex_rollout(bad, env) is None
    assert ai.codex_rollout("0" * 64, env) is None
    assert ai.codex_rollout("A" * 8, env) is None


def test_rollout_turn_context_takes_the_latest(tmp_path: Path) -> None:
    path = write_rollout(
        tmp_path,
        CHILD_THREAD,
        [
            meta(source={"subagent": {}}),
            turn("gpt-5.6-sol", "medium"),
            "not json",
            {"type": "other", "note": "turn_context", "payload": {"model": "x"}},
            {"type": "turn_context", "payload": "nope"},
            {"type": "turn_context", "payload": {"model": 3, "effort": ["x"]}},
            turn("gpt-5.6-luna", "low"),
            {"type": "event"},
            {"type": "other", "note": "turn_context"},
        ],
    )
    assert ai.rollout_turn_context(path) == ("gpt-5.6-luna", "low")
    path = write_rollout(
        tmp_path,
        CHILD_THREAD,
        [turn("a", "b"), {"type": "turn_context", "payload": {}}],
    )
    assert ai.rollout_turn_context(path) == (None, None)
    path = write_rollout(
        tmp_path, CHILD_THREAD, [turn("a", "b"), "[1]", '["turn_context"]']
    )
    assert ai.rollout_turn_context(path) == ("a", "b")
    path = write_rollout(
        tmp_path,
        CHILD_THREAD,
        [turn("a", "b"), {"type": "other", "note": "turn_context"}],
    )
    assert ai.rollout_turn_context(path) == ("a", "b")
    assert ai.rollout_turn_context(tmp_path / "missing") == (None, None)
    path = write_rollout(tmp_path, CHILD_THREAD, [meta()])
    assert ai.rollout_turn_context(path) == (None, None)


def test_rollout_turn_context_reads_one_megabyte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_rollout(tmp_path, CHILD_THREAD, [turn("old", "low"), "x" * 200])
    assert ai.rollout_turn_context(path) == ("old", "low")
    monkeypatch.setattr(ai, "CODEX_TAIL", 100)
    assert ai.rollout_turn_context(path) == (None, None)
    assert ai.CODEX_TAIL == 100
    monkeypatch.undo()
    assert ai.CODEX_TAIL == 1024 * 1024


def test_rollout_meta(tmp_path: Path) -> None:
    path = write_rollout(
        tmp_path, CHILD_THREAD, [meta(id="i", cwd="/c"), turn("m", "e")]
    )
    assert ai.rollout_meta(path) == {"id": "i", "cwd": "/c"}
    assert ai.rollout_meta(tmp_path / "missing") == {}
    for first in (
        turn("m", "e"),
        "not json",
        "[1]",
        {"type": "session_meta", "payload": 3},
        {"type": "session_meta"},
    ):
        assert ai.rollout_meta(write_rollout(tmp_path, CHILD_THREAD, [first])) == {}
    assert ai.rollout_meta(write_rollout(tmp_path, CHILD_THREAD, [])) == {}


RAW_PATCH = (
    "*** Begin Patch\n*** Add File: a.txt\n+x\n*** Update File: dir/b.py\n@@\n-1\n+2\n"
    "*** Delete File: c\n*** Update File: d\n*** Move to: e\n*** End Patch"
)


def test_patch_paths_formats() -> None:
    expected = ["a.txt", "dir/b.py", "c", "d", "e"]
    assert ai.patch_paths(RAW_PATCH) == expected
    js = (
        'const patch = "*** Begin Patch\\n*** Add File: codex.txt\\n+hi\\n'
        '*** End Patch";\nawait tools.apply_patch(patch);'
    )
    assert ai.patch_paths(js) == ["codex.txt"]
    assert ai.patch_paths(json.dumps({"input": RAW_PATCH})) == expected
    assert ai.patch_paths("no patch here *** Add File: x") == []
    assert ai.patch_paths(None) == []
    assert ai.patch_paths(5) == []
    assert ai.patch_paths("*** Begin Patch\n*** Add File: x\n*** Add File: x\n") == [
        "x"
    ]
    assert ai.patch_paths("*** Begin Patch\n*** Add File:  spaced  \n") == ["spaced"]
    assert ai.patch_paths("*** Begin Patch\r\n*** Add File: w\r\n") == ["w"]
    assert ai.patch_paths("*** Begin Patch\n*** Add File: tick`\n") == ["tick"]
    assert ai.patch_paths("*** Begin Patch\n*** Add File: at-end") == ["at-end"]
    assert ai.patch_paths("*** Begin Patch\n*** Move to: m'q") == ["m"]


@given(
    st.lists(
        st.from_regex(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,15}", fullmatch=True),
        min_size=1,
        max_size=5,
        unique=True,
    )
)
def test_patch_paths_round_trip(paths: list[str]) -> None:
    body = "*** Begin Patch\n" + "".join(f"*** Add File: {p}\n+x\n" for p in paths)
    assert ai.patch_paths(body + "*** End Patch") == paths
    assert ai.patch_paths(json.dumps({"input": body})) == paths


def test_tool_call_paths() -> None:
    def call(**payload: object) -> Json:
        return {"type": "response_item", "payload": payload}

    assert ai.tool_call_paths(patch_call("a.txt")) == ["a.txt"]
    assert (
        ai.tool_call_paths(call(type="custom_tool_call", input=RAW_PATCH))[0] == "a.txt"
    )
    assert (
        ai.tool_call_paths(call(type="function_call", arguments=RAW_PATCH))[0]
        == "a.txt"
    )
    assert (
        ai.tool_call_paths(call(type="function_call", input=3, arguments=RAW_PATCH))[0]
        == "a.txt"
    )
    action = {"command": ["apply_patch", RAW_PATCH]}
    assert (
        ai.tool_call_paths(call(type="local_shell_call", action=action))[0] == "a.txt"
    )
    assert ai.tool_call_paths(call(type="local_shell_call")) == []
    assert ai.tool_call_paths(call(type="function_call", input=3, arguments=4)) == []
    assert ai.tool_call_paths(call(type="message", input=RAW_PATCH)) == []
    assert ai.tool_call_paths(call(input=RAW_PATCH)) == []
    assert ai.tool_call_paths({"type": "response_item", "payload": "x"}) == []
    assert ai.tool_call_paths({"type": "response_item"}) == []
    assert (
        ai.tool_call_paths(
            {"type": "event", "payload": call(type="function_call")["payload"]}
        )
        == []
    )
    other: Json = {
        "type": "event_msg",
        "payload": {"type": "custom_tool_call", "input": RAW_PATCH},
    }
    assert ai.tool_call_paths(other) == []


def test_rollout_patch_edits_is_incremental(tmp_path: Path) -> None:
    msg = {
        "type": "response_item",
        "payload": {"type": "message", "content": RAW_PATCH},
    }
    path = write_rollout(
        tmp_path, CHILD_THREAD, [meta(), patch_call("a.txt"), msg, "Begin Patch junk"]
    )
    edits, offset = ai.rollout_patch_edits(path)
    assert edits == ["a.txt"]
    assert offset == path.stat().st_size
    assert ai.rollout_patch_edits(path, offset) == ([], offset)
    partial = json.dumps(patch_call("c.txt"))[:30]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(patch_call("b.txt")) + "\n" + partial)
    edits2, offset2 = ai.rollout_patch_edits(path, offset)
    assert edits2 == ["b.txt"]
    assert offset2 < path.stat().st_size
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert ai.rollout_patch_edits(path, offset2) == ([], path.stat().st_size)
    assert ai.rollout_patch_edits(tmp_path / "missing", 7) == ([], 7)
    assert ai.rollout_patch_edits(path, 10**6) == ([], 10**6)


def test_rollout_patch_edits_counts_every_path_in_order(tmp_path: Path) -> None:
    path = write_rollout(
        tmp_path, CHILD_THREAD, [patch_call("a", "b"), patch_call("c")]
    )
    assert patch_paths_of(path) == ["a", "b", "c"]


def test_codex_threads_select_the_root_sessions_rollouts(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {"CODEX_HOME": str(home)}
    root = write_rollout(
        home, ROOT_THREAD, [meta(id=ROOT_THREAD, session_id=ROOT_THREAD)]
    )
    child = write_rollout(
        home, CHILD_THREAD, [meta(id=CHILD_THREAD, session_id=ROOT_THREAD)]
    )
    write_rollout(home, "0" * 16, [meta(id="other", session_id="other")])
    stale = write_rollout(home, "1" * 16, [meta(id="old", session_id=ROOT_THREAD)])
    os.utime(stale, (1_000, 1_000))
    (home / "sessions" / "2026" / "11" / "02" / "rollout-broken.jsonl").symlink_to(
        tmp_path / "nowhere"
    )
    by_id = write_rollout(home, "2" * 16, [meta(id=ROOT_THREAD)])
    got = ai.codex_threads(ROOT_THREAD, env)
    assert sorted(p.name for p, _ in got) == sorted(
        p.name for p in (root, child, by_id)
    )
    assert all(isinstance(m, dict) for _, m in got)
    fresh = write_rollout(home, "3" * 16, [meta(id="edge", session_id=ROOT_THREAD)])
    os.utime(fresh, (time.time() - ai.CODEX_MAX_AGE + 60,) * 2)
    assert fresh in [p for p, _ in ai.codex_threads(ROOT_THREAD, env)]
    os.utime(fresh, (time.time() - ai.CODEX_MAX_AGE - 60,) * 2)
    assert fresh not in [p for p, _ in ai.codex_threads(ROOT_THREAD, env)]
    assert ai.codex_threads("unknown", env) == []


def test_codex_ident_and_scan(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {"CODEX_HOME": str(home), "CODEX_VERSION": "0.99.0"}
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, session_id=ROOT_THREAD, cwd=str(repo), source="exec"),
            turn("gpt-5.6-sol", "high"),
            patch_call("a.txt", "a.txt"),
        ],
    )
    write_rollout(
        home,
        CHILD_THREAD,
        [
            meta(
                id=CHILD_THREAD,
                session_id=ROOT_THREAD,
                cwd=str(repo),
                source={"subagent": 1},
            ),
            turn("gpt-5.6-luna", "low"),
            patch_call("b.txt"),
        ],
    )
    store = ai.repo_info(repo)
    assert store is not None
    ai.codex_scan(ROOT_THREAD, store, env)
    got = {str(r["path"]): r for r in receipts_of(repo, ai.sha256_64(ROOT_THREAD))}
    assert sorted(got) == ["a.txt", "b.txt"]
    assert got["a.txt"]["subagent"] is None
    assert got["a.txt"]["model"] == "openai:gpt-5.6-sol"
    assert got["a.txt"]["harness"] == "codex:0.99.0"
    assert got["a.txt"]["surface"] == "exec"
    assert got["b.txt"]["subagent"] == ai.sha256_64(CHILD_THREAD)
    assert got["b.txt"]["model"] == "openai:gpt-5.6-luna"
    assert got["b.txt"]["surface"] is None
    marks = sorted((store.interim / "marks").glob("*.json"))
    assert len(marks) == 2
    assert all(isinstance(loaded(m)["offset"], int) for m in marks)
    # A second scan has nothing new; consumed receipts stay consumed.
    for path, rec in ai.load_edits(store, ai.sha256_64(ROOT_THREAD)):
        ai.write_json(path, {**rec, "consumed": True})
    ai.codex_scan(ROOT_THREAD, store, env)
    assert receipts_of(repo, ai.sha256_64(ROOT_THREAD)) == []


def test_codex_scan_falls_back_to_repo_top_without_cwd(
    repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "codex"
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    write_rollout(home, ROOT_THREAD, [meta(id=ROOT_THREAD), patch_call("a.txt")])
    store = ai.repo_info(repo)
    assert store is not None
    ai.codex_scan(ROOT_THREAD, store, {"CODEX_HOME": str(home)})
    (got,) = receipts_of(repo, ai.sha256_64(ROOT_THREAD))
    assert got["path"] == "a.txt"
    assert got["blob"] is not None


def test_codex_ident_defaults(tmp_path: Path) -> None:
    env = {"CODEX_HOME": str(tmp_path)}
    got = ai.codex_ident(ROOT_THREAD, {}, None, env)
    assert got == ai.Ident(ai.sha256_64(ROOT_THREAD), "codex")
    meta_: Json = {"id": CHILD_THREAD, "cli_version": "1.2.3", "source": "cli"}
    got = ai.codex_ident(ROOT_THREAD, meta_, None, env)
    assert got.subagent == ai.sha256_64(CHILD_THREAD)
    assert (got.harness, got.surface) == ("codex:1.2.3", "cli")
    got = ai.codex_ident(ROOT_THREAD, {"id": ROOT_THREAD}, None, {"CODEX_VERSION": "7"})
    assert (got.subagent, got.harness) == (None, "codex:7")


# --------------------------------------------------------------------- agy

CONV = "conv-raw"
SUB = "sub-raw"
GRAND = "grand-raw"


def write_message(brain: Path, cid: str, name: str, body: object) -> None:
    folder = brain / cid / ".system_generated" / "messages"
    folder.mkdir(parents=True, exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body)
    (folder / name).write_text(text, encoding="utf-8")


def invoke(sender: str | None, recipient: str, name: str = "invoke_subagent") -> Json:
    tool: Json = {"toolCall": {"name": name}, "conversationId": "tool-sender"}
    msg: Json = {"recipient": recipient, "sourceMetadata": {"tool": tool}}
    if sender is not None:
        msg["sender"] = sender
    return msg


def test_agy_parent(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    assert ai.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "read.json", invoke(CONV, SUB))
    assert ai.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "a.txt", invoke(CONV, SUB))
    write_message(brain, SUB, "b.json", "not json")
    write_message(brain, SUB, "c.json", {"recipient": SUB})
    write_message(brain, SUB, "d.json", invoke(CONV, "someone-else"))
    write_message(brain, SUB, "e.json", invoke(CONV, SUB, "other_tool"))
    write_message(brain, SUB, "f.json", [1])
    assert ai.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "g.json", invoke(CONV, SUB))
    assert ai.agy_parent(brain, SUB) == CONV
    write_message(brain, SUB, "g.json", invoke(None, SUB))
    assert ai.agy_parent(brain, SUB) == "tool-sender"
    write_message(brain, SUB, "g.json", invoke(SUB, SUB))
    assert ai.agy_parent(brain, SUB) is None
    write_message(brain, SUB, "g.json", invoke("", SUB))
    assert ai.agy_parent(brain, SUB) == "tool-sender"


def test_agy_root_walks_up_safely(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    assert ai.agy_root(brain, CONV) == CONV
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    write_message(brain, GRAND, "m.json", invoke(SUB, GRAND))
    assert ai.agy_root(brain, SUB) == CONV
    assert ai.agy_root(brain, GRAND) == CONV
    assert ai.agy_root(brain, GRAND, max_depth=1) == SUB
    assert ai.agy_root(brain, GRAND, max_depth=0) == GRAND
    write_message(brain, CONV, "m.json", invoke(GRAND, CONV))  # a cycle
    assert ai.agy_root(brain, GRAND) in {CONV, SUB, GRAND}
    assert ai.agy_root(brain, SUB, max_depth=100) == GRAND


def test_agy_root_depth_default(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    names = [f"c{i}" for i in range(12)]
    for child, parent in itertools.pairwise(names):
        write_message(brain, child, "m.json", invoke(parent, child))
    assert ai.agy_root(brain, names[0]) == names[8]


def test_agy_brain_root(tmp_path: Path) -> None:
    recorded = tmp_path / "recorded"
    appdata = tmp_path / "appdata"
    (appdata / "brain").mkdir(parents=True)
    default = tmp_path / "home" / ".gemini" / "antigravity-cli" / "brain"
    assert ai.agy_brain_root({}, {}) is None
    default.mkdir(parents=True)
    assert ai.agy_brain_root({}, {}) == default
    env = {"ANTIGRAVITY_APP_DATA_DIR": str(appdata)}
    assert ai.agy_brain_root({}, env) == appdata / "brain"
    assert ai.agy_brain_root({"brain_root": str(recorded)}, env) == appdata / "brain"
    recorded.mkdir()
    assert ai.agy_brain_root({"brain_root": str(recorded)}, env) == recorded
    assert ai.agy_brain_root({"brain_root": 3}, {}) == default
    missing = {"ANTIGRAVITY_APP_DATA_DIR": str(tmp_path / "nope")}
    assert ai.agy_brain_root({}, missing) == default


def test_agy_tool_and_edit_path() -> None:
    assert ai.agy_tool({"toolCall": {"name": "w", "args": {"a": 1}}}) == ("w", {"a": 1})
    assert ai.agy_tool({"toolName": "w", "toolInput": "x"}) == ("w", "x")
    assert ai.agy_tool({"tool_name": "w"}) == ("w", None)
    assert ai.agy_tool({"toolCall": "x", "tool_name": "w"}) == ("w", None)
    assert ai.agy_tool({"toolCall": {"name": "n"}, "toolName": "x"}) == ("n", None)
    assert ai.agy_tool({}) == (None, None)
    assert ai.agy_edit_path({"Path": "a"}) == "a"
    assert ai.agy_edit_path(json.dumps({"TargetFile": "t"})) == "t"
    assert ai.agy_edit_path("{") is None
    assert ai.agy_edit_path("[1]") is None
    assert ai.agy_edit_path(None) is None
    assert ai.agy_edit_path({"Path": "", "path": "p"}) == "p"
    assert ai.agy_edit_path({"other": "x"}) is None
    for key in ai.AGY_PATH_KEYS:
        assert ai.agy_edit_path({key: "k"}) == "k"


def test_agy_ident(tmp_path: Path) -> None:
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    rec: Json = {"model": "gemini-3.8-flash-low"}
    got = ai.agy_ident(CONV, rec, env)
    assert got == ai.Ident(
        ai.sha256_64(CONV),
        "antigravity:1.4.12",
        None,
        "gcp.gemini:gemini-3.8-flash",
        "low",
        "cli",
    )
    assert ai.agy_ident(CONV, {"model": "m", "effort": "high"}, {}) == ai.Ident(
        ai.sha256_64(CONV), "antigravity", None, "m", "high", None
    )
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    child = ai.agy_ident(SUB, rec, env, brain)
    assert (child.root, child.subagent) == (ai.sha256_64(CONV), ai.sha256_64(SUB))
    top = ai.agy_ident(CONV, rec, env, brain)
    assert (top.root, top.subagent) == (ai.sha256_64(CONV), None)


def agy_post(**overrides: object) -> Json:
    return stdin_fixture("agy-posttooluse-write.json", **overrides)


def test_cmd_record_agy_edit(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    brain = repo.parent / "brain"
    brain.mkdir()
    data = agy_post(
        workspacePaths=[str(repo)], artifactDirectoryPath=f"{brain / CONV}/"
    )
    feed(monkeypatch, data)
    assert ai.cmd_record("antigravity", env) == 0
    rec = ai.load_record(ai.sha256_64(CONV))
    assert rec["harness"] == "antigravity"
    assert rec["model"] == "gemini-3.8-flash-low"
    assert rec["brain_root"] == str(brain)
    (got,) = receipts_of(repo, ai.sha256_64(CONV))
    assert (got["path"], got["effort"], got["surface"]) == ("hello.txt", "low", "cli")
    assert got["model"] == "gcp.gemini:gemini-3.8-flash"


def test_cmd_record_agy_ignores_non_edits(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    base = agy_post(workspacePaths=[str(repo)])
    cases = [
        {**base, "error": "boom"},
        {**base, "toolCall": {"name": "run_command", "args": {"Path": "hello.txt"}}},
        {**base, "toolCall": {"name": "write_file", "args": {"x": 1}}},
        {k: v for k, v in base.items() if k != "error"},
    ]
    for case in cases:
        feed(monkeypatch, case)
        assert ai.cmd_record("antigravity", {}) == 0
    assert receipts_of(repo, ai.sha256_64(CONV)) == []
    assert ai.load_record(ai.sha256_64(CONV))["harness"] == "antigravity"


def test_cmd_record_agy_without_workspace_uses_cwd(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    data = {k: v for k, v in agy_post().items() if k != "workspacePaths"}
    feed(monkeypatch, data)
    assert ai.cmd_record("antigravity", {}) == 0
    assert len(receipts_of(repo, ai.sha256_64(CONV))) == 1


def test_cmd_record_session_from_env_and_filters(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"model": "<synthetic>", "effort": {"level": "high"}})
    assert ai.cmd_record("antigravity", {"ANTIGRAVITY_CONVERSATION_ID": CONV}) == 0
    rec = ai.load_record(ai.sha256_64(CONV))
    assert rec == {"harness": "antigravity", "effort": "high"}
    feed(monkeypatch, {"model": "gpt-5.6-sol", "session_id": CONV})
    assert ai.cmd_record("antigravity", {}) == 0
    assert ai.load_record(ai.sha256_64(CONV)) == {
        "harness": "antigravity",
        "effort": "high",
        "model": "gpt-5.6-sol",
    }
    # No session anywhere, or no repository: nothing is written.
    feed(monkeypatch, {"model": "m"})
    assert ai.cmd_record("antigravity", {}) == 0
    assert len(list((repo / ".git" / "ascm-interim" / "sessions").glob("*"))) == 1


def test_cmd_record_outside_a_repository(
    plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert ai.cmd_record("antigravity", {}) == 0
    assert list(plain.iterdir()) == []


@pytest.mark.usefixtures("repo")
def test_cmd_record_brain_root_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    feed(monkeypatch, {"session_id": "s"})
    ai.cmd_record("antigravity", {})
    assert "brain_root" not in ai.load_record(ai.sha256_64("s"))
    feed(monkeypatch, {"session_id": "s", "artifactDirectoryPath": "/x/brain/s"})
    ai.cmd_record("antigravity", {})
    assert ai.load_record(ai.sha256_64("s"))["brain_root"] == "/x/brain"


# --------------------------------------------------------------- committers

CLAUDE_COMMIT_ENV = {
    "CLAUDE_CODE_SESSION_ID": "session-raw",
    "CLAUDE_CODE_EXECPATH": "/x/2.1.316",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_EFFORT": "low",
}


def test_detect_prefers_the_innermost_harness() -> None:
    env = {
        "CLAUDE_CODE_SESSION_ID": "c",
        "CODEX_SESSION_ID": "x",
        "ANTIGRAVITY_CONVERSATION_ID": "a",
    }
    assert ai.detect(env) == (ai.ANTIGRAVITY, "a")
    del env["ANTIGRAVITY_CONVERSATION_ID"]
    assert ai.detect(env) == (ai.CODEX, "x")
    del env["CODEX_SESSION_ID"]
    assert ai.detect(env) == (ai.CLAUDE, "c")
    assert ai.detect({"CLAUDE_CODE_SESSION_ID": ""}) is None
    assert ai.detect({}) is None


def test_committer_identity_outside_harness() -> None:
    assert ai.committer_identity({}) is None


def seed_claude(repo: Path) -> None:
    ai.write_record(
        SESSION_HASH,
        {"version": "2.1.316", "model": "claude-opus-5-5", "effort": "high"},
        repo,
    )


def test_claude_committer_main_agent(repo: Path) -> None:
    seed_claude(repo)
    got = ai.committer_identity(CLAUDE_COMMIT_ENV, ["unrelated"])
    assert got is not None
    assert got.key == ai.CLAUDE
    assert got.ident == ai.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        None,
        "anthropic:claude-opus-5-5",
        "low",
        "cli",
    )
    assert got.root_ident == ai.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        None,
        "anthropic:claude-opus-5-5",
        "high",
        "cli",
    )
    no_effort = {k: v for k, v in CLAUDE_COMMIT_ENV.items() if k != "CLAUDE_EFFORT"}
    again = ai.committer_identity(no_effort, [])
    assert again is not None
    assert again.ident.effort == "high"


def test_claude_committer_uses_real_ancestry_by_default(repo: Path) -> None:
    seed_claude(repo)
    got = ai.committer_identity(CLAUDE_COMMIT_ENV)
    assert got is not None
    assert got.ident.subagent is None


def test_claude_committer_child_matched_by_ancestry(repo: Path) -> None:
    seed_claude(repo)
    ai.write_record(
        AGENT,
        {"root": SESSION_HASH, "model": "claude-haiku-5-5", "effort": "medium"},
        repo,
    )
    ai.record_command(SESSION_HASH, AGENT, "git commit -m x", repo)
    chain = ["/bin/zsh -c eval 'git commit -m x'"]
    got = ai.committer_identity(CLAUDE_COMMIT_ENV, chain)
    assert got is not None
    assert got.ident == ai.Ident(
        SESSION_HASH,
        "claude-code:2.1.316",
        AGENT,
        "anthropic:claude-haiku-5-5",
        "medium",
        "cli",
    )
    assert got.root_ident.subagent is None
    assert got.root_ident.model == "anthropic:claude-opus-5-5"


def test_claude_committer_child_model_from_transcript(repo: Path) -> None:
    seed_claude(repo)
    transcript = repo / "child.jsonl"
    transcript.write_text(
        json.dumps({"message": {"model": "claude-haiku-5-5"}}) + "\n", encoding="utf-8"
    )
    ai.write_record(AGENT, {"root": SESSION_HASH, "transcript": str(transcript)}, repo)
    env = {**CLAUDE_COMMIT_ENV, ai.AGENT_ENV: AGENT}
    got = ai.committer_identity(env, [])
    assert got is not None
    assert got.ident.subagent == AGENT
    assert got.ident.model == "anthropic:claude-haiku-5-5"


def test_claude_committer_rejects_foreign_agent_records(repo: Path) -> None:
    seed_claude(repo)
    ai.write_record(AGENT, {"root": OTHER, "model": "claude-haiku-5-5"}, repo)
    got = ai.committer_identity({**CLAUDE_COMMIT_ENV, ai.AGENT_ENV: AGENT}, [])
    assert got is not None
    assert got.ident.subagent is None
    assert got.ident.model == "anthropic:claude-opus-5-5"
    # An unknown agent id is treated the same way; a malformed one is ignored.
    for bad in (CHILD, "not-a-hash", ""):
        got = ai.committer_identity({**CLAUDE_COMMIT_ENV, ai.AGENT_ENV: bad}, [])
        assert got is not None
        assert got.ident.subagent is None


def test_claude_committer_version_falls_back_to_the_record(repo: Path) -> None:
    seed_claude(repo)
    got = ai.committer_identity({"CLAUDE_CODE_SESSION_ID": "session-raw"}, [])
    assert got is not None
    assert got.ident.harness == "claude-code:2.1.316"
    assert got.ident.surface is None


def test_codex_committer(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": CHILD_THREAD,
        "CODEX_VERSION": "0.99.0",
    }
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, source="exec"), turn("gpt-5.6-sol", "high")],
    )
    write_rollout(
        home,
        CHILD_THREAD,
        [meta(id=CHILD_THREAD, session_id=ROOT_THREAD), turn("gpt-5.6-luna", "low")],
    )
    got = ai.committer_identity(env)
    assert got is not None
    assert got.key == ai.CODEX
    assert got.ident == ai.Ident(
        ai.sha256_64(ROOT_THREAD),
        "codex:0.99.0",
        ai.sha256_64(CHILD_THREAD),
        "openai:gpt-5.6-luna",
        "low",
        None,
    )
    assert got.root_ident == ai.Ident(
        ai.sha256_64(ROOT_THREAD),
        "codex:0.99.0",
        None,
        "openai:gpt-5.6-sol",
        "high",
        "exec",
    )
    solo = ai.committer_identity(
        {k: v for k, v in env.items() if k != "CODEX_THREAD_ID"}
    )
    assert solo is not None
    assert solo.ident == solo.root_ident == got.root_ident


def test_codex_committer_without_rollouts(tmp_path: Path) -> None:
    env = {
        "CODEX_HOME": str(tmp_path),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": "",
    }
    got = ai.committer_identity(env)
    assert got is not None
    assert got.ident == ai.Ident(ai.sha256_64(ROOT_THREAD), "codex")


def test_agy_committer(repo: Path, tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    ai.write_record(
        ai.sha256_64(CONV),
        {"brain_root": str(brain), "model": "gemini-3.8-flash-high"},
        repo,
    )
    ai.write_record(
        ai.sha256_64(SUB),
        {"brain_root": str(brain), "model": "claude-opus-5-5-low"},
        repo,
    )
    env = {"ANTIGRAVITY_CONVERSATION_ID": SUB, "ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    got = ai.committer_identity(env)
    assert got is not None
    assert got.key == ai.ANTIGRAVITY
    assert got.ident == ai.Ident(
        ai.sha256_64(CONV),
        "antigravity:1.4.12",
        ai.sha256_64(SUB),
        "anthropic:claude-opus-5-5",
        "low",
        "cli",
    )
    assert got.root_ident == ai.Ident(
        ai.sha256_64(CONV),
        "antigravity:1.4.12",
        None,
        "gcp.gemini:gemini-3.8-flash",
        "high",
        "cli",
    )
    top = ai.committer_identity({**env, "ANTIGRAVITY_CONVERSATION_ID": CONV})
    assert top is not None
    assert top.ident is top.root_ident


@pytest.mark.usefixtures("repo")
def test_agy_committer_without_a_brain() -> None:
    env = {"ANTIGRAVITY_CONVERSATION_ID": CONV}
    got = ai.committer_identity(env)
    assert got is not None
    assert got.ident == got.root_ident == ai.Ident(ai.sha256_64(CONV), "antigravity")


# ------------------------------------------------------------- role decision


def test_staged_info(repo: Path, plain: Path) -> None:
    assert ai.staged_info(repo) == ([], {})
    stage(repo, "a.txt")
    stage(repo, "dir/b c.txt")
    paths, blobs = ai.staged_info(repo)
    assert sorted(paths) == ["a.txt", "dir/b c.txt"]
    assert blobs["a.txt"] == run_git(repo, "hash-object", "a.txt").strip()
    assert set(blobs) == set(paths)
    assert ai.staged_info(plain) == ([], {})


def test_staged_info_skips_unmerged_entries(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = {
        "diff": (0, "conflict.txt\x00ok.txt\x00"),
        "ls-files": (
            0,
            "100644 abc 1\tconflict.txt\x00100644 def 0\tok.txt\x00bad entry\x00",
        ),
    }

    def fake(*args: str, **_kwargs: Path | None) -> tuple[int, str]:
        return outputs[args[0]]

    monkeypatch.setattr(ai, "git", fake)
    assert ai.staged_info(repo) == (["conflict.txt", "ok.txt"], {"ok.txt": "def"})


def test_receipt_rank() -> None:
    who = make_ident()
    staged = {"a": "b1"}
    hit = make_receipt("a", who, blob="b1", ts=1)
    newer_miss = make_receipt("a", who, blob="zz", ts=9)
    no_blob = make_receipt("a", who, blob=None, ts=5)
    assert ai.receipt_rank(hit, staged) == (0, -1)
    assert ai.receipt_rank(newer_miss, staged) == (1, -9)
    assert ai.receipt_rank(no_blob, staged) == (1, -5)
    assert ai.receipt_rank(make_receipt("a", who, blob=None), {}) == (1, -1)
    assert sorted(
        [newer_miss, no_blob, hit], key=lambda r: ai.receipt_rank(r, staged)
    ) == [hit, newer_miss, no_blob]


def test_decide_fallback_gives_the_committer_generated_by() -> None:
    who = make_ident()
    assert ai.decide(committer_of(who), [], {"a"}, {}) == ([who], [])
    other = make_receipt("elsewhere", make_ident(CHILD))
    assert ai.decide(committer_of(who), [other], {"a"}, {}) == ([who], [])


def test_decide_committer_who_wrote_is_not_assisted() -> None:
    who = make_ident()
    got = ai.decide(committer_of(who), [make_receipt("a", who)], {"a"}, {})
    assert got == ([who], [])


def test_decide_committer_who_wrote_nothing_assists() -> None:
    writer = make_ident(CHILD)
    boss = make_ident()
    got = ai.decide(committer_of(boss), [make_receipt("a", writer)], {"a"}, {})
    assert got == ([writer], [boss])


def test_decide_root_assists_when_a_child_commits() -> None:
    child_writer = make_ident(CHILD)
    other_child = make_ident(OTHER)
    root = make_ident()
    committer = committer_of(other_child, root)
    got = ai.decide(committer, [make_receipt("a", child_writer)], {"a"}, {})
    assert got == ([child_writer], [other_child, root])
    # The root already wrote: it is not repeated. The child committer still assists.
    rooted = [make_receipt("a", root), make_receipt("b", child_writer)]
    got = ai.decide(committer, rooted, {"a", "b"}, {})
    assert got[0] == [root, child_writer] or got[0] == [child_writer, root]
    assert got[1] == [other_child]
    # A root-level committer never adds the root twice.
    got = ai.decide(committer_of(root), [make_receipt("a", child_writer)], {"a"}, {})
    assert got == ([child_writer], [root])


def test_decide_orders_by_last_writer_then_age_and_dedupes() -> None:
    first, second = make_ident(CHILD), make_ident(OTHER)
    receipts = [
        make_receipt("a", first, blob="old", ts=1),
        make_receipt("a", second, blob="mine", ts=2),
        make_receipt("b", first, blob="x", ts=3),
    ]
    gen, _ = ai.decide(committer_of(make_ident()), receipts, {"a", "b"}, {"a": "mine"})
    assert gen == [second, first]
    # Only receipts whose path is staged count.
    gen, _ = ai.decide(committer_of(make_ident()), receipts, {"b"}, {})
    assert gen == [first]


def test_decide_different_model_same_agent_counts_as_written() -> None:
    a = make_ident(model="anthropic:one")
    b = make_ident(model="anthropic:two")
    gen, assisted = ai.decide(committer_of(b), [make_receipt("a", a)], {"a"}, {})
    assert gen == [a]
    assert assisted == []


# ------------------------------------------------------------------ enrich


def test_enrich_fills_harness_version_and_surface_from_the_committer() -> None:
    cm = committer_of(
        ai.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", None, "cli")
    )
    bare = ai.Ident(ROOT, "claude-code")
    assert ai.enrich(bare, cm) == ai.Ident(
        ROOT, "claude-code:2.1", None, None, None, "cli"
    )
    versioned = ai.Ident(ROOT, "claude-code:1.0", None, None, None, "ide")
    assert ai.enrich(versioned, cm) == versioned
    # Committer lacking either value fills nothing.
    plain_cm = committer_of(ai.Ident(ROOT, "claude-code"))
    assert ai.enrich(bare, plain_cm) == bare
    # A different harness never lends its values.
    codex_cm = ai.Committer(
        ai.CODEX,
        ai.Ident(ROOT, "codex:9", None, None, None, "exec"),
        ai.Ident(ROOT, "codex"),
    )
    assert ai.enrich(bare, codex_cm) == bare


def test_enrich_child_model_and_effort_from_its_record(repo: Path) -> None:
    ai.write_record(
        AGENT,
        {"root": SESSION_HASH, "model": "claude-haiku-5-5", "effort": "medium"},
        repo,
    )
    cm = committer_of(ai.Ident(SESSION_HASH, "claude-code:2.1"))
    child = ai.Ident(SESSION_HASH, "claude-code", AGENT)
    got = ai.enrich(child, cm)
    assert (got.model, got.effort, got.harness) == (
        "anthropic:claude-haiku-5-5",
        "medium",
        "claude-code:2.1",
    )
    # Existing values win over the record.
    full = ai.Ident(SESSION_HASH, "claude-code", AGENT, "anthropic:x", "low")
    assert ai.enrich(full, cm) == replace_harness(full, "claude-code:2.1")
    half = ai.Ident(SESSION_HASH, "claude-code", AGENT, "anthropic:x")
    got = ai.enrich(half, cm)
    assert (got.model, got.effort) == ("anthropic:x", "medium")
    half = ai.Ident(SESSION_HASH, "claude-code", AGENT, None, "low")
    got = ai.enrich(half, cm)
    assert (got.model, got.effort) == ("anthropic:claude-haiku-5-5", "low")


def replace_harness(ident: ai.Ident, harness: str) -> ai.Ident:
    return ai.Ident(
        ident.root, harness, ident.subagent, ident.model, ident.effort, ident.surface
    )


def test_enrich_child_ignores_records_it_cannot_trust(repo: Path) -> None:
    ai.write_record(
        AGENT, {"root": OTHER, "model": "claude-haiku-5-5", "effort": "medium"}, repo
    )
    cm = committer_of(ai.Ident(SESSION_HASH, "claude-code"))
    child = ai.Ident(SESSION_HASH, "claude-code", AGENT)
    assert ai.enrich(child, cm) == child
    ai.write_record(AGENT, {"root": SESSION_HASH, "model": "claude-haiku-5-5"}, repo)
    codex_cm = ai.Committer(
        ai.CODEX, ai.Ident(SESSION_HASH, "codex"), ai.Ident(SESSION_HASH, "codex")
    )
    assert ai.enrich(child, codex_cm) == child
    # A child of the main agent (no subagent) is never looked up.
    assert ai.enrich(ai.Ident(SESSION_HASH, "claude-code"), cm) == ai.Ident(
        SESSION_HASH, "claude-code"
    )


def test_enrich_child_reads_the_transcript(repo: Path) -> None:
    transcript = repo / "t.jsonl"
    transcript.write_text(
        json.dumps({"message": {"model": "claude-haiku-5-5"}}) + "\n", encoding="utf-8"
    )
    ai.write_record(AGENT, {"root": SESSION_HASH, "transcript": str(transcript)}, repo)
    cm = committer_of(ai.Ident(SESSION_HASH, "claude-code"))
    got = ai.enrich(ai.Ident(SESSION_HASH, "claude-code", AGENT), cm)
    assert got.model == "anthropic:claude-haiku-5-5"
    assert got.effort is None


# -------------------------------------------------------- trailers and hooks


def msg_file(tmp_path: Path, text: str = "subject\n\nbody\n") -> Path:
    path = tmp_path / "MSG"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.usefixtures("repo")
def test_add_trailers_formats_and_dedupes(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    gen = make_ident(CHILD, "anthropic:m", ROOT, "low", "cli")
    assisted = make_ident(model=None)
    ai.add_trailers(path, [gen, gen], [assisted])
    text = path.read_text(encoding="utf-8")
    assert text == (
        "subject\n\nbody\n\n"
        "Generated-by: LLM effort=low;harness=claude-code;model=anthropic:m;"
        f"session={ROOT};subagent={CHILD};surface=cli\n"
        f"Assisted-by: LLM harness=claude-code;session={ROOT}\n"
    )
    assert text.count("Generated-by:") == 1
    assert text.index("Generated-by:") < text.index("Assisted-by:")
    ai.add_trailers(path, [gen], [assisted])
    assert path.read_text(encoding="utf-8").count("Generated-by:") == 1


def test_add_trailers_leaves_the_file_when_git_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = msg_file(tmp_path)
    monkeypatch.setattr(ai, "git", returning((1, "garbage")))
    ai.add_trailers(path, [make_ident()], [])
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"


@pytest.mark.usefixtures("repo")
def test_add_trailers_keeps_a_human_signoff_in_place(tmp_path: Path) -> None:
    path = msg_file(tmp_path, "subject\n\nSigned-off-by: Human <h@example.invalid>\n")
    ai.add_trailers(path, [make_ident()], [])
    text = path.read_text(encoding="utf-8")
    assert text.index("Signed-off-by") < text.index("Generated-by")


@pytest.mark.usefixtures("repo")
def test_cmd_trailer_skips_merges_and_squashes(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    for source in ("merge", "squash"):
        assert ai.cmd_trailer(str(path), source, CLAUDE_COMMIT_ENV) == 0
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"
    assert ai.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    assert "Generated-by" in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("repo", "plain")
def test_cmd_trailer_does_nothing_outside_harness_or_repo(tmp_path: Path) -> None:
    path = msg_file(tmp_path)
    assert ai.cmd_trailer(str(path), None, {}) == 0
    assert ai.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0  # cwd is `plain`
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"


def test_cmd_trailer_scans_codex_rollouts(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_VERSION": "1",
    }
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, cwd=str(repo)),
            turn("gpt-5.6-sol", "high"),
            patch_call("a.txt"),
        ],
    )
    stage(repo, "a.txt")
    path = msg_file(tmp_path)
    assert ai.cmd_trailer(str(path), None, env) == 0
    session = ai.sha256_64(ROOT_THREAD)
    assert path.read_text(encoding="utf-8") == (
        "subject\n\nbody\n\n"
        "Generated-by: LLM effort=high;harness=codex:1;"
        f"model=openai:gpt-5.6-sol;session={session}\n"
    )
    assert len(list((repo / ".git" / "ascm-interim" / "marks").glob("*.json"))) == 1
    # Claude does not scan rollouts.
    other = msg_file(tmp_path)
    assert ai.cmd_trailer(str(other), None, CLAUDE_COMMIT_ENV) == 0


# ------------------------------------------------------------- amend


GEN_ROOT = ("Generated-by", f"LLM harness=claude-code;session={ROOT}")
GEN_SUB = ("Generated-by", f"LLM harness=claude-code;session={ROOT};subagent={CHILD}")
ASSIST_ROOT = ("Assisted-by", f"LLM harness=claude-code;session={ROOT}")
ASSIST_OTHER = ("Assisted-by", f"LLM harness=codex;session={OTHER}")


def test_llm_trailers_reads_only_llm_trailer_lines() -> None:
    text = (
        "s\n\nGenerated-by: robot\nNote: Generated-by: LLM x=y\n"
        f"{GEN_ROOT[0]}: {GEN_ROOT[1]}\nAssisted-by: LLMX a=b\n"
        f"{ASSIST_OTHER[0]}: {ASSIST_OTHER[1]}\n"
    )
    assert ai.llm_trailers(text) == [GEN_ROOT, ASSIST_OTHER]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"LLM harness=x;session={ROOT};subagent={CHILD}", (ROOT, CHILD)),
        (f"LLM session={ROOT}", (ROOT, None)),
        (f"LLM xsession={ROOT};model=a:subagent=b", (None, None)),
        ("LLM", (None, None)),
    ],
)
def test_trailer_agent(value: str, expected: tuple[str | None, str | None]) -> None:
    assert ai.trailer_agent(value) == expected


def test_merge_trailers_dedupes_orders_and_lets_generated_win() -> None:
    found = [ASSIST_ROOT, ASSIST_OTHER, GEN_SUB, GEN_ROOT, GEN_SUB, ASSIST_OTHER]
    assert ai.merge_trailers(found) == [GEN_SUB, GEN_ROOT, ASSIST_OTHER]


def test_add_trailers_rewrites_existing_ones_in_order(tmp_path: Path) -> None:
    path = msg_file(
        tmp_path,
        f"s\n\nbody\n\n{ASSIST_ROOT[0]}: {ASSIST_ROOT[1]}\nGenerated-by: robot\n",
    )
    ai.add_trailers(path, [make_ident(model=None)], [], [GEN_SUB])
    assert path.read_text(encoding="utf-8") == (
        "s\n\nbody\n\nGenerated-by: robot\n"
        f"{GEN_SUB[0]}: {GEN_SUB[1]}\n{GEN_ROOT[0]}: {GEN_ROOT[1]}\n"
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("git commit --amend -m x", True),
        ("/opt/bin/git -C d commit -q --am", True),
        ("git commit --ame --no-edit", True),
        ("git commit --amen", True),
        ("git commit -m x -- --amend", False),
        ("git commit -m x", False),
        ("git commit --a", False),
        ("zsh -c git commit --amend", False),
        ("git log --amend", False),
        ("", False),
    ],
)
def test_amending(line: str, expected: bool) -> None:
    assert ai.amending([line]) is expected


def test_amending_takes_the_nearest_git_commit() -> None:
    assert ai.amending(["python x", "git commit -m x", "git commit --amend"]) is False
    assert ai.amending(["python x", "git commit --amend", "git commit"]) is True
    assert ai.amending([]) is False


def test_amended_trailers_reads_head_only_when_amending(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo, f"s\n\n{GEN_SUB[0]}: {GEN_SUB[1]}\n")
    monkeypatch.setattr(ai, "ancestry", returning(["git commit -m x"]))
    assert ai.amended_trailers(repo) == []
    monkeypatch.setattr(ai, "ancestry", returning(["git commit --amend -m x"]))
    assert ai.amended_trailers(repo) == [GEN_SUB]


def test_cmd_trailer_amend_keeps_the_amended_commits_trailers(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``--amend -m`` dropped a subagent's consumed Generated-by."""
    session = ai.sha256_64("session-raw")
    sub = f"LLM harness=claude-code;session={session};subagent={CHILD}"
    stage(repo, "a.txt")
    commit(repo, f"s\n\nGenerated-by: {sub}\nAssisted-by: {ASSIST_OTHER[1]}\n")
    monkeypatch.setattr(ai, "ancestry", returning(["git commit --amend -m x"]))
    path = msg_file(tmp_path, "reworded\n")
    assert ai.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    # Message only: no new edit, so no committer fallback either.
    assert path.read_text(encoding="utf-8") == (
        f"reworded\n\nGenerated-by: {sub}\nAssisted-by: {ASSIST_OTHER[1]}\n"
    )
    # New content in the amend adds its writer and keeps the rest.
    stage(repo, "b.txt")
    put_receipt(
        repo, "b.json", make_receipt("b.txt", make_ident(root=session)), session
    )
    path = msg_file(tmp_path, "reworded\n")
    assert ai.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[2] == f"Generated-by: {sub}"
    assert lines[3] == (
        "Generated-by: LLM harness=claude-code:2.1.316;model=anthropic:m;"
        f"session={session};surface=cli"
    )
    assert lines[4:] == [f"Assisted-by: {ASSIST_OTHER[1]}"]


def test_cmd_trailer_human_amend_keeps_trailers_and_adds_none(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo, f"s\n\n{GEN_SUB[0]}: {GEN_SUB[1]}\n")
    path = msg_file(tmp_path, "human\n")
    monkeypatch.setattr(ai, "ancestry", returning(["git commit -m x"]))
    assert ai.cmd_trailer(str(path), "message", {}) == 0
    assert path.read_text(encoding="utf-8") == "human\n"
    monkeypatch.setattr(ai, "ancestry", returning(["git commit --amend -m x"]))
    assert ai.cmd_trailer(str(path), "message", {}) == 0
    assert path.read_text(encoding="utf-8") == f"human\n\n{GEN_SUB[0]}: {GEN_SUB[1]}\n"


def test_cmd_trailer_keeps_the_fallback_without_any_generated_by(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo, f"s\n\n{ASSIST_OTHER[0]}: {ASSIST_OTHER[1]}\n")
    monkeypatch.setattr(ai, "ancestry", returning(["git commit --amend -m x"]))
    path = msg_file(tmp_path, "x\n")
    assert ai.cmd_trailer(str(path), "message", CLAUDE_COMMIT_ENV) == 0
    keys = [k for k, _ in ai.llm_trailers(path.read_text(encoding="utf-8"))]
    assert keys == ["Generated-by", "Assisted-by"]


# ------------------------------------------------- claims and consume


def commit(repo: Path, message: str = "m") -> None:
    run_git(repo, "commit", "-q", "--no-verify", "-m", message)


def edits_dir(repo: Path, root: str = ROOT) -> Path:
    return repo / ".git" / "ascm-interim" / "edits" / ai.hx(root)


def put_receipt(repo: Path, name: str, receipt: Json, root: str = ROOT) -> Path:
    path = edits_dir(repo, root) / name
    ai.write_json(path, receipt)
    return path


def claims_of(repo: Path) -> dict[str, Json]:
    folder = repo / ".git" / "ascm-interim" / "claims"
    return {p.stem: loaded(p) for p in sorted(folder.glob("*.json"))}


def test_unstaged_blobs_covers_only_staged_files_that_differ(repo: Path) -> None:
    stage(repo, "same.txt")
    stage(repo, "changed.txt")
    stage(repo, "gone.txt")
    stage(repo, "other.txt")
    (repo / "changed.txt").write_text("later\n", encoding="utf-8")
    (repo / "other.txt").write_text("later\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    later = run_git(repo, "hash-object", "changed.txt").strip()
    staged = {"same.txt", "changed.txt", "gone.txt"}
    assert ai.unstaged_blobs(repo, staged) == {"changed.txt": later}


def test_committing_keeps_staged_paths_except_still_unstaged_edits() -> None:
    who = make_ident()
    edits = [
        (Path(name), make_receipt(path, who, blob))
        for name, path, blob in (
            ("staged", "a.txt", "b1"),
            ("unstaged", "a.txt", "b2"),
            ("no-blob", "a.txt", None),
            ("other-path", "c.txt", "b2"),
            ("clean-path", "d.txt", "b2"),
            ("not-staged", "e.txt", "b1"),
        )
    ]
    got = ai.committing(
        edits, {"a.txt", "c.txt", "d.txt"}, {"a.txt": "b2", "c.txt": "x"}
    )
    assert [path.name for path, _ in got] == [
        "staged",
        "no-blob",
        "other-path",
        "clean-path",
    ]


def test_write_claim_keys_the_receipts_by_the_index_tree(repo: Path) -> None:
    stage(repo, "a.txt")
    info = ai.repo_info()
    assert info is not None
    ai.write_claim(info, ROOT, [Path("/x/b.json"), Path("/y/a.json")])
    tree = run_git(repo, "write-tree").strip()
    assert claims_of(repo) == {tree: {"root": ROOT, "receipts": ["a.json", "b.json"]}}


def test_write_claim_writes_nothing_when_the_index_has_no_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = ai.repo_info()
    assert info is not None
    monkeypatch.setattr(ai, "git", returning((128, "")))
    ai.write_claim(info, ROOT, [Path("a.json")])
    assert not (repo / ".git" / "ascm-interim").exists()


def test_cmd_trailer_claims_what_it_credits(repo: Path, tmp_path: Path) -> None:
    stage(repo, "a.txt")
    stage(repo, "b.txt")
    blob = run_git(repo, "rev-parse", ":b.txt").strip()
    (repo / "b.txt").write_text("after staging\n", encoding="utf-8")
    later = run_git(repo, "hash-object", "b.txt").strip()
    session = ai.sha256_64("session-raw")
    child = make_ident(CHILD, root=session)
    put_receipt(
        repo, "a.json", make_receipt("a.txt", make_ident(root=session)), session
    )
    put_receipt(repo, "b1.json", make_receipt("b.txt", child, blob), session)
    put_receipt(repo, "b2.json", make_receipt("b.txt", child, later), session)
    put_receipt(repo, "c.json", make_receipt("c.txt", child), session)
    path = msg_file(tmp_path)
    assert ai.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    tree = run_git(repo, "write-tree").strip()
    assert claims_of(repo) == {
        tree: {"root": session, "receipts": ["a.json", "b1.json"]}
    }
    # The edit made after staging is not this commit's: no credit from it alone.
    ai.write_json(edits_dir(repo, session) / "b1.json", {})
    other = msg_file(tmp_path, "s\n")
    assert ai.cmd_trailer(str(other), None, CLAUDE_COMMIT_ENV) == 0
    assert f"subagent={CHILD}" not in other.read_text(encoding="utf-8")


def test_cmd_consume_consumes_exactly_the_claim_of_the_committed_tree(
    repo: Path,
) -> None:
    who = make_ident()
    claimed = put_receipt(repo, "claimed.json", make_receipt("a.txt", who))
    later = put_receipt(repo, "later.json", make_receipt("a.txt", who))
    elsewhere = put_receipt(repo, "x.json", make_receipt("a.txt", who), OTHER)
    stage(repo, "a.txt")
    info = ai.repo_info()
    assert info is not None
    ai.write_claim(info, ROOT, [claimed, edits_dir(repo) / "missing.json"])
    stale = repo / ".git" / "ascm-interim" / "claims" / "stale.json"
    ai.write_json(stale, {"root": ROOT, "receipts": ["later.json"]})
    commit(repo)
    assert ai.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (claimed, later, elsewhere)] == [
        True,
        False,
        False,
    ]
    assert not (edits_dir(repo) / "missing.json").exists()
    assert list(claims_of(repo)) == ["stale"]
    # Nothing to claim: a second post-commit changes nothing.
    assert ai.cmd_consume() == 0
    assert loaded(later)["consumed"] is False


def test_cmd_consume_without_a_claim_consumes_nothing(repo: Path) -> None:
    receipt = put_receipt(repo, "x.json", make_receipt("a.txt", make_ident()))
    stage(repo, "a.txt")
    commit(repo, f"s\n\nGenerated-by: LLM harness=claude-code;session={ROOT}\n")
    assert ai.cmd_consume() == 0
    assert loaded(receipt)["consumed"] is False


def test_cmd_consume_outside_a_repository(plain: Path) -> None:
    assert ai.cmd_consume() == 0
    assert list(plain.iterdir()) == []


def test_failed_attempt_then_retry_keeps_and_then_consumes_every_receipt(
    repo: Path, tmp_path: Path
) -> None:
    """Regression: a failed commit attempt must not consume or drop receipts."""
    session = ai.sha256_64("session-raw")
    root = put_receipt(
        repo, "root.json", make_receipt("a.txt", make_ident(root=session)), session
    )
    sub = put_receipt(
        repo,
        "sub.json",
        make_receipt("b.txt", make_ident(CHILD, root=session)),
        session,
    )
    stage(repo, "a.txt")
    stage(repo, "b.txt")
    first = msg_file(tmp_path)
    assert ai.cmd_trailer(str(first), None, CLAUDE_COMMIT_ENV) == 0
    # commit-msg rejects the attempt: no commit, so no post-commit.
    assert [loaded(p)["consumed"] for p in (root, sub)] == [False, False]
    retry = msg_file(tmp_path)
    assert ai.cmd_trailer(str(retry), None, CLAUDE_COMMIT_ENV) == 0
    assert retry.read_text(encoding="utf-8") == first.read_text(encoding="utf-8")
    assert retry.read_text(encoding="utf-8").count("Generated-by:") == 2
    commit(repo)
    assert ai.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (root, sub)] == [True, True]


def test_a_receipt_recorded_during_the_commit_is_left_for_the_next(
    repo: Path, tmp_path: Path
) -> None:
    """Regression: post-commit consumed edits recorded after the trailers ran."""
    session = ai.sha256_64("session-raw")
    child = make_ident(CHILD, root=session)
    first = put_receipt(repo, "1.json", make_receipt("b.txt", child), session)
    stage(repo, "b.txt")
    assert ai.cmd_trailer(str(msg_file(tmp_path)), None, CLAUDE_COMMIT_ENV) == 0
    late = put_receipt(repo, "2.json", make_receipt("b.txt", child), session)
    commit(repo)
    assert ai.cmd_consume() == 0
    assert [loaded(p)["consumed"] for p in (first, late)] == [True, False]
    stage(repo, "b.txt", "second edit\n")
    path = msg_file(tmp_path)
    assert ai.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    assert f"subagent={CHILD}" in path.read_text(encoding="utf-8")


# ------------------------------------------------------------------- check


GOOD = f"harness=claude-code:2.1;model=anthropic:m;session={ROOT}"


@pytest.mark.parametrize(
    "value",
    [
        "LLM harness=claude-code",
        (
            "LLM effort=max;harness=codex:1;model=openai:gpt-5;"
            f"session={ROOT};subagent={CHILD};surface=cli"
        ),
        f"LLM {GOOD}",
        "LLM harness=antigravity;surface=cli",
        "LLM harness=codex some prose",
    ],
)
def test_check_value_accepts(value: str) -> None:
    assert ai.check_value(value) is None


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("", "must start with literal LLM"),
        ("harness=codex", "must start with literal LLM"),
        ("llm harness=codex", "must start with literal LLM"),
        ("LLM", "need exactly one attribute blob"),
        ("LLM a=b c=d", "need exactly one attribute blob"),
        ("LLM harness=codex;", "bad attribute ''"),
        ("LLM harness=codex;Bad=1", "bad attribute 'Bad=1'"),
        ("LLM harness=codex;color=red", "unknown attribute color"),
        ("LLM effort=huge;harness=codex", "bad effort huge"),
        ("LLM harness=codex;session=nope", "bad session hash"),
        (f"LLM harness=codex;session={ROOT};subagent=x", "bad subagent hash"),
        ("LLM harness=cursor", "unknown harness cursor"),
        ("LLM effort=low", "harness required"),
        ("LLM model=a:b;harness=codex", "attributes out of order or duplicated"),
        ("LLM harness=codex;harness=codex", "attributes out of order or duplicated"),
    ],
)
def test_check_value_rejects(value: str, problem: str) -> None:
    assert ai.check_value(value) == problem


def test_check_pair() -> None:
    assert ai.check_pair("harness=codex") is None
    assert ai.check_pair("surface=a:b/c") is None
    assert ai.check_pair("harness=") == "bad attribute 'harness='"
    assert ai.check_pair("=x") == "bad attribute '=x'"
    assert ai.check_pair("a=b=c") is not None
    for effort in ai.EFFORTS:
        assert ai.check_pair(f"effort={effort}") is None
    assert ai.check_pair("subagent=sha-256-64:ABCDEF0123456789") == "bad subagent hash"
    assert ai.check_pair("session=sha-256-64:" + "a" * 17) == "bad session hash"
    assert ai.check_pair("harness=codex:1:2") is None


def test_check_message_orders_and_signoff() -> None:
    gen, assist = f"Generated-by: LLM {GOOD}", f"Assisted-by: LLM {GOOD}"
    assert ai.check_message(f"s\n\n{gen}\n{assist}\n") == []
    assert ai.check_message(f"s\n\n{assist}\n{gen}\n") == [
        f"{gen}: Generated-by must come before Assisted-by"
    ]
    assert ai.check_message(f"s\n\n{gen}\n{gen}\n{assist}\n{assist}\n") == []
    assert ai.check_message("s\n\nGenerated-by: nope\n") == [
        "Generated-by: nope: must start with literal LLM"
    ]
    # Sign-off and co-author trailers are not checked, for anyone.
    assert (
        ai.check_message(
            f"s\n\n{gen}\nSigned-off-by: H <h@x>\nCo-authored-by: C <c@x>\n"
        )
        == []
    )
    assert ai.check_message("Generated-by:LLM harness=codex\n") == []
    assert ai.check_message("  Generated-by: nope\n") == []
    assert ai.check_message("") == []


@given(
    st.lists(
        st.sampled_from(
            ["effort", "harness", "model", "session", "subagent", "surface"]
        ),
        unique=True,
        min_size=1,
    )
)
def test_check_value_order_property(keys: list[str]) -> None:
    samples = {
        "effort": "low",
        "harness": "codex",
        "model": "openai:m",
        "session": ROOT,
        "subagent": CHILD,
        "surface": "cli",
    }
    ordered = [k for k in ai.ATTR_ORDER if k in keys]
    value = "LLM " + ";".join(f"{k}={samples[k]}" for k in ordered)
    expected = None if "harness" in keys else "harness required"
    assert ai.check_value(value) == expected
    shuffled = "LLM " + ";".join(f"{k}={samples[k]}" for k in reversed(ordered))
    if len(ordered) > 1 and "harness" in keys:
        assert ai.check_value(shuffled) == "attributes out of order or duplicated"


def test_cmd_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = msg_file(tmp_path, f"s\n\nGenerated-by: LLM {GOOD}\n")
    assert ai.cmd_check(str(good)) == 0
    assert capsys.readouterr().err == ""
    bad = msg_file(tmp_path, "s\n\nGenerated-by: nope\nSigned-off-by: A <a@x>\n")
    assert ai.cmd_check(str(bad)) == 1
    assert capsys.readouterr().err == (
        "agent_identity: Generated-by: nope: must start with literal LLM\n"
    )
    signed = msg_file(
        tmp_path, f"s\n\nGenerated-by: LLM {GOOD}\nSigned-off-by: A <a@x>\n"
    )
    assert ai.cmd_check(str(signed)) == 0
    assert capsys.readouterr().err == ""


# -------------------------------------------------------------------- main


@pytest.mark.usefixtures("repo")
def test_main_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = msg_file(tmp_path)
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert ai.main(["record", "codex"], {}) == 0
    assert ai.main(["record", "antigravity", "extra"], {}) == 0
    assert ai.main(["trailer", str(path)], {}) == 0
    assert ai.main(["trailer", str(path), "message", "sha"], {}) == 0
    assert ai.main(["trailer", str(path), "merge"], CLAUDE_COMMIT_ENV) == 0
    env_source = {**CLAUDE_COMMIT_ENV, ai.SOURCE_ENV: "merge"}
    assert ai.main(["trailer", str(path)], env_source) == 0
    assert path.read_text(encoding="utf-8") == "subject\n\nbody\n"
    assert ai.main(["consume"], {}) == 0
    assert ai.main(["consume", "x"], {}) == 0
    assert ai.main(["check", str(path)], {}) == 0
    assert ai.main(["check", str(path), "x"], {}) == 0
    assert capsys.readouterr().err == ""
    for argv in (
        cast("list[str]", []),
        ["record"],
        ["trailer"],
        ["check"],
        ["bogus"],
        ["bogus", "x"],
    ):
        assert ai.main(argv, {}) == 2
        assert capsys.readouterr().err == ai.USAGE + "\n"
    assert (
        ai.USAGE == "usage: record <harness> | trailer <msgfile> [source] | consume | "
        "check <msgfile> | stamp-docs"
    )


def test_script_runs_as_main(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "nope"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=hook_env(),
    )
    assert (done.returncode, done.stdout, done.stderr) == (2, "", ai.USAGE + "\n")
    done = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "check",
            str(msg_file(tmp_path, "s\n\nGenerated-by: x\n")),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=hook_env(),
    )
    assert done.returncode == 1


def test_run_reports_a_crash_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> int:
        raise KeyError(RAW_ID)

    monkeypatch.setattr(ai, "cmd_consume", boom)
    assert ai.run(["consume"], {}) == 1
    line = boom.__code__.co_firstlineno + 1
    assert capsys.readouterr().err == (
        f"agent_identity consume: failed: KeyError in boom (line {line})\n"
    )
    monkeypatch.setattr(ai, "cmd_consume", returning(0))
    assert ai.run(["consume"], {}) == 0


RAW_ID = "raw-session-id-0000"


# ----------------------------------------------- end to end with real hooks

# The repository's live prek config, so the end-to-end tests exercise exactly
# the hooks this repository installs.
PREK_CONFIG = Path(__file__).parents[2] / ".pre-commit-config.yaml"
INSTALL = Path(__file__).parent / "install.sh"


needs_prek = pytest.mark.skipif(
    shutil.which("prek") is None and shutil.which("mise") is None,
    reason="prek (or mise to run it) is required",
)


def identity_hooks(config: Path) -> dict[str, object]:
    """Keep only the agent-identity hooks from CONFIG."""
    loaded = cast(
        "dict[str, object]", yaml.safe_load(config.read_text(encoding="utf-8"))
    )
    kept = []
    for entry in cast("list[dict[str, object]]", loaded["repos"]):
        mine = [
            h
            for h in cast("list[dict[str, str]]", entry["hooks"])
            if h["id"].startswith("agent-identity-")
        ]
        if mine:
            kept.append({**entry, "hooks": mine})
    return {**loaded, "repos": kept}


def test_identity_hooks_keeps_only_agent_identity_hooks(tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "default_install_hook_types": ["pre-commit"],
                "repos": [
                    {"repo": "builtin", "hooks": [{"id": "trailing-whitespace"}]},
                    {
                        "repo": "local",
                        "hooks": [
                            {"id": "lint"},
                            {"id": "agent-identity-trailer"},
                            {"id": "agent-identity-check"},
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    assert identity_hooks(config) == {
        "default_install_hook_types": ["pre-commit"],
        "repos": [
            {
                "repo": "local",
                "hooks": [
                    {"id": "agent-identity-trailer"},
                    {"id": "agent-identity-check"},
                ],
            }
        ],
    }


def test_live_config_runs_only_stamp_docs_under_prek() -> None:
    hooks = [
        h
        for entry in cast(
            "list[dict[str, object]]", identity_hooks(PREK_CONFIG)["repos"]
        )
        for h in cast("list[dict[str, object]]", entry["hooks"])
    ]
    assert [(h["id"], h["stages"]) for h in hooks] == [
        ("agent-identity-stamp-docs", ["pre-commit"])
    ]


@needs_prek
def test_install_writes_plain_identity_hooks(wired: Path) -> None:
    hooks = wired / ".git" / "hooks"
    for hook, command in (
        ("prepare-commit-msg", "trailer"),
        ("commit-msg", "check"),
        ("post-commit", "consume"),
    ):
        text = (hooks / hook).read_text(encoding="utf-8")
        assert "hook-impl" not in text
        assert f'tools/interim/agent_identity.py {command} "$@"\n' in text
        assert os.access(hooks / hook, os.X_OK)
        # The live config has no hooks for these stages: nothing to chain.
        assert not (hooks / f"{hook}.prek").exists()
    assert "prek" in (hooks / "pre-commit").read_text(encoding="utf-8")


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with the module and this repository's prek hooks installed."""
    path = init_repo(tmp_path / "wired")
    (path / "tools" / "interim").mkdir(parents=True)
    shutil.copy(SCRIPT, path / "tools" / "interim" / "agent_identity.py")
    (path / ".pre-commit-config.yaml").write_text(
        yaml.safe_dump(identity_hooks(PREK_CONFIG)), encoding="utf-8"
    )
    monkeypatch.chdir(path)
    subprocess.run(
        ["bash", str(INSTALL)], check=True, capture_output=True, env=hook_env()
    )
    run_git(path, "config", "user.name", "Human")
    run_git(path, "config", "user.email", "human@example.invalid")
    stage(path, "base.txt")
    run_git(path, "commit", "-q", "--no-verify", "-m", "init")
    return path


def hook_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(AGENT_VARS[1:])}
    # Mutmut's marker would make the child look up mutmut's config from the
    # temp repo; the child runs the module unmutated.
    env.pop("MUTANT_UNDER_TEST", None)
    env.update(extra or {})
    return env


def record(
    repo: Path, harness: str, data: Json, extra: dict[str, str] | None = None
) -> None:
    script = repo / "tools" / "interim" / "agent_identity.py"
    done = subprocess.run(
        [sys.executable, str(script), "record", harness],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        check=False,
        cwd=repo,
        env=hook_env(extra),
    )
    assert (done.returncode, done.stderr) == (0, "")


def git_commit(
    repo: Path,
    message: str,
    extra: dict[str, str] | None = None,
    *flags: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "commit", "-q", "-m", message, *flags],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd or repo,
        env=hook_env(extra),
    )


def last_message(repo: Path) -> str:
    return run_git(repo, "log", "-1", "--format=%B").rstrip("\n")


def trailers(repo: Path) -> list[str]:
    return [
        x
        for x in last_message(repo).splitlines()
        if x.startswith(("Generated-by", "Assisted-by"))
    ]


CLAUDE_E2E = {
    "CLAUDE_CODE_SESSION_ID": "session-raw",
    "CLAUDE_CODE_EXECPATH": "/x/2.1.316",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
}


def claude_write(
    repo: Path, name: str, content: str | None = None, **kw: object
) -> None:
    (repo / "t.jsonl").write_text(
        json.dumps({"message": {"model": "claude-opus-5-5"}}) + "\n", encoding="utf-8"
    )
    data = stdin_fixture(
        "claude-posttooluse-write.json",
        cwd=str(repo),
        transcript_path=str(repo / "t.jsonl"),
        tool_input={"file_path": name},
        **kw,
    )
    (repo / name).write_text(
        f"{name}\n" if content is None else content, encoding="utf-8"
    )
    record(repo, "claude", data, CLAUDE_E2E)


@needs_prek
def test_e2e_human_commit_is_untouched(wired: Path) -> None:
    stage(wired, "h.txt")
    done = git_commit(wired, "human work", None, "-s")
    assert done.returncode == 0, done.stderr
    assert "Signed-off-by: Test <test@example.invalid>" in last_message(wired)
    assert trailers(wired) == []


@needs_prek
def test_e2e_claude_edit_then_commit(wired: Path) -> None:
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: add a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    session = ai.sha256_64("session-raw")
    (line,) = trailers(wired)
    assert (
        line == "Generated-by: LLM harness=claude-code:2.1.316;"
        f"model=anthropic:claude-opus-5-5;session={session};surface=cli"
    )
    receipts = list((wired / ".git" / "ascm-interim" / "edits").rglob("*.json"))
    assert [loaded(p)["consumed"] for p in receipts] == [True]
    # Consumed: a later commit of another file falls back to the committer.
    stage(wired, "b.txt")
    assert git_commit(wired, "chore: b", CLAUDE_E2E).returncode == 0
    assert len(trailers(wired)) == 1
    assert "harness=claude-code:2.1.316" in trailers(wired)[0]


@needs_prek
def test_e2e_hook_works_from_a_subdirectory(wired: Path) -> None:
    stage(wired, "sub/x.txt")
    done = git_commit(wired, "feat: x", CLAUDE_E2E, cwd=wired / "sub")
    assert done.returncode == 0, done.stderr
    assert len(trailers(wired)) == 1


@needs_prek
@pytest.mark.parametrize("env", [None, CLAUDE_E2E], ids=["human", "agent"])
def test_e2e_signoff_passes_the_check_for_everyone(
    wired: Path, env: dict[str, str] | None
) -> None:
    """`git commit -s` passes, human or agent: sign-off is not checked."""
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a", env, "-s")
    assert done.returncode == 0, done.stderr
    assert "Signed-off-by: Test <test@example.invalid>" in last_message(wired)


@needs_prek
def test_e2e_malformed_trailer_is_rejected(wired: Path) -> None:
    stage(wired, "a.txt")
    done = git_commit(wired, "feat: a\n\nGenerated-by: robot")
    assert done.returncode != 0
    assert "must start with literal LLM" in done.stderr


@needs_prek
def test_e2e_merge_commits_get_no_trailers(wired: Path) -> None:
    run_git(wired, "checkout", "-q", "-b", "side")
    stage(wired, "side.txt")
    run_git(wired, "commit", "-q", "--no-verify", "-m", "side")
    run_git(wired, "checkout", "-q", "-")
    stage(wired, "main.txt")
    run_git(wired, "commit", "-q", "--no-verify", "-m", "main")
    done = subprocess.run(
        ["git", "merge", "-q", "--no-ff", "-m", "merge side", "side"],
        capture_output=True,
        text=True,
        check=False,
        cwd=wired,
        env=hook_env(CLAUDE_E2E),
    )
    assert done.returncode == 0, done.stderr
    assert trailers(wired) == []


@needs_prek
def test_e2e_claude_subagent_commits_its_parents_edit(wired: Path) -> None:
    claude_write(wired, "a.txt")
    child_data = stdin_fixture(
        "claude-pretooluse-bash-child.json",
        cwd=str(wired),
        transcript_path=str(wired / "t.jsonl"),
    )
    record(wired, "claude", child_data, CLAUDE_E2E)
    run_git(wired, "add", "a.txt")
    agent = ai.sha256_64("agent-raw")
    done = git_commit(wired, "feat: a", {**CLAUDE_E2E, ai.AGENT_ENV: agent})
    assert done.returncode == 0, done.stderr
    session = ai.sha256_64("session-raw")
    gen, assist = trailers(wired)
    assert gen.startswith("Generated-by: LLM ")
    assert f"session={session}" in gen
    assert "subagent" not in gen
    assert assist.startswith("Assisted-by: LLM ")
    assert f"subagent={agent}" in assist
    assert ai.check_message(last_message(wired)) == []


@needs_prek
def test_e2e_codex_rollout_edit_then_commit(wired: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, cwd=str(wired)),
            turn("gpt-5.6-sol", "high"),
            patch_call("c.txt"),
        ],
    )
    stage(wired, "c.txt")
    env = {
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_HOME": str(home),
        "CODEX_VERSION": "0.166.0",
    }
    done = git_commit(wired, "feat: c", env)
    assert done.returncode == 0, done.stderr
    session = ai.sha256_64(ROOT_THREAD)
    assert trailers(wired) == [
        (
            "Generated-by: LLM effort=high;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-sol;session={session}"
        )
    ]


@needs_prek
def test_e2e_agy_edit_then_commit(wired: Path) -> None:
    (wired / "hello.txt").write_text("hi\n", encoding="utf-8")
    record(
        wired,
        "antigravity",
        agy_post(workspacePaths=[str(wired)]),
        {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"},
    )
    run_git(wired, "add", "hello.txt")
    env = {"ANTIGRAVITY_CONVERSATION_ID": CONV, "ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    done = git_commit(wired, "feat: hello", env)
    assert done.returncode == 0, done.stderr
    assert trailers(wired) == [
        (
            "Generated-by: LLM effort=low;harness=antigravity:1.4.12;"
            f"model=gcp.gemini:gemini-3.8-flash;session={ai.sha256_64(CONV)};"
            "surface=cli"
        )
    ]


@needs_prek
def test_e2e_store_is_shared_between_worktrees(wired: Path, tmp_path: Path) -> None:
    other = tmp_path / "second"
    run_git(wired, "worktree", "add", "-q", str(other), "-b", "second")
    claude_write(wired, "a.txt")
    store = ai.Repo(
        other,
        Path(
            run_git(
                other, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).strip()
        )
        / "ascm-interim",
    )
    session = ai.sha256_64("session-raw")
    assert [r["path"] for _, r in ai.load_edits(store, session)] == ["a.txt"]


SUB_E2E = ai.sha256_64("agent-raw")


def generated_subagents(repo: Path) -> list[str | None]:
    """The ``subagent=`` of each Generated-by trailer on HEAD (None: the root)."""
    return [
        (m[1] if (m := re.search(r"subagent=(\S+?)(;|$)", line)) else None)
        for line in trailers(repo)
        if line.startswith("Generated-by")
    ]


@needs_prek
def test_e2e_failed_commit_then_retry_keeps_the_subagent_trailer(
    wired: Path,
) -> None:
    claude_write(wired, "a.txt")
    claude_write(wired, "b.txt", agent_id="agent-raw")
    run_git(wired, "add", "a.txt", "b.txt")
    head = run_git(wired, "rev-parse", "HEAD")
    failed = git_commit(
        wired, "feat: ab", CLAUDE_E2E, "--trailer", "Generated-by: robot"
    )
    assert failed.returncode != 0
    assert run_git(wired, "rev-parse", "HEAD") == head
    done = git_commit(wired, "feat: ab", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert sorted(generated_subagents(wired), key=str) == [None, SUB_E2E]
    receipts = list((wired / ".git" / "ascm-interim" / "edits").rglob("*.json"))
    assert [loaded(p)["consumed"] for p in receipts] == [True, True]


@needs_prek
@pytest.mark.parametrize("hook", ["prepare-commit-msg", "commit-msg"])
def test_e2e_a_concurrent_edit_during_a_hook_does_not_fail_the_commit(
    wired: Path, hook: str
) -> None:
    """Regression: under prek these stages failed on a concurrent edit.

    prek reported "files were modified by this hook" and stashed unstaged
    edits away while the hook ran.
    """
    (wired / "base.txt").write_text("unstaged\n", encoding="utf-8")
    shim = wired / ".git" / "hooks" / hook
    lines = shim.read_text(encoding="utf-8").splitlines(keepends=True)
    # Another agent's edit lands while the hook runs, and sees the real file.
    lines.insert(1, "cat base.txt > seen.txt; echo concurrent >> base.txt\n")
    shim.write_text("".join(lines), encoding="utf-8")
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert (wired / "seen.txt").read_text(encoding="utf-8") == "unstaged\n"
    base = (wired / "base.txt").read_text(encoding="utf-8")
    assert base == "unstaged\nconcurrent\n"
    assert generated_subagents(wired) == [None]


@needs_prek
def test_e2e_a_formatter_rewrite_keeps_the_subagent_trailer(wired: Path) -> None:
    config = wired / ".pre-commit-config.yaml"
    loaded_config = cast("dict[str, list[object]]", yaml.safe_load(config.read_text()))
    loaded_config["repos"].append(
        {"repo": "builtin", "hooks": [{"id": "trailing-whitespace"}]}
    )
    config.write_text(yaml.safe_dump(loaded_config), encoding="utf-8")
    claude_write(wired, "b.txt", "sub   \n", agent_id="agent-raw")
    run_git(wired, "add", "b.txt")
    failed = git_commit(wired, "feat: b", CLAUDE_E2E)
    assert failed.returncode != 0
    assert (wired / "b.txt").read_text(encoding="utf-8") == "sub\n"
    run_git(wired, "add", "b.txt")
    done = git_commit(wired, "feat: b", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [SUB_E2E]
    (receipt,) = (wired / ".git" / "ascm-interim" / "edits").rglob("*.json")
    assert loaded(receipt)["consumed"] is True


@needs_prek
def test_e2e_an_edit_after_staging_is_credited_to_the_commit_that_has_it(
    wired: Path,
) -> None:
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    claude_write(wired, "a.txt", "a.txt\nsub\n", agent_id="agent-raw")
    done = git_commit(wired, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [None]
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: sub", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(wired) == [SUB_E2E]


@needs_prek
@pytest.mark.parametrize("flags", [("--amend",), ("--amend", "--no-edit")])
def test_e2e_amend_keeps_the_subagent_trailer(
    wired: Path, flags: tuple[str, ...]
) -> None:
    """Regression: an amend after post-commit dropped the subagent's credit."""
    claude_write(wired, "a.txt")
    claude_write(wired, "b.txt", agent_id="agent-raw")
    run_git(wired, "add", "a.txt", "b.txt")
    assert git_commit(wired, "feat: ab", CLAUDE_E2E).returncode == 0
    before = trailers(wired)
    args = ["git", "commit", "-q", *flags]
    if "--no-edit" not in flags:
        args += ["-m", "feat: reworded"]
    done = subprocess.run(
        args, capture_output=True, text=True, check=False, cwd=wired,
        env=hook_env(CLAUDE_E2E),
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert trailers(wired) == before


@needs_prek
def test_e2e_amend_with_new_root_edit_orders_generated_first(wired: Path) -> None:
    claude_write(wired, "b.txt", agent_id="agent-raw")
    run_git(wired, "add", "b.txt")
    assert git_commit(wired, "feat: b", CLAUDE_E2E).returncode == 0
    assert [t.split(":")[0] for t in trailers(wired)] == ["Generated-by", "Assisted-by"]
    claude_write(wired, "a.txt")
    run_git(wired, "add", "a.txt")
    done = git_commit(wired, "feat: ab", CLAUDE_E2E, "--amend", "--no-edit")
    assert done.returncode == 0, done.stderr
    assert sorted(generated_subagents(wired), key=str) == [None, SUB_E2E]
    assert not any(t.startswith("Assisted-by") for t in trailers(wired))


COG_LIKE = """#!/bin/sh
echo ran >> cog.log
grep -qE '^(feat|fix|chore): ' "$1" && exit 0
echo "cog: not a conventional commit" >&2
exit 1
"""


@pytest.fixture
def with_msg_linter(wired: Path) -> Path:
    """``wired`` plus a prek commit-msg hook (a cog-like linter), reinstalled."""
    (wired / "cog.sh").write_text(COG_LIKE, encoding="utf-8")
    config = wired / ".pre-commit-config.yaml"
    loaded_config = cast("dict[str, list[object]]", yaml.safe_load(config.read_text()))
    loaded_config["repos"].append(
        {
            "repo": "local",
            "hooks": [
                {
                    "id": "cog",
                    "name": "cog verify",
                    "language": "system",
                    "entry": "sh cog.sh",
                    "stages": ["commit-msg"],
                }
            ],
        }
    )
    config.write_text(yaml.safe_dump(loaded_config), encoding="utf-8")
    subprocess.run(
        ["bash", str(INSTALL)], check=True, capture_output=True, env=hook_env()
    )
    run_git(wired, "add", "cog.sh", ".pre-commit-config.yaml")
    run_git(wired, "commit", "-q", "--no-verify", "-m", "chore: linter")
    (wired / "cog.log").unlink(missing_ok=True)
    return wired


@needs_prek
def test_e2e_prek_commit_msg_hook_still_rejects_a_message(
    with_msg_linter: Path,
) -> None:
    """Regression: the plain commit-msg hook stopped running prek's hooks."""
    repo = with_msg_linter
    assert (repo / ".git" / "hooks" / "commit-msg.prek").is_file()
    claude_write(repo, "a.txt")
    run_git(repo, "add", "a.txt")
    head = run_git(repo, "rev-parse", "HEAD")
    done = git_commit(repo, "not conventional", CLAUDE_E2E)
    assert done.returncode != 0
    assert "cog: not a conventional commit" in done.stdout + done.stderr
    assert run_git(repo, "rev-parse", "HEAD") == head


@needs_prek
def test_e2e_prek_commit_msg_hook_passes_and_trailers_land(
    with_msg_linter: Path,
) -> None:
    repo = with_msg_linter
    claude_write(repo, "a.txt")
    run_git(repo, "add", "a.txt")
    done = git_commit(repo, "feat: a", CLAUDE_E2E)
    assert done.returncode == 0, done.stderr
    assert generated_subagents(repo) == [None]
    assert (repo / "cog.log").read_text(encoding="utf-8") == "ran\n"


@needs_prek
def test_e2e_identity_check_fails_even_when_prek_passes(
    with_msg_linter: Path,
) -> None:
    repo = with_msg_linter
    stage(repo, "a.txt")
    done = git_commit(repo, "feat: a", CLAUDE_E2E, "--trailer", "Generated-by: robot")
    assert done.returncode != 0
    assert "must start with literal LLM" in done.stderr
    # prek's hooks still ran after the identity step failed.
    assert (repo / "cog.log").read_text(encoding="utf-8") == "ran\n"


# ------------------------------------------------- pinning exact behavior


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("Xa\n", "Xa"), ("  Xa  \n\n", "  Xa  "), (" a \n", " a "), ("\n", "")],
)
def test_git_trims_only_newlines(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake(argv: list[str], **kwargs: object) -> FakeRun:
        calls.append((argv, kwargs))
        return FakeRun(3, stdout)

    monkeypatch.setattr(ai.subprocess, "run", fake)
    assert ai.git("status", "-s", cwd=Path("/w")) == (3, expected)
    assert calls == [
        (
            ["git", "status", "-s"],
            {"capture_output": True, "text": True, "cwd": Path("/w"), "check": False},
        )
    ]


def test_repo_info_handles_spaces_in_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = init_repo(tmp_path / "my repo")
    monkeypatch.chdir(path)
    assert ai.repo_info() == ai.Repo(path, path / ".git" / "ascm-interim")


def test_defaults_for_missing_fields() -> None:
    assert ai.rec_ts({}) == 0
    assert ai.rec_ts({"ts": 5}) == 5
    assert ai.rec_hashes({}) == set()
    assert ai.rec_hashes({"cmd": ["a", "b"]}) == {"a", "b"}


def spy_dirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every directory that is globbed or listed."""
    seen: list[Path] = []
    real_glob, real_iterdir = Path.glob, Path.iterdir

    def glob(self: Path, pattern: str) -> object:
        seen.append(self)
        return real_glob(self, pattern)

    def iterdir(self: Path) -> object:
        seen.append(self)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "glob", glob)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    return seen


def test_store_directory_names(repo: Path, tmp_path: Path) -> None:
    store = repo / ".git" / "ascm-interim"
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    ai.write_record(ROOT, {"model": "m"})
    ai.record_command(ROOT, None, "ls")
    ai.write_edit(make_ident(), str(repo / "a.txt"))
    home = tmp_path / "codex"
    write_rollout(home, ROOT_THREAD, [meta(id=ROOT_THREAD), patch_call("a.txt")])
    info = ai.repo_info()
    assert info is not None
    ai.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert sorted(os.listdir(store)) == ["cmds", "edits", "lock", "marks", "sessions"]  # noqa: PTH208 -- exact on-disk names
    feed_sid = ai.sha256_64("fresh")
    ai.write_json(store / "sessions" / f"{ai.hx(feed_sid)}.json", {})
    assert sorted(os.listdir(store / "sessions"))  # noqa: PTH208 -- exact on-disk names


def test_store_is_read_from_the_same_names(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = repo / ".git" / "ascm-interim"
    info = ai.repo_info()
    assert info is not None
    seen = spy_dirs(monkeypatch)
    list(ai.command_records(store, ROOT))
    ai.load_edits(info, ROOT)
    assert seen == [store / "cmds" / ai.hx(ROOT), store / "edits" / ai.hx(ROOT)]
    paths: list[Path] = []

    def spy(path: Path) -> Json:
        paths.append(path)
        return {}

    monkeypatch.setattr(ai, "read_record", spy)
    ai.load_record(ROOT)
    assert paths == [store / "sessions" / f"{ai.hx(ROOT)}.json"]


def test_cmd_record_writes_the_sessions_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert ai.cmd_record("antigravity", {}) == 0
    assert sorted(os.listdir(repo / ".git" / "ascm-interim")) == ["lock", "sessions"]  # noqa: PTH208 -- exact on-disk names


def test_finish_command_prefers_the_newest_not_the_last_path(repo: Path) -> None:
    interim = repo / ".git" / "ascm-interim" / "cmds" / ai.hx(ROOT)
    hashes = ai.cmd_hashes("ls")
    for name, ts in (("a.json", 20), ("z.json", 10)):
        ai.write_json(
            interim / name, {"agent": None, "cmd": hashes, "ts": ts, "done": None}
        )
    ai.finish_command(ROOT, "ls", None)
    assert loaded(interim / "a.json")["done"] is not None
    assert loaded(interim / "z.json")["done"] is None


def test_finish_command_ties_go_to_the_first_record(repo: Path) -> None:
    store = repo / ".git" / "ascm-interim"
    interim = store / "cmds" / ai.hx(ROOT)
    for name in ("one.json", "two.json"):
        ai.write_json(
            interim / name,
            {"agent": None, "cmd": ai.cmd_hashes("ls"), "ts": 5, "done": None},
        )
    first = next(path for path, _ in ai.command_records(store, ROOT))
    ai.finish_command(ROOT, "ls", None)
    done = [p for p in interim.glob("*.json") if loaded(p)["done"] is not None]
    assert done == [first]


def test_match_command_agent_window_is_exclusive(repo: Path) -> None:
    line = "sh -c eval 'ls'"
    ai.record_command(ROOT, CHILD, "ls", repo)
    edge = time_of(repo) + ai.CMD_WINDOW
    assert ai.match_command_agent(ROOT, [line], repo, now=edge) == (None, False)


def test_blob_of_applies_the_repos_filters(repo: Path, plain: Path) -> None:
    run_git(repo, "config", "core.autocrlf", "true")
    (repo / "f.txt").write_bytes(b"a\r\nb\r\n")
    run_git(repo, "add", "f.txt")
    staged = run_git(repo, "ls-files", "-s", "f.txt").split()[1]
    assert plain.is_dir()
    assert ai.blob_of(repo, "f.txt") == staged


def test_write_edit_resolves_relative_paths_against_the_base(
    repo: Path, plain: Path
) -> None:
    (repo / "r.txt").write_text("r\n", encoding="utf-8")
    ai.write_edit(make_ident(), "r.txt", repo)
    assert [r["path"] for r in receipts_of(repo, ROOT)] == ["r.txt"]
    assert plain.is_dir()


def test_claude_events_follow_the_hook_cwd_not_the_process_cwd(
    repo: Path, plain: Path
) -> None:
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    child = claude_data(repo, "claude-pretooluse-bash-child.json")
    ai.handle_claude(child, CLAUDE_HOOK_ENV)
    assert ai.load_record(AGENT, repo)["effort"] == "high"
    assert ai.load_record(AGENT, repo)["root"] == SESSION_HASH
    assert ai.load_record(SESSION_HASH, repo) == {}
    cmds = repo / ".git" / "ascm-interim" / "cmds"
    assert os.listdir(cmds) == [ai.hx(SESSION_HASH)]  # noqa: PTH208 -- exact on-disk names
    (rec,) = pending(repo)
    assert (rec["agent"], rec["done"]) == (AGENT, None)
    done = {**child, "hook_event_name": "PostToolUse"}
    ai.handle_claude(done, CLAUDE_HOOK_ENV)
    assert isinstance(pending(repo)[0]["done"], float)
    assert plain.is_dir()


def test_claude_main_events_follow_the_hook_cwd(repo: Path, plain: Path) -> None:
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    main = claude_data(repo, effort="high")
    ai.handle_claude(main, CLAUDE_HOOK_ENV)
    assert ai.load_record(SESSION_HASH, repo)["harness"] == "claude-code"
    response = {"bashEditDiff": "diff --git a/x.txt b/x.txt\n+++ b/x.txt\n"}
    post = {**main, "hook_event_name": "PostToolUse", "tool_response": response}
    ai.handle_claude(post, CLAUDE_HOOK_ENV)
    write = stdin_fixture(
        "claude-posttooluse-write.json", cwd=str(repo), transcript_path=""
    )
    ai.handle_claude(write, CLAUDE_HOOK_ENV)
    paths = sorted(str(r["path"]) for r in receipts_of(repo, SESSION_HASH))
    assert paths == ["hello.txt", "x.txt"]
    assert plain.is_dir()


def test_claude_ident_reads_the_record_in_the_hook_cwd(repo: Path, plain: Path) -> None:
    ai.write_record(SESSION_HASH, {"version": "9.9"}, repo)
    got = ai.claude_ident(claude_data(repo), {})
    assert got.harness == "claude-code:9.9"
    assert plain.is_dir()


def test_rollout_turn_context_ignores_other_records_with_payloads(
    tmp_path: Path,
) -> None:
    other = {"type": "event_msg", "payload": {"model": "x", "effort": "low"}}
    path = write_rollout(tmp_path, ROOT_THREAD, [turn("m", "high"), other])
    assert ai.rollout_turn_context(path) == ("m", "high")


def test_tool_call_paths_reads_the_action_of_shell_calls() -> None:
    patch = "*** Begin Patch\n*** Add File: z.txt\n+x\n*** End Patch"
    call: Json = {
        "type": "response_item",
        "payload": {"type": "local_shell_call", "action": {"command": [patch]}},
    }
    assert ai.tool_call_paths(call) == ["z.txt"]
    payload = cast("Json", call["payload"])
    assert ai.tool_call_paths({**call, "payload": {**payload, "action": None}}) == []
    assert ai.tool_call_paths({**call, "payload": {"type": "local_shell_call"}}) == []


def test_rollouts_tolerate_invalid_utf8(tmp_path: Path) -> None:
    good = json.dumps(turn("m", "high"))
    path = tmp_path / "r.jsonl"
    path.write_bytes(b"\xff\xfe broken\n" + good.encode() + b"\n")
    assert ai.read_tail(path, 1000) == ["�� broken", good]
    assert ai.rollout_turn_context(path) == ("m", "high")
    patch = json.dumps(patch_call("a.txt"))
    path.write_bytes(b"\xff Begin Patch\n" + patch.encode() + b"\n")
    assert patch_paths_of(path) == ["a.txt"]


def test_codex_threads_age_limit_is_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_rollout(tmp_path, ROOT_THREAD, [meta(id=ROOT_THREAD)])
    os.utime(path, (1000, 1000))
    env = {"CODEX_HOME": str(tmp_path)}
    monkeypatch.setattr(ai.time, "time", lambda: 1000 + ai.CODEX_MAX_AGE)
    assert len(ai.codex_threads(ROOT_THREAD, env)) == 1
    monkeypatch.setattr(ai.time, "time", lambda: 1001 + ai.CODEX_MAX_AGE)
    assert ai.codex_threads(ROOT_THREAD, env) == []


def test_codex_scan_starts_at_zero_and_uses_the_rollout_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = init_repo(tmp_path / "other")
    (other / "o.txt").write_text("o\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home, ROOT_THREAD, [meta(id=ROOT_THREAD, cwd=str(other)), patch_call("o.txt")]
    )
    offsets: list[int] = []
    real = ai.rollout_patch_edits

    def spy(path: Path, offset: int, skip_lines: int) -> tuple[list[str], int]:
        offsets.append(offset)
        return real(path, offset, skip_lines)

    monkeypatch.setattr(ai, "rollout_patch_edits", spy)
    info = ai.repo_info()
    assert info is not None
    ai.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert offsets == [0]
    root = ai.sha256_64(ROOT_THREAD)
    assert [r["path"] for r in receipts_of(other, root)] == ["o.txt"]
    assert receipts_of(repo, root) == []


def test_agy_parent_reads_the_documented_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = spy_dirs(monkeypatch)
    ai.agy_parent(tmp_path, SUB)
    assert seen == [tmp_path / SUB / ".system_generated" / "messages"]


def test_agy_root_remembers_every_conversation_it_visited(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    write_message(brain, "a", "m.json", invoke("b", "a"))
    write_message(brain, "b", "m.json", invoke("c", "b"))
    write_message(brain, "c", "m.json", invoke("b", "c"))
    assert ai.agy_root(brain, "a", max_depth=3) == "c"


def test_cmd_record_agy_child_follows_the_hook_cwd_and_brain(
    repo: Path, plain: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
    data = agy_post(
        conversationId=SUB, cwd=str(repo), artifactDirectoryPath=f"{brain / SUB}/"
    )
    feed(monkeypatch, data)
    env = {"ANTIGRAVITY_LS_VERSION": "cli-1.4.12"}
    assert ai.cmd_record("antigravity", env) == 0
    assert ai.load_record(ai.sha256_64(SUB), repo)["brain_root"] == str(brain)
    (got,) = receipts_of(repo, ai.sha256_64(CONV))
    assert got["subagent"] == ai.sha256_64(SUB)
    assert got["harness"] == "antigravity:1.4.12"
    assert plain.is_dir()


def test_claude_committer_prefers_the_env_version(repo: Path) -> None:
    ai.write_record(SESSION_HASH, {"version": "1.0"}, repo)
    got = ai.committer_identity(CLAUDE_COMMIT_ENV, [])
    assert got is not None
    assert got.root_ident.harness == "claude-code:2.1.316"


def test_codex_committer_pins_the_thread_ids(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": CHILD_THREAD,
    }
    got = ai.committer_identity(env)
    assert got is not None
    assert got.ident.subagent == ai.sha256_64(CHILD_THREAD)
    assert got.root_ident.subagent is None
    # Rollout metadata naming some other thread never overrides the env.
    stray = "01a19ebf-0000-7000-8000-000000000000"
    write_rollout(home, ROOT_THREAD, [meta(id=stray)])
    write_rollout(home, CHILD_THREAD, [meta(id=stray)])
    again = ai.committer_identity(env)
    assert again is not None
    assert again.ident.subagent == ai.sha256_64(CHILD_THREAD)
    assert again.root_ident.subagent is None


@pytest.mark.usefixtures("repo")
def test_agy_committer_keys_and_roots(tmp_path: Path) -> None:
    solo = ai.committer_identity({"ANTIGRAVITY_CONVERSATION_ID": CONV})
    assert solo is not None
    assert solo.key == ai.ANTIGRAVITY
    assert solo.ident == solo.root_ident
    brain = tmp_path / "brain"
    write_message(brain, SUB, "m.json", invoke(CONV, SUB))
    ai.write_record(ai.sha256_64(SUB), {"brain_root": str(brain)})
    child = ai.committer_identity({"ANTIGRAVITY_CONVERSATION_ID": SUB})
    assert child is not None
    assert child.key == ai.ANTIGRAVITY
    assert child.ident.subagent == ai.sha256_64(SUB)
    assert child.root_ident.subagent is None
    assert child.root_ident.root == ai.sha256_64(CONV)


def test_staged_info_edge_paths(repo: Path) -> None:
    stage(repo, "committed.txt")
    run_git(repo, "commit", "-q", "--no-verify", "-m", "c")
    (repo / "committed.txt").write_text("changed\n", encoding="utf-8")
    stage(repo, "-dash.txt")
    stage(repo, "tab\there.txt")
    paths, blobs = ai.staged_info(repo)
    assert sorted(paths) == ["-dash.txt", "tab\there.txt"]
    assert sorted(blobs) == ["-dash.txt", "tab\there.txt"]


def test_staged_info_defaults_to_the_process_cwd(repo: Path) -> None:
    stage(repo, "top.txt")
    os.chdir(repo)
    paths, blobs = ai.staged_info()
    assert (paths, sorted(blobs)) == (["top.txt"], ["top.txt"])


def test_cmd_trailer_ranks_by_blob_from_a_subdirectory(
    repo: Path, tmp_path: Path
) -> None:
    stage(repo, "top.txt")
    blob = run_git(repo, "ls-files", "-s", "top.txt").split()[1]
    last = make_ident(root=SESSION_HASH, model="anthropic:last")
    stale = make_ident(CHILD, "anthropic:stale", SESSION_HASH)
    info = ai.repo_info()
    assert info is not None
    for name, who, receipt_blob, ts in (
        ("last", last, blob, 1),
        ("stale", stale, "other", 2),
    ):
        ai.write_json(
            info.interim / "edits" / ai.hx(SESSION_HASH) / f"{name}.json",
            make_receipt("top.txt", who, receipt_blob, ts),
        )
    (repo / "sub").mkdir()
    os.chdir(repo / "sub")
    path = msg_file(tmp_path)
    assert ai.cmd_trailer(str(path), None, CLAUDE_COMMIT_ENV) == 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    generated = [ln for ln in lines if ln.startswith("Generated-by:")]
    assert "model=anthropic:last" in generated[0]
    assert "model=anthropic:stale" in generated[1]


def test_enrich_matches_harness_ignoring_versions() -> None:
    cm = committer_of(
        ai.Ident(ROOT, "claude-code:2.1", None, "anthropic:m", None, "cli")
    )
    other = ai.Ident(ROOT, "claude-code:1.0")
    assert ai.enrich(other, cm) == ai.Ident(
        ROOT, "claude-code:1.0", None, None, None, "cli"
    )


def test_add_trailers_asks_git_to_add_only_different_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def spy(*args: str, **_kwargs: Path | None) -> tuple[int, str]:
        calls.append(args)
        return 1, ""

    monkeypatch.setattr(ai, "git", spy)
    path = msg_file(tmp_path)
    ai.add_trailers(
        path, [make_ident(model=None)], [make_ident(model=None, root=OTHER)]
    )
    assert calls == [
        (
            "interpret-trailers",
            "--if-exists",
            "addIfDifferent",
            f"--trailer=Generated-by: LLM harness=claude-code;session={ROOT}",
            f"--trailer=Assisted-by: LLM harness=claude-code;session={OTHER}",
            str(path.with_name("MSG.ascm")),
        )
    ]
    assert not path.with_name("MSG.ascm").exists()


def test_cmd_consume_asks_git_exact_questions(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "a.txt")
    commit(repo)
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    real = ai.git

    def spy(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        calls.append((args, cwd))
        return real(*args, cwd=cwd)

    monkeypatch.setattr(ai, "git", spy)
    assert ai.cmd_consume() == 0
    assert calls[-1] == (("rev-parse", "HEAD^{tree}"), repo)


def test_check_pair_splits_at_the_first_equals_sign() -> None:
    assert ai.check_pair("model=a=b") is None
    assert ai.check_pair("model=a=b=c") is None
    assert ai.check_pair("harness=codex:1:2") is None
    assert ai.check_pair("harness=x:codex") == "unknown harness x:codex"


def test_check_value_splits_keys_at_the_first_equals_sign() -> None:
    assert ai.check_value("LLM harness=codex;model=a=b") is None
    assert ai.check_value("LLM harness=codex;model=a=b;surface=c=d") is None
    # Splitting at the last "=" would read these keys as surface=a and
    # surface=b and miss the duplicate.
    assert (
        ai.check_value("LLM harness=codex;surface=a=1;surface=b=1")
        == "attributes out of order or duplicated"
    )


def test_check_messages_are_exact() -> None:
    for line in ("Signed-off-by: A <a@b.c>", "Co-authored-by: A <a@b.c>"):
        assert ai.check_message(f"{line}\n") == []
    assert ai.check_value("LLM harness=codex") is None


@pytest.mark.usefixtures("repo")
def test_main_runs_each_command_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(monkeypatch, {"session_id": "s", "model": "m"})
    assert ai.main(["record", "antigravity"], {}) == 0
    assert ai.load_record(ai.sha256_64("s"))["harness"] == "antigravity"
    path = msg_file(tmp_path)
    assert ai.main(["trailer", str(path)], CLAUDE_COMMIT_ENV) == 0
    assert "Generated-by:" in path.read_text(encoding="utf-8")


# ------------------------------------------------------- forked Codex threads


def fork_lines(replayed: int) -> list[object]:
    """A synthetic forked rollout: ``replayed`` inherited lines, then its own."""
    inherited: list[object] = [
        meta(
            id=CHILD_THREAD,
            session_id=ROOT_THREAD,
            forked_from_id=ROOT_THREAD,
            **{START: replayed},
        ),
        patch_call("parent.md"),
        *[{"type": "response_item", "payload": {"type": "message"}}] * (replayed - 2),
    ]
    own: list[object] = [
        {"type": "event_msg", "payload": {"type": "thread_settings_applied"}},
        patch_call("child.md"),
    ]
    return [*inherited, *own]


START = "subagent_history_start_ordinal"


def test_rollout_patch_edits_skips_the_replayed_prefix(tmp_path: Path) -> None:
    path = write_rollout(tmp_path, CHILD_THREAD, fork_lines(4))
    assert patch_paths_of(path) == ["parent.md", "child.md"]
    edits, new = ai.rollout_patch_edits(path, 0, 4)
    assert edits == ["child.md"]
    assert new == path.stat().st_size
    # A saved offset beyond the prefix wins over the prefix.
    assert ai.rollout_patch_edits(path, new, 4) == ([], new)
    # A prefix beyond the saved offset wins over the offset.
    assert patch_paths_of(path, 1, 4) == ["child.md"]


def test_codex_scan_ignores_patches_replayed_into_a_fork(
    repo: Path, tmp_path: Path
) -> None:
    for name in ("parent.md", "child.md"):
        (repo / name).write_text("x\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, session_id=ROOT_THREAD), patch_call("parent.md")],
    )
    write_rollout(home, CHILD_THREAD, fork_lines(4))
    info = ai.repo_info()
    assert info is not None
    ai.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    by_thread = {
        r["path"]: r["subagent"] for r in receipts_of(repo, ai.sha256_64(ROOT_THREAD))
    }
    assert by_thread == {"parent.md": None, "child.md": ai.sha256_64(CHILD_THREAD)}


@pytest.mark.parametrize("bad", [None, 0, -3, "4", 2.5])
def test_codex_scan_tolerates_odd_fork_markers(
    repo: Path, tmp_path: Path, bad: object
) -> None:
    (repo / "parent.md").write_text("x\n", encoding="utf-8")
    home = tmp_path / "codex"
    write_rollout(
        home,
        CHILD_THREAD,
        [
            meta(id=CHILD_THREAD, session_id=ROOT_THREAD, **{START: bad}),
            patch_call("parent.md"),
        ],
    )
    info = ai.repo_info()
    assert info is not None
    ai.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    assert len(receipts_of(repo, ai.sha256_64(ROOT_THREAD))) == 1


# ------------------------------------------------------------------ stamp-docs

SCHEMA = loaded(
    Path(__file__).parents[2] / "docs" / "schemas" / "frontmatter.schema.json"
)
VALIDATOR = jsonschema.Draft202012Validator(
    SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)
HEAD = "type: Reference\ntitle: T\ndescription: D.\ntags:\n  - a-b\nstatus: draft\n"
VERIFIED = 'verified:\n  by: human:tony\n  at: "2026-01-01T00:00:00Z"\n'
AT1 = "2026-11-02T10:00:00Z"
AT2 = "2026-11-02T11:00:00Z"
SESSION = H + "1" * 16
type Entry = dict[str, str]


def entry(**fields: str) -> Entry:
    base = {
        "role": "generated",
        "harness": "claude-code:2.1.316",
        "model": "anthropic:claude-opus-5-5",
        "at": AT1,
    }
    return {**base, **fields}


def document(*lines: str, head: str = HEAD, tail: str = "", body: str = "# T\n") -> str:
    block = "".join(f"{line}\n" for line in lines)
    return f"---\n{head}{block}{tail}---\n{body}"


def front_of(text: str) -> Json:
    """The parsed frontmatter, checked against the repo's schema."""
    data = yaml.safe_load(text.split("---\n")[1])
    VALIDATOR.validate(data)
    return cast("Json", data)


def listing(*entries: Entry, indent: int = 2) -> list[str]:
    return [line for e in entries for line in ai.render_entry(e, indent)]


def test_valid_entry() -> None:
    assert ai.valid_entry(entry())
    full = entry(
        effort="high",
        surface="cli",
        session=SESSION,
        subagent=CHILD,
        role="assisted",
        harness="codex",
        model="openai:gpt-5.6-sol",
    )
    assert ai.valid_entry(full)


@pytest.mark.parametrize(
    "bad",
    [
        {"role": "author"},
        {"harness": "gemini-cli"},
        {"harness": "claude-code:"},
        {"harness": "claude-code:2.1 beta"},
        {"model": "claude-opus-5-5"},
        {"model": "azure:gpt"},
        {"model": "anthropic:"},
        {"effort": "High"},
        {"surface": "a;b"},
        {"surface": "a,b"},
        {"surface": "café"},
        {"surface": "a b"},
        {"surface": ""},
        {"session": "sha-256-64:ABCDEF0123456789"},
        {"session": "sha-256-64:abc"},
        {"subagent": "raw-id"},
        {"at": "2026-11-02 10:00:00"},
        {"at": "2026-11-02T10:00:00+00:00"},
        {"unknown": "x"},
    ],
)
def test_valid_entry_rejects(bad: Entry) -> None:
    assert not ai.valid_entry(entry(**bad))


@pytest.mark.parametrize("missing", ["role", "harness", "model", "at"])
def test_valid_entry_requires(missing: str) -> None:
    incomplete = entry()
    del incomplete[missing]
    assert not ai.valid_entry(incomplete)


def test_contributor_key_ignores_version_role_and_time() -> None:
    first = entry(session=SESSION, subagent=CHILD)
    other = entry(
        session=SESSION,
        subagent=CHILD,
        harness="claude-code",
        role="assisted",
        at=AT2,
        effort="low",
        surface="cli",
    )
    assert ai.contributor_key(first) == ai.contributor_key(other)
    assert ai.contributor_key(first) == (
        "claude-code",
        "anthropic:claude-opus-5-5",
        SESSION,
        CHILD,
    )
    assert ai.contributor_key(entry()) == (
        "claude-code",
        "anthropic:claude-opus-5-5",
        None,
        None,
    )
    for change in (
        {"harness": "codex"},
        {"model": "anthropic:other"},
        {"session": OTHER},
        {"subagent": OTHER},
    ):
        assert ai.contributor_key({**first, **change}) != ai.contributor_key(first)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("claude-code:2.1.316", "claude-code:2.1.316"),
        ("gcp.gemini:gemini-3.8-flash", "gcp.gemini:gemini-3.8-flash"),
        (SESSION, SESSION),
        ("a/b+c_d", "a/b+c_d"),
        ("a:", '"a:"'),
        ("n", '"n"'),
        ("no", '"no"'),
        ("Yes", '"Yes"'),
        ("NULL", '"NULL"'),
        ("true", '"true"'),
        ("null", '"null"'),
        ("off", '"off"'),
        ("123", '"123"'),
        ("-x", '"-x"'),
        ("*x", '"*x"'),
        ('a"b', '"a\\"b"'),
        ("a\\b", '"a\\\\b"'),
        ("a#b", '"a#b"'),
        (AT1, f'"{AT1}"'),
    ],
)
def test_yaml_scalar(value: str, expected: str) -> None:
    assert ai.yaml_scalar(value) == expected


def test_yaml_scalar_words_are_matched_exactly() -> None:
    assert ai.yaml_scalar("nope") == "nope"
    assert ai.yaml_scalar("yes") == '"yes"'
    assert ai.yaml_scalar("false") == '"false"'
    assert ai.yaml_scalar("on") == '"on"'
    assert ai.yaml_scalar("y") == '"y"'
    assert ai.yaml_scalar("ab") == "ab"
    assert ai.yaml_scalar("a") == "a"
    assert ai.yaml_scalar("a-") == "a-"


def test_render_entry_orders_the_fields_canonically() -> None:
    full = entry(
        effort="high", surface="cli", session=SESSION, subagent=CHILD, role="assisted"
    )
    shuffled = dict(reversed(list(full.items())))
    expected = [
        "- role: assisted",
        "  harness: claude-code:2.1.316",
        "  model: anthropic:claude-opus-5-5",
        "  effort: high",
        "  surface: cli",
        f"  session: {SESSION}",
        f"  subagent: {CHILD}",
        f'  at: "{AT1}"',
    ]
    assert ai.render_entry(shuffled, 0) == expected
    assert ai.render_entry(full, 3) == [
        "   - role: assisted",
        *[f"     {line[2:]}" for line in expected[1:]],
    ]
    assert ai.render_entry({"model": "m"}, 1) == [" - model: m"]
    assert ai.render_entry({}, 1) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("1.2.3", "1.2.3"),
        ("a:b", "a:b"),
        ("plain  ", "plain"),
        ('"quoted value"', "quoted value"),
        ('""', ""),
        ('"a\\"b\\\\c"', 'a"b\\c'),
        ("'single'", "single"),
        ("''", ""),
        ("'a\"b'", 'a"b'),
        ('"a"  ', "a"),
    ],
)
def test_unquote(raw: str, expected: str) -> None:
    assert ai.unquote(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "  ",
        "a #comment",
        "a #",
        "#a",
        "[a]",
        "{a: b}",
        "|",
        ">",
        "&anchor",
        "*alias",
        "!tag",
        '"unterminated',
        '"a" "b"',
        '"a\\nb"',
        "'a'b'",
        '"a" # c',
        "-x",
        "a b",
    ],
)
def test_unquote_rejects(raw: str) -> None:
    assert ai.unquote(raw) is None


def test_parse_entries_reads_block_lists() -> None:
    lines = [
        "  - role: generated",
        "    harness: 'claude-code:1'",
        '    model: "anthropic:m"',
        f"    at: {AT1}",
        "  - role: assisted",
        "    model: x",
    ]
    parsed = ai.parse_entries(lines)
    assert parsed is not None
    assert [(p.first, p.last, p.indent) for p in parsed] == [(0, 4, 2), (4, 6, 2)]
    assert parsed[0].fields == {
        "role": "generated",
        "harness": "claude-code:1",
        "model": "anthropic:m",
        "at": AT1,
    }
    assert parsed[1].fields == {"role": "assisted", "model": "x"}
    assert ai.parse_entries([]) == []
    top = ai.parse_entries(["- role: generated", "  at: x"])
    assert top is not None
    assert (top[0].indent, top[0].last) == (0, 2)
    deep = ai.parse_entries(["    - role: generated", "      at: x"])
    assert deep is not None
    assert deep[0].indent == 4


@pytest.mark.parametrize(
    "lines",
    [
        ["  role: generated"],
        ["  - role: generated", "   harness: x"],
        ["  - role: generated", "     harness: x"],
        ["  - role: generated", "  harness: x"],
        ["  - role: generated", "    role: assisted"],
        ["  - role: generated", "    # comment"],
        ["  - role: generated", ""],
        ["  - role:"],
        ["  - role: [a]"],
        ["  - Role: x"],
        ["  - role: generated", "    - nested"],
        ["  - role: generated", "    harness: x #c"],
        ["# comment", "  - role: generated"],
    ],
)
def test_parse_entries_rejects(lines: list[str]) -> None:
    assert ai.parse_entries(lines) is None


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # Newer sighting: at, effort, surface and a harness version move forward.
        (
            entry(effort="low", surface="cli"),
            entry(at=AT2, effort="high", surface="vscode", harness="claude-code:2.2"),
            entry(at=AT2, effort="high", surface="vscode", harness="claude-code:2.2"),
        ),
        # Same time counts as newer.
        (entry(effort="low"), entry(effort="high"), entry(effort="high")),
        # A sighting without those fields keeps what is known.
        (
            entry(effort="low", surface="cli"),
            entry(at=AT2, harness="claude-code"),
            entry(at=AT2, effort="low", surface="cli"),
        ),
        # Older sighting: only fills what is missing.
        (
            entry(at=AT2, effort="low"),
            entry(effort="high", surface="cli"),
            entry(at=AT2, effort="low", surface="cli"),
        ),
        (
            entry(at=AT2, harness="claude-code"),
            entry(harness="claude-code:1.0"),
            entry(at=AT2, harness="claude-code:1.0"),
        ),
        (
            entry(at=AT2, harness="claude-code:2.0"),
            entry(harness="claude-code:1.0"),
            entry(at=AT2, harness="claude-code:2.0"),
        ),
        (
            entry(harness="claude-code:1.0"),
            entry(at=AT2, harness="claude-code"),
            entry(at=AT2, harness="claude-code:1.0"),
        ),
        # Generated beats assisted, and never the reverse.
        (entry(role="assisted"), entry(role="generated"), entry()),
        (
            entry(role="assisted"),
            entry(role="assisted", at=AT2),
            entry(role="assisted", at=AT2),
        ),
        (entry(), entry(role="assisted", at=AT2, effort="x"), entry()),
        (entry(role="generated"), entry(role="generated", at=AT2), entry(at=AT2)),
    ],
)
def test_merge_entry(old: Entry, new: Entry, expected: Entry) -> None:
    assert ai.merge_entry(old, new) == expected


def test_merge_entry_does_not_mutate_its_arguments() -> None:
    old, new = entry(effort="low"), entry(at=AT2, effort="high")
    snapshot = (dict(old), dict(new))
    ai.merge_entry(old, new)
    assert (old, new) == snapshot


# ---- stamp_text


def test_stamp_text_adds_the_key_before_the_closing_fence() -> None:
    text = document()
    out = ai.stamp_text(text, [entry(effort="high")])
    assert out == document(
        "generated:",
        "  - role: generated",
        "    harness: claude-code:2.1.316",
        "    model: anthropic:claude-opus-5-5",
        "    effort: high",
        f'    at: "{AT1}"',
    )
    assert front_of(out)["generated"] == [
        {
            "role": "generated",
            "harness": "claude-code:2.1.316",
            "model": "anthropic:claude-opus-5-5",
            "effort": "high",
            "at": AT1,
        }
    ]


def test_stamp_text_keeps_everything_around_the_block() -> None:
    other = entry(model="openai:gpt-5.6-sol", harness="codex")
    text = document(
        "generated:",
        *listing(entry()),
        "verified:",
        "  by: human:tony",
        f'  at: "{AT1}"',
        body="body\n\n# T\n---\nx\r\n",
    )
    out = ai.stamp_text(text, [other])
    expected = document(
        "generated:",
        *listing(other),
        "verified:",
        "  by: human:tony",
        f'  at: "{AT1}"',
        body="body\n\n# T\n---\nx\r\n",
    )
    assert out == expected


def test_stamp_text_replaces_in_the_existing_indent() -> None:
    other = entry(model="openai:gpt-5.6-sol", harness="codex")
    text = document("generated:", *listing(entry(), indent=0))
    out = ai.stamp_text(text, [other])
    assert out == document("generated:", *listing(other, indent=0))
    wide = document("generated:", *listing(entry(), indent=4))
    assert ai.stamp_text(wide, [other]) == document(
        "generated:", *listing(other, indent=4)
    )


def test_stamp_text_replaces_rather_than_merges() -> None:
    before = document("generated:", *listing(entry(role="assisted", effort="low")))
    out = ai.stamp_text(before, [entry(at=AT2, surface="cli")])
    assert out == document("generated:", *listing(entry(at=AT2, surface="cli")))


def test_stamp_text_keeps_only_the_latest_generation() -> None:
    first = entry(model="openai:gpt-5.6-sol", harness="codex", effort="low")
    second = entry(effort="low")
    third = entry(model="anthropic:other")
    before = document("generated:", *listing(first, second, third))
    latest = [entry(at=AT2, effort="high"), entry(at=AT2, model="anthropic:other")]
    out = ai.stamp_text(before, latest)
    assert out == document("generated:", *listing(*latest))


def test_stamp_text_does_not_grow_across_sessions() -> None:
    text = document()
    for n in range(5):
        session = f"sha-256-64:{n:016x}"
        text = ai.stamp_text(text, [entry(session=session)])
    assert front_of(text)["generated"] == [entry(session=f"sha-256-64:{4:016x}")]


def test_stamp_text_leaves_the_same_generation_byte_for_byte() -> None:
    odd = [
        "generated:",
        '- role: "generated"',
        "  harness: 'claude-code:2.1.316'",
        "  model: anthropic:claude-opus-5-5",
        f"  at: {AT1}",
    ]
    text = document(*odd)
    assert ai.stamp_text(text, [entry()]) == text
    assert ai.stamp_text(text, [entry(role="assisted", at=AT2)]) != text


def test_stamp_text_adds_a_changed_field_in_canonical_form() -> None:
    odd = [
        "generated:",
        '- role: "generated"',
        "  harness: 'claude-code:2.1.316'",
        "  model: anthropic:claude-opus-5-5",
        f"  at: {AT1}",
    ]
    out = ai.stamp_text(document(*odd), [entry(at=AT2, effort="high")])
    assert out == document(
        "generated:", *listing(entry(at=AT2, effort="high"), indent=0)
    )


def test_stamp_text_empty_forms() -> None:
    expected = document("generated:", *listing(entry()))
    assert ai.stamp_text(document("generated: []"), [entry()]) == expected
    assert ai.stamp_text(document("generated:"), [entry()]) == expected
    assert ai.stamp_text(document("generated: [] "), [entry()]) == expected


@pytest.mark.parametrize(
    "lines",
    [
        ["generated: ~"],
        ["generated: null"],
        ["generated: [{role: generated}]"],
        ["generated: &a"],
        ["generated:", "  # note", *listing(entry())],
        ["generated:", *listing(entry()), "  # note"],
        ["generated:", "  - role: generated", "    note: x y"],
        ["generated:", "  - 5"],
    ],
)
def test_stamp_text_leaves_unsupported_generated_values(lines: list[str]) -> None:
    text = document(*lines)
    assert (
        ai.stamp_text(text, [entry(model="openai:gpt-5.6-sol", harness="codex")])
        == text
    )


def test_stamp_text_leaves_an_unsupported_list_with_several_entries() -> None:
    text = document("generated:", *listing(entry()), "  # note")
    other = entry(model="openai:gpt-5.6-sol", harness="codex")
    assert ai.stamp_text(text, [entry(at=AT2), other]) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no frontmatter\n",
        "\n---\ntype: x\n---\n",
        "---\ntype: x\n",
        "---\r\ntype: x\r\n---\r\n",
        " ---\ntype: x\n---\n",
        "---x\ntype: x\n---\n",
        "----\ntype: x\n----\n",
    ],
)
def test_stamp_text_needs_lf_frontmatter(text: str) -> None:
    assert ai.stamp_text(text, [entry()]) == text


def test_stamp_text_accepts_a_closing_fence_with_trailing_blanks() -> None:
    out = ai.stamp_text("---\ntype: x\n---  \t\nbody\n", [entry()])
    assert (
        out
        == "---\ntype: x\n"
        + "\n".join(["generated:", *listing(entry())])
        + "\n---  \t\nbody\n"
    )
    assert "---\n---\n" not in out
    assert ai.stamp_text("---\n---\n", [entry()]) == "\n".join(
        ["---", "generated:", *listing(entry()), "---", ""]
    )
    assert (
        ai.stamp_text("---\ntype: x\n--- x\n---\n", [entry()]).count("generated:") == 1
    )


def test_stamp_text_ignores_look_alike_keys() -> None:
    text = document("generated_by: x", " generated:", "xgenerated:")
    out = ai.stamp_text(text, [entry()])
    assert out.count("\ngenerated:\n") == 1
    assert out.endswith("\n".join(listing(entry())) + "\n---\n# T\n")
    text = document("generated:", *listing(entry()), "generated:")
    assert ai.stamp_text(text, [entry(at=AT2)]) == document(
        "generated:", *listing(entry(at=AT2)), "generated:"
    )


def test_stamp_text_only_looks_for_the_key_in_the_frontmatter() -> None:
    text = document(body="generated:\n  - role: generated\n")
    out = ai.stamp_text(text, [entry()])
    assert out.startswith(
        "---\n" + HEAD + "generated:\n  - role: generated\n    harness"
    )


def test_stamp_text_drops_invalid_entries_and_stamps_nothing_for_none() -> None:
    text = document()
    assert ai.stamp_text(text, []) == text
    assert ai.stamp_text(text, [entry(model="nope")]) == text
    assert ai.stamp_text(text, [{"role": "generated"}]) == text
    out = ai.stamp_text(text, [entry(model="nope"), entry()])
    assert out == document("generated:", *listing(entry()))


def test_stamp_text_puts_generated_before_assisted() -> None:
    assisted = entry(role="assisted", model="openai:gpt-5.6-sol", harness="codex")
    out = ai.stamp_text(document(), [assisted, entry()])
    assert out == document("generated:", *listing(entry(), assisted))


def test_stamp_text_handles_repeated_contributors_in_one_call() -> None:
    out = ai.stamp_text(document(), [entry(effort="low"), entry(at=AT2, effort="high")])
    assert out == document("generated:", *listing(entry(at=AT2, effort="high")))


def test_stamp_text_is_idempotent() -> None:
    entries = [
        entry(effort="high", surface="cli"),
        entry(role="assisted", harness="codex", model="openai:gpt-5.6-sol"),
    ]
    once = ai.stamp_text(document(), entries)
    assert ai.stamp_text(once, entries) == once
    assert once != document()


def test_stamp_text_accepts_any_iterable() -> None:
    out = ai.stamp_text(document(), iter([entry()]))
    assert out == document("generated:", *listing(entry()))


def hashed() -> st.SearchStrategy[str]:
    return st.from_regex(ai.DOC_PATTERNS["session"], fullmatch=True)


def entries() -> st.SearchStrategy[Entry]:
    when = st.datetimes(
        min_value=__import__("datetime").datetime(2000, 1, 1),
        max_value=__import__("datetime").datetime(2099, 12, 31),
    ).map(lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return st.fixed_dictionaries(
        {
            "role": st.sampled_from(["generated", "assisted"]),
            "harness": st.from_regex(ai.DOC_PATTERNS["harness"], fullmatch=True),
            "model": st.from_regex(ai.DOC_PATTERNS["model"], fullmatch=True),
            "at": when,
        },
        optional={
            "effort": st.from_regex(ai.DOC_PATTERNS["effort"], fullmatch=True),
            "surface": st.from_regex(ai.DOC_PATTERNS["surface"], fullmatch=True),
            "session": hashed(),
            "subagent": hashed(),
        },
    )


@settings(deadline=None)
@given(
    first=st.lists(entries(), max_size=4),
    second=st.lists(entries(), max_size=4),
    tail=st.sampled_from(["", VERIFIED]),
    body=st.binary(max_size=40),
)
def test_stamp_text_preserves_bytes_and_validates(
    first: list[Entry], second: list[Entry], tail: str, body: bytes
) -> None:
    text = "---\n" + HEAD + tail + "---\n" + body.decode(errors="surrogateescape")
    once = ai.stamp_text(text, first)
    twice = ai.stamp_text(once, second)
    for out in (once, twice):
        assert out.startswith("---\n" + HEAD + tail)
        assert out.endswith("---\n" + body.decode(errors="surrogateescape"))
        assert out.encode(errors="surrogateescape").endswith(b"---\n" + body)
        # Each stamp replaces the list; an empty one leaves it as it was.
        latest = first if out is once or not second else second
        keys = {ai.contributor_key(e) for e in latest}
        front = front_of(out)
        assert len(cast("list[object]", front.get("generated", []))) == len(keys)
    assert ai.stamp_text(twice, second) == twice
    assert ai.stamp_text(once, first) == once
    for out in (once, twice):
        block = (
            out.split("generated:\n")[-1].split("---\n")[0]
            if "generated:" in out
            else ""
        )
        if block and not tail:
            parsed = ai.parse_entries(block.splitlines())
            assert parsed is not None
            assert all(ai.valid_entry(p.fields) for p in parsed)


# ---- receipts to contributors


def edit_by(who: ai.Ident, path: str, ts: float, repo: Path | None = None) -> None:
    info = ai.repo_info(repo)
    assert info is not None
    name = f"{path.replace('/', '_')}-{who.root[-4:]}-{who.subagent}-{ts}.json"
    receipt = make_receipt(path, who, None, ts)
    ai.write_json(info.interim / "edits" / ai.hx(who.root) / name, receipt)


MAIN = ai.Ident(
    SESSION_HASH, "claude-code", None, "anthropic:claude-opus-5-5", "high", "cli"
)
WRITER = ai.Ident(
    SESSION_HASH, "claude-code", AGENT, "anthropic:claude-haiku-5-5", "low", "cli"
)
STAMP_AT = ai.utc(1_800_000_000)


def test_utc_formats_whole_seconds() -> None:
    assert ai.utc(0) == "1970-01-01T00:00:00Z"
    assert ai.utc(1_800_000_000.9) == "2027-01-15T08:00:00Z"


def test_contributor_of() -> None:
    assert ai.contributor_of(None, "generated", AT1) is None
    assert ai.contributor_of(ai.Ident(ROOT, "codex"), "generated", AT1) is None
    got = ai.contributor_of(MAIN, "assisted", AT1)
    assert got == {
        "role": "assisted",
        "effort": "high",
        "harness": "claude-code",
        "model": "anthropic:claude-opus-5-5",
        "session": SESSION_HASH,
        "surface": "cli",
        "at": AT1,
    }
    assert got is not None
    assert ai.valid_entry(got)


def test_keep_latest() -> None:
    group: dict[ai.ContributorKey, Entry] = {}
    ai.keep_latest(group, None)
    assert group == {}
    ai.keep_latest(group, entry(at=AT2, effort="a"))
    ai.keep_latest(group, entry(at=AT1, effort="b"))
    assert list(group.values()) == [entry(at=AT2, effort="a")]
    ai.keep_latest(group, entry(at=AT2, effort="c"))
    assert list(group.values()) == [entry(at=AT2, effort="c")]
    ai.keep_latest(group, entry(model="anthropic:other"))
    assert len(group) == 2


def committed_as(who: ai.Ident, root: ai.Ident | None = None) -> ai.Committer:
    return ai.Committer(ai.CLAUDE, who, root or who)


def test_delegating_root_prefers_the_committer_of_that_root(repo: Path) -> None:
    root = ai.Ident(SESSION_HASH, "claude-code:9", None, "anthropic:root-model")
    committer = committed_as(MAIN, root)
    assert ai.delegating_root(WRITER, committer, repo) is root
    foreign = ai.Ident(OTHER, "claude-code", AGENT)
    rebuilt = ai.delegating_root(foreign, committer, repo)
    assert rebuilt is not None
    assert rebuilt.root == OTHER


def test_delegating_root_rebuilds_a_claude_root_from_its_record(repo: Path) -> None:
    ai.write_record(
        SESSION_HASH,
        {"version": "3.1", "model": "claude-opus-5-5", "effort": "max"},
        repo,
    )
    got = ai.delegating_root(WRITER, None, repo)
    assert got == ai.Ident(
        SESSION_HASH, "claude-code:3.1", None, "anthropic:claude-opus-5-5", "max", "cli"
    )
    versioned = ai.replace(WRITER, root=OTHER, harness="claude-code:2.0")
    again = ai.delegating_root(versioned, None, repo)
    assert again is not None
    assert again.harness == "claude-code:2.0"
    unversioned = ai.delegating_root(ai.replace(WRITER, root=OTHER), None, repo)
    assert unversioned is not None
    assert unversioned.harness == "claude-code"
    assert unversioned.model is None


def test_delegating_root_reads_the_record_in_the_given_directory(
    repo: Path, plain: Path
) -> None:
    ai.write_record(SESSION_HASH, {"version": "3.1"}, repo)
    got = ai.delegating_root(WRITER, None, repo)
    assert got is not None
    assert got.harness == "claude-code:3.1"
    assert plain.is_dir()


def test_delegating_root_knows_only_claude(repo: Path) -> None:
    for harness in ("codex", "antigravity:1.4"):
        child = ai.Ident(SESSION_HASH, harness, AGENT)
        assert ai.delegating_root(child, None, repo) is None
    assert ai.delegating_root(ai.Ident(SESSION_HASH, "claude-code"), None, repo)


def receipt_of(who: ai.Ident, path: str, ts: float) -> Json:
    return make_receipt(path, who, None, ts)


def test_stamp_entries_one_agent_latest_edit_wins(repo: Path) -> None:
    receipts = [
        receipt_of(MAIN, "d.md", 1_800_000_000),
        receipt_of(MAIN, "d.md", 1_800_000_100),
    ]
    got = ai.stamp_entries(receipts, None, 0, repo)
    assert got == [
        {
            "role": "generated",
            "effort": "high",
            "harness": "claude-code",
            "model": "anthropic:claude-opus-5-5",
            "session": SESSION_HASH,
            "surface": "cli",
            "at": ai.utc(1_800_000_100),
        }
    ]
    receipts.reverse()
    assert ai.stamp_entries(receipts, None, 0, repo) == got


def test_stamp_entries_writer_subagent_makes_its_root_assisted(repo: Path) -> None:
    committer = committed_as(MAIN, ai.replace(MAIN, harness="claude-code:2.1"))
    receipts = [receipt_of(WRITER, "d.md", 1_800_000_000)]
    got = ai.stamp_entries(receipts, committer, 0, repo)
    assert [(e["role"], e.get("subagent"), e["harness"]) for e in got] == [
        ("generated", AGENT, "claude-code"),
        ("assisted", None, "claude-code:2.1"),
    ]
    assert all(e["at"] == ai.utc(1_800_000_000) for e in got)


def test_stamp_entries_root_that_wrote_too_is_not_assisted(repo: Path) -> None:
    receipts = [
        receipt_of(WRITER, "d.md", 1_800_000_000),
        receipt_of(MAIN, "d.md", 1_800_000_500),
    ]
    got = ai.stamp_entries(receipts, committed_as(MAIN), 0, repo)
    assert [(e["role"], e.get("subagent")) for e in got] == [
        ("generated", AGENT),
        ("generated", None),
    ]
    assert [e["at"] for e in got] == [ai.utc(1_800_000_000), ai.utc(1_800_000_500)]


def test_stamp_entries_sorts_by_time_and_only_subagents_get_roots(repo: Path) -> None:
    other = ai.replace(MAIN, root=OTHER, model="anthropic:z")
    receipts = [
        receipt_of(other, "d.md", 1_800_000_900),
        receipt_of(MAIN, "d.md", 1_800_000_100),
    ]
    got = ai.stamp_entries(receipts, None, 0, repo)
    assert [e["session"] for e in got] == [SESSION_HASH, OTHER]


def test_stamp_entries_enriches_receipts_from_the_committer(repo: Path) -> None:
    committer = committed_as(ai.replace(MAIN, harness="claude-code:2.1.316"))
    got = ai.stamp_entries(
        [receipt_of(MAIN, "d.md", 1_800_000_000)], committer, 0, repo
    )
    assert got[0]["harness"] == "claude-code:2.1.316"


def test_stamp_entries_skips_agents_without_a_model(repo: Path) -> None:
    unknown = ai.replace(MAIN, model=None)
    assert ai.stamp_entries([receipt_of(unknown, "d.md", 1)], None, 0, repo) == []
    blind = ai.replace(WRITER, model=None)
    got = ai.stamp_entries(
        [receipt_of(blind, "d.md", 1_800_000_000)], committed_as(MAIN), 0, repo
    )
    assert [(e["role"], e.get("subagent")) for e in got] == [("assisted", None)]


def test_stamp_entries_falls_back_to_the_committer_at_commit_time(repo: Path) -> None:
    committer = committed_as(ai.replace(MAIN, harness="claude-code:2.1"))
    got = ai.stamp_entries([], committer, 1_800_000_000, repo)
    assert [(e["role"], e["at"], e["harness"]) for e in got] == [
        ("generated", STAMP_AT, "claude-code:2.1")
    ]
    assert ai.stamp_entries([], None, 1_800_000_000, repo) == []
    nameless = committed_as(ai.replace(MAIN, model=None))
    assert ai.stamp_entries([], nameless, 1_800_000_000, repo) == []
    # With receipts, the committer is not added for a file it did not edit.
    mine = [receipt_of(ai.replace(MAIN, root=OTHER), "d.md", 1_800_000_000)]
    assert [e["session"] for e in ai.stamp_entries(mine, committer, 0, repo)] == [OTHER]


# ---- git side


def stage_doc(repo: Path, rel: str, text: str | bytes | None = None) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    content = document() if text is None else text
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8", newline="")
    else:
        target.write_bytes(content)
    run_git(repo, "add", "--", rel)


def staged_text(repo: Path, rel: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f":{rel}"],
        check=True,
        capture_output=True,
        env=hook_env(),
    ).stdout


def test_all_edits_spans_roots_and_skips_consumed(repo: Path) -> None:
    edit_by(MAIN, "a.md", 1)
    edit_by(ai.replace(MAIN, root=OTHER), "b.md", 2)
    info = ai.repo_info()
    assert info is not None
    gone = info.interim / "edits" / ai.hx(ROOT) / "gone.json"
    ai.write_json(gone, {**make_receipt("c.md", MAIN), "consumed": True})
    (info.interim / "edits" / ai.hx(ROOT) / "junk.json").write_text(
        "{", encoding="utf-8"
    )
    (info.interim / "edits" / "stray.json").write_text("{}", encoding="utf-8")
    assert sorted(str(r["path"]) for r in ai.all_edits(info)) == ["a.md", "b.md"]
    assert ai.all_edits(ai.Repo(repo, repo / "nowhere")) == []


def test_git_bytes_returns_raw_stdout_and_feeds_stdin(repo: Path) -> None:
    raw = b"\xff\n\n"
    blob = (
        ai.git_bytes("hash-object", "-w", "--stdin", cwd=repo, data=raw)
        .decode()
        .strip()
    )
    assert ai.git_bytes("cat-file", "blob", blob, cwd=repo) == raw
    assert ai.git_bytes("nonsense-command", cwd=repo) == b""


def test_decode_round_trips_any_bytes() -> None:
    raw = b"a\xff\xfe\x80b\n"
    assert ai.decode(raw).encode(errors="surrogateescape") == raw


def test_staged_docs_picks_stampable_markdown(repo: Path) -> None:
    for rel in (
        "docs/a.md",
        "docs/sp ace/b c.md",
        "docs/d.MD",
        "a/b/c/deep.md",
        "docs/guide/index.md",
        "docs/log.md",
        "docs/AGENTS.md",
        "sub/CLAUDE.md",
        "README.md",
        "docs/notes.txt",
        "docs/data.md.bak",
        "docs/mdfile",
        "tools/testdata/x.md",
        "testdata/y.md",
        "a/testdata/b/z.md",
    ):
        stage_doc(repo, rel, "x\n")
    got = ai.staged_docs(repo)
    assert sorted(d[0] for d in got) == [
        "a/b/c/deep.md",
        "docs/a.md",
        "docs/d.MD",
        "docs/sp ace/b c.md",
    ]
    for rel, mode, blob in got:
        assert mode == "100644"
        assert blob == run_git(repo, "rev-parse", f":{rel}").strip()


def test_staged_docs_modes_and_changes(repo: Path) -> None:
    stage_doc(repo, "docs/run.md", "x\n")
    run_git(repo, "update-index", "--chmod=+x", "docs/run.md")
    stage_doc(repo, "docs/keep.md", "k\n")
    stage_doc(repo, "docs/gone.md", "g\n")
    stage_doc(repo, "docs/old.md", "same\nbody\nlines\nhere\n")
    commit(repo)
    stage_doc(repo, "docs/keep.md", "changed\n")
    run_git(repo, "rm", "-q", "docs/gone.md")
    run_git(repo, "mv", "docs/old.md", "docs/new.md")
    got = {rel: mode for rel, mode, _ in ai.staged_docs(repo)}
    assert got == {"docs/keep.md": "100644", "docs/new.md": "100644"}
    stage_doc(repo, "docs/run.md", "x2\n")
    run_git(repo, "update-index", "--chmod=+x", "docs/run.md")
    assert {r: m for r, m, _ in ai.staged_docs(repo)}["docs/run.md"] == "100755"


def test_staged_docs_empty_without_staged_changes(repo: Path) -> None:
    assert ai.staged_docs(repo) == []


def test_stamp_document_rewrites_index_and_worktree_together(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage_doc(repo, "docs/a.md")
    (doc,) = ai.staged_docs(repo)
    ai.stamp_document(repo, doc, [entry(effort="high")])
    stamped = document("generated:", *listing(entry(effort="high")))
    assert (repo / "docs" / "a.md").read_text(encoding="utf-8") == stamped
    assert staged_text(repo, "docs/a.md") == stamped.encode()
    assert run_git(repo, "status", "--porcelain").startswith("A  docs/a.md")
    assert capsys.readouterr().err == "agent_identity: stamped docs/a.md\n"
    (again,) = ai.staged_docs(repo)
    ai.stamp_document(repo, again, [entry(effort="high")])
    assert capsys.readouterr().err == ""
    assert staged_text(repo, "docs/a.md") == stamped.encode()


def test_stamp_document_keeps_the_file_mode(repo: Path) -> None:
    stage_doc(repo, "docs/run.md")
    run_git(repo, "update-index", "--chmod=+x", "docs/run.md")
    (doc,) = ai.staged_docs(repo)
    ai.stamp_document(repo, doc, [entry()])
    assert run_git(repo, "ls-files", "-s", "docs/run.md").startswith("100755 ")
    assert "generated:" in staged_text(repo, "docs/run.md").decode()


def test_stamp_document_preserves_unstaged_work_tree_changes(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    path = repo / "docs" / "a.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "unstaged tail\n", encoding="utf-8"
    )
    (doc,) = ai.staged_docs(repo)
    ai.stamp_document(repo, doc, [entry()])
    stamped = document("generated:", *listing(entry()))
    assert staged_text(repo, "docs/a.md") == stamped.encode()
    assert path.read_text(encoding="utf-8") == stamped + "unstaged tail\n"


def test_stamp_document_keeps_the_work_tree_when_it_cannot_be_stamped(
    repo: Path,
) -> None:
    stage_doc(repo, "docs/a.md")
    path = repo / "docs" / "a.md"
    path.write_text("no frontmatter here\n", encoding="utf-8")
    (doc,) = ai.staged_docs(repo)
    before = path.stat().st_mtime_ns
    ai.stamp_document(repo, doc, [entry()])
    assert path.read_text(encoding="utf-8") == "no frontmatter here\n"
    assert path.stat().st_mtime_ns == before
    assert "generated:" in staged_text(repo, "docs/a.md").decode()


def test_stamp_document_without_a_work_tree_file(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    (repo / "docs" / "a.md").unlink()
    (doc,) = ai.staged_docs(repo)
    ai.stamp_document(repo, doc, [entry()])
    assert not (repo / "docs" / "a.md").exists()
    assert "generated:" in staged_text(repo, "docs/a.md").decode()


def test_stamp_document_preserves_arbitrary_bytes(repo: Path) -> None:
    body = b"\xff\xfe\r\nbinary \x00 tail without newline"
    stage_doc(repo, "docs/a.md", b"---\n" + HEAD.encode() + b"---\n" + body)
    (doc,) = ai.staged_docs(repo)
    ai.stamp_document(repo, doc, [entry()])
    stamped = staged_text(repo, "docs/a.md")
    assert stamped.endswith(b"---\n" + body)
    assert (repo / "docs" / "a.md").read_bytes() == stamped


def test_stamp_document_leaves_unstampable_files_alone(repo: Path) -> None:
    stage_doc(repo, "docs/a.md", "plain\n")
    (doc,) = ai.staged_docs(repo)
    ai.stamp_document(repo, doc, [entry()])
    assert staged_text(repo, "docs/a.md") == b"plain\n"
    assert (repo / "docs" / "a.md").read_bytes() == b"plain\n"


@pytest.mark.usefixtures("repo")
def test_refresh_codex_scans_only_for_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ai.Repo, object]] = []

    def spy(root: str, info: ai.Repo, env: object) -> None:
        calls.append((root, info, env))

    monkeypatch.setattr(ai, "codex_scan", spy)
    info = ai.repo_info()
    assert info is not None
    env = {"CODEX_SESSION_ID": ROOT_THREAD}
    ai.refresh_codex(ai.Committer(ai.CODEX, MAIN, MAIN), info, env)
    assert calls == [(ROOT_THREAD, info, env)]
    ai.refresh_codex(committed_as(MAIN), info, env)
    ai.refresh_codex(ai.Committer(ai.ANTIGRAVITY, MAIN, MAIN), info, env)
    assert len(calls) == 1


def test_cmd_stamp_docs_outside_a_repository(plain: Path) -> None:
    assert ai.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    assert not list(plain.iterdir())


def test_cmd_stamp_docs_stamps_a_human_commit_from_receipts(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    stage_doc(repo, "docs/b.md")
    edit_by(MAIN, "docs/a.md", 1_800_000_000)
    assert ai.cmd_stamp_docs({}) == 0
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert staged_text(repo, "docs/b.md") == document().encode()
    stamped = front_of(staged_text(repo, "docs/a.md").decode())
    assert stamped["generated"] == [
        {
            "role": "generated",
            "harness": "claude-code",
            "model": "anthropic:claude-opus-5-5",
            "effort": "high",
            "surface": "cli",
            "session": SESSION_HASH,
            "at": STAMP_AT,
        }
    ]
    # Receipts are read, not consumed.
    assert len(receipts_of(repo, SESSION_HASH)) == 1


def test_cmd_stamp_docs_leaves_testdata_fixtures_byte_identical(repo: Path) -> None:
    """Regression: a staged fixture under testdata/ got a ``generated`` block."""
    rel = "tools/interim/testdata/pull_request/passing-draft.md"
    stage_doc(repo, rel)
    before = (repo / rel).read_bytes()
    edit_by(MAIN, rel, 1_800_000_000)
    assert ai.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    assert staged_text(repo, rel) == before
    assert (repo / rel).read_bytes() == before


def test_cmd_stamp_docs_credits_a_shell_edit_to_the_committer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai.write_record(
        SESSION_HASH, {"model": "claude-opus-5-5", "version": "2.1.316"}, repo
    )
    stage_doc(repo, "docs/a.md")
    monkeypatch.setattr(ai.time, "time", lambda: 1_800_000_000.0)
    assert ai.cmd_stamp_docs(CLAUDE_COMMIT_ENV) == 0
    (stamped,) = cast(
        "list[Entry]", front_of(staged_text(repo, "docs/a.md").decode())["generated"]
    )
    assert stamped["at"] == STAMP_AT
    assert stamped["harness"] == "claude-code:2.1.316"
    assert stamped["session"] == SESSION_HASH


def test_cmd_stamp_docs_scans_codex_rollouts(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(id=ROOT_THREAD, cwd=str(repo)),
            turn("gpt-5.6-sol", "low"),
            patch_call("docs/a.md"),
        ],
    )
    stage_doc(repo, "docs/a.md")
    env = {"CODEX_HOME": str(home), "CODEX_SESSION_ID": ROOT_THREAD}
    assert ai.cmd_stamp_docs(env) == 0
    (stamped,) = cast(
        "list[Entry]", front_of(staged_text(repo, "docs/a.md").decode())["generated"]
    )
    assert stamped["harness"] == "codex"
    assert stamped["model"] == "openai:gpt-5.6-sol"
    assert stamped["effort"] == "low"
    assert stamped["session"] == ai.sha256_64(ROOT_THREAD)


def test_main_dispatches_stamp_docs(repo: Path) -> None:
    stage_doc(repo, "docs/a.md")
    edit_by(MAIN, "docs/a.md", 1_800_000_000)
    assert ai.main(["stamp-docs", "ignored"], {}) == 0
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert "stamp-docs" in ai.USAGE


# ------------------------------------------------------- surviving-mutant pins


def test_ancestry_recognizes_claude_by_its_first_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ps(
        monkeypatch,
        {
            "10": ("20", "/bin/claude --a b"),
            "20": ("30", "claude"),
            "30": ("1", "login"),
        },
    )
    assert ai.ancestry(10) == ["/bin/claude --a b"]
    assert ai.ancestry(20) == ["claude"]


def test_enrich_compares_only_the_harness_slug() -> None:
    cm = committer_of(ai.Ident(ROOT, "claude-code:2.1:x", None, "anthropic:m"))
    got = ai.enrich(ai.Ident(ROOT, "claude-code"), cm)
    assert got.harness == "claude-code:2.1:x"


def test_parse_entries_a_one_line_item_ends_on_the_next_line() -> None:
    (only,) = ai.parse_entries(["  - role: generated"]) or []
    assert (only.first, only.last) == (0, 1)
    first, second = ai.parse_entries(["- role: generated", "- role: assisted"]) or []
    assert (first.first, first.last, second.first, second.last) == (0, 1, 1, 2)


def test_stamp_text_closing_fence_ends_at_a_space_or_tab_only() -> None:
    for tail in ("\r", "X", "\f", "\v"):
        text = f"---\ntype: x\n---{tail}\nbody\n---\n"
        expected = f"---\ntype: x\n---{tail}\nbody\n" + "\n".join(
            ["generated:", *listing(entry())]
        )
        assert ai.stamp_text(text, [entry()]) == expected + "\n---\n"


def test_stamp_text_finds_generated_as_the_first_key() -> None:
    text = "---\n" + "\n".join(["generated:", *listing(entry())]) + "\ntype: x\n---\n"
    other = entry(model="openai:gpt-5.6-sol", harness="codex")
    out = ai.stamp_text(text, [other])
    assert (
        out == "---\n" + "\n".join(["generated:", *listing(other)]) + "\ntype: x\n---\n"
    )


def test_stamp_entries_reads_the_root_record_from_the_given_base(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai.write_record(SESSION_HASH, {"model": "claude-opus-5-5", "version": "3.1"}, repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    child = ai.replace(WRITER, root=SESSION_HASH, model="anthropic:claude-haiku-5-5")
    got = ai.stamp_entries([receipt_of(child, "d.md", 1_800_000_000)], None, 0, repo)
    assert [(e["role"], e["model"], e["harness"]) for e in got] == [
        ("generated", "anthropic:claude-haiku-5-5", "claude-code"),
        ("assisted", "anthropic:claude-opus-5-5", "claude-code:3.1"),
    ]


class RunSpy:
    """Records subprocess.run calls and passes them through."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        real = subprocess.run

        def run(command: list[str], **kwargs: object) -> object:
            self.calls.append((command, kwargs))
            return real(command, **kwargs)  # type: ignore[call-overload]

        monkeypatch.setattr(ai.subprocess, "run", run)


def test_git_bytes_passes_its_options_to_subprocess(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = RunSpy(monkeypatch)
    ai.git_bytes("hash-object", "--stdin", cwd=repo, data=b"x")
    assert spy.calls == [
        (
            ["git", "hash-object", "--stdin"],
            {"capture_output": True, "cwd": repo, "input": b"x", "check": False},
        )
    ]


def test_git_side_commands_run_in_the_given_repository_not_the_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_doc(repo, "docs/a.md")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert [d[0] for d in ai.staged_docs(repo)] == ["docs/a.md"]
    (doc,) = ai.staged_docs(repo)
    ai.stamp_document(repo, doc, [entry()])
    assert "generated:" in staged_text(repo, "docs/a.md").decode()
    assert (repo / "docs" / "a.md").read_text(encoding="utf-8").count("generated:") == 1


def test_codex_scan_credits_a_forks_replayed_patches_to_the_parent_only(
    repo: Path, tmp_path: Path
) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        ROOT_THREAD,
        [meta(id=ROOT_THREAD, session_id=ROOT_THREAD), patch_call("parent.md")],
    )
    write_rollout(home, CHILD_THREAD, fork_lines(4))
    info = ai.repo_info()
    assert info is not None
    ai.codex_scan(ROOT_THREAD, info, {"CODEX_HOME": str(home)})
    got = sorted(
        (str(r["path"]), str(r["subagent"]))
        for r in receipts_of(repo, ai.sha256_64(ROOT_THREAD))
    )
    assert got == [
        ("child.md", ai.sha256_64(CHILD_THREAD)),
        ("parent.md", "None"),
    ]


def test_check_message_orders_attributes_alphabetically() -> None:
    ok = (
        "Generated-by: LLM effort=high;harness=codex;model=openai:m;session="
        + SESSION_HASH
    )
    assert ai.check_value(ok.removeprefix("Generated-by: ")) is None
    swapped = "LLM harness=codex;effort=high"
    assert ai.check_value(swapped) == "attributes out of order or duplicated"


# -------------------------------------------------------------- Codex hooks

HOOKS_JSON = Path(__file__).parents[2] / ".codex" / "hooks.json"


def hook_data(name: str, repo: Path, rollout: Path | None, **kw: object) -> Json:
    data = stdin_fixture(name, cwd=str(repo), **kw)
    if rollout:
        data["transcript_path"] = str(rollout)
    return data


def hook_rollouts(home: Path, repo: Path) -> tuple[Path, Path]:
    """(root rollout, child rollout) shaped like Codex 0.166.0's."""
    root = write_rollout(
        home,
        ROOT_THREAD,
        [
            meta(
                id=ROOT_THREAD,
                session_id=ROOT_THREAD,
                cwd=str(repo),
                source="exec",
                cli_version="0.166.0",
            ),
            turn("gpt-5.6-sol", "high"),
        ],
    )
    child = write_rollout(
        home,
        CHILD_THREAD,
        [
            meta(
                id=CHILD_THREAD,
                session_id=ROOT_THREAD,
                cwd=str(repo),
                source={"subagent": 1},
                cli_version="0.166.0",
            ),
            turn("gpt-5.6-terra", "low"),
        ],
    )
    return root, child


def run_hook(monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    feed(monkeypatch, data)
    assert ai.cmd_record("codex", {}) == 0


def interim_of(repo: Path) -> Path:
    return repo / ".git" / "ascm-interim"


def test_codex_sessionstart_records_the_root_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    run_hook(monkeypatch, hook_data("codex-hook-sessionstart.json", repo, root))
    assert ai.load_record(ai.sha256_64(ROOT_THREAD)) == {
        "harness": "codex:0.166.0",
        "root": ai.sha256_64(ROOT_THREAD),
        "model": "openai:gpt-5.6-sol",
        "effort": "high",
        "surface": "exec",
        "transcript": str(root),
    }


def test_codex_subagentstart_records_the_executing_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, child = hook_rollouts(tmp_path / "codex", repo)
    run_hook(monkeypatch, hook_data("codex-hook-subagentstart.json", repo, child))
    # Keyed by the subagent; the model is stdin's, effort the rollout's.
    assert ai.load_record(ai.sha256_64(CHILD_THREAD)) == {
        "harness": "codex:0.166.0",
        "root": ai.sha256_64(ROOT_THREAD),
        "agent_type": "default",
        "model": "openai:gpt-5.6-luna",
        "effort": "low",
        "transcript": str(child),
    }
    assert ai.load_record(ai.sha256_64(ROOT_THREAD)) == {}


def test_codex_hook_ident_fallbacks(repo: Path, tmp_path: Path) -> None:
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    start = hook_data("codex-hook-sessionstart.json", repo, root)
    ident = ai.codex_hook_ident(ROOT_THREAD, start, {})
    assert ident.model == "openai:gpt-5.6-sol"
    # Without a usable stdin model the rollout's turn context is used.
    no_model = {k: v for k, v in start.items() if k != "model"}
    assert ai.codex_hook_ident(ROOT_THREAD, no_model, {}).model == (
        "openai:gpt-5.6-sol"
    )
    unusable = {**start, "model": "<synthetic>"}
    assert ai.codex_hook_ident(ROOT_THREAD, unusable, {}).model == (
        "openai:gpt-5.6-sol"
    )
    # Without a rollout only stdin's model is known.
    for gone in (None, str(tmp_path / "missing.jsonl"), 3):
        bare = {**start, "transcript_path": gone}
        assert ai.codex_hook_ident(ROOT_THREAD, bare, {}) == ai.Ident(
            ai.sha256_64(ROOT_THREAD), "codex", None, "openai:gpt-5.6-sol"
        )
    assert ai.codex_hook_ident(ROOT_THREAD, {}, {}) == ai.Ident(
        ai.sha256_64(ROOT_THREAD), "codex"
    )


def test_codex_apply_patch_writes_an_edit_time_receipt(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    expected = run_git(repo, "hash-object", "root.txt").strip()
    data = hook_data("codex-hook-posttooluse-apply-patch.json", repo, root)
    run_hook(monkeypatch, data)
    # The file changes after the hook: the receipt keeps the edit-time blob.
    (repo / "root.txt").write_text("later\n", encoding="utf-8")
    (got,) = receipts_of(repo, ai.sha256_64(ROOT_THREAD))
    assert got["path"] == "root.txt"
    assert got["blob"] == expected
    assert run_git(repo, "cat-file", "blob", expected) == "root\n"
    assert got["subagent"] is None
    assert got["harness"] == "codex:0.166.0"
    assert got["model"] == "openai:gpt-5.6-sol"
    assert got["effort"] == "high"
    assert got["surface"] == "exec"
    assert ROOT_THREAD not in json.dumps(got)


def test_codex_apply_patch_in_a_subagent_is_attributed_to_it(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, child = hook_rollouts(tmp_path / "codex", repo)
    (repo / "sub.txt").write_text("sub\n", encoding="utf-8")
    data = hook_data("codex-hook-posttooluse-subagent-apply-patch.json", repo, child)
    run_hook(monkeypatch, data)
    (got,) = receipts_of(repo, ai.sha256_64(ROOT_THREAD))
    assert got["root"] == ai.sha256_64(ROOT_THREAD)
    assert got["subagent"] == ai.sha256_64(CHILD_THREAD)
    assert got["model"] == "openai:gpt-5.6-luna"
    assert got["effort"] == "low"
    assert got["surface"] is None


def test_codex_apply_patch_covers_every_patch_operation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = (
        "*** Begin Patch\n*** Add File: a.txt\n+a\n*** Update File: b.txt\n"
        "*** Move to: sub/c.txt\n@@\n-b\n+c\n*** Delete File: gone.txt\n"
        "*** End Patch"
    )
    for name in ("a.txt", "sub/c.txt"):
        (repo / name).parent.mkdir(exist_ok=True)
        (repo / name).write_text(name, encoding="utf-8")
    data = hook_data("codex-hook-posttooluse-apply-patch.json", repo, None)
    data["tool_input"] = {"command": patch}
    run_hook(monkeypatch, data)
    got = {str(r["path"]): r for r in receipts_of(repo, ai.sha256_64(ROOT_THREAD))}
    assert sorted(got) == ["a.txt", "b.txt", "gone.txt", "sub/c.txt"]
    assert got["a.txt"]["blob"] == run_git(repo, "hash-object", "a.txt").strip()
    assert got["b.txt"]["blob"] is None
    assert got["gone.txt"]["blob"] is None


def test_blob_of_only_stores_the_object_on_request(repo: Path) -> None:
    (repo / "f.txt").write_text("blob me\n", encoding="utf-8")
    blob = run_git(repo, "hash-object", "f.txt").strip()
    assert ai.blob_of(repo, "f.txt") == blob
    missing = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", blob],
        check=False,
        capture_output=True,
        env=hook_env(),
    )
    assert missing.returncode != 0
    assert ai.blob_of(repo, "f.txt", store=True) == blob
    assert run_git(repo, "cat-file", "blob", blob) == "blob me\n"


@pytest.mark.parametrize(
    "mutate",
    [
        {"hook_event_name": "PreToolUse"},
        {"hook_event_name": "UserPromptSubmit"},
        {"hook_event_name": "SessionEnd"},
        {"tool_name": "Bash"},
        {"tool_name": "apply_patch_x"},
        {"session_id": None},
        {"session_id": 7},
        {"session_id": ""},
    ],
)
def test_codex_ignores_other_events_and_tools(
    repo: Path, monkeypatch: pytest.MonkeyPatch, mutate: Json
) -> None:
    data = hook_data("codex-hook-posttooluse-apply-patch.json", repo, None, **mutate)
    run_hook(monkeypatch, data)
    assert not interim_of(repo).exists()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[1, 2]",
        "{}",
        '{"hook_event_name": "PostToolUse", "tool_name": "apply_patch"}',
        (
            '{"session_id": "s", "hook_event_name": "PostToolUse",'
            ' "tool_name": "apply_patch", "tool_input": "oops", "tool_use_id": 3,'
            ' "cwd": 4, "transcript_path": 5}'
        ),
        (
            '{"session_id": "s", "hook_event_name": "PostToolUse",'
            ' "tool_name": "apply_patch", "tool_input": {"command": 9}}'
        ),
    ],
)
def test_codex_hook_fails_open_on_bad_stdin(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str,
) -> None:
    run_hook(monkeypatch, raw)
    assert capsys.readouterr().out == ""
    assert not list(interim_of(repo).glob("edits/**/*.json"))


def test_codex_hook_outside_a_repository(
    plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (plain / "root.txt").write_text("root\n", encoding="utf-8")
    run_hook(
        monkeypatch, hook_data("codex-hook-posttooluse-apply-patch.json", plain, None)
    )
    run_hook(monkeypatch, hook_data("codex-hook-sessionstart.json", plain, None))
    assert [p.name for p in plain.iterdir()] == ["root.txt"]


@pytest.mark.parametrize("hooked", ["root", "child"])
def test_codex_scan_takes_nothing_from_a_hooked_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hooked: str
) -> None:
    home = tmp_path / "codex"
    root, child = hook_rollouts(home, repo)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    (repo / "sub.txt").write_text("sub\n", encoding="utf-8")
    # Only one thread has a hook record; the other never fired a hook.
    start, rollout = (
        ("codex-hook-sessionstart.json", root)
        if hooked == "root"
        else ("codex-hook-subagentstart.json", child)
    )
    run_hook(monkeypatch, hook_data(start, repo, rollout))
    for path, name in ((root, "root.txt"), (child, "sub.txt")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(patch_call(name)) + "\n")
    store = ai.repo_info(repo)
    assert store is not None
    checked: list[str] = []
    real = Path.exists

    def spy(self: Path) -> bool:
        checked.append(self.parent.name)
        return real(self)

    monkeypatch.setattr(Path, "exists", spy)
    ai.codex_scan(ROOT_THREAD, store, {"CODEX_HOME": str(home)})
    monkeypatch.undo()
    assert "sessions" in checked
    got = receipts_of(repo, ai.sha256_64(ROOT_THREAD))
    # The hooked thread's rollout patch added nothing; the other's was read.
    expect = ("sub.txt", False) if hooked == "root" else ("root.txt", True)
    assert [(str(r["path"]), r["subagent"] is None) for r in got] == [expect]


def test_trailers_mix_hooked_edits_and_a_rollout_only_thread(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    root, child = hook_rollouts(home, repo)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    run_hook(
        monkeypatch, hook_data("codex-hook-posttooluse-apply-patch.json", repo, root)
    )
    # The child never fired a hook (untrusted, say): only its rollout shows it.
    (repo / "sub.txt").write_text("sub\n", encoding="utf-8")
    with child.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(patch_call("sub.txt")) + "\n")
    run_git(repo, "add", "root.txt", "sub.txt")
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": ROOT_THREAD,
    }
    path = msg_file(tmp_path)
    assert ai.cmd_trailer(str(path), None, env) == 0
    session = ai.sha256_64(ROOT_THREAD)
    assert sorted(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if ": LLM " in line
    ) == [
        (
            "Generated-by: LLM effort=high;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-sol;session={session};surface=exec"
        ),
        (
            "Generated-by: LLM effort=low;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-terra;session={session};"
            f"subagent={ai.sha256_64(CHILD_THREAD)};surface=exec"
        ),
    ]


def test_hooks_json_registers_one_stable_command() -> None:
    hooks = cast("dict[str, list[dict[str, object]]]", loaded(HOOKS_JSON)["hooks"])
    # Appending groups never shifts the identity hooks' trust keys.
    assert list(hooks)[:3] == ["SessionStart", "SubagentStart", "PostToolUse"]
    commands = set()
    for event, groups in hooks.items():
        if event == "PreToolUse":  # the gh pr guard (test_guard_gh_pr.py)
            continue
        group = groups[0]
        if event != "PostToolUse":
            assert len(groups) == 1
        (handler,) = cast("list[dict[str, str]]", group["hooks"])
        assert set(handler) == {"type", "command"}  # no timeout to drift
        assert handler["type"] == "command"
        commands.add(handler["command"])
        assert group.get("matcher") == (
            "apply_patch" if event == "PostToolUse" else None
        )
    (command,) = commands
    assert command.endswith('/tools/interim/agent_identity.py" record codex')
    assert command.startswith("uv run --no-project --quiet --python '>=3.15' python ")


def test_hooks_json_command_works_from_a_subdirectory(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "tools" / "interim").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "tools" / "interim" / "agent_identity.py")
    (repo / "deep" / "dir").mkdir(parents=True)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    command = cast(
        "dict[str, list[dict[str, list[dict[str, str]]]]]", loaded(HOOKS_JSON)["hooks"]
    )["PostToolUse"][0]["hooks"][0]["command"]
    # Swap uv for the running interpreter; the path expression is under test.
    command = command.replace(
        "uv run --no-project --quiet --python '>=3.15' python",
        shlex.quote(sys.executable),
    )
    data = hook_data("codex-hook-posttooluse-apply-patch.json", repo, root)
    done = subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        check=False,
        cwd=repo / "deep" / "dir",
        env=hook_env(),
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert [r["path"] for r in receipts_of(repo, ai.sha256_64(ROOT_THREAD))] == [
        "root.txt"
    ]


@needs_prek
def test_e2e_codex_hooks_then_commit(wired: Path, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    root, child = hook_rollouts(home, wired)
    start = hook_data("codex-hook-sessionstart.json", wired, root)
    record(wired, "codex", start)
    (wired / "root.txt").write_text("root\n", encoding="utf-8")
    record(
        wired,
        "codex",
        hook_data("codex-hook-posttooluse-apply-patch.json", wired, root),
    )
    (wired / "sub.txt").write_text("sub\n", encoding="utf-8")
    sub = hook_data("codex-hook-posttooluse-subagent-apply-patch.json", wired, child)
    record(wired, "codex", sub)
    run_git(wired, "add", "root.txt", "sub.txt")
    env = {
        "CODEX_HOME": str(home),
        "CODEX_SESSION_ID": ROOT_THREAD,
        "CODEX_THREAD_ID": ROOT_THREAD,
    }
    done = git_commit(wired, "feat: both", env)
    assert done.returncode == 0, done.stderr
    session = ai.sha256_64(ROOT_THREAD)
    assert sorted(trailers(wired)) == [
        (
            "Generated-by: LLM effort=high;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-sol;session={session};surface=exec"
        ),
        (
            "Generated-by: LLM effort=low;harness=codex:0.166.0;"
            f"model=openai:gpt-5.6-luna;session={session};"
            f"subagent={ai.sha256_64(CHILD_THREAD)};surface=exec"
        ),
    ]


def test_write_edit_stores_the_blob_only_when_asked(repo: Path) -> None:
    (repo / "f.txt").write_text("f\n", encoding="utf-8")
    blob = run_git(repo, "hash-object", "f.txt").strip()
    ai.write_edit(make_ident(), "f.txt")
    (got,) = receipts_of(repo, ROOT)
    assert got["blob"] == blob
    missing = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", blob],
        check=False,
        capture_output=True,
        env=hook_env(),
    )
    assert missing.returncode != 0
    ai.write_edit(make_ident(), "f.txt", store=True)
    assert run_git(repo, "cat-file", "blob", blob) == "f\n"


def test_rollout_patch_edits_defaults_read_from_the_first_line(
    tmp_path: Path,
) -> None:
    path = write_rollout(tmp_path, CHILD_THREAD, [patch_call("first.txt")])
    assert ai.rollout_patch_edits(path) == (["first.txt"], path.stat().st_size)


def test_codex_hook_ident_takes_the_thread_from_the_hook_not_the_rollout(
    repo: Path, tmp_path: Path
) -> None:
    root, child = hook_rollouts(tmp_path / "codex", repo)
    other = write_rollout(tmp_path / "codex", OTHER_THREAD, [meta(id=OTHER_THREAD)])
    start = hook_data("codex-hook-sessionstart.json", repo, other)
    assert ai.codex_hook_ident(ROOT_THREAD, start, {}).subagent is None
    sub = hook_data("codex-hook-subagentstart.json", repo, other)
    got = ai.codex_hook_ident(ROOT_THREAD, sub, {})
    assert got.subagent == ai.sha256_64(CHILD_THREAD)
    assert (root, child) != (other, other)


def test_codex_hooks_use_the_hook_cwd_not_the_process_cwd(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = hook_rollouts(tmp_path / "codex", repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    run_hook(monkeypatch, hook_data("codex-hook-sessionstart.json", repo, root))
    run_hook(
        monkeypatch, hook_data("codex-hook-posttooluse-apply-patch.json", repo, root)
    )
    assert [r["path"] for r in receipts_of(repo, ai.sha256_64(ROOT_THREAD))] == [
        "root.txt"
    ]
    assert (interim_of(repo) / "sessions").is_dir()
