"""A gate that leaves a helper running must not hold the harness past its limit.

Live on duet 0.18.2 (an iOS UI-test gate, --gate-timeout 60 minutes): five gate
runs were reported "timed out" but the harness got control back after 60, 60,
60, 60 and 221 minutes. After the time limit, run_gate stopped the gate's
process group and then read its pipes with no time limit — and a process the
gate had started in a session of its own (`xcrun simctl spawn ... log stream`,
a daemon) survives the group kill and keeps those pipes open, so the read
waited for it.

Each test here builds that gate: it starts a helper with start_new_session,
prints a line, and then outlasts its limit (or finishes). On the original code
each call below waits for the helper (HELPER_SECONDS); now it returns within
the limit plus a few seconds of grace.

Nothing this fix introduced is imported at the top of this file: the harness
replays these tests against the code from before the session, where they must
fail on the time they take, not on an ImportError.
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from duet.workspace import Workspace

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="start_new_session (setsid) is POSIX; Windows has no process groups to escape")

# Long enough that waiting for it is unmistakable, short enough that the
# replay against the original code does not stall the suite for long.
HELPER_SECONDS = 90
# The limit plus duet's grace is about ten seconds; this leaves room for a
# heavily loaded machine (seen: load average 900, a second per interpreter
# start) and is still half of HELPER_SECONDS.
FAST_ENOUGH = 45

# The gate's limit. A few seconds, so the interpreter has started and the
# helper is running well before it.
LIMIT = 5

GATE = """
print("printed before the limit", flush=True)
import subprocess, sys, time
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%d)"],
                          start_new_session=True)
with open(%r, "w", encoding="utf-8") as f:
    f.write(str(helper.pid))
time.sleep(%s)
"""


class Leftover:
    """A gate that leaves a helper; the helper is killed when the test ends."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.pidfile = tmp_path / "helper.pid"

    def __call__(self, sleep):
        script = self.tmp_path / "gate.py"
        script.write_text(GATE % (HELPER_SECONDS, str(self.pidfile), sleep), encoding="utf-8")
        return '"%s" "%s"' % (sys.executable, script)

    def started(self):
        """Without this a gate stopped before it got that far would pass on
        the original code too, and test nothing."""
        return (self.pidfile.is_file()
                and self.pidfile.read_text(encoding="utf-8").strip().isdigit())


