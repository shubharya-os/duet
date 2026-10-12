"""The gate runs when it decides something, not after every turn.

Measured over 89 finished sessions: 331 gate runs, 65.7 of 223 hours, came
right after a turn that said CONTINUE — a report on half-done work nobody can
sign off. So after a CONTINUE the gate is deferred, and runs when someone votes
DONE (or BLOCKED), whenever a sign-off would be counted, at the opening check,
and once at the end for the report. A deferred gate is never a pass.

Nothing this change introduced is imported at the top of this file: the
harness replays these tests against the code as it was before the session, and
they must fail there on what they assert, not on an ImportError for a new name.
"""

import json
import sys
from pathlib import Path

from duet.adapters.mock import MockAdapter, envelope
from duet.cli import build_parser, main
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator

DONE = envelope("I would ship this", "DONE", confidence=0.9)


def edit(text, verdict="CONTINUE", path="app.py"):
    """A turn that changes the workspace, so the state it leaves is new."""
    return envelope("wrote %s" % text, verdict, patches=[{"path": path, "content": text + "\n"}])


def python_gate(tmp_path, body):
    """A gate that runs `body` as Python and counts every time it starts.
    The counter lives outside the workspace, so the files never change."""
    runs = tmp_path / "runs"
    script = tmp_path / "gate_script.py"
    script.write_text("import os, sys\nopen(%r, 'a').write('run\\n')\n%s\n" % (str(runs), body),
                      encoding="utf-8")
    return '"%s" "%s"' % (sys.executable, script), runs


def count(runs):
    return len(runs.read_text(encoding="utf-8").splitlines()) if runs.exists() else 0


def session(tmp_path, gate, script, peer, **kwargs):
    root = tmp_path / "ws"
    root.mkdir()
    cfg = Config(task="add it", root=str(root),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=kwargs.pop("max_rounds", 4), gate=gate, **kwargs)
    adapters = {
        "claude": MockAdapter(name="claude", cwd=str(root), config={"script": list(script)}),
        "gpt": MockAdapter(name="gpt", cwd=str(root), config={"script": list(peer)}),
    }
    return Orchestrator(cfg, adapters=adapters)


def events(orch):
    path = Path(orch.session_dir) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def kinds(orch, kind):
    return [e for e in events(orch) if e["kind"] == kind]


