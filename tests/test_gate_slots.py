"""Gate slots: a machine-wide limit on how many duet gates run at once.

Measured on 122 gate runs on one machine: with up to 3 other duet gates
alongside, 7 of 86 timed out; with 4 or more others, 21 of 36. So every gate
run takes a slot from a pool shared by every duet process of the user, and
waits for one when the pool is full — see duet/gate_slots.py.

Every test here points DUET_HOME at a directory of its own, so the pool it
uses is never the real ~/.duet one, and clears DUET_GATE, which a gate run
from inside a gate (this suite run as a duet gate) uses to skip the pool. The
gates are small Python scripts run with this interpreter, so they work on
Windows too.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from duet import agent_gate
from duet.cli import live_position, main, make_reporter
from duet.workspace import Workspace

SESSION = "20260101-000000-slot"

# Writes when it started and ended into the file named by its argument, and
# sleeps between: two runs overlap exactly when their intervals do.
TIMED = """
import sys, time
from pathlib import Path
out = Path(sys.argv[1])
start = time.time()
with out.open("a") as fh:
    fh.write("start %r\\n" % start)
time.sleep(float(sys.argv[2]))
with out.open("a") as fh:
    fh.write("end %r %r\\n" % (start, time.time()))
"""

# Waits (up to 10s) for `n` runs to have started before it ends: only runs
# that are allowed to overlap all get there quickly.
TOGETHER = """
import sys, time
from pathlib import Path
out = Path(sys.argv[1])
with out.open("a") as fh:
    fh.write("start\\n")
deadline = time.time() + 10
while time.time() < deadline:
    if out.read_text(encoding="utf-8").count("start") >= int(sys.argv[2]):
        break
    time.sleep(0.05)
"""

# Takes a slot and holds it until it is killed.
HOLDER = """
import time
from duet.gate_slots import gate_slot
with gate_slot():
    print("held", flush=True)
    time.sleep(120)
"""


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A pool of its own, of one slot, outside any gate."""
    monkeypatch.setenv("DUET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DUET_GATE_SLOTS", "1")
    monkeypatch.delenv("DUET_GATE", raising=False)
    monkeypatch.setattr("duet.gate_slots.POLL", 0.05, raising=False)
    return tmp_path


def script(tmp_path, body, name):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def command(path, *args):
    return " ".join(['"%s"' % sys.executable, '"%s"' % path] + ['"%s"' % a for a in args])


def workspace(tmp_path, gate, timeout=600):
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    return Workspace(str(root), gate=gate, gate_timeout=timeout)


