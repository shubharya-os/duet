"""Adapter contracts, verified against stub binaries and a stubbed API.

The real `claude` and `codex` CLIs are not available in CI, so these tests stand
in for them: they assert the exact flags duet passes and the exact shapes it
parses back.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from fakebin import fake_cli
from duet.adapters.claude_code import ClaudeCodeAdapter
from duet.adapters.codex_cli import CodexCliAdapter
from duet.adapters import openai_api
from duet.adapters.openai_api import OpenAIAdapter


def fake_bin(tmp_path: Path, name: str, body: str) -> str:
    return fake_cli(tmp_path, name, body)


# -- Claude Code -----------------------------------------------------------
CLAUDE_STUB = r'''
printf '%s\n' "$@" > "$ARGDUMP"
cat <<'JSON'
{"type":"result","subtype":"success","is_error":false,
 "result":"Did the thing.\n```json\n{\"message\":\"did it\",\"verdict\":\"DONE\"}\n```",
 "session_id":"sess-123","total_cost_usd":0.04,"num_turns":3}
JSON
'''


def test_claude_adapter_sends_the_right_flags_and_parses_the_result(tmp_path, monkeypatch):
    dump = tmp_path / "args.txt"
    monkeypatch.setenv("ARGDUMP", str(dump))
    binary = fake_bin(tmp_path, "claude", CLAUDE_STUB)

    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path), config={"extra_args": []})
    agent.bin = binary
    reply = agent.send("do the work", system="you are claude")

    assert reply.ok
    assert "did it" in reply.text
    assert reply.meta["session_id"] == "sess-123"
    assert reply.meta["cost_usd"] == 0.04
    assert agent.session_id == "sess-123"

    args = dump.read_text(encoding="utf-8").splitlines()
    assert "-p" in args and "do the work" in args
    assert args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert args[args.index("--append-system-prompt") + 1] == "you are claude"
    assert "--resume" not in args          # nothing to resume on the first turn
    assert "--dangerously-skip-permissions" not in args


def test_claude_adapter_resumes_its_session_on_later_turns(tmp_path, monkeypatch):
    dump = tmp_path / "args.txt"
    monkeypatch.setenv("ARGDUMP", str(dump))
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "claude", CLAUDE_STUB)

    agent.send("turn one")
    agent.send("turn two")
    args = dump.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--resume") + 1] == "sess-123"


def test_claude_adapter_honours_bypass_mode_only_when_asked(tmp_path, monkeypatch):
    dump = tmp_path / "args.txt"
    monkeypatch.setenv("ARGDUMP", str(dump))
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path), config={"permission_mode": "bypass"})
    agent.bin = fake_bin(tmp_path, "claude", CLAUDE_STUB)
    agent.send("go")
    args = dump.read_text(encoding="utf-8").splitlines()
    assert "--dangerously-skip-permissions" in args and "--permission-mode" not in args


def test_claude_adapter_accepts_a_stream_of_events(tmp_path):
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "claude", r'''
cat <<'JSON'
[{"type":"assistant","message":"thinking"},
 {"type":"result","is_error":false,"result":"final answer","session_id":"s9"}]
JSON
''')
    reply = agent.send("go")
    assert reply.ok and reply.text == "final answer" and agent.session_id == "s9"


def test_claude_adapter_falls_back_to_plain_text(tmp_path):
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "claude", 'echo "not json at all"')
    reply = agent.send("go")
    assert reply.ok and "not json" in reply.text


def test_claude_adapter_reports_a_failure_instead_of_pretending(tmp_path):
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "claude", 'echo "boom" >&2; exit 1')
    reply = agent.send("go")
    assert not reply.ok and "boom" in reply.error


def test_claude_adapter_explains_a_missing_cli(tmp_path):
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = str(tmp_path / "definitely-not-here")
    reply = agent.send("go")
    assert not reply.ok and "npm install -g @anthropic-ai/claude-code" in reply.error


def test_claude_adapter_names_the_sign_in_when_the_cli_is_not_logged_in(tmp_path):
    """The CLI answers a prompt with a successful payload whose text says it is
    not logged in. That must surface as a sign-in problem, not as an agent turn."""
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "claude", r"""
echo '{"type":"result","subtype":"success","is_error":true,"result":"Not logged in · Please run /login","session_id":"s1"}'
""")
    reply = agent.send("go")
    assert not reply.ok
    assert "claude auth login" in reply.error


# -- sign-in probes (no API keys anywhere) ---------------------------------
def test_claude_probe_reads_a_real_login(tmp_path, monkeypatch):
    binary = fake_bin(tmp_path, "claude",
                      """echo '{"loggedIn": true, "authMethod": "claudeai"}'""")
    monkeypatch.setattr("duet.adapters.claude_code.Adapter.which", staticmethod(lambda *a: binary))
    probe = ClaudeCodeAdapter.probe()
    assert probe.ok and "claudeai" in probe.detail


def test_claude_probe_catches_an_installed_but_signed_out_cli(tmp_path, monkeypatch):
    """The bug this replaces: `--version` succeeds on a signed-out CLI, so duet
    reported ready and then failed on the first turn."""
    binary = fake_bin(tmp_path, "claude",
                      """echo '{"loggedIn": false, "authMethod": "none"}'""")
    monkeypatch.setattr("duet.adapters.claude_code.Adapter.which", staticmethod(lambda *a: binary))
    probe = ClaudeCodeAdapter.probe()
    assert not probe.ok
    assert "not signed in" in probe.detail
    assert "claude auth login" in probe.fix
    assert "API key" in probe.fix          # and it says you do not need one


def test_claude_probe_does_not_claim_ready_when_the_state_is_unreadable(tmp_path, monkeypatch):
    binary = fake_bin(tmp_path, "claude", 'echo "who knows"')
    monkeypatch.setattr("duet.adapters.claude_code.Adapter.which", staticmethod(lambda *a: binary))
    probe = ClaudeCodeAdapter.probe()
    assert not probe.ok and "could not be read" in probe.detail


def test_claude_probe_reports_a_missing_cli_with_an_install_command(monkeypatch):
    monkeypatch.setattr("duet.adapters.claude_code.Adapter.which", staticmethod(lambda *a: None))
    probe = ClaudeCodeAdapter.probe()
    assert not probe.ok and "npm install -g @anthropic-ai/claude-code" in probe.fix


def test_codex_probe_reports_a_chatgpt_login(tmp_path, monkeypatch):
    binary = fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    probe = CodexCliAdapter.probe()
    assert probe.ok and "ChatGPT" in probe.detail


def test_codex_probe_catches_a_signed_out_cli(tmp_path, monkeypatch):
    binary = fake_bin(tmp_path, "codex", 'echo "Not logged in"; exit 1')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    probe = CodexCliAdapter.probe()
    assert not probe.ok and "codex login" in probe.fix


def test_codex_adapter_reports_a_signed_out_cli_instead_of_failing_oddly(tmp_path):
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", 'echo "Not logged in. Run codex login." >&2; exit 1')
    reply = agent.send("go")
    assert not reply.ok and "codex login" in reply.error


# -- OpenAI API ------------------------------------------------------------
def test_openai_adapter_refuses_to_run_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    reply = OpenAIAdapter(name="gpt").send("hi")
    assert not reply.ok and "OPENAI_API_KEY" in reply.error


def test_openai_adapter_builds_a_conversation_and_keeps_history(monkeypatch):
    seen = {}

    def fake_request(url, key, payload=None, timeout=300):
        if url.endswith("/models"):
            return {"data": [{"id": "gpt-5"}, {"id": "gpt-4o"}]}
        seen["payload"] = payload
        return {"model": payload["model"], "usage": {"prompt_tokens": 10},
                "choices": [{"message": {"content": "my answer"}}]}

    monkeypatch.setattr(openai_api, "_request", fake_request)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("DUET_OPENAI_MODEL", raising=False)

    agent = OpenAIAdapter(name="gpt")
    assert agent.send("first", system="be a peer").text == "my answer"
    assert seen["payload"]["model"] == "gpt-5"          # newest the key can see
    assert seen["payload"]["messages"][0] == {"role": "system", "content": "be a peer"}

    agent.send("second", system="be a peer")
    roles = [m["role"] for m in seen["payload"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]   # it remembers the exchange


def test_openai_adapter_surfaces_an_http_error(monkeypatch):
    import urllib.error

    def boom(url, key, payload=None, timeout=300):
        if url.endswith("/models"):
            return {"data": [{"id": "gpt-4o"}]}
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(openai_api, "_request", boom)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-bad")
    reply = OpenAIAdapter(name="gpt", config={"max_retries": 1}).send("hi")
    assert not reply.ok and "401" in reply.error


def test_openai_history_is_trimmed_but_keeps_the_opening_and_the_recent_turns(monkeypatch):
    calls = {}

    def fake_request(url, key, payload=None, timeout=300):
        if url.endswith("/models"):
            return {"data": [{"id": "gpt-4o"}]}
        calls["payload"] = payload
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(openai_api, "_request", fake_request)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    agent = OpenAIAdapter(name="gpt")
    for i in range(20):
        agent.send("turn %d" % i)
    messages = calls["payload"]["messages"]
    assert any("earlier turns trimmed" in str(m.get("content")) for m in messages)
    assert messages[-1]["content"] == "turn 19"
    assert len(messages) <= openai_api.MAX_HISTORY_TURNS + 3


def test_openai_state_round_trips(monkeypatch):
    agent = OpenAIAdapter(name="gpt")
    agent.history = [{"role": "user", "content": "a"}]
    restored = OpenAIAdapter(name="gpt")
    restored.restore(agent.state())
    assert restored.history == agent.history


# -- Codex CLI -------------------------------------------------------------
def test_codex_adapter_passes_sandbox_and_workdir(tmp_path, monkeypatch):
    dump = tmp_path / "args.txt"
    monkeypatch.setenv("ARGDUMP", str(dump))
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", 'printf "%s\\n" "$@" > "$ARGDUMP"; echo "codex says hi"')

    reply = agent.send("build it", system="you are gpt")
    assert reply.ok and "codex says hi" in reply.text
    args = dump.read_text(encoding="utf-8").splitlines()
    assert args[0] == "exec" and "--skip-git-repo-check" in args
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    dumped = dump.read_text(encoding="utf-8")          # the prompt spans lines; check the whole dump
    assert "you are gpt" in dumped and "build it" in dumped


def test_codex_adapter_falls_back_when_resume_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGDUMP", str(tmp_path / "args.txt"))
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", r'''
for a in "$@"; do if [ "$a" = "resume" ]; then echo "unknown subcommand" >&2; exit 2; fi; done
echo "fresh run ok"
''')
    agent.use_resume = True
    agent.turns = 1                      # pretend a session exists
    reply = agent.send("go")
    assert reply.ok and "fresh run ok" in reply.text


def test_codex_adapter_prefers_the_last_message_file_over_scraped_stdout(tmp_path):
    """Codex prints a live log; the final message is what duet must parse, so it
    asks for it in a file and only falls back to stdout."""
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", r'''
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
echo "thinking... [2m dim ansi noise"
printf '%s' '{"message":"the real envelope","verdict":"DONE"}' > "$out"
''')
    reply = agent.send("go")
    assert reply.ok
    assert reply.text == '{"message":"the real envelope","verdict":"DONE"}'
    assert reply.meta["used_last_message_file"] is True


def test_codex_adapter_disables_colour_so_output_stays_parseable(tmp_path, monkeypatch):
    dump = tmp_path / "args.txt"
    monkeypatch.setenv("ARGDUMP", str(dump))
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", 'printf "%s\\n" "$@" > "$ARGDUMP"; echo hi')
    agent.send("go")
    args = dump.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--color") + 1] == "never"
    assert args[args.index("-C") + 1] == str(tmp_path)
    assert "-o" in args


def test_agent_clis_are_not_given_an_inherited_stdin(tmp_path):
    """A CLI that reads stdin when it is a pipe hangs forever under duet, which
    is never run from a tty. Both adapters must close it.

    This is a regression test for a real hang: `codex exec` appends piped stdin
    to the prompt, so a live session sat idle for eleven minutes.
    """
    reader = r'''
if [ -t 0 ]; then echo "tty"; else
  # would block indefinitely on an inherited pipe that is never closed
  cat > /dev/null
fi
echo done
'''
    codex = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    codex.bin = fake_bin(tmp_path, "codex", reader)
    codex.timeout = 20
    assert codex.send("go").ok            # returns rather than hanging

    claude = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    claude.bin = fake_bin(tmp_path, "claude", reader)
    claude.timeout = 20
    assert claude.send("go").ok


def test_claude_is_allowed_to_run_the_gate_itself(tmp_path, monkeypatch):
    """Under acceptEdits the agent can write files but not run them, so it had
    to take the harness's word for the test run. Grant exactly the gate."""
    dump = tmp_path / "args.txt"
    monkeypatch.setenv("ARGDUMP", str(dump))
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "claude", 'printf "%s\\n" "$@" > "$ARGDUMP"; echo "{}"')
    agent.allow_gate("pytest -q && npm run lint")
    agent.send("go")

    args = dump.read_text(encoding="utf-8")
    assert "Bash(pytest:*)" in args
    assert "Bash(npm:*)" in args
    assert "--dangerously-skip-permissions" not in args   # not a blanket grant


