"""Finding the test command, without inventing one.

A wrong gate that always passes is worse than no gate: it looks like
verification and is not. So every case here is either a real signal or a None.
"""

import json
import os
import sys

import pytest

from duet.gate import describe, detect

# Captured at import. Several tests below replace `sys.executable` with an
# interpreter that has no pytest, and that patch is visible everywhere — so
# the real one has to be remembered before any of them runs.
REAL_PYTHON = sys.executable

# These fake a virtualenv's programs as `#!/bin/sh` scripts in `<venv>/bin`. A
# Windows virtualenv keeps real `.exe` files in `Scripts`, which no script can
# stand in for; the Windows layout and quoting are covered in test_windows.py.
POSIX_VENV = pytest.mark.skipif(os.name == "nt", reason="fakes POSIX virtualenv binaries "
                                "(<venv>/bin/<name> as sh scripts); Windows venvs hold .exe files")


@pytest.fixture
def tools_present(monkeypatch):
    """Pretend every tool is installed.

    These cases test the mapping from project marker to command. Whether the
    command can actually start is a separate concern, covered below — and a
    machine without cargo or maven should not fail the mapping tests.
    """
    monkeypatch.setattr("duet.gate.shutil.which", lambda name: "/usr/bin/" + name)


def test_a_python_project_with_tests(tmp_path, tools_present):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "tests").mkdir()
    assert detect(str(tmp_path)) == "pytest -q"


def test_pyproject_without_any_tests_is_not_enough(tmp_path, tools_present):
    """A pyproject with no tests would give a gate that passes vacuously."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect(str(tmp_path)) is None


def test_loose_test_files_count(tmp_path, tools_present):
    (tmp_path / "test_thing.py").write_text("def test_ok():\n    assert True\n")
    assert detect(str(tmp_path)) == "pytest -q"


def test_a_swift_test_directory_is_not_a_python_one(tmp_path, tools_present):
    """macOS matches filenames without regard to case, so `(root / "tests")`
    was also Xcode's `Tests/`, and an iOS repo with no --gate was handed
    `pytest -q`: it collects nothing, exits 5, and is red every round, which
    blocks both sign-offs for the whole session."""
    (tmp_path / "Tests").mkdir()
    (tmp_path / "Tests" / "AppLogicTests.swift").write_text("func testScore() {}\n")
    assert detect(str(tmp_path)) is None


def test_a_tests_directory_with_python_in_it_still_counts(tmp_path, tools_present):
    """Nested layouts included — the old check never looked inside at all."""
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_deep.py").write_text("def test_ok():\n    assert True\n")
    assert detect(str(tmp_path)) == "pytest -q"


def test_npm_test_script(tmp_path, tools_present):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert detect(str(tmp_path)) == "npm test"


def test_the_npm_placeholder_script_is_ignored(tmp_path, tools_present):
    """`npm init` writes a test script that only prints an error and exits 1 —
    taking it would gate every session on a guaranteed failure."""
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert detect(str(tmp_path)) is None


def test_npm_falls_back_to_check_then_ci(tmp_path, tools_present):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"check": "tsc --noEmit"}}))
    assert detect(str(tmp_path)) == "npm run check"


def test_a_makefile_needs_an_actual_test_target(tmp_path, tools_present):
    (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n")
    assert detect(str(tmp_path)) is None
    (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n\ntest:\n\t./run_tests\n")
    assert detect(str(tmp_path)) == "make test"


def test_a_makefile_is_no_use_without_make(tmp_path, monkeypatch):
    """Fresh macOS without the command line tools, or a slim container, has no
    `make`. The Makefile branch returned early and skipped the runnability
    check every other marker goes through, so it handed back a gate that could
    not start — and a gate that cannot start fails every round and vetoes both
    agents forever."""
    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    (tmp_path / "Makefile").write_text("test:\n\t./run_tests\n")
    assert detect(str(tmp_path)) is None


def test_other_ecosystems(tmp_path, tools_present):
    for marker, expected in [("Cargo.toml", "cargo test"), ("go.mod", "go test ./..."),
                             ("mix.exs", "mix test"), ("pom.xml", "mvn -q test")]:
        root = tmp_path / marker.replace(".", "_")
        root.mkdir()
        (root / marker).write_text("x")
        assert detect(str(root)) == expected


def test_an_empty_directory_gets_nothing(tmp_path):
    assert detect(str(tmp_path)) is None
    assert "no test command found" in describe(None)


def test_a_broken_package_json_does_not_raise(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert detect(str(tmp_path)) is None


# --- a gate that cannot start is worse than no gate -------------------------

def test_a_detected_gate_that_cannot_start_is_not_returned(tmp_path, monkeypatch):
    """Both agents caught this in a live session: `pytest -q` was detected on a
    machine where pytest lives only inside a virtualenv, so the gate failed
    every turn and no session could ever reach consensus, however right the
    pair were. Worse than no gate, because it blocks the finish."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    monkeypatch.setattr("duet.gate._python_module_form", lambda command: None)
    assert detect(str(tmp_path)) is None


