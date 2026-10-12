"""`duet resume`: carry on an interrupted session instead of starting over.

The interrupted sessions here are real orchestrator runs cut short by their own
round budget, so what `resume` reads back is a state file the loop actually
wrote — the issue ledger, the sign-offs and the round history included.
"""

import json
import sys
from pathlib import Path

import pytest

from duet.adapters.mock import MockAdapter, envelope
from duet.cli import main
from duet.config import AgentSpec, Config
from duet.orchestrator import STATUS_CONSENSUS, Orchestrator

PASSING_GATE = "%s -c pass" % sys.executable
FAILING_GATE = "%s -c \"raise SystemExit(1)\"" % sys.executable

WRITE = envelope("first pass", "CONTINUE",
                 patches=[{"path": "app.py", "content": "print('hi')\n"}])
OBJECT = envelope("that is wrong", "CONTINUE",
                  issues=[{"id": "needs-tests", "title": "no tests", "severity": "major",
                           "detail": "add a test that runs the CLI"}])
FIX = envelope("added tests", "CONTINUE", resolves=["needs-tests"],
               patches=[{"path": "test_app.py", "content": "def test_ok():\n    assert True\n"}])
DONE = envelope("I would ship this", "DONE", confidence=0.9)
BLOCKED = envelope("I cannot get at the database", "BLOCKED")


def interrupted(tmp_path, claude_script, gpt_script, max_rounds=2, gate=PASSING_GATE,
                session_id=None, task="build the thing"):
    """Run a real session that stops before agreeing, and return it.

    The scripts travel in each agent's options so that they survive into
    state.json — `resume` builds its adapters from the recorded config, exactly
    as it would build two real CLIs.
    """
    cfg = Config(
        task=task,
        acceptance="it works",
        root=str(tmp_path),
        gate=gate,
        agents=[AgentSpec("claude", "mock", options={"script": claude_script}),
                AgentSpec("gpt", "mock", options={"script": gpt_script})],
        start="claude",
        max_rounds=max_rounds,
    )
    orch = Orchestrator(cfg, session_id=session_id)
    result = orch.run()
    assert result.status != STATUS_CONSENSUS
    return orch, result


def resume(tmp_path, *extra):
    return main(["resume", "-C", str(tmp_path), *extra])


def resume_json(tmp_path, capsys, *extra):
    code = resume(tmp_path, "--json", *extra)
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l.startswith("{")]
    return code, json.loads(lines[0])


def state_of(session_dir):
    return json.loads((Path(session_dir) / "state.json").read_text(encoding="utf-8"))


def newest_session(tmp_path):
    return sorted((tmp_path / ".duet" / "sessions").iterdir())[-1]


# -- the thing it exists for -----------------------------------------------
def test_a_resumed_session_keeps_the_argument_and_finishes_it(tmp_path, capsys):
    dead, _ = interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])
    assert [i.id for i in dead.state.open_issues()] == ["needs-tests"]

    assert resume(tmp_path, "--rounds", "6") == 0
    out = capsys.readouterr().out
    assert "needs-tests" in out                 # the open objection was carried in
    assert "round 3 of 6" in out                # and it starts after the rounds already taken

    fresh = newest_session(tmp_path)
    assert fresh.name != dead.session_id
    saved = state_of(fresh)
    assert saved["resumed_from"] == dead.session_id
    assert saved["rounds"] == 5                 # 3, 4 and 5 — the counter continued

    # The objection was argued out rather than forgotten: raised before the
    # interruption, fixed and verified after it.
    issues = {i["id"]: i for i in saved["state"]["issues"]}
    assert issues["needs-tests"]["status"] == "resolved"
    assert issues["needs-tests"]["opened_round"] == 2
    assert "verified fixed by gpt in round 4" in issues["needs-tests"]["resolution"]

    # Both halves of the history are in one timeline, in order.
    rounds = [line.split()[0] for line in saved["history"]]
    assert rounds == ["R1", "R2", "R3", "R4", "R5"]

    # And the dead session's own record is left exactly as it was.
    assert state_of(Path(dead.session_dir))["rounds"] == 2
    assert "Round 3" not in (Path(dead.session_dir) / "transcript.md").read_text(encoding="utf-8")


