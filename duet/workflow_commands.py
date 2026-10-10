"""`duet fix`, `duet add`, `duet refactor`, `duet plan`.

Each is `duet run` with a task that states a rule and a workflow that checks
it — see workflows.py for what each check can and cannot prove. This module is
only the glue: choosing a gate each workflow can trust, and refusing to start
when the rule could not possibly be checked.
"""

from __future__ import annotations

from pathlib import Path

from duet import ui
from duet.build import human_output
from duet.config import DEFAULT_GATE_TIMEOUT
from duet.workspace import GateResult, Workspace

TASKS = {
"fix": """\
Fix this bug:

%(what)s

The rule this session is held to, checked by the harness rather than taken on
trust: when you both sign off, the harness runs your tests against a snapshot
of the code as it was before this session. They must FAIL there and pass now.
If they would have passed on the buggy code too, they do not catch the bug, and
the sign-offs are cleared. So:

1. REPRODUCE IT. Write a test that fails because of this bug. If an existing
   test already fails because of it, say which. You do not need to leave the
   gate red for a turn to prove it — the harness replays the original itself —
   and you should not re-break working code to show it.

2. FIX IT, until the gate is green with that test in place. Do not delete a
   test that existed at the start; that is refused outright.

3. NAME THE CAUSE. Say what was actually wrong and why the change addresses the
   cause rather than the symptom. Look for the same mistake elsewhere.

The gate is exactly this, run from the root of this directory:

    %(gate)s

Reviewer: the replay proves the test fails on the original code, not WHY.
Check that it fails for the reason the bug describes — not, say, because it
imports a helper the fix introduced, which would fail on the original too.""",

"add": """\
Add this to the project:

%(what)s

The rule this session is held to, checked by the harness: when you both sign
off, it runs your tests against a snapshot of the code as it was before this
session. At least one must FAIL there — a test that would have passed without
the feature does not test the feature.

1. AGREE WHAT IT SHOULD DO before writing it — including the unhappy paths.
2. WRITE THE TEST FIRST, and watch it fail.
3. BUILD IT until the gate is green, existing tests included.
4. FIT THE CODEBASE. Its conventions, its structure, its error handling — not
   new ones you would have chosen on a blank page.

The gate is exactly this, run from the root of this directory:

    %(gate)s

Reviewer: check that the new test exercises the new behaviour, and would
fail without it.""",

"refactor": """\
Refactor:

%(what)s

Behaviour must not change. The rule, checked by the harness: every test file
that existed at the start must be byte-for-byte unchanged when you sign off,
and the gate must be green. Add new tests if you want more coverage. Do not
edit the existing ones — they are the definition of the behaviour you are
preserving, and changing them moves the goalposts.

Tests only cover what they cover. Say what behaviour this touches that no test
checks, and either add a test for it or state plainly that it is unverified.

The gate is exactly this, run from the root of this directory:

    %(gate)s""",

"plan": """\
Plan this. Do not build it:

%(what)s

Write the plan to PLAN.md. Only PLAN.md may change — the harness checks every
other file against how it was when the session started.

A plan worth having says: what changes and in what order; what could go wrong
at each step and how you would know; what you are deliberately leaving out;
and how the finished thing will be verified. Argue about the plan with your
peer the way you would argue about code. Where you disagree and cannot settle
it, write both positions into PLAN.md rather than papering over it.

Walk through the existing tests: for each one, say whether the plan keeps it
passing and how. They are the behaviour you are changing, and the harness
checks that every one is named in PLAN.md.%(tests)s%(run)s

Size the plan to the change. A two-line change needs a paragraph, not a
document — a live session once produced 326 lines for one. If you are the
reviewer and the plan is out of proportion to the work, that is an objection
worth raising; thoroughness nobody needed is a cost, not a virtue.""",
}

QUICK = """

QUICK — this session has exactly two turns, one each. There is no round three.
If you write the plan: write all of it now, and vote DONE if you would ship it
as written — your peer still has to approve it. If you review it: this is the
only review. Fix what is wrong directly in PLAN.md rather than raising it for a
turn that will not come, and vote DONE only if you would ship the result."""

USAGE = {
    "fix": 'duet fix "the export drops rows whose name contains a comma"',
    "add": 'duet add "a --json flag that prints the report as JSON"',
    "refactor": 'duet refactor "split parser.py into a tokenizer and a parser"',
    "plan": 'duet plan "move the storage layer from SQLite to Postgres"',
}