def test_allowing_the_gate_ignores_a_leading_env_assignment(tmp_path):
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.allow_gate("CI=1 pytest -q")
    assert agent.allowed_tools == ["Bash(pytest:*)"]


def test_allowing_the_gate_steps_over_a_quoted_env_assignment(tmp_path):
    """`SIM_NAME="iPhone 17" make all` was split on spaces, so `17"` was
    allow-listed instead of `make` and every build the agent tried was refused."""
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.allow_gate('SIM_NAME="iPhone 17" make all')
    assert agent.allowed_tools == ["Bash(make:*)"]


def test_an_adapter_without_permissions_ignores_the_gate(tmp_path):
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.allow_gate("pytest -q")      # codex has its own sandbox; nothing to do


def test_a_transient_sign_in_blip_is_confirmed_before_being_believed(tmp_path):
    """A live session lost a whole round to a 'not logged in' from codex whose
    login was fine before and after. Check the real state before giving up."""
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    # Per test, not a fixed /tmp path: two suites run side by side raced on it.
    marker = str(tmp_path / "blip-done")
    agent.bin = fake_bin(tmp_path, "codex", r'''
for a in "$@"; do
  if [ "$a" = "status" ]; then echo "Logged in using ChatGPT"; exit 0; fi
done
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
if [ -f %(marker)s ]; then
  printf '%%s' '{"message":"second attempt worked","verdict":"DONE"}' > "$out"; exit 0
fi
touch %(marker)s
echo "Not logged in" >&2; exit 1
''' % {"marker": marker})
    reply = agent.send("go")
    assert reply.ok, reply.error
    assert "second attempt worked" in reply.text


