"""The scope check: a task that names files outside the workspace is refused
before round 1, with the command that would work, instead of both agents
finding out the slow way and ending BLOCKED."""
import json
import io
import subprocess
import sys
from pathlib import Path

import pytest

from duet import cli
from duet.cli import main

SESSION = ["--pair", "mock+mock", "--rounds", "1", "--no-gate", "--allow-nested"]


@pytest.fixture
def repo(tmp_path):
    """A git repository with `studio/` as the workspace and files beside it."""
    top = tmp_path / "shop"
    (top / "studio" / "src").mkdir(parents=True)
    (top / "studio" / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (top / "studio" / "README.md").write_text("studio\n", encoding="utf-8")
    (top / "deploy").mkdir()
    (top / "deploy" / ".env.example").write_text("KEY=\n", encoding="utf-8")
    (top / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(top)], check=True)
    return top.resolve()


def _sessions(workspace: Path):
    folder = workspace / ".duet" / "sessions"
    return [p for p in folder.iterdir() if p.is_dir()] if folder.is_dir() else []


def _session_start(workspace: Path) -> dict:
    [session] = _sessions(workspace)
    first = (session / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
    event = json.loads(first)
    assert event["kind"] == "session_start"
    return event


def _refused(code, out, workspace, top, mention):
    assert code == 3, out
    assert mention in out
    assert str(workspace) in out
    assert "-C %s" % top in out or '-C "%s"' % top in out, out
    assert not _sessions(workspace), "a session started although the task was refused"


def test_a_sibling_file_named_with_dotdot_is_refused(repo, capsys):
    studio = repo / "studio"
    code = main(["run", "fill in ../deploy/.env.example from the README",
                 "-C", str(studio)] + SESSION)
    out = capsys.readouterr().out
    _refused(code, out, studio, repo, "../deploy/.env.example")
    # The suggested command is this one with -C moved up, task and flags kept.
    assert "--allow-outside" in out
    assert "mock+mock" in out and "fill in ../deploy/.env.example" in out


def test_a_repo_path_that_exists_only_at_the_top_level_is_refused(repo, capsys):
    studio = repo / "studio"
    code = main(["run", "add a healthcheck to `deploy/docker-compose.yml`.",
                 "-C", str(studio)] + SESSION)
    out = capsys.readouterr().out
    _refused(code, out, studio, repo, "deploy/docker-compose.yml")


def test_a_mention_in_the_context_file_counts_too(repo, tmp_path, capsys):
    studio = repo / "studio"
    note = tmp_path / "handoff.md"
    note.write_text("We agreed the values live in ../deploy/.env.example.\n", encoding="utf-8")
    code = main(["run", "wire the config up", "-C", str(studio),
                 "--context-file", str(note)] + SESSION)
    out = capsys.readouterr().out
    _refused(code, out, studio, repo, "../deploy/.env.example")


def test_a_task_piped_on_stdin_is_checked_and_reused(repo, capsys, monkeypatch):
    studio = repo / "studio"
    monkeypatch.setattr(sys, "stdin", io.StringIO("fill in ../deploy/.env.example"))
    code = main(["run", "-", "-C", str(studio)] + SESSION)
    out = capsys.readouterr().out
    _refused(code, out, studio, repo, "../deploy/.env.example")


def test_a_piped_task_that_passes_the_check_still_reaches_the_session(repo, capsys, monkeypatch):
    """Read once by the check, and that same text is the session's task."""
    studio = repo / "studio"
    monkeypatch.setattr(sys, "stdin", io.StringIO("fix the bug in src/app.py"))
    main(["run", "-", "-C", str(studio)] + SESSION)
    capsys.readouterr()
    assert _session_start(studio)["task"] == "fix the bug in src/app.py"


def test_detach_does_not_read_stdin(repo, capsys, monkeypatch):
    """The detached child's stdin is DEVNULL; the parent must not block on it."""
    studio = repo / "studio"

    class Unreadable(io.StringIO):
        def read(self, *a):
            raise AssertionError("--detach read stdin")

        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", Unreadable())
    monkeypatch.setattr(cli, "cmd_detach", lambda argv, args: 0)
    assert main(["run", "-", "-C", str(studio), "--detach"] + SESSION) == 0


@pytest.mark.parametrize("task", [
    "fix the bug in src/app.py and update ./README.md",
    "read ../deploy/missing.txt and docs/nowhere.md if they exist",
    "follow https://example.com/deploy/docker-compose.yml and www.example.com/a/b",
    "make it work like node.js does, e.g. v1.2 of the API",
    "call /usr/bin/env python3 and /bin/sh",
    "leave .git/HEAD and ../.git/config alone",
])
def test_tasks_that_stay_inside_start_normally(repo, task, capsys):
    studio = repo / "studio"
    main(["run", task, "-C", str(studio)] + SESSION)
    out = capsys.readouterr().out
    assert "outside the workspace" not in out
    assert _session_start(studio)


def test_a_path_inside_the_workspace_wins_over_the_same_one_at_the_top(repo, capsys):
    studio = repo / "studio"
    (studio / "deploy").mkdir()
    (studio / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    main(["run", "edit deploy/docker-compose.yml", "-C", str(studio)] + SESSION)
    assert "outside the workspace" not in capsys.readouterr().out
    assert _session_start(studio)


def test_allow_outside_starts_and_is_recorded(repo, capsys):
    studio = repo / "studio"
    main(["run", "fill in ../deploy/.env.example", "-C", str(studio),
          "--allow-outside"] + SESSION)
    assert "outside the workspace" not in capsys.readouterr().out
    assert _session_start(studio).get("allow_outside") is True


def test_a_session_without_the_flag_does_not_record_it(repo, capsys):
    studio = repo / "studio"
    main(["run", "fix src/app.py", "-C", str(studio)] + SESSION)
    capsys.readouterr()
    assert "allow_outside" not in _session_start(studio)


def test_detach_refuses_before_detaching(repo, capsys, monkeypatch):
    studio = repo / "studio"

    def no_child(*a, **k):
        raise AssertionError("--detach started a child for a task it should have refused")

    monkeypatch.setattr(cli, "cmd_detach", no_child)
    code = main(["run", "fill in ../deploy/.env.example", "-C", str(studio),
                 "--detach"] + SESSION)
    out = capsys.readouterr().out
    _refused(code, out, studio, repo, "../deploy/.env.example")
    assert not (studio / ".duet").exists(), "--detach made a session log before refusing"


def test_workflow_commands_are_checked_before_their_gate(repo, capsys):
    """`duet refactor` runs the gate at once; the refusal comes before it."""
    studio = repo / "studio"
    marker = repo / "gate-ran"
    gate = "%s -c \"open(r'%s', 'w').close()\"" % (Path(sys.executable).as_posix(), marker)
    code = main(["refactor", "move ../deploy/docker-compose.yml into studio",
                 "-C", str(studio), "--gate", gate] + SESSION[:-2] + ["--allow-nested"])
    out = capsys.readouterr().out
    _refused(code, out, studio, repo, "../deploy/docker-compose.yml")
    assert not marker.exists(), "the gate ran before the scope check refused"
