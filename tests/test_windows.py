"""What breaks on Windows, reproduced on any platform.

The suite runs on macOS and Linux here, so each test builds the Windows
situation itself: a CreateProcess that refuses batch files, a .gitattributes
rule read through git, and a scan for text I/O that leaves the encoding to the
platform default (cp1252 on Windows).
"""

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from duet.adapters.claude_code import ClaudeCodeAdapter
from duet.adapters.codex_cli import CodexCliAdapter

ROOT = Path(__file__).resolve().parent.parent
REAL_RUN = subprocess.run


def pretend_windows(monkeypatch):
    """Make duet launch as it would on Windows, and make the OS behave like it.

    CreateProcess cannot execute a .cmd or .bat file: it fails with
    "[WinError 193] %1 is not a valid Win32 application". That is the failure
    every npm-installed CLI hit, so the fake run refuses them the same way.
    """
    try:
        from duet import winlaunch
        monkeypatch.setattr(winlaunch, "WINDOWS", True)
    except ImportError:
        pass        # the code before the fix had no Windows launcher at all

    def run(args, *rest, **kwargs):
        program = args[0] if isinstance(args, (list, tuple)) else ""
        if str(program).lower().endswith((".cmd", ".bat")):
            raise OSError(193, "%1 is not a valid Win32 application")
        return REAL_RUN(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


FAKE_CLI = r'''
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[2:], fh)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "ok\n```json\n{\"message\":\"done\",\"verdict\":\"DONE\"}\n```",
                  "session_id": "s-1"}))
'''


def npm_shim(folder: Path, name: str, dump: Path) -> Path:
    """A shim shaped like the one `npm install -g` writes, forwarding %* on."""
    (folder / "fake_cli.py").write_text(FAKE_CLI, encoding="utf-8")
    shim = folder / (name + ".cmd")
    shim.write_text(
        "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%%~dp0\r\nEXIT /b\r\n:start\r\n"
        "SETLOCAL\r\nCALL :find_dp0\r\n"
        'endLocal & goto #_undefined_# 2>NUL || title %%COMSPEC%% & "%s" "%%dp0%%\\fake_cli.py" "%s" %%*\r\n'
        % (sys.executable, dump),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


# -- 3. npm's .cmd shims ----------------------------------------------------
def test_a_claude_installed_by_npm_on_windows_runs_with_a_multi_line_prompt(tmp_path, monkeypatch):
    pretend_windows(monkeypatch)
    dump = tmp_path / "args.json"
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path), config={"extra_args": []})
    agent.bin = str(npm_shim(tmp_path, "claude", dump))

    prompt = 'line one\nline two & "quoted" 100% | <tag>'
    reply = agent.send(prompt, system="sys")

    assert reply.ok, reply.error
    args = json.loads(dump.read_text(encoding="utf-8"))
    assert args[args.index("-p") + 1] == prompt     # every character, newline included


def test_a_codex_installed_by_npm_on_windows_is_started(tmp_path, monkeypatch):
    pretend_windows(monkeypatch)
    dump = tmp_path / "args.json"
    agent = CodexCliAdapter(name="codex", cwd=str(tmp_path), config={"resume": False})
    agent.bin = str(npm_shim(tmp_path, "codex", dump))

    agent.send("first line\nsecond line")

    args = json.loads(dump.read_text(encoding="utf-8"))
    assert args[0] == "exec" and any("second line" in a for a in args)