def test_a_real_sign_out_is_still_reported(tmp_path):
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", 'echo "Not logged in" >&2; exit 1')
    reply = agent.send("go")
    assert not reply.ok and "codex login" in reply.error


def test_codex_continues_its_session_across_turns(tmp_path, monkeypatch):
    """Continuity is on by default and costs nothing when it fails: every prompt
    duet builds is self-contained, so a failed resume falls back to a fresh
    session without losing anything."""
    dump = tmp_path / "args.txt"
    monkeypatch.setenv("ARGDUMP", str(dump))
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", r'''
out=""
args="$@"
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
printf '%s' "$args" > "$ARGDUMP"
printf '%s' '{"message":"ok","verdict":"DONE"}' > "$out"
''')
    agent.send("turn one")
    agent.send("turn two")
    assert "--last" in dump.read_text(encoding="utf-8")


def test_codex_state_round_trips_so_a_resumed_duet_session_keeps_continuity(tmp_path):
    """`turns` is this adapter's whole memory, and `duet resume` rebuilds from it.

    It only changes anything when `use_resume` is on, but that is precisely the
    setting where losing it starts codex from scratch behind the user's back.
    """
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path), config={"use_resume": True})
    agent.turns = 3
    restored = CodexCliAdapter(name="gpt", cwd=str(tmp_path), config={"use_resume": True})
    assert restored.turns == 0
    restored.restore(agent.state())
    assert restored.turns == 3

    restored.restore({})                       # a state file from before this existed
    assert restored.turns == 0


def test_codex_resume_can_be_switched_off(tmp_path, monkeypatch):
    dump = tmp_path / "args.txt"
    monkeypatch.setenv("ARGDUMP", str(dump))
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path), config={"use_resume": False})
    agent.bin = fake_bin(tmp_path, "codex", r'''
