"""Starting a project from nothing.

A greenfield directory is where duet used to lose its own mechanism: no tests
exist, so gate detection finds nothing, and the session header reads "gate:
none — they can only agree by argument". That is also exactly where people
build fastest and ship worst.

So a build session sets the gate to the project's test command from round one,
before a single test exists. It is red until the pair writes real tests that
pass, and a red gate blocks both sign-offs. Having nothing to verify against
becomes the thing that stops them finishing, instead of the thing nobody
noticed.

The hard part is not choosing a command, it is refusing to choose a *flattering*
one. Three of the obvious candidates pass an empty directory — `unittest
discover` before Python 3.12, `go test ./...`, `cargo test` — and a gate that
goes green on nothing is a false proof, which is the one thing the double
sign-off exists to rule out. So whatever is chosen is run once before the
session starts, and a gate that is already green on a workspace with no tests
is refused rather than used.
"""

from __future__ import annotations

import contextlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from duet import ui, workflows

TASK = """\
Build this, from nothing, in this directory:

%(idea)s

Work in this order. Do not skip ahead.

1. AGREE WHAT DONE MEANS. Before any implementation, settle the acceptance
   criteria with your peer: concrete, checkable statements about behaviour,
   including the unhappy paths — bad input, a missing file, the empty case.
   Write them to ACCEPTANCE.md. Disagreeing now is cheap; after the code
   exists it is not.

   Name the environment those criteria assume — language runtime and lowest
   supported version, OS, anything that must already be installed. The gate
   runs one command, on one machine, with one interpreter, so whatever the
   criteria leave unstated is what nothing will check. A previous session
   ended in agreement on a CLI that crashed on the older interpreter neither
   agent had thought to name.

   Write down the defaults too, especially the ones with consequences: what it
   binds to, what it writes and where, what it exposes, what it keeps. Another
   session shipped a service listening on 0.0.0.0 — not because either agent
   argued for it, but because neither mentioned it, and an unstated default is
   the one thing neither the gate nor your peer can object to.

2. WRITE THE TESTS FIRST, and watch them fail. Encode every criterion from
   ACCEPTANCE.md as a test. The gate already runs them, so it is red until
   they exist and pass, and neither of you can sign off while it is red. A
   test that cannot fail is worse than no test: it looks like proof and is
   not, so check each one fails before you make it pass.

   The gate is exactly this, run from the root of this directory:

       %(gate)s

   Your tests have to be the ones that command runs. If it is the wrong
   command for what you are building, say so on your first turn — do not
   quietly build something it cannot see.

3. THEN BUILD, until the gate is green.

4. CHECK WHAT YOU BUILT against the criteria you agreed in step 1, not
   against your memory of the task.

5. LEAVE A WAY IN. Write README.md with a section headed "How to run it" whose
   first code block is the exact command that starts or uses what you built,
   runnable from this directory as it stands. The person who asked for this
   has not read your code; that command is the first thing they will try.

The ordinary rules still apply: answer every objection your peer raises, and
vote DONE only if you would ship exactly what is in the workspace now."""


