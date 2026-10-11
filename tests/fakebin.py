"""Fake command-line programs that run on every platform the suite runs on.

The suite stands in for `claude`, `codex`, `npx`, an installed `duet` and so on
with small shell scripts. Windows cannot execute a `#!/bin/sh` file, so there a
fake is the same script plus a `<name>.cmd` beside it, shaped like the shim npm
writes, that forwards `%*` to Git for Windows' bash. duet's launcher reads that
shim and starts bash on the script directly, exactly as it does for a real
npm-installed CLI, so the tests exercise the real Windows launch path.
"""

import os
import shutil
import stat
from pathlib import Path

import pytest

WINDOWS = os.name == "nt"


def posix_shell() -> str:
    """Git for Windows' bash — not System32\\bash.exe, which is WSL's."""
    candidates = []
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        candidates += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(Path(program_files) / "Git" / "bin" / "bash.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    pytest.skip("no POSIX shell (Git for Windows' bash) to run a fake program with")


def fake_cli(folder, name: str, body: str, shebang: str = "#!/usr/bin/env bash") -> str:
    """A program called `name` in `folder` that runs `body`; returns its path.

    On Windows the path returned is the `.cmd` shim, which is also what
    shutil.which finds for a bare `name` when `folder` is on PATH.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        path = folder / name
        path.write_text(shebang + "\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)
    scripts = folder / ".fake-scripts"
    scripts.mkdir(exist_ok=True)
    script = scripts / name
    with open(str(script), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(shebang + "\n" + body)
    shim = folder / (name + ".cmd")
    with open(str(shim), "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write('@"%s" "%%~dp0\\.fake-scripts\\%s" %%*\n' % (posix_shell(), name))
    return str(shim)
