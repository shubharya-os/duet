import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_OWN_BASETEMP = ""


def pytest_configure(config):
    """Each run gets a temp root of its own.

    By default every pytest run by this user shares one root and numbers its
    directories there, pruning older ones as it goes. This suite is run by
    several duet sessions at once on one machine, and under that contention the
    runs collide in it: a whole stretch of tests errored at setup with "could not
    create numbered dir", which read as a red gate on code nobody had broken. An
    explicit --basetemp is left alone.
    """
    global _OWN_BASETEMP
    if config.option.basetemp is None:
        _OWN_BASETEMP = tempfile.mkdtemp(prefix="duet-tests-")
        config.option.basetemp = _OWN_BASETEMP


def pytest_unconfigure(config):
    if _OWN_BASETEMP:
        shutil.rmtree(_OWN_BASETEMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def _outside_a_duet_session(monkeypatch):
    """No test inherits the marker of a session it is running inside.

    `DUET_SESSION` is set in the environment of every agent duet runs, and this
    suite gets run from inside duet sessions. Left alone, every command that
    calls `refuse_nested` — `run`, `resume` — would refuse before doing anything,
    and the tests for them would only pass if some earlier test happened to run a
    session first and clear it. The tests that want the marker set it themselves.
    """
    monkeypatch.delenv("DUET_SESSION", raising=False)
    yield
    os.environ.pop("DUET_SESSION", None)


@pytest.fixture(autouse=True)
def _no_backoff_sleeping(monkeypatch):
    """The suite never sleeps through the transient-failure backoff.

    The orchestrator waits minutes between re-sending a turn that died on a
    network blip, which is right in a session and intolerable in a test run. The
    schedule itself is a pure function (`orchestrator.retry_delays`) and is tested
    as one; a test that wants to watch the waits happen replaces this with its own
    recorder, which overrides this fixture.

    `raising=False` because this is autouse over the whole suite: on a tree with
    no backoff in it — the snapshot the harness replays these tests against to
    check they catch the bug — a hard setattr would error out every test in every
    file at setup. That is a red replay that proves nothing about the bug. This
    way each test still fails there on what it actually asserts.
    """
    monkeypatch.setattr("duet.orchestrator.SLEEP", lambda seconds: None, raising=False)


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path_factory, monkeypatch):
    """No test reads or writes the developer's real ~/.duet.

    duet remembers a Codex usage limit there. Left alone, a machine that had
    actually hit one would fail the probe tests, and a test that provokes one
    would leave that memory behind for the next real `duet doctor`.
    """
    monkeypatch.setenv("DUET_STATE_DIR", str(tmp_path_factory.mktemp("duet-state")))