def run_together(*workspaces):
    """Run each workspace's gate on a thread of its own, all at once."""
    results = [None] * len(workspaces)

    def go(i, ws):
        results[i] = ws.run_gate()

    threads = [threading.Thread(target=go, args=(i, ws)) for i, ws in enumerate(workspaces)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def intervals(log):
    return sorted((float(a), float(b)) for _, a, b in
                  (line.split() for line in log.read_text(encoding="utf-8").splitlines()
                   if line.startswith("end")))


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# -- one slot: gates take turns ------------------------------------------------
def test_with_one_slot_two_concurrent_gates_never_overlap_and_the_second_waits(pool):
    log = pool / "runs.log"
    gate = command(script(pool, TIMED, "timed.py"), log, "1.0")
    results = run_together(workspace(pool, gate), workspace(pool, gate))
    assert all(r is not None and r.ok for r in results), results
    (a_start, a_end), (b_start, b_end) = intervals(log)
    assert b_start >= a_end, "the two gates ran at the same time"
    # One of them waited for the other's slot, about as long as it ran.
    assert max(getattr(r, "waited", 0.0) for r in results) >= 0.7


def test_waiting_for_a_slot_is_not_counted_against_the_gate_timeout(pool):
    log = pool / "runs.log"
    slow = workspace(pool, command(script(pool, TIMED, "timed.py"), log, "3"))
    fast = workspace(pool, command(script(pool, "print('ok')\n", "fast.py")), timeout=2)
    holder = threading.Thread(target=slow.run_gate)
    holder.start()
    try:
        assert wait_for(lambda: log.exists())
        result = fast.run_gate()
    finally:
        holder.join(60)
    # It waited longer than its own limit for the slot, and still passed:
    # the limit starts when the gate does.
    assert getattr(result, "waited", 0.0) > 2
    assert result.ok and not result.timed_out, result.output


# -- what holds a slot ---------------------------------------------------------
def test_a_process_killed_while_holding_a_slot_frees_it(pool):
    from duet.gate_slots import gate_slot

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    proc = subprocess.Popen([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env)
    try:
        assert proc.stdout.readline().strip() == "held"
        # While it lives, the one slot is taken: a gate waiting a short while gives up.
        with gate_slot(notify=lambda *a, **k: None, max_wait=0.5) as slot:
            assert slot.gave_up
    finally:
        proc.kill()
        proc.wait(30)
        proc.stdout.close()
    # Killed, not released: the OS let go of its lock, so the slot is free at once.
    with gate_slot(notify=lambda *a, **k: None, max_wait=5) as slot:
        assert not slot.gave_up and slot.waited == 0.0


def test_a_gate_that_never_gets_a_slot_runs_anyway_and_says_so(pool, monkeypatch):
    from duet.gate_slots import gate_slot

    monkeypatch.setattr("duet.gate_slots.MAX_WAIT", 0.3)
    seen = []
    with gate_slot(notify=lambda *a, **k: None):
        ws = workspace(pool, command(script(pool, "print('ok')\n", "fast.py")))
        ws.slot_notify = lambda kind, **payload: seen.append((kind, payload))
        result = ws.run_gate()
    assert result.ok
    assert [k for k, _ in seen] == ["gate_queued", "gate_slot"]
    assert seen[1][1]["gave_up"] is True


def test_with_slots_set_to_zero_gates_run_unlimited(pool, monkeypatch):
    monkeypatch.setenv("DUET_GATE_SLOTS", "0")
    log = pool / "runs.log"
    gate = command(script(pool, TOGETHER, "together.py"), log, "3")
    started = time.monotonic()
    results = run_together(*(workspace(pool, gate) for _ in range(3)))
    assert all(r is not None and r.ok for r in results)
    # All three were running at once: none waited on another to finish.
    assert time.monotonic() - started < 8
    assert all(getattr(r, "waited", 0.0) == 0.0 for r in results)


# -- what the user sees --------------------------------------------------------
def test_a_waiting_gate_emits_gate_queued_and_prints_one_line(pool, capfd):
    log = pool / "runs.log"
    slow = workspace(pool, command(script(pool, TIMED, "timed.py"), log, "1.0"))
    fast = workspace(pool, command(script(pool, "print('ok')\n", "fast.py")))
    holder = threading.Thread(target=slow.run_gate)
    holder.start()
    try:
        assert wait_for(lambda: log.exists())
        # No session to report to (`duet gate`, a workflow replay): stderr.
        fast.run_gate()
    finally:
        holder.join(60)
    err = capfd.readouterr().err
    assert "waiting for a gate slot: 1 duet gate is running on this machine" in err

    # In a session: an event, which the terminal shows as that line and
    # `duet status` reads as waiting rather than running.
    seen = []
    fast.slot_notify = lambda kind, **payload: seen.append(dict(kind=kind, **payload))
    holder = threading.Thread(target=slow.run_gate)
    holder.start()
    try:
        assert wait_for(lambda: log.read_text(encoding="utf-8").count("start") >= 2)
        result = fast.run_gate()
    finally:
        holder.join(60)
    assert [e["kind"] for e in seen] == ["gate_queued", "gate_slot"]
    assert seen[0]["running"] == 1 and seen[0]["slots"] == 1
    assert seen[1]["waited"] > 0 and seen[1]["gave_up"] is False
    assert result.waited > 0

    make_reporter(["claude", "chatgpt"])(seen[0])
    out = capfd.readouterr().out
    assert "waiting for a gate slot: 1 duet gate is running on this machine" in out
    events = [{"kind": "turn_start", "round": 2, "agent": "claude"},
              {"kind": "gate_start", "command": "x"}, seen[0]]
    assert live_position(events)[2] == "gate_queued"
    assert live_position(events + [seen[1]])[2] == "gate"


# -- `duet gate` ---------------------------------------------------------------
def test_duet_gate_takes_a_slot(pool, monkeypatch, capfd):
    log = pool / "runs.log"
    slow = workspace(pool, command(script(pool, TIMED, "timed.py"), log, "1.5"))
    root = Path(slow.root)
    where = root / ".duet" / "sessions" / SESSION
    where.mkdir(parents=True)
    fast = command(script(pool, "print('ok')\n", "fast.py"))
    (where / "events.jsonl").write_text(
        '{"kind": "session_start", "session": "%s", "root": %s, "gate": %s, "gate_timeout": 600}\n'
        % (SESSION, _json(str(root)), _json(fast)), encoding="utf-8")
    monkeypatch.setenv("DUET_SESSION", SESSION)
    holder = threading.Thread(target=slow.run_gate)
    holder.start()
    try:
        assert wait_for(lambda: log.exists())
        code = main(["gate", "-C", str(root)])
        finished = time.time()
    finally:
        holder.join(60)
    assert code == 0
    (run,) = agent_gate.read_runs(where)
    assert run["ok"] is True
    assert "waiting for a gate slot" in capfd.readouterr().err
    (start, end), = intervals(log)
    # It finished only after the other gate had let go of the slot.
    assert finished >= end


def _json(value):
    return json.dumps(value)


def test_no_home_directory_falls_back_to_a_temp_folder(monkeypatch, tmp_path):
    """Windows with neither USERPROFILE nor HOME makes Path.home() raise; a gate
    must still run (`duet demo` in a bare environment failed on windows-latest)."""
    from pathlib import Path
    from duet import gate_slots
    monkeypatch.delenv("DUET_HOME", raising=False)
    monkeypatch.setattr(gate_slots.tempfile, "gettempdir", lambda: str(tmp_path))

    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    assert gate_slots.state_dir().parent == tmp_path
    with gate_slots.gate_slot(size=1):
        pass
