"""`duet gate`: the sanctioned way for an agent inside a session to run the gate.

Measured on 262 sessions on duet 0.15+, 72 (27%) had an agent refused while
running the gate itself — `./gate.sh > /tmp/gate.log 2>&1; echo exit=$?`,
`... | tail -60`, `... | tee log`. The gate's executable is allow-listed, but
every other piece of a compound command needs its own permission and output
written outside the workspace is refused, so the agent's effort went nowhere.

This command is the whole of what those wrappers were for: it runs the
session's own gate in the workspace with the session's time limit, prints what
the harness would show (failures first, then head and tail), saves the whole
output in the session directory and says where, and exits with the gate's own
code. It starts no agents, so the nesting guard does not apply to it.

Each run is recorded in the session directory (AGENT_GATES), with the digest
before and after and what the run changed, so the orchestrator can learn the
gate's outputs from it after the turn as it does from its own runs, and tell
the peer the agent ran the gate and what it said. A run recorded here is never
the harness's gate: no sign-off is ever counted against it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from duet.workspace import GateResult, Workspace, changed_paths

# One JSON object per run, appended; read back by Orchestrator.
AGENT_GATES = "agent-gates.jsonl"

# Distinct from any code a test runner exits with for a failure, so a caller
# can tell "ran out of time" from "failed" by the exit code alone. 124 is what
# GateResult already records for a timeout, and what coreutils `timeout` uses.
TIMED_OUT_EXIT = 124
# No session, or a session with no gate: nothing was run.
USAGE_EXIT = 2


class NoGate(Exception):
    """There is no session to run the gate for, or it has no gate."""


def find_session(start: str, session_id: str) -> Path:
    """The directory of session `session_id`, looked for from `start` upwards.

    An agent's shell starts in the workspace root, but may have moved into a
    subdirectory; the session lives at <root>/.duet/sessions/<id> either way.
    """
    if not session_id or "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise NoGate("not inside a duet session (DUET_SESSION is not set)")
    here = Path(start).expanduser().resolve()
    for base in [here] + list(here.parents):
        candidate = base / ".duet" / "sessions" / session_id
        if candidate.is_dir():
            return candidate
    raise NoGate("no directory for session %s under %s or any directory above it"
                 % (session_id, here))


def session_gate(session_dir: Path) -> Tuple[str, int, List[str]]:
    """(gate command, gate timeout in seconds, gate outputs known so far).

    The command and the limit come from the session's own record of how it was
    started — the last `session_start` event, so a resumed session's raised
    `--gate-timeout` is the one used — and failing that its saved config. The
    gate outputs come from the saved state, for a digest counted the way the
    harness counts it.
    """
    gate, timeout = "", 0
    try:
        lines = (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") == "session_start":
            gate = str(event.get("gate") or "")
            timeout = int(event.get("gate_timeout") or 0)
            break
    outputs: List[str] = []
    try:
        state = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if isinstance(state, dict):
        config = state.get("config") or {}
        if not gate and isinstance(config, dict):
            gate = str(config.get("gate") or "")
            timeout = timeout or int(config.get("gate_timeout") or 0)
        outputs = [str(p) for p in state.get("gate_outputs") or [] if str(p).strip()]
    if not gate.strip():
        raise NoGate("session %s has no gate to run" % session_dir.name)
    return gate, timeout, outputs


def read_runs(session_dir: Path) -> List[Dict[str, Any]]:
    """Every run recorded so far, oldest first. A torn last line is skipped."""
    try:
        lines = (session_dir / AGENT_GATES).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    runs: List[Dict[str, Any]] = []
    for line in lines:
        try:
            run = json.loads(line)
        except ValueError:
            continue
        if isinstance(run, dict):
            runs.append(run)
    return runs


def run(session_dir: Path) -> Tuple[GateResult, Dict[str, Any]]:
    """Run the session's gate in its workspace, save the log, record the run."""
    gate, timeout, outputs = session_gate(session_dir)
    root = session_dir.parents[2]
    kwargs = {"gate_timeout": timeout} if timeout else {}
    workspace = Workspace(str(root), gate=gate, **kwargs)
    workspace.gate_outputs = set(outputs)
    digest_before = workspace.digest()
    before = workspace.fingerprint(include_gate_outputs=True)
    started = time.time()
    result = workspace.run_gate()
    after = workspace.fingerprint(include_gate_outputs=True)
    digest_after = workspace.digest()

    number = len(read_runs(session_dir)) + 1
    log = session_dir / ("agent-gate-%d-%s.log" % (number, digest_before[:8]))
    try:
        log.write_text("$ %s\n%s\n%s" % (result.command, result.headline(), result.output),
                       encoding="utf-8")
        result.log = str(log)
    except OSError:
        pass
    record: Dict[str, Any] = {
        "n": number,
        "t": started,
        "command": gate,
        "ok": result.ok,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "timeout": result.timeout,
        "duration": round(result.duration, 3),
        "digest_before": digest_before,
        "digest_after": digest_after,
        "log": result.log,
        # The manifest diff: each path the run added, removed or rewrote, with
        # its hash before and after (None for absent). What the orchestrator
        # needs to tell the gate's writes from the agent's own edits.
        "changed": {p: [before.get(p), after.get(p)] for p in changed_paths(before, after)},
    }
    with (session_dir / AGENT_GATES).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return result, record


def exit_code(result: GateResult) -> int:
    if result.timed_out:
        return TIMED_OUT_EXIT
    return result.exit_code


def main(root: str) -> int:
    """`duet gate`, as run by an agent: print what the harness would, exit with the gate's code."""
    try:
        session_dir = find_session(root, os.environ.get("DUET_SESSION", "").strip())
        result, record = run(session_dir)
    except NoGate as exc:
        print("duet gate: %s" % exc)
        print("It runs the gate of the duet session you are an agent in, and only there.")
        return USAGE_EXIT
    # The log is always named, not only when the output is trimmed: the whole
    # output is the point of running it yourself.
    print(result.render())
    print()
    print("The whole output is in %s" % result.log if result.log else
          "The whole output could not be saved in the session directory.")
    print("This is your own run, on state %s. It is shown to your peer, but it is not the "
          "harness's gate: sign-offs are only ever counted against the harness's own run "
          "(say CONTINUE with \"run_gate\": true to have it run)." % record["digest_before"][:8])
    if record["digest_after"] != record["digest_before"]:
        print("The gate changed %d path(s) in the workspace. After your turn the harness "
              "leaves what the gate wrote out of the signed state where it safely can, as "
              "it does for its own runs." % len(record["changed"]))
    return exit_code(result)
