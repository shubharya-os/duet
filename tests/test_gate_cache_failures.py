"""Opening-gate failures remembered across sessions, and never decided on.

Measured on 0.15.0: 47 of 257 opening gates had already run on the identical
digest with the same command within the day, 37 of them failing, and every
failing one was re-run in full. A failure is now kept like a pass, shown to
the agents for what it is, and never fed to a workflow rule, counted toward or
against a sign-off, or re-checked as flaky.

The gates append a line to a log outside the workspace each time they run, so
a test counts real invocations without moving the workspace digest.
"""

import json
import sys
from pathlib import Path

from duet import build, gate_cache, workflows
from duet.config import AgentSpec, Config
from duet.orchestrator import STATUS_CONSENSUS, Orchestrator
from duet.workflow_commands import gate_before
from duet.workspace import GateResult

from tests.test_gate_cache import (DONE, WAIT, before_first_turn, cache_entries, counting_gate,
                                   events, runs, session, workspace)


def orchestrator(ws: Path, gate: str, **kwargs) -> Orchestrator:
    cfg = Config(task="build the thing", root=str(ws), gate=gate,
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=2, **kwargs)
    return Orchestrator(cfg, adapters={})


def remember_failure(ws: Path, gate: str, at=None, output="FAILED tests/test_x.py::test_y") -> str:
    """A failure of `gate` on the workspace as it is now, as a session would leave it."""
    digest = orchestrator(ws, gate).workspace.digest()
    gate_cache.remember(str(ws), digest, GateResult(command=gate, ok=False, exit_code=1,
                                                     output=output, duration=1860.0), at=at)
    return digest


def rewrite(ws: Path, entries) -> None:
    (ws / ".duet" / "gate-cache.json").write_text(
        json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


class Spy(workflows.Workflow):
    """A rule that has seen what it needs: it records every gate it is shown."""

    name = "spy"

    def __init__(self, wants=False):
        self.seen = []
        self.wants = wants

    def on_gate(self, state, digest, ok):
        self.seen.append((digest, ok))

    def wants_every_gate(self, state):
        return self.wants


# -- what is stored ---------------------------------------------------------

def test_a_failing_opening_gate_is_stored_with_its_exit_code_and_output(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=3)
    _, result, _ = session(ws, gate, [WAIT], [WAIT], max_rounds=1)
    [entry] = cache_entries(ws)
    assert entry["digest"] == result.digest
    assert entry["command"] == gate
    assert entry["ok"] is False and entry["exit_code"] == 3
    assert "output" in entry


def test_a_stored_output_is_capped_per_entry(tmp_path):
    ws = workspace(tmp_path)
    huge = "x" * (gate_cache.OUTPUT_CHARS * 5) + "THE END"
    gate_cache.remember(str(ws), "d", GateResult(command="pytest", ok=False, exit_code=1, output=huge))
    [entry] = cache_entries(ws)
    assert len(entry["output"]) == gate_cache.OUTPUT_CHARS
    assert entry["output"].endswith("THE END"), "the tail is where a test runner says what failed"


# -- what is reused ---------------------------------------------------------

def test_a_remembered_failure_is_reused_without_running_the_gate(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=1)
    digest = remember_failure(ws, gate)

    orch = orchestrator(ws, gate)
    gate_result = orch.opening_gate(digest)
    assert runs(log) == 0, "the opening gate ran over files that had just failed it"
    assert not gate_result.ok and gate_result.remembered
    assert gate_result.exit_code == 1
    assert "FAILED (remembered from a run on these exact files" in gate_result.headline()
    assert "31 minutes; not re-run" in gate_result.headline()
    assert "FAILED tests/test_x.py::test_y" in gate_result.render()
    # Kept apart from the session's own runs, which sign-offs are counted against.
    assert digest not in orch._gate_cache


def test_the_agents_and_the_terminal_are_told_it_was_not_re_run(tmp_path, capsys):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=1)
    session(ws, gate, [WAIT], [WAIT], max_rounds=1)
    assert runs(log) == 1

    _, result, adapters = session(ws, gate, [WAIT], [WAIT], max_rounds=1)
    opening = before_first_turn(result)
    assert "gate_start" not in opening and "gate_remembered" in opening
    remembered = next(e for e in events(result) if e["kind"] == "gate_remembered")
    assert remembered["ok"] is False and remembered["exit_code"] == 1
    prompt = adapters["claude"].prompts[0]
    assert "FAILED (remembered from a run on these exact files" in prompt
    assert "not re-run" in prompt

    from duet.cli import make_reporter
    make_reporter(["claude", "gpt"])(remembered)
    out = capsys.readouterr().out
    assert "gate failed" in out and "not re-run" in out and "gate passed" not in out


