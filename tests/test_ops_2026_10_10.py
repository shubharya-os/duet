"""Three things that made running duet harder than it should be on 2026-10-10.

1. A session could not change its pair: a ChatGPT side out of Codex quota
   meant hand-editing state.json to carry the argument on with another one.
2. `duet login gpt` could not sign in the default ChatGPT side (it is named
   "chatgpt"), and `duet login chatgpt` was refused by the argument parser.
3. A Chrome that was only slow under load was reported as a setup fault.
"""
import argparse
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from duet import chrome
from duet.adapters.mock import envelope
from duet.cli import build_parser, main
from duet.config import AgentSpec, Config, default_agents, save_config
from duet.orchestrator import STATUS_CONSENSUS, Orchestrator

PASSING_GATE = "%s -c pass" % sys.executable

WRITE = envelope("first pass", "CONTINUE",
                 patches=[{"path": "app.py", "content": "print('hi')\n"}])
OBJECT = envelope("that is wrong", "CONTINUE",
                  issues=[{"id": "needs-tests", "title": "no tests", "severity": "major",
                           "detail": "add a test that runs the CLI"}])
DONE = envelope("I would ship this", "DONE", confidence=0.9)
USAGE_LIMIT = ("ERROR: You've hit your usage limit. Upgrade to Plus to continue "
               "using Codex (https://openai.com/chatgpt/pricing), or try again at "
               "Oct 18th, 2099 11:18 PM.")


def state_of(session_dir):
    return json.loads((Path(session_dir) / "state.json").read_text(encoding="utf-8"))


def newest_session(tmp_path):
    return sorted((tmp_path / ".duet" / "sessions").iterdir())[-1]


# -- 1. duet resume --pair ---------------------------------------------------
def _dead_session(tmp_path, gpt_options, max_rounds=2):
    cfg = Config(
        task="build the thing", acceptance="it works", root=str(tmp_path), gate=PASSING_GATE,
        agents=[AgentSpec("claude", "mock", options={"script": [WRITE, WRITE]}),
                AgentSpec("chatgpt", "mock", options=gpt_options)],
        start="claude", max_rounds=max_rounds,
        workflow_state={"baseline_dir": "kept-baseline", "reproduced": True},
    )
    orch = Orchestrator(cfg)
    result = orch.run()
    assert result.status != STATUS_CONSENSUS
    return orch, result


def test_resume_can_swap_the_pair_and_keeps_the_argument(tmp_path, capsys):
    dead, _ = _dead_session(tmp_path, {"script": [OBJECT]})
    before = state_of(dead.session_dir)
    assert [i["raised_by"] for i in before["state"]["issues"]] == ["chatgpt"]
    assert set(before["adapters"]) == {"claude", "chatgpt"}

    code = main(["resume", "-C", str(tmp_path), dead.session_id, "--rounds", "6",
                 "--pair", "mock:left+mock:right"])
    out = capsys.readouterr().out
    assert "needs-tests" in out                         # the objection came along
    assert "round 3 of 6" in out                        # and the rounds already taken

    saved = state_of(newest_session(tmp_path))
    assert saved["resumed_from"] == dead.session_id
    assert [a["name"] for a in saved["config"]["agents"]] == ["mock-left", "mock-right"]
    assert sorted(saved["state"]["agents"]) == ["mock-left", "mock-right"]
    # Renamed by position, consistently: the issue chatgpt raised is now the
    # right-hand side's, so it is the left-hand side's homework, not its own.
    issue = {i["id"]: i for i in saved["state"]["issues"]}["needs-tests"]
    assert issue["raised_by"] == "mock-right"
    assert issue["opened_round"] == 2
    assert saved["config"]["start"] == "mock-left"
    # The workflow state and its baseline are the session's, not the pair's.
    assert saved["config"]["workflow_state"]["baseline_dir"] == "kept-baseline"
    assert saved["config"]["workflow_state"]["reproduced"] is True
    # Nothing still answers to the old names.
    assert "chatgpt" not in json.dumps(saved["state"])
    assert code in (0, 1)
    # And the dead session's record is untouched.
    assert state_of(dead.session_dir) == before


def test_a_replaced_side_does_not_resume_the_old_agents_thread(tmp_path, capsys):
    dead, _ = _dead_session(tmp_path, {"script": [OBJECT]})
    before = state_of(dead.session_dir)
    assert before["adapters"]["chatgpt"]["turns"] == 1

    # Left side unchanged (same backend, same model), right side replaced.
    # A "round" here is one agent's turn, not a pair of them, so --rounds 4
    # is the minimum that lets both renamed sides take a turn after the dead
    # session's 2 (one each) — --rounds 3 would only give the first mover
    # (mock) a turn and never reach mock-other at all.
    main(["resume", "-C", str(tmp_path), dead.session_id, "--rounds", "4",
          "--pair", "mock+mock:other", "--json"])
    capsys.readouterr()
    resumed = state_of(newest_session(tmp_path))
    names = [a["name"] for a in resumed["config"]["agents"]]
    assert names[1] == "mock-other"
    # The replaced side started its own thread: its count is only the turns it
    # took in this session. The unchanged side carried its thread on.
    def took(name):
        return sum(1 for line in resumed["history"][2:] if line.split()[1] == name)

    assert took("mock-other") >= 1
    assert resumed["adapters"]["mock-other"]["turns"] == took("mock-other")
    assert (resumed["adapters"]["mock"]["turns"]
            == before["adapters"]["claude"]["turns"] + took("mock"))


