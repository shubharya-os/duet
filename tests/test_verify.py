"""`duet verify`: does the last sign-off still describe this workspace?

The sessions here are real orchestrator runs against scripted peers, so the
sign-off `verify` reads back is one the consensus rules actually produced
rather than a hand-written state.json.
"""

import json
import sys

from duet.adapters.mock import MockAdapter, envelope
from duet.cli import main
from duet.config import AgentSpec, Config
from duet.orchestrator import STATUS_CONSENSUS, Orchestrator

PASSING_GATE = "%s -c pass" % sys.executable
FAILING_GATE = "%s -c \"raise SystemExit(1)\"" % sys.executable

WRITE = envelope("first pass", "CONTINUE",
                 patches=[{"path": "app.py", "content": "print('hi')\n"}])
DONE = envelope("I would ship this", "DONE", confidence=0.9)


def signed_session(tmp_path, claude_script=None, gpt_script=None, gate=PASSING_GATE):
    """Run a real session to a double sign-off and return (orchestrator, result)."""
    cfg = Config(
        task="build the thing",
        root=str(tmp_path),
        gate=gate,
        agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
        start="claude",
        max_rounds=6,
    )
    adapters = {
        "claude": MockAdapter(name="claude", cwd=str(tmp_path),
                              config={"script": claude_script or [WRITE, DONE]}),
        "gpt": MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": gpt_script or [DONE]}),
    }
    orch = Orchestrator(cfg, adapters=adapters)
    return orch, orch.run()


def verify(tmp_path, *extra):
    return main(["verify", "-C", str(tmp_path), *extra])


def verify_json(tmp_path, capsys, *extra):
    code = verify(tmp_path, "--json", *extra)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# -- the three cases the command exists for --------------------------------
def test_unchanged_workspace_still_holds(tmp_path, capsys):
    _, result = signed_session(tmp_path)
    assert result.status == STATUS_CONSENSUS

    assert verify(tmp_path) == 0
    out = capsys.readouterr().out
    assert result.digest in out          # the state they signed
    assert "still holds" in out
    assert "passed" in out               # the gate was really re-run


def test_drifted_workspace_no_longer_holds(tmp_path, capsys):
    _, result = signed_session(tmp_path)
    (tmp_path / "app.py").write_text("print('something else')\n", encoding="utf-8")

    assert verify(tmp_path) == 1
    out = capsys.readouterr().out
    assert "no longer holds" in out
    assert "changed since sign-off" in out
    assert result.digest in out          # both ids are shown, so the drift is legible


def test_no_sessions_yet(tmp_path, capsys):
    assert verify(tmp_path) == 2
    out = capsys.readouterr().out
    assert "no sessions yet" in out
    assert "duet run" in out


# -- everything else it has to get right -----------------------------------
def test_json_report_carries_both_state_ids(tmp_path, capsys):
    _, result = signed_session(tmp_path)
    code, data = verify_json(tmp_path, capsys)
    assert code == 0 and data["ok"] is True
    assert data["signed_digest"] == result.digest == data["current_digest"]
    assert data["match"] is True
    assert data["gate"]["ok"] is True and data["gate"]["skipped"] is False
    assert set(data["signoffs"]) == {"claude", "gpt"}

    (tmp_path / "app.py").write_text("drift\n", encoding="utf-8")
    code, data = verify_json(tmp_path, capsys)
    assert code == 1 and data["ok"] is False
    assert data["match"] is False
    assert data["signed_digest"] == result.digest
    assert data["current_digest"] != result.digest


def test_no_sessions_in_json_mode_is_still_machine_readable(tmp_path, capsys):
    code, data = verify_json(tmp_path, capsys)
    assert code == 2 and data["ok"] is False
    assert data["session"] is None


def test_gate_failure_fails_even_when_the_state_matches(tmp_path, capsys):
    _, result = signed_session(tmp_path)
    code, data = verify_json(tmp_path, capsys, "--gate", FAILING_GATE)
    assert code == 1
    assert data["match"] is True          # the files are untouched...
    assert data["gate"]["ok"] is False    # ...but the gate no longer passes
    assert data["current_digest"] == result.digest


def test_a_session_that_never_agreed_does_not_verify(tmp_path, capsys):
    # gpt keeps objecting, so only claude ever votes DONE.
    stubborn = envelope("not yet", "CONTINUE",
                        issues=[{"id": "needs-tests", "title": "no tests", "severity": "blocker",
                                 "detail": "add a test"}])
    _, result = signed_session(tmp_path, claude_script=[WRITE, DONE], gpt_script=[stubborn])
    assert result.status != STATUS_CONSENSUS

    assert verify(tmp_path) == 1
    out = capsys.readouterr().out
    assert "never produced a double sign-off" in out
    assert "gpt never signed off" in out


def test_falls_back_past_a_session_that_saved_no_state(tmp_path, capsys):
    orch, _ = signed_session(tmp_path)
    crashed = tmp_path / ".duet" / "sessions" / "99999999-235959-dead"
    crashed.mkdir(parents=True)
    (crashed / "events.jsonl").write_text("{}\n", encoding="utf-8")  # killed before its first save

    assert verify(tmp_path) == 0
    out = capsys.readouterr().out
    assert orch.session_id in out
    assert "99999999-235959-dead" in out  # and it says which one it skipped


def test_a_named_session_can_be_verified(tmp_path, capsys):
    orch, _ = signed_session(tmp_path)
    assert verify(tmp_path, orch.session_id) == 0
    assert orch.session_id in capsys.readouterr().out

    assert verify(tmp_path, "no-such-session") == 2
    assert "no session" in capsys.readouterr().out


def test_a_session_with_no_gate_says_so_instead_of_claiming_green(tmp_path, capsys):
    signed_session(tmp_path, gate="")
    code, data = verify_json(tmp_path, capsys)
    assert code == 0
    assert data["gate"]["skipped"] is True

    assert verify(tmp_path) == 0
    out = capsys.readouterr().out
    assert "none recorded for this session" in out
    assert "no gate was recorded" in out


def test_the_gate_runs_after_the_digest_is_taken(tmp_path, capsys):
    """A gate that writes a file must not make its own output look like drift."""
    _, result = signed_session(tmp_path)
    littering = "%s -c \"open('gate-artifact.txt','w').write('x')\"" % sys.executable
    code, data = verify_json(tmp_path, capsys, "--gate", littering)
    assert (tmp_path / "gate-artifact.txt").is_file()
    assert data["current_digest"] == result.digest
    assert code == 0