def test_it_does_not_re_run_the_turns_that_already_happened(tmp_path, capsys):
    """Each agent picks its script up where it left off, not at the start."""
    dead, _ = interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])
    assert resume(tmp_path, "--rounds", "6") == 0

    transcript = (Path(newest_session(tmp_path)) / "transcript.md").read_text(encoding="utf-8")
    assert "Round 1" not in transcript and "Round 2" not in transcript
    assert "Round 3 — claude" in transcript
    assert "added tests" in transcript          # claude's *second* scripted reply
    assert "first pass" not in transcript       # not its first, again
    assert "resumed from" in transcript.lower()


def test_a_signoff_survives_the_interruption(tmp_path, capsys):
    # gpt signs off in round 2; claude never does, so the session ends without
    # consensus with one sign-off on the books.
    dead, _ = interrupted(tmp_path, [WRITE, DONE], [DONE])
    assert set(dead.state.signoffs) == {"gpt"}

    code, data = resume_json(tmp_path, capsys, "--rounds", "4")
    assert data["resumed"] is True
    assert data["signoffs"] == ["gpt"]          # carried across, not re-earned
    assert data["from_round"] == 3
    assert data["session"] == dead.session_id
    assert code == 0


def test_open_issues_and_their_age_survive(tmp_path, capsys):
    """An objection resumes with its age, so arbitration is not reset either."""
    stubborn = envelope("still wrong", "CONTINUE",
                        issues=[{"id": "needs-tests", "title": "no tests",
                                 "severity": "blocker", "detail": "add a test"}])
    dead, _ = interrupted(tmp_path, [WRITE, WRITE], [stubborn], max_rounds=2)
    aged = dead.state.issues["needs-tests"].rounds_open
    assert aged == 1

    code, data = resume_json(tmp_path, capsys, "--rounds", "3")
    assert data["open_issues"] == ["needs-tests"]
    resumed = state_of(newest_session(tmp_path))
    assert resumed["state"]["issues"][0]["rounds_open"] > aged


# -- the four refusals, each naming its fix --------------------------------
def test_it_refuses_a_session_that_already_agreed(tmp_path, capsys):
    cfg = Config(task="t", root=str(tmp_path), gate=PASSING_GATE, max_rounds=6, start="claude",
                 agents=[AgentSpec("claude", "mock", options={"script": [WRITE, DONE]}),
                         AgentSpec("gpt", "mock", options={"script": [DONE]})])
    result = Orchestrator(cfg).run()
    assert result.status == STATUS_CONSENSUS

    assert resume(tmp_path) == 2
    out = capsys.readouterr().out
    assert "already reached consensus" in out
    assert "duet verify" in out and "duet run" in out      # the fix, not just the problem


def test_it_refuses_a_session_that_is_not_there(tmp_path, capsys):
    interrupted(tmp_path, [WRITE], [OBJECT])
    assert resume(tmp_path, "no-such-session") == 2
    out = capsys.readouterr().out
    assert "no session 'no-such-session'" in out
    assert "duet sessions" in out


def test_it_refuses_when_there_are_no_rounds_left(tmp_path, capsys):
    dead, _ = interrupted(tmp_path, [WRITE, FIX], [OBJECT])
    assert resume(tmp_path) == 2
    out = capsys.readouterr().out
    assert "no rounds left" in out
    assert "--rounds 6" in out                  # used 2, so the fix names a bigger budget
    # and taking that advice works
    assert resume(tmp_path, "--rounds", "6") != 2


