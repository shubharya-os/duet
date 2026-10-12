"""Calls the permission mode refused: seen by the harness, not left to the peer to find."""

import argparse

from duet.adapters.base import AgentReply
from duet.adapters.claude_code import ClaudeCodeAdapter
from duet.adapters.mock import MockAdapter, envelope
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator, refused_writes

DONE = envelope("I would ship this", "DONE", confidence=0.9)


def test_the_cli_s_own_denial_list_is_read_as_tool_and_target():
    denials = [
        {"tool_name": "Bash", "tool_use_id": "t1",
         "tool_input": {"command": "pytest  -q\ntests/", "description": "run"}},
        {"tool_name": "Edit", "tool_use_id": "t2", "tool_input": {"file_path": "PLAN.md"}},
        {"tool_name": "WebFetch", "tool_use_id": "t3", "tool_input": {}},
        "not a dict",
    ]
    assert ClaudeCodeAdapter._refused(denials) == ["Bash: pytest -q tests/", "Edit: PLAN.md", "WebFetch"]
    assert ClaudeCodeAdapter._refused(None) == []


def test_only_calls_that_would_have_changed_a_file_count_as_refused_writes():
    writes = refused_writes([
        "Edit: PLAN.md",
        "Write: notes.md",
        "Bash: cat > PLAN.md <<'EOF'",
        "Bash: sed -i 's/a/b/' app.py",
        "Bash: python3 -c \"open('x','w').write('1')\"",
    ])
    assert len(writes) == 5
    # Running things, and redirects that write nowhere, are not edits.
    assert refused_writes([
        "Bash: pytest -q 2>&1",
        "Bash: python3 -c 'print(6*7)' > /dev/null",
        "Bash: sqlite3 --version",
        "WebFetch",
    ]) == []


