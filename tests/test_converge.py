"""Agreement converges: once one side has signed, the other agrees without editing.

Measured on real sessions, a quarter of all turns in the ones that agreed came
after the first DONE, because each DONE arrived with a small edit — so the two
signatures were never on the same files and the peer had to look again. These
run the real orchestrator against scripted peers.
"""

import json
from pathlib import Path

from duet.adapters.mock import MockAdapter, envelope
from duet.cli import make_reporter
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator

BUILD = envelope("built it, I would ship it", "DONE",
                 patches=[{"path": "app.py", "content": "print('hi')\n"}])
DONE = envelope("I would ship this", "DONE")


def session(tmp_path, claude, gpt, reporter=None, max_rounds=10):
    cfg = Config(task="build the thing", root=str(tmp_path),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=max_rounds)
    adapters = {
        "claude": claude if isinstance(claude, MockAdapter)
        else MockAdapter(name="claude", cwd=str(tmp_path), config={"script": claude}),
        "gpt": gpt if isinstance(gpt, MockAdapter)
        else MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": gpt}),
    }
    orch = Orchestrator(cfg, adapters=adapters, reporter=reporter)
    return orch, orch.run()


def events(result, kind):
    path = Path(result.session_dir) / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("kind") == kind]


def first_done_round(orch):
    return min(t.round for t in orch.turns if t.envelope.verdict == "DONE")


class Polisher(MockAdapter):
    """An agent that always finds one more thing to tidy.

    It does what its prompt tells it: told plainly that its peer has signed and
    that polish goes in as a minor issue rather than an edit, it raises the
    polish and agrees. Not told, it does what agents did in the measured
    sessions — votes DONE and makes the edit anyway.
    """

    def send(self, prompt, system="", round_no=0):
        self.prompts.append(prompt)
        self.systems.append(system)
        self.turns += 1
        if self.turns == 1 and self.name == "claude":
            text = BUILD
        elif "WITHOUT CHANGING ANY FILE" in prompt:
            text = envelope("Holds up. One nicety, raised rather than edited.", "DONE",
                            issues=[{"id": "polish-%s" % self.name, "title": "tidy the greeting",
                                     "severity": "minor", "detail": "say hello, not hi"}])
        else:
            text = envelope("Holds up; tidied the greeting while I was there.", "DONE",
                            patches=[{"path": "app.py",
                                      "content": "print('hello %s %d')\n" % (self.name, self.turns)}])
        from duet.adapters.base import AgentReply
        return AgentReply(text=text, meta={"backend": "mock", "turn": self.turns})


def test_a_polishing_pair_agrees_in_the_turn_after_the_first_done(tmp_path):
    claude = Polisher(name="claude", cwd=str(tmp_path), config={})
    gpt = Polisher(name="gpt", cwd=str(tmp_path), config={})
    orch, result = session(tmp_path, claude, gpt, max_rounds=8)
    assert result.status == "consensus", result.reason
    assert result.rounds == first_done_round(orch) + 1
    # The polish was not lost: it is open, and the report lists it to do later.
    assert orch.state.issues["polish-gpt"].status == "open"
    report = Path(result.report).read_text()
    assert "## Left for later" in report
    later = report.split("## Left for later", 1)[1]
    assert "polish-gpt" in later and "say hello, not hi" in later


def test_the_prompt_after_a_peers_done_states_the_rule(tmp_path):
    orch, result = session(tmp_path, [BUILD], [DONE])
    prompt = orch.adapters["gpt"].prompts[0]
    assert "voted DONE on exactly this workspace state" in prompt
    assert "WITHOUT CHANGING ANY FILE" in prompt
    assert "only to fix something you would block on" in prompt
    assert "minor issue" in prompt and "restarts the" in prompt
    # The opener had no signed peer, so it got the ordinary turn.
    assert "WITHOUT CHANGING ANY FILE" not in orch.adapters["claude"].prompts[0]


def test_a_done_with_an_edit_tells_both_agents_what_restarted(tmp_path, capsys):
    polish = envelope("Agreed, and tidied the greeting.", "DONE",
                      patches=[{"path": "app.py", "content": "print('hello')\n"},
                               {"path": "NOTES.md", "content": "notes\n"}])
    reporter = make_reporter(["claude", "gpt"])
    orch, result = session(tmp_path, [BUILD, DONE], [polish, DONE], reporter=reporter)
    assert result.status == "consensus" and result.rounds == 3

    # The signer's next prompt names the edit and narrows the re-check to it.
    notice = orch.adapters["claude"].prompts[1]
    assert "AN EDIT RESTARTED THE AGREEMENT" in notice
    section = notice.split("AN EDIT RESTARTED THE AGREEMENT", 1)[1]
    assert "- NOTES.md" in section and "- app.py" in section
    assert "Re-check only that change" in section
    # ...and the editor is owed the same notice for its next prompt.
    owed = orch.pending_restarted["gpt"]
    assert "NOTES.md" in owed and "app.py" in owed and "instead of editing" in owed

    assert events(result, "agreement_restarted")[0]["files"] == ["NOTES.md", "app.py"]
    assert any("RESTARTED agreement" in line for line in orch.history)
    shown = capsys.readouterr().out
    assert "agreement restarted" in shown and "NOTES.md, app.py" in shown


def test_an_unchanged_done_or_a_first_done_is_not_a_restart(tmp_path):
    orch, result = session(tmp_path, [BUILD], [DONE])
    assert result.status == "consensus" and result.rounds == 2
    assert not events(result, "agreement_restarted")
    assert not any("RESTARTED" in p for a in orch.adapters.values() for p in a.prompts)


def test_a_blocking_objection_with_an_edit_still_reopens_the_session(tmp_path):
    objection = envelope("The greeting crashes on empty input; fixed it, please check.", "DONE",
                         issues=[{"id": "greeting-crash", "title": "greeting crashes",
                                  "severity": "major", "detail": "use print() correctly"}],
                         patches=[{"path": "app.py", "content": "print('hi')  # fixed\n"}])
    claim = envelope("Checked your fix, it holds.", "CONTINUE", resolves=["greeting-crash"])
    orch, result = session(tmp_path, [BUILD, claim, DONE], [objection, DONE])
    # DONE alongside a major issue is downgraded, so this is an objection, not
    # a restarted agreement — and the session goes on until it is settled.
    assert orch.turns[1].envelope.verdict == "CONTINUE"
    assert not events(result, "agreement_restarted")
    assert result.status == "consensus" and result.rounds > 2
    assert orch.state.issues["greeting-crash"].status == "resolved"
    assert "## Left for later" not in Path(result.report).read_text()


def test_the_owed_notice_survives_a_resume(tmp_path):
    polish = envelope("Agreed, and tidied.", "DONE",
                      patches=[{"path": "app.py", "content": "print('hello')\n"}])
    orch, result = session(tmp_path, [BUILD], [polish], max_rounds=2)
    assert result.status == "exhausted"
    saved = json.loads((Path(result.session_dir) / "state.json").read_text())
    assert "app.py" in saved["pending_restarted"]["claude"]

    cfg = Config(task="build the thing", root=str(tmp_path),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")], start="claude")
    again = Orchestrator(cfg, adapters={
        n: MockAdapter(name=n, cwd=str(tmp_path), config={"script": [DONE]}) for n in ("claude", "gpt")
    })
    again.restore(saved)
    assert "app.py" in again.pending_restarted["claude"]
