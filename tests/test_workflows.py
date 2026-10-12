"""Workflow rules, checked against what happened rather than what was said.

Every case here is a pair that agrees — both vote DONE on the same state with
the gate green — and the question is only whether that agreement is allowed
to count. A plain `duet run` would accept all of them.
"""

import json
import sys
from pathlib import Path

from duet import workflows
from duet.adapters.mock import MockAdapter, envelope
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator
from duet.winlaunch import quote_for_shell


def run(tmp_path, claude_script, gpt_script, **kwargs):
    cfg = Config(
        task="t", root=str(tmp_path),
        agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
        start="claude", max_rounds=kwargs.pop("max_rounds", 8), **kwargs,
    )
    orch = Orchestrator(cfg, adapters={
        "claude": MockAdapter(name="claude", cwd=str(tmp_path), config={"script": claude_script}),
        "gpt": MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": gpt_script}),
    })
    return orch, orch.run()


def kinds(result):
    path = Path(result.session_dir) / "events.jsonl"
    return [json.loads(l).get("kind") for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write(path, content, verdict="DONE"):
    return envelope("wrote %s" % path, verdict, patches=[{"path": path, "content": content}])


DONE = envelope("I would ship this", "DONE", confidence=0.9)


def python_gate(code):
    """A gate that means the same in sh and in cmd.exe, which runs it on Windows.

    `test "$(cat f)" = x` is sh: cmd never expands `$(...)`, so the comparison
    was of two literal strings, the same answer every time, and the rule under
    test never saw the gate change.
    """
    return '%s -c "import pathlib, sys; %s"' % (quote_for_shell(sys.executable), code)


# -- fix: the bug has to have been seen to fail --------------------------------

def test_a_fix_that_never_reproduced_the_bug_does_not_count(tmp_path):
    # The gate is red at the start only because nothing exists yet — no tests,
    # so no reproduction. The pair makes it green and both sign off.
    _, result = run(tmp_path, [write("fixed.txt", "yes")] + [DONE] * 6, [DONE] * 6,
                    workflow="fix", gate="test -f fixed.txt")
    assert result.status != "consensus"
    assert "workflow_veto" in kinds(result)


def test_a_fix_whose_bug_went_red_first_counts(tmp_path):
    gate = python_gate("p = pathlib.Path('state.txt'); "
                       "sys.exit(p.is_file() and p.read_text(encoding='utf-8') == 'red')")
    _, result = run(
        tmp_path,
        [write("state.txt", "red", "CONTINUE"), DONE, DONE],   # reproduce
        [write("state.txt", "green"), DONE, DONE],              # then fix
        workflow="fix", gate=gate,
    )
    assert result.status == "consensus"
    assert "workflow_veto" not in kinds(result)


def test_a_suite_already_failing_on_arrival_counts_as_reproduced(tmp_path):
    (tmp_path / "test_bug.py").write_text("def test_bug():\n    assert False\n", encoding="utf-8")
    _, result = run(tmp_path, [write("fixed.txt", "yes")] + [DONE] * 4, [DONE] * 4,
                    workflow="fix", gate="test -f fixed.txt")
    assert result.status == "consensus"


# -- add: something has to test it ---------------------------------------------

def test_a_feature_with_no_test_does_not_count(tmp_path):
    _, result = run(tmp_path, [write("feature.py", "x = 1\n")] + [DONE] * 6, [DONE] * 6,
                    workflow="add", gate="true")
    assert result.status != "consensus"
    assert "workflow_veto" in kinds(result)


PYTEST = "%s -m pytest -q -p no:cacheprovider" % sys.executable


def test_a_feature_with_a_test_that_fails_without_it_counts(tmp_path):
    feature = envelope("added double() and its test", "DONE", patches=[
        {"path": "feature.py", "content": "def double(x):\n    return 2 * x\n"},
        {"path": "test_feature.py",
         "content": "from feature import double\n\ndef test_double():\n    assert double(2) == 4\n"},
    ])
    _, result = run(tmp_path, [feature] + [DONE] * 3, [DONE] * 3, workflow="add", gate=PYTEST)
    assert result.status == "consensus"


def test_a_feature_whose_new_test_would_pass_without_it_does_not_count(tmp_path):
    """The whitespace-edit loophole: a test changed, but tests nothing new."""
    (tmp_path / "core.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_core.py").write_text("from core import one\n\ndef test_one():\n    assert one() == 1\n", encoding="utf-8")
    unrelated = envelope("added a feature and a test", "DONE", patches=[
        {"path": "feature.py", "content": "def two():\n    return 2\n"},
        {"path": "test_extra.py",   # passes on the original code too
         "content": "from core import one\n\ndef test_one_again():\n    assert one() == 1\n"},
    ])
    _, result = run(tmp_path, [unrelated] + [DONE] * 6, [DONE] * 6, workflow="add", gate=PYTEST)
    assert result.status != "consensus"
    assert "workflow_veto" in kinds(result)


# -- fix, replayed: the tests have to catch the bug in the original ----------

BUGGY = 'def slugify(text):\n    return "-".join(text.lower().split())\n'
FIXED = ('import re\n\ndef slugify(text):\n'
         '    return "-".join(re.sub(r"[^\\w\\s-]", "", text.lower()).split())\n')
OLD_TEST = 'from slug import slugify\n\ndef test_spaces():\n    assert slugify("a b") == "a-b"\n'


def test_a_fix_written_in_one_turn_counts_without_re_breaking_anything(tmp_path):
    """What the first live `duet fix` did: test and fix together, gate never red.

    The history check vetoed it, and one agent then re-broke working code on
    purpose to show the harness a red gate. The replay accepts it outright,
    because the new test fails against the original code by itself.
    """
    (tmp_path / "slug.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "test_slug.py").write_text(OLD_TEST, encoding="utf-8")
    both = envelope("fixed, with a test", "DONE", patches=[
        {"path": "slug.py", "content": FIXED},
        {"path": "test_slug.py", "content": OLD_TEST +
         '\ndef test_punctuation():\n    assert slugify("Hello, World!") == "hello-world"\n'},
    ])
    _, result = run(tmp_path, [both] + [DONE] * 3, [DONE] * 3, workflow="fix", gate=PYTEST)
    assert result.status == "consensus"
    assert "workflow_veto" not in kinds(result)


def test_a_fix_whose_tests_pass_on_the_buggy_code_does_not_count(tmp_path):
    (tmp_path / "slug.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "test_slug.py").write_text(OLD_TEST, encoding="utf-8")
    blind = envelope("fixed, with a test that does not look at the bug", "DONE", patches=[
        {"path": "slug.py", "content": FIXED},
        {"path": "test_slug.py", "content": OLD_TEST +
         '\ndef test_lowercase():\n    assert slugify("ABC") == "abc"\n'},
    ])
    _, result = run(tmp_path, [blind] + [DONE] * 6, [DONE] * 6, workflow="fix", gate=PYTEST)
    assert result.status != "consensus"
    assert "workflow_veto" in kinds(result)


def test_a_fix_may_not_delete_the_failing_test(tmp_path):
    (tmp_path / "slug.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "test_bug.py").write_text(
        'from slug import slugify\n\ndef test_bug():\n    assert slugify("a,b") == "ab"\n', encoding="utf-8")
    delete = envelope("removed the failing test", "DONE",
                      patches=[{"path": "test_bug.py", "action": "delete"}])
    _, result = run(tmp_path, [delete] + [DONE] * 6, [DONE] * 6, workflow="fix", gate=PYTEST)
    assert result.status != "consensus"


# -- refactor: the yardstick may not move --------------------------------------

def test_a_refactor_that_edits_an_existing_test_does_not_count(tmp_path):
    (tmp_path / "test_core.py").write_text("def test_core():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    _, result = run(tmp_path,
                    [write("test_core.py", "def test_core():\n    pass\n")] + [DONE] * 6,
                    [DONE] * 6, workflow="refactor", gate="true")
    assert result.status != "consensus"
    assert "workflow_veto" in kinds(result)


def test_a_refactor_that_leaves_the_tests_alone_counts(tmp_path):
    (tmp_path / "test_core.py").write_text("def test_core():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    _, result = run(tmp_path, [write("core.py", "def add(a, b):\n    return a + b\n")] + [DONE] * 3,
                    [DONE] * 3, workflow="refactor", gate="true")
    assert result.status == "consensus"


def test_a_refactor_may_add_tests(tmp_path):
    (tmp_path / "test_core.py").write_text("def test_core():\n    assert True\n", encoding="utf-8")
    _, result = run(tmp_path, [write("test_extra.py", "def test_more():\n    pass\n")] + [DONE] * 3,
                    [DONE] * 3, workflow="refactor", gate="true")
    assert result.status == "consensus"


# -- plan: nothing but the plan ------------------------------------------------

def test_a_plan_that_also_changed_code_does_not_count(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    both = envelope("planned, and started", "DONE",
                    patches=[{"path": "PLAN.md", "content": "# plan\n1. do it\n"},
                             {"path": "app.py", "content": "x = 2\n"}])
    _, result = run(tmp_path, [both] + [DONE] * 6, [DONE] * 6, workflow="plan")
    assert result.status != "consensus"
    assert "workflow_veto" in kinds(result)


def test_a_plan_with_no_plan_in_it_does_not_count(tmp_path):
    _, result = run(tmp_path, [DONE] * 6, [DONE] * 6, workflow="plan")
    assert result.status != "consensus"


def test_a_plan_that_is_only_a_plan_counts(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _, result = run(tmp_path, [write("PLAN.md", "# plan\n1. do it\n")] + [DONE] * 3, [DONE] * 3,
                    workflow="plan")
    assert result.status == "consensus"


# -- mechanics -----------------------------------------------------------------

def test_a_plain_run_is_unaffected(tmp_path):
    _, result = run(tmp_path, [write("feature.py", "x = 1\n"), DONE], [DONE, DONE], gate="true")
    assert result.status == "consensus"


def test_a_resumed_session_keeps_its_original_baseline():
    """begin() must not retake the baseline from wherever a session died."""
    wf = workflows.get("refactor")
    state = {"baseline_tests": {"test_core.py": "original-hash"}}
    wf.begin("/nonexistent", state)
    assert state["baseline_tests"] == {"test_core.py": "original-hash"}


def test_test_files_ignore_dependency_trees(tmp_path):
    (tmp_path / "test_mine.py").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "a.test.js").write_text("x", encoding="utf-8")
    assert list(workflows.test_files(str(tmp_path))) == ["test_mine.py"]


# -- the commands: refusing to start when the rule could not be checked --------

import argparse  # noqa: E402

from duet import workflow_commands  # noqa: E402


def args_for(tmp_path, what="the thing", **kw):
    base = dict(what=[what], root=str(tmp_path), gate=None, no_gate=False,
                allow_nested=False, json=False, command="")
    base.update(kw)
    return argparse.Namespace(**base)


def test_fix_refuses_to_start_without_a_test_command(tmp_path, capsys):
    """No starter gate here: red because nothing exists would count as reproduced."""
    assert workflow_commands.run(args_for(tmp_path), "fix") == 3
    assert "needs this project's test command" in capsys.readouterr().out


def test_refactor_refuses_to_start_on_a_failing_suite(tmp_path, capsys):
    assert workflow_commands.run(args_for(tmp_path, gate="false"), "refactor") == 3
    out = capsys.readouterr().out
    assert "failing before the refactor has started" in out
    assert "duet fix" in out     # and says what to do instead


def test_every_workflow_refuses_an_empty_description(tmp_path, capsys):
    for name in ("fix", "add", "refactor", "plan"):
        assert workflow_commands.run(args_for(tmp_path, what=""), name) == 2
        assert "usage: duet %s" % name in capsys.readouterr().out


def test_each_task_names_its_gate_and_states_its_rule():
    for name in ("fix", "add", "refactor"):
        task = workflow_commands.TASKS[name] % {"what": "x", "gate": "pytest -q"}
        assert "pytest -q" in task, name
        assert "checked by the harness" in task, name
    plan = workflow_commands.TASKS["plan"] % {"what": "x", "gate": "", "tests": "", "run": ""}
    assert "Only PLAN.md may change" in plan


def test_a_met_rule_is_announced_with_what_it_proved(tmp_path):
    """The replay's verdict is the reason to use `fix` — so the user sees it."""
    (tmp_path / "slug.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "test_slug.py").write_text(OLD_TEST, encoding="utf-8")
    both = envelope("fixed, with a test", "DONE", patches=[
        {"path": "slug.py", "content": FIXED},
        {"path": "test_slug.py", "content": OLD_TEST +
         '\ndef test_punctuation():\n    assert slugify("Hello, World!") == "hello-world"\n'},
    ])
    _, result = run(tmp_path, [both] + [DONE] * 3, [DONE] * 3, workflow="fix", gate=PYTEST)
    path = Path(result.session_dir) / "events.jsonl"
    proven = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
              if json.loads(l).get("kind") == "workflow_proven"]
    assert proven and "fail on the original code" in proven[0]["detail"]


def test_the_plan_task_asks_for_proportion():
    """Two Claude models wrote a 326-line plan for a two-line edit.

    Neither pushed back on the scope, and the task gave them no reason to:
    it listed everything a plan should cover and nothing about size.
    """
    task = workflow_commands.TASKS["plan"] % {"what": "x", "gate": "", "tests": "", "run": ""}
    assert "Size the plan to the change" in task


def test_a_fix_whose_tests_only_fail_on_an_import_is_accepted_but_flagged(tmp_path):
    """The replay's documented blind spot, now said out loud.

    The test imports `strip_punct`, a helper the fix added. On the original
    code that import fails, so the replay "fails on original" — proving the
    test needs the new code, not that it catches the bug. Blocking would be
    wrong (pytest drops the whole module on one bad import, real checks
    included), so it is accepted and the verdict says what it did not prove.
    """
    (tmp_path / "slug.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "test_slug.py").write_text(OLD_TEST, encoding="utf-8")
    helper_fix = envelope("fixed via a helper, tested via the helper", "DONE", patches=[
        {"path": "slug.py", "content": FIXED + "\n\ndef strip_punct(t):\n    return t\n"},
        {"path": "test_helper.py",
         "content": "from slug import strip_punct\n\ndef test_helper():\n    assert strip_punct('a') == 'a'\n"},
    ])
    _, result = run(tmp_path, [helper_fix] + [DONE] * 3, [DONE] * 3, workflow="fix", gate=PYTEST)
    assert result.status == "consensus"
    path = Path(result.session_dir) / "events.jsonl"
    proven = [json.loads(l)["detail"] for l in path.read_text(encoding="utf-8").splitlines()
              if json.loads(l).get("kind") == "workflow_proven"]
    assert proven and "only because they import" in proven[0]


# -- plan: the existing tests have to be walked through --------------------------

def test_a_plan_that_skips_an_existing_test_does_not_count(tmp_path):
    """Both plans in a head-to-head missed a pitfall in a test neither had read."""
    (tmp_path / "test_core.py").write_text(
        "def test_adds():\n    pass\n\ndef test_rejects_bools():\n    pass\n", encoding="utf-8")
    partial = write("PLAN.md", "# plan\nKeep test_adds passing by leaving add() alone.\n")
    _, result = run(tmp_path, [partial] + [DONE] * 6, [DONE] * 6, workflow="plan")
    assert result.status != "consensus"
    assert "workflow_veto" in kinds(result)


def test_a_plan_that_accounts_for_every_test_counts(tmp_path):
    (tmp_path / "test_core.py").write_text(
        "def test_adds():\n    pass\n\ndef test_rejects_bools():\n    pass\n", encoding="utf-8")
    full = write("PLAN.md", "# plan\n- test_adds: unchanged.\n- test_rejects_bools: validate first.\n")
    _, result = run(tmp_path, [full] + [DONE] * 3, [DONE] * 3, workflow="plan")
    assert result.status == "consensus"


def test_test_names_are_read_across_ecosystems(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_one():\n    pass\nasync def test_two():\n    pass\n", encoding="utf-8")
    (tmp_path / "x_test.go").write_text("func TestThree(t *testing.T) {}\n", encoding="utf-8")
    (tmp_path / "y.test.ts").write_text("it('handles the empty case', () => {})\n", encoding="utf-8")
    names = workflows.test_names(str(tmp_path))
    assert {"test_one", "test_two", "TestThree", "handles the empty case"} <= set(names)


def test_the_plan_task_hands_the_pair_the_test_names(tmp_path, capsys, monkeypatch):
    (tmp_path / "test_core.py").write_text("def test_adds():\n    pass\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr("duet.cli.cmd_run", lambda a: seen.setdefault("task", a.task[0]) and 0)
    workflow_commands.run(args_for(tmp_path, what="move storage"), "plan")
    assert "test_adds" in seen["task"]


# -- what duet recognises as a test, per ecosystem ----------------------------
#
# Three live sessions in an iOS repo ended EXHAUSTED with a green gate and ~90
# XCTest cases between them: TEST_DIRS was compared case-sensitively, so Xcode's
# conventional `Tests/` never matched, and no glob knew about Swift. duet saw a
# repo with zero tests, so `add` said no test had been added and `fix` had
# nothing to replay.

import pytest  # noqa: E402

RECOGNISED = [
    # Xcode's conventional directory, and its test-target directories.
    ("Tests/FooTests.swift", True),
    ("Tests/Helpers.swift", True),          # anything inside a test directory
    ("GalleryTests/AppLogicTests.swift", True),
    ("MyAppTests/FooTests.swift", True),
    ("MyAppUITests/LoginUITests.swift", True),
    ("UITests/Flow.swift", True),
    ("TESTS/thing.py", True),               # the exact names, any casing
    ("Specs/thing.rb", True),
    ("__tests__/a.js", True),
    # Ordinary source, which must stay ordinary source.
    ("Sources/Foo.swift", False),
    ("Sources/Contest.swift", False),
    ("Contests/Foo.swift", False),          # ends in "tests", lowercase t
    ("Latests/Foo.swift", False),
    ("Attestations/Foo.swift", False),
    ("Tests.swift", True),                  # the glob, with nothing in front
    # Swift by filename, outside any test directory.
    ("Sources/FooTests.swift", True),
    ("Sources/FooTest.swift", True),
    ("Sources/FooSpec.swift", True),
    # The other suffix-named ecosystems.
    ("src/CalculatorTests.cs", True),
    ("src/CalculatorTest.cs", True),
    ("src/contest.cs", False),
    ("lib/parser_test.exs", True),
    ("lib/parser.exs", False),
    ("src/ParserSpec.scala", True),
    ("src/ParserTest.scala", True),
    ("src/ParserSuite.scala", True),
    ("src/Parser.scala", False),
    ("src/InvoiceTest.php", True),
    ("src/latest.php", False),
    ("lib/widget_test.dart", True),
    ("lib/widget.dart", False),
]


@pytest.mark.parametrize("rel,is_test", RECOGNISED)
def test_what_counts_as_a_test_file(tmp_path, rel, is_test):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    assert (rel in workflows.test_files(str(tmp_path))) is is_test


def test_a_changed_swift_test_satisfies_the_add_rule(tmp_path):
    """The `add` veto in an Xcode layout, which compared two empty sets."""
    (tmp_path / "GalleryTests").mkdir()
    suite = tmp_path / "GalleryTests" / "AppLogicTests.swift"
    suite.write_text("func testOne() { XCTAssertTrue(true) }\n", encoding="utf-8")
    wf = workflows.get("add")
    state = {}
    wf.begin(str(tmp_path), state)
    assert "GalleryTests/AppLogicTests.swift" in state["baseline_tests"]

    # Untouched: still "no test was added or extended".
    assert "No test was added or extended" in (wf.veto(str(tmp_path), state) or "")

    suite.write_text("func testOne() { XCTAssertTrue(true) }\n"
                     "func testTwo() { XCTAssertFalse(false) }\n", encoding="utf-8")
    assert wf.veto(str(tmp_path), state) is None


def test_swift_test_names_are_read_for_a_plan(tmp_path):
    (tmp_path / "Tests").mkdir()
    (tmp_path / "Tests" / "BoardTests.swift").write_text(
        "final class BoardTests: XCTestCase {\n"
        "    func testLadderMovesUp() {}\n"
        "    private func helper() {}\n"
        "    func testSnakeMovesDown() throws {}\n}\n", encoding="utf-8")
    assert workflows.test_names(str(tmp_path)) == ["testLadderMovesUp", "testSnakeMovesDown"]


def test_a_go_helper_is_not_mistaken_for_a_swift_test(tmp_path):
    """`func testServer(t *testing.T)` is a helper in Go and a test in Swift.

    Read out of a Go file, it would arrive at the plan rule as a test the plan
    has to account for by name — a demand for something that is not a test.
    """
    (tmp_path / "server_test.go").write_text(
        "func TestServes(t *testing.T) {}\n\nfunc testServer(t *testing.T) *S { return nil }\n", encoding="utf-8")
    assert workflows.test_names(str(tmp_path)) == ["TestServes"]


# -- the replay, which copies the files the same detector finds ---------------

SWIFT_GATE = python_gate("t = pathlib.Path('Tests/AppTests.swift').read_text(encoding='utf-8'); "
                         "s = pathlib.Path('Sources/App.swift').read_text(encoding='utf-8'); "
                         "sys.exit(t != s)")


def swift_workspace(tmp_path, app, expectation):
    (tmp_path / "Sources").mkdir(exist_ok=True)
    (tmp_path / "Tests").mkdir(exist_ok=True)
    (tmp_path / "Sources" / "App.swift").write_text(app, encoding="utf-8")
    (tmp_path / "Tests" / "AppTests.swift").write_text(expectation, encoding="utf-8")


def test_the_replay_runs_todays_swift_tests_against_the_original(tmp_path):
    """Invisible test files meant the replay copied nothing and answered None."""
    swift_workspace(tmp_path, "buggy\n", "buggy\n")
    state = {"gate": SWIFT_GATE}
    workflows.snapshot(str(tmp_path), state)
    swift_workspace(tmp_path, "fixed\n", "fixed\n")       # the fix, and its test

    assert workflows.replay_fails(str(tmp_path), state) is True
    assert "replay_note" not in state


def test_a_swift_test_that_would_have_passed_anyway_is_still_caught(tmp_path):
    swift_workspace(tmp_path, "same\n", "same\n")
    state = {"gate": SWIFT_GATE}
    workflows.snapshot(str(tmp_path), state)
    (tmp_path / "Tests" / "AppTests.swift").write_text("same\n", encoding="utf-8")   # rewritten, not stronger
    assert workflows.replay_fails(str(tmp_path), state) is False


def test_a_workspace_with_nothing_that_looks_like_a_test_says_so(tmp_path):
    (tmp_path / "App.swift").write_text("x\n", encoding="utf-8")
    state = {"gate": "true"}
    workflows.snapshot(str(tmp_path), state)
    assert workflows.replay_fails(str(tmp_path), state) is None
    assert "nothing to replay" in state["replay_note"]


PBXPROJ = """// !$*UTF8*$!
{ objects = {
    A1 /* ExistingTests.swift */ = {isa = PBXFileReference; path = ExistingTests.swift; };
  };
}
"""

SYNCED_PBXPROJ = """// !$*UTF8*$!
{ objects = {
    A1 = {isa = PBXFileSystemSynchronizedRootGroup; path = AppTests; };
  };
}
"""


def xcode_workspace(tmp_path, pbxproj=PBXPROJ):
    (tmp_path / "App.xcodeproj").mkdir(parents=True, exist_ok=True)
    (tmp_path / "App.xcodeproj" / "project.pbxproj").write_text(pbxproj, encoding="utf-8")
    (tmp_path / "AppTests").mkdir(exist_ok=True)
    (tmp_path / "AppTests" / "ExistingTests.swift").write_text("func testOld() {}\n", encoding="utf-8")


# A gate driven by Xcode — the driver's name is what marks it as one. Where it
# is reached at all it stands in for a build that failed; the case that matters
# most never runs it, because the replay refuses before getting that far.
XCODE_GATE = "false   # xcodebuild test -scheme App"


def test_a_new_test_xcode_would_not_compile_is_reported_as_could_not_run(tmp_path):
    """Honest failure mode: the old target cannot contain a test written today.

    Copying the file in is enough for pytest or go test, which walk the tree.
    Xcode builds what project.pbxproj lists, so the replay would run the old
    tests and pass — which is not the same as "your tests pass on the original".
    """
    xcode_workspace(tmp_path)
    state = {"gate": XCODE_GATE}
    workflows.snapshot(str(tmp_path), state)
    (tmp_path / "AppTests" / "NewTests.swift").write_text("func testNew() {}\n", encoding="utf-8")

    assert workflows.replay_fails(str(tmp_path), state) is None
    assert "NewTests.swift" in state["replay_note"]
    assert "not listed in the original Xcode project" in state["replay_note"]


def test_an_edited_swift_test_the_xcode_project_already_lists_still_replays(tmp_path):
    """Only *new* files are unreplayable; a fix usually extends an existing suite."""
    xcode_workspace(tmp_path)
    state = {"gate": XCODE_GATE}
    workflows.snapshot(str(tmp_path), state)
    (tmp_path / "AppTests" / "ExistingTests.swift").write_text("func testOld() { stronger }\n", encoding="utf-8")
    assert workflows.replay_fails(str(tmp_path), state) is True
    assert "replay_note" not in state


def test_a_synchronized_xcode_group_compiles_new_files_so_the_replay_runs(tmp_path):
    xcode_workspace(tmp_path, SYNCED_PBXPROJ)
    state = {"gate": XCODE_GATE}
    workflows.snapshot(str(tmp_path), state)
    (tmp_path / "AppTests" / "NewTests.swift").write_text("func testNew() {}\n", encoding="utf-8")
    assert workflows.replay_fails(str(tmp_path), state) is True
    assert "replay_note" not in state


def test_a_project_whose_gate_is_not_xcode_keeps_its_replay(tmp_path):
    """A React Native repo has an .xcodeproj and runs its tests with jest.

    Bailing out because an .xcodeproj exists somewhere would trade a real
    check — the one this whole rule rests on — for "could not run" in every
    such project, and in every Swift package built with `swift test`, both of
    which find a new test file by walking the tree.
    """
    xcode_workspace(tmp_path)
    (tmp_path / "__tests__").mkdir()
    state = {"gate": "test ! -f __tests__/new.test.js"}      # jest, in miniature
    workflows.snapshot(str(tmp_path), state)
    (tmp_path / "__tests__" / "new.test.js").write_text("it('works', () => {})\n", encoding="utf-8")
    assert workflows.replay_fails(str(tmp_path), state) is True
    assert "replay_note" not in state


def test_a_new_javascript_test_beside_an_xcode_project_still_replays(tmp_path):
    """Even under an Xcode gate, only what Xcode compiles is Xcode's problem."""
    xcode_workspace(tmp_path)
    (tmp_path / "__tests__").mkdir()
    state = {"gate": XCODE_GATE}
    workflows.snapshot(str(tmp_path), state)
    (tmp_path / "__tests__" / "new.test.js").write_text("it('works', () => {})\n", encoding="utf-8")
    assert workflows.replay_fails(str(tmp_path), state) is True
    assert "replay_note" not in state


def test_the_reason_a_replay_could_not_run_reaches_the_verdict(tmp_path):
    """"No replay was possible" on its own is a shrug; the pair needs the why."""
    (tmp_path / "App.swift").write_text("x\n", encoding="utf-8")
    wf = workflows.get("fix")
    state = {"gate": "true"}
    wf.begin(str(tmp_path), state)
    veto = wf.veto(str(tmp_path), state)
    assert veto and "nothing to replay" in veto
    assert "nothing to replay" in wf.proven(state)
