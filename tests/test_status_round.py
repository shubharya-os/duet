"""`duet status` names the round under way, and what is happening in it.

The round used to come from finished turns only, so a live session spent its
whole first turn — and the gate run on it — reading "round 0" with every agent
at "no turn yet". These event logs are the ones a real session writes at each
point; status reads them through `main`, the way someone checking on it would.
"""

import json
import os
import re
import time

from duet.cli import main

SESSION = "20261115-100000-live"


def start_event(t):
    return {"kind": "session_start", "t": t, "task": "fix the thing", "max_rounds": 14,
            "pid": os.getpid(), "agents": {"claude": "Claude Code", "chatgpt": "ChatGPT"},
            "turn_timeout": {"claude": 3600, "chatgpt": 3600}, "gate_timeout": 2400}


def first_turn(t):
    return [start_event(t),
            {"kind": "turn_start", "t": t + 1, "round": 1, "agent": "claude",
             "role": "lead", "backend": "claude-code"}]


def gate_on_first_turn(t):
    return first_turn(t) + [
        {"kind": "refused_writes", "t": t + 2, "round": 1, "agent": "claude", "calls": []},
        {"kind": "gate_start", "t": t + 3, "command": "pytest -q", "digest": "abc"}]


def second_turn(t):
    return gate_on_first_turn(t) + [
        {"kind": "gate_done", "t": t + 4, "ok": True, "exit_code": 0, "digest": "abc"},
        {"kind": "turn_done", "t": t + 5, "round": 1, "agent": "claude", "verdict": "CONTINUE",
         "message": "first pass", "issues": [], "resolves": [], "gate_ok": True},
        {"kind": "turn_start", "t": t + 6, "round": 2, "agent": "chatgpt",
         "role": "lead", "backend": "codex"}]


def final_gate(t):
    return second_turn(t) + [
        {"kind": "turn_done", "t": t + 7, "round": 2, "agent": "chatgpt", "verdict": "DONE",
         "message": "ship it", "issues": [], "resolves": [], "gate_ok": True},
        {"kind": "final_gate_start", "t": t + 8, "command": "pytest", "digest": "def"}]


def write(tmp_path, events):
    session = tmp_path / ".duet" / "sessions" / SESSION
    session.mkdir(parents=True)
    (session / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def status(tmp_path, capsys):
    assert main(["status", SESSION, "-C", str(tmp_path), "--quiet"]) == 0
    text = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    main(["status", SESSION, "-C", str(tmp_path), "--json"])
    return text, json.loads(capsys.readouterr().out)


def agent_line(text, agent):
    return next(line for line in text.splitlines() if line.strip().startswith(agent + " "))


def test_the_first_turn_in_progress_is_round_1(tmp_path, capsys):
    write(tmp_path, first_turn(time.time() - 60))
    text, data = status(tmp_path, capsys)
    assert "in round 1 now" in agent_line(text, "claude")
    assert "no turn yet" in agent_line(text, "chatgpt")
    assert "round 1 of 14" in text and "waiting on claude, thinking" in text
    assert data["running"] is True
    assert data["rounds"] == 0                    # nothing has finished yet
    assert data["round"] == 1 and data["doing"] == "turn"
    assert data["waiting_on"] == "claude"


def test_the_gate_running_after_the_first_turn_is_still_round_1(tmp_path, capsys):
    # The session from the bug: turn_start, refused_writes, gate_start, and
    # status said "round 0 of 14" and "claude no turn yet".
    write(tmp_path, gate_on_first_turn(time.time() - 60))
    text, data = status(tmp_path, capsys)
    assert "round 0" not in text
    assert "round 1 of 14" in text
    assert "no turn yet" not in agent_line(text, "claude")
    assert "in round 1 now" in agent_line(text, "claude")
    assert "the gate is running on claude's round-1 turn" in text
    assert data["round"] == 1 and data["doing"] == "gate"
    assert data["waiting_on"] is None             # it is the gate being waited on


def test_the_second_turn_in_progress_is_round_2(tmp_path, capsys):
    write(tmp_path, second_turn(time.time() - 60))
    text, data = status(tmp_path, capsys)
    assert "round 2 of 14" in text and "waiting on chatgpt, thinking" in text
    assert "in round 2 now" in agent_line(text, "chatgpt")
    claude = agent_line(text, "claude")
    assert "CONTINUE" in claude and "round 1" in claude and "now" not in claude
    assert data["rounds"] == 1
    assert data["round"] == 2 and data["doing"] == "turn"
    assert data["waiting_on"] == "chatgpt"


def test_the_final_gate_is_named_with_the_round_it_follows(tmp_path, capsys):
    write(tmp_path, final_gate(time.time() - 60))
    text, data = status(tmp_path, capsys)
    assert data["round"] == 2 and data["doing"] == "final_gate"
    assert data["waiting_on"] is None
    assert "round 2 of 14" in text and "the final gate is running" in text
    assert "now" not in agent_line(text, "claude") and "now" not in agent_line(text, "chatgpt")


def test_a_finished_session_reports_no_activity(tmp_path, capsys):
    t = time.time() - 60
    write(tmp_path, second_turn(t) + [
        {"kind": "session_end", "t": t + 9, "status": "max_rounds", "reason": "out of rounds"}])
    main(["status", SESSION, "-C", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["doing"] is None and data["waiting_on"] is None