@pytest.fixture
def leftover(tmp_path):
    # Warm the interpreter so a cold start does not eat the gate's limit.
    subprocess.run([sys.executable, "-c", "import subprocess"], check=True)
    made = Leftover(tmp_path)
    yield made
    try:
        os.kill(int(made.pidfile.read_text(encoding="utf-8")), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def ws_root(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    return root


def test_run_gate_returns_at_its_limit_despite_a_detached_helper(tmp_path, leftover):
    """The reproduction from the bug report."""
    ws = Workspace(str(ws_root(tmp_path)), gate=leftover(600), gate_timeout=LIMIT)
    started = time.monotonic()
    result = ws.run_gate()
    took = time.monotonic() - started
    assert leftover.started()
    assert took < FAST_ENOUGH, "run_gate waited %.0fs for the gate's helper" % took
    assert result.timed_out and not result.ok
    # What it printed before the limit is kept...
    assert "printed before the limit" in result.output
    # ...and the user is told something is still running.
    assert "still running" in result.output


def test_a_gate_that_finishes_but_leaves_a_helper_is_not_a_timeout(tmp_path, leftover):
    """Waiting for the pipes to close, not for the gate, turned a finished run
    into one that "timed out" at its limit."""
    ws = Workspace(str(ws_root(tmp_path)), gate=leftover(0), gate_timeout=120)
    started = time.monotonic()
    result = ws.run_gate()
    took = time.monotonic() - started
    assert leftover.started()
    assert took < FAST_ENOUGH, "run_gate waited %.0fs for the gate's helper" % took
    assert result.ok and not result.timed_out and result.exit_code == 0
    assert "printed before the limit" in result.output


def test_a_gate_within_its_limit_is_unchanged(tmp_path):
    gate = '"%s" -c "import sys; print(\'all good\'); sys.exit(2)"' % sys.executable
    result = Workspace(str(ws_root(tmp_path)), gate=gate, gate_timeout=60).run_gate()
    assert result.exit_code == 2 and not result.ok and not result.timed_out
    assert "all good" in result.output
    assert "still running" not in result.output


def test_duet_gate_returns_at_its_limit_despite_a_detached_helper(tmp_path, monkeypatch, capsys, leftover):
    from duet import agent_gate
    from duet.cli import main

    root = ws_root(tmp_path)
    where = root / ".duet" / "sessions" / "20260101-000000-abcd"
    where.mkdir(parents=True)
    event = {"kind": "session_start", "session": where.name, "root": str(root),
             "gate": leftover(600), "gate_timeout": LIMIT}
    (where / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    monkeypatch.setenv("DUET_SESSION", where.name)
    started = time.monotonic()
    code = main(["gate", "-C", str(root)])
    took = time.monotonic() - started
    assert leftover.started()
    assert took < FAST_ENOUGH, "`duet gate` waited %.0fs for the gate's helper" % took
    assert code == agent_gate.TIMED_OUT_EXIT
    out = capsys.readouterr().out
    assert "TIMED OUT" in out and "printed before the limit" in out


def test_the_jury_walkthrough_returns_at_its_limit_despite_a_detached_helper(tmp_path, leftover):
    from duet import jury

    root = ws_root(tmp_path)
    started = time.monotonic()
    walk = jury.walkthrough(str(root), leftover(600), timeout=LIMIT)
    took = time.monotonic() - started
    assert leftover.started()
    assert took < FAST_ENOUGH, "the walkthrough waited %.0fs for the helper" % took
    assert walk.timed_out
    assert "printed before the limit" in walk.output


def test_the_fix_replay_returns_at_its_limit_despite_a_detached_helper(tmp_path, leftover):
    from duet import workflows

    root = ws_root(tmp_path)
    (root / "test_app.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    state = {"gate": leftover(600), "gate_timeout": LIMIT}
    workflows.snapshot(str(root), state)
    started = time.monotonic()
    assert workflows.replay_fails(str(root), state) is None
    took = time.monotonic() - started
    assert leftover.started()
    assert took < FAST_ENOUGH, "the replay waited %.0fs for the helper" % took
    assert "ran out of time" in state.get("replay_note", "")


def test_builds_no_tests_check_does_not_wait_for_a_detached_helper(tmp_path, leftover):
    """`duet build` runs the gate once to see it is not green with no tests.
    It read the gate's pipes to EOF, so a gate that finished but left a helper
    held it for as long as the helper ran (up to its 300-second limit)."""
    from duet import build

    root = ws_root(tmp_path)
    started = time.monotonic()
    assert build.gate_proves_nothing(leftover(0), str(root)) is True
    took = time.monotonic() - started
    assert leftover.started()
    assert took < FAST_ENOUGH, "the check waited %.0fs for the helper" % took


def test_subprocess_run_with_a_timeout_is_already_bounded_on_posix(tmp_path, leftover):
    """Why the remaining `subprocess.run(..., capture_output=True, timeout=...)`
    calls (git, `--version` probes, sign-in status checks, the agents' CLIs)
    are left as they are: on POSIX the standard library kills the child at the
    limit and only wait()s for it, taking the partial output from the
    TimeoutExpired, rather than reading the pipes to EOF. A detached helper
    holding them does not delay it. (Pinned here so a change to that is seen.)"""
    leftover(600)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        # The script itself, not a shell: kill() then stops the gate, not
        # just a shell above it that would leave it sleeping.
        subprocess.run([sys.executable, str(tmp_path / "gate.py")],
                       capture_output=True, timeout=LIMIT)
    took = time.monotonic() - started
    assert leftover.started()
    assert took < FAST_ENOUGH, "subprocess.run waited %.0fs for the helper" % took