def test_a_failure_older_than_a_day_is_not_reused(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=1)
    import time
    digest = remember_failure(ws, gate, at=time.time() - 25 * 3600)
    orchestrator(ws, gate).opening_gate(digest)
    assert runs(log) == 1


def test_a_failure_on_other_files_or_for_another_command_is_not_reused(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=1)
    digest = remember_failure(ws, gate)

    orchestrator(ws, gate + " ").opening_gate(digest)
    assert runs(log) == 1

    (ws / "app.py").write_text("print('changed')\n", encoding="utf-8")
    orch = orchestrator(ws, gate)
    other = orch.workspace.digest()
    assert other != digest
    orch.opening_gate(other)
    assert runs(log) == 2


def test_fresh_gate_runs_over_a_remembered_failure(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=1)
    digest = remember_failure(ws, gate)
    result = orchestrator(ws, gate, fresh_gate=True).opening_gate(digest)
    assert runs(log) == 1 and not result.remembered


# -- what is never decided on it --------------------------------------------

def test_a_remembered_failure_is_never_fed_to_a_workflow_rule(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=1)
    digest = remember_failure(ws, gate)
    orch = orchestrator(ws, gate)
    orch.workflow = Spy(wants=False)
    result = orch.opening_gate(digest)
    assert result.remembered and not result.ok
    assert orch.workflow.seen == [], "a remembered failure reached Workflow.on_gate"
    assert runs(log) == 0


def test_a_rule_that_still_needs_the_gate_gets_a_real_run(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=1)
    digest = remember_failure(ws, gate)
    orch = orchestrator(ws, gate)
    orch.workflow = Spy(wants=True)
    result = orch.opening_gate(digest)
    assert runs(log) == 1 and not result.remembered
    assert orch.workflow.seen == [(digest, False)]


def test_fix_until_reproduced_runs_its_opening_gate_for_real(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    (ws / "tests").mkdir()
    (ws / "tests" / "test_app.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    gate = counting_gate(log, code=1)
    digest = remember_failure(ws, gate)

    orch = orchestrator(ws, gate, workflow="fix")
    orch.workflow.begin(str(ws), orch.config.workflow_state)
    assert orch.workflow.wants_every_gate(orch.config.workflow_state)
    result = orch.opening_gate(digest)
    assert runs(log) == 1 and not result.remembered
    # The red it needs was seen on this session's own run.
    assert orch.config.workflow_state["reproduced"] is True

    # Once reproduced (a resumed fix, say), it no longer needs to see the
    # gate, and the remembered failure is only shown.
    again = orchestrator(ws, gate, workflow="fix",
                         workflow_state=dict(orch.config.workflow_state))
    assert again.opening_gate(digest).remembered
    assert runs(log) == 1


def test_refactor_and_build_check_their_starting_gate_for_real(tmp_path):
    """refactor refuses a red start, build refuses a gate green with no tests:
    both are checked by running the gate before the session, never from the cache."""
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log, code=1)
    remember_failure(ws, gate)
    assert not gate_before(gate, str(ws)).ok
    assert runs(log) == 1
    build.gate_proves_nothing(gate, str(ws))
    assert runs(log) == 2


def test_consensus_still_rests_on_a_real_run(tmp_path):
    """The gate was red when remembered and is green now: the opening check
    shows red, and the sign-offs are counted against this session's own run."""
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    passing = counting_gate(log, code=0)
    remember_failure(ws, passing)

    _, result, _ = session(ws, passing, [DONE], [DONE])
    assert "gate_remembered" in before_first_turn(result)
    assert result.status == STATUS_CONSENSUS
    assert result.gate.ok and not result.gate.remembered
    assert runs(log) >= 1
    kinds = [e["kind"] for e in events(result)]
    assert kinds.index("gate_start") > kinds.index("turn_start")


def test_a_failure_that_passes_on_its_re_run_is_forgotten(tmp_path):
    """The flaky re-run applies to real runs only, and a failure it shows to be
    the machine is not left for the next session to open on."""
    ws = workspace(tmp_path)
    count = tmp_path / "count.log"
    # Fails the first time it runs, passes after.
    flaky = ("%s -c \"import os; p=r'%s'; n=os.path.exists(p); open(p,'a').write('ran\\n'); "
             "raise SystemExit(0 if n else 1)\"" % (sys.executable, count))
    _, result, _ = session(ws, flaky, [DONE, DONE], [DONE, DONE], max_rounds=4)
    assert any(e["kind"] == "gate_flaky" for e in events(result))
    assert not [e for e in cache_entries(ws) if e["digest"] == result.digest]
    assert gate_cache.recall(str(ws), result.digest, flaky) is None
