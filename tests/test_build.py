"""Starting from nothing, without a gate that passes nothing.

The greenfield case is where duet's own mechanism used to lapse: no tests
exist, so nothing is detected, and the pair can only agree by argument. The
risk in fixing that is worse than the gap — a starter gate that goes green on
an empty directory would be a false proof, which is exactly what the double
sign-off is meant to rule out. So the cases here are mostly about staying red.
"""

import argparse
import subprocess

import pytest

from duet import build


def run_gate(command, cwd):
    proc = subprocess.run(command, shell=True, cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture
def no_pytest(monkeypatch):
    """A machine with no test runner on PATH — the fallback's own case."""
    monkeypatch.setattr("duet.build.shutil.which", lambda name: None)


def test_greenfield_gate_is_red_before_any_test_exists(tmp_path, no_pytest):
    code, output = run_gate(build.starter_gate(str(tmp_path)), tmp_path)
    assert code != 0, "an empty directory must not pass its own gate"
    assert "no tests ran" in output


def test_the_same_gate_goes_green_on_a_real_test(tmp_path, no_pytest):
    (tmp_path / "test_thing.py").write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(1, 1)\n"
    , encoding="utf-8")
    code, _ = run_gate(build.starter_gate(str(tmp_path)), tmp_path)
    assert code == 0


def test_a_failing_test_keeps_it_red(tmp_path, no_pytest):
    (tmp_path / "test_thing.py").write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_no(self):\n"
        "        self.assertEqual(1, 2)\n"
    , encoding="utf-8")
    code, _ = run_gate(build.starter_gate(str(tmp_path)), tmp_path)
    assert code != 0


def test_the_fallback_survives_a_python_path_with_spaces(tmp_path, no_pytest, monkeypatch):
    # The gate runs through the platform's shell: sh takes single quotes,
    # cmd.exe only double quotes.
    monkeypatch.setattr("duet.build.sys.executable", "/opt/some where/python3")
    monkeypatch.setattr("duet.winlaunch.WINDOWS", False)
    assert "'/opt/some where/python3'" in build.starter_gate(str(tmp_path))
    monkeypatch.setattr("duet.winlaunch.WINDOWS", True)
    assert '"/opt/some where/python3"' in build.starter_gate(str(tmp_path))


def test_an_existing_project_shape_wins_over_the_python_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("duet.build.shutil.which", lambda name: "/usr/bin/" + name)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert build.starter_gate(str(tmp_path)) == "npm test"


def test_a_starter_is_not_offered_for_a_runner_that_is_not_installed(tmp_path, no_pytest):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    # No cargo on this machine, so a `cargo test` gate would fail every round
    # and veto consensus outright. The fallback has to be something runnable.
    assert "cargo" not in build.starter_gate(str(tmp_path))


def test_pytest_is_preferred_when_it_is_there(tmp_path, monkeypatch):
    monkeypatch.setattr("duet.build.shutil.which",
                        lambda name: "/usr/bin/pytest" if name == "pytest" else None)
    assert build.starter_gate(str(tmp_path)) == "pytest -q"


def test_the_task_puts_the_criteria_before_the_code(tmp_path):
    task = build.task_for("  a URL shortener  ", "pytest -q")
    assert "a URL shortener" in task
    assert task.index("AGREE WHAT DONE MEANS") < task.index("THEN BUILD")
    assert task.index("WRITE THE TESTS FIRST") < task.index("THEN BUILD")


def test_the_task_names_the_gate_the_pair_has_to_satisfy(tmp_path):
    """They were being told a gate existed without being told what it runs.

    A pair that cannot see the command writes tests it cannot see either — the
    reviewer's case was a Rust idea handed a Python gate, with nothing in the
    task that would let either of them notice.
    """
    task = build.task_for("a URL shortener", "cargo test")
    assert "cargo test" in task
    assert task.index("cargo test") < task.index("THEN BUILD")


def test_the_task_says_to_name_the_runtime_the_criteria_assume(tmp_path):
    # A live session ended in consensus on a CLI that crashed on Python 3.9,
    # because the criteria never named a version and the gate ran on 3.12.
    task = build.task_for("a URL shortener", "pytest -q")
    assert "Name the environment" in task
    assert "supported version" in task


def test_build_refuses_to_nest_and_names_its_own_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DUET_SESSION", "20250101-000000-aaaa")
    args = argparse.Namespace(
        command="build", idea=["a thing"], root=str(tmp_path), gate=None,
        no_gate=False, allow_nested=False,
    )
    assert build.run(args) == 4
    out = capsys.readouterr().out
    # Telling a `duet build` user to retry with `duet run` would name a command
    # that does something else.
    assert "duet build --allow-nested" in out
    assert "gate" not in out.lower()


def test_an_empty_idea_is_refused_before_any_agent_is_started(tmp_path, capsys):
    args = argparse.Namespace(
        command="build", idea=[], root=str(tmp_path), gate=None,
        no_gate=False, allow_nested=False,
    )
    assert build.run(args) == 2
    assert "no idea given" in capsys.readouterr().out


