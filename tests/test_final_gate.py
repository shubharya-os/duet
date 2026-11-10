"""`--final-gate`: a slow gate run only where a consensus would be reached.

Measured on duet 0.17 sessions over iOS projects (unit tests plus a ~30 minute
Xcode UI-test run as the gate): 87% and 69% of the wall time went to the gate,
12 and 6 runs, most of which only needed to know the code compiled and the unit
tests passed. So the fast gate (`--gate`) does everything it does today, and the
final gate runs once per agreed state, just before the pair would finish.

Nothing this change introduced is imported at the top of this file: the
harness replays these tests against the code as it was before the session, and
they must fail there on what they assert, not on an ImportError for a new name.
"""

import json
import sys
from pathlib import Path

from duet.adapters.mock import MockAdapter, envelope
from duet.cli import build_config, build_parser, main
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator

DONE = envelope("I would ship this", "DONE", confidence=0.9)


def edit(text, verdict="CONTINUE", path="app.py", **kwargs):
    return envelope("wrote %s" % text, verdict, patches=[{"path": path, "content": text + "\n"}],
                    **kwargs)


def counted_gate(tmp_path, name, body):
    """A gate that runs `body` as Python in the workspace and counts its runs.
    The counter lives outside the workspace, so the files never change."""
    runs = tmp_path / ("%s-runs" % name)
    script = tmp_path / ("%s.py" % name)
    script.write_text("import os, sys, time\nopen(%r, 'a').write('run\\n')\n%s\n" % (str(runs), body),
                      encoding="utf-8")
    return '"%s" "%s"' % (sys.executable, script), runs


def count(runs):
    return len(runs.read_text(encoding="utf-8").splitlines()) if runs.exists() else 0


# The final gate fails, naming a UI test, until app.py says "good".
FINAL_NEEDS_GOOD = (
    "text = open('app.py').read() if os.path.exists('app.py') else ''\n"
    "if 'good' in text:\n"
    "    print('UI suite: 12 passed')\n"
    "    sys.exit(0)\n"
    "print('FAILED UITests/test_login_button_is_visible')\n"
    "sys.exit(1)"
)


def session(tmp_path, script, peer, final_body="sys.exit(0)", fast_body="sys.exit(0)", **kwargs):
    fast, fast_runs = counted_gate(tmp_path, "fast", fast_body)
    final, final_runs = counted_gate(tmp_path, "final", final_body)
    if kwargs.pop("no_final", False):
        final = ""
    root = tmp_path / "ws"
    root.mkdir()
    cfg = Config(task="add it", root=str(root),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=kwargs.pop("max_rounds", 4), gate=fast,
                 final_gate=final, **kwargs)
    adapters = {
        "claude": MockAdapter(name="claude", cwd=str(root), config={"script": list(script)}),
        "gpt": MockAdapter(name="gpt", cwd=str(root), config={"script": list(peer)}),
    }
    return Orchestrator(cfg, adapters=adapters), fast_runs, final_runs


def events(orch):
    path = Path(orch.session_dir) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def kinds(orch, kind):
    return [e for e in events(orch) if e["kind"] == kind]


# -- consensus ---------------------------------------------------------------
def test_two_dones_with_both_gates_green_reach_consensus_and_the_final_gate_ran_once(tmp_path):
    orch, fast_runs, final_runs = session(tmp_path, [DONE], [DONE])
    result = orch.run()

    assert result.status == "consensus", result.reason
    assert count(final_runs) == 1
    assert count(fast_runs) >= 1
    (done,) = kinds(orch, "final_gate_done")
    assert done["ok"] is True and done["digest"] == result.digest
    # It ran after both sign-offs, not before.
    log = events(orch)
    turns = [i for i, e in enumerate(log) if e["kind"] == "turn_done"]
    assert len(turns) == 2 and log.index(done) > turns[-1]
    start = kinds(orch, "session_start")[0]
    assert start["final_gate"] == orch.config.final_gate
    report = Path(result.report).read_text(encoding="utf-8")
    assert "## Final gate" in report
    assert "Both gates passed on state `%s`" % result.digest[:8] in report