def test_a_cli_that_cannot_be_started_is_reported_not_raised(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(193, "%1 is not a valid Win32 application")
    monkeypatch.setattr(subprocess, "run", refuse)
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = str(tmp_path / "claude.exe")
    reply = agent.send("go")
    assert not reply.ok and "not a valid Win32 application" in reply.error


def test_a_batch_file_that_is_not_a_shim_goes_through_cmd_escaped(tmp_path, monkeypatch):
    from duet import winlaunch
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    bat = tmp_path / "tool.bat"
    bat.write_text("@echo off\r\necho %1\r\n", encoding="utf-8")
    bat.chmod(0o755)

    line = winlaunch.command([str(bat), "a&b", 'say "hi"', "50%"], windows=True)

    assert isinstance(line, str)        # verbatim: a list would be re-quoted for C, not cmd
    assert line.startswith(r'"C:\Windows\system32\cmd.exe" /d /s /c "')
    assert "a&b" not in line and "^^^&" in line        # escaped for both cmd passes
    assert '\\^^^"hi\\^^^"' in line and "50^^^%" in line
    with pytest.raises(OSError, match="spans lines"):
        winlaunch.command([str(bat), "two\nlines"], windows=True)


def test_off_windows_a_command_is_left_alone(tmp_path):
    from duet import winlaunch
    shim = tmp_path / "claude.cmd"
    assert winlaunch.command([str(shim), "-p", "x"], windows=False) == [str(shim), "-p", "x"]


# -- 4. line endings --------------------------------------------------------
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git to read .gitattributes")
@pytest.mark.parametrize("path", ["install.sh", "duet/skill/SKILL.md", "duet/cli.py"])
def test_files_whose_line_endings_matter_are_checked_out_lf_everywhere(path):
    proc = REAL_RUN(["git", "check-attr", "eol", "--", path], cwd=str(ROOT),
                    capture_output=True, text=True, encoding="utf-8")
    assert proc.stdout.strip().endswith("eol: lf"), proc.stdout + proc.stderr


# -- 1. encodings -----------------------------------------------------------
def _keywords(call: ast.Call) -> dict:
    return {k.arg: k.value for k in call.keywords if k.arg}


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def encoding_left_to_the_platform(source: str):
    """Every text read, write and subprocess pipe here that names no encoding."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func, kw = node.func, _keywords(node)
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        owner = getattr(getattr(func, "value", None), "id", "")
        if "encoding" in kw:
            continue
        if name in ("read_text", "write_text"):
            found.append((node.lineno, name))
        elif name == "open" and owner != "os":
            # builtin open(path, mode) or Path.open(mode)
            position = 0 if isinstance(func, ast.Attribute) else 1
            mode = kw.get("mode") or (node.args[position] if len(node.args) > position else None)
            text = mode.value if isinstance(mode, ast.Constant) else "r"
            if "b" not in str(text):
                found.append((node.lineno, "open"))
        elif owner == "subprocess" and name in ("run", "Popen", "check_output", "call"):
            if _is_true(kw.get("text")) or _is_true(kw.get("universal_newlines")):
                found.append((node.lineno, "subprocess.%s" % name))
    return found


def test_every_text_file_and_pipe_duet_reads_says_utf_8():
    """On Windows the default is cp1252, which turned "—" into "�" in reports."""
    missing = []
    for path in sorted((ROOT / "duet").rglob("*.py")):
        for line, what in encoding_left_to_the_platform(path.read_text(encoding="utf-8")):
            missing.append("%s:%d %s" % (path.relative_to(ROOT), line, what))
    assert not missing, "no encoding given:\n" + "\n".join(missing)


def test_the_scan_catches_what_it_is_meant_to():
    assert encoding_left_to_the_platform("p.read_text()\nopen(x, 'w')\n"
                                         "subprocess.run(a, text=True)\n") == [
        (1, "read_text"), (2, "open"), (3, "subprocess.run")]
    assert encoding_left_to_the_platform("open(x, 'rb')\nos.open(x, 1)\n"
                                         "subprocess.run(a)\np.read_text(encoding='utf-8')\n") == []


def test_a_status_check_decodes_the_cli_as_utf_8(monkeypatch):
    from duet.adapters import base
    seen = {}

    def run(args, *rest, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    base.run_status_check(["claude", "auth", "status"], 5)
    assert seen.get("encoding") == "utf-8" and seen.get("errors") == "replace"


# -- 2. cmd.exe quoting and env-var syntax -----------------------------------
def test_quote_for_shell_does_not_single_quote_a_windows_path():
    """`shlex.quote` wraps any path with a backslash in single quotes, which
    cmd.exe does not strip as quoting at all — the gate then tried to run a
    program literally named `'C:\\...\\pytest.exe'`, quotes included, and
    failed with "The filename ... is incorrect". cmd.exe only needs quoting
    for a space, and only understands double quotes for it."""
    from duet import winlaunch

    assert winlaunch.quote_for_shell(r"C:\Users\run\pytest.exe", windows=True) == \
        r"C:\Users\run\pytest.exe"
    assert winlaunch.quote_for_shell(r"C:\My Tools\pytest.exe", windows=True) == \
        r'"C:\My Tools\pytest.exe"'
    # Off Windows, the old POSIX-safe behaviour is unchanged.
    assert winlaunch.quote_for_shell(r"/opt/some where/python3", windows=False) == \
        "'/opt/some where/python3'"


def test_gate_commands_use_cmd_quoting_on_windows(monkeypatch, tmp_path):
    """The same bug, from the gate's own command-building: a detected
    project virtualenv's pytest must not come back wrapped in single quotes
    when the gate that string is run through is cmd.exe.

    `shlex.quote` decides to quote by scanning for characters outside its
    POSIX-safe set, and a backslash is one of them regardless of which OS is
    running it — so a path with a backslash in it (every real Windows path
    has one; this one fakes it with a literal backslash in a directory name,
    which POSIX filesystems allow) reproduces the exact bug on any platform.
    """
    from duet import gate

    try:
        from duet import winlaunch
        monkeypatch.setattr(winlaunch, "WINDOWS", True)
    except ImportError:
        pass        # the code before the fix had no notion of "Windows mode"
    monkeypatch.setattr(gate, "_can_start", lambda argv: True)

    bindir = tmp_path / "with\\backslash" / ".venv" / "Scripts"
    bindir.mkdir(parents=True)
    tool = bindir / "pytest.exe"
    tool.write_text("", encoding="utf-8")
    monkeypatch.setattr(gate, "_venv_bin", lambda venv: venv / "Scripts")
    monkeypatch.setattr(gate, "_tool_word", lambda name: name + ".exe")

    found = gate._project_venv_form("pytest -q", tmp_path / "with\\backslash")
    assert found == "%s -q" % tool
    assert "'" not in found


@pytest.mark.parametrize("command, program", [
    (r"C:\hostedtoolcache\Python\python.exe -c pass", r"C:\hostedtoolcache\Python\python.exe"),
    (r'"C:\Program Files\Python\python.exe" -m pytest -q', r"C:\Program Files\Python\python.exe"),
])
def test_the_program_a_windows_gate_runs_keeps_its_backslashes(monkeypatch, command, program):
    """POSIX splitting reads a backslash as an escape, so on Windows every gate
    built on an interpreter path was refused as "'C:hostedtoolcache...python.exe'
    is not on PATH" — the backslashes gone — before a single round ran."""
    from duet import gate

    monkeypatch.setattr(os, "name", "nt")
    first = gate._first_word(command)
    monkeypatch.undo()
    assert first == program


def test_a_timed_out_gate_on_windows_is_stopped_with_everything_it_started(monkeypatch):
    """Under shell=True the gate's child is cmd.exe. Stopping only that left the
    test runner beneath it alive and holding the pipes, so collecting the output
    of a gate that had run out of time waited for the whole suite after all."""
    from duet import workspace

    class Child:
        pid = 4242
        def terminate(self):
            pass
        def kill(self):
            pass
        def wait(self, timeout=None):
            return 1

    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(list(argv)))
    monkeypatch.setattr(os, "name", "nt")
    workspace.stop_process_group(Child(), grace=0)
    monkeypatch.undo()
    assert ["taskkill", "/T", "/F", "/PID", "4242"] in ran


