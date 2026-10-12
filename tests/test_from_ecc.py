"""`duet from-ecc`: what you actually used from ECC, counted without lying.

Every trap below is one the hand-run version of this audit fell into on the
machine it was built on: it reported one agent "4x" (really once), counted a
resumed session twice, missed a second agent entirely, and listed two slash
commands that were only a grep mentioning them.
"""

import argparse
import json

from duet import from_ecc


def log(home, project_dir, name, entries):
    folder = home / ".claude" / "projects" / project_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def agent_call(call_id, agent, cwd="/Users/me/code/Gallery-repo"):
    return {"type": "assistant", "cwd": cwd, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": call_id, "name": "Agent", "input": {"subagent_type": agent}}]}}


def test_only_real_invocations_count_and_each_counts_once(tmp_path):
    call = agent_call("toolu_1", "ecc:swift-reviewer")
    noise = [
        # every session lists ECC's agents in its context: a mention, not a use
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "available agents: ecc:swift-reviewer, ecc:go-reviewer"}]}},
        # a grep for ECC names inside a Bash call: a mention, not a use
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash",
             "input": {"command": "grep '<command-name>/ecc:plan' *.jsonl"}}]}},
        # a tool result quoting one: role user, but not something the user typed
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_9",
             "content": "<command-name>/ecc:plan</command-name>"}]}},
    ]
    log(tmp_path, "-Users-me-code-Gallery-repo", "a.jsonl", [call] + noise)
    # a resumed session copies history into a new file: same call, same id
    log(tmp_path, "-Users-me-code-Gallery-repo", "b.jsonl", [call])
    log(tmp_path, "-Users-me-code-Gallery-repo", "c.jsonl",
        [agent_call("toolu_2", "ecc:csharp-reviewer")])

    usage, sessions = from_ecc.audit(tmp_path)
    assert sessions == 3
    assert usage == {("agent", "swift-reviewer"): {"Gallery-repo": 1},
                     ("agent", "csharp-reviewer"): {"Gallery-repo": 1}}


def test_a_slash_command_the_user_typed_does_count(tmp_path):
    log(tmp_path, "-p", "a.jsonl", [{"type": "user", "cwd": "/w/proj", "uuid": "u1", "message": {
        "role": "user", "content": "<command-name>/ecc:plan</command-name> the thing"}}])
    usage, _ = from_ecc.audit(tmp_path)
    assert usage == {("command", "plan"): {"proj": 1}}


def test_the_project_name_comes_from_the_real_path_not_the_folder(tmp_path):
    # "-Users-me-code-Gallery-repo" cannot tell "Gallery-repo" from "Gallery/repo"
    log(tmp_path, "-Users-me-code-Gallery-repo", "a.jsonl",
        [agent_call("t", "ecc:x", cwd="/Users/me/code/Gallery-repo")])
    usage, _ = from_ecc.audit(tmp_path)
    assert list(usage[("agent", "x")]) == ["Gallery-repo"]


def fake_cache(home):
    cache = home / ".claude" / "plugins" / "cache" / "ecc" / "ecc" / "2.2.2"
    (cache / "agents").mkdir(parents=True)
    (cache / "skills" / "go-patterns").mkdir(parents=True)
    (cache / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026 Someone\n", encoding="utf-8")
    (cache / "agents" / "swift-reviewer.md").write_text(
        "---\nname: swift-reviewer\ndescription: reviews swift\n---\n\nBody.\n", encoding="utf-8")
    (cache / "skills" / "go-patterns" / "SKILL.md").write_text("---\nname: go-patterns\n---\nx\n", encoding="utf-8")
    return cache


def test_keep_copies_an_agent_with_its_licence_and_never_overwrites(tmp_path):
    fake_cache(tmp_path)
    ok, _ = from_ecc.keep(tmp_path, "swift-reviewer")
    kept = (tmp_path / ".claude" / "agents" / "swift-reviewer.md").read_text(encoding="utf-8")
    assert ok and kept.startswith("---\nname: swift-reviewer")      # front matter still first
    assert "MIT License, Copyright (c) 2026 Someone" in kept and "Body." in kept
    ok, message = from_ecc.keep(tmp_path, "swift-reviewer")
    assert not ok and "not overwriting" in message


def test_keep_copies_a_skill_directory(tmp_path):
    fake_cache(tmp_path)
    ok, _ = from_ecc.keep(tmp_path, "go-patterns")
    assert ok and (tmp_path / ".claude" / "skills" / "go-patterns" / "SKILL.md").is_file()
    assert "MIT" in (tmp_path / ".claude" / "skills" / "go-patterns" / "KEPT_FROM_ECC.md").read_text(encoding="utf-8")


def test_keep_says_so_when_there_is_nothing_to_keep(tmp_path):
    ok, message = from_ecc.keep(tmp_path, "anything")
    assert not ok and "plugin cache" in message
    fake_cache(tmp_path)
    ok, message = from_ecc.keep(tmp_path, "no-such-thing")
    assert not ok and "no ECC agent or skill" in message


def test_the_report_never_uninstalls_and_lists_the_steps(tmp_path, capsys):
    fake_cache(tmp_path)
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"ecc@ecc": [{}]}}), encoding="utf-8")
    log(tmp_path, "-p", "a.jsonl", [agent_call("t1", "ecc:swift-reviewer", cwd="/w/app")])
    assert from_ecc.run(argparse.Namespace(dir=str(tmp_path), keep=None)) == 0
    out = capsys.readouterr().out
    assert "duet from-ecc --keep swift-reviewer" in out
    assert "claude plugin uninstall ecc@ecc" in out and "you run this" in out
    assert (tmp_path / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8").count("ecc@ecc") == 1