def test_a_red_final_gate_blocks_consensus_and_the_session_goes_on_until_it_is_fixed(tmp_path):
    orch, fast_runs, final_runs = session(
        tmp_path, [DONE, edit("good", "DONE")], [DONE, DONE], final_body=FINAL_NEEDS_GOOD)
    result = orch.run()

    # Rounds 1-2: both DONE, fast green, final red -> no consensus.
    (veto,) = kinds(orch, "final_gate_veto")
    first_done = kinds(orch, "final_gate_done")[0]
    assert first_done["ok"] is False and veto["digest"] == first_done["digest"]
    # Both sign-offs were cleared: claude's round-3 turn is not a final check,
    # and both agents were shown the final gate's output as the reason.
    claude, gpt = orch.adapters["claude"], orch.adapters["gpt"]
    for prompt in (claude.prompts[1], gpt.prompts[1]):
        assert "THE FINAL GATE FAILED ON THIS STATE" in prompt
        assert "FAILED UITests/test_login_button_is_visible" in prompt
    # Rounds 3-4: fixed, both DONE again -> final gate green -> consensus.
    assert result.status == "consensus", result.reason
    assert result.rounds == 4
    assert count(final_runs) == 2
    assert [e["ok"] for e in kinds(orch, "final_gate_done")] == [False, True]
    assert (Path(orch.session_dir) / ("final-gate-%s.log" % first_done["digest"][:8])).is_file()


def test_sign_offs_are_cleared_when_the_final_gate_fails(tmp_path):
    orch, _, _ = session(tmp_path, [DONE], [DONE], final_body=FINAL_NEEDS_GOOD, max_rounds=2)
    result = orch.run()

    assert result.status != "consensus"
    assert orch.state.signoffs == {}
    report = Path(result.report).read_text(encoding="utf-8")
    assert "the final gate is failing on this state" in report
    assert "FAILED UITests/test_login_button_is_visible" in report


def test_a_final_gate_that_times_out_is_not_green(tmp_path):
    orch, _, final_runs = session(tmp_path, [DONE], [DONE], final_body="time.sleep(30)",
                                  max_rounds=2, gate_timeout=2)
    result = orch.run()

    assert result.status != "consensus"
    (done,) = kinds(orch, "final_gate_done")
    assert done["ok"] is False and done["timed_out"] is True
    assert "THE FINAL GATE TIMED OUT ON THIS STATE" in orch.pending_directive.get("claude", "")


# -- when it does not run ------------------------------------------------------
def test_the_final_gate_never_runs_on_continue_run_gate_or_a_single_sign_off(tmp_path):
    orch, fast_runs, final_runs = session(
        tmp_path,
        [edit("one", run_gate=True), edit("two")],
        [DONE, edit("three", "DONE")],
        max_rounds=4)
    result = orch.run()

    assert result.status != "consensus"
    # The run_gate request ran the fast gate, never the final one.
    assert any(e.get("gate_requested") and e.get("gate_ok") is True for e in kinds(orch, "turn_done"))
    assert count(fast_runs) >= 2
    assert count(final_runs) == 0
    assert not kinds(orch, "final_gate_start")
    report = Path(result.report).read_text(encoding="utf-8")
    assert "Not run on state" in report


def test_the_final_gate_waits_for_the_fast_gate(tmp_path):
    orch, _, final_runs = session(tmp_path, [DONE], [DONE], fast_body="sys.exit(1)", max_rounds=2)
    result = orch.run()

    assert result.status != "consensus"
    assert count(final_runs) == 0


def test_the_same_state_is_not_final_gated_again_beyond_the_one_re_run(tmp_path):
    # Both sign off on the same unchanged state three times; the final gate
    # keeps failing. It runs once, is re-run once (the flaky rule), and the
    # third agreement is answered from the cache.
    orch, _, final_runs = session(tmp_path, [DONE] * 3, [DONE] * 3,
                                  final_body=FINAL_NEEDS_GOOD, max_rounds=6)
    result = orch.run()

    assert result.status != "consensus"
    assert len(kinds(orch, "final_gate_veto")) == 3
    assert count(final_runs) == 2
    assert len(kinds(orch, "final_gate_rerun")) == 1
    assert len({e["digest"] for e in kinds(orch, "final_gate_done")}) == 1


def test_a_final_gate_that_passes_on_its_re_run_is_called_flaky(tmp_path):
    # Fails on its first run only: the counter file it appends to says which run.
    body = ("n = len(open(%r).read().splitlines())\n"
            "sys.exit(1 if n == 1 else 0)" % str(tmp_path / "final-runs"))
    orch, _, final_runs = session(tmp_path, [DONE, DONE], [DONE, DONE], final_body=body)
    result = orch.run()

    assert result.status == "consensus", result.reason
    assert count(final_runs) == 2
    (flaky,) = kinds(orch, "final_gate_flaky")
    assert "possibly flaky" in flaky["note"]