# What to run when the project has no tests yet but its shape is recognisable.
# Only a runner that is actually installed is offered: a gate that cannot start
# fails every turn and blocks consensus outright, which is worse than admitting
# there is no gate.
STARTERS = (
    ("package.json", "npm test"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
)

# What to call a toolchain in a sentence, when its command name would read oddly.
TOOL_NAMES = {"npm": "JavaScript or TypeScript", "go": "Go", "cargo": "Rust"}


def task_for(idea: str, gate: str) -> str:
    return TASK % {"idea": idea.strip(), "gate": gate.strip() or "(no gate this session)"}


# The gate runner duet writes for a machine with no test runner installed.
# `unittest discover` cannot see a conventional `tests/` directory unless it
# holds an `__init__.py` — checked on 3.9 and 3.12, both find zero tests — so
# the pair would write tests where every Python project puts them and watch the
# gate stay red while it told them no tests existed. This imports by path
# instead, which has no such requirement.
RUNNER = '''"""The acceptance gate for a project that had no test runner.

Written by `duet build`. Runs every test*.py under this directory and fails if
none of them ran, because a gate that passes an empty workspace proves nothing.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

SKIP = {".duet", ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox"}

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

suite = unittest.TestSuite()
loader = unittest.defaultTestLoader
broken = []

for path in sorted(root.rglob("test*.py")):
    if SKIP & set(path.relative_to(root).parts):
        continue
    name = "duet_gate_" + "_".join(path.relative_to(root).with_suffix("").parts)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception as exc:                     # a test file that will not import
        broken.append("%s: %s: %s" % (path.relative_to(root), type(exc).__name__, exc))
        continue
    suite.addTests(loader.loadTestsFromModule(module))

result = unittest.TextTestRunner(verbosity=1).run(suite)

for line in broken:
    print("could not import " + line)
if not result.testsRun and not broken:
    print("no tests ran: this gate stays red until tests exist")

ok = result.wasSuccessful() and result.testsRun and not broken
sys.exit(0 if ok else 1)
'''

RUNNER_PATH = ".duet/gate_unittest.py"

# Directories that are never part of what the pair built.
SKIP_DIRS = {".duet", ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox"}


# Languages duet can name a test command for, keyed by what the idea says.
# Deliberately short: a wrong guess here hands the pair a gate their work can
# never satisfy, which is worse than the Python default they can argue with.
LANGUAGES: Tuple[Tuple[str, str, str], ...] = (
    (r"\brust\b|\bcargo\b", "cargo", "cargo test"),
    # "go" on its own is an ordinary English word — "a tool to go through
    # photos" is not a Go project — so it only counts next to something that
    # makes it a language.
    (r"\bgolang\b|\bin go\b|\bgo (?:service|cli|tool|program|binary|module|package|app|server)\b",
     "go", "go test ./..."),
    # Same for "node", which is a tree node more often than a runtime here.
    (r"\bnode\.?js\b|\bjavascript\b|\btypescript\b|\bnpm\b",
     "npm", "npm test"),
)


def language_gate(idea: str) -> Tuple[Optional[str], Optional[str]]:
    """(command, missing toolchain) for a language the idea names outright.

    Returns (None, None) when the idea names nothing duet knows, which is the
    common case and means the Python starter is used.
    """
    text = (idea or "").lower()
    for pattern, tool, command in LANGUAGES:
        if re.search(pattern, text):
            if shutil.which(tool):
                return command, None
            return None, tool
    return None, None


# Commands that report success when they ran no tests at all. `go test ./...`
# exits 0 printing "[no test files]" for any package without tests — verified
# here — and `cargo test` exits 0 on "running 0 tests"; cargo's is from its
# documented output, because the cargo on this machine is the wrong
# architecture to run. Each is wrapped in the assertion `duet build` actually
# makes: at least one test passed. The output still reaches both agents
# through `tee`, so nothing is hidden by the pipe.
ZERO_TEST_PROOF = {
    "go test ./...": 'go test ./... 2>&1 | tee /dev/stderr | grep -q "^ok "',
    "cargo test": 'cargo test 2>&1 | tee /dev/stderr | grep -qE "^test result: ok\\. [1-9]"',
}


def proof_against_zero_tests(command: str) -> str:
    """The same gate, unable to pass a project that has no tests."""
    return ZERO_TEST_PROOF.get((command or "").strip(), command)


def has_tests(root: str) -> bool:
    """Does anything here look like a test the gate could be running?

    Asked of the same detector the workflow rules use, rather than of a second
    list. This was that second list, hand-copied, and it drifted exactly as
    the first one did: it never learned Swift, and it looked for `tests/` at
    the top level only, case-sensitively. An Xcode project with ninety XCTest
    cases read as a directory with nothing in it — so `duet build` called a
    green `xcodebuild` gate "green with nothing here to have passed", and
    refused to start when it had detected that gate itself.
    """
    return workflows.any_test_file(root)


def write_runner(root: str) -> str:
    """Put the fallback gate script in .duet, and return the command for it.

    `.duet` is excluded from the workspace digest, so the script cannot move
    the state the two sign-offs are counted against.
    """
    base = Path(root).expanduser().resolve()
    target = base / RUNNER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(RUNNER, encoding="utf-8")
    return "%s %s" % (shlex.quote(sys.executable), RUNNER_PATH)


def starter_gate(root: str, idea: str = "") -> Optional[str]:
    """A test command for a directory that has no tests yet.

    Follows the language the idea names, then the shape of whatever is already
    on disk, and otherwise assumes Python — the one runner duet can always
    reach, because it is the interpreter duet is running on.
    """
    base = Path(root).expanduser().resolve()
    named, _missing = language_gate(idea)
    if named:
        return named
    for marker, command in STARTERS:
        if (base / marker).is_file() and shutil.which(command.split()[0]):
            return command
    if shutil.which("pytest"):
        return "pytest -q"
    return write_runner(root)


def gate_proves_nothing(gate: str, root: str) -> bool:
    """Is this gate already green on a workspace with no tests in it?

    `go test ./...` and `cargo test` both exit 0 with no tests to run, and so
    does `unittest discover` before 3.12. Such a gate is not a weak check, it
    is the absence of one wearing the same clothes — and `duet build` promises
    the opposite, that the gate is red until real tests pass. So it is run once
    before the session starts, rather than trusted.
    """
    if not gate or has_tests(root):
        return False
    try:
        proc = subprocess.run(gate, cwd=str(Path(root).expanduser().resolve()),
                              shell=True, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False        # cannot start is a different problem, already checked
    return proc.returncode == 0


def announce(gate: str, source: str) -> None:
    """Say where the gate came from, before the session header prints it.

    `source` is "starter" only when nothing existed to detect. Saying "nothing
    here to test yet" over a project's real test command would be a claim about
    the workspace that is simply false, and the line exists to be trusted.
    """
    if not gate:
        print(ui.yellow("!  ") + "no gate: they can only agree by argument.")
        return
    print(ui.dim("   gate  ") + gate)
    if source == "starter":
        print(ui.dim("         nothing here to test yet, so this starts red — "
                     "and a red gate blocks both sign-offs."))
    elif source == "detected":
        print(ui.dim("         this project's own test command, found here — "
                     "not a starter."))
    elif source == "configured":
        print(ui.dim("         from .duet/config.json."))


def warn_vacuous_gate(gate: str, mine: bool) -> None:
    """Say a chosen gate proves nothing — and refuse only if duet chose it.

    duet refuses its own bad choices and warns about yours. `has_tests` reads a
    handful of conventions and will not recognise every layout, so a project
    whose tests it cannot see would otherwise have no way to start a session at
    all — a refusal the user cannot argue with is worse than the gate it is
    protecting them from.
    """
    mark = ui.red("✗ ") if mine else ui.yellow("!  ")
    print(mark + "this gate passes with nothing here that looks like a test.")
    print(ui.dim("  gate: ") + gate)
    print()
    print("  `duet build` is only worth running because the gate is red before the")
    print("  code exists — that is what stops the two of them agreeing on nothing.")
    print("  This one is green already, with nothing here to have passed.")
    print()
    if not mine:
        print(ui.dim("  yours, so it stands") + " — but a session that starts green can end")
        print("  green without a test ever being written.")
        return
    print(ui.dim("  fix: ") + "give a command that fails when no tests ran, with "
          + ui.bold("--gate"))
    # The example is the wrapper duet uses itself, not an improvised one: an
    # obvious-looking `grep -qv "no test files"` inverts line by line and
    # passes anyway, which is the same bug this message is about.
    print(ui.dim("       ") + "e.g. for Go:  "
          + ui.bold("--gate '%s'" % ZERO_TEST_PROOF["go test ./..."]))


def _measure(path: Path):
    """Lines for a text file, a size for anything else.

    A SQLite file a test left behind was being reported as "7 lines", which is
    not a small inaccuracy about a real thing — it is a statement about a file
    that has no lines at all.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
        if b"\x00" in head:
            size = path.stat().st_size
            return "%d KB" % (size // 1024) if size >= 1024 else "%d bytes" % size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


RUN_HEADING = re.compile(r"^#{1,6}\s*(how to run|run it|running|usage|quick\s*start|getting started)\b",
                         re.IGNORECASE)


def run_command(root: str) -> str:
    """The first command under README.md's "How to run it", or "".

    Read, never guessed. A build that ends with a command duet invented, which
    then fails, is worse than one that ends with no command at all.
    """
    readme = Path(root).expanduser().resolve() / "README.md"
    try:
        lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    in_section = in_block = False
    # An indented code block (four spaces or a tab) is as much a code block as
    # a fenced one, but only where Markdown makes it one: it cannot interrupt a
    # paragraph, and inside a list an indented line continues the list item.
    # `previous` is the last line's kind: "blank", "text", "list" or "code".
    previous = "blank"
    for line in lines:
        indented = line.startswith(("    ", "\t"))
        # A heading is indented at most three spaces; `    # install` is a
        # comment inside an indented code block, not a heading.
        if not in_block and not indented and line.lstrip().startswith("#"):
            in_section = bool(RUN_HEADING.match(line.strip()))
            previous = "blank"
            continue
        if in_section and line.strip().startswith("```"):
            if in_block:
                return ""          # an empty block; do not look further
            in_block = True
            continue
        if not line.strip():
            previous = "blank" if previous != "list" else "list"
            continue
        if in_block or (in_section and indented and previous in ("blank", "code")):
            previous = "code"
            command = _command_in(line)
            if command:
                return command
            continue
        if re.match(r"\s{0,3}([-*+]|\d+[.)])\s", line) or (indented and previous == "list"):
            previous = "list"
        else:
            previous = "text"
    return ""


def _command_in(line: str) -> str:
    """The command on one line of a code block, or "" for a comment or nothing."""
    command = line.strip()
    if not command or command.startswith("#"):
        return ""
    command = command[2:] if command.startswith("$ ") else command
    # `python3 run.py   # http://127.0.0.1:8000/` — the comment is
    # for the reader of the README, not part of the command.
    return re.sub(r"\s+#.*$", "", command)


def summarise(root: str, seconds: float, gate: str) -> None:
    """What now exists, after a build session agrees.

    `duet run` ends by naming a state digest and a report path, which is the
    right answer for a change to a project you already know. For a build, the
    thing you want to see is the project — it did not exist an hour ago, and
    nobody has a picture of it yet.
    """
    base = Path(root).expanduser().resolve()
    rows = []
    for path in sorted(base.rglob("*")):
        parts = path.relative_to(base).parts
        # Same exclusions the gate runner uses: a project with node_modules
        # would otherwise be summarised by counting a hundred thousand files
        # nobody wrote here.
        if not path.is_file() or SKIP_DIRS & set(parts):
            continue
        rows.append(("/".join(parts), _measure(path)))
    if not rows:
        return

    lines_total = sum(n for _, n in rows if isinstance(n, int))
    minutes, secs = divmod(int(seconds), 60)
    print()
    print(ui.bold("built in %dm %02ds" % (minutes, secs))
          + ui.dim("  — %d files, %d lines" % (len(rows), lines_total)))
    width = max(len(name) for name, _ in rows)
    for name, measure in rows[:12]:
        shown = "%d lines" % measure if isinstance(measure, int) else measure
        print("  %-*s  %s" % (width, name, ui.dim(shown)))
    if len(rows) > 12:
        print(ui.dim("  ... and %d more" % (len(rows) - 12)))
    if gate:
        print(ui.dim("  verified by: ") + gate)
    command = run_command(root)
    if command:
        print(ui.dim("  run it:      ") + ui.bold(command))


def human_output(args):
    """Send duet's own prose to stderr when stdout is a machine-readable stream.

    `duet build --json` printed the gate line and the closing summary onto
    stdout ahead of the event stream, so anything reading stdout a line at a
    time hit prose before its first JSON object. The information is worth
    keeping, so it moves rather than disappears.
    """
    if getattr(args, "json", False):
        return contextlib.redirect_stdout(sys.stderr)
    return contextlib.nullcontext()


def run(args) -> int:
    """`duet build "<idea>"` — a run whose gate exists before the code does."""
    # Imported here, not at module scope: cli imports this module to register
    # the subcommand, so importing it back at the top is a cycle.
    from duet import cli

    # Before anything is printed: a nested session is refused outright, and a
    # gate line above that refusal would be noise about a session that is not
    # going to start.
    nested = cli.refuse_nested("build", args)
    if nested is not None:
        return nested

    idea = " ".join(getattr(args, "idea", None) or []).strip()
    if not idea:
        print(ui.red("no idea given."))
        print('usage: duet build "a CLI that renames photos by the date in their EXIF"')
        return 2

    root = str(Path(args.root).expanduser().resolve())
    configured = cli.load_config(root).gate
    source = "yours" if args.gate else ("configured" if configured else "")
    if args.gate is None and not args.no_gate and not configured:
        _named, missing = language_gate(idea)
        if missing:
            print(ui.red("✗ ") + "that idea says %s, and %s is not installed here."
                  % (TOOL_NAMES.get(missing, missing), missing))
            print(ui.dim("  A gate the work cannot satisfy blocks both sign-offs for the"))
            print(ui.dim("  whole session, so this stops here instead."))
            print(ui.dim("  fix: ") + "install %s, or pass " % missing + ui.bold("--gate")
                  + " with a command that does run here")
            return 3
        # detect() first: a project with tests already has a real gate, and a
        # starter would be a worse one. It only falls through on greenfield.
        found = cli.gate_detect.detect(root)
        # Wrapped either way: a detected `go test ./...` passes a module with
        # no tests just as readily as a starter would, and this command's whole
        # claim is that the gate is red until real tests pass.
        args.gate = proof_against_zero_tests(found or starter_gate(root, idea))
        source = "detected" if found else "starter"

    effective = args.gate or configured or ""
    with human_output(args):
        announce(effective, source)
    if effective and not args.no_gate and gate_proves_nothing(effective, root):
        mine = source in ("starter", "detected")
        with human_output(args):
            warn_vacuous_gate(effective, mine)
        if mine:
            return 3

    # cmd_run owns the parts that must not diverge between the two commands:
    # the nested-session guard, the unrunnable-gate check, sign-in preflight,
    # and the orchestrator itself. Only the task and the gate differ here.
    args.task = [task_for(idea, effective)]
    args.file = None
    started = time.time()
    code = cli.cmd_run(args)
    if code == 0:
        with human_output(args):
            summarise(root, time.time() - started, effective)
    return code
