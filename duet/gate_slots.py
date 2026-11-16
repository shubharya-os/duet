"""Gate slots: a machine-wide limit on how many duet gates run at once.

Measured on 122 gate runs on duet 0.17-0.19.1 across a dozen projects on one
machine, mostly iOS gates building with xcodebuild and testing in simulators:
with no other duet gate running alongside, 2 of 33 runs timed out (6%); with 1
other, 3 of 17 (18%); with 2-3 others, 2 of 36 (6%); with 4 or more others, 21
of 36 (58%). Five sessions ended BLOCKED on a gate that kept running out of
time at a load average in the hundreds — not on anything in their code.

So every gate run takes a slot first, from a pool shared by every duet process
of this user: files under ~/.duet/gate-slots/ (or $DUET_HOME/gate-slots/),
each locked while a gate holds it — flock on POSIX, msvcrt on Windows. A lock
belongs to an open file, so the OS lets go of it when the process dies, however
it dies: a crashed session can never keep a slot. There is no daemon.

What it is not allowed to do, and why:
- Waiting is not running. The gate's time limit starts when it gets its slot.
- A gate never waits forever: after MAX_WAIT it runs anyway, and says so. A
  pool that has stuck is better slow than a session that has stuck.
- A gate run from inside a gate (DUET_GATE is set) takes no slot: the gate
  around it already holds one, and waiting on itself would deadlock.
- A state directory that cannot be made or locked costs the limit, never the
  gate: it runs, as it did before there were slots.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional

from duet.adapters.base import human_duration

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt
else:
    import fcntl

# Up to 3 others alongside were measured fine; 4 or more were not.
DEFAULT_SLOTS = 3
# Long enough for a queue of full-length gates to drain, short enough that a
# wedged pool cannot hold a session past an afternoon.
MAX_WAIT = 3 * 3600
# Between tries for a free slot.
POLL = 0.5
SLOTS_DIRNAME = "gate-slots"

# Called as notify(kind, **payload) with "gate_queued" (running=, slots=) when
# a gate has to wait, then "gate_slot" (waited=, gave_up=) when it stops waiting.
Notify = Callable[..., None]


def state_dir() -> Path:
    """The user's duet state directory: $DUET_HOME, or ~/.duet."""
    home = os.environ.get("DUET_HOME", "").strip()
    return Path(home).expanduser() if home else Path.home() / ".duet"


def slots_dir() -> Path:
    return state_dir() / SLOTS_DIRNAME


def pool_size() -> int:
    """How many gates may run at once: $DUET_GATE_SLOTS, 0 for no limit.

    There is no user-level config file in duet, so the environment is the
    one place to set it. Anything that is not a whole number >= 0 is the default.
    """
    raw = os.environ.get("DUET_GATE_SLOTS", "").strip()
    try:
        size = int(raw)
    except ValueError:
        return DEFAULT_SLOTS
    return size if size >= 0 else DEFAULT_SLOTS


@dataclass
class Slot:
    """What taking a slot came to: how long it waited, and whether it gave up."""
    waited: float = 0.0
    queued: bool = False
    gave_up: bool = False


def describe(kind: str, payload: Any) -> str:
    """The one terminal line for a slot event."""
    if kind == "gate_queued":
        running = int(payload.get("running") or 0)
        return "waiting for a gate slot: %d duet gate%s %s running on this machine" % (
            running, "" if running == 1 else "s", "is" if running == 1 else "are")
    if payload.get("gave_up"):
        return ("no gate slot came free in %s; running the gate anyway"
                % human_duration(payload.get("waited") or 0))
    return "got a gate slot after waiting %s" % human_duration(payload.get("waited") or 0)


def print_notify(kind: str, **payload: Any) -> None:
    """Where there is no session to report to: one line on stderr."""
    try:
        print("duet: " + describe(kind, payload), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _lock(fh: Any) -> bool:
    try:
        if os.name == "nt":  # pragma: no cover
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh: Any) -> None:
    try:
        if os.name == "nt":  # pragma: no cover
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _take(directory: Path, size: int) -> Optional[Any]:
    """An open, locked slot file, or None if all `size` are held."""
    for number in range(size):
        fh = open(directory / ("slot-%d.lock" % number), "a+b")
        if _lock(fh):
            return fh
        fh.close()
    return None


def running(directory: Optional[Path] = None) -> int:
    """How many slot files are locked right now, by any duet process.

    Counted over every slot file there, not just this pool's: a process with
    a larger DUET_GATE_SLOTS may hold one beyond it.
    """
    directory = directory or slots_dir()
    held = 0
    try:
        paths: List[Path] = sorted(directory.glob("slot-*.lock"))
    except OSError:
        return 0
    for path in paths:
        try:
            fh = open(path, "a+b")
        except OSError:
            continue
        try:
            if _lock(fh):
                _unlock(fh)
            else:
                held += 1
        finally:
            fh.close()
    return held


@contextmanager
def gate_slot(notify: Optional[Notify] = None, size: Optional[int] = None,
              max_wait: Optional[float] = None) -> Iterator[Slot]:
    """Hold a gate slot for the body of the `with`; released however it ends."""
    notify = notify or print_notify
    size = pool_size() if size is None else size
    max_wait = MAX_WAIT if max_wait is None else max_wait
    slot = Slot()
    if size <= 0 or os.environ.get("DUET_GATE"):
        yield slot
        return
    directory = slots_dir()
    held = None
    started = time.monotonic()
    try:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            held = _take(directory, size)
            while held is None:
                slot.waited = time.monotonic() - started
                if slot.waited >= max_wait:
                    slot.gave_up = True
                    break
                if not slot.queued:
                    slot.queued = True
                    notify("gate_queued", running=max(running(directory), size), slots=size)
                time.sleep(POLL)
                held = _take(directory, size)
        except OSError:
            held = None
        slot.waited = time.monotonic() - started if slot.queued else 0.0
        if slot.queued:
            notify("gate_slot", waited=round(slot.waited, 1), gave_up=slot.gave_up)
        yield slot
    finally:
        if held is not None:
            _unlock(held)
            held.close()