def test_it_refuses_a_half_written_state_file_instead_of_guessing(tmp_path, capsys):
    """A truncated state.json must not start a 20-minute session on an empty brief."""
    dead, _ = interrupted(tmp_path, [WRITE], [OBJECT])
    saved = Path(dead.session_dir) / "state.json"

    saved.write_text("{not json at all", encoding="utf-8")
    assert resume(tmp_path, dead.session_id, "--rounds", "6") == 2
    assert "saved no state.json" in capsys.readouterr().out

    saved.write_text("{}", encoding="utf-8")
    assert resume(tmp_path, dead.session_id, "--rounds", "6") == 2
    out = capsys.readouterr().out
    assert "recorded no task" in out
    assert "duet run" in out


def test_it_refuses_an_argument_between_agents_this_pair_does_not_contain(tmp_path, capsys):
    """The ledger attributes every objection to a name.

    Resuming under different names would hand one agent the other's objections,
    which is worse than not resuming at all.
    """
    dead, _ = interrupted(tmp_path, [WRITE], [OBJECT])
    saved = Path(dead.session_dir) / "state.json"
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["state"]["agents"] = ["claude", "someone-else"]
    saved.write_text(json.dumps(data), encoding="utf-8")

    assert resume(tmp_path, dead.session_id, "--rounds", "6") == 2
    out = capsys.readouterr().out
    assert "someone-else" in out and "would not be in the room" in out
    assert "duet run" in out


def test_a_budget_smaller_than_the_rounds_already_used_explains_itself(tmp_path, capsys):
    """`--rounds` reads as "extra rounds" to about half the people who type it."""
    dead, _ = interrupted(tmp_path, [WRITE, FIX], [OBJECT], max_rounds=2)
    assert resume(tmp_path, "--rounds", "1") == 2
    out = capsys.readouterr().out
    assert "has taken 2, and the budget is 1" in out
    assert "new total, not rounds on top" in out
    assert "--rounds 6" in out


def test_it_refuses_a_session_that_saved_no_state(tmp_path, capsys):
    crashed = tmp_path / ".duet" / "sessions" / "99999999-235959-dead"
    crashed.mkdir(parents=True)
    (crashed / "events.jsonl").write_text("{}\n", encoding="utf-8")

    assert resume(tmp_path, "99999999-235959-dead") == 2
    assert "saved no state.json" in capsys.readouterr().out

    # With no id it is the same refusal, and it says how many it looked at.
    assert resume(tmp_path) == 2
    out = capsys.readouterr().out
    assert "no session in" in out and "99999999-235959-dead" in out


def test_it_refuses_with_no_sessions_at_all(tmp_path, capsys):
    assert resume(tmp_path) == 2
    out = capsys.readouterr().out
    assert "no sessions yet" in out
    assert "duet run" in out


def test_it_refuses_from_inside_a_session(tmp_path, capsys, monkeypatch):
    interrupted(tmp_path, [WRITE], [OBJECT])
    monkeypatch.setenv("DUET_SESSION", "20250101-000000-aaaa")
    assert resume(tmp_path, "--rounds", "6") == 4
    out = capsys.readouterr().out
    assert "refusing to start a duet session from inside one" in out
    assert "--allow-nested" in out


def test_a_refusal_is_machine_readable_too(tmp_path, capsys):
    code, data = resume_json(tmp_path, capsys)
    assert code == 2
    assert data["ok"] is False and data["resumed"] is False
    assert "no sessions yet" in data["error"]
    assert data["fix"]


# -- the flags -------------------------------------------------------------
def test_gate_override_is_used_for_the_resumed_rounds(tmp_path, capsys):
    """--gate replaces the recorded one, and a red gate blocks agreement."""
    interrupted(tmp_path, [WRITE, DONE], [DONE])
    assert resume(tmp_path, "--rounds", "4", "--gate", FAILING_GATE) == 1
    assert "gate failed" in capsys.readouterr().out

    saved = state_of(newest_session(tmp_path))
    assert saved["config"]["gate"] == FAILING_GATE
    assert saved["result"]["status"] != STATUS_CONSENSUS