# -- deferral ----------------------------------------------------------------
def test_the_gate_is_not_run_after_a_turn_that_says_continue(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    orch = session(tmp_path, gate, [edit("one")], [edit("two")], max_rounds=2)
    result = orch.run()

    # The opening check, and one run at the end for the report on the state
    # the session stopped on. Not one per CONTINUE turn.
    assert count(runs) == 2
    done = kinds(orch, "turn_done")
    assert [e.get("gate_deferred") for e in done] == [True, True]
    assert [e.get("gate_ok") for e in done] == [None, None]
    assert all("gate deferred (CONTINUE)" in line for line in orch.history)
    report = Path(result.report).read_text(encoding="utf-8")
    assert "gate deferred (CONTINUE)" in report
    # The report's gate is a real run on the final state, not the deferral.
    assert result.gate.ok and not getattr(result.gate, "deferred", False)
    assert kinds(orch, "gate_done")[-1]["digest"] == result.digest


def test_the_gate_runs_on_the_state_a_done_turn_left(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    orch = session(tmp_path, gate, [edit("one", "DONE")], [edit("two")], max_rounds=2)
    orch.run()

    first, second = kinds(orch, "turn_done")
    assert first.get("gate_ok") is True and not first.get("gate_deferred")
    assert second.get("gate_deferred") is True
    # The run came straight after the DONE turn, on the state it left.
    log = events(orch)
    after = log.index(next(e for e in log if e["kind"] == "turn_done"))
    ran = [e for e in log[:after] if e["kind"] == "gate_done"]
    assert ran[-1]["digest"] == first["digest"]


def test_a_blocked_turn_runs_the_gate(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    blocked = envelope("cannot go on", "BLOCKED", patches=[{"path": "app.py", "content": "x\n"}])
    orch = session(tmp_path, gate, [blocked], [DONE], max_rounds=2)
    orch.run()

    (turn,) = kinds(orch, "turn_done")
    assert turn.get("gate_ok") is True and not turn.get("gate_deferred")


def test_continue_then_peer_done_without_an_edit_runs_the_gate_before_the_signoff(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    orch = session(tmp_path, gate, [edit("one"), DONE], [DONE], max_rounds=3)
    result = orch.run()

    assert result.status == "consensus", result.reason
    log = events(orch)
    turns = [e for e in log if e["kind"] == "turn_done"]
    assert turns[0].get("gate_deferred") is True
    state = turns[0]["digest"]
    # Nothing ran on the CONTINUE state until gpt voted DONE on it, and then
    # it ran before gpt's turn was recorded — before the sign-off counted.
    peer_start = log.index(next(e for e in log if e["kind"] == "turn_start" and e["agent"] == "gpt"))
    peer_done = log.index(turns[1])
    on_state = [i for i, e in enumerate(log) if e["kind"] == "gate_done" and e["digest"] == state]
    assert on_state and peer_start < on_state[0] < peer_done
    # Opening check plus the one run on the agreed state.
    assert count(runs) == 2


def test_consensus_is_never_reached_on_a_state_whose_gate_was_only_deferred(tmp_path, monkeypatch):
    """Force every turn's gate to be deferred — both agents vote DONE and the
    gate has not run after either turn. The sign-off still waits for a real
    run on that exact state, and a failing one keeps the session open."""
    gate, runs = python_gate(tmp_path, "sys.exit(1)")
    orch = session(tmp_path, gate, [edit("one", "DONE"), DONE], [DONE, DONE], max_rounds=4)
    monkeypatch.setattr(orch, "gate_after_turn",
                        lambda digest, verdict: orch.deferred_gate(digest, "forced by the test"))
    result = orch.run()

    assert result.status != "consensus"
    assert not result.gate.ok
    failed = {e["digest"] for e in kinds(orch, "gate_done") if not e["ok"]}
    assert result.digest in failed


def test_a_deferred_gate_counts_as_a_pass_nowhere(tmp_path):
    from duet import gate_cache
    from duet.workspace import GateResult

    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    orch = session(tmp_path, gate, [], [])
    passed = GateResult(command=gate, ok=True, exit_code=0, output="all good")
    deferred = GateResult.deferral(gate, "the last turn said CONTINUE", passed, "abcdef1234")

    assert deferred.deferred and not deferred.ok and not deferred.skipped
    # Not by consensus...
    for agent in ("claude", "gpt"):
        orch.state.ingest(json_envelope(agent, "DONE"), "d" * 64)
    assert orch.state.decide("d" * 64, True).kind == "consensus"      # the control
    assert orch.state.decide("d" * 64, deferred.ok).kind != "consensus"
    # ...nor by the cache another session's opening check trusts...
    gate_cache.remember(orch.config.root, "d" * 64, deferred)
    assert gate_cache.recall(orch.config.root, "d" * 64, gate) is None
    # ...nor by what the agents are told.
    assert "PASSED" not in deferred.headline()
    assert "not run" in deferred.problem()


def test_a_digest_with_a_signoff_on_it_is_gated_at_the_start_of_a_round(tmp_path):
    """The round-start lookup defers like the rest — unless a sign-off on that
    exact state is about to be counted, which needs the real result."""
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    orch = session(tmp_path, gate, [], [])
    digest = orch.workspace.digest()

    shown = orch.round_gate(digest)
    assert shown.deferred and count(runs) == 0

    orch.state.ingest(json_envelope("claude", "DONE"), digest)
    assert orch.state.signoffs.get("claude")
    shown = orch.round_gate(digest)
    assert shown.ok and not shown.deferred and count(runs) == 1


def json_envelope(agent, verdict):
    from duet.protocol import parse_envelope
    return parse_envelope(envelope("x", verdict, confidence=0.9), agent=agent, round_no=1)


# -- what the next agent is told ----------------------------------------------
def test_the_next_agent_is_told_the_gate_was_deferred_and_sees_the_last_real_result(tmp_path):
    gate, runs = python_gate(tmp_path, "print('BOOM-MARKER in test_thing'); sys.exit(1)")
    orch = session(tmp_path, gate, [edit("one")], [edit("two")], max_rounds=2)
    opening = orch.workspace.digest()
    orch.run()

    prompt = orch.adapters["gpt"].prompts[0]
    assert "not run on this state" in prompt
    assert "CONTINUE" in prompt
    # The most recent run that did happen, and which state it was for.
    assert "BOOM-MARKER" in prompt
    assert opening[:8] in prompt
    assert "votes DONE" in prompt
    assert "targeted tests" in prompt
    # The FINAL CHECK is never offered on a deferred state.
    assert "FINAL CHECK" not in prompt


def test_the_terminal_says_the_gate_was_deferred(tmp_path, capsys):
    from duet.cli import make_reporter

    say = make_reporter(["claude", "gpt"], verbose=False)
    say({"kind": "turn_done", "agent": "claude", "verdict": "CONTINUE", "digest": "abcdef1234",
         "gate_ok": None, "gate_deferred": True})
    out = capsys.readouterr().out
    assert "gate deferred (CONTINUE)" in out
    assert "FAILING" not in out


# -- --gate-every-turn ----------------------------------------------------------
def test_gate_every_turn_restores_a_run_after_every_turn(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    orch = session(tmp_path, gate, [edit("one")], [edit("two")], max_rounds=2,
                   gate_every_turn=True)
    orch.run()

    # Opening, after each turn; the end reuses the last turn's run.
    assert count(runs) == 3
    assert [e.get("gate_ok") for e in kinds(orch, "turn_done")] == [True, True]
    assert not any(e.get("gate_deferred") for e in kinds(orch, "turn_done"))


def test_every_session_command_takes_gate_every_turn(tmp_path):
    from duet.cli import build_config

    for command in ("run", "fix", "add", "refactor", "plan", "build", "resume"):
        args = build_parser().parse_args([command, "something", "--gate-every-turn"])
        assert args.gate_every_turn is True
    args = build_parser().parse_args(["fix", "the bug", "-C", str(tmp_path), "--gate-every-turn",
                                      "--no-gate"])
    assert build_config(args).gate_every_turn is True
    args = build_parser().parse_args(["fix", "the bug", "-C", str(tmp_path), "--no-gate"])
    assert build_config(args).gate_every_turn is False


def test_resume_keeps_gate_every_turn(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    write = edit("one")
    cfg = Config(task="add it", root=str(tmp_path), gate=gate, max_rounds=1, gate_every_turn=True,
                 agents=[AgentSpec("claude", "mock", options={"script": [write, edit("three")]}),
                         AgentSpec("gpt", "mock", options={"script": [edit("two")]})],
                 start="claude")
    first = Orchestrator(cfg)
    first.run()
    saved = json.loads((Path(first.session_dir) / "state.json").read_text(encoding="utf-8"))
    assert saved["config"]["gate_every_turn"] is True

    assert main(["resume", "-C", str(tmp_path), "--rounds", "2"]) in (0, 1)
    newest = sorted((tmp_path / ".duet" / "sessions").iterdir())[-1]
    state = json.loads((newest / "state.json").read_text(encoding="utf-8"))
    assert state["config"]["gate_every_turn"] is True
    log = [json.loads(line) for line in (newest / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    resumed = [e for e in log if e["kind"] == "turn_done"]
    assert resumed and all(e.get("gate_ok") is True and not e.get("gate_deferred") for e in resumed)


# -- workflow rules that judge by what the gate has seen -----------------------
def test_fix_keeps_gating_continue_turns_only_until_it_has_seen_the_bug_red(tmp_path):
    """`fix` falls back on "the gate went red during the session" when there
    is nothing to replay, and the pair leaves that red under a CONTINUE. So
    CONTINUE turns are gated until a red is seen, and deferred after it."""
    gate, runs = python_gate(
        tmp_path, "sys.exit(open('state.txt').read().strip() == 'red' if os.path.exists('state.txt') else 0)")
    orch = session(tmp_path, gate, [edit("red", path="state.txt"), edit("still", path="other.txt")],
                   [edit("green", path="state.txt")], max_rounds=3, workflow="fix")
    orch.run()

    done = kinds(orch, "turn_done")
    assert done[0].get("gate_ok") is False and not done[0].get("gate_deferred")
    assert orch.config.workflow_state.get("reproduced") is True
    assert [e.get("gate_deferred") for e in done[1:]] == [True, True]
