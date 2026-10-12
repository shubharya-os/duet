"""`duet review`: one agent, one pass over the diff, no debate.

Every test here drives the real CLI. The reviewer is a mock backend pinned in
`.duet/config.json`, so the command builds its own adapter, its own prompt and
its own exit code exactly as it would with a real agent behind it.
"""

import json
import re
import subprocess
import sys

from duet.adapters.mock import envelope
from duet.cli import main

PASSING_GATE = "%s -c pass" % sys.executable
FAILING_GATE = "%s -c \"raise SystemExit(3)\"" % sys.executable

FINDINGS = envelope(
    "The loader never closes the file and blows up on an empty CSV.",
    issues=[
        {"id": "leaked-file-handle", "title": "the file is never closed",
         "severity": "major", "detail": "Use a with-statement in load() so the handle closes."},
        {"id": "no-empty-csv-case", "title": "an empty CSV raises IndexError",
         "severity": "blocker", "detail": "Return [] when there are no rows."},
        {"id": "unused-import", "title": "`os` is imported and unused",
         "severity": "minor", "detail": "Drop the import."},
    ],
    summary="two real defects, one nit",
)
CLEAN = envelope("Read it against the task; it does what was asked and the edges are covered.",
                 issues=[], summary="looks right", confidence=0.8)