def gate_before(gate: str, root: str, timeout: int = DEFAULT_GATE_TIMEOUT) -> GateResult:
    """The gate on the code as it is before any session — the same run, and the
    same limit, a session's own gate gets."""
    return Workspace(root, gate=gate, gate_timeout=timeout).run_gate()


def run(args, name: str) -> int:
    from duet import cli     # cli imports this module; see build.run

    nested = cli.refuse_nested(name, args)
    if nested is not None:
        return nested

    what = " ".join(getattr(args, "what", None) or []).strip()
    if not what:
        print(ui.red("nothing to %s." % name))
        print("usage: " + USAGE[name])
        return 2

    root = str(Path(args.root).expanduser().resolve())
    args.workflow = name

    if name == "plan":
        # A gate has nothing to say about a document, and "the tests still
        # pass" would be a strange thing to make a plan wait on.
        args.gate, args.no_gate = None, True
        effective = ""
        # Not a gate, but the agents may run it: a plan whose claims about the
        # tests were checked beats one traced by hand, which is what both
        # agents were reduced to when no command was allowed at all.
        args.allow_run = cli.load_config(root).gate or cli.gate_detect.detect(root) or ""
    else:
        configured = cli.load_config(root).gate
        effective = args.gate or configured or cli.gate_detect.detect(root) or ""
        if not effective or args.no_gate:
            # Deliberately no starter gate here, unlike `duet build`: a gate
            # that is red because no tests exist yet would satisfy `fix`'s
            # "the gate went red" on day one, reproducing nothing.
            print(ui.red("✗ ") + "`duet %s` needs this project's test command, and none was found."
                  % name)
            print(ui.dim("  Its rule is checked against the gate, so without one there is"))
            print(ui.dim("  nothing to check it against."))
            print(ui.dim("  fix: ") + "pass " + ui.bold('--gate "<your test command>"')
                  + (", or start from nothing with duet build" if name == "add" else ""))
            return 3
        args.gate = effective
        before = None
        if name == "refactor":
            before = gate_before(effective, root,
                                 cli.gate_timeout_seconds(args)
                                 or cli.load_config(root).gate_timeout)
        if before is not None and before.timed_out:
            print(ui.yellow("✗ ") + "before the refactor started, " + before.timeout_note() + ".")
            print(ui.dim("  gate: ") + effective)
            print(ui.dim("  A refactor is measured against a green suite, and this one did"))
            print(ui.dim("  not finish, so nothing was shown to pass. Give it longer with ")
                  + ui.bold("--gate-timeout MINUTES") + ui.dim("."))
            return 3
        if before is not None and not before.ok:
            print(ui.red("✗ ") + "the tests are failing before the refactor has started.")
            print(ui.dim("  gate: ") + effective)
            print(ui.dim("  A refactor has to show behaviour did not change, and the tests are"))
            print(ui.dim("  the measure of that. Measured against a suite that already fails,"))
            print(ui.dim("  green at the end could mean anything. Fix the suite first —"))
            print(ui.dim("  ") + ui.bold('duet fix "..."') + ui.dim(" is the command for that."))
            return 3

    with human_output(args):
        if effective:
            print(ui.dim("   gate  ") + effective)
        print(ui.dim("   rule  ") + RULES[name])

    tests = ""
    if name == "plan":
        from duet.workflows import NAMED_TEST_LIMIT, test_names
        names = test_names(root)
        if names and len(names) <= NAMED_TEST_LIMIT:
            tests = " They are:\n\n" + "\n".join("    - " + n for n in names)
        elif names:
            tests = (" There are %d of them — too many to name one by one, so give the"
                     " existing tests a section of their own instead." % len(names))
    run = ""
    if name == "plan" and getattr(args, "allow_run", ""):
        run = ("\n\nYou can run them: `%s` is allowed, and it is the only command that"
               " is. Run it to settle a claim about what the tests do rather than"
               " tracing it by hand." % args.allow_run)
    args.task = [TASKS[name] % {"what": what, "gate": effective or "(none — this is a plan)",
                                "tests": tests, "run": run}]
    if name == "plan" and getattr(args, "quick", False):
        args.rounds = 2
        args.task[0] += QUICK
    args.file = None
    return cli.cmd_run(args)


RULES = {
    "fix": "the tests must fail against the original code, and pass now",
    "add": "a test must fail against the code as it was before",
    "refactor": "existing tests must end byte-for-byte unchanged, gate green",
    "plan": "only PLAN.md may change, and it must not be empty",
}