out=""
args="$@"
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
printf '%s' "$args" > "$ARGDUMP"
printf '%s' '{"message":"ok","verdict":"DONE"}' > "$out"
''')
    agent.send("turn one")
    agent.send("turn two")
    assert "--last" not in dump.read_text(encoding="utf-8")


def test_codex_failures_report_the_error_not_the_banner(tmp_path):
    """codex prints a banner and echoes the prompt before failing, so the real
    line is last. Reporting the first N characters reports the banner — which is
    exactly how a plain usage limit was misdiagnosed as a stdin bug."""
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", r'''
echo "Reading additional input from stdin..." >&2
echo "OpenAI Codex v0.155.0" >&2
echo "--------" >&2
echo "workdir: /tmp" >&2
echo "user" >&2
echo "do the thing" >&2
echo "ERROR: You've hit your usage limit. Try again at Oct 18th." >&2
exit 1
''')
    reply = agent.send("go")
    assert not reply.ok
    assert "usage limit" in reply.error
    assert "OpenAI Codex v0.155.0" not in reply.error       # not the banner
    assert "--pair claude+gpt" in reply.error               # and a way forward


def test_a_plain_codex_error_is_reported_verbatim(tmp_path):
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", 'echo "banner" >&2; echo "ERROR: disk full" >&2; exit 1')
    reply = agent.send("go")
    assert "disk full" in reply.error and "usage limit" not in reply.error


def test_output_with_no_error_line_falls_back_to_the_tail(tmp_path):
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", 'echo "banner line" >&2; echo "something odd at the end" >&2; exit 2')
    reply = agent.send("go")
    assert "something odd at the end" in reply.error


# --- a CLI that cannot start is not a CLI that is signed out ----------------

BROKEN_RUNTIME = 'echo "env: node: Bad CPU type in executable" >&2; exit 126'


def test_a_codex_that_cannot_start_is_not_reported_as_signed_out(tmp_path, monkeypatch):
    """Reporting 'not signed in' for a CLI that never ran sends people to a
    login command that fails the same way, with nothing to explain why. Found
    on a real machine where a broken node shadowed the working one."""
    binary = fake_bin(tmp_path, "codex", BROKEN_RUNTIME)
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    probe = CodexCliAdapter.probe()
    assert not probe.ok
    assert "could not run" in probe.detail
    assert "Bad CPU type" in probe.detail
    assert "codex login" not in probe.fix          # not the wrong advice
    assert "node" in probe.fix                     # the actual cause


def test_a_claude_that_cannot_start_is_not_reported_as_signed_out(tmp_path, monkeypatch):
    binary = fake_bin(tmp_path, "claude", BROKEN_RUNTIME)
    monkeypatch.setattr("duet.adapters.claude_code.Adapter.which", staticmethod(lambda *a: binary))
    probe = ClaudeCodeAdapter.probe()
    assert not probe.ok
    assert "could not run" in probe.detail
    assert "auth login" not in probe.fix
    assert "node" in probe.fix


@pytest.mark.parametrize("noise", [
    "env: node: Bad CPU type in executable",
    "exec format error",
    "/usr/local/bin/node: command not found",
    "dyld: symbol not found",
])
def test_every_unrunnable_signature_is_recognised(noise):
    from duet.adapters.base import looks_unrunnable

    assert looks_unrunnable(noise)
    assert not looks_unrunnable("Logged in using ChatGPT")
    assert not looks_unrunnable('{"loggedIn": true}')


@pytest.mark.skipif(os.name == "nt", reason="the fake reads PATH as a POSIX shell sees it "
                    "(':'-separated, /usr/bin first); the PATH repair itself is tested below")
def test_a_broken_interpreter_on_path_is_worked_around(tmp_path, monkeypatch):
    """A `node` earlier on PATH than the working one broke both CLIs on a real
    machine. The CLI's own directory almost certainly holds the toolchain it was
    installed with, so trying that is worth one retry before giving up."""
    bin_dir = tmp_path / "good"
    bin_dir.mkdir()
    # Only runs when its own directory is first on PATH; otherwise it fails the
    # way a shadowed interpreter does.
    binary = fake_bin(bin_dir, "codex", r'''
first="${PATH%%:*}"
if [ "$first" = "$(dirname "$0")" ]; then
  echo "Logged in using ChatGPT"
else
  echo "env: node: Bad CPU type in executable" >&2
  exit 126
