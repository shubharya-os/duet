"""Two ways a review went wrong without anyone noticing, both seen live on 2026-10-10.

1. New files vanished from the review. `Workspace.diff` put a listing of
   untracked files — names only — after `git diff HEAD` and then trimmed the
   whole view at 12,000 characters, so in a feature whose main code is new
   files the listing was the first thing cut. The lead had to tell its reviewer
   that the two files it was reviewing existed.

2. One bad gate run blocked a session until someone edited a file. The gate
   result is cached per workspace state, failures included, so a test that
   timed out under load stayed red for the rest of the session unless an agent
   changed a file just to get the gate run again.

Nothing this fix introduced is imported at the top of this file: the harness
replays these tests against the code as it was before the session, and they
must fail there on what they assert, not on an ImportError for a new name.
"""

import json
import subprocess
import sys
from pathlib import Path

from duet.adapters.mock import MockAdapter, envelope
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator
from duet.workspace import Workspace

DONE = envelope("I would ship this", "DONE", confidence=0.9)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def repo(root, lines=3000):
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "big.py").write_text("".join("line_%d = %d\n" % (i, i) for i in range(lines)))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")


def edit_tracked(root):
    """A tracked change far bigger than the whole 12,000-character view."""
    (root / "big.py").write_text("".join("line_%d = %d  # edited\n" % (i, i) for i in range(3000)))


# -- 1. new files are always named, and their text is shown ------------------
def test_new_files_survive_a_diff_that_fills_the_budget(tmp_path):
    repo(tmp_path)
    edit_tracked(tmp_path)
    (tmp_path / "gate_cache.py").write_text("def recall():\n    return 'REMEMBERED-PASS'\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_gate_cache.py").write_text(
        "def test_recall():\n    assert recall() == 'REMEMBERED-PASS'  # NEW-TEST-BODY\n")

    view = Workspace(str(tmp_path)).diff()

    # Both new files are named, and named before anything that can be trimmed.
    assert "gate_cache.py" in view and "tests/test_gate_cache.py" in view
    assert view.index("tests/test_gate_cache.py") < view.index("diff --git")
    # Small new files are shown whole, not just named.
    assert "return 'REMEMBERED-PASS'" in view
    assert "NEW-TEST-BODY" in view
    # The view stays inside its budget.
    assert len(view) <= 12000
    # The tracked diff was cut, and the reviewer is told exactly which file to read.
    assert "NOT SHOWN IN FULL" in view
    not_shown = view.split("NOT SHOWN IN FULL", 1)[1].split("\n\n", 1)[0]
    assert "big.py" in not_shown
    assert "gate_cache.py" not in not_shown
    # And nothing was staged to get there.
    assert git(tmp_path, "diff", "--cached", "--name-only").stdout.strip() == ""


def test_the_diff_and_the_new_files_share_the_budget(tmp_path):
    """Neither side starves the other: a huge diff leaves room for a big new
    file, and a big new file is cut and named, not dropped."""
    repo(tmp_path)
    edit_tracked(tmp_path)
    (tmp_path / "feature.py").write_text(
        "FEATURE_START = 1\n" + "".join("def f%d():\n    return %d\n" % (i, i) for i in range(2000)))

    view = Workspace(str(tmp_path)).diff()

    assert "FEATURE_START = 1" in view           # the new file's text is there
    assert "-line_0 = 0\n" in view                # and so is the diff's
    assert len(view) <= 12000
    diff_part = view[view.index("diff --git"):view.index("--- NEW FILE feature.py")]
    file_part = view[view.index("--- NEW FILE feature.py"):]
    assert len(diff_part) > 4000 and len(file_part) > 4000   # fair shares, not scraps
    not_shown = view.split("NOT SHOWN IN FULL", 1)[1].split("\n\n", 1)[0]
    assert "feature.py" in not_shown and "big.py" in not_shown


def test_binary_and_very_large_new_files_are_listed_not_inlined(tmp_path):
    repo(tmp_path)
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0BINARY-MARKER")
    (tmp_path / "dump.json").write_text('{"x": "' + "LARGE-MARKER" * 30000 + '"}')
    (tmp_path / "small.py").write_text("SMALL = 1\n")

    view = Workspace(str(tmp_path)).diff()

    assert "logo.png" in view and "BINARY-MARKER" not in view
    assert "dump.json" in view and "LARGE-MARKER" not in view
    assert "SMALL = 1" in view
    not_shown = view.split("NOT SHOWN IN FULL", 1)[1].split("\n\n", 1)[0]
    assert "dump.json" in not_shown              # too large: read it yourself


def test_a_tight_view_shows_whole_pieces_and_names_the_rest(tmp_path):
    """Many new files and little room: no file is given a header and an empty
    body — what does not fit is named as not shown, and what is shown is real."""
    repo(tmp_path)
    edit_tracked(tmp_path)
    names = ["mod_%02d.py" % i for i in range(30)]
    for name in names:
        (tmp_path / name).write_text(("%s_VALUE = 1\n" % name[:-3]) * 30)

    view = Workspace(str(tmp_path)).diff(limit=4000)

    for name in names:
        assert name in view.split("\n\n", 1)[0]          # every one listed up front
    shown = [n for n in names if "--- NEW FILE %s ---" % n in view]
    assert shown, "some new file's text should fit"
    for name in shown:
        assert "%s_VALUE = 1" % name[:-3] in view
    assert "trimmed at 0 " not in view
    assert "NOT SHOWN IN FULL" in view
    not_shown = view.split("NOT SHOWN IN FULL", 1)[1].split("\n\n", 1)[0]
    for name in names:
        assert name in shown or name in not_shown         # shown, or said not to be
    assert len(view) <= 4000


