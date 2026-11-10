"""Gate results remembered across sessions, for the opening check only.

Every session, and every `duet resume`, used to run the whole gate before
round 1 — even over files that had just passed that very gate. On a loaded
machine that is 5 to 15 minutes before either agent does anything: three
sessions in one day each waited ten minutes or more on an opening gate over
files that had just passed, and a resumed session re-ran a gate on a
workspace nobody had touched.

So a result is written to .duet/gate-cache.json, keyed by the workspace
digest and the exact gate command, and a session that opens on the same bytes
with the same command within a day says so instead of running it again.

Failures are kept too. Measured on 0.15.0: of 257 opening gates (131 hours),
47 had already run on the identical digest with the same command within the
day — 37 of them failing — and every failing one was re-run in full.

What it is not allowed to do, and why:
- A time-out, a skipped gate or a deferral is never stored: none of them says
  what the files do.
- Nothing is decided on it. The result consensus depends on comes from a run
  made in this session (the orchestrator's own per-digest cache), a workflow
  rule never sees a remembered failure, and a rule that still needs to see the
  gate red for itself gets a real run; this only removes the wait before round 1.
- A failure that passes on its one re-run is forgotten: it may have been the
  machine, not the code.
- A file that cannot be read, or reads as something other than what this
  module writes, is the same as no file. A cache is never worth a crash.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from duet.adapters.base import human_duration
from duet.config import config_dir
from duet.workspace import GateResult

CACHE_FILENAME = "gate-cache.json"
VERSION = 1
# A day: long enough to cover a session, its resume and the next one started
# on the same files; short enough that what a passing gate depends on outside
# the workspace (the interpreter, installed packages) has had little time to move.
MAX_AGE = 24 * 3600
# Enough for every state a few sessions open on, small enough to rewrite whole.
MAX_ENTRIES = 50
# What the agents are shown is trimmed to 3000 characters anyway; keep a little
# more than that so the trimmed view is the same as for a run made now. It is
# a cap per entry, so the whole file stays around a megabyte at most.
OUTPUT_CHARS = 20000


def cache_path(root: str) -> Path:
    return config_dir(root) / CACHE_FILENAME


def _entries(root: str) -> List[Dict[str, Any]]:
    """The well-formed entries on disk, or none at all."""
    try:
        data = json.loads(cache_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("digest"), str) or not isinstance(entry.get("command"), str):
            continue
        if not isinstance(entry.get("at"), (int, float)) or isinstance(entry.get("at"), bool):
            continue
        out.append(entry)
    return out


def remember(root: str, digest: str, result: GateResult, at: Optional[float] = None) -> None:
    """Keep a finished run — pass or failure — for this digest and command.

    A time-out, a skipped gate, a deferral and a remembered result are not runs
    that finished on these files, and are not kept.
    """
    if (result.skipped or result.timed_out or result.deferred or result.remembered
            or not result.command):
        return
    entry = {
        "digest": digest,
        "command": result.command,
        "ok": bool(result.ok),
        "exit_code": int(result.exit_code),
        "at": time.time() if at is None else at,
        "duration": round(float(result.duration or 0.0), 3),
        "output": (result.output or "")[-OUTPUT_CHARS:],
    }
    entries = [e for e in _entries(root)
               if not (e["digest"] == digest and e["command"] == result.command)]
    entries.append(entry)
    _write(root, entries[-MAX_ENTRIES:])


def forget(root: str, digest: str, command: str) -> None:
    """Drop what is kept for this digest and command, if anything."""
    entries = _entries(root)
    kept = [e for e in entries if not (e["digest"] == digest and e["command"] == command)]
    if len(kept) != len(entries):
        _write(root, kept)


def _write(root: str, entries: List[Dict[str, Any]]) -> None:
    path = cache_path(root)
    tmp = path.with_name("%s.%d.tmp" % (CACHE_FILENAME, os.getpid()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"version": VERSION, "entries": entries}, indent=1) + "\n",
                       encoding="utf-8")
        # Atomic, so two sessions finishing at once leave one whole file
        # rather than a torn one.
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def when(at: float, now: Optional[float] = None) -> str:
    """"2026-10-10 09:12 (2 hours ago)"."""
    now = time.time() if now is None else now
    return "%s (%s ago)" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(at)),
                            human_duration(max(0, now - at)))


def recall(root: str, digest: str, command: str, now: Optional[float] = None) -> Optional[GateResult]:
    """A run on exactly this digest and command, younger than a day — or None.

    A pass or a failure: whether a remembered failure may be shown is the
    caller's decision (see Orchestrator.opening_gate).
    """
    if not command:
        return None
    now = time.time() if now is None else now
    for entry in reversed(_entries(root)):
        if entry["digest"] != digest or entry["command"] != command:
            continue
        ok, exit_code = entry.get("ok"), entry.get("exit_code")
        # A failure written before failures were kept has no exit code, and a
        # "failure" that exited 0 is not one this module wrote.
        failed = (ok is False and isinstance(exit_code, int)
                  and not isinstance(exit_code, bool) and exit_code != 0)
        if ok is not True and not failed:
            return None
        age = now - entry["at"]
        # A time in the future is a clock that moved, not a fresh pass.
        if age < 0 or age >= MAX_AGE:
            return None
        output = entry.get("output")
        duration = entry.get("duration")
        return GateResult(
            command=command,
            ok=ok is True,
            exit_code=0 if ok is True else exit_code,
            output=output if isinstance(output, str) else "",
            duration=float(duration) if isinstance(duration, (int, float)) else 0.0,
            remembered=when(entry["at"], now),
        )
    return None