# -- 6. a feature with no process-group signal on Windows -------------------
def test_a_stuck_walkthrough_is_still_killed_without_killpg(monkeypatch, tmp_path):
    """`os.killpg` is a POSIX-only attribute: there is no such function on
    Windows at all, so looking it up raises AttributeError, not the OSError
    the old code anticipated — and that AttributeError was never caught."""
    from duet import jury
    from tests.test_jury import project

    monkeypatch.delattr(jury.os, "killpg", raising=False)
    project(tmp_path)
    command = "%s -c \"import time; time.sleep(30)\"" % sys.executable

    walk = jury.walkthrough(str(tmp_path), command, timeout=1)

    assert walk.timed_out


def test_detach_is_a_setup_fault_on_windows_not_a_bad_argument(monkeypatch, capsys):
    """`--detach` needs `start_new_session`'s POSIX session-leader behaviour,
    which Windows has no equivalent for — refused with exit 3, the code this
    suite uses throughout for "the platform cannot do this", not 2, which
    means a bad argument."""
    import argparse

    from duet import cli

    monkeypatch.setattr(cli.os, "name", "nt")
    code = cli.cmd_detach(["run", "x", "--detach"], argparse.Namespace(command="run"))
    assert code == 3
    assert "not supported on Windows" in capsys.readouterr().out