def test_a_signoff_is_kept_under_the_new_name(tmp_path, capsys):
    dead, _ = _dead_session(tmp_path, {"script": [DONE]})
    assert set(dead.state.signoffs) == {"chatgpt"}

    # Both scriptless sides vote DONE on the same digest at once, so this
    # reaches consensus in the first resumed round (round 3).
    main(["resume", "-C", str(tmp_path), dead.session_id, "--rounds", "3",
          "--pair", "mock:left+mock:right", "--json"])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    data = json.loads(lines[0])
    assert data["resumed"] is True
    assert data["signoffs"] == ["mock-right"]


def test_a_quota_failure_names_the_resume_command_that_swaps_the_pair(tmp_path):
    dead, result = _dead_session(tmp_path, {"error": USAGE_LIMIT}, max_rounds=4)
    expected = "duet resume %s --pair claude:opus+claude:sonnet" % dead.session_id
    assert "failed twice" in result.reason and expected in result.reason
    assert all(expected in turn.error for turn in dead.turns if turn.error)
    # The command it names refers to a session that really holds the state.
    assert state_of(dead.session_dir)["session_id"] == dead.session_id


def test_a_failure_that_is_not_quota_does_not_suggest_swapping_the_pair(tmp_path):
    _, result = _dead_session(tmp_path, {"error": "codex: not signed in"}, max_rounds=4)
    assert "failed twice" in result.reason and "--pair" not in result.reason


# -- 2. duet login <side> ----------------------------------------------------
def test_login_accepts_every_name_for_the_chatgpt_side():
    parser = build_parser()
    for side in ("claude", "gpt", "chatgpt", "codex"):
        assert parser.parse_args(["login", side]).agent == side


def _fake_bin(tmp_path, name, body):
    path = tmp_path / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.mark.parametrize("side", ["gpt", "chatgpt", "codex"])
def test_login_signs_in_just_the_default_chatgpt_side(tmp_path, monkeypatch, capsys, side):
    from duet import cli
    from duet.adapters import claude_code, codex_cli

    cfg = Config(root=str(tmp_path), agents=default_agents())
    assert [a.name for a in cfg.agents] == ["claude", "chatgpt"]
    save_config(cfg, str(tmp_path))
    codex = _fake_bin(tmp_path, "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: codex))
    monkeypatch.setattr(codex_cli, "clear_usage_limit", lambda *a, **k: None)

    def no_claude(*a, **k):
        raise AssertionError("signing in the ChatGPT side touched the Claude one")

    monkeypatch.setattr(claude_code.ClaudeCodeAdapter, "probe", classmethod(no_claude))
    ran = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: ran.append(argv) or 0)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)

    args = argparse.Namespace(root=str(tmp_path), agent=side, force=True)
    assert cli.cmd_login(args) == 0
    out = capsys.readouterr().out
    assert "no agent named" not in out
    assert len(ran) == 1 and ran[0][-1] == "login"


# -- 3. a slow Chrome is not a broken one ------------------------------------
def _fake_chrome(tmp_path, delay):
    """A browser that speaks the pipe protocol, answering after `delay`."""
    path = tmp_path / "slow-chrome"
    path.write_text(
        "#!%s\n"
        "import json, os, time\n"
        "buf = b''\n"
        "while True:\n"
        "    chunk = os.read(3, 65536)\n"
        "    if not chunk:\n"
        "        break\n"
        "    buf += chunk\n"
        "    while b'\\0' in buf:\n"
        "        raw, _, buf = buf.partition(b'\\0')\n"
        "        msg = json.loads(raw)\n"
        "        time.sleep(%r)\n"
        "        os.write(4, json.dumps({'id': msg['id'], 'result': {'product': 'Slow/1'}}).encode() + b'\\0')\n"
        % (sys.executable, delay)
    )
    path.chmod(0o755)
    return str(path)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="the pipe protocol needs fds 3 and 4")
def test_a_chrome_slower_than_its_allowance_gets_a_second_longer_wait(tmp_path, monkeypatch):
    # The answer comes 1.5s after the question is read, so the first 1s
    # allowance always runs out; the second is generous, because this runs on
    # loaded machines too and the test is about the retry, not the clock.
    monkeypatch.setattr(chrome, "START_TIMEOUT", 1.0)
    monkeypatch.setattr(chrome, "SLOW_RETRY_FACTOR", 30.0)
    browser = chrome.Browser(chrome=_fake_chrome(tmp_path, 1.5), timeout=1.0)
    try:
        assert browser.version == "Slow/1"
    finally:
        browser.close()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="the pipe protocol needs fds 3 and 4")
def test_a_chrome_that_never_starts_is_still_a_setup_fault(tmp_path, monkeypatch, capsys):
    dead = tmp_path / "dead-chrome"
    dead.write_text("#!/bin/sh\nexit 1\n")
    dead.chmod(0o755)
    monkeypatch.setattr(chrome, "START_TIMEOUT", 0.6)
    with pytest.raises(chrome.SetupError):
        chrome.Browser(chrome=str(dead), timeout=0.6)

    page = tmp_path / "index.html"
    page.write_text("<!doctype html><title>x</title><p>hi</p>")
    monkeypatch.setenv("DUET_CHROME", str(dead))
    assert main(["page", "check", str(page)]) == 3
