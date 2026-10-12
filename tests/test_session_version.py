"""Which duet ran a session: recorded at its start, and said where it matters.

On one machine, sessions in several projects were silently running an install
three releases old, launched by path from an old virtualenv. Nothing in their
record said so, and neither `duet status` nor the report could show it.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import duet
from duet import cli
from duet.adapters.mock import envelope
from duet.cli import main
from duet.config import AgentSpec, Config
from duet.orchestrator import STATUS_CONSENSUS, Orchestrator

PASSING_GATE = "%s -c pass" % sys.executable
WRITE = envelope("first pass", "CONTINUE",
                 patches=[{"path": "app.py", "content": "print('hi')\n"}])
OBJECT = envelope("that is wrong", "CONTINUE",
                  issues=[{"id": "needs-tests", "title": "no tests", "severity": "major",
                           "detail": "add a test that runs the CLI"}])
FIX = envelope("added tests", "CONTINUE", resolves=["needs-tests"],
               patches=[{"path": "test_app.py", "content": "def test_ok():\n    assert True\n"}])
DONE = envelope("I would ship this", "DONE", confidence=0.9)


def run_session(tmp_path, claude_script, gpt_script, max_rounds=2):
    cfg = Config(
        task="build the thing", acceptance="it works", root=str(tmp_path), gate=PASSING_GATE,
        agents=[AgentSpec("claude", "mock", options={"script": claude_script}),
                AgentSpec("gpt", "mock", options={"script": gpt_script})],
        start="claude", max_rounds=max_rounds,
    )
    orch = Orchestrator(cfg)
    return orch, orch.run()


def events_of(session_dir):
    lines = (Path(session_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def start_of(session_dir):
    return next(e for e in events_of(session_dir) if e["kind"] == "session_start")


def rewrite_start(session_dir, **changes):
    """Make a real session's record look as if another duet wrote it.

    A value of None removes the field: that is a record from before versions
    were recorded at all.
    """
    path = Path(session_dir) / "events.jsonl"
    events = events_of(session_dir)
    for event in events:
        if event["kind"] == "session_start":
            for key, value in changes.items():
                if value is None:
                    event.pop(key, None)
                else:
                    event[key] = value
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def write_session(tmp_path, name, events):
    session = tmp_path / ".duet" / "sessions" / name
    session.mkdir(parents=True)
    (session / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n",
                                          encoding="utf-8")


def gone_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def newest_session(tmp_path):
    return sorted((tmp_path / ".duet" / "sessions").iterdir())[-1]


START = {"kind": "session_start", "task": "t", "max_rounds": 6,
         "agents": {"a": "A", "b": "B"}, "turn_timeout": {"a": 1800, "b": 1800},
         "gate_timeout": 900}


def running(tmp_path, name, **start):
    now = time.time()
    write_session(tmp_path, name, [
        dict(START, t=now - 30, pid=os.getpid(), **start),
        {"kind": "turn_start", "t": now - 20, "round": 1, "agent": "a"}])


def finished(tmp_path, name, **start):
    now = time.time()
    write_session(tmp_path, name, [
        dict(START, t=now - 30, pid=gone_pid(), **start),
        {"kind": "session_end", "t": now - 5, "status": "consensus", "reason": "agreed"}])


def dead(tmp_path, name, **start):
    now = time.time()
    write_session(tmp_path, name, [
        dict(START, t=now - 30, pid=gone_pid(), **start),
        {"kind": "turn_start", "t": now - 20, "round": 3, "agent": "b"}])


def status(tmp_path, capsys, *args):
    main(["status", *args, "-C", str(tmp_path)])
    return capsys.readouterr().out


# -- 1. the record says which duet ran it ---------------------------------------
def test_session_start_records_the_duet_version_and_the_python_that_ran_it(tmp_path):
    orch, result = run_session(tmp_path, [WRITE, DONE], [DONE], max_rounds=4)
    assert result.status == STATUS_CONSENSUS
    start = start_of(orch.session_dir)
    assert start["duet_version"] == duet.__version__
    assert start["python"] == sys.executable


def test_a_resumed_session_records_them_on_its_own_session_start(tmp_path, capsys):
    old, _ = run_session(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])
    rewrite_start(old.session_dir, duet_version="0.8.0", python="/old/venv/bin/python")

    assert main(["resume", "-C", str(tmp_path), "--rounds", "6"]) == 0
    fresh = newest_session(tmp_path)
    assert fresh.name != old.session_id
    start = start_of(fresh)
    assert start["duet_version"] == duet.__version__
    assert start["python"] == sys.executable
    # The old session's record is left saying what actually ran it.
    assert start_of(old.session_dir)["duet_version"] == "0.8.0"


# -- 2. the report says which duet produced it ----------------------------------
def test_the_report_header_names_the_duet_version(tmp_path):
    orch, _ = run_session(tmp_path, [WRITE, DONE], [DONE])
    report = (Path(orch.session_dir) / "report.md").read_text(encoding="utf-8")
    header = report.split("## Sign-off")[0]
    assert "**duet version:** %s" % duet.__version__ in header


# -- 3. duet status ------------------------------------------------------------
def test_status_warns_when_a_running_session_started_on_another_version(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "__version__", "0.13.0")
    running(tmp_path, "20261011-100000-live", duet_version="0.8.0")
    out = status(tmp_path, capsys, "20261011-100000-live")
    assert "runs on duet 0.8.0 (this is 0.13.0)" in out
    assert "it finishes on the version it started with" in out
    # It may have been launched from an old install rather than caught by an
    # upgrade, so the note does not claim to know which.
    assert "upgrade" not in out


def test_status_says_nothing_extra_when_the_version_is_the_same(tmp_path, capsys):
    running(tmp_path, "20261011-100000-live", duet_version=duet.__version__)
    finished(tmp_path, "20261011-100001-done", duet_version=duet.__version__)
    dead(tmp_path, "20261011-100002-gone", duet_version=duet.__version__)
    out = status(tmp_path, capsys)                       # the recent list and the newest
    out += status(tmp_path, capsys, "20261011-100000-live")
    out += status(tmp_path, capsys, "20261011-100001-done")
    assert "runs on" not in out and "ran on" not in out
    assert "older duet" not in out and "upgrade" not in out


def test_status_calls_a_record_without_a_version_an_older_duet(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "__version__", "0.14.0")
    running(tmp_path, "20261011-100000-live")            # an old-format record: no version
    out = status(tmp_path, capsys, "20261011-100000-live")
    assert "runs on an older duet (before %s)" % cli.FIRST_RECORDED_VERSION in out
    assert "(this is 0.14.0)" in out


def test_finished_and_dead_sessions_show_a_different_version_without_the_warning(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "__version__", "0.13.0")
    finished(tmp_path, "20261011-100001-done", duet_version="0.8.0")
    dead(tmp_path, "20261011-100002-gone", duet_version="0.9.0")
    finished(tmp_path, "20261011-100003-old")            # old format

    done = status(tmp_path, capsys, "20261011-100001-done")
    state_line = next(l for l in done.splitlines() if "finished" in l)
    assert "duet 0.8.0" in state_line
    assert "upgrade" not in done and "this is" not in done

    gone = status(tmp_path, capsys, "20261011-100002-gone")
    state_line = next(l for l in gone.splitlines() if "stopped without finishing" in l)
    assert "duet 0.9.0" in state_line
    assert "upgrade" not in gone and "this is" not in gone

    # A finished record with no version adds nothing: most of a listing is
    # old sessions, and the same note on every line is noise.
    old = status(tmp_path, capsys, "20261011-100003-old")
    assert "older duet" not in old and "ran on" not in old

    listing = status(tmp_path, capsys)
    assert "duet 0.8.0" in listing and "duet 0.9.0" in listing
    assert "upgrade" not in listing and "older duet" not in listing


def test_status_json_carries_the_recorded_version_and_old_records_still_load(tmp_path, capsys):
    finished(tmp_path, "20261011-100001-done", duet_version="0.8.0")
    finished(tmp_path, "20261011-100002-old")
    main(["status", "20261011-100001-done", "-C", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["duet_version"] == "0.8.0"
    main(["status", "20261011-100002-old", "-C", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["duet_version"] is None


# -- 4. duet resume -------------------------------------------------------------
def test_resume_says_once_that_the_rest_runs_on_this_version(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "__version__", "0.13.0")
    old, _ = run_session(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])
    rewrite_start(old.session_dir, duet_version="0.8.0")
    capsys.readouterr()

    assert main(["resume", "-C", str(tmp_path), "--rounds", "6"]) == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "0.8.0" in l]
    assert len(lines) == 1, out
    assert "0.13.0" in lines[0] and "rest of the session runs on" in lines[0]


def test_resume_of_the_same_version_says_nothing_about_it(tmp_path, capsys):
    old, _ = run_session(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])
    capsys.readouterr()
    assert main(["resume", "-C", str(tmp_path), "--rounds", "6"]) == 0
    assert "rest of the session runs on" not in capsys.readouterr().out


def test_an_old_format_record_resumes_and_reports_with_no_error(tmp_path, capsys):
    old, _ = run_session(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])
    rewrite_start(old.session_dir, duet_version=None, python=None)
    capsys.readouterr()

    assert main(["resume", "-C", str(tmp_path), "--rounds", "6"]) == 0
    fresh = newest_session(tmp_path)
    report = (fresh / "report.md").read_text(encoding="utf-8")
    assert "**duet version:** %s" % duet.__version__ in report
    # status reads both the old record and the new one without tripping.
    assert main(["status", old.session_id, "-C", str(tmp_path)]) in (0, 1)
    assert main(["status", fresh.name, "-C", str(tmp_path)]) in (0, 1)