def test_pytest_falls_back_to_the_interpreter_that_has_it(tmp_path, monkeypatch):
    """pytest is usually in a virtualenv rather than on PATH, and the python
    running duet is the one most likely to have it."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    found = detect(str(tmp_path))
    assert found is not None
    assert "-m pytest" in found and found.endswith("-q")


# --- the project's own virtualenv is where its pytest lives -----------------

def _executable(path, body="#!/bin/sh\nexit 0\n"):
    """A stand-in for a program that starts and answers --version."""
    import stat

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _a_python_project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "tests").mkdir(exist_ok=True)
    return root


@pytest.fixture
def duet_has_no_pytest(tmp_path_factory, monkeypatch):
    """duet installed by pipx, or by its own installer into ~/.duet/venv.

    That virtualenv holds duet and nothing else, so `<this python> -m pytest`
    does not start — which was the whole of duet's fallback.
    """
    fake = _executable(tmp_path_factory.mktemp("duet-venv") / "python",
                       "#!/bin/sh\nexit 1\n")
    monkeypatch.setattr("duet.gate.sys.executable", str(fake))
    return fake


@POSIX_VENV
def test_the_projects_own_virtualenv_is_found_when_duet_has_no_pytest(
        tmp_path, monkeypatch, duet_has_no_pytest):
    """The reported bug: an ordinary Python project, pytest in `.venv`, duet
    installed with pipx — and duet reported no test command at all, so `duet
    add` refused to start on a project that tests itself fine."""
    _a_python_project(tmp_path)
    pytest_exe = _executable(tmp_path / ".venv" / "bin" / "pytest")

    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    assert detect(str(tmp_path)) == "%s -q" % pytest_exe.resolve()


@POSIX_VENV
def test_the_projects_virtualenv_beats_duets_interpreter(tmp_path, monkeypatch):
    """Even when duet's own python has pytest, it is the wrong one: it cannot
    import the project's dependencies, so the gate would collect errors every
    round. The project's virtualenv is where those dependencies are."""
    _a_python_project(tmp_path)
    pytest_exe = _executable(tmp_path / ".venv" / "bin" / "pytest")

    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    assert detect(str(tmp_path)) == "%s -q" % pytest_exe.resolve()


@POSIX_VENV
def test_the_projects_virtualenv_beats_a_pytest_on_path(tmp_path, tools_present):
    """And it beats a pytest on PATH too, which is the case that bites.

    A global pytest — homebrew, pipx, the system one — starts perfectly well
    and then cannot import the project's dependencies, so the gate collects
    errors every round: the same unwinnable session `_runnable` exists to
    prevent, arrived at from the other side. Pinned because nothing else in
    this file would notice `_project_venv_form` being moved after `_runnable`
    in `usable()`; every other venv case here has PATH empty.
    """
    _a_python_project(tmp_path)
    pytest_exe = _executable(tmp_path / ".venv" / "bin" / "pytest")

    assert detect(str(tmp_path)) == "%s -q" % pytest_exe.resolve()


@POSIX_VENV
@pytest.mark.parametrize("venv", ["venv", "env"])
def test_venv_and_env_count_too_and_python_dash_m_is_enough(
        tmp_path, monkeypatch, duet_has_no_pytest, venv):
    """A virtualenv with pytest installed as a library but no `bin/pytest`
    console script still runs the tests, via `bin/python -m pytest`."""
    _a_python_project(tmp_path)
    python = _executable(tmp_path / venv / "bin" / "python")

    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    assert detect(str(tmp_path)) == "%s -m pytest -q" % python.resolve()