def test_the_final_gate_is_never_remembered_across_sessions(tmp_path):
    orch, _, final_runs = session(tmp_path, [DONE], [DONE])
    orch.run()
    cache = Path(orch.config.root) / ".duet" / "gate-cache.json"
    if cache.exists():
        assert orch.config.final_gate not in cache.read_text(encoding="utf-8")


def test_a_final_gate_result_is_kept_when_the_same_session_is_resumed(tmp_path):
    orch, _, final_runs = session(tmp_path, [DONE], [DONE])
    digest = orch.workspace.digest()
    result = orch._run_final_gate(digest)
    orch._final_cache[digest] = result
    orch._save()
    state = json.loads((Path(orch.session_dir) / "state.json").read_text(encoding="utf-8"))

    resumed_cfg = Config.from_dict(orch.config.to_dict())
    resumed = Orchestrator(resumed_cfg, adapters={
        "claude": MockAdapter(name="claude", cwd=resumed_cfg.root, config={"script": [DONE]}),
        "gpt": MockAdapter(name="gpt", cwd=resumed_cfg.root, config={"script": [DONE]}),
    }, session_id=orch.session_id)
    resumed.restore(state)

    assert resumed.final_gate_for(digest).ok is True
    assert count(final_runs) == 1


# -- prompts -------------------------------------------------------------------
def test_agents_are_told_about_both_gates(tmp_path):
    orch, _, _ = session(tmp_path, [DONE], [DONE])
    orch.run()

    system = orch.adapters["claude"].systems[0]
    assert "two gates" in system
    assert orch.config.gate in system and orch.config.final_gate in system
    assert "only before consensus" in system
    assert "Neither \"run_gate\" nor `duet gate` runs the final gate" in system


def test_without_a_final_gate_nothing_changes(tmp_path):
    orch, _, _ = session(tmp_path, [DONE], [DONE], no_final=True)
    result = orch.run()

    assert result.status == "consensus", result.reason
    assert not [e for e in events(orch) if e["kind"].startswith("final_gate")]
    assert "final_gate" not in kinds(orch, "session_start")[0]
    for adapter in orch.adapters.values():
        for text in adapter.systems + adapter.prompts:
            assert "final gate" not in text.lower()
    assert "Final gate" not in Path(result.report).read_text(encoding="utf-8")


# -- configuration ---------------------------------------------------------------
def test_every_session_command_takes_final_gate(tmp_path):
    for command in ("run", "fix", "add", "refactor", "plan", "build", "resume"):
        args = build_parser().parse_args([command, "something", "--final-gate", "bash full.sh"])
        assert args.final_gate == "bash full.sh"
    args = build_parser().parse_args(["run", "x", "-C", str(tmp_path), "--gate", "true",
                                      "--final-gate", "bash full.sh"])
    assert build_config(args).final_gate == "bash full.sh"


def test_final_gate_is_read_from_the_project_config(tmp_path):
    (tmp_path / ".duet").mkdir()
    (tmp_path / ".duet" / "config.json").write_text(
        json.dumps({"gate": "true", "final_gate": "bash full.sh"}), encoding="utf-8")
    args = build_parser().parse_args(["run", "x", "-C", str(tmp_path)])
    assert build_config(args).final_gate == "bash full.sh"


def test_resume_keeps_the_final_gate(tmp_path):
    fast, _ = counted_gate(tmp_path, "fast", "sys.exit(0)")
    final, final_runs = counted_gate(tmp_path, "final", "sys.exit(0)")
    root = tmp_path / "ws"
    root.mkdir()
    cfg = Config(task="add it", root=str(root), gate=fast, final_gate=final, max_rounds=1,
                 agents=[AgentSpec("claude", "mock", options={"script": [edit("one"), DONE]}),
                         AgentSpec("gpt", "mock", options={"script": [DONE]})],
                 start="claude")
    first = Orchestrator(cfg)
    first.run()
    saved = json.loads((Path(first.session_dir) / "state.json").read_text(encoding="utf-8"))
    assert saved["config"]["final_gate"] == final
    assert count(final_runs) == 0

    assert main(["resume", "-C", str(root), "--rounds", "3"]) in (0, 1)
    newest = sorted((root / ".duet" / "sessions").iterdir())[-1]
    state = json.loads((newest / "state.json").read_text(encoding="utf-8"))
    assert state["config"]["final_gate"] == final
    log = [json.loads(line) for line in (newest / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e for e in log if e["kind"] == "session_start"][-1]["final_gate"] == final
    # The resumed pair agreed, so the kept final gate ran on that state.
    assert [e for e in log if e["kind"] == "session_end"][-1]["status"] == "consensus"
    assert count(final_runs) == 1
