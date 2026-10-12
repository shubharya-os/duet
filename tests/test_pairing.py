"""Any two agents, in either order."""

import pytest

from duet.config import DEFAULT_PAIR, default_agents, parse_pair


def spec(pair):
    return [(a.name, a.backend, a.model) for a in parse_pair(pair)]


def test_the_default_pair_is_two_subscription_logins():
    assert [a.backend for a in default_agents()] == ["claude-code", "codex-cli"]
    assert DEFAULT_PAIR == "claude+codex"


def test_claude_leads_chatgpt_reviews():
    assert spec("claude+codex") == [
        ("claude", "claude-code", ""),
        ("chatgpt", "codex-cli", ""),
    ]


def test_the_order_flips_who_leads():
    """The whole 'vice versa' case is one word in one flag."""
    assert [a.name for a in parse_pair("codex+claude")] == ["chatgpt", "claude"]


def test_chatgpt_can_be_reached_by_key_instead_of_login():
    assert spec("claude+gpt")[1] == ("chatgpt", "openai-api", "")
    assert spec("claude+openai")[1][1] == "openai-api"


@pytest.mark.parametrize("alias,backend", [
    ("claude", "claude-code"), ("cc", "claude-code"), ("claude-code", "claude-code"),
    ("codex", "codex-cli"), ("chatgpt", "codex-cli"), ("codex-cli", "codex-cli"),
    ("gpt", "openai-api"), ("openai", "openai-api"), ("openai-api", "openai-api"),
])
def test_every_alias_resolves(alias, backend):
    assert parse_pair("%s+mock" % alias)[0].backend == backend


def test_two_of_the_same_agent_are_told_apart():
    """Same model twice is a real setup — a second opinion from the same family
    still catches things — but the transcript has to name them distinctly."""
    names = [a.name for a in parse_pair("claude+claude")]
    assert names == ["claude-1", "claude-2"]
    assert len(set(names)) == 2


def test_models_can_be_pinned_per_side():
    assert spec("claude:opus+claude:sonnet") == [
        ("claude-opus", "claude-code", "opus"),
        ("claude-sonnet", "claude-code", "sonnet"),
    ]


def test_a_model_can_be_pinned_on_one_side_only():
    """The name stays short when nothing collides — the model is recorded in the
    agent's description and printed at session start, so round lines stay readable."""
    assert spec("claude:opus+codex") == [
        ("claude", "claude-code", "opus"),
        ("chatgpt", "codex-cli", ""),
    ]


@pytest.mark.parametrize("bad", ["claude", "claude+codex+gpt", "", "claude+nope", "nope+claude"])
def test_a_bad_pair_explains_itself(bad):
    with pytest.raises(ValueError) as exc:
        parse_pair(bad)
    assert "claude" in str(exc.value).lower()      # the message lists what works


def test_the_pair_order_decides_who_moves_first():
    from duet.config import Config

    cfg = Config(agents=parse_pair("codex+claude"))
    assert cfg.order()[0] == "chatgpt"
    cfg = Config(agents=parse_pair("claude+codex"))
    assert cfg.order()[0] == "claude"


def test_two_of_the_same_model_are_still_two_agents():
    """`claude:sonnet+claude:sonnet` named both of them claude-sonnet.

    Found by running one. The suffix was the model, which distinguishes
    nothing when both sides ask for the same model, and the collision is not
    cosmetic: the orchestrator keys adapters by name, so a single adapter
    served both turns — the "reviewer" was the same session that wrote the
    code, context intact. Sign-offs are keyed by name too, so the pair could
    hold only one and `len(signoffs) < len(agents)` was permanently true: the
    session spent its whole round budget and could never reach consensus.
    """
    from duet.config import parse_pair

    for spec in ("claude:sonnet+claude:sonnet", "claude:opus+claude:opus",
                 "codex+codex", "claude+claude", "claude:opus+claude:sonnet",
                 "claude+codex"):
        names = [a.name for a in parse_pair(spec)]
        assert len(set(names)) == 2, "%s collapsed to %s" % (spec, names)

    # and the model is kept in the name when it is there to keep
    assert [a.name for a in parse_pair("claude:sonnet+claude:sonnet")] == [
        "claude-sonnet-1", "claude-sonnet-2"]


def test_a_same_model_pair_can_actually_reach_consensus(tmp_path):
    """The collision made consensus unreachable, so prove it is reachable."""
    from duet.adapters.mock import MockAdapter, envelope
    from duet.config import Config, parse_pair
    from duet.orchestrator import Orchestrator, STATUS_CONSENSUS

    agents = parse_pair("claude:sonnet+claude:sonnet")
    done = envelope("I would ship this", "DONE", confidence=0.9)
    cfg = Config(task="t", root=str(tmp_path), agents=agents,
                 start=agents[0].name, max_rounds=6)
    orch = Orchestrator(cfg, adapters={
        a.name: MockAdapter(name=a.name, cwd=str(tmp_path), config={"script": [done, done]})
        for a in agents
    })
    # Two adapters, not one collapsed into the other.
    assert len(orch.adapters) == 2
    assert orch.run().status == STATUS_CONSENSUS


def test_a_config_saved_with_colliding_names_is_repaired_on_load(tmp_path):
    """The parse_pair fix does not reach a config already written to disk.

    `duet init --pair claude:sonnet+claude:sonnet` persisted both agents under
    one name, and a config is read straight back into AgentSpecs — so the
    collapse would survive the fix for anyone who had already run it.
    """
    import json
    from duet.config import load_config

    (tmp_path / ".duet").mkdir()
    (tmp_path / ".duet" / "config.json").write_text(json.dumps({"agents": [
        {"name": "claude-sonnet", "backend": "claude-code", "model": "sonnet"},
        {"name": "claude-sonnet", "backend": "claude-code", "model": "sonnet"},
    ]}), encoding="utf-8")

    names = load_config(str(tmp_path)).agent_names
    assert names == ["claude-sonnet-1", "claude-sonnet-2"]
    # not claude-sonnet-sonnet-1: the stored name already carries the model
    assert "sonnet-sonnet" not in "".join(names)
