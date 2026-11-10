"""A gate that writes into the workspace must not keep two agents from agreeing.

Measured on 431 sessions: in 94 the gate changed the workspace when it ran, and
those reached consensus 33% of the time against 53%. Sign-offs are counted
against a digest of the files, so a gate that writes a build product moved the
digest on every run and no two sign-offs ever landed on the same state.

The gates here are small Python scripts kept *outside* the workspace (so they
are not part of its state) and run with this interpreter, which is what makes
them work on Windows as well. Their output goes to `out/` and `gen/`, not
`build/`: `build` is one of the directory names duet already skips, and the
bug is about the output paths no name list can cover.
"""

import json
import subprocess
import sys
from pathlib import Path

from duet.adapters.mock import MockAdapter, envelope
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator

DONE = envelope("I would ship this", "DONE", confidence=0.9)

# Each run writes a new value — a timestamp, in effect, but a counter so the
# test does not depend on the clock's resolution.
STAMP = """
from pathlib import Path
p = Path("out/stamp/x.txt")
p.parent.mkdir(parents=True, exist_ok=True)
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
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


def run(root, claude_script, gpt_script, gate_cmd, max_rounds=6, data=None):
    cfg = Config(task="t", root=str(root),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=max_rounds, gate=gate_cmd)
    orch = Orchestrator(cfg, adapters={
        "claude": MockAdapter(name="claude", cwd=str(root), config={"script": claude_script}),
        "gpt": MockAdapter(name="gpt", cwd=str(root), config={"script": gpt_script}),
    })
    if data is not None:
        orch.restore(data)
    return orch, orch.run()


def events(result, kind):
    path = Path(result.session_dir) / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("kind") == kind]


def test_a_gate_that_stamps_its_output_still_lets_two_done_votes_agree(tmp_path):
    """On the original code this ran out of rounds: every gate run moved the
    digest, so the second DONE was never cast on the state the first signed."""
    root = workspace(tmp_path)
    orch, result = run(root, [DONE] * 6, [DONE] * 6, gate(tmp_path, STAMP))
    assert result.status == "consensus", result.reason
    assert result.rounds == 2
    # One run: the state never moved, so every later check is answered from it.
    assert (root / "out/stamp/x.txt").read_text(encoding="utf-8") == "1"
    # The gate created out/ whole, so out/ is the one entry.
    assert orch.workspace.gate_outputs == {"out"}
    (said,) = events(result, "gate_outputs")
    assert said["paths"] == ["out"] and said["count"] == 1
    # The agents are told, and so is the report.
    line = "written by the gate and are not part of the signed state: out"
    assert line in orch.adapters["gpt"].prompts[-1]
    assert line in Path(result.report).read_text(encoding="utf-8")


def test_a_directory_the_gate_creates_whole_is_one_entry(tmp_path):
    root = workspace(tmp_path)
    many = """
from pathlib import Path
for i in range(60):
    p = Path("gen/deep/part%d/file%d.o" % (i % 4, i))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("object %d" % i)
"""
    orch, result = run(root, [DONE] * 4, [DONE] * 4, gate(tmp_path, many))
    assert result.status == "consensus"
    assert orch.workspace.gate_outputs == {"gen"}
    (said,) = events(result, "gate_outputs")
    assert said["paths"] == ["gen"] and said["count"] == 1 and said["files"] == 60


def test_a_file_written_into_an_existing_directory_is_named_alone(tmp_path):
    """data/ held a file before the gate ran, so it is not the gate's directory."""
    root = workspace(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "seed.csv").write_text("a,b\n", encoding="utf-8")
    body = STAMP.replace("out/stamp/x.txt", "data/run.log")
    orch, result = run(root, [DONE] * 4, [DONE] * 4, gate(tmp_path, body))
    assert result.status == "consensus"
    assert orch.workspace.gate_outputs == {"data/run.log"}
    # The seed is still part of the signed state.
    before = orch.workspace.digest()
    (root / "data" / "seed.csv").write_text("a,b,c\n", encoding="utf-8")
    assert orch.workspace.digest() != before