def test_rounds_is_a_new_total_not_an_extra_allowance(tmp_path, capsys):
    dead, _ = interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE], max_rounds=2)
    assert resume(tmp_path, "--rounds", "3") == 1     # one more round only: 3
    saved = state_of(newest_session(tmp_path))
    assert saved["rounds"] == 3
    assert saved["config"]["max_rounds"] == 3


def test_a_named_session_can_be_resumed_past_a_newer_one(tmp_path, capsys):
    older, _ = interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])
    crashed = tmp_path / ".duet" / "sessions" / "99999999-235959-dead"
    crashed.mkdir(parents=True)
    (crashed / "events.jsonl").write_text("{}\n", encoding="utf-8")

    code, data = resume_json(tmp_path, capsys, older.session_id, "--rounds", "6")
    assert code == 0 and data["session"] == older.session_id


def test_a_dead_session_with_no_state_is_skipped_when_no_id_is_given(tmp_path, capsys):
    older, _ = interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])
    crashed = tmp_path / ".duet" / "sessions" / "99999999-235959-dead"
    crashed.mkdir(parents=True)
    (crashed / "events.jsonl").write_text("{}\n", encoding="utf-8")

    code, data = resume_json(tmp_path, capsys, "--rounds", "6")
    assert code == 0
    assert data["session"] == older.session_id
    assert data["skipped_sessions"] == ["99999999-235959-dead"]


def test_a_completed_newer_session_is_skipped_when_no_id_is_given(tmp_path, capsys):
    older, _ = interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE])

    cfg = Config(task="done", root=str(tmp_path), gate=PASSING_GATE, max_rounds=6, start="claude",
                 agents=[AgentSpec("claude", "mock", options={"script": [DONE]}),
                         AgentSpec("gpt", "mock", options={"script": [DONE]})])
    completed = Orchestrator(cfg, session_id="99999999-235959-done")
    result = completed.run()
    assert result.status == STATUS_CONSENSUS

    code, data = resume_json(tmp_path, capsys, "--rounds", "6")
    assert code == 0
    assert data["session"] == older.session_id
    assert data["skipped_not_resumable"] == [completed.session_id]


def test_a_newer_session_that_only_ran_out_of_rounds_is_not_stepped_over(tmp_path, capsys):
    """Out of rounds is not finished — it is the session you meant, one flag short.

    Walking past it to an older argument would answer a question nobody asked,
    and would swallow the one message that gets the user what they wanted.
    """
    interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE],
                session_id="11111111-000000-old", task="the older argument")
    newer, _ = interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE],
                           session_id="99999999-235959-spent", task="the one that just died")

    assert resume(tmp_path) == 2
    out = capsys.readouterr().out
    assert newer.session_id in out and "no rounds left" in out
    assert "--rounds 6" in out
    assert "11111111-000000-old" not in out          # and it did not quietly take the older one

    # Taking the advice resumes the session the user actually meant.
    code, data = resume_json(tmp_path, capsys, "--rounds", "6")
    assert code == 0 and data["session"] == newer.session_id


def test_it_says_which_argument_it_is_carrying_on(tmp_path, capsys):
    """An id is not something anyone recognises, and with no id this can step
    back past newer sessions. Resuming the wrong one costs a whole session."""
    interrupted(tmp_path, [WRITE, FIX, DONE], [OBJECT, DONE],
                task="add retry with backoff to the fetch client")
    assert resume(tmp_path, "--rounds", "6") == 0
    assert "add retry with backoff to the fetch client" in capsys.readouterr().out