fi
''')
    # A PATH that can still run a shell, but does not contain the CLI's own
    # directory — which is the shape of the real breakage.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    probe = CodexCliAdapter.probe()
    assert probe.ok, probe.detail
    assert "ChatGPT" in probe.detail


def test_the_repaired_path_puts_the_cli_directory_first(tmp_path):
    from duet.adapters.base import env_with_sibling_path

    binary = str(tmp_path / "bin" / "claude")
    env = env_with_sibling_path(binary, {"PATH": os.pathsep.join(["/usr/local/bin", "/usr/bin"])})
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    assert "/usr/bin" in env["PATH"]           # the rest is kept

    # already first: left alone rather than duplicated
    again = env_with_sibling_path(binary, env)
    assert again["PATH"] == env["PATH"]


def test_a_genuinely_broken_cli_still_reports_honestly(tmp_path, monkeypatch):
    """The retry must not turn a real failure into a false green."""
    binary = fake_bin(tmp_path, "codex", 'echo "env: node: Bad CPU type in executable" >&2; exit 126')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    probe = CodexCliAdapter.probe()
    assert not probe.ok
    assert "could not run" in probe.detail


# --- signed in is not the same as having quota left -------------------------
#
# A ChatGPT account can be signed in and out of Codex allowance at the same
# time. `codex login status` answers the first question and says nothing about
# the second, so doctor used to report "ready" and the first turn died.

USAGE_LIMIT = ("ERROR: You've hit your usage limit. Upgrade to Plus to continue "
               "using Codex (https://chatgpt.com/explore/plus), or try again at "
               "Oct 18th, 2026 11:18 PM.")


def state_dir() -> Path:
    return Path(os.environ["DUET_STATE_DIR"])


def note_file() -> Path:
    from duet.adapters.codex_cli import QUOTA_NOTE
    return state_dir() / QUOTA_NOTE


def test_codex_probe_does_not_claim_quota_it_has_not_checked(tmp_path, monkeypatch):
    """The signed-in line may only claim what `codex login status` proves."""
    binary = fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    probe = CodexCliAdapter.probe()
    assert probe.ok
    assert probe.signed_in is True
    assert "quota not checked" in probe.detail


def test_a_usage_limit_is_written_down_when_it_happens(tmp_path):
    """The account itself is the only cheap source of truth about the
    allowance, and it only speaks when the limit is hit. Keep what it said."""
    import json

    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", "cat >&2 <<'MSG'\n%s\nMSG\nexit 1\n" % USAGE_LIMIT)
    reply = agent.send("go")

    assert not reply.ok and "usage limit" in reply.error
    assert "duet doctor" in reply.error            # says what will happen next
    from datetime import datetime
    note = json.loads(note_file().read_text(encoding="utf-8"))
    assert "usage limit" in note["message"]
    assert datetime.fromtimestamp(note["resets_at"]).strftime("%Y-%m-%d %H:%M") \
        == "2026-10-18 23:18"


def test_doctor_refuses_to_say_ready_while_the_recorded_limit_is_in_force(tmp_path, monkeypatch):
    from duet.adapters import codex_cli

    binary = fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    codex_cli.record_usage_limit(USAGE_LIMIT.replace("Oct 18th, 2026", "Oct 18th, 2099"))

    probe = CodexCliAdapter.probe()
    assert not probe.ok
    assert "out of Codex usage quota" in probe.detail
    assert "2099" in probe.detail                        # the deadline it checked
    assert "--pair claude:opus+claude:sonnet" in probe.fix
    assert str(note_file()) in probe.fix                 # and how to overrule it
    # out of quota is not signed out, and `duet login` must not confuse the two
    assert probe.signed_in is True
    assert "codex login" not in probe.fix


def test_a_recorded_limit_whose_reset_has_passed_is_forgotten(tmp_path, monkeypatch):
    from duet.adapters import codex_cli

    binary = fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    codex_cli.record_usage_limit(USAGE_LIMIT.replace("Oct 18th, 2026", "Oct 18th, 2020"))

    probe = CodexCliAdapter.probe()
    assert probe.ok and "quota not checked" in probe.detail
    assert not note_file().exists()             # and not carried forward


def test_a_limit_with_no_stated_reset_is_reported_without_blocking(tmp_path, monkeypatch):
    """Unknown is not the same as exhausted. Say what is known, and do not
    lock the user out of the one turn that would settle it."""
    from duet.adapters import codex_cli

    binary = fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    codex_cli.record_usage_limit("ERROR: You've hit your usage limit.")

    probe = CodexCliAdapter.probe()
    assert probe.ok                                       # not a claim of exhaustion
    assert "quota not checked" in probe.detail
    assert "hit the usage limit" in probe.detail
    assert "--pair claude+gpt" in probe.detail            # names the way round it


def test_a_turn_that_succeeds_clears_the_recorded_limit(tmp_path):
    from duet.adapters import codex_cli

    codex_cli.record_usage_limit(USAGE_LIMIT.replace("2026", "2099"))
    assert note_file().exists()

    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", 'echo "codex says hi"')
    assert agent.send("go").ok
    assert not note_file().exists()


def test_a_prompt_that_talks_about_usage_limits_is_not_mistaken_for_one(tmp_path):
    """codex echoes the prompt into its own output, and duet's prompts are full
    of words like "quota". Matching anywhere would let a task description about
    usage limits convince duet the account had hit one."""
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", r'''
printf '%s\n' "$@" >&2
echo "ERROR: disk full" >&2
exit 1
''')
    reply = agent.send("fix the doctor that ignores the Codex usage limit quota")
    assert "disk full" in reply.error
    assert not note_file().exists()


def test_the_reset_time_codex_actually_prints_is_understood():
    from duet.adapters.codex_cli import parse_reset_at

    at = parse_reset_at(USAGE_LIMIT)
    assert at is not None
    from datetime import datetime
    assert datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") == "2026-10-18 23:18"
    # no deadline stated is a real answer, not a parse failure to paper over
    assert parse_reset_at("ERROR: You've hit your usage limit.") is None
    assert parse_reset_at("") is None


def test_the_reset_time_is_read_without_help_from_the_locale():
    """`strptime` reads %b and %p through LC_TIME, so on a machine set to a
    non-English locale it refuses "Oct" and "PM" — which codex prints whatever
    the locale is set to. parse_reset_at therefore reads the month and the
    meridiem itself; these are the shapes it has to cover."""
    from datetime import datetime
    from duet.adapters.codex_cli import parse_reset_at

    def when(text):
        at = parse_reset_at("try again at %s" % text)
        return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") if at else None

    assert when("Oct 18th, 2026 11:18 PM") == "2026-10-18 23:18"
    assert when("October 18, 2026 23:18") == "2026-10-18 23:18"
    assert when("2026-10-18 23:18") == "2026-10-18 23:18"
    assert when("Oct 18, 2026 12:30 AM") == "2026-10-18 00:30"
    assert when("Oct 18, 2026 12:30 PM") == "2026-10-18 12:30"
    assert when("Oct 18, 2026") == "2026-10-18 00:00"
    assert when("Frobsday the 40th") is None         # nonsense stays unknown
    assert when("Oct 40, 2026 11:18 PM") is None     # and so does an impossible date


def test_duet_login_does_not_answer_a_quota_problem_with_a_browser(tmp_path, monkeypatch, capsys):
    """`duet login` ran the CLI's browser sign-in for any failed probe, then
    announced "still not signed in" — which for an exhausted allowance is a
    pointless browser window and a false statement."""
    import argparse
    from duet import cli
    from duet.adapters.base import Probe

    binary = fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    from duet.adapters import codex_cli
    codex_cli.record_usage_limit(USAGE_LIMIT.replace("Oct 18th, 2026", "Oct 18th, 2099"))
    assert not CodexCliAdapter.probe().ok               # the probe does fail

    def refuse(*a, **k):
        raise AssertionError("duet login ran a sign-in for an account that is signed in")

    monkeypatch.setattr(cli.subprocess, "call", refuse)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 1)

    args = argparse.Namespace(root=str(tmp_path), agent="chatgpt", force=False)
    assert cli.cmd_login(args) == 1                     # still not ready, but honest
    out = capsys.readouterr().out
    assert "already signed in" in out
    assert "out of Codex usage quota" in out
    assert Probe(ok=False, detail="x").signed_in is None   # other backends unchanged


def test_duet_login_force_does_not_call_a_quota_problem_a_failed_sign_in(tmp_path, monkeypatch, capsys):
    """--force runs the sign-in anyway. When that sign-in is abandoned the
    account is still the one that hit the limit, so the probe afterwards still
    fails on quota — and the old wording then announced "still not signed in"
    about an account that is signed in.

    (This test used to run a sign-in that *succeeded* and expect the quota
    note to survive it. That expectation was the relogin bug: see
    test_a_successful_codex_sign_in_forgets_the_old_accounts_usage_limit.)"""
    import argparse
    from duet import cli
    from duet.adapters import codex_cli

    binary = fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    codex_cli.record_usage_limit(USAGE_LIMIT.replace("Oct 18th, 2026", "Oct 18th, 2099"))
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: 1)   # cancelled
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 1)

    args = argparse.Namespace(root=str(tmp_path), agent="chatgpt", force=True)
    assert cli.cmd_login(args) == 1                 # doctor still says not ready
    out = capsys.readouterr().out
    assert "still not signed in" not in out        # because it is signed in
    assert "out of Codex usage quota" in out       # this is the real problem
    assert note_file().exists()                    # a cancelled sign-in forgets nothing


def test_a_successful_codex_sign_in_forgets_the_old_accounts_usage_limit(tmp_path, monkeypatch, capsys):
    """2026-10-10: one ChatGPT account hit its Codex limit, the user ran
    `duet login --force` and signed in with a different account — and duet
    went on calling the new account out of quota until the old one's reset,
    because only a successful turn ever cleared the note. duet cannot tell
    whether the new sign-in is the same account, so a sign-in that worked has
    to forget what the old one said."""
    import argparse
    from duet import cli
    from duet.adapters import codex_cli

    binary = fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    codex_cli.record_usage_limit(USAGE_LIMIT.replace("Oct 18th, 2026", "Oct 18th, 2099"))
    assert not CodexCliAdapter.probe().ok
    ran = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: ran.append(argv) or 0)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)

    args = argparse.Namespace(root=str(tmp_path), agent="chatgpt", force=True)
    assert cli.cmd_login(args) == 0
    assert ran and ran[0][-1] == "login"           # the sign-in really ran
    out = capsys.readouterr().out
    assert "out of Codex usage quota" not in out
    assert not note_file().exists()
    probe = CodexCliAdapter.probe()
    assert probe.ok and probe.signed_in is True


def test_a_codex_login_that_exits_0_but_leaves_it_signed_out_forgets_nothing(tmp_path, monkeypatch):
    """"Worked" means exit 0 *and* signed in afterwards, not exit 0 alone."""
    import argparse
    from duet import cli
    from duet.adapters import codex_cli

    binary = fake_bin(tmp_path, "codex", 'echo "Not logged in"; exit 1')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    codex_cli.record_usage_limit(USAGE_LIMIT.replace("Oct 18th, 2026", "Oct 18th, 2099"))
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 1)

    args = argparse.Namespace(root=str(tmp_path), agent="chatgpt", force=True)
    assert cli.cmd_login(args) == 1
    assert note_file().exists()


# -- a sign-in check that runs out of time ----------------------------------
def _slow_then(module, monkeypatch, answers):
    """Make `module`'s status command time out, or answer, in the given order."""
    import subprocess
    calls = []

    def run(argv, **kw):
        calls.append(kw.get("timeout"))
        answer = answers[min(len(calls), len(answers)) - 1]
        if answer is None:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, answer, "")

    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def test_a_slow_claude_sign_in_check_is_retried_with_more_time(tmp_path, monkeypatch):
    """2026-10-10, load average ~200: `claude auth status` took longer than
    45 seconds for a signed-in account and duet refused to start."""
    from duet.adapters import claude_code

    calls = _slow_then(claude_code, monkeypatch, [None, '{"loggedIn": true, "authMethod": "claudeai"}'])
    assert claude_code.read_auth_status(["claude"]) == (True, "signed in (claudeai)")
    assert len(calls) == 2 and calls[1] > calls[0]


def test_a_claude_sign_in_check_that_never_answers_starts_with_a_warning(tmp_path, monkeypatch, capsys):
    from duet import cli
    from duet.adapters import claude_code
    from duet.config import AgentSpec, Config

    binary = fake_bin(tmp_path, "claude", "exit 0")
    monkeypatch.setattr("duet.adapters.claude_code.Adapter.which", staticmethod(lambda *a: binary))
    _slow_then(claude_code, monkeypatch, [None])

    probe = ClaudeCodeAdapter.probe()
    assert probe.ok                                  # unknown is not signed out
    assert probe.signed_in is not False
    for text in (probe.detail, probe.fix, probe.warning):
        assert "auth login" not in text              # no login advice for a timeout
    assert "timed out" in probe.warning and "first turn" in probe.warning

    cfg = Config(agents=[AgentSpec(name="claude-opus", backend="claude-code")])
    assert cli.preflight(cfg) == []                  # the session starts...
    err = capsys.readouterr().err
    assert "claude-opus" in err and "timed out" in err   # ...and says why it is unsure
    assert "auth login" not in err


def test_a_definite_claude_sign_out_still_refuses(tmp_path, monkeypatch):
    from duet import cli
    from duet.config import AgentSpec, Config

    binary = fake_bin(tmp_path, "claude", """echo '{"loggedIn": false, "authMethod": "none"}'""")
    monkeypatch.setattr("duet.adapters.claude_code.Adapter.which", staticmethod(lambda *a: binary))
    cfg = Config(agents=[AgentSpec(name="claude-opus", backend="claude-code")])
    problems = cli.preflight(cfg)
    assert len(problems) == 1 and "claude auth login" in problems[0]


def test_a_codex_sign_in_check_that_never_answers_is_not_a_sign_out(tmp_path, monkeypatch):
    """The same mistake in the Codex probe: a timeout advised `codex login`."""
    from duet.adapters import codex_cli

    binary = fake_bin(tmp_path, "codex", "exit 0")
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    calls = _slow_then(codex_cli, monkeypatch, [None])

    probe = CodexCliAdapter.probe()
    assert len(calls) == 2                           # retried once, then gave up
    assert probe.ok and probe.signed_in is None
    assert probe.fix == ""                           # no login advice for a timeout
    assert codex_cli.LOGIN_HINT not in probe.detail + probe.warning
    assert "timed out" in probe.warning


def test_a_timed_out_codex_check_does_not_refuse_on_an_old_quota_note(tmp_path, monkeypatch):
    """A dated usage-limit note from a previous sign-in is not proof of the
    *current* sign-in state. If `codex login status` only timed out, saying
    "signed in, but out of quota" states a sign-in duet never confirmed, and
    refusing on it is the exact mistake this bug is about — just dressed up
    as a quota message instead of a login hint."""
    from duet.adapters import codex_cli
    from duet.config import AgentSpec, Config
    from duet import cli

    binary = fake_bin(tmp_path, "codex", "exit 0")
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    codex_cli.record_usage_limit(USAGE_LIMIT.replace("Oct 18th, 2026", "Oct 18th, 2099"))
    _slow_then(codex_cli, monkeypatch, [None])

    probe = CodexCliAdapter.probe()
    assert probe.ok                                  # unknown is not out of quota either
    assert probe.signed_in is None
    assert probe.fix == ""
    assert "timed out" in probe.warning

    cfg = Config(agents=[AgentSpec(name="chatgpt", backend="codex-cli")])
    assert cli.preflight(cfg) == []                  # starts, with a warning — not refused


def test_duet_login_does_not_open_a_browser_because_a_check_was_slow(tmp_path, monkeypatch, capsys):
    import argparse
    from duet import cli
    from duet.adapters import codex_cli

    binary = fake_bin(tmp_path, "codex", "exit 0")
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    _slow_then(codex_cli, monkeypatch, [None])

    def refuse(*a, **k):
        raise AssertionError("duet login opened a sign-in because a status check was slow")

    monkeypatch.setattr(cli.subprocess, "call", refuse)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)
    args = argparse.Namespace(root=str(tmp_path), agent="chatgpt", force=False)
    cli.cmd_login(args)
    out = capsys.readouterr().out
    assert "already signed in" not in out and "timed out" in out


def test_a_usage_limit_wins_over_an_earlier_unrelated_error(tmp_path):
    """duet writes the usage limit down and doctor reports it afterwards, so
    reporting a different error on screen would leave the user with a mystery
    error and a quota verdict that nothing they saw accounts for."""
    agent = CodexCliAdapter(name="gpt", cwd=str(tmp_path))
    agent.bin = fake_bin(tmp_path, "codex", "cat >&2 <<'MSG'\nERROR: stream disconnected before completion\n%s\nMSG\nexit 1\n" % USAGE_LIMIT)
    reply = agent.send("go")

    assert "usage limit" in reply.error            # not "stream disconnected"
    assert "claude:opus+claude:sonnet" in reply.error
    assert note_file().exists()                    # and the two agree


def test_a_usage_limit_reaches_the_console_with_its_fix_intact(capsys):
    """The reporter used to clip errors at 400 characters, which cut the usage
    limit's advice off mid-sentence and left a stopped session unexplained."""
    from duet.cli import make_reporter
    from duet.adapters.codex_cli import explain_usage_limit

    report = make_reporter(["claude", "gpt"])
    report({"kind": "turn_error", "round": 1, "agent": "gpt",
            "error": explain_usage_limit(USAGE_LIMIT)})
    out = capsys.readouterr().out

    assert "usage limit" in out
    # wrapped output can break between words, so assert on unsplittable tokens
    assert "11:18" in out                             # when it comes back
    assert "claude:opus+claude:sonnet" in out         # and what to do meanwhile
    assert "succeeds" in out                          # the last sentence survives


# --- running with nothing installed globally --------------------------------

def test_a_real_binary_is_preferred_over_npx(tmp_path, monkeypatch):
    binary = fake_bin(tmp_path, "claude", 'echo ok')
    monkeypatch.setattr("duet.adapters.base.Adapter.which",
                        staticmethod(lambda *c: binary if "claude" in c else "/usr/bin/npx"))
    launch, found, globally = ClaudeCodeAdapter.resolve_launch(("claude",))
    assert launch == [binary] and found == binary and globally is True


def test_npx_runs_the_cli_when_nothing_is_installed_globally(monkeypatch):
    """The global npm install was the one setup step that changes the machine
    outside duet's own directory. npx removes it: the sign-in lives in the
    agent's own config, so an npx-run CLI sees the same account."""
    monkeypatch.setattr("duet.adapters.base.Adapter.which",
                        staticmethod(lambda *c: "/usr/bin/npx" if "npx" in c else None))
    launch, found, globally = ClaudeCodeAdapter.resolve_launch(("claude",))
    assert launch == ["/usr/bin/npx", "-y", "@anthropic-ai/claude-code"]
    assert globally is False
    assert found == "/usr/bin/npx"

    launch, _, _ = CodexCliAdapter.resolve_launch(("codex",))
    assert launch[-1] == "@openai/codex"


def test_nothing_found_is_reported_as_nothing_found(monkeypatch):
    """Returning a bare name as if it had been located made a probe report a
    missing CLI as present."""
    monkeypatch.setattr("duet.adapters.base.Adapter.which", staticmethod(lambda *c: None))
    launch, found, _ = ClaudeCodeAdapter.resolve_launch(("claude",))
    assert found == ""                 # not "claude"
    assert launch == ["claude"]        # but the name is kept for the message

    probe = ClaudeCodeAdapter.probe()
    assert not probe.ok and "not found" in probe.detail


def test_setting_bin_rewrites_what_actually_runs(tmp_path):
    """Assigning to .bin is how a caller says "run exactly this". Keeping the
    argv prefix as a separate attribute let a test set one and silently execute
    the other — which meant the real CLI, over the network, in a unit test."""
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    stub = fake_bin(tmp_path, "claude-stub", 'echo "{}"')
    agent.bin = stub
    assert agent.launch == [stub]


def test_the_probe_says_via_npx_rather_than_naming_it_as_the_install(tmp_path, monkeypatch):
    """`where` was computed and then used on only one branch, so the green line
    almost every healthy machine sees still claimed the CLI was installed at
    npx's path — the exact misreport the fix was supposed to remove.

    An error that quotes the failing path is fine; claiming the CLI lives there
    is not.
    """
    npx = fake_bin(tmp_path, "npx", r"""
# `npx -y <pkg> auth status` and `... login status` both answer as the CLI would
case "$*" in
  *"auth status"*) echo '{"loggedIn": true, "authMethod": "claudeai"}' ;;
  *"login status"*) echo "Logged in using ChatGPT" ;;
  *) echo "1.0.0" ;;
esac
""")
    monkeypatch.setattr("duet.adapters.base.Adapter.which",
                        staticmethod(lambda *c: npx if any(str(x).endswith("npx") for x in c) else None))
    for cls in (ClaudeCodeAdapter, CodexCliAdapter):
        probe = cls.probe()
        assert probe.ok, "%s: %s" % (cls.__name__, probe.detail)
        assert "via npx, nothing installed globally" in probe.detail, probe.detail
        assert npx not in probe.detail, "named npx as the install location: %s" % probe.detail
