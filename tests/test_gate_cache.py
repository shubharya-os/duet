"""Gate passes remembered across sessions, to spare the opening wait.

A session used to run the whole gate before round 1 even when the files had
just passed that exact gate in another session — 5 to 15 minutes on a loaded
machine before an agent did anything. A pass is now kept in
.duet/gate-cache.json and reused for the opening check only: never a failure,
never for other files or another command, never past a day, and never as the
gate result a sign-off is counted against.

The gates here append a line to a log outside the workspace each time they
run, so a test can tell a run that happened from one that was remembered
without the log itself moving the workspace digest.
"""

import json
import sys
import time
from pathlib import Path

from duet import gate_cache
from duet.adapters.mock import MockAdapter, envelope
from duet.cli import build_parser, main
from duet.config import AgentSpec, Config
from duet.orchestrator import STATUS_CONSENSUS, Orchestrator
from duet.workspace import GateResult, Workspace

DONE = envelope("I would ship this", "DONE", confidence=0.9)
WAIT = envelope("still reading", "CONTINUE")


def counting_gate(log: Path, code: int = 0) -> str:
    return "%s -c \"open(r'%s', 'a').write('ran\\n'); raise SystemExit(%d)\"" % (
        sys.executable, log, code)


def runs(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return ws


def session(ws: Path, gate: str, claude, gpt, **kwargs):
    cfg = Config(
        task="build the thing",
        root=str(ws),
        gate=gate,
        agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
        start="claude",
        max_rounds=kwargs.pop("max_rounds", 4),
        **kwargs,
    )
    adapters = {
        "claude": MockAdapter(name="claude", cwd=str(ws), config={"script": claude}),
        "gpt": MockAdapter(name="gpt", cwd=str(ws), config={"script": gpt}),
    }
    orch = Orchestrator(cfg, adapters=adapters)
    return orch, orch.run(), adapters


def events(result):
    path = Path(result.session_dir) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def before_first_turn(result):
    out = []
    for event in events(result):
        if event["kind"] == "turn_start":
            break
        out.append(event["kind"])
    return out


def cache_entries(ws: Path):
    return json.loads((ws / ".duet" / "gate-cache.json").read_text(encoding="utf-8"))["entries"]


# -- what is stored ---------------------------------------------------------

def test_a_passing_gate_is_stored_with_digest_command_time_duration_and_output(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    orch, result, _ = session(ws, gate, [DONE], [DONE])
    assert result.status == STATUS_CONSENSUS
    [entry] = cache_entries(ws)
    assert entry["digest"] == result.digest == orch.workspace.digest()
    assert entry["command"] == gate
    assert entry["ok"] is True
    assert abs(entry["at"] - time.time()) < 120
    assert entry["duration"] >= 0
    assert "output" in entry


def test_a_failing_or_timed_out_gate_is_never_stored(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    session(ws, counting_gate(log, code=1), [WAIT], [WAIT], max_rounds=2)
    assert not (ws / ".duet" / "gate-cache.json").exists()

    timed_out = GateResult(command="pytest", ok=False, exit_code=124, output="",
                           timed_out=True, timeout=5)
    gate_cache.remember(str(ws), "abc", timed_out)
    failed = GateResult(command="pytest", ok=False, exit_code=1, output="1 failed")
    gate_cache.remember(str(ws), "abc", failed)
    assert not (ws / ".duet" / "gate-cache.json").exists()


# -- what is reused ---------------------------------------------------------

def test_a_second_session_on_the_same_files_skips_the_opening_gate(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    session(ws, gate, [DONE], [DONE])
    assert runs(log) == 1

    orch, result, adapters = session(ws, gate, [DONE], [DONE])
    opening = before_first_turn(result)
    assert "gate_start" not in opening, "the opening gate ran over files that had just passed"
    assert "gate_remembered" in opening
    # The agents are told plainly where the result came from.
    first_prompt = adapters["claude"].prompts[0]
    assert "remembered from" in first_prompt
    assert "--fresh-gate to rerun" in first_prompt
    # Consensus still rests on a run made during this session.
    assert result.status == STATUS_CONSENSUS
    assert runs(log) == 2
    kinds = [e["kind"] for e in events(result)]
    assert kinds.index("gate_start") > kinds.index("turn_start")


def test_the_remembered_result_is_never_what_a_signoff_is_counted_against(tmp_path):
    """The gate went red after it was remembered (a test that depends on the
    outside world, say). The opening check may say green; the sign-off may not."""
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    session(ws, gate, [DONE], [DONE])
    # Stand-in for a gate that has gone red since: the cache holds a pass for a
    # command that, run now, fails.
    failing = counting_gate(log, code=1)
    entries = cache_entries(ws)
    entries[0]["command"] = failing
    (ws / ".duet" / "gate-cache.json").write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")

    orch, result, _ = session(ws, failing, [DONE, DONE], [DONE, DONE], max_rounds=4)
    assert "gate_remembered" in before_first_turn(result)
    assert result.status != STATUS_CONSENSUS
    assert not result.gate.ok


def test_a_different_digest_or_command_is_not_reused(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    session(ws, gate, [DONE], [DONE])

    other_command = gate + " "
    _, result, _ = session(ws, other_command, [DONE], [DONE])
    assert "gate_start" in before_first_turn(result)

    (ws / "app.py").write_text("print('changed')\n", encoding="utf-8")
    _, result, _ = session(ws, gate, [DONE], [DONE])
    assert "gate_start" in before_first_turn(result)


def test_a_pass_older_than_a_day_is_not_reused(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    session(ws, gate, [DONE], [DONE])
    entries = cache_entries(ws)
    entries[0]["at"] -= 25 * 3600
    (ws / ".duet" / "gate-cache.json").write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")

    _, result, _ = session(ws, gate, [DONE], [DONE])
    assert "gate_start" in before_first_turn(result)


def test_a_stored_failure_is_never_reused(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    session(ws, gate, [DONE], [DONE])
    entries = cache_entries(ws)
    entries[0]["ok"] = False
    (ws / ".duet" / "gate-cache.json").write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")

    _, result, _ = session(ws, gate, [DONE], [DONE])
    assert "gate_start" in before_first_turn(result)


def test_fresh_gate_ignores_the_cache(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    session(ws, gate, [DONE], [DONE])
    _, result, _ = session(ws, gate, [DONE], [DONE], fresh_gate=True)
    opening = before_first_turn(result)
    assert "gate_start" in opening and "gate_remembered" not in opening


def test_a_corrupt_cache_is_ignored_not_fatal(tmp_path):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    for junk in ("{not json", "[]", '{"entries": "nope"}', '{"entries": [7, {"digest": 3}]}'):
        (ws / ".duet").mkdir(exist_ok=True)
        (ws / ".duet" / "gate-cache.json").write_text(junk, encoding="utf-8")
        _, result, _ = session(ws, gate, [DONE], [DONE])
        assert result.status == STATUS_CONSENSUS
        assert "gate_start" in before_first_turn(result)
    # ...and the next pass replaces the junk with something readable.
    assert cache_entries(ws)[0]["command"] == gate


def test_an_unreadable_cache_path_is_ignored(tmp_path):
    ws = workspace(tmp_path)
    (ws / ".duet" / "gate-cache.json").mkdir(parents=True)   # a directory, not a file
    assert gate_cache.recall(str(ws), "abc", "pytest") is None
    gate_cache.remember(str(ws), "abc", GateResult(command="pytest", ok=True, exit_code=0, output=""))


def test_the_opening_line_says_where_the_result_came_from(tmp_path):
    ws = workspace(tmp_path)
    passed = GateResult(command="pytest -q", ok=True, exit_code=0, output="3 passed")
    gate_cache.remember(str(ws), "d1", passed)
    remembered = gate_cache.recall(str(ws), "d1", "pytest -q")
    assert remembered is not None and remembered.ok
    text = remembered.render()
    assert "remembered from" in text and "same files, same command" in text
    assert "--fresh-gate to rerun" in text
    assert "3 passed" in text
    assert gate_cache.recall(str(ws), "d2", "pytest -q") is None
    assert gate_cache.recall(str(ws), "d1", "pytest") is None


def test_the_cache_file_does_not_move_the_workspace_digest(tmp_path):
    ws = workspace(tmp_path)
    before = Workspace(str(ws)).digest()
    gate_cache.remember(str(ws), before, GateResult(command="x", ok=True, exit_code=0, output=""))
    assert Workspace(str(ws)).digest() == before


def test_verify_never_takes_the_remembered_pass(tmp_path, capsys):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    _, result, _ = session(ws, gate, [DONE], [DONE])
    assert result.status == STATUS_CONSENSUS
    before = runs(log)
    assert main(["verify", "-C", str(ws)]) == 0
    assert runs(log) == before + 1
    assert "remembered" not in capsys.readouterr().out


# -- the flag ---------------------------------------------------------------

def test_every_session_command_and_resume_take_fresh_gate():
    parser = build_parser()
    for argv in (["run", "x"], ["build", "x"], ["fix", "x"], ["add", "x"],
                 ["refactor", "x"], ["plan", "x"], ["resume"]):
        args = parser.parse_args(argv + ["--fresh-gate"])
        assert args.fresh_gate is True, argv
        assert parser.parse_args(argv).fresh_gate is False, argv


def test_resume_reuses_a_pass_and_fresh_gate_makes_it_rerun(tmp_path, capsys):
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    cfg = Config(
        task="build the thing", root=str(ws), gate=gate,
        agents=[AgentSpec("claude", "mock", options={"script": [WAIT, DONE]}),
                AgentSpec("gpt", "mock", options={"script": [WAIT, DONE]})],
        start="claude", max_rounds=2,
    )
    first = Orchestrator(cfg).run()
    assert first.status != STATUS_CONSENSUS
    assert runs(log) == 1

    main(["resume", "-C", str(ws), "--rounds", "3"])
    out = capsys.readouterr().out
    assert "remembered from" in out
    resumed = sorted(p for p in (ws / ".duet" / "sessions").iterdir())[-1]
    kinds = [json.loads(l)["kind"] for l in (resumed / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "gate_start" not in kinds[: kinds.index("turn_start")]

    main(["resume", "-C", str(ws), "--rounds", "4", "--fresh-gate"])
    resumed = sorted(p for p in (ws / ".duet" / "sessions").iterdir())[-1]
    kinds = [json.loads(l)["kind"] for l in (resumed / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "gate_start" in kinds[: kinds.index("turn_start")]


def test_fresh_gate_is_not_carried_over_into_a_resume(tmp_path, capsys):
    """A session started with --fresh-gate saves the flag in its config; a later
    `duet resume` without it is a new choice, and may use the cache."""
    ws, log = workspace(tmp_path), tmp_path / "runs.log"
    gate = counting_gate(log)
    cfg = Config(
        task="build the thing", root=str(ws), gate=gate, fresh_gate=True,
        agents=[AgentSpec("claude", "mock", options={"script": [WAIT, WAIT]}),
                AgentSpec("gpt", "mock", options={"script": [WAIT, WAIT]})],
        start="claude", max_rounds=2,
    )
    assert Orchestrator(cfg).run().status != STATUS_CONSENSUS

    main(["resume", "-C", str(ws), "--rounds", "3"])
    assert "remembered from" in capsys.readouterr().out