def test_duet_invocation_does_not_use_a_posix_env_prefix_on_windows(monkeypatch):
    """`PYTHONPATH=<path> python -m duet` is bash syntax: on Windows it failed
    with "'PYTHONPATH' is not recognized as an internal or external command."
    A Windows candidate must set the variable with its own statement instead."""
    from duet import cli

    try:
        from duet import winlaunch
        monkeypatch.setattr(winlaunch, "WINDOWS", True)
    except ImportError:
        pass        # the code before the fix had no notion of "Windows mode"
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    candidates = cli._candidate_invocations()
    posix_prefix = next((c for c in candidates if c.startswith("PYTHONPATH=")), None)
    assert posix_prefix is None, candidates
    # No chain either: an agent on Windows runs this through Git Bash or
    # PowerShell, where cmd's `set "VAR=..." &&` sets nothing.
    assert not any("&&" in c or "set " in c for c in candidates), candidates
    assert any("sys.path.insert" in c for c in candidates), candidates


def test_the_windows_fallback_invocation_actually_runs_duet(monkeypatch, tmp_path):
    """The form is run, not just inspected: with no `duet` on PATH and an
    interpreter that cannot import duet by itself, it still answers --version."""
    from duet import __version__, cli, winlaunch

    monkeypatch.setattr(winlaunch, "WINDOWS", True)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    fallback = cli._candidate_invocations()[-1]
    proc = subprocess.run("%s --version" % fallback, shell=True, cwd=str(tmp_path),
                          capture_output=True, text=True, encoding="utf-8",
                          env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert __version__ in proc.stdout + proc.stderr


def test_the_windows_fallback_invocation_still_pre_authorises_bash(monkeypatch, tmp_path):
    """The Windows fallback is the most complex candidate — nested quotes, a
    semicolon-joined `-c` script, and the literal parentheses of `main())` —
    and it is the one `test_the_command_pre_authorises_the_invocation_it_
    actually_uses` never exercises, since on a normal machine a simpler
    candidate always wins first. Force it to be the chosen one and check the
    same invariant that test checks for the simple cases: the allow-rule in
    the header names exactly the invocation the body calls, parens and all."""
    from duet import cli, winlaunch

    monkeypatch.setattr(winlaunch, "WINDOWS", True)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    # Nothing but the sys.path.insert form "works" — the same situation as a
    # duet checked out from source, not pip-installed, with no venv found.
    monkeypatch.setattr(cli, "_invocation_works", lambda command: "sys.path.insert" in command)

    assert cli.main(["skill", "install", "--dir", str(tmp_path)]) == 0
    text = (tmp_path / ".claude" / "commands" / "duet.md").read_text(encoding="utf-8")
    header = text.split("---")[1]

    invocation = next(line.split(" run")[0].strip() for line in text.splitlines()
                      if "sys.path.insert" in line and " run " in line)
    assert "sys.path.insert" in invocation        # the complex form was genuinely chosen
    assert "Bash(%s:*)" % invocation in header, header
