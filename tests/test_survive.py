"""Sessions do not die silently.

A session is usually launched from a shell that does not outlive it — an
agent's tool shell, a terminal someone closes. The signal that shell's death
sends used to kill duet mid-turn with no session_end and nothing saying the
work could be resumed. These tests send the signals for real, to a child
process running a real session, so a regression kills the child rather than
the test run.
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from duet.cli import main

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")


def _pid_alive(pid):
    """Local on purpose: importing the fix's own helper would make this whole
    file fail to import on the original code, which proves nothing about it."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

ROOT = Path(__file__).resolve().parent.parent

# A session whose first agent's turn is where the signal lands: the adapter
# signals its own process and then waits, the way a slow CLI would.
CHILD = textwrap.dedent("""
    import os, signal, sys, time
    from duet.adapters.mock import MockAdapter, envelope
    from duet.config import AgentSpec, Config
    from duet.orchestrator import Orchestrator

    root, where, second = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
    signum = getattr(signal, sys.argv[4])

    class Slow(MockAdapter):
        def send(self, prompt, system="", round_no=0):
            if where == "turn" and round_no == 3:
                os.kill(os.getpid(), signum)
                time.sleep(30)
            return super().send(prompt, system=system, round_no=round_no)

    def reporter(event):
        if second and event["kind"] == "session_stopping":
            print("stopping:", event["signal"], flush=True)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(30)

    gate = sys.executable + ' -c "import time; time.sleep(0)"'
    if where == "gate":
        # The gate itself signals duet, then runs long: the signal lands
        # while duet is waiting on it.
        gate = sys.executable + ' -c "import os, signal, time; os.kill(%d, signal.%s); time.sleep(60)"' % (
            os.getpid(), sys.argv[4])
    cfg = Config(task="build the thing", root=root, gate=gate,
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=6)
    work = envelope("more", "CONTINUE", patches=[{"path": "app.py", "content": "x = 1\\n"}])
    adapters = {"claude": Slow("claude", script=[work] * 6),
                "gpt": Slow("gpt", script=[work] * 6)}
    orch = Orchestrator(cfg, reporter=reporter, session_id="20261011-120000-sig1", adapters=adapters)
    print(orch.run().status)
""")


def run_child(tmp_path, where, second=False, sig="SIGTERM"):
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path), where, "1" if second else "0", sig],
        capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    return proc, time.monotonic() - started


def events_of(tmp_path):
    path = tmp_path / ".duet" / "sessions" / "20261011-120000-sig1" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP", "SIGINT"])
def test_a_signal_mid_turn_ends_the_session_on_the_record(tmp_path, sig):
    proc, took = run_child(tmp_path, "turn", sig=sig)
    assert took < 25, "the agent's turn was waited out instead of stopped"
    ends = [e for e in events_of(tmp_path) if e["kind"] == "session_end"]
    assert ends, "no session_end: died silently (exit %s)\n%s" % (proc.returncode, proc.stderr)
    end = ends[-1]
    assert end["status"] == "interrupted"
    assert "stopped by %s during claude's turn in round 3" % sig in end["reason"]
    assert "duet resume 20261011-120000-sig1" in end["reason"]
    assert end["resume"].startswith("duet resume 20261011-120000-sig1")

    session = tmp_path / ".duet" / "sessions" / "20261011-120000-sig1"
    report = (session / "report.md").read_text(encoding="utf-8")
    assert "duet resume 20261011-120000-sig1" in report
    state = json.loads((session / "state.json").read_text(encoding="utf-8"))
    assert state["result"]["status"] == "interrupted"
    # Rounds 1 and 2 happened; the one that was cut off is the one resume re-runs.
    assert state["rounds"] == 2


def test_the_interrupted_session_resumes_at_the_round_it_lost(tmp_path, capsys):
    proc, _ = run_child(tmp_path, "turn")
    assert proc.returncode == 0, proc.stderr
    code = main(["resume", "20261011-120000-sig1", "-C", str(tmp_path), "--json", "--rounds", "3"])
    data = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert data["resumed"] is True, data
    assert code in (0, 1)


