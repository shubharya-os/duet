"""Two things seen live on 2026-10-10.

1. A README whose "How to run it" section held a standard Markdown *indented*
   code block — four spaces, no fence — gave duet no run command. `duet jury`'s
   user juror reported "[no run command found ...]" and could not try the
   product; `duet build`'s summary would have shown no run line either.

2. On a heavily loaded machine the gate (two full test suites) outran the
   hard-coded 900-second limit. duet printed "gate failed (exit 124)" and
   "gate FAILING", and both agents were told the gate fails — so they went
   looking for a test failure that did not exist.

Nothing this fix introduced is imported at the top of this file. The harness
replays these tests against the code as it was before the session, and they
must fail there on what they assert — a missing command, a gate called
"FAILED", an unknown `--gate-timeout` — not on an ImportError for a new name.
"""

import json
import sys
from pathlib import Path

import pytest

from duet import build
from duet.adapters.mock import envelope
from duet.cli import build_parser, main, make_reporter
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator
from duet.workspace import Workspace

DONE = envelope("I would ship this", "DONE", confidence=0.9)
WRITE = envelope("first pass", "CONTINUE",
                 patches=[{"path": "app.py", "content": "print('hi')\n"}])
# Python, not `sleep 30` / `sleep 5; false`: the gate runs through cmd.exe on
# Windows, which has neither `sleep` nor `;`. Double quotes read the same in both.
SLOW_GATE = '"%s" -c "import time; time.sleep(30)"' % sys.executable
SLOW_THEN_FAILING_GATE = '"%s" -c "import sys, time; time.sleep(5); sys.exit(1)"' % sys.executable


# -- 1. an indented code block is a code block ------------------------------
def readme(tmp_path, text):
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    return build.run_command(str(tmp_path))


def test_an_indented_command_under_how_to_run_it_is_found(tmp_path):
    """The README from the live session, verbatim in shape."""
    assert readme(tmp_path, "# splitbill\n\nSplits a bill.\n\n## How to run it\n\n"
                            "    python3 splitbill.py 100 3\n") == "python3 splitbill.py 100 3"


def test_a_tab_indented_command_with_a_prompt_is_found(tmp_path):
    assert readme(tmp_path, "## How to run it\n\n\t$ python3 app.py --port 9000\n") \
        == "python3 app.py --port 9000"


def test_an_indented_block_may_open_with_a_comment(tmp_path):
    """`    # install` is a comment inside the block, not a heading that ends
    the section."""
    assert readme(tmp_path, "## How to run it\n\nFrom the project root:\n\n"
                            "    # start the server\n"
                            "    $ python3 run.py   # http://127.0.0.1:8000/\n") == "python3 run.py"


def test_prose_under_the_heading_is_still_not_a_command(tmp_path):
    assert readme(tmp_path, "## How to run it\n\nRun the script with python and "
                            "pass the total and the number of people.\n") == ""


def test_an_indented_line_continuing_a_paragraph_is_not_a_command(tmp_path):
    """Markdown: an indented block cannot interrupt a paragraph."""
    assert readme(tmp_path, "## How to run it\n\nRun the script with python\n"
                            "    and pass it the total.\n") == ""


def test_an_indented_line_continuing_a_list_item_is_not_a_command(tmp_path):
    assert readme(tmp_path, "## How to run it\n\n- Install Python 3,\n\n"
                            "    then open a terminal here.\n") == ""


def test_an_indented_block_in_another_section_is_not_the_run_command(tmp_path):
    assert readme(tmp_path, "## Install\n\n    pip install nothing\n\n"
                            "## How to run it\n\n    python3 app.py\n") == "python3 app.py"


def test_the_jury_user_can_now_find_it(tmp_path):
    """What the user juror actually calls."""
    from duet import jury

    (tmp_path / "README.md").write_text("## How to run it\n\n    python3 splitbill.py 100 3\n",
                                        encoding="utf-8")
    assert jury.run_command_for(str(tmp_path)) == "python3 splitbill.py 100 3"


# -- 2. a gate that ran out of time is not a failing test --------------------
def test_a_gate_stopped_at_its_limit_says_it_ran_out_of_time(tmp_path):
    result = Workspace(str(tmp_path), gate=SLOW_GATE, gate_timeout=1).run_gate()
    assert not result.ok                       # still not green
    shown = result.render()
    assert "ran out of time" in shown
    assert "not a test failure" in shown
    assert "FAILED" not in shown


def test_a_gate_that_really_fails_is_still_called_failing(tmp_path):
    result = Workspace(str(tmp_path), gate="exit 3", gate_timeout=60).run_gate()
    assert "FAILED (exit 3)" in result.render()
    assert "ran out of time" not in result.render()


def test_the_limit_is_shown_in_minutes(tmp_path):
    """"after N minutes" — not a bare count of seconds."""
    ws = Workspace(str(tmp_path), gate=SLOW_GATE, gate_timeout=1)
    result = ws.run_gate()
    result.timeout = 1800          # as if it had been the 30-minute limit
    assert "after 30 minutes" in result.render()


