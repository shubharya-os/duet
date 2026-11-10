"""Codex can build Swift inside its own sandbox.

Real Swift sessions on duet 0.17.0: the Codex side asked for `run_gate` on 17
of 29 turns, and the gate (about 30 minutes a run) took 87% and 76% of the
wall time, because `swift build` could not run under `codex exec --sandbox
workspace-write`. clang's module cache, ~/.cache/clang/ModuleCache, was not
writable ("Operation not permitted"), and with it writable SwiftPM still
failed starting its own sandbox inside Codex's ("sandbox_apply: Operation not
permitted"). With the cache passed as `--add-dir` and SwiftPM run with
`--disable-sandbox`, both build and test work.

The codex here is a fake that writes down its command line; no network.
"""

from pathlib import Path

from fakebin import fake_cli
from duet.adapters.claude_code import ClaudeCodeAdapter
from duet.adapters.codex_cli import CodexCliAdapter
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator

HELP_WITH_ADD_DIR = "Usage: codex exec [OPTIONS] [PROMPT]\n  --add-dir <DIR>  Additional writable directories\n"
HELP_WITHOUT = "Usage: codex exec [OPTIONS] [PROMPT]\n  -C, --cd <DIR>\n"

# Answers `exec --help` with $HELP_TEXT; any other call is a turn, whose
# command line goes to $ARGDUMP, one argument per line.
FAKE_CODEX = r'''
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  printf '%s' "$HELP_TEXT"
  echo help >> "$HELPS"
  exit 0
fi
printf '%s\n' "$@" > "$ARGDUMP"
out=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; esac
  shift
done
printf 'did it' > "$out"
'''


def codex(tmp_path, monkeypatch, help_text=HELP_WITH_ADD_DIR, cache_home=None):
    monkeypatch.setenv("HELP_TEXT", help_text)
    monkeypatch.setenv("HELPS", str(tmp_path / "helps.txt"))
    monkeypatch.setenv("ARGDUMP", str(tmp_path / "args.txt"))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Where clang puts its cache on Windows, which has no XDG: given a cache
    # home, the test stands it in here too.
    monkeypatch.setenv("LOCALAPPDATA", str(cache_home or home / ".cache"))
    if cache_home is None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    agent = CodexCliAdapter(name="chatgpt", cwd=str(workspace))
    agent.bin = fake_cli(tmp_path / "bin", "codex", FAKE_CODEX)
    return agent, home


def added_dirs(tmp_path):
    args = (tmp_path / "args.txt").read_text(encoding="utf-8").splitlines()
    return [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]


def module_cache(base: Path) -> Path:
    return base / "clang" / "ModuleCache"


def test_a_writable_sandbox_can_write_the_clang_module_cache(tmp_path, monkeypatch, codex_help_probe):
    agent, home = codex(tmp_path, monkeypatch)
    cache = module_cache(home / ".cache")
    assert not cache.exists()
    reply = agent.send("build it")
    assert reply.ok, reply.error
    assert added_dirs(tmp_path) == [str(cache)]
    assert cache.is_dir()                    # made for codex, which cannot make it itself


def test_xdg_cache_home_is_where_the_module_cache_is(tmp_path, monkeypatch, codex_help_probe):
    xdg = tmp_path / "xdg-cache"
    agent, home = codex(tmp_path, monkeypatch, cache_home=xdg)
    assert agent.send("build it").ok
    assert added_dirs(tmp_path) == [str(module_cache(xdg))]
    assert module_cache(xdg).is_dir()
    assert not (home / ".cache").exists()


def test_a_read_only_sandbox_gets_no_writable_directory(tmp_path, monkeypatch, codex_help_probe):
    agent, home = codex(tmp_path, monkeypatch)
    agent.read_only()
    assert agent.send("review it").ok
    args = (tmp_path / "args.txt").read_text(encoding="utf-8").splitlines()
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert added_dirs(tmp_path) == []
    assert not (home / ".cache").exists()


def test_a_codex_without_add_dir_is_not_given_it(tmp_path, monkeypatch, codex_help_probe):
    agent, home = codex(tmp_path, monkeypatch, help_text=HELP_WITHOUT)
    assert agent.send("build it").ok
    assert agent.send("again").ok
    assert added_dirs(tmp_path) == []
    assert not (home / ".cache").exists()    # nothing made for an option never passed
    # Asked once, not every turn.
    assert (tmp_path / "helps.txt").read_text(encoding="utf-8").split() == ["help"]


def test_a_cache_that_cannot_be_made_does_not_cost_the_turn(tmp_path, monkeypatch, codex_help_probe):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the cache directory would go", encoding="utf-8")
    agent, _ = codex(tmp_path, monkeypatch, cache_home=blocker)
    reply = agent.send("build it")
    assert reply.ok, reply.error
    assert added_dirs(tmp_path) == []


# -- what the agents are told ----------------------------------------------------
def system_prompts(root: Path):
    config = Config(task="t", root=str(root), start="claude",
                    agents=[AgentSpec("claude", "claude-code"), AgentSpec("chatgpt", "codex-cli")])
    orch = Orchestrator(config, adapters={
        "claude": ClaudeCodeAdapter(name="claude", cwd=str(root)),
        "chatgpt": CodexCliAdapter(name="chatgpt", cwd=str(root)),
    })
    return orch._system_prompt("claude", 1), orch._system_prompt("chatgpt", 1)


def test_codex_is_told_to_run_swiftpm_without_its_sandbox_in_a_swift_package(tmp_path):
    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n", encoding="utf-8")
    claude, chatgpt = system_prompts(tmp_path)
    assert "swift build --disable-sandbox" in chatgpt
    assert "--package-path" in chatgpt
    assert "--disable-sandbox" not in claude      # Claude Code has no such sandbox


def test_a_nested_package_counts_and_one_in_a_skipped_dir_does_not(tmp_path):
    (tmp_path / ".build" / "checkouts" / "dep").mkdir(parents=True)
    (tmp_path / ".build" / "checkouts" / "dep" / "Package.swift").write_text("//\n", encoding="utf-8")
    assert "--disable-sandbox" not in system_prompts(tmp_path)[1]
    (tmp_path / "ios" / "Core").mkdir(parents=True)
    (tmp_path / "ios" / "Core" / "Package.swift").write_text("//\n", encoding="utf-8")
    assert "swift test --disable-sandbox" in system_prompts(tmp_path)[1]


def test_no_swift_paragraph_without_a_swift_package(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    claude, chatgpt = system_prompts(tmp_path)
    assert "--disable-sandbox" not in chatgpt and "--disable-sandbox" not in claude


def test_every_agent_is_told_run_gate_is_not_a_compile_check(tmp_path):
    for prompt in system_prompts(tmp_path):
        words = " ".join(prompt.split())
        assert "`run_gate` is for the full gate" in words
        assert "not the way to find out whether code compiles" in words