def test_a_signal_while_the_gate_runs_stops_the_gate_too(tmp_path):
    proc, took = run_child(tmp_path, "gate")
    assert took < 25, "the 60-second gate was waited out"
    end = [e for e in events_of(tmp_path) if e["kind"] == "session_end"][-1]
    assert end["status"] == "interrupted"
    assert "stopped by SIGTERM while the gate was running" in end["reason"]


def test_a_second_signal_during_shutdown_exits_at_once(tmp_path):
    proc, took = run_child(tmp_path, "turn", second=True)
    # The shutdown it must not wait for takes 30 s; 25 s still tells the two
    # apart, and leaves room for a fresh interpreter to start on a loaded
    # machine (21 s was measured at load 600).
    assert took < 25, "the second signal waited for the shutdown"
    assert proc.returncode == 128 + signal.SIGTERM, (proc.returncode, proc.stderr)
    assert "stopping: SIGTERM" in proc.stdout       # the first one began a shutdown
    assert "session_end" not in [e["kind"] for e in events_of(tmp_path)]


# -- duet status: running, finished, dead -----------------------------------
def write_session(tmp_path, name, events):
    session = tmp_path / ".duet" / "sessions" / name
    session.mkdir(parents=True)
    (session / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def gone_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def status_json(tmp_path, capsys, session):
    main(["status", session, "-C", str(tmp_path), "--json"])
    return json.loads(capsys.readouterr().out)


def test_status_tells_running_finished_and_dead_apart(tmp_path, capsys):
    now = time.time()
    start = {"kind": "session_start", "task": "t", "max_rounds": 6,
             "agents": {"a": "A", "b": "B"}, "turn_timeout": {"a": 1800, "b": 1800},
             "gate_timeout": 900}
    write_session(tmp_path, "20261011-100000-live", [
        dict(start, t=now - 30, pid=os.getpid()),
        {"kind": "turn_start", "t": now - 20, "round": 1, "agent": "a"}])
    write_session(tmp_path, "20261011-100001-done", [
        dict(start, t=now - 30, pid=gone_pid()),
        {"kind": "session_end", "t": now - 5, "status": "consensus", "reason": "agreed"}])
    write_session(tmp_path, "20261011-100002-gone", [
        dict(start, t=now - 30, pid=gone_pid()),
        {"kind": "turn_start", "t": now - 20, "round": 3, "agent": "b"}])
    # It saved after rounds 1 and 2, so there is something to resume.
    (tmp_path / ".duet" / "sessions" / "20261011-100002-gone" / "state.json").write_text(
        "{}", encoding="utf-8")
    # From before pids were recorded: judged by a silence no limit allows.
    write_session(tmp_path, "20261011-100003-old", [
        dict(start, t=now - 9000),
        {"kind": "gate_start", "t": now - 8000, "round": 2}])

    live = status_json(tmp_path, capsys, "20261011-100000-live")
    assert live["state"] == "running" and live["running"] is True

    done = status_json(tmp_path, capsys, "20261011-100001-done")
    assert done["state"] == "finished" and done["running"] is False

    gone = status_json(tmp_path, capsys, "20261011-100002-gone")
    assert gone["state"] == "dead" and gone["running"] is False
    assert "stopped without finishing (process gone)" in gone["reason"]
    assert "during b's turn in round 3" in gone["reason"]
    assert gone["resume"].startswith("duet resume 20261011-100002-gone")

    old = status_json(tmp_path, capsys, "20261011-100003-old")
    assert old["state"] == "dead"
    assert "while the gate was running" in old["reason"]


def test_status_with_no_session_lists_recent_ones_and_names_the_dead(tmp_path, capsys):
    now = time.time()
    start = {"kind": "session_start", "task": "t", "max_rounds": 6, "agents": {"a": "A"}}
    write_session(tmp_path, "20261011-090000-gone", [
        dict(start, t=now - 30, pid=gone_pid()),
        {"kind": "turn_start", "t": now - 20, "round": 2, "agent": "a"}])
    (tmp_path / ".duet" / "sessions" / "20261011-090000-gone" / "state.json").write_text(
        "{}", encoding="utf-8")
    write_session(tmp_path, "20261011-090001-done", [
        dict(start, t=now - 30),
        {"kind": "session_end", "t": now - 5, "status": "consensus", "reason": "agreed"}])

    main(["status", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert "20261011-090000-gone" in out and "20261011-090001-done" in out
    assert "stopped without finishing (process gone)" in out
    assert "duet resume 20261011-090000-gone" in out


def test_a_dead_session_that_saved_no_state_is_offered_a_restart_not_a_resume(tmp_path, capsys):
    """`duet resume` refuses a session with no state.json; status must not offer it."""
    now = time.time()
    start = {"kind": "session_start", "task": "fix the parser", "max_rounds": 6,
             "agents": {"a": "A"}, "gate": "pytest -q",
             "started_with": ["fix", "the parser", "--gate", "pytest -q", "--rounds", "6"]}
    write_session(tmp_path, "20261011-110000-gone", [
        dict(start, t=now - 30, pid=gone_pid()),
        {"kind": "turn_start", "t": now - 20, "round": 1, "agent": "a"}])

    gone = status_json(tmp_path, capsys, "20261011-110000-gone")
    assert gone["state"] == "dead"
    assert gone["resume"] is None
    assert gone["restart"].startswith("duet fix 'the parser' --gate 'pytest -q' --rounds 6")

    main(["status", "20261011-110000-gone", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert "duet resume" not in out
    assert "nothing to resume" in out
    assert "start it again with: duet fix 'the parser'" in out

    # And `duet resume` names the same command when it refuses.
    assert main(["resume", "20261011-110000-gone", "-C", str(tmp_path)]) != 0
    assert "duet fix 'the parser' --gate 'pytest -q' --rounds 6" in capsys.readouterr().out


def test_the_recent_list_offers_a_restart_for_a_dead_session_with_no_state(tmp_path, capsys):
    now = time.time()
    # From before the command line was recorded: rebuilt from the task, gate and rounds.
    start = {"kind": "session_start", "task": "say hi", "max_rounds": 4,
             "agents": {"a": "A"}, "gate": "make test"}
    write_session(tmp_path, "20261011-120000-gone", [
        dict(start, t=now - 30, pid=gone_pid()),
        {"kind": "turn_start", "t": now - 20, "round": 1, "agent": "a"}])
    write_session(tmp_path, "20261011-120001-done", [
        dict(start, t=now - 30),
        {"kind": "session_end", "t": now - 5, "status": "consensus", "reason": "agreed"}])

    main(["status", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert "duet resume 20261011-120000-gone" not in out
    assert "duet run 'say hi' --gate 'make test' --rounds 4" in out


def test_a_session_records_the_command_line_it_was_started_with(tmp_path, capsys):
    main(["run", "say hi", "-C", str(tmp_path), "--pair", "mock+mock", "--rounds", "1",
          "--no-gate", "--allow-nested"])
    capsys.readouterr()
    [session] = [p for p in (tmp_path / ".duet" / "sessions").iterdir() if p.is_dir()]
    start = json.loads((session / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert start["kind"] == "session_start"
    assert start["started_with"] == ["run", "say hi", "-C", str(tmp_path), "--pair", "mock+mock",
                                     "--rounds", "1", "--no-gate", "--allow-nested"]


# -- duet --detach -----------------------------------------------------------
def test_detach_returns_at_once_and_runs_in_its_own_session(tmp_path, capsys):
    our_sid = os.getsid(os.getpid())
    # A gate that takes a few seconds keeps the detached child alive long
    # enough to inspect — real work, not a sleep standing in for it.
    gate = "%s -c %s" % (shlex.quote(sys.executable), shlex.quote("import time; time.sleep(4)"))

    started = time.monotonic()
    code = main(["run", "say hi", "-C", str(tmp_path), "--pair", "mock+mock",
                 "--gate", gate, "--rounds", "1", "--detach", "--allow-nested"])
    took = time.monotonic() - started
    out = capsys.readouterr().out

    assert code == 0
    assert took < 2, "duet run --detach waited on the session instead of returning at once"
    match = re.search(r"session:\s+(\S+)", out)
    assert match, out
    session_id = match.group(1)
    assert "tail -f" in out and "duet status %s" % session_id in out

    session_dir = tmp_path / ".duet" / "sessions" / session_id
    events_path = session_dir / "events.jsonl"
    # How long the child may take to start is not what this test is about
    # (`took < 2` above is): on a loaded machine a fresh interpreter can take
    # well over 5 s to import duet and write its first event.
    deadline = time.monotonic() + 30
    pid = None
    while time.monotonic() < deadline and pid is None:
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = json.loads(line)
                    if event.get("kind") == "session_start":
                        pid = event.get("pid")
                        break
        if pid is None:
            time.sleep(0.1)
    assert pid, "the detached session never started (log: %s)" % (
        (tmp_path / ".duet" / "sessions" / (session_id + ".log")).read_text(encoding="utf-8")
        if (tmp_path / ".duet" / "sessions" / (session_id + ".log")).exists() else "<missing>")
    assert _pid_alive(pid), "the detached child had already died"
    assert os.getsid(pid) != our_sid, "the detached child is still in this process's session"

    # Let it actually finish (the gate takes ~4s) instead of leaving a
    # subprocess of this test process behind for the rest of the suite.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _pid_alive(pid):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.2)


def test_detach_on_resume_continues_the_right_session(tmp_path, capsys):
    proc, _ = run_child(tmp_path, "turn")
    assert proc.returncode == 0, proc.stderr

    started = time.monotonic()
    code = main(["resume", "20261011-120000-sig1", "-C", str(tmp_path),
                 "--detach", "--allow-nested"])
    took = time.monotonic() - started
    out = capsys.readouterr().out

    assert code == 0
    assert took < 2
    match = re.search(r"session:\s+(\S+)", out)
    assert match, out
    continued_id = match.group(1)
    assert continued_id != "20261011-120000-sig1"
    assert continued_id > "20261011-120000-sig1"

    deadline = time.monotonic() + 30
    pid = None
    events_path = tmp_path / ".duet" / "sessions" / continued_id / "events.jsonl"
    while time.monotonic() < deadline and pid is None:
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = json.loads(line)
                    if event.get("kind") == "session_start":
                        pid = event.get("pid")
                        break
        if pid is None:
            time.sleep(0.1)
    assert pid, "the resumed, detached session never started"
    while time.monotonic() < deadline and _pid_alive(pid):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.2)


def test_status_shows_why_a_detached_session_never_started(tmp_path, capsys):
    sessions = tmp_path / ".duet" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "20261011-140000-nope.log").write_text("✗ the acceptance gate cannot run: no such command\n", encoding="utf-8")
    assert main(["status", "20261011-140000-nope", "-C", str(tmp_path)]) == 2
    assert "the acceptance gate cannot run" in capsys.readouterr().out


def test_the_detached_id_is_used_once_and_not_inherited(tmp_path, monkeypatch):
    """The id --detach hands its child is for that child's session only. Left
    in the environment, the gate and both agents inherit it — and a test suite
    run as the gate builds every one of its sessions under the same id."""
    from duet.config import AgentSpec, Config
    from duet.orchestrator import Orchestrator

    monkeypatch.setenv("DUET_FORCE_SESSION_ID", "20261011-130000-once")
    cfg = Config(task="t", root=str(tmp_path), gate="",
                 agents=[AgentSpec("a", "mock"), AgentSpec("b", "mock")], max_rounds=1)
    first = Orchestrator(cfg)
    assert first.session_id == "20261011-130000-once"
    assert "DUET_FORCE_SESSION_ID" not in os.environ
    assert Orchestrator(cfg).session_id != first.session_id


# -- duet wait ----------------------------------------------------------------
# `duet wait` blocks on the same classification `duet status` reads from the
# event log — never the process, never the raw log text — then prints that
# same final summary. These sessions are written finished or dead outright
# (no real waiting needed) except where a test is specifically about the
# polling loop itself.

def test_wait_exits_0_on_consensus(tmp_path, capsys):
    now = time.time()
    start = {"kind": "session_start", "task": "say hi", "max_rounds": 2, "agents": {"a": "A"}}
    write_session(tmp_path, "20261011-170000-done", [
        dict(start, t=now - 30),
        {"kind": "session_end", "t": now - 5, "status": "consensus", "reason": "agreed"}])

    code = main(["wait", "20261011-170000-done", "-C", str(tmp_path)])
    assert code == 0
    assert "finished" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["exhausted", "blocked", "error", "interrupted"])
def test_wait_exits_1_on_any_other_end(tmp_path, capsys, status):
    now = time.time()
    start = {"kind": "session_start", "task": "say hi", "max_rounds": 2, "agents": {"a": "A"}}
    name = "20261011-170001-" + status
    write_session(tmp_path, name, [
        dict(start, t=now - 30),
        {"kind": "session_end", "t": now - 5, "status": status, "reason": "see report"}])

    code = main(["wait", name, "-C", str(tmp_path)])
    assert code == 1
    assert status in capsys.readouterr().out


def test_wait_detects_a_dead_session(tmp_path, capsys):
    now = time.time()
    start = {"kind": "session_start", "task": "t", "max_rounds": 6, "agents": {"a": "A"}}
    name = "20261011-170002-gone"
    write_session(tmp_path, name, [
        dict(start, t=now - 30, pid=gone_pid()),
        {"kind": "turn_start", "t": now - 20, "round": 3, "agent": "a"}])

    code = main(["wait", name, "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "stopped without finishing (process gone)" in out


def test_wait_times_out_while_the_session_is_still_running(tmp_path, capsys):
    now = time.time()
    start = {"kind": "session_start", "task": "t", "max_rounds": 6, "agents": {"a": "A"},
             "turn_timeout": {"a": 1800}, "gate_timeout": 900, "pid": os.getpid()}
    name = "20261011-170003-live"
    write_session(tmp_path, name, [
        dict(start, t=now),
        {"kind": "turn_start", "t": now, "round": 1, "agent": "a"}])

    started = time.monotonic()
    code = main(["wait", name, "-C", str(tmp_path), "--timeout", "0.01"])
    took = time.monotonic() - started
    out = capsys.readouterr().out
    assert code == 2
    assert "timed out" in out
    assert took < 10, "the timeout itself should be fast, not a long sleep"


def test_wait_is_not_fooled_by_an_agent_message_containing_error(tmp_path, capsys):
    """A plain `tail -f | grep ERROR` would quit on this line; `duet wait` reads
    the event log's own kinds, never the raw log text, so it keeps waiting."""
    now = time.time()
    start = {"kind": "session_start", "task": "t", "max_rounds": 6, "agents": {"a": "A"},
             "turn_timeout": {"a": 1800}, "gate_timeout": 900, "pid": os.getpid()}
    name = "20261011-170004-live"
    write_session(tmp_path, name, [
        dict(start, t=now),
        {"kind": "turn_start", "t": now, "round": 1, "agent": "a"}])
    (tmp_path / ".duet" / "sessions" / (name + ".log")).write_text(
        "a: I checked the stack trace — it says ERROR: connection refused, but that's "
        "expected here and not the session ending.\n", encoding="utf-8")

    started = time.monotonic()
    code = main(["wait", name, "-C", str(tmp_path), "--timeout", "0.01"])
    took = time.monotonic() - started
    out = capsys.readouterr().out
    assert code == 2 and "timed out" in out, "the ERROR line in the log ended the wait early"
    assert took >= 0.3, "returned before the timeout — the ERROR line was mistaken for the end"


def test_wait_prints_the_same_summary_duet_status_would(tmp_path, capsys):
    now = time.time()
    start = {"kind": "session_start", "task": "say hi", "max_rounds": 2, "agents": {"a": "A"}}
    name = "20261011-170005-done"
    write_session(tmp_path, name, [
        dict(start, t=now - 30),
        {"kind": "session_end", "t": now - 5, "status": "consensus", "reason": "agreed"}])

    assert main(["wait", name, "-C", str(tmp_path)]) == 0
    wait_out = capsys.readouterr().out
    assert main(["status", name, "-C", str(tmp_path)]) == 0
    status_out = capsys.readouterr().out
    assert wait_out == status_out


def test_wait_defaults_to_the_newest_session(tmp_path, capsys):
    now = time.time()
    start = {"kind": "session_start", "task": "say hi", "max_rounds": 2, "agents": {"a": "A"}}
    write_session(tmp_path, "20261011-170006-first", [
        dict(start, t=now - 90),
        {"kind": "session_end", "t": now - 60, "status": "consensus", "reason": "agreed"}])
    write_session(tmp_path, "20261011-170007-second", [
        dict(start, t=now - 30),
        {"kind": "session_end", "t": now - 5, "status": "blocked", "reason": "see report"}])

    code = main(["wait", "-C", str(tmp_path)])
    assert code == 1
    assert "20261011-170007-second" in capsys.readouterr().out


def _finish_on_first_poll(monkeypatch, tmp_path, name, before=None):
    """A WAIT_POLL that, the first time `duet wait` sleeps, lets the detached
    child get as far as finishing — the way a real one would while it waited."""
    import duet.cli as cli

    def poll(seconds):
        if before:
            before()
        session = tmp_path / ".duet" / "sessions" / name
        session.mkdir(parents=True, exist_ok=True)
        now = time.time()
        (session / "events.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"kind": "session_start", "t": now - 1, "task": "say hi", "max_rounds": 2,
             "agents": {"a": "A"}},
            {"kind": "session_end", "t": now, "status": "consensus", "reason": "agreed"},
        ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "WAIT_POLL", poll)


def test_wait_right_after_detach_waits_for_the_session_to_start(tmp_path, capsys, monkeypatch):
    """--detach prints the id before its child has written anything but the
    log; the next command the skill runs is `duet wait <id>`. That is a session
    starting, not "no such session"."""
    name = "20261011-170008-starting"
    sessions = tmp_path / ".duet" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / (name + ".log")).write_text("", encoding="utf-8")
    _finish_on_first_poll(monkeypatch, tmp_path, name)

    assert main(["wait", name, "-C", str(tmp_path)]) == 0
    assert "finished" in capsys.readouterr().out


def test_wait_on_a_session_with_no_events_yet_is_not_called_dead(tmp_path, capsys, monkeypatch):
    """An empty event log has no pid and its silence counts from 1970, which
    `classify_session` calls dead; a session directory made a moment ago is
    a session that has not written its first event yet."""
    name = "20261011-170009-starting"
    session = tmp_path / ".duet" / "sessions" / name
    session.mkdir(parents=True)
    (session / "events.jsonl").write_text("", encoding="utf-8")
    _finish_on_first_poll(monkeypatch, tmp_path, name)

    assert main(["wait", name, "-C", str(tmp_path)]) == 0


def test_wait_on_a_detach_that_stopped_before_starting_says_why(tmp_path, capsys):
    name = "20261011-170010-never"
    sessions = tmp_path / ".duet" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / (name + ".log")
    log.write_text("✗ not signed in to codex\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(log, (old, old))

    assert main(["wait", name, "-C", str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert "not signed in to codex" in out


def test_wait_exits_2_for_no_such_session(tmp_path, capsys):
    assert main(["wait", "no-such-id", "-C", str(tmp_path)]) == 2
    assert "no-such-id" in capsys.readouterr().out


def test_wait_exits_2_when_there_are_no_sessions_at_all(tmp_path, capsys):
    assert main(["wait", "-C", str(tmp_path)]) == 2
    assert "no sessions" in capsys.readouterr().out
