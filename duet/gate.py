"""Finding the command that decides whether the work is done.

The gate is the single most valuable thing you can give a session: the harness
runs it, both agents see the real output, and it outranks both their opinions.
A session without one is two models agreeing by argument alone.

So duet looks for it rather than making you remember the flag — and says what
it picked, because a gate you did not choose silently running the wrong command
is worse than no gate at all.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from duet.page import PAGE_RULES
from duet.winlaunch import command as launchable
from duet.winlaunch import quote_for_shell

# Ordered: the first that matches a real signal in the project wins.
CANDIDATES: List[Tuple[str, str]] = [
    ("Makefile", "make test"),
    ("pyproject.toml", "pytest -q"),
    ("pytest.ini", "pytest -q"),
    ("tox.ini", "pytest -q"),
    ("setup.cfg", "pytest -q"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
    ("Gemfile", "bundle exec rspec"),
    ("mix.exs", "mix test"),
    ("build.gradle", "./gradlew test"),
    ("pom.xml", "mvn -q test"),
]


def _package_json_script(root: Path) -> Optional[str]:
    path = root / "package.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return None
    for name in ("test", "check", "ci"):
        script = scripts.get(name)
        # `npm init` writes a placeholder test script that only prints an error.
        if isinstance(script, str) and script.strip() and "no test specified" not in script:
            return "npm test" if name == "test" else "npm run %s" % name
    return None


def _make_has_test_target(root: Path) -> bool:
    try:
        text = (root / "Makefile").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"^test\s*:", text, re.MULTILINE) is not None


def _has_python_tests(root: Path) -> bool:
    """Is there a Python test here — or only a directory named like one?

    macOS and Windows match filenames without regard to case, so
    `(root / "tests").is_dir()` is also true of Xcode's `Tests/`. An iOS
    project was handed `pytest -q` on the strength of it: a gate that collects
    nothing, exits 5, and is red every round — which blocks both sign-offs for
    the whole session, however right the pair are. A directory holding another
    language's tests is not evidence of Python ones.

    An empty `tests/` still counts. That is a greenfield project about to have
    tests written into it, and the gate it needs is the one that will run them.
    """
    for name in ("tests", "test"):
        directory = root / name
        if not directory.is_dir():
            continue
        other_language = False
        for path in directory.rglob("*"):
            if not path.is_file() or not path.suffix:
                continue
            if path.suffix == ".py":
                return True
            other_language = True
        if not other_language:
            return True
    return any(root.glob("test_*.py")) or any(root.glob("*_test.py"))


def _first_word(command: str) -> str:
    """The program a shell would actually run, not the first run of characters.

    `command.split()[0]` breaks the moment a path is quoted: a gate built
    around an interpreter at "/opt/some where/python3" was judged unrunnable
    and the session refused to start, because the first "word" was `'/opt/some`.

    On Windows the gate runs through cmd.exe, so it is split the way cmd reads
    it: POSIX splitting treats a backslash as an escape and turned
    `C:\\Python\\python.exe` into `C:Pythonpython.exe`, a program that does not
    exist, and every gate built on an interpreter path was refused.
    """
    windows = os.name == "nt"
    try:
        parts = shlex.split(command, posix=not windows)
    except ValueError:          # unbalanced quotes; let the shell complain
        parts = command.split()
    if windows:
        parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in parts]
    return parts[0] if parts else ""


def _runnable(command: str, root: str) -> bool:
    """Is the first word of this command something that exists?

    A detected gate that cannot start fails every single turn, which blocks
    consensus for the whole session — worse than having no gate, because the
    pair can never finish however right they are. Found by both agents in a
    live session: `pytest -q` was detected on a machine where pytest is only
    inside a virtualenv, so the gate could never pass.
    """
    first = _first_word(command)
    if shutil.which(first):
        return True
    # A relative launcher like ./gradlew
    candidate = Path(root) / first
    return candidate.is_file() and os.access(str(candidate), os.X_OK)


# Where a Python project keeps its own virtualenv, in the order a project with
# more than one most likely means. This is where the project's dependencies
# are, so its pytest is the one that can actually import the code under test.
VENV_DIRS = (".venv", "venv", "env")


def _venv_bin(venv: Path) -> Path:
    """The directory a virtualenv puts its executables in on this OS."""
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _can_start(argv: List[str]) -> bool:
    """Does this program exist and answer `--version`?

    The same rule the rest of detection follows: a gate that cannot start fails
    every round and vetoes both agents, so nothing is handed back unproven.
    """
    try:
        proc = subprocess.run(launchable(argv), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _tool_word(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def _project_venv_form(command: str, base: Path) -> Optional[str]:
    """`pytest -q` becomes the project's own virtualenv's pytest, if it has one.

    Preferred over every other way of spelling pytest, including one on PATH
    and the interpreter running duet: the project's virtualenv is the only one
    holding the project's dependencies, and a pytest that cannot import the
    code under test collects errors every round just as surely as one that is
    missing. duet installed with pipx or by its own installer has a virtualenv
    of its own that shares nothing with the project's.
    """
    first, _, rest = command.partition(" ")
    if first not in ("pytest",):
        return None
    suffix = (" " + rest) if rest else ""
    for name in VENV_DIRS:
        bindir = _venv_bin(base / name)
        if not bindir.is_dir():
            continue
        tool = bindir / _tool_word(first)
        if tool.is_file() and _can_start([str(tool), "--version"]):
            return "%s%s" % (quote_for_shell(str(tool)), suffix)
        python = bindir / _tool_word("python")
        if python.is_file() and _can_start([str(python), "-m", first, "--version"]):
            return "%s -m %s%s" % (quote_for_shell(str(python)), first, suffix)
    return None


def _python_module_form(command: str) -> Optional[str]:
    """`pytest -q` becomes `<a python that has pytest> -m pytest -q`.

    pytest is usually installed into a virtualenv rather than onto PATH. The
    interpreter running duet is tried first, then a python3 on PATH — because
    duet installed with pipx, or by its own installer into ~/.duet/venv, lives
    in a virtualenv that has duet and nothing else, and used to leave an
    ordinary Python project with no gate at all.

    The path is shell-quoted: an interpreter under a directory with a space in
    it produced a gate whose first word was half a path, which duet then
    refused to start a session on. `shlex.quote`, which wraps anything holding
    a backslash in single quotes, is right for that shell on POSIX but not
    for cmd.exe on Windows, where every path has a backslash and single
    quotes are not quoting at all — so the quoting follows the OS the gate
    will actually run on, not always POSIX rules.
    """
    first, _, rest = command.partition(" ")
    if first not in ("pytest",):
        return None
    candidates = [sys.executable]
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)
    for python in candidates:
        if python and _can_start([python, "-m", first, "--version"]):
            return "%s -m %s%s" % (
                quote_for_shell(python), first, (" " + rest) if rest else "")
    return None


# Where a static site keeps the page a visitor lands on. A website has no test
# command, so `duet page check` is the gate it can have instead — and a session
# with a gate is the only kind where "done" is not just two opinions.
PAGE_DIRS = ("", "site", "public", "docs", "dist")
PAGE_FILE = "index.html"


def duet_command() -> str:
    """How to invoke duet from inside a gate.

    `duet` is not always on PATH — a gate runs with a minimal one, and duet is
    often installed inside a virtualenv — and a gate that cannot start fails
    every round, which blocks both sign-offs however good the work is. So the
    detected command names the interpreter running duet when the name is not
    there to be found.
    """
    if shutil.which("duet"):
        return "duet"
    return "%s -m duet" % quote_for_shell(sys.executable)


def page_gate(root: str) -> Optional[str]:
    """`duet page check <page>` for this project, or None if it is not a site.

    The page comes from duet-page.json's "page" when it names one — a project
    that has written its own page rules has already said which page it means —
    and otherwise from the index.html a static site is laid out around.
    """
    base = Path(root).expanduser().resolve()
    page = ""
    rules = base / PAGE_RULES
    if rules.is_file():
        try:
            data = json.loads(rules.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        named = data.get("page") if isinstance(data, dict) else None
        if isinstance(named, str) and named.strip():
            named = named.strip()
            if re.match(r"^(https?|file):", named) or (base / named).is_file():
                page = named
    if not page:
        for directory in PAGE_DIRS:
            candidate = (base / directory / PAGE_FILE) if directory else (base / PAGE_FILE)
            if candidate.is_file():
                page = candidate.relative_to(base).as_posix()
                break
    if not page:
        return None
    # Runnable by construction: duet_command() only says "duet" when the name
    # resolves, so this never needs the check the other candidates get.
    return "%s page check %s" % (duet_command(), page)


def detect(root: str) -> Optional[str]:
    """A test command this project plausibly has, or None.

    Deliberately conservative: a wrong gate that always passes is worse than no
    gate, because it looks like verification and is not.
    """
    base = Path(root).expanduser().resolve()

    def usable(command: Optional[str]) -> Optional[str]:
        """Only hand back a command that can actually start.

        The project's own virtualenv comes first for pytest — see
        `_project_venv_form`; for everything else this is the old order, PATH
        and then a relative launcher in the project.
        """
        if not command:
            return None
        local = _project_venv_form(command, base)
        if local:
            return local
        if _runnable(command, str(base)):
            return command
        return _python_module_form(command)

    script = usable(_package_json_script(base))
    if script:
        return script

    for marker, command in CANDIDATES:
        if not (base / marker).is_file():
            continue
        if marker == "Makefile":
            # `usable` too: a `test:` target is no use on a machine without
            # make, and this branch returned early, skipping the one check that
            # exists to stop an unstartable gate from vetoing every round.
            if _make_has_test_target(base):
                found = usable(command)
                if found:
                    return found
            continue
        if command.startswith("pytest") and not _has_python_tests(base):
            continue
        found = usable(command)
        if found:
            return found
        continue

    if _has_python_tests(base):
        return usable("pytest -q")
    # Last, not first: a project that has real tests is judged by them. This is
    # for the project that has none — a website — which would otherwise run on
    # argument alone.
    return page_gate(str(base))


def why_unusable(command: str, root: str) -> Optional[str]:
    """Why this gate cannot run, or None if it can.

    A gate that cannot start fails every turn, and a failing gate vetoes both
    agents — so the session is unwinnable before it begins, however good the
    work is. Better to refuse in a second than to discover it eight rounds and
    two subscriptions later, which is exactly what happened once.
    """
    command = (command or "").strip()
    if not command:
        return None
    first = _first_word(command)
    if shutil.which(first):
        return None
    candidate = Path(root) / first
    if candidate.is_file() and os.access(str(candidate), os.X_OK):
        return None
    if first in ("PYTHONPATH", "CI") or "=" in first:
        return None          # a leading assignment; the real command follows
    return "%r is not on PATH and is not an executable in this directory" % first


def describe(command: Optional[str]) -> str:
    if command and " page check " in " %s " % command:
        return "no test command, but there is a page to check: %s" % command
    if command:
        return "found a test command: %s" % command
    return (
        "no test command found — the pair can only agree by argument. "
        "Pass --gate \"<your tests>\" if you have one."
    )