def test_a_directive_owed_to_an_agent_survives(tmp_path):
    """Something the harness owes an agent but has not said yet is argument state.

    Both directives matter, and the stall one most: detecting a stall also zeroes
    the counter, so losing the directive erases the finding too and the pair
    grinds out another whole stall window before noticing again.
    """
    prose = "I rewrote the loader. It looks right to me."      # no envelope at all
    dead, _ = interrupted(tmp_path, [prose], [OBJECT], max_rounds=2)

    saved = state_of(Path(dead.session_dir))
    assert "no JSON envelope" in saved["pending_directive"]["claude"]

    # Carried into the resumed session, and spent on that agent's next turn.
    adapters = {name: MockAdapter(name=name, cwd=str(tmp_path), config={"script": [DONE]})
                for name in ("claude", "gpt")}
    cfg = Config.from_dict(saved["config"])
    cfg.root = str(tmp_path)
    cfg.max_rounds = 3
    resumed = Orchestrator(cfg, adapters=adapters)
    resumed.restore(saved)
    assert resumed.pending_directive["claude"]

    resumed.run()
    assert "no JSON envelope" in adapters["claude"].prompts[0]
    assert not resumed.pending_directive                        # and not repeated after


# -- the awkward case ------------------------------------------------------
def test_a_stale_blocked_verdict_does_not_end_the_resumed_session_at_once(tmp_path, capsys):
    """BLOCKED is why that session stopped; resuming is the answer to it.

    Left in place it would be read as a live position and stop the resumed
    session after a single turn, before its author had spoken again.
    """
    dead, result = interrupted(tmp_path, [WRITE, DONE, DONE], [BLOCKED, DONE], max_rounds=4)
    assert result.status == "blocked"
    assert dead.state.last_verdict["gpt"] == "BLOCKED"

    code, data = resume_json(tmp_path, capsys, "--rounds", "6")
    assert code == 0
    assert any("BLOCKED" in note for note in data["notes"])
    assert state_of(newest_session(tmp_path))["rounds"] > data["from_round"]


def test_resuming_a_resumed_session_chains(tmp_path, capsys):
    first, _ = interrupted(tmp_path, [WRITE, WRITE, FIX, DONE], [OBJECT, OBJECT, DONE])
    assert resume(tmp_path, "--rounds", "4") == 1      # still no agreement at round 4
    middle = newest_session(tmp_path)

    assert resume(tmp_path, "--rounds", "8") == 0
    last = state_of(newest_session(tmp_path))
    assert last["resumed_from"] == middle.name
    assert [line.split()[0] for line in last["history"]][:2] == ["R1", "R2"]


# --- findings from an independent review of this feature --------------------

def _interruptible_session(tmp_path, fail_on_round):
    """A session whose agent raises KeyboardInterrupt mid-turn, like Ctrl-C."""
    from duet.adapters.mock import MockAdapter, envelope
    from duet.config import AgentSpec, Config
    from duet.orchestrator import Orchestrator

    keep_going = envelope("still working", "CONTINUE")

    class Interrupts(MockAdapter):
        def send(self, prompt, system="", round_no=0):
            if round_no == fail_on_round:
                raise KeyboardInterrupt
            return super().send(prompt, system, round_no)

    cfg = Config(task="t", root=str(tmp_path), max_rounds=8,
                 agents=[AgentSpec("a", "mock"), AgentSpec("b", "mock")])
    orch = Orchestrator(cfg, adapters={
        "a": Interrupts(name="a", cwd=str(tmp_path), config={"script": [keep_going]}),
        "b": MockAdapter(name="b", cwd=str(tmp_path), config={"script": [keep_going]}),
    })
    return orch, orch.run()


def test_a_round_interrupted_mid_turn_is_not_counted_as_taken(tmp_path):
    """Ctrl-C is the headline reason to resume. Counting the interrupted round
    as finished made resume start one round late, hand the turn to the other
    agent, and drop the directive this one was owed."""
    orch, result = _interruptible_session(tmp_path, fail_on_round=3)
    assert result.status == "interrupted"
    assert orch.round_no == 2, "only rounds that produced a turn should count"

    state = json.loads((Path(result.session_dir) / "state.json").read_text(encoding="utf-8"))
    assert state["rounds"] == 2

    from duet.orchestrator import rounds_taken
    assert rounds_taken(state) == 2          # so resume starts at round 3, with agent 'a'


