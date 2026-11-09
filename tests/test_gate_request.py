"""Agents can ask for the gate without ending the session, and a gate failure
is never trimmed out of what they are shown.

Measured on 242 sessions run on duet 0.15.0: 24 of the 67 that ended BLOCKED
did so only to get the gate run — in projects where the agents cannot build,
the deferred gate ran after DONE or BLOCKED and nowhere else, and BLOCKED stops
the session. And 61 turns complained that the trimmed gate output had lost the
names of the failing tests, which xcodebuild, XCTest and pytest print in the
middle of a long log.

Nothing this change introduced is imported at the top of this file: the
harness replays these tests against the code as it was before the session, and
they must fail there on what they assert, not on an ImportError for a new name.
"""

import json
import sys
from pathlib import Path

import pytest

from duet.adapters.mock import MockAdapter, envelope
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator
from duet.protocol import parse_envelope
from duet.workspace import GateResult


def edit(text, verdict="CONTINUE", path="app.py", **kwargs):
    """A turn that changes the workspace, so the state it leaves is new."""
    return envelope("wrote %s" % text, verdict,
                    patches=[{"path": path, "content": text + "\n"}], **kwargs)


def python_gate(tmp_path, body):
    """A gate that runs `body` as Python and counts every time it starts."""
    runs = tmp_path / "runs"
    script = tmp_path / "gate_script.py"
    script.write_text("import os, sys\nopen(%r, 'a').write('run\\n')\n%s\n" % (str(runs), body),
                      encoding="utf-8")
    return '"%s" "%s"' % (sys.executable, script), runs


def count(runs):
    return len(runs.read_text(encoding="utf-8").splitlines()) if runs.exists() else 0


def session(tmp_path, gate, script, peer, **kwargs):
    root = tmp_path / "ws"
    root.mkdir()
    cfg = Config(task="add it", root=str(root),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=kwargs.pop("max_rounds", 4), gate=gate, **kwargs)
    adapters = {
        "claude": MockAdapter(name="claude", cwd=str(root), config={"script": list(script)}),
        "gpt": MockAdapter(name="gpt", cwd=str(root), config={"script": list(peer)}),
    }
    return Orchestrator(cfg, adapters=adapters)


def events(orch):
    path = Path(orch.session_dir) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def kinds(orch, kind):
    return [e for e in events(orch) if e["kind"] == kind]


# -- the envelope field ---------------------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("run_gate", True), ("run_gate", "yes"), ("run_gate", "true"), ("run_gate", "True"),
    ("run_gate", 1), ("gate", True), ("request_gate", True), ("request_gate", "on"),
])
def test_the_envelope_reads_a_gate_request_in_its_variants(field, value):
    env = parse_envelope(envelope("please check", "CONTINUE", **{field: value}))
    assert env.run_gate is True


@pytest.mark.parametrize("body", [
    {}, {"run_gate": False}, {"run_gate": "no"}, {"run_gate": "false"}, {"run_gate": 0},
    {"run_gate": None}, {"gate": "PASSED"}, {"run_gate": "maybe"},
])
def test_anything_else_is_not_a_gate_request(body):
    env = parse_envelope(envelope("working", "CONTINUE", **body))
    assert env.run_gate is False


def test_the_request_survives_a_resume():
    from duet.protocol import Envelope

    env = parse_envelope(envelope("please check", "CONTINUE", run_gate=True))
    assert Envelope.from_dict(env.to_dict()).run_gate is True