def test_a_python3_on_path_is_the_last_resort(tmp_path, monkeypatch,
                                              duet_has_no_pytest):
    """No project virtualenv and duet's python has no pytest — but the machine
    has a python3 that does. That is still a gate; reporting none is not."""
    _a_python_project(tmp_path)
    monkeypatch.setattr(
        "duet.gate.shutil.which",
        lambda name: REAL_PYTHON if name in ("python3", "python") else None)
    assert detect(str(tmp_path)) == "%s -m pytest -q" % REAL_PYTHON


def test_a_virtualenv_without_pytest_is_not_used(tmp_path, monkeypatch):
    """Conservative as before: the directory existing proves nothing. Nothing
    is handed back that has not been seen to start."""
    _a_python_project(tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    _executable(tmp_path / ".venv" / "bin" / "python", "#!/bin/sh\nexit 1\n")

    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    assert detect(str(tmp_path)) == "%s -m pytest -q" % REAL_PYTHON


@POSIX_VENV
def test_a_virtualenv_path_with_a_space_is_quoted(tmp_path, monkeypatch,
                                                  duet_has_no_pytest):
    """`duet` is often run on a project under "~/My Projects/…". An unquoted
    path makes the gate's first word half a path, and duet refuses to start a
    session on a gate whose program it cannot find."""
    from duet.gate import _first_word, why_unusable

    root = _a_python_project(tmp_path / "my project")
    pytest_exe = _executable(root / ".venv" / "bin" / "pytest")

    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    found = detect(str(root))
    assert found is not None
    assert _first_word(found) == str(pytest_exe.resolve())
    assert why_unusable(found, str(root)) is None


def test_a_relative_launcher_in_the_project_counts(tmp_path, monkeypatch):
    import os
    import stat

    (tmp_path / "build.gradle").write_text("x")
    launcher = tmp_path / "gradlew"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    assert detect(str(tmp_path)) == "./gradlew test"


def test_an_npm_script_is_dropped_when_npm_is_missing(tmp_path, monkeypatch):
    import json as _json

    (tmp_path / "package.json").write_text(_json.dumps({"scripts": {"test": "jest"}}))
    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    assert detect(str(tmp_path)) is None


def test_an_unrunnable_gate_is_refused_before_the_session_starts(tmp_path, capsys):
    """Discovered the expensive way: a session ran eight rounds, hit arbitration,
    and could never have finished, because its gate could not start and a failing
    gate vetoes both agents. Refusing takes a second."""
    from duet.cli import main

    code = main(["run", "do a thing", "-C", str(tmp_path), "--gate", "definitely-not-installed"])
    assert code == 3
    out = capsys.readouterr().out
    assert "cannot run" in out
    assert "could never finish" in out
    assert "--no-gate" in out                 # and both ways out are named
    assert "--gate" in out


def test_a_runnable_gate_is_not_refused(tmp_path, monkeypatch):
    from duet.gate import why_unusable

    assert why_unusable("echo hello", str(tmp_path)) is None
    assert why_unusable("", str(tmp_path)) is None
    assert why_unusable("CI=1 echo hello", str(tmp_path)) is None


def test_a_project_local_launcher_is_runnable(tmp_path):
    import stat

    from duet.gate import why_unusable

    launcher = tmp_path / "run-tests.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    assert why_unusable("run-tests.sh", str(tmp_path)) is None
    assert why_unusable("missing.sh", str(tmp_path)) is not None


@pytest.mark.skipif(os.name == "nt", reason="single quotes and /bin/sh are POSIX shell; "
                    "cmd.exe splitting is tested in test_windows.py")
def test_a_quoted_interpreter_path_is_not_mistaken_for_a_missing_program():
    """`command.split()[0]` breaks on any gate whose program is quoted.

    The greenfield starter shell-quotes sys.executable, so an interpreter at
    "/opt/some where/python3" yielded the first "word" `'/opt/some`, which is
    on nobody's PATH — and duet refused to start the session over it.
    """
    from duet.gate import _first_word, why_unusable

    assert _first_word("'/opt/some where/python3' -c 'x'") == "/opt/some where/python3"
    assert _first_word("pytest -q") == "pytest"
    assert _first_word("") == ""
    # unbalanced quotes are the shell's problem, not a crash here
    assert _first_word('pytest "-q') == 'pytest'
    assert why_unusable("'/bin/sh' -c true", "/tmp") is None