class Refuses(MockAdapter):
    """First reply claims a fix whose Edit was refused; the correction is honest."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def send(self, prompt, system="", round_no=0):
        self.calls += 1
        if self.calls == 1:
            self.prompts.append(prompt)
            return AgentReply(text=envelope("fixed the plan", "DONE", confidence=0.9),
                              meta={"backend": "mock", "cost_usd": 0.5,
                                    "refused": ["Edit: PLAN.md"]})
        reply = super().send(prompt, system, round_no)
        reply.meta["cost_usd"] = 0.25
        return reply


def session(tmp_path, claude, gpt, **kwargs):
    cfg = Config(task="t", root=str(tmp_path), start="claude", max_rounds=kwargs.pop("max_rounds", 4),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")], **kwargs)
    orch = Orchestrator(cfg, adapters={"claude": claude, "gpt": gpt})
    return orch, orch.run()


def test_a_refused_edit_is_put_back_to_the_agent_before_its_peer_sees_the_claim(tmp_path):
    honest = envelope("my edit was refused and PLAN.md is unchanged; nothing is fixed yet",
                      "CONTINUE")
    claude = Refuses(name="claude", cwd=str(tmp_path), config={"script": [honest, DONE, DONE]})
    gpt = MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": [DONE, DONE]})
    orch, _ = session(tmp_path, claude, gpt)

    correction = claude.prompts[1]
    assert "Edit: PLAN.md" in correction and "byte-identical" in correction
    # the turn on record is the corrected one, and the peer never saw "fixed"
    first = orch.turns[0]
    assert first.envelope.verdict == "CONTINUE" and "refused" in first.envelope.message
    assert "fixed the plan" not in gpt.prompts[0]
    assert first.meta["cost_usd"] == 0.75                  # both calls are paid for
    assert any(e == "refused_writes" for e in _kinds(orch))


def test_a_refused_test_run_reaches_the_peer_as_unverified_without_an_extra_turn(tmp_path):
    class RefusedRun(MockAdapter):
        def send(self, prompt, system="", round_no=0):
            reply = super().send(prompt, system, round_no)
            if self.turns == 1:
                reply.meta["refused"] = ["Bash: pytest -q"]
            return reply

    claude = RefusedRun(name="claude", cwd=str(tmp_path),
                        config={"script": [envelope("tests pass", "DONE", confidence=0.9)]})
    gpt = MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": [DONE]})
    orch, _ = session(tmp_path, claude, gpt, max_rounds=2)

    assert claude.turns == 1                               # no correction for a run
    assert "PERMISSION MODE REFUSED" in gpt.prompts[0] and "Bash: pytest -q" in gpt.prompts[0]
    assert "1 call(s) refused" in orch.history[0]


def test_a_plan_session_may_run_the_project_s_tests_though_it_has_no_gate(tmp_path, monkeypatch):
    from duet import cli, workflow_commands

    (tmp_path / "test_app.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(cli.gate_detect, "detect", lambda root: "pytest -q")
    monkeypatch.setattr(cli, "cmd_run", lambda args: seen.update(vars(args)) or 0)
    args = argparse.Namespace(what=["move storage to Postgres"], root=str(tmp_path), gate=None,
                              no_gate=False, json=False, allow_nested=True)
    assert workflow_commands.run(args, "plan") == 0
    assert seen["gate"] is None and seen["allow_run"] == "pytest -q"
    assert "`pytest -q` is allowed" in seen["task"][0]

    granted = []

    class Grants(MockAdapter):
        def allow_gate(self, gate):
            granted.append(gate)

    cfg = Config(task="t", root=str(tmp_path), allow_run="pytest -q",
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")])
    Orchestrator(cfg, adapters={"claude": Grants(name="claude", cwd=str(tmp_path)),
                                "gpt": Grants(name="gpt", cwd=str(tmp_path))})
    assert granted == ["pytest -q", "pytest -q"]


def _kinds(orch):
    import json
    path = orch.session_dir / "events.jsonl"
    return [json.loads(l)["kind"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# -- duet plan --quick ------------------------------------------------------

def quick(tmp_path, lead, reviewer):
    from duet.orchestrator import Orchestrator as O
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    cfg = Config(task="t", root=str(tmp_path), start="claude", max_rounds=2, workflow="plan",
                 workflow_state={"quick": True},
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")])
    orch = O(cfg, adapters={
        "claude": MockAdapter(name="claude", cwd=str(tmp_path), config={"script": [lead]}),
        "gpt": MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": [reviewer]}),
    })
    return orch.run()


DRAFT = envelope("the plan", "DONE", confidence=0.9,
                 patches=[{"path": "PLAN.md", "content": "1. do it\n"}])


def test_quick_plan_approved_after_a_reviewer_edit_is_reviewed_not_signed_off(tmp_path):
    edited = envelope("tightened step 1", "DONE", confidence=0.9,
                      patches=[{"path": "PLAN.md", "content": "1. do it, then check it\n"}])
    result = quick(tmp_path, DRAFT, edited)
    assert result.status == "reviewed"
    assert "not re-checked by claude" in result.reason


def test_quick_plan_the_reviewer_leaves_alone_is_a_real_double_sign_off(tmp_path):
    assert quick(tmp_path, DRAFT, DONE).status == "consensus"


def test_quick_plan_the_reviewer_rejects_is_not_reported_as_reviewed(tmp_path):
    no = envelope("step 1 is wrong", "CONTINUE",
                  issues=[{"id": "bad-step", "title": "wrong", "severity": "major", "detail": "redo"}])
    result = quick(tmp_path, DRAFT, no)
    assert result.status == "exhausted" and "did not approve" in result.reason


def test_quick_plan_is_still_held_to_the_plan_rule(tmp_path):
    touched = envelope("also fixed the code", "DONE", confidence=0.9,
                       patches=[{"path": "app.py", "content": "x = 2\n"}])
    result = quick(tmp_path, DRAFT, touched)
    assert result.status == "exhausted" and "app.py" in result.reason


def test_quick_is_two_turns_and_says_so(tmp_path, monkeypatch):
    from duet import cli, workflow_commands

    seen = {}
    monkeypatch.setattr(cli.gate_detect, "detect", lambda root: "")
    monkeypatch.setattr(cli, "cmd_run", lambda args: seen.update(vars(args)) or 0)
    parser = cli.build_parser()
    args = parser.parse_args(["plan", "--quick", "-C", str(tmp_path), "--allow-nested", "move it"])
    assert workflow_commands.run(args, "plan") == 0
    assert seen["rounds"] == 2 and "exactly two turns" in seen["task"][0]
    assert cli.build_config(args).workflow_state.get("quick") is True


def test_running_the_tests_in_a_plan_session_does_not_break_the_plan_rule(tmp_path):
    """Found live: the plan rule counted .pytest_cache as a changed file, so the
    first quick plan whose agents ran the suite was refused for running it."""
    run = envelope("ran the tests; plan written", "DONE", confidence=0.9,
                   patches=[{"path": ".pytest_cache/v/cache/nodeids", "content": "[]\n"}])
    assert quick(tmp_path, DRAFT, run).status == "consensus"