def test_an_interrupted_turn_keeps_the_directive_it_was_owed(tmp_path):
    from duet.adapters.mock import MockAdapter, envelope
    from duet.config import AgentSpec, Config
    from duet.orchestrator import Orchestrator

    class Interrupts(MockAdapter):
        def send(self, prompt, system="", round_no=0):
            if round_no == 2:
                raise KeyboardInterrupt
            return super().send(prompt, system, round_no)

    cfg = Config(task="t", root=str(tmp_path), max_rounds=6,
                 agents=[AgentSpec("a", "mock"), AgentSpec("b", "mock")])
    orch = Orchestrator(cfg, adapters={
        "a": MockAdapter(name="a", cwd=str(tmp_path),
                         config={"script": [envelope("go on", "CONTINUE")]}),
        "b": Interrupts(name="b", cwd=str(tmp_path),
                        config={"script": [envelope("go on", "CONTINUE")]}),
    })
    orch.pending_directive["b"] = "break the stall"
    orch.run()
    assert orch.pending_directive.get("b") == "break the stall"

    saved = json.loads((Path(orch.session_dir) / "state.json").read_text(encoding="utf-8"))
    assert saved["pending_directive"]["b"] == "break the stall"


def test_files_an_agent_asked_to_see_survive_a_resume(tmp_path):
    """`reads` is owed to the agent that asked. Losing it means its next prompt
    silently omits the file and it spends a turn asking again."""
    from duet.adapters.mock import MockAdapter, envelope
    from duet.config import AgentSpec, Config
    from duet.orchestrator import Orchestrator

    cfg = Config(task="t", root=str(tmp_path), max_rounds=2,
                 agents=[AgentSpec("a", "mock"), AgentSpec("b", "mock")])
    orch = Orchestrator(cfg, adapters={
        n: MockAdapter(name=n, cwd=str(tmp_path), config={"script": [
            envelope("show me that file", "CONTINUE", reads=["duet/cli.py"])]})
        for n in ("a", "b")
    })
    orch.run()
    saved = json.loads((Path(orch.session_dir) / "state.json").read_text(encoding="utf-8"))
    assert saved["pending_reads"]["a"] == ["duet/cli.py"]

    fresh = Orchestrator(cfg, adapters={
        n: MockAdapter(name=n, cwd=str(tmp_path)) for n in ("a", "b")
    })
    fresh.restore(saved)
    assert fresh.pending_reads["a"] == ["duet/cli.py"]


@pytest.mark.parametrize("broken", [
    {"state": {"agents": ["a", "b"], "signoffs": {"a": {"unexpected": 1}}}},
    {"state": {"agents": ["a", "b"], "issues": ["not an object"]}},
    {"config": {"agents": ["not an object"]}},
])
def test_a_malformed_state_file_never_raises(tmp_path, broken, capsys):
    """This is the exact file class the command already anticipates. Whether it
    refuses or skips the unusable part, it must not reach the user as a
    traceback out of a command whose whole contract is to fail politely."""
    from duet.cli import main

    session = tmp_path / ".duet" / "sessions" / "20260101-000000-bbbb"
    session.mkdir(parents=True)
    payload = {"session_id": "20260101-000000-bbbb",
               "config": {"task": "t", "agents": [{"name": "a", "backend": "mock"},
                                                  {"name": "b", "backend": "mock"}],
                          "max_rounds": 8},
               "state": {"agents": ["a", "b"]}, "rounds": 1}
    payload.update(broken)
    session.joinpath("state.json").write_text(json.dumps(payload), encoding="utf-8")

    code = main(["resume", "20260101-000000-bbbb", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert isinstance(code, int)                  # returned, did not raise
    assert "traceback" not in out.lower()
    assert "AttributeError" not in out and "TypeError" not in out