def test_the_gate_line_does_not_claim_an_empty_workspace_when_tests_exist(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "test_thing.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    build.announce("pytest -q", "detected")
    out = capsys.readouterr().out
    assert "nothing here to test yet" not in out
    assert "own test command" in out


def test_the_gate_line_says_so_on_a_real_greenfield(capsys):
    build.announce("pytest -q", "starter")
    assert "nothing here to test yet" in capsys.readouterr().out


def test_no_gate_is_stated_plainly_rather_than_left_silent(capsys):
    build.announce("", "yours")
    assert "no gate" in capsys.readouterr().out


# -- what the reviewer found ------------------------------------------------
# Six findings against the first version of this module. These pin the four
# that were real, plus the two the reviewer could not run and I did.

def test_a_conventional_tests_directory_is_actually_found(tmp_path, no_pytest):
    """`unittest discover` cannot see tests/ without __init__.py.

    Checked on 3.9 and 3.12: both report zero tests, and so does discovery
    started at tests/ with top_level_dir — the fix the review suggested. So the
    pair would put tests where every Python project puts them, and the gate
    would tell them no tests existed. The runner imports by path instead.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_thing.py").write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(1, 1)\n"
    , encoding="utf-8")
    code, output = run_gate(build.starter_gate(str(tmp_path)), tmp_path)
    assert code == 0, output


def test_a_test_file_that_will_not_import_keeps_the_gate_red(tmp_path, no_pytest):
    (tmp_path / "test_broken.py").write_text("import a_module_that_is_not_installed\n", encoding="utf-8")
    code, output = run_gate(build.starter_gate(str(tmp_path)), tmp_path)
    assert code != 0
    # and says which file, rather than reporting "no tests" and blaming the pair
    assert "could not import test_broken.py" in output


def test_an_idea_in_another_language_does_not_get_a_python_gate(monkeypatch):
    monkeypatch.setattr("duet.build.shutil.which", lambda name: "/usr/bin/" + name)
    assert build.starter_gate("/tmp", "a CLI in Rust that renames photos") == "cargo test"
    assert build.starter_gate("/tmp", "a Go service for uploads").startswith("go test")


def test_a_language_word_in_ordinary_english_is_not_a_language(monkeypatch):
    # "a tool to go through photos" is not a Go project, and a wrong gate here
    # is one the pair could never turn green.
    monkeypatch.setattr("duet.build.shutil.which", lambda name: "/usr/bin/" + name)
    for idea in ("a tool to go through photos", "a tree node visualiser"):
        assert build.language_gate(idea) == (None, None)


def test_a_named_language_with_no_toolchain_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr("duet.build.shutil.which", lambda name: None)
    command, missing = build.language_gate("a CLI in Rust")
    assert command is None and missing == "cargo"


def test_a_gate_that_passes_an_empty_workspace_is_caught(tmp_path):
    assert build.gate_proves_nothing("true", str(tmp_path)) is True
    assert build.gate_proves_nothing("false", str(tmp_path)) is False


def test_a_passing_gate_is_fine_once_tests_exist(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    # The check is "green with nothing to have passed", not "green".
    assert build.gate_proves_nothing("true", str(tmp_path)) is False


def test_an_xcode_project_is_not_called_a_workspace_with_no_tests(tmp_path):
    """`has_tests` was a hand-copy of the workflow rules' detector, and drifted.

    It knew no Swift and looked for `tests/` at the top level only, matched
    case-sensitively — so an iOS repo read as empty of tests. A green
    `xcodebuild` gate there was called "green with nothing here to have
    passed", and a *detected* gate (a Makefile whose `test:` target runs
    xcodebuild) made `duet build` refuse to start with exit 3.
    """
    (tmp_path / "GalleryTests").mkdir()
    (tmp_path / "GalleryTests" / "AppLogicTests.swift").write_text(
        "final class AppLogicTests: XCTestCase {\n    func testScore() {}\n}\n", encoding="utf-8")
    assert build.has_tests(str(tmp_path)) is True
    assert build.gate_proves_nothing("true", str(tmp_path)) is False


def test_one_detector_answers_for_both_the_rules_and_the_gate_check(tmp_path):
    """Two lists drifted apart once already; this is the same list, asked twice."""
    for rel in ("Tests/FooTests.swift", "src/CalculatorTests.cs", "lib/widget_test.dart"):
        workspace = tmp_path / rel.replace("/", "_")
        (workspace / rel).parent.mkdir(parents=True)
        (workspace / rel).write_text("x", encoding="utf-8")
        assert build.has_tests(str(workspace)) is True, rel
    empty = tmp_path / "ordinary-source"
    (empty / "Sources").mkdir(parents=True)
    (empty / "Sources" / "App.swift").write_text("x", encoding="utf-8")
    (empty / "Contests").mkdir()
    (empty / "Contests" / "Entry.swift").write_text("x", encoding="utf-8")
    assert build.has_tests(str(empty)) is False


def test_go_and_cargo_gates_are_wrapped_so_zero_tests_cannot_pass():
    wrapped = build.proof_against_zero_tests("go test ./...")
    assert wrapped != "go test ./..."
    assert "ok " in wrapped
    assert build.proof_against_zero_tests("pytest -q") == "pytest -q"


def test_the_gate_line_is_not_a_lie_when_the_config_supplies_one(capsys):
    build.announce("pytest -q", "configured")
    out = capsys.readouterr().out
    assert "no gate" not in out
    assert "config.json" in out


def test_a_gate_you_chose_yourself_is_a_warning_not_a_refusal(capsys):
    """duet refuses its own bad choices and warns about yours.

    `has_tests` reads a handful of conventions and will not recognise every
    layout, so refusing a user's explicit --gate would leave a project whose
    tests duet cannot see with no way to start a session at all.
    """
    build.warn_vacuous_gate("true", mine=False)
    out = capsys.readouterr().out
    assert "yours, so it stands" in out
    assert "--gate" not in out          # nothing to fix; they already chose

    build.warn_vacuous_gate("true", mine=True)
    out = capsys.readouterr().out
    assert "yours, so it stands" not in out
    assert "--gate" in out              # duet picked it, so duet offers the way out


def test_the_summary_names_what_now_exists(tmp_path, capsys):
    """A build ends with a project nobody has seen yet, not just a digest."""
    (tmp_path / "app.py").write_text("print('hi')\n" * 3, encoding="utf-8")
    (tmp_path / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / ".duet").mkdir()
    (tmp_path / ".duet" / "noise.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.pyc").write_bytes(b"\x00\x01")

    build.summarise(str(tmp_path), 125.0, "pytest -q")
    out = capsys.readouterr().out
    assert "2m 05s" in out
    assert "app.py" in out and "test_app.py" in out
    # duet's own directory is not part of what the pair built
    assert "noise.py" not in out and ".pyc" not in out
    assert "pytest -q" in out


def test_the_summary_says_nothing_when_nothing_was_built(tmp_path, capsys):
    build.summarise(str(tmp_path), 10.0, "pytest -q")
    assert capsys.readouterr().out == ""


def test_the_summary_does_not_count_a_dependency_tree(tmp_path, capsys):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    for junk in ("node_modules", ".venv"):
        (tmp_path / junk).mkdir()
        (tmp_path / junk / "vendored.py").write_text("y = 2\n" * 500, encoding="utf-8")
    build.summarise(str(tmp_path), 10.0, "")
    out = capsys.readouterr().out
    assert "1 files" in out
    assert "vendored.py" not in out


def test_json_mode_keeps_stdout_machine_readable(tmp_path, capsys, monkeypatch):
    """`duet build --json` printed prose above the event stream.

    Anything reading stdout a line at a time hit the gate line before its
    first JSON object. The lines still exist — they go to stderr.
    """
    import argparse

    args = argparse.Namespace(json=True)
    with build.human_output(args):
        build.announce("pytest -q", "starter")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pytest -q" in captured.err

    args.json = False
    with build.human_output(args):
        build.announce("pytest -q", "starter")
    captured = capsys.readouterr()
    assert "pytest -q" in captured.out


def test_the_summary_does_not_claim_a_binary_file_has_lines(tmp_path, capsys):
    """A SQLite file a test left behind was reported as "7 lines"."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "data.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 3000)
    build.summarise(str(tmp_path), 10.0, "")
    out = capsys.readouterr().out
    assert "data.db" in out
    assert "bytes" in out or "KB" in out
    # two files, but only the text one contributes lines
    assert "2 files, 1 lines" in out