# -- the gate runs on request, and the session goes on ---------------------------
def test_run_gate_with_continue_runs_the_gate_and_the_session_continues(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    orch = session(tmp_path, gate, [edit("one", run_gate=True), edit("three")],
                   [edit("two")], max_rounds=3)
    result = orch.run()

    # Opening, the requested run, and the one at the end for the report.
    assert count(runs) == 3
    first, second, third = kinds(orch, "turn_done")
    assert first["verdict"] == "CONTINUE"
    assert first.get("gate_requested") is True
    assert first.get("gate_ok") is True and not first.get("gate_deferred")
    # It is not a vote: the session went on to the next turns.
    assert second.get("gate_deferred") is True and not second.get("gate_requested")
    assert third["round"] == 3
    assert result.status != "blocked"
    # The run was on the state the requesting turn left, and the peer saw it.
    log = events(orch)
    after = log.index(next(e for e in log if e["kind"] == "turn_done"))
    assert [e for e in log[:after] if e["kind"] == "gate_done"][-1]["digest"] == first["digest"]
    prompt = orch.adapters["gpt"].prompts[0]
    assert "PASSED" in prompt and "NOT RUN" not in prompt.split("=== ACCEPTANCE GATE ===")[1][:200]
    assert "gate run on request" in orch.history[0]
    assert "gate run on request" in Path(result.report).read_text(encoding="utf-8")


def test_without_run_gate_continue_keeps_the_deferral(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    orch = session(tmp_path, gate, [edit("one")], [edit("two")], max_rounds=2)
    orch.run()

    assert count(runs) == 2
    assert [e.get("gate_requested") for e in kinds(orch, "turn_done")] == [False, False]


def test_a_request_on_a_state_already_gated_is_answered_from_the_cache(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(0)")
    ask = envelope("check it again", "CONTINUE", run_gate=True)
    # The first turn's request runs it; neither the peer's nor the next one
    # changes a file, so both are answered with the same run.
    orch = session(tmp_path, gate, [edit("one", run_gate=True), ask], [ask], max_rounds=3)
    orch.run()

    # Opening, one requested run; the end reuses it.
    assert count(runs) == 2
    turns = kinds(orch, "turn_done")
    assert all(t.get("gate_requested") for t in turns)
    assert all(t.get("gate_ok") is True for t in turns)


def test_a_red_request_on_unchanged_files_gets_only_the_flaky_rerun(tmp_path):
    gate, runs = python_gate(tmp_path, "sys.exit(1)")
    ask = envelope("check it again", "CONTINUE", run_gate=True)
    orch = session(tmp_path, gate, [edit("one", run_gate=True), ask], [ask], max_rounds=3)
    orch.run()

    # Opening, the requested run, its one re-run when nothing changed. No more.
    assert count(runs) == 3


def test_the_terminal_says_the_gate_ran_on_request(capsys):
    from duet.cli import make_reporter

    say = make_reporter(["claude", "gpt"], verbose=False)
    say({"kind": "turn_done", "agent": "claude", "verdict": "CONTINUE", "digest": "abcdef1234",
         "gate_ok": False, "gate_deferred": False, "gate_requested": True})
    out = capsys.readouterr().out
    assert "gate run on request" in out
    assert "FAILING" in out


# -- the prompts ----------------------------------------------------------------
def test_every_agent_is_told_it_can_ask_for_the_gate_and_what_blocked_is_for():
    from duet import prompts

    for edits in (True, False):
        text = prompts.system_prompt(name="a", display="A", peer="b", peer_display="B",
                                     role=prompts.ROLE_LEAD, root="/w", edits_workspace=edits)
        assert '"run_gate": true' in text
        assert "CONTINUE" in text
        lowered = text.lower()
        # BLOCKED is narrowed to what neither agent can fix, and they are told why.
        assert "blocked" in lowered and "stops the session" in lowered
        assert "credentials" in lowered
        assert "compile" in lowered


# -- failures are never trimmed away --------------------------------------------
def noise(n, word="ok"):
    return "".join("line %d of build chatter %s\n" % (i, word) for i in range(n))


def long_log(middle):
    """A log far over the render limit with `middle` buried in the centre."""
    return noise(400) + middle + noise(400, "later")


SAMPLES = {
    "pytest": (
        "____ test_parse ____\n"
        "    def test_parse():\n"
        ">       assert parse('x') == 2\n"
        "E       AssertionError: assert 1 == 2\n"
        "E        +  where 1 = parse('x')\n"
        "E        more detail one\n"
        "E        more detail two\n"
        "E        more detail three\n"
        "FAILED tests/test_parser.py::test_parse - AssertionError: assert 1 == 2\n"
        "ERROR tests/test_db.py::test_connect - OSError\n",
        ["FAILED tests/test_parser.py::test_parse", "ERROR tests/test_db.py::test_connect",
         "E       AssertionError: assert 1 == 2"],
    ),
    "unittest": (
        "======================================================================\n"
        "FAIL: test_add (test_math.MathTest.test_add)\n"
        "----------------------------------------------------------------------\n"
        "ERROR: test_div (test_math.MathTest.test_div)\n",
        ["FAIL: test_add (test_math.MathTest.test_add)", "ERROR: test_div (test_math.MathTest.test_div)"],
    ),
    "xcodebuild": (
        "/Users/me/App/Sources/View.swift:42:17: error: cannot find 'Foo' in scope\n"
        "Test Case '-[AppTests.LoginTests testLogin]' failed (0.012 seconds).\n"
        "Failing tests:\n"
        "\tAppTests.LoginTests/testLogin()\n"
        "\tAppTests.CartTests/testTotal()\n"
        "\n"
        "** TEST FAILED **\n"
        "** BUILD FAILED **\n",
        ["View.swift:42:17: error: cannot find 'Foo' in scope",
         "Test Case '-[AppTests.LoginTests testLogin]' failed",
         "AppTests.LoginTests/testLogin()", "AppTests.CartTests/testTotal()",
         "** TEST FAILED **", "** BUILD FAILED **"],
    ),
    "swiftc": (
        "error: emit-module command failed with exit code 1\n",
        ["error: emit-module command failed with exit code 1"],
    ),
    "jest": (
        "FAIL src/cart.test.js\n"
        "  ✕ adds totals (5 ms)\n"
        "  ● Cart › adds totals\n",
        ["FAIL src/cart.test.js", "✕ adds totals", "● Cart › adds totals"],
    ),
    "go": (
        "--- FAIL: TestHandler (0.00s)\n",
        ["--- FAIL: TestHandler"],
    ),
    "cargo": (
        "test parser::tests::roundtrip ... FAILED\n"
        "thread 'parser::tests::roundtrip' panicked at src/parser.rs:12:5:\n",
        ["test parser::tests::roundtrip ... FAILED", "panicked at src/parser.rs:12:5"],
    ),
    "traceback": (
        "Traceback (most recent call last):\n"
        '  File "run.py", line 3, in <module>\n'
        "    main()\n"
        "KeyError: 'missing-config-key'\n",
        ["KeyError: 'missing-config-key'"],
    ),
}


@pytest.mark.parametrize("runner", sorted(SAMPLES))
def test_a_failure_in_the_middle_of_a_long_log_survives_rendering(runner):
    middle, wanted = SAMPLES[runner]
    result = GateResult(command="make test", ok=False, exit_code=1, output=long_log(middle))
    text = result.render()
    assert "[trimmed]" in text
    for line in wanted:
        assert line in text, (runner, line)
    # The failures come before the head and tail of the log.
    assert text.index(wanted[0]) < text.index("line 0 of build chatter")


def test_the_failure_section_is_deduplicated_ordered_and_capped():
    from duet.workspace import failure_lines

    out = "FAILED t.py::a\nFAILED t.py::b\nFAILED t.py::a\n" + "".join(
        "FAILED t.py::n%d\n" % i for i in range(200))
    lines, more = failure_lines(out)
    assert lines[:2] == ["FAILED t.py::a", "FAILED t.py::b"]
    assert lines.count("FAILED t.py::a") == 1
    assert len(lines) <= 80
    assert more == 202 - len(lines)
    text = GateResult(command="pytest", ok=False, exit_code=1, output=out + noise(400)).render()
    assert "%d more" % more in text


def test_pytest_assertion_detail_is_limited():
    from duet.workspace import failure_lines

    lines, _ = failure_lines(SAMPLES["pytest"][0])
    assert sum(1 for line in lines if line.startswith("E ")) <= 2


def test_a_short_log_is_shown_whole_with_no_failure_heading():
    out = "FAILED t.py::a - boom\n1 failed"
    text = GateResult(command="pytest", ok=False, exit_code=1, output=out).render()
    assert text.count("FAILED t.py::a") == 1
    assert "[trimmed]" not in text


# -- the full log ---------------------------------------------------------------
def test_every_gate_run_writes_its_full_log_and_a_trimmed_view_names_it(tmp_path):
    body = "print('x' * 50 + '\\n') or None\n" \
           "for i in range(400): print('chatter', i)\n" \
           "print('FAILED tests/test_core.py::test_deep_in_the_middle')\n" \
           "for i in range(400): print('more chatter', i)\n" \
           "sys.exit(1)"
    gate, runs = python_gate(tmp_path, body)
    orch = session(tmp_path, gate, [edit("one", run_gate=True)], [edit("two")], max_rounds=2)
    result = orch.run()

    digest = kinds(orch, "turn_done")[0]["digest"]
    log = Path(orch.session_dir) / ("gate-%s.log" % digest[:8])
    assert log.is_file()
    full = log.read_text(encoding="utf-8")
    assert "chatter 0" in full and "more chatter 399" in full
    assert "test_deep_in_the_middle" in full
    prompt = orch.adapters["gpt"].prompts[0]
    assert str(log) in prompt
    assert "test_deep_in_the_middle" in prompt
    report = Path(result.report).read_text(encoding="utf-8")
    assert "gate-%s.log" % result.digest[:8] in report


def test_an_agent_without_tools_can_read_the_saved_log_through_reads(tmp_path):
    gate, runs = python_gate(tmp_path, "print('WHOLE-LOG-MARKER'); sys.exit(1)")
    orch = session(tmp_path, gate, [edit("one", run_gate=True)], [edit("two")], max_rounds=3)
    orch.run()

    digest = kinds(orch, "turn_done")[0]["digest"]
    rel = ".duet/sessions/%s/gate-%s.log" % (orch.session_id, digest[:8])
    assert orch._read_gate_log(rel).count("WHOLE-LOG-MARKER") == 1
    assert orch._read_gate_log(str(orch.workspace.root / rel)) is not None
    # Only this session's gate logs: nothing else under .duet/, nothing elsewhere.
    assert orch._read_gate_log("gate-%s.log" % digest[:8]) is None
    assert orch._read_gate_log(".duet/sessions/%s/events.jsonl" % orch.session_id) is None