def test_an_agent_edit_to_a_gate_output_brings_it_back_into_the_state(tmp_path):
    """The gate writes out/x.txt once (only if missing). gpt then edits it: from
    then on it is gpt's work, part of the digest, and claude's earlier sign-off
    no longer covers it — consensus needs claude to sign the new state."""
    root = workspace(tmp_path)
    once = """
from pathlib import Path
p = Path("out/x.txt")
if not p.exists():
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("from the gate")
"""
    edit = envelope("I changed the output by hand", "DONE",
                    patches=[{"path": "out/x.txt", "content": "from gpt"}])
    orch, result = run(root, [DONE] * 4, [edit, DONE, DONE], gate(tmp_path, once))
    assert result.status == "consensus"
    assert result.rounds == 3                       # claude had to sign again
    assert orch.workspace.gate_outputs == set()
    (back,) = events(result, "gate_outputs_returned")
    assert back["agent"] == "gpt" and back["paths"] == ["out"]
    signed = {s.digest for s in orch.state.signoffs.values()}
    assert signed == {result.digest}
    (root / "out/x.txt").write_text("changed again", encoding="utf-8")
    assert orch.workspace.digest() != result.digest   # it counts again


def test_a_path_an_agent_changed_is_never_taken_as_a_gate_output(tmp_path):
    """gpt edits out/stamp/x.txt; the gate keeps rewriting it. That rewrite must
    keep moving the state, as before, rather than hide gpt's edit."""
    root = workspace(tmp_path)
    edit = envelope("touching the stamp", "CONTINUE",
                    patches=[{"path": "out/stamp/x.txt", "content": "100"}])
    orch, result = run(root, [DONE] * 4, [edit] + [DONE] * 4, gate(tmp_path, STAMP), max_rounds=5)
    assert result.status != "consensus"
    assert "out/stamp/x.txt" in orch.agent_changed
    assert not orch.workspace.is_gate_output("out/stamp/x.txt")
    assert events(result, "gate_outputs_returned")


def test_a_tracked_file_the_gate_rewrites_is_never_left_out(tmp_path):
    """A formatter rewriting committed source must keep moving the state, and
    keep the warning."""
    root = workspace(tmp_path)

    def git(*args):
        subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (root / "fmt.txt").write_text("0", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    body = STAMP.replace("out/stamp/x.txt", "fmt.txt")
    orch, result = run(root, [DONE] * 4, [DONE] * 4, gate(tmp_path, body), max_rounds=4)
    assert result.status != "consensus"
    assert orch.workspace.gate_outputs == set()
    assert not events(result, "gate_outputs")
    (warned,) = events(result, "gate_mutates")
    assert warned["moved"] == ["fmt.txt"]


def test_a_test_file_the_gate_writes_is_never_left_out(tmp_path):
    root = workspace(tmp_path)
    body = STAMP.replace("out/stamp/x.txt", "tests/test_generated.py")
    orch, result = run(root, [DONE] * 4, [DONE] * 4, gate(tmp_path, body), max_rounds=4)
    assert result.status != "consensus"
    assert orch.workspace.gate_outputs == set()


def test_verify_counts_the_state_the_way_the_sign_offs_did(tmp_path, capsys):
    """`duet verify` re-hashes the workspace: without the session's gate
    outputs it would include out/, and no sign-off would ever still hold."""
    from duet.cli import main

    root = workspace(tmp_path)
    _, result = run(root, [DONE] * 4, [DONE] * 4, gate(tmp_path, STAMP))
    assert result.status == "consensus"
    assert main(["verify", "-C", str(root), "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["match"] is True and out["current_digest"] == result.digest


def test_resume_keeps_the_gate_outputs(tmp_path):
    root = workspace(tmp_path)
    cmd = gate(tmp_path, STAMP)
    hold = envelope("still looking", "CONTINUE")
    orch, result = run(root, [hold], [hold], cmd, max_rounds=2)
    assert orch.workspace.gate_outputs == {"out"}
    saved = json.loads((Path(result.session_dir) / "state.json").read_text(encoding="utf-8"))
    assert saved["gate_outputs"] == ["out"]

    again, result = run(root, [DONE] * 4, [DONE] * 4, cmd, max_rounds=6, data=saved)
    assert again.workspace.gate_outputs == {"out"}
    assert result.status == "consensus", result.reason
    assert not events(result, "gate_outputs")      # nothing new to learn