# -- workspace fixtures ----------------------------------------------------
def git(tmp_path, *args):
    return subprocess.run(["git", *args], cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8")


def repo(tmp_path, changed=True):
    """A git repo with one commit, and (by default) an uncommitted change."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "loader.py").write_text("def load(path):\n    return open(path).read()\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "first")
    if changed:
        (tmp_path / "loader.py").write_text(
            "import os\n\ndef load(path):\n    return open(path).read().splitlines()[1:]\n"
        , encoding="utf-8")
    return tmp_path


def pin(tmp_path, script=None, error=None, writes=None, backend="mock",
        name="chatgpt", peer="claude"):
    """Pin a pair in .duet/config.json: `peer` leads, `name` is the default reviewer."""
    options = {}
    if script is not None:
        options["script"] = script
    if error is not None:
        options["error"] = error
    if writes is not None:
        options["writes"] = writes
    cfg = {
        "agents": [
            {"name": peer, "backend": "mock", "model": "", "options": {}},
            {"name": name, "backend": backend, "model": "", "options": options},
        ],
        "start": peer,
    }
    (tmp_path / ".duet").mkdir(exist_ok=True)
    (tmp_path / ".duet" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if (tmp_path / ".git").is_dir():
        # config.json travels with the repo, so a real workspace has it committed.
        # Leaving it untracked would show up as part of the change under review.
        git(tmp_path, "add", ".duet/config.json")
        git(tmp_path, "commit", "-qm", "pin the pair")
    return tmp_path


def review(tmp_path, *extra):
    return main(["review", "-C", str(tmp_path), *extra])


def review_json(tmp_path, capsys, *extra):
    code = review(tmp_path, "--json", *extra)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# -- the four cases the command exists for ---------------------------------
def test_findings_are_printed_by_severity_and_exit_1(tmp_path, capsys):
    pin(repo(tmp_path), script=[FINDINGS])
    assert review(tmp_path, "make the loader stream") == 1

    out = capsys.readouterr().out
    assert out.index("blocker (1)") < out.index("major (1)") < out.index("minor (1)")
    assert "an empty CSV raises IndexError" in out
    assert "Return [] when there are no rows." in out       # the fix, not just the complaint
    assert "3 findings" in out
    assert "should not ship" in out


def test_a_clean_review_exits_0(tmp_path, capsys):
    pin(repo(tmp_path), script=[CLEAN])
    assert review(tmp_path, "make the loader stream") == 0
    out = capsys.readouterr().out
    assert "no findings" in out
    assert "blocker" not in out


def test_only_minor_findings_do_not_block(tmp_path, capsys):
    nit = envelope("One nit.", issues=[{"id": "unused-import", "title": "`os` unused",
                                        "severity": "minor", "detail": "drop it"}])
    pin(repo(tmp_path), script=[nit])
    assert review(tmp_path) == 0
    assert "nothing blocking" in capsys.readouterr().out


def test_an_empty_diff_is_not_sent_to_the_reviewer(tmp_path, capsys):
    pin(repo(tmp_path, changed=False), script=[FINDINGS])
    assert review(tmp_path) == 0
    out = capsys.readouterr().out
    assert "nothing to review" in out
    assert "matches HEAD" in out
    assert "blocker" not in out          # the scripted findings were never asked for

    code, data = review_json(tmp_path, capsys)
    assert code == 0 and data["reviewed"] is False and data["issues"] == []


def test_a_backend_error_fails_loudly_instead_of_passing(tmp_path, capsys):
    pin(repo(tmp_path), error="codex exited 1: not signed in")
    assert review(tmp_path) == 2
    out = capsys.readouterr().out
    assert "not signed in" in out
    assert "no findings" not in out      # a dead backend is not a clean review

    code, data = review_json(tmp_path, capsys)
    assert code == 2 and data["ok"] is False
    assert "not signed in" in data["error"]


# -- everything else it has to get right -----------------------------------
def test_the_default_reviewer_is_the_peer_of_the_lead(tmp_path, capsys):
    pin(repo(tmp_path), script=[CLEAN], name="chatgpt", peer="claude")
    code, data = review_json(tmp_path, capsys)
    assert code == 0 and data["reviewer"] == "chatgpt"


def test_reviewer_can_be_chosen_and_is_checked(tmp_path, capsys):
    # `claude` leads, so it is not the default reviewer — but it can be asked.
    pin(repo(tmp_path), script=[CLEAN], name="chatgpt", peer="claude")
    code, data = review_json(tmp_path, capsys, "--reviewer", "claude")
    assert code == 0 and data["reviewer"] == "claude"
    assert data["issues"] == []          # claude's slot has no script: the default reply

    assert review(tmp_path, "--reviewer", "nobody") == 2
    assert "not one of this pair" in capsys.readouterr().out


def test_pair_selects_the_agents(tmp_path, capsys):
    pin(repo(tmp_path), script=[CLEAN])
    code, data = review_json(tmp_path, capsys, "--pair", "mock+mock")
    assert code == 0
    assert data["reviewer"] == "mock-2"   # the right-hand side of the pair reviews

    assert review(tmp_path, "--pair", "claude+nonsense") == 2
    assert "unknown agent" in capsys.readouterr().out


def test_the_gate_runs_first_and_its_output_reaches_the_reviewer(tmp_path, capsys):
    root = pin(repo(tmp_path), script=[CLEAN])
    code, data = review_json(root, capsys, "--gate", FAILING_GATE)
    assert code == 0                      # the gate result is context, not the verdict
    assert data["gate"]["ok"] is False and data["gate"]["exit_code"] == 3
    assert data["gate"]["skipped"] is False

    code, data = review_json(root, capsys, "--gate", PASSING_GATE)
    assert data["gate"]["ok"] is True


# A failing run that talks: 3000 chars, the interesting part, 3000 more. The
# default render limit would keep the two ends and drop exactly the middle.
NOISY_GATE = (
    "%s -c 'print(chr(65)*3000); print(\"E   IndexError: list index out of range\"); "
    "print(chr(66)*3000); raise SystemExit(1)'" % sys.executable
)


def test_the_reviewer_sees_the_middle_of_a_long_gate_failure(tmp_path):
    """On a failing test run the middle is the traceback. Cutting it out leaves
    the reviewer the progress dots and the word FAILED."""
    root = pin(repo(tmp_path), script=[CLEAN])
    _, prompt = sent_prompt(root, "--gate", NOISY_GATE)
    gate_section = prompt.split("=== COMMAND RUN AGAINST THIS CHANGE ===")[1]
    assert "IndexError: list index out of range" in gate_section
    assert "FAILED (exit 1)" in gate_section


def test_a_failing_gate_is_repeated_next_to_a_clean_verdict(tmp_path, capsys):
    """The gate line is printed before the review; a long review buries it. A
    reader who stops at "no findings" must not take a red gate for a green one."""
    root = pin(repo(tmp_path), script=[CLEAN])
    assert review(root, "--gate", FAILING_GATE) == 0
    out = capsys.readouterr().out
    assert out.index("no findings") < out.index("but the gate failed (exit 3)")
    assert "defect in this change" in out

    # With something blocking already said, the verdict is not misleading and the
    # line would just be noise.
    pin(root, script=[FINDINGS])
    assert review(root, "--gate", FAILING_GATE) == 1
    assert "but the gate failed" not in capsys.readouterr().out


def test_a_gate_that_writes_files_does_not_pollute_the_diff(tmp_path, capsys):
    """A gate is free to write files; the reviewer must not be shown them as the change."""
    root = pin(repo(tmp_path), script=[CLEAN])
    littering = "%s -c \"open('gate-artifact.txt','w').write('x')\"" % sys.executable
    _, prompt = sent_prompt(root, "--gate", littering)
    assert (root / "gate-artifact.txt").is_file()
    change = prompt.split("=== COMMAND RUN AGAINST THIS CHANGE ===")[0]
    assert "gate-artifact.txt" not in change   # only the gate's own command line mentions it


def test_a_reply_with_no_envelope_is_not_reported_as_clean(tmp_path, capsys):
    pin(repo(tmp_path), script=["Looks fine to me, ship it."])
    assert review(tmp_path) == 2
    out = capsys.readouterr().out
    assert "without a JSON envelope" in out
    assert "no findings" not in out


def test_json_output_carries_the_findings(tmp_path, capsys):
    pin(repo(tmp_path), script=[FINDINGS])
    code, data = review_json(tmp_path, capsys, "stream the loader")
    assert code == 1 and data["ok"] is False
    assert data["counts"] == {"blocker": 1, "major": 1, "minor": 1}
    assert [i["id"] for i in data["issues"]] == [
        "leaked-file-handle", "no-empty-csv-case", "unused-import"]
    assert data["task"] == "stream the loader"
    assert data["reviewed"] is True
    assert data["summary"] == "two real defects, one nit"


def test_the_task_falls_back_to_the_last_recorded_session(tmp_path, capsys):
    root = pin(repo(tmp_path), script=[CLEAN])
    session = root / ".duet" / "sessions" / "20260101-000000-aaaa"
    session.mkdir(parents=True)
    (session / "state.json").write_text(json.dumps(
        {"config": {"task": "port the CSV loader to streaming", "acceptance": "pytest passes"}}), encoding="utf-8")

    code, data = review_json(root, capsys)
    assert data["task"] == "port the CSV loader to streaming"
    # Named and dated, not just "the last recorded session". A review that
    # borrows a stale task judges the change against the wrong brief, and the
    # only way to catch that is to be told which session it came from.
    assert data["task_source"].startswith("session 20260101-000000-aaaa")
    assert "ago)" in data["task_source"]

    code, data = review_json(root, capsys, "something else entirely")
    assert data["task"] == "something else entirely"   # the argument wins


def test_no_task_anywhere_is_still_a_review(tmp_path, capsys):
    pin(repo(tmp_path), script=[CLEAN])
    code, data = review_json(tmp_path, capsys)
    assert code == 0 and data["task"] == "" and data["task_source"] is None


def test_a_non_git_directory_is_reviewed_as_a_file_listing(tmp_path, capsys):
    pin(tmp_path, script=[CLEAN])
    (tmp_path / "thing.py").write_text("x = 1\n", encoding="utf-8")
    code, data = review_json(tmp_path, capsys)
    assert code == 0 and data["git"] is False and data["reviewed"] is True


def test_an_empty_non_git_directory_has_nothing_to_review(tmp_path, capsys):
    pin(tmp_path, script=[FINDINGS])
    code, data = review_json(tmp_path, capsys)
    assert code == 0 and data["reviewed"] is False and data["git"] is False

    assert review(tmp_path) == 0
    assert "the workspace is empty" in capsys.readouterr().out


# -- the prompt the reviewer actually gets ---------------------------------
def sent_prompt(tmp_path, *extra):
    """Run a review and return (system, user) as the adapter saw them."""
    import duet.adapters as adapters_mod

    captured = {}
    real_build = adapters_mod.build

    def spy(backend, **kwargs):
        adapter = real_build(backend, **kwargs)
        captured["adapter"] = adapter
        return adapter

    import duet.cli as cli_mod
    original = cli_mod.build_adapter
    cli_mod.build_adapter = spy
    try:
        review(tmp_path, *extra)
    finally:
        cli_mod.build_adapter = original
    adapter = captured["adapter"]
    return adapter.systems[0], adapter.prompts[0]


def test_the_review_prompt_has_no_loop_in_it(tmp_path):
    """The reviewer is alone. Telling it about voting or a peer is a lie."""
    pin(repo(tmp_path), script=[CLEAN])
    system, prompt = sent_prompt(tmp_path, "stream the loader", "--gate", PASSING_GATE)
    both = system + "\n" + prompt

    assert "DONE" not in both                      # there is no verdict to cast
    for word in ("consensus", "peer", "peers", "sign off", "signed off", "vote",
                 "votes", "verdict", "round", "rounds", "arbitration", "debate"):
        assert not re.search(r"\b%s\b" % word, both, re.IGNORECASE), \
            "review prompt still mentions %r" % word


def test_the_review_prompt_carries_the_change_the_task_and_the_gate(tmp_path):
    pin(repo(tmp_path), script=[CLEAN])
    system, prompt = sent_prompt(tmp_path, "stream the loader", "--gate", PASSING_GATE)

    assert "stream the loader" in prompt
    assert "loader.py" in prompt and "splitlines" in prompt   # the real diff
    assert "COMMAND RUN AGAINST THIS CHANGE" in prompt and "PASSED" in prompt
    assert '"issues"' in system and "severity" in system      # how to answer
    assert "blocker" in system and "minor" in system


def test_the_prompt_says_so_when_there_is_no_task(tmp_path):
    pin(repo(tmp_path), script=[CLEAN])
    _, prompt = sent_prompt(tmp_path)
    assert "not recorded" in prompt


# -- what the reviewer can and cannot be allowed to get away with ----------
def test_a_new_file_is_sent_with_its_contents_not_just_its_name(tmp_path, capsys):
    """A new module and its tests is the commonest change there is, and it
    appears in no diff. A reviewer given only the filename reviews nothing."""
    root = pin(repo(tmp_path), script=[CLEAN])
    (root / "brand_new.py").write_text("def added_later():\n    return 'the body of a new file'\n", encoding="utf-8")

    _, prompt = sent_prompt(root)
    assert "=== NEW FILE: brand_new.py ===" in prompt
    assert "the body of a new file" in prompt

    code, data = review_json(root, capsys)
    assert data["new_files"] == ["brand_new.py"]


def test_an_incomplete_view_says_which_kind_it_was(tmp_path, capsys):
    """A skipped file is not an oversized diff. Blaming the wrong one sends the
    reader looking at the diff limit for a file that was simply left out."""
    from duet.cli import NEW_FILE_COUNT

    root = pin(repo(tmp_path), script=[CLEAN])
    for n in range(NEW_FILE_COUNT + 3):
        (root / ("extra_%02d.py" % n)).write_text("x = %d\n" % n, encoding="utf-8")

    code, data = review_json(root, capsys)
    assert code == 0 and data["truncated"] is True     # the reviewer is still warned

    assert review(root) == 0
    out = capsys.readouterr().out
    assert "some of the new files did not fit" in out
    assert "larger than" not in out
    assert "not sent at all" in out                    # and the note names them


def test_a_new_file_with_an_awkward_name_still_has_a_body(tmp_path):
    """git escapes non-ASCII paths in its status output by default. An escaped
    name resolves to nothing on disk, so the file quietly arrives as a name."""
    root = pin(repo(tmp_path), script=[CLEAN])
    try:
        (root / "café loader.py").write_text("def accented():\n    return 'body of the odd one'\n", encoding="utf-8")
    except (OSError, UnicodeEncodeError):          # pragma: no cover - exotic filesystem
        import pytest
        pytest.skip("filesystem will not take that name")

    _, prompt = sent_prompt(root)
    assert "body of the odd one" in prompt


def test_duets_own_session_files_are_not_sent_as_the_change(tmp_path):
    root = pin(repo(tmp_path), script=[CLEAN])
    session = root / ".duet" / "sessions" / "20260101-000000-aaaa"
    session.mkdir(parents=True)
    (session / "transcript.md").write_text("pages and pages of an old argument\n", encoding="utf-8")

    _, prompt = sent_prompt(root)
    assert "pages and pages" not in prompt


def test_a_truncated_change_is_flagged_not_silently_shortened(tmp_path, capsys):
    root = pin(repo(tmp_path), script=[CLEAN])
    (root / "loader.py").write_text("".join("line %05d %s\n" % (n, "x" * 40) for n in range(3000)), encoding="utf-8")

    code, data = review_json(root, capsys)
    assert code == 0 and data["truncated"] is True

    _, prompt = sent_prompt(root)
    assert "not looking at the whole change" in prompt   # the reviewer is told
    assert review(root) == 0
    assert "cut short" in capsys.readouterr().out        # and so is the human


def test_a_reviewer_that_edits_the_tree_does_not_get_a_clean_exit(tmp_path, capsys):
    """A review is an opinion, not a turn. If the reviewer writes, the findings
    stop describing what is on disk and the exit code has to say so."""
    root = pin(repo(tmp_path), script=[CLEAN], writes={"loader.py": "print('I fixed it myself')\n"})
    assert review(root) == 2

    out = capsys.readouterr().out
    assert "no findings" in out                    # what it said is still shown
    assert "the workspace changed while" in out
    # duet sees the change, not who made it, so the message must not assert
    # that the reviewer did it — it fired once on an unrelated concurrent edit.
    assert "held read-only" in out
    assert (root / "loader.py").read_text(encoding="utf-8") == "print('I fixed it myself')\n"

    # A second run needs the reviewer to write something new: rewriting the same
    # bytes is not a change to the tree, and must not be reported as one.
    pin(root, script=[CLEAN], writes={"loader.py": "print('and again')\n"})
    code, data = review_json(root, capsys)
    assert code == 2 and data["workspace_changed"] is True


def test_a_gate_that_writes_is_not_mistaken_for_the_reviewer_editing(tmp_path, capsys):
    root = pin(repo(tmp_path), script=[CLEAN])
    littering = "%s -c \"open('gate-artifact.txt','w').write('x')\"" % sys.executable
    code, data = review_json(root, capsys, "--gate", littering)
    assert code == 0 and data["workspace_changed"] is False


def test_a_degraded_reply_is_reported_in_both_outputs(tmp_path, capsys):
    """A reply that arrives *with* an error is the case the guard exists for."""
    pin(repo(tmp_path), script=[CLEAN], error="codex exited 1: output may be truncated")
    assert review(tmp_path) == 0
    out = capsys.readouterr().out
    assert "output may be truncated" in out         # not only in --json

    code, data = review_json(tmp_path, capsys)
    assert any("output may be truncated" in n for n in data["notes"])


def test_an_unknown_backend_is_a_setup_error_not_a_failed_review(tmp_path, capsys):
    pin(repo(tmp_path), backend="gpt-5-turbo-imaginary")
    code, data = review_json(tmp_path, capsys)
    assert code == 2 and data["ok"] is False        # not 1: nobody reviewed anything
    assert data["reviewed"] is False and data["issues"] == []
    assert "unknown backend" in data["error"]


def test_an_unreadable_task_file_is_a_setup_error(tmp_path, capsys):
    pin(repo(tmp_path), script=[CLEAN])
    code, data = review_json(tmp_path, capsys, "-f", str(tmp_path / "nope.txt"))
    assert code == 2 and "could not read --file" in data["error"]


def test_review_accepts_no_gate_like_run_does(tmp_path):
    """`--no-gate` existed on `run` and not on `review`, for no reason a user
    could infer. I hit the resulting error twice in one session before fixing
    it, which is the definition of a papercut worth removing."""
    from duet.cli import build_parser

    parser = build_parser()
    for command in ("run", "review"):
        args = parser.parse_args([command, "x", "--no-gate"])
        assert args.no_gate is True, command
