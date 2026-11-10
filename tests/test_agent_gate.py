"""`duet gate`: an agent inside a session runs the gate the sanctioned way.

Measured on 262 sessions on duet 0.15+, 27% had an agent refused while running
the gate itself, wrapped in redirects and pipes the permission mode will not
allow. `duet gate` is the whole of what those wrappers were for, and each run
it makes is recorded so the harness can learn the gate's outputs from it — an
agent running a gate that writes build output used to make that output look
like the agent's own work, and the state moved on every harness run after.

The gates are small Python scripts kept outside the workspace and run with
this interpreter, as in test_gate_outputs.py, so they work on Windows too.
"""

import json
import re
import sys
from pathlib import Path

from duet import agent_gate, gate_cache
from duet.adapters.claude_code import ClaudeCodeAdapter
from duet.adapters.mock import MockAdapter, envelope
from duet.cli import main
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator
from duet.workspace import GateResult, Workspace

DONE = envelope("I would ship this", "DONE", confidence=0.9)
SESSION = "20260101-000000-abcd"

STAMP = """
from pathlib import Path
p = Path("out/stamp/x.txt")
p.parent.mkdir(parents=True, exist_ok=True)
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
"""

# Long enough that the harness trims it, with the failure in the middle — the
# part a head-and-tail trim drops.
FAILING = """
import sys
print("collecting ...")
for i in range(200):
    print("tests/test_a.py::test_ok_%d PASSED" % i)
print("FAILED tests/test_a.py::test_the_middle - assert 1 == 2")
for i in range(200):
    print("tests/test_b.py::test_ok_%d PASSED" % i)
print("1 failed, 400 passed")
sys.exit(3)
"""


def gate(tmp_path, body, name="gate.py"):
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return '"%s" "%s"' % (sys.executable, script)


def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return root