def test_the_user_is_told_it_ran_out_of_time(capsys):
    report = make_reporter(["claude", "gpt"])
    report({"kind": "gate_done", "ok": False, "exit_code": 124, "timed_out": True,
            "timeout": 900, "digest": "abc"})
    report({"kind": "turn_done", "round": 1, "agent": "claude", "verdict": "DONE",
            "digest": "abcdef1234", "gate_ok": False, "gate_timed_out": True})
    out = capsys.readouterr().out
    assert "ran out of time after 15 minutes" in out
    assert "not a test failure" in out
    assert "gate failed" not in out
    assert "FAILING" not in out


def session(tmp_path, **kwargs):
    cfg = Config(
        task="build the thing", root=str(tmp_path), start="claude",
        agents=[AgentSpec("claude", "mock", options={"script": [DONE]}),
                AgentSpec("gpt", "mock", options={"script": [DONE]})],
        **kwargs,
    )
    orch = Orchestrator(cfg)
    return orch, orch.run()


def test_the_agents_are_told_it_ran_out_of_time_not_that_it_fails(tmp_path):
    orch, result = session(tmp_path, gate=SLOW_GATE, gate_timeout=1, max_rounds=2)
    for name in ("claude", "gpt"):
        prompt = orch.adapters[name].prompts[-1]
        assert "ran out of time" in prompt
        assert "not a test failure" in prompt
        assert "the acceptance gate is failing" not in prompt
        assert "FAILED (exit 124)" not in prompt
    # Both signed off on the same state, and still it is not consensus: a gate
    # that never finished showed nothing passing.
    assert result.status != "consensus"


def test_a_replay_that_runs_out_of_time_is_not_a_test_result(tmp_path):
    """The `fix` rule's replay ran the gate with its own hard-coded 900 seconds.
    It now uses the session's limit, and running out of it proves nothing
    either way — it does not get to count as "the tests fail on the original"."""
    from duet import workflows

    (tmp_path / "test_app.py").write_text("def test_x():\n    assert True\n")
    state = {"gate": SLOW_THEN_FAILING_GATE, "gate_timeout": 1}
    workflows.snapshot(str(tmp_path), state)
    assert workflows.replay_fails(str(tmp_path), state) is None
    assert "ran out of time" in state.get("replay_note", "")


def test_refactor_does_not_call_a_slow_suite_failing(tmp_path, capsys):
    """`duet refactor` checks the suite is green before it starts, with the same
    hard-coded limit — and called a suite that ran out of time "failing"."""
    import argparse

    from duet import workflow_commands

    (tmp_path / ".duet").mkdir()
    (tmp_path / ".duet" / "config.json").write_text(json.dumps({"gate_timeout": 1}))
    args = argparse.Namespace(what=["split it"], root=str(tmp_path), gate=SLOW_THEN_FAILING_GATE,
                              no_gate=False, allow_nested=False, json=False, command="")
    assert workflow_commands.run(args, "refactor") == 3
    out = capsys.readouterr().out
    assert "ran out of time" in out
    assert "failing before the refactor" not in out


# -- 3. the limit is settable and saved -------------------------------------
def parse(*argv):
    return build_parser().parse_args(list(argv))


@pytest.mark.parametrize("command", ["run", "fix", "add", "refactor", "plan", "build", "resume"])
def test_every_session_command_takes_a_gate_timeout(command):
    args = parse(command, "something", "--gate-timeout", "40")
    assert args.gate_timeout == 40


def test_the_gate_timeout_reaches_the_config_in_seconds(tmp_path):
    from duet.cli import build_config

    args = parse("fix", "the bug", "-C", str(tmp_path), "--gate-timeout", "40", "--no-gate")
    assert build_config(args).gate_timeout == 2400


def test_a_gate_timeout_under_a_minute_is_refused(tmp_path):
    from duet.cli import build_config

    with pytest.raises(SystemExit):
        build_config(parse("run", "x", "-C", str(tmp_path), "--gate-timeout", "0", "--no-gate"))


def first_session(tmp_path, gate_timeout):
    cfg = Config(
        task="build the thing", root=str(tmp_path), gate_timeout=gate_timeout, max_rounds=1,
        agents=[AgentSpec("claude", "mock", options={"script": [WRITE]}),
                AgentSpec("gpt", "mock", options={"script": [WRITE]})],
        start="claude",
    )
    first = Orchestrator(cfg)
    first.run()
    return json.loads((Path(first.session_dir) / "state.json").read_text(encoding="utf-8"))


def newest_state(tmp_path):
    newest = sorted((tmp_path / ".duet" / "sessions").iterdir())[-1]
    return json.loads((newest / "state.json").read_text(encoding="utf-8"))


def test_the_gate_timeout_survives_a_resume(tmp_path):
    assert first_session(tmp_path, 2400)["config"]["gate_timeout"] == 2400
    assert main(["resume", "-C", str(tmp_path), "--rounds", "3"]) in (0, 1)
    assert newest_state(tmp_path)["config"]["gate_timeout"] == 2400


def test_resume_can_raise_the_gate_timeout(tmp_path):
    first_session(tmp_path, 900)
    assert main(["resume", "-C", str(tmp_path), "--rounds", "3",
                 "--gate-timeout", "45"]) in (0, 1)
    assert newest_state(tmp_path)["config"]["gate_timeout"] == 2700
