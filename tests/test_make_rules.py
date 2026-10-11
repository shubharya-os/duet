"""The rules `duet make` hands to duet's own sessions.

Kept apart from test_make.py, and importing nothing from duet.make, so they
exercise the orchestrator and the plan workflow directly: a session whose
workflow_state carries a clean room may not reach consensus while the sweep is
dirty, and a plan must name every must-have. The reference is invented:
"Bookly", at bookly.example.
"""

from pathlib import Path

from duet.adapters.mock import MockAdapter, envelope
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator, STATUS_CONSENSUS
from duet.workflows import Plan

DONE = envelope("I would ship this", "DONE", confidence=0.9)
CLEAN_ROOM = {"avoid": ["Bookly"], "domains": ["bookly.example"], "colors": ["#ff3366"]}


class Cleaner(MockAdapter):
    """Renames the leftover identifier on its second turn."""

    def send(self, prompt, system="", round_no=0):
        if self.turns == 1:
            (Path(self.cwd) / "app.py").write_text("shelf = 1\n")
        return super().send(prompt, system, round_no)


def config(root, rounds):
    return Config(task="build a reading list", root=str(root),
                  agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                  start="claude", max_rounds=rounds,
                  workflow_state={"clean_room": dict(CLEAN_ROOM)})


def test_consensus_is_vetoed_while_the_sweep_is_dirty(tmp_path):
    (tmp_path / "app.py").write_text("booklyShelf = 1\n")
    adapters = {
        "claude": Cleaner(name="claude", cwd=str(tmp_path), config={"script": [DONE]}),
        "gpt": MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": [DONE]}),
    }
    events = []
    result = Orchestrator(config(tmp_path, 8), reporter=events.append, adapters=adapters).run()
    vetoes = [e for e in events if e.get("kind") == "workflow_veto"]
    assert len(vetoes) == 1 and vetoes[0]["workflow"] == "clean-room"
    assert "app.py:1" in vetoes[0]["reason"]
    # once the name was gone, the next pair of sign-offs counted
    assert result.status == STATUS_CONSENSUS and result.rounds > 2


def test_a_dirty_sweep_never_reaches_consensus(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "theme.css").write_text("a { color: #F36; }\n")
    adapters = {n: MockAdapter(name=n, cwd=str(tmp_path), config={"script": [DONE]})
                for n in ("claude", "gpt")}
    result = Orchestrator(config(tmp_path, 4), adapters=adapters).run()
    assert result.status != STATUS_CONSENSUS


def test_a_clean_product_reaches_consensus_as_before(tmp_path):
    (tmp_path / "app.py").write_text("shelf = 1\n")
    adapters = {n: MockAdapter(name=n, cwd=str(tmp_path), config={"script": [DONE]})
                for n in ("claude", "gpt")}
    result = Orchestrator(config(tmp_path, 4), adapters=adapters).run()
    assert result.status == STATUS_CONSENSUS and result.rounds == 2


def test_the_plan_workflow_refuses_a_plan_missing_a_must_have(tmp_path):
    state = {"must_cover": ["shared shelf", "reading goals"]}
    plan = Plan()
    plan.begin(str(tmp_path), state)
    (tmp_path / "PLAN.md").write_text("1. shared shelf\n")
    veto = plan.veto(str(tmp_path), state)
    assert veto is not None and "reading goals" in veto
    (tmp_path / "PLAN.md").write_text("1. shared shelf\n2. Reading goals\n")
    assert plan.veto(str(tmp_path), state) is None