def test_the_task_asks_for_the_defaults_that_have_consequences():
    """A sonnet pair shipped a service on 0.0.0.0 without discussing it.

    Not a decision either agent argued for — one neither mentioned. An
    unstated default is the one thing neither the gate nor a peer can object
    to, so the task asks for them out loud.
    """
    task = build.task_for("a URL shortener", "pytest -q")
    assert "binds to" in task
    assert task.index("binds to") < task.index("THEN BUILD")


def test_the_summary_ends_with_the_command_to_run_what_was_built(tmp_path, capsys):
    """An expense-tracker build ended with 2,963 lines and no way in.

    Finding `python3 run.py` meant reading a 51-criterion spec. The task now
    asks the pair for a "How to run it" section, and the summary prints its
    first command — read from the README, never guessed.
    """
    (tmp_path / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# Thing\n\nIntro.\n\n```\npip install nothing\n```\n\n"
        "## How to run it\n\n```sh\n$ python3 run.py   # http://127.0.0.1:8000/\n"
        "python3 run.py --port 9000\n```\n", encoding="utf-8")
    build.summarise(str(tmp_path), 60.0, "")
    out = capsys.readouterr().out
    assert "run it:" in out
    assert "python3 run.py" in out
    assert "pip install" not in out          # not the first block in the file
    assert "8000" not in out                 # the comment is not the command


def test_no_run_section_means_no_guess(tmp_path, capsys):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Thing\n\n```\npython3 app.py\n```\n", encoding="utf-8")
    build.summarise(str(tmp_path), 60.0, "")
    assert "run it:" not in capsys.readouterr().out


def test_the_build_task_asks_for_a_way_in():
    assert "How to run it" in build.task_for("a thing", "pytest -q")