def session(root, gate_cmd, gate_timeout=600):
    """A session directory the way Orchestrator.run leaves it before turn 1."""
    path = root / ".duet" / "sessions" / SESSION
    path.mkdir(parents=True)
    event = {"kind": "session_start", "session": SESSION, "root": str(root),
             "gate": gate_cmd, "gate_timeout": gate_timeout}
    (path / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    return path


# -- the command ---------------------------------------------------------------
def test_duet_gate_runs_the_sessions_gate_and_reports_it_like_the_harness(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    where = session(root, gate(tmp_path, FAILING))
    monkeypatch.setenv("DUET_SESSION", SESSION)
    # From a subdirectory: the session is found above it. And the nesting guard,
    # which refuses `duet run` here, does not stop this: it starts no agents.
    (root / "pkg").mkdir()
    code = main(["gate", "-C", str(root / "pkg")])
    out = capsys.readouterr().out
    assert code == 3                                   # the gate's own code
    assert "FAILED (exit 3)" in out
    failures = out.index("Failures, from the whole output:")
    assert out.index("test_the_middle") > failures
    assert failures < out.index("Head and tail of the output:")
    # The whole output is saved in the session directory, and named.
    (run,) = agent_gate.read_runs(where)
    log = Path(run["log"])
    assert log.parent == where and str(log) in out
    assert "test_ok_150 PASSED" in log.read_text(encoding="utf-8")
    assert run["ok"] is False and run["exit_code"] == 3 and run["n"] == 1
    assert run["digest_before"] == run["digest_after"] and run["changed"] == {}
    assert "not the harness's gate" in out


def test_duet_gate_records_what_the_run_changed(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    where = session(root, gate(tmp_path, STAMP))
    monkeypatch.setenv("DUET_SESSION", SESSION)
    assert main(["gate", "-C", str(root)]) == 0
    assert main(["gate", "-C", str(root)]) == 0
    first, second = agent_gate.read_runs(where)
    assert "PASSED" in capsys.readouterr().out
    assert first["digest_before"] != first["digest_after"]
    found, left = first["changed"]["out/stamp/x.txt"]
    assert found is None and left
    assert second["changed"]["out/stamp/x.txt"][0] == left
    assert second["n"] == 2 and Path(first["log"]) != Path(second["log"])


def test_duet_gate_says_a_timeout_with_its_own_exit_code(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    session(root, gate(tmp_path, "import time\nprint('starting')\ntime.sleep(30)\n"), gate_timeout=1)
    monkeypatch.setenv("DUET_SESSION", SESSION)
    assert main(["gate", "-C", str(root)]) == agent_gate.TIMED_OUT_EXIT
    assert "TIMED OUT" in capsys.readouterr().out


def test_duet_gate_refuses_outside_a_session(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    monkeypatch.delenv("DUET_SESSION", raising=False)
    assert main(["gate", "-C", str(root)]) == agent_gate.USAGE_EXIT
    assert "not inside a duet session" in capsys.readouterr().out
    # Set, but naming no session here.
    monkeypatch.setenv("DUET_SESSION", SESSION)
    assert main(["gate", "-C", str(root)]) == agent_gate.USAGE_EXIT
    assert "no directory for session" in capsys.readouterr().out


def test_duet_gate_refuses_a_session_without_a_gate(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    where = session(root, "")
    monkeypatch.setenv("DUET_SESSION", SESSION)
    assert main(["gate", "-C", str(root)]) == agent_gate.USAGE_EXIT
    assert "has no gate" in capsys.readouterr().out
    assert agent_gate.read_runs(where) == []


# -- inside a real session ------------------------------------------------------
class GateRunner(MockAdapter):
    """A scripted agent that runs `duet gate` during its turn, as a real one
    would from its shell — the environment, cwd and all, are the session's."""

    def __init__(self, *args, then_write=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.edits_workspace = True
        self.codes = []
        self.then_write = then_write or {}

    def send(self, prompt, system="", round_no=0):
        self.codes.append(main(["gate", "-C", self.cwd]))
        for rel, content in self.then_write.items():
            path = Path(self.cwd) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return super().send(prompt, system=system, round_no=round_no)


def run(root, claude, gpt_script, gate_cmd, max_rounds=6):
    cfg = Config(task="t", root=str(root),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=max_rounds, gate=gate_cmd)
    orch = Orchestrator(cfg, adapters={
        "claude": claude,
        "gpt": MockAdapter(name="gpt", cwd=str(root), config={"script": gpt_script}),
    })
    return orch, orch.run()


def remembered(root, cmd):
    """A pass for the workspace as it is, remembered from an earlier session —
    so the opening check does not run the gate, and the first run in the
    session is the agent's own."""
    gate_cache.remember(str(root), Workspace(str(root)).digest(),
                        GateResult(command=cmd, ok=True, exit_code=0, output="ok"))
    return cmd


def events(result, kind):
    path = Path(result.session_dir) / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("kind") == kind]


def test_the_harness_learns_gate_outputs_from_an_agents_own_run(tmp_path):
    """The opening check is a pass remembered from an earlier session, so the
    first time the gate runs in this one is the agent's own run, in its turn.
    On the original code what that run wrote counted as the agent's change, so
    it could never be a gate output: each harness run then rewrote it, the
    state moved, and two DONE votes never landed on the same digest."""
    root = workspace(tmp_path)
    cmd = remembered(root, gate(tmp_path, STAMP))
    claude = GateRunner(name="claude", cwd=str(root), config={"script": [DONE] * 6})
    orch, result = run(root, claude, [DONE] * 6, cmd)
    assert events(result, "gate_remembered")
    assert result.status == "consensus", result.reason
    assert claude.codes[0] == 0
    assert orch.workspace.gate_outputs == {"out"}
    assert "out/stamp/x.txt" not in orch.agent_changed
    (learned,) = events(result, "gate_outputs")
    assert learned["paths"] == ["out"] and learned["agent"] == "claude"
    ran = events(result, "agent_gate")
    assert ran and ran[0]["agent"] == "claude" and ran[0]["ok"] is True
    # The peer is shown it, and the agent was told how to run the gate.
    assert "CLAUDE RAN THE GATE ITSELF (duet gate)" in orch.adapters["gpt"].prompts[0]
    assert orch.agent_gate_command in claude.systems[0]
    assert any("RAN GATE" in line for line in orch.history)


def test_an_agent_edit_after_its_own_gate_run_stays_the_agents(tmp_path):
    """The gate wrote out/stamp/x.txt, then the agent overwrote it: that is the
    agent's work and must keep counting, exactly as before."""
    root = workspace(tmp_path)
    claude = GateRunner(name="claude", cwd=str(root), config={"script": [DONE] * 4},
                        then_write={"out/stamp/x.txt": "100"})
    orch, result = run(root, claude, [DONE] * 4, remembered(root, gate(tmp_path, STAMP)),
                       max_rounds=3)
    assert "out/stamp/x.txt" in orch.agent_changed
    assert not orch.workspace.is_gate_output("out/stamp/x.txt")
    assert not [e for e in events(result, "gate_outputs") if e.get("agent")]


def test_an_agents_own_passing_run_never_counts_as_the_harness_gate(tmp_path):
    """The gate passes the first time it runs and fails every time after. The
    agent's own run is the pass; the harness's run, the only one a sign-off is
    counted against, fails — so two DONE votes are not a consensus."""
    root = workspace(tmp_path)
    counter = tmp_path / "runs.txt"
    once = """
import sys
from pathlib import Path
p = Path(%r)
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
sys.exit(0 if n == 1 else 1)
""" % str(counter)
    claude = GateRunner(name="claude", cwd=str(root), config={"script": [DONE] * 4})
    orch, result = run(root, claude, [DONE] * 4, remembered(root, gate(tmp_path, once)),
                       max_rounds=2)
    assert claude.codes[0] == 0                       # the agent's run passed
    assert result.status != "consensus"
    assert events(result, "agent_gate")[0]["ok"] is True
    harness = events(result, "gate_done")
    assert harness and not any(e["ok"] for e in harness)


def test_written_by_agent_gates_follows_each_path_run_by_run():
    start = {"a": "1", "b": "1", "c": "1"}
    runs = [
        {"changed": {"a": ["1", "2"], "b": ["1", "2"], "new": [None, "x"]}},
        # b was edited by the agent between the runs: found 9, not 2.
        {"changed": {"a": ["2", "3"], "b": ["9", "3"]}},
    ]
    now = {"a": "3", "b": "3", "c": "5", "new": "y"}   # new edited after the runs
    assert Orchestrator._written_by_agent_gates(runs, start, now) == ["a"]


def test_forged_agent_gate_record_without_saved_log_cannot_hide_an_agent_edit(tmp_path):
    root = workspace(tmp_path)
    cmd = remembered(root, gate(tmp_path, STAMP))
    claude = GateRunner(name="claude", cwd=str(root), config={"script": [DONE]})
    orch, result = run(root, claude, [DONE], cmd, max_rounds=1)
    session_dir = Path(result.session_dir)
    # A record alone is not proof that duet gate ran. If it could classify
    # changes, an agent could append JSON and conceal an untracked file.
    start = orch.workspace.fingerprint(include_gate_outputs=True)
    path = root / "notes.txt"
    path.write_text("agent work", encoding="utf-8")
    after = dict(start)
    import hashlib
    after["notes.txt"] = hashlib.sha256(b"agent work").hexdigest()
    forged = {"changed": {"notes.txt": [None, after["notes.txt"]]},
              "digest_before": "0" * 16, "log": str(session_dir / "missing.log")}
    (session_dir / agent_gate.AGENT_GATES).write_text(json.dumps(forged) + "\n", encoding="utf-8")
    orch._agent_gates_seen = len(agent_gate.read_runs(session_dir)) - 1
    orch._note_agent_changes("claude", 1, start)
    assert "notes.txt" in orch.agent_changed
    assert not orch.workspace.is_gate_output("notes.txt")


# -- permissions -----------------------------------------------------------------
def allowed(adapter):
    """The rules as Claude Code reads its one --allowedTools string: split on
    spaces and commas, but never inside a rule's parentheses."""
    command = adapter._command("hi", "")
    return re.findall(r"[^\s,(]+\([^)]*\)|[^\s,]+", command[command.index("--allowedTools") + 1])


def test_claude_may_run_the_read_only_commands_and_never_a_broad_git_rule(tmp_path):
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    rules = allowed(agent)
    for rule in ("Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)",
                 "Bash(wc:*)", "Bash(ls:*)", "Bash(git status:*)"):
        assert rule in rules
    git = [r for r in rules if r.startswith("Bash(git")]
    assert git == ["Bash(git status:*)"]
    assert not any(r in rules for r in ("Bash(git:*)", "Bash(git *)", "Bash(duet:*)"))


def test_a_session_with_a_gate_lets_claude_run_duet_gate_and_nothing_wider(tmp_path):
    root = workspace(tmp_path)
    claude = ClaudeCodeAdapter(name="claude", cwd=str(root))
    cfg = Config(task="t", root=str(root),
                 agents=[AgentSpec("claude", "claude-code"), AgentSpec("gpt", "mock")],
                 gate=gate(tmp_path, STAMP))
    orch = Orchestrator(cfg, adapters={"claude": claude,
                                       "gpt": MockAdapter(name="gpt", cwd=str(root))})
    assert orch.agent_gate_command.endswith(" gate")
    assert "Bash(%s:*)" % orch.agent_gate_command in allowed(claude)

    bare = ClaudeCodeAdapter(name="claude", cwd=str(root))
    cfg = Config(task="t", root=str(root),
                 agents=[AgentSpec("claude", "claude-code"), AgentSpec("gpt", "mock")])
    orch = Orchestrator(cfg, adapters={"claude": bare,
                                       "gpt": MockAdapter(name="gpt", cwd=str(root))})
    assert orch.agent_gate_command == ""
    assert not any(" gate:" in r for r in allowed(bare))