def test_a_renamed_file_is_named_by_its_new_path(tmp_path):
    repo(tmp_path)
    (tmp_path / "old_name.py").write_text("X = 1\n")
    git(tmp_path, "add", "old_name.py")
    git(tmp_path, "commit", "-qm", "add")
    git(tmp_path, "mv", "old_name.py", "new_name.py")
    edit_tracked(tmp_path)
    (tmp_path / "extra.py").write_text("EXTRA = 1\n" * 2000)

    view = Workspace(str(tmp_path)).diff()

    assert "NOT SHOWN IN FULL" in view
    not_shown = view.split("NOT SHOWN IN FULL", 1)[1].split("\n\n", 1)[0]
    assert "  new_name.py" in not_shown.splitlines()
    assert "old_name.py b" not in not_shown


def test_a_view_with_room_for_everything_is_not_marked_trimmed(tmp_path):
    repo(tmp_path, lines=3)
    (tmp_path / "big.py").write_text("changed = True\n")
    (tmp_path / "new.py").write_text("NEW = 1\n")
    view = Workspace(str(tmp_path)).diff()
    assert "NEW = 1" in view and "+changed = True" in view
    assert "NOT SHOWN IN FULL" not in view
    assert Workspace.TRIM_MARKER not in view


# -- 2. a failed gate over unchanged files is re-run once --------------------
def session(tmp_path, gate, script=(DONE,), peer=(DONE,), **kwargs):
    root = tmp_path / "ws"
    root.mkdir()
    cfg = Config(task="fix it", root=str(root),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=kwargs.pop("max_rounds", 4), gate=gate, **kwargs)
    adapters = {
        "claude": MockAdapter(name="claude", cwd=str(root), config={"script": list(script)}),
        "gpt": MockAdapter(name="gpt", cwd=str(root), config={"script": list(peer)}),
    }
    orch = Orchestrator(cfg, adapters=adapters)
    return orch, orch.run()


def events(result):
    path = Path(result.session_dir) / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def python_gate(tmp_path, body):
    """A gate that runs `body` as Python: the same command under sh and cmd.exe."""
    script = tmp_path / "gate_script.py"
    script.write_text("import os, sys, time\n" + body + "\n", encoding="utf-8")
    return '"%s" "%s"' % (sys.executable, script)


def counting(runs):
    return "open(%r, 'a').write('run\\n')\n" % str(runs)


def flaky_gate(tmp_path, first):
    """Does `first` (Python) the first time it runs, passes every time after.
    The counter lives outside the workspace, so the files never change."""
    marker = tmp_path / "ran-once"
    runs = tmp_path / "runs"
    return python_gate(tmp_path, counting(runs) + (
        "if os.path.exists(%r):\n    sys.exit(0)\nopen(%r, 'w').close()\n%s"
        % (str(marker), str(marker), first))), runs


def test_a_gate_that_failed_once_is_re_run_and_the_pass_is_called_flaky(tmp_path):
    gate, runs = flaky_gate(
        tmp_path, "print('FAILED tests/test_page_chrome.py::test_real_chrome - Timeout'); sys.exit(1)")
    orch, result = session(tmp_path, gate)

    assert result.status == "consensus", result.reason
    assert len(runs.read_text().splitlines()) == 2          # one re-run, no more
    note = result.gate.headline()
    assert "failed once and passed on a re-run with no file changes" in note
    assert "possibly flaky" in note
    assert "tests/test_page_chrome.py::test_real_chrome" in note
    # The user is told, not just the agents.
    assert any(e["kind"] == "gate_flaky" for e in events(result))
    # And the agents see it in what they are shown of the gate.
    assert "possibly flaky" in result.gate.render()


def test_a_failure_that_repeats_is_a_failure(tmp_path):
    runs = tmp_path / "runs"
    orch, result = session(tmp_path, python_gate(tmp_path, counting(runs) + "sys.exit(1)"),
                           max_rounds=4)

    assert result.status != "consensus"
    assert not result.gate.ok
    assert result.gate.headline().startswith("FAILED")
    # Re-run once for the unchanged state, then believed: not once per turn.
    assert len(runs.read_text().splitlines()) == 2


def test_a_gate_that_timed_out_is_re_run_and_keeps_its_wording(tmp_path):
    gate, runs = flaky_gate(tmp_path, "time.sleep(30)")
    # Long enough for a Python start on a loaded machine; the first run sleeps 30.
    orch, result = session(tmp_path, gate, gate_timeout=5)

    assert result.status == "consensus", result.reason
    note = result.gate.headline()
    assert "ran out of time once and passed on a re-run with no file changes" in note
    assert "possibly flaky" in note
