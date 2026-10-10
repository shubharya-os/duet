from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from duet import __version__, prompts, ui
from duet.adapters import REGISTRY
from duet.adapters import build as build_adapter
from duet.adapters.base import Adapter, DEFAULT_TURN_TIMEOUT, human_duration
from duet import gate as gate_detect
from duet import build as build_cmd
from duet import page as page_check
from duet import workflow_commands
from duet import from_ecc
from duet import jury
from duet.config import (
    BACKEND_ALIASES,
    DEFAULT_GATE_TIMEOUT,
    DEFAULT_PAIR,
    AgentSpec,
    Config,
    config_dir,
    config_path,
    default_agents,
    load_config,
    load_env_file,
    parse_pair,
    save_config,
)
from duet.orchestrator import Orchestrator, STATUS_CONSENSUS, STATUS_REVIEWED, rounds_taken
from duet.protocol import BLOCKING_SEVERITIES, SEVERITIES, parse_envelope

PREVIEW_CHARS = 700
ERROR_CHARS = 1200

TURN_TIMEOUT_HELP = (
    "minutes one agent's turn may take before the harness cuts it off "
    "(default %s). It is stated to both agents as their budget, and a turn cut "
    "off after it changed the workspace hands over to the peer instead of "
    "failing the session." % human_duration(DEFAULT_TURN_TIMEOUT)
)
GATE_TIMEOUT_HELP = (
    "minutes one run of the acceptance gate may take before it is stopped "
    "(default %s). A gate stopped at its limit is reported as having run out of "
    "time, not as failing tests, and it never counts as green."
    % human_duration(DEFAULT_GATE_TIMEOUT)
)


# --------------------------------------------------------------------------
# console reporting
# --------------------------------------------------------------------------
def make_reporter(agents: List[str], verbose: bool = True, as_json: bool = False):
    def say(*parts: str) -> None:
        # A session is long and mostly waiting. Without an explicit flush, a piped
        # or redirected run shows nothing until it ends and looks hung.
        print(*parts, flush=True)

    def report(event: Dict[str, Any]) -> None:
        if as_json:
            print(json.dumps(event, default=str), flush=True)
            return
        kind = event.get("kind")
        if kind == "session_start":
            say(ui.rule())
            say(ui.bold("duet") + "  " + ui.dim(event.get("session", "")))
            for name, desc in (event.get("agents") or {}).items():
                say("  %s  %s" % (ui.agent_tag(name, agents), ui.dim(desc)))
            say("  %s %s" % (ui.dim("workspace:"), event.get("root")))
            if event.get("gate"):
                note = ui.dim("  (found for you — override with --gate)") if event.get("gate_was_detected") else ""
                say("  %s %s%s" % (ui.dim("gate:     "), event["gate"], note))
            else:
                say("  %s %s" % (ui.dim("gate:     "),
                                 ui.yellow("none — they can only agree by argument")))
            say("  %s %s goes first, up to %d rounds"
                  % (ui.dim("order:    "), (event.get("order") or ["?"])[0], event.get("max_rounds", 0)))
            budgets = sorted({int(v) for v in (event.get("turn_timeout") or {}).values()})
            if budgets:
                say("  %s %s per turn%s"
                    % (ui.dim("turn:     "),
                       " / ".join(human_duration(b) for b in budgets),
                       ui.dim(" (--turn-timeout MINUTES)")))
            say(ui.rule())
        elif kind == "turn_start":
            say("\n%s %s %s" % (
                ui.dim("round %d" % event["round"]),
                ui.agent_tag(event["agent"], agents),
                ui.dim("(%s, %s) thinking..." % (event.get("role", ""), event.get("backend", ""))),
            ))
        elif kind == "turn_done":
            say("  %s %s  %s" % (
                ui.agent_tag(event["agent"], agents),
                ui.verdict_tag(event.get("verdict", "")),
                ui.dim("state %s%s" % (str(event.get("digest", ""))[:8],
                                       "" if event.get("gate_ok", True)
                                       else ", gate TIMED OUT" if event.get("gate_timed_out")
                                       else ", gate FAILING")),
            ))
            if verbose and event.get("message"):
                msg = event["message"].strip()
                if len(msg) > PREVIEW_CHARS:
                    msg = msg[:PREVIEW_CHARS].rstrip() + " …"
                say(ui.wrap(msg))
            if event.get("ingest") and event["ingest"] != "no change to open issues":
                say("     " + ui.dim(event["ingest"]))
            for note in event.get("notes") or []:
                say("     " + ui.yellow("note: " + note))
        elif kind == "turn_error" and event.get("cut_off"):
            pass   # turn_cut_off already said what happened, and it was not an error
        elif kind == "turn_error":
            # Not clipped at 400: the errors worth reading — a usage limit, a
            # sign-out — carry their fix in the last sentence, and cutting the
            # fix off is how a stopped session becomes a mystery.
            error = str(event.get("error") or "")
            if len(error) > ERROR_CHARS:
                error = error[:ERROR_CHARS].rstrip() + " …"
            colour = ui.yellow if (event.get("cut_off") or event.get("never_happened")) else ui.red
            say("  " + colour("error: ") + ui.wrap(error).lstrip())
        elif kind == "backend_retry":
            say("  " + ui.yellow("backend blip") + ui.dim(
                " (%s) — attempt %s of %s failed; retrying in %ss"
                % (event.get("reason"), event.get("attempt"), event.get("of"),
                   event.get("wait"))))
        elif kind == "turn_cut_off":
            say("  " + ui.yellow("cut off at %s" % event.get("budget"))
                + ui.dim(" — the workspace changed, so %s carries it on. Not a failed turn."
                         % event.get("peer")))
            say("     " + ui.dim("give turns longer with --turn-timeout MINUTES"))
        elif kind == "turn_never_happened":
            say("  " + ui.yellow("the turn never happened")
                + ui.dim(" — %s after %s attempt(s); the round is not spent"
                         % (event.get("reason"), event.get("attempts"))))
        elif kind == "patches":
            for line in event.get("log") or []:
                say("     " + ui.dim("fs: " + line))
        elif kind == "gate_start":
            say("     " + ui.dim("running gate: %s" % event.get("command")))
        elif kind == "gate_done":
            if event.get("ok"):
                say("     " + ui.green("gate passed"))
            elif event.get("timed_out"):
                say("     " + ui.yellow("the gate ran out of time after %s; this is not a test failure"
                                        % human_duration(event.get("timeout")))
                    + ui.dim(" (give it longer with --gate-timeout MINUTES)"))
            else:
                say("     " + ui.red("gate failed (exit %s)" % event.get("exit_code")))
        elif kind == "workflow_proven":
            say("  " + ui.green("✓ `%s` rule met: " % event.get("workflow")) + str(event.get("detail") or ""))
        elif kind == "workflow_veto":
            say("  " + ui.yellow("✗ both signed off, but the `%s` rule is not met" % event.get("workflow")))
            say("    " + ui.dim(ui.wrap(str(event.get("reason") or "")).lstrip()))
            say("    " + ui.dim("sign-offs cleared; both agents have been told why"))
        elif kind == "gate_mutates":
            say("     " + ui.yellow("! the gate writes into the workspace"))
            say("     " + ui.dim("  sign-offs are counted against the files as they were before"))
            say("     " + ui.dim("  it ran, so a gate that changes them can keep the two of them"))
            say("     " + ui.dim("  from ever agreeing on the same state. Have it clean up, or"))
            say("     " + ui.dim("  point it at a scratch directory outside the workspace."))
        elif kind == "decision":
            say("  " + ui.yellow("→ %s: %s" % (event.get("decision"), event.get("reason"))))
        elif kind == "arbitration_start":
            say("\n" + ui.bold(ui.yellow("ARBITRATION")) + " on %s — %s" % (event.get("issue"), event.get("title")))
        elif kind == "arbitration_done":
            say("  " + ui.yellow("ruling by %s: %s" % (event.get("decider"), str(event.get("ruling"))[:200])))
        elif kind == "jury_start":
            say("\n" + ui.bold(ui.yellow("JURY")) + " sits (%s of %s): %s"
                % (event.get("convening"), event.get("of"), ", ".join(event.get("panel") or [])))
        elif kind == "jury_done":
            opened = event.get("opened") or []
            say("  " + ui.yellow(str(event.get("summary"))))
            if opened:
                say("  " + ui.red("reopened with: %s" % ", ".join(opened)))
            if event.get("report"):
                say("  " + ui.dim("jury: " + str(event["report"])))
        elif kind == "jury_failed":
            say("  " + ui.red("the jury could not sit: %s" % event.get("reason")))
        elif kind == "commit":
            say("  " + (ui.green("committed") if event.get("ok") else ui.dim("commit skipped: " + str(event.get("output"))[:120])))
        elif kind == "session_end":
            say("\n" + ui.rule())
            status = event.get("status")
            if status == STATUS_CONSENSUS:
                say(ui.green(ui.bold("BOTH AGENTS SIGNED OFF")) + "  " + ui.dim(str(event.get("reason"))))
            else:
                say(ui.yellow(ui.bold(str(status).upper())) + "  " + str(event.get("reason")))
            say("%s %d   %s %s" % (ui.dim("rounds:"), event.get("rounds", 0),
                                     ui.dim("state:"), str(event.get("digest", ""))[:8]))
            if event.get("report"):
                say(ui.dim("report: " + event["report"]))
            say(ui.rule())

    return report


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def read_task(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).expanduser().read_text(encoding="utf-8")
    task = " ".join(args.task or []).strip()
    if task == "-" or (not task and not sys.stdin.isatty()):
        return sys.stdin.read()
    return task


def read_context(args: argparse.Namespace) -> str:
    """The conversation this task came out of, if the caller passed one.

    `/duet` inside Claude Code or Codex is a handoff, not a fresh start: the
    human has already been talking to one assistant. Carrying that across means
    the pair does not re-ask what was settled ten messages ago.
    """
    parts: List[str] = []
    path = getattr(args, "context_file", None)
    if path:
        if str(path) == "-":
            parts.append(sys.stdin.read())
        else:
            try:
                parts.append(Path(path).expanduser().read_text(encoding="utf-8"))
            except OSError as exc:
                raise SystemExit("could not read --context-file %s: %s" % (path, exc))
    inline = getattr(args, "context", None)
    if inline:
        parts.append(str(inline))
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def turn_timeout_seconds(args: argparse.Namespace) -> Optional[int]:
    """`--turn-timeout MINUTES` as seconds, or None when it was not given.

    Minutes on the command line because that is the unit the limit is felt in —
    a turn is tens of minutes — and seconds everywhere inside, because that is
    what subprocess timeouts take.
    """
    return _minutes_flag(args, "turn_timeout", "a turn", DEFAULT_TURN_TIMEOUT)


def gate_timeout_seconds(args: argparse.Namespace) -> Optional[int]:
    """`--gate-timeout MINUTES` as seconds, or None when it was not given."""
    return _minutes_flag(args, "gate_timeout", "the gate", DEFAULT_GATE_TIMEOUT)


def _minutes_flag(args: argparse.Namespace, name: str, what: str,
                  default: int) -> Optional[int]:
    flag = "--" + name.replace("_", "-")
    minutes = getattr(args, name, None)
    if minutes is None:
        return None
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise SystemExit("%s takes a number of minutes" % flag)
    if minutes < 1:
        raise SystemExit(
            "%s %s: %s needs at least a minute. Leave it out for "
            "the default of %s." % (flag, minutes, what, human_duration(default))
        )
    return minutes * 60


def build_config(args: argparse.Namespace) -> Config:
    root = str(Path(args.root).expanduser().resolve())
    load_env_file(root)
    cfg = load_config(root)
    cfg.root = root

    if not cfg.agents:
        cfg.agents = default_agents()
    if getattr(args, "pair", None):
        try:
            cfg.agents = parse_pair(args.pair)
        except ValueError as exc:
            raise SystemExit(str(exc))
    for name in ("start", "decider"):
        chosen = getattr(args, name, "")
        if chosen and chosen not in cfg.agent_names:
            raise SystemExit(
                "--%s %r is not one of this pair: %s"
                % (name, chosen, ", ".join(cfg.agent_names))
            )

    # Read with getattr: `duet review` shares this builder but defines only the
    # flags it actually offers.
    if getattr(args, "gate", None) is not None:
        cfg.gate = args.gate
    elif not cfg.gate and not getattr(args, "no_gate", False):
        # The flag people forget is the one that keeps "done" honest, so look
        # for it rather than letting a session run on argument alone. What was
        # picked is printed, because a gate you did not choose running the
        # wrong command looks like verification and is not.
        found = gate_detect.detect(cfg.root)
        if found:
            cfg.gate = found
            cfg.gate_was_detected = True
    if getattr(args, "rounds", None) is not None:
        cfg.max_rounds = args.rounds
    turn_timeout = turn_timeout_seconds(args)
    if turn_timeout is not None:
        cfg.turn_timeout = turn_timeout
    gate_timeout = gate_timeout_seconds(args)
    if gate_timeout is not None:
        cfg.gate_timeout = gate_timeout
    if getattr(args, "max_debate", None) is not None:
        cfg.max_debate = args.max_debate
    if getattr(args, "start", ""):
        cfg.start = args.start
    if getattr(args, "decider", ""):
        cfg.decider = args.decider
    if getattr(args, "swap", None) is not None:
        cfg.swap_every = args.swap
    if getattr(args, "commit", False):
        cfg.commit = True
    if getattr(args, "workflow", ""):
        cfg.workflow = args.workflow
    if getattr(args, "allow_run", ""):
        cfg.allow_run = args.allow_run
    if getattr(args, "quick", False):
        cfg.workflow_state["quick"] = True
    if getattr(args, "jury", False):
        cfg.jury = True
        cfg.jury_panel = getattr(args, "panel", "") or ""
    if getattr(args, "accept", None):
        cfg.acceptance = args.accept
    if getattr(args, "accept_file", None):
        cfg.acceptance = Path(args.accept_file).expanduser().read_text(encoding="utf-8")
    return cfg


def refuse_nested(command: str, args: argparse.Namespace) -> Optional[int]:
    """Stop a duet agent from starting another duet session.

    Agents reach for the tools they are told about, and duet is one of them. A
    nested session means the outer agent's turn blocks on a whole second pair of
    agents — which in a real run hit the 30-minute adapter timeout and threw away
    the turn. Reviewing is allowed; running a full session is not.
    """
    session = os.environ.get("DUET_SESSION")
    if not session or getattr(args, "allow_nested", False):
        return None
    print(ui.red("refusing to start a duet session from inside one") + ui.dim(" (%s)" % session))
    print("  You are already an agent in a duet session. Starting another one makes")
    print("  this turn wait on a second pair of agents, which is slow enough to time")
    print("  out and costs twice over.")
    print()
    print("  Do the work yourself and report it in your envelope.")
    print(ui.dim("  If you really mean it: ") + ui.bold("duet %s --allow-nested" % command))
    return 4


def cmd_run(args: argparse.Namespace) -> int:
    nested = refuse_nested(getattr(args, "command", "") or "run", args)
    if nested is not None:
        return nested
    task = read_task(args)
    if not task.strip():
        print(ui.red("no task given."))
        print("usage: duet run \"build a CLI that ...\"  [--gate \"pytest -q\"]")
        return 2

    cfg = build_config(args)
    cfg.task = task
    cfg.context = read_context(args)

    if getattr(args, "panel", None) and not cfg.jury:
        print(ui.red("✗ ") + "--panel chooses who sits on the jury, and there is no jury "
              "without --jury.")
        return 3
    if cfg.jury:
        # Checked before a single turn is spent: a typo in duet-jury.json found
        # only when the pair first signs off has cost the whole session.
        try:
            jury_config = jury.load_config(cfg.root)
            jury.choose_panel(jury_config, cfg.jury_panel)
        except jury.JuryError as exc:
            print(ui.red("✗ ") + "the jury cannot sit: %s" % exc)
            return 3
        cfg.jury_max = jury_config.max_convenings

    if cfg.gate:
        unusable = gate_detect.why_unusable(cfg.gate, cfg.root)
        if unusable:
            print(ui.red("✗ ") + "the acceptance gate cannot run: %s" % unusable)
            print(ui.dim("  gate: ") + cfg.gate)
            print()
            print("  A gate that cannot start fails every turn, and a failing gate")
            print("  vetoes both agents — so the session could never finish.")
            print()
            print(ui.dim("  fix: ") + "install it, give a command that works with "
                  + ui.bold("--gate") + ", or")
            print(ui.dim("       ") + "run without one using " + ui.bold("--no-gate")
                  + ui.dim(" (they can then only agree by argument)"))
            return 3

    problems = preflight(cfg)
    if problems:
        for line in problems:
            print(ui.red("✗ ") + line)
        # Someone who owns one subscription and not two is not a broken
        # install, and telling them to sign in to both reads as "go buy the
        # other one". duet works with either side paired against itself, so
        # offer that as the command they can actually run.
        fallback = _same_vendor_fallback(cfg)
        if fallback:
            pair, why = fallback
            print()
            print(ui.dim("Only have one of the two? ") + why)
            print("  " + ui.bold(rerun_with_pair(pair)))
            print(ui.dim("  Two of the same family share more blind spots than two vendors do,"))
            print(ui.dim("  but the rules that make this work do not depend on them differing."))
        else:
            print("\nfix both sides in one step with " + ui.bold("duet login")
                  + ui.dim("  (or `duet doctor` for the full check)"))
        return 3

    orch = Orchestrator(cfg, reporter=make_reporter(cfg.agent_names, verbose=not args.quiet, as_json=args.json))
    result = orch.run()
    return 0 if result.status in (STATUS_CONSENSUS, STATUS_REVIEWED) else 1


def preflight(cfg: Config) -> List[str]:
    problems: List[str] = []
    for spec in cfg.agents:
        cls = REGISTRY.get(spec.backend)
        if cls is None:
            problems.append("agent %s: unknown backend %r" % (spec.name, spec.backend))
            continue
        probe = cls.probe(spec.options)
        if not probe.ok:
            line = "agent %s (%s): %s" % (spec.name, spec.backend, probe.detail)
            if probe.fix:
                line += "\n    fix: " + probe.fix
            problems.append(line)
    return problems


def _same_vendor_fallback(cfg: Config):
    """A pair that works using only the side that is currently usable."""
    usable = set()
    for spec in cfg.agents:
        cls = REGISTRY.get(spec.backend)
        if cls is None:
            continue
        probe = cls.probe(spec.options)
        # `ok`, not `signed_in`. This recommends a pair that can run right now;
        # an account signed in but out of quota cannot, and doctor is holding
        # that verdict two lines above.
        if probe.ok:
            usable.add(spec.backend)
    if "claude-code" in usable and "codex-cli" not in usable:
        return "claude:opus+claude:sonnet", "Claude alone can still pair two of its models:"
    if "codex-cli" in usable and "claude-code" not in usable:
        return "codex+codex", "ChatGPT alone can still pair two sessions:"
    return None


def rerun_with_pair(pair: str) -> str:
    """The command the user just typed, with the pair swapped in.

    Advice they have to assemble themselves is advice most people skip. This
    is the thing they can paste, arguments and all.
    """
    out: List[str] = []
    skip = False
    for arg in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if arg == "--pair":
            skip = True
            continue
        if arg.startswith("--pair="):
            continue
        out.append(arg)
    out += ["--pair", pair]
    return "duet " + " ".join(shlex.quote(a) for a in out)


def cmd_doctor(args: argparse.Namespace) -> int:
    root = str(Path(args.root).expanduser().resolve())
    loaded = load_env_file(root)
    cfg = load_config(root)
    print(ui.bold("duet %s" % __version__))
    print(ui.dim("workspace: %s" % root))
    if loaded:
        print(ui.dim("loaded from .duet/.env: %s" % ", ".join(loaded)))
    if config_path(root).is_file():
        print(ui.dim("config: %s" % config_path(root)))
    else:
        print(ui.dim("config: none (using defaults) — run `duet init` to pin one"))
    print()

    ok = True
    for spec in cfg.agents:
        cls = REGISTRY.get(spec.backend)
        print(ui.bold("%s" % spec.name) + ui.dim("  (%s)" % spec.backend))
        if cls is None:
            print("  " + ui.red("✗ unknown backend"))
            ok = False
            continue
        probe = cls.probe(spec.options)
        if probe.ok:
            print("  " + ui.green("✓ ") + probe.detail)
        else:
            ok = False
            print("  " + ui.red("✗ ") + probe.detail)
            if probe.fix:
                print("    " + ui.yellow("fix: ") + probe.fix)
        print()

    # optional extras
    from duet.adapters.codex_cli import CodexCliAdapter
    from duet.workspace import Workspace

    if not any(s.backend == "codex-cli" for s in cfg.agents):
        probe = CodexCliAdapter.probe()
        print(ui.dim("optional: codex CLI — ") + (ui.green(probe.detail) if probe.ok else ui.dim(probe.detail)))
        if probe.ok:
            print(ui.dim("  (run `duet init --pair claude+codex` to give ChatGPT its own tools)"))

    ws = Workspace(root)
    print(ui.dim("git repo: ") + ("yes" if ws.is_git_repo else ui.yellow("no — duet works anyway, but a repo makes review much better")))
    if cfg.gate:
        print(ui.dim("gate: ") + cfg.gate)
    else:
        print(ui.yellow("no acceptance gate configured") + ui.dim(" — set one with --gate \"pytest -q\"; it is what keeps \"done\" honest"))

    print()
    if ok:
        print(ui.green(ui.bold("ready.")) + " try: " + ui.bold('duet run "your task here"'))
    else:
        print(ui.red(ui.bold("not ready yet")) + " — run " + ui.bold("duet login")
              + " to sign both sides in, then " + ui.bold("duet doctor") + " again")
        # Not everyone has both subscriptions, and telling someone with one of
        # them that duet is unusable is wrong: two models of the same family
        # still review each other, and a reviewer with no memory of writing the
        # code is a real reviewer. Say so rather than letting them give up.
        fallback = _same_vendor_fallback(cfg)
        if fallback:
            pair, why = fallback
            print()
            print(ui.dim("Only have one of the two? ") + why)
            print("  " + ui.bold('duet run "..." --pair %s' % pair))
            print(ui.dim("  Two of the same family share more blind spots than two vendors do,"))
            print(ui.dim("  but the rules that make this work do not depend on them differing."))
    return 0 if ok else 1


def cmd_init(args: argparse.Namespace) -> int:
    root = str(Path(args.root).expanduser().resolve())
    load_env_file(root)
    cfg = load_config(root)
    cfg.root = root
    if getattr(args, "pair", None):
        try:
            cfg.agents = parse_pair(args.pair)
        except ValueError as exc:
            raise SystemExit(str(exc))
    elif getattr(args, "gpt_backend", None):   # older flag, still honoured
        for spec in cfg.agents:
            if spec.backend in ("codex-cli", "openai-api"):
                spec.backend = args.gpt_backend
    if args.gate is not None:
        cfg.gate = args.gate
    elif not cfg.gate and not getattr(args, "no_gate", False):
        # The flag people forget is the one that keeps "done" honest, so look
        # for it — and say what was picked, since a gate you did not choose
        # running the wrong command is worse than none.
        found = gate_detect.detect(root)
        if found:
            cfg.gate = found
            cfg.gate_was_detected = True
    if args.rounds is not None:
        cfg.max_rounds = args.rounds

    path = save_config(cfg, root)
    print(ui.green("wrote ") + str(path))

    env_path = config_dir(root) / ".env"
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    uses_api = any(s.backend == "openai-api" for s in cfg.agents)
    if uses_api and not has_key:
        print()
        print(ui.yellow("ChatGPT needs an API key."))
        print("  Get one at " + ui.bold("https://platform.openai.com/api-keys") + ", then either:")
        print("    export OPENAI_API_KEY=sk-...")
        print("  or put it in " + ui.bold(str(env_path)) + " as:")
        print("    OPENAI_API_KEY=sk-...")
        print(ui.dim("  (.duet/.env is gitignored by this repo's template; never commit a key)"))

    gitignore = Path(root) / ".gitignore"
    entries = [".duet/sessions/", ".duet/.env", ".duet/cache.json"]
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
        missing = [e for e in entries if e not in existing]
        if missing:
            with gitignore.open("a", encoding="utf-8") as fh:
                fh.write(("\n" if existing and not existing.endswith("\n") else "") + "\n".join(missing) + "\n")
            print(ui.dim("added %s to .gitignore" % ", ".join(missing)))
    except OSError:
        pass

    print()
    return cmd_doctor(args)


def cmd_login(args: argparse.Namespace) -> int:
    """Walk both sign-ins. Neither one involves an API key.

    duet never handles your credentials: each CLI runs its own browser sign-in
    attached to your terminal, and duet only asks afterwards whether it worked.
    """
    from duet.adapters.claude_code import ClaudeCodeAdapter
    from duet.adapters.codex_cli import CodexCliAdapter

    root = str(Path(args.root).expanduser().resolve())
    load_env_file(root)
    cfg = load_config(root)

    plan = []
    for spec in cfg.agents:
        if spec.backend == "claude-code":
            plan.append((spec.name, ClaudeCodeAdapter, ["auth", "login"], "Claude"))
        elif spec.backend == "codex-cli":
            plan.append((spec.name, CodexCliAdapter, ["login"], "ChatGPT"))
        elif spec.backend == "openai-api":
            print(ui.yellow("%s uses the API-key backend (openai-api); nothing to sign in to." % spec.name))
            print(ui.dim("  switch it to a ChatGPT login with: duet init --pair claude+codex"))

    if args.agent:
        plan = [item for item in plan if item[0] == args.agent]
        if not plan:
            print(ui.red("no agent named %r uses a sign-in backend" % args.agent))
            return 2

    failures = 0
    for name, cls, sub, account in plan:
        probe = cls.probe()
        # `signed_in` rather than `ok`: an account that is signed in but out of
        # usage allowance fails the probe, and running `codex login` at it opens
        # a browser for nothing and then reports "still not signed in", which is
        # false. doctor at the end of this command reports the real problem.
        if (probe.signed_in or probe.ok) and not args.force:
            print(ui.green("✓ ") + "%s is already signed in — %s" % (name, probe.detail))
            continue
        agent = cls(name=name, cwd=root)
        binary = agent.bin
        if not Adapter.which(binary):
            print(ui.red("✗ ") + "%s: %s" % (name, probe.detail))
            if probe.fix:
                print("    " + ui.yellow("install it first: ") + probe.fix)
            failures += 1
            continue
        print()
        print(ui.bold("signing %s in with your %s account" % (name, account)))
        print(ui.dim("  running: %s" % " ".join(list(agent.launch) + list(sub))))
        print(ui.dim("  this opens your browser; duet never sees your credentials."))
        try:
            # The launch prefix, not the binary: under the npx fallback the
            # binary is npx itself, and appending `auth login` to it asks the
            # registry for packages by those names instead of signing anyone in.
            code = subprocess.call(list(agent.launch) + list(sub))
        except (OSError, KeyboardInterrupt) as exc:
            print(ui.red("  could not run it: %s" % exc))
            failures += 1
            continue
        after = cls.probe()
        if after.ok:
            print(ui.green("✓ ") + "%s signed in — %s" % (name, after.detail))
        elif after.signed_in:
            # The sign-in worked; something else is not ready. Saying "still not
            # signed in" here would be false, and would send the user round the
            # same browser loop for a problem no login can fix. doctor, which
            # this command ends with, reports the real one.
            print(ui.green("✓ ") + "%s signed in — %s" % (name, after.detail))
            if after.fix:
                print("    " + ui.yellow("but: ") + after.fix)
        else:
            failures += 1
            # Name the same thing the "running:" line above named. This used to
            # print sub[0] — "auth exited 1" — a command that does not exist,
            # and under npx neither word is the program being run.
            print(ui.red("✗ ") + "%s is still not signed in (exited %d)" % (name, code))
            if after.fix:
                print("    " + ui.yellow("try: ") + after.fix)

    print()
    if failures:
        return 1
    if getattr(args, "skip_summary", False):
        return 0
    return cmd_doctor(args)


def skill_dir() -> Path:
    return Path(__file__).resolve().parent / "skill"


def skill_source() -> Path:
    return skill_dir() / "SKILL.md"


# Everything `duet skill install` places, and where each one has to land for its
# host to find it. `/duet` only exists if the file is in the right directory.
SKILL_TARGETS = [
    ("Claude Code skill", "SKILL.md", Path(".claude") / "skills" / "duet" / "SKILL.md",
     "so Claude Code can reach for duet on its own"),
    ("Claude Code /duet", "claude-command.md", Path(".claude") / "commands" / "duet.md",
     "so you can type /duet in Claude Code"),
    # Codex enumerates ~/.codex/skills/*/SKILL.md into its own prompt — you can
    # see it there with `codex debug prompt-input` — so this is the path that is
    # known to work. It is triggered by `$duet` or by matching the description,
    # not by a slash command.
    ("Codex skill", "codex-skill.md", Path(".codex") / "skills" / "duet" / "SKILL.md",
     "so Codex picks up duet — say $duet, or just ask for a second opinion"),
    # Custom slash-command prompts are a newer Codex feature and this location is
    # not confirmed on every version, so it is installed but not promised.
    ("Codex /duet (if supported)", "codex-prompt.md", Path(".codex") / "prompts" / "duet.md",
     "gives /duet on Codex versions that read ~/.codex/prompts"),
]


def _invocation_works(command: str) -> bool:
    """Run it. An invocation nobody has executed is a guess."""
    try:
        proc = subprocess.run(
            "%s --version" % command,
            shell=True, capture_output=True, text=True, timeout=60,
            cwd=tempfile.gettempdir(),      # anywhere but here, like the caller
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "duet" in (proc.stdout or "").lower()


def _candidate_invocations() -> List[str]:
    """Ways to run duet, best first. Every one is a shell command line.

    Each path is quoted: on a machine whose home directory has a space in it
    — `/Users/Ada Lovelace/...`, which is ordinary on macOS — an unquoted
    `PYTHONPATH=/Users/Ada Lovelace/x python -m duet` runs `Lovelace/x` as a
    command and dies with "et: command not found".
    """
    candidates = []
    found = shutil.which("duet")
    if found:
        candidates.append(shlex.quote(found))
    candidates.append("%s -m duet" % shlex.quote(sys.executable))
    # Importable only because of where it happens to live: say so explicitly
    # rather than relying on the caller's working directory.
    package_root = Path(__file__).resolve().parent.parent
    candidates.append("PYTHONPATH=%s %s -m duet"
                      % (shlex.quote(str(package_root)), shlex.quote(sys.executable)))
    return candidates


def duet_invocation_checked() -> Tuple[str, bool]:
    """How to run duet from a session that is not this one, and whether that is
    known to work rather than hoped.

    A spawned Claude Code or Codex session inherits neither this PATH nor this
    working directory. Both previous attempts at this failed in exactly that
    gap: a bare `duet` was not on the session's PATH, and `<python> -m duet`
    only worked from the directory duet was being run out of.

    So each candidate is executed, from somewhere else, before it is written
    into a file that something else will have to run. If none of them runs, the
    caller has to say so: writing the files and printing a tick is a claim that
    `/duet` works, and at that point nothing has shown that it does.
    """
    candidates = _candidate_invocations()
    for candidate in candidates:
        if _invocation_works(candidate):
            return candidate, True
    # Nothing ran. Emit the most explicit form so the failure names a real path.
    return candidates[-1], False


def duet_invocation() -> str:
    return duet_invocation_checked()[0]


def _is_stale_duet_file(current: str, template: str) -> bool:
    """Did duet write this file itself, with a different invocation baked in?

    `{{DUET}}` is the only thing that varies between the packaged template and
    what lands on disk, so a file matching the template everywhere else is ours
    and merely out of date. That happens on any second install where duet has
    moved — a `pip --user` install later replaced by a virtualenv, or a machine
    where `duet` has since reached PATH — and refusing it as "a different
    version is already there" sent an ordinary re-run of `duet setup` to a
    `--force` it should never have needed, and failed the whole command if the
    user did not know to pass it.

    A file someone has actually edited still will not match, so it is still
    protected.
    """
    parts = template.split("{{DUET}}")
    if len(parts) == 1:
        return False
    pattern = "(.+?)".join(re.escape(part) for part in parts)
    match = re.fullmatch(pattern, current, re.DOTALL)
    if match is None:
        return False
    # The same invocation everywhere, or it is not a clean render of ours.
    return len(set(match.groups())) == 1


DEFAULT_START = "<!-- duet:default:start — written by `duet skill default`; remove with `duet skill undefault` -->"
DEFAULT_END = "<!-- duet:default:end -->"
DEFAULT_TARGETS = [
    # Claude Code reads ~/.claude/CLAUDE.md into every session, in every project.
    ("Claude Code", Path(".claude") / "CLAUDE.md"),
    # Codex reads ~/.codex/AGENTS.md the same way.
    ("Codex", Path(".codex") / "AGENTS.md"),
]


def _without_block(text: str) -> str:
    """The file with duet's block taken out, and nothing else changed."""
    start, end = text.find(DEFAULT_START), text.find(DEFAULT_END)
    if start == -1 or end == -1 or end < start:
        return text
    before = text[:start].rstrip("\n")
    after = text[end + len(DEFAULT_END):].lstrip("\n")
    if before and after:
        return before + "\n\n" + after
    return before + "\n" if before else after


def _default_targets(args: argparse.Namespace, home: Path):
    """Which instruction files get duet's block, as (label, absolute path)."""
    if getattr(args, "project", False):
        # Cursor keeps its user rules in a settings screen, not a file — but it
        # reads a project's AGENTS.md, as do Codex, OpenCode and most newer
        # agents. One file in the repository reaches all of them.
        return [("this project", Path(args.root).expanduser().resolve() / "AGENTS.md")]
    targets = [(label, home / relative) for label, relative in DEFAULT_TARGETS]
    if (home / ".gemini").is_dir():
        # Shared by Gemini CLI and Google Antigravity: both read global rules here.
        targets.append(("Gemini + Antigravity", home / ".gemini" / "GEMINI.md"))
    opencode = home / ".config" / "opencode" / "AGENTS.md"
    # Only if it already exists. OpenCode reads ~/.claude/CLAUDE.md *instead*
    # when this file is absent — so creating it to add duet would silently
    # stop OpenCode reading every other rule the user keeps there. Absent, it
    # already gets duet's block through that fallback.
    if opencode.is_file():
        targets.append(("OpenCode", opencode))
    return targets


def cmd_skill_default(args: argparse.Namespace, enable: bool) -> int:
    """Make duet the default for real code changes in every session, or stop.

    A block between two markers in the user-level instructions file each
    harness reads everywhere. Only the text between the markers is ever
    written or removed, so whatever the user already keeps there survives
    both directions, and running either twice changes nothing the second time.
    """
    home = Path(args.dir).expanduser() if args.dir else Path.home()
    if enable:
        # The block tells the model to use the skill, so the skill has to be
        # there first — a default pointing at something missing is worse than none.
        install = argparse.Namespace(**{**vars(args), "action": "install"})
        code = cmd_skill(install)
        if code != 0:
            print(ui.red("✗ ") + "not making duet the default until the skill itself installs.")
            return code
        invocation, _ = duet_invocation_checked()
        body = (skill_dir() / "default.md").read_text(encoding="utf-8").replace("{{DUET}}", invocation)
        block = "%s\n%s\n%s\n" % (DEFAULT_START, body.strip(), DEFAULT_END)
    print()
    for label, target in _default_targets(args, home):
        try:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
        except OSError as exc:
            print(ui.red("✗ ") + "%-12s could not read %s: %s" % (label, target, exc))
            return 1
        stripped = _without_block(current)
        if enable:
            updated = (stripped.rstrip("\n") + "\n\n" if stripped.strip() else "") + block
        else:
            updated = stripped
        if updated == current:
            print(ui.dim("· %-12s %s" % (label, "already so")))
            continue
        try:
            if not updated.strip():
                # The block was all there was — duet created the file, so the
                # undo removes it rather than leaving an empty one behind.
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            print(ui.red("✗ ") + "%-12s could not write %s: %s" % (label, target, exc))
            return 1
        print(ui.green("✓ ") + "%-12s %s  %s" % (label, "duet is the default" if enable else "back to normal",
                                                   ui.dim(str(target))))
    print()
    if enable:
        print("New sessions now hand real code changes to duet — fix, add, refactor,")
        print("build, plan — and do questions and one-line edits directly. They say so")
        print("before starting a session, so you can say no.")
        print(ui.dim("undo: ") + ui.bold("duet skill undefault"))
    return 0


DERIVED_MARK = "written by `duet skill install`"


def _command_body(invocation: str) -> tuple:
    """(description, body) of the /duet command, from the one Claude Code source.

    Other harnesses get the same instructions rather than a copy that drifts:
    only the argument placeholder and the metadata around it differ.
    """
    text = (skill_dir() / "claude-command.md").read_text(encoding="utf-8").replace("{{DUET}}", invocation)
    _, _, rest = text.partition("---\n")
    front, _, body = rest.partition("---\n")
    description = ""
    for line in front.splitlines():
        if line.startswith("description:"):
            description = line.split(":", 1)[1].strip()
    return description, body.lstrip("\n")


def _gemini_command(invocation: str) -> str:
    description, body = _command_body(invocation)
    body = body.replace("$ARGUMENTS", "{{args}}")
    # A TOML multi-line *literal* string: no escapes are processed, so the
    # body goes in byte for byte — it just cannot contain three quotes itself.
    assert "\'\'\'" not in body, "command body would end the TOML literal early"
    return ("# %s — Gemini CLI reads this as the /duet command.\n"
            "description = %s\n"
            "prompt = \'\'\'\n%s\'\'\'\n") % (DERIVED_MARK, json.dumps(description), body)


def _opencode_command(invocation: str) -> str:
    description, body = _command_body(invocation)
    return "---\ndescription: %s\n---\n<!-- %s -->\n\n%s" % (description, DERIVED_MARK, body)


# Harnesses that get /duet only if they are already in use here: a config
# directory for a tool the user has never run is clutter, not support.
DERIVED_TARGETS = [
    ("Gemini CLI /duet", Path(".gemini"), Path(".gemini") / "commands" / "duet.toml", _gemini_command),
    ("OpenCode /duet", Path(".config") / "opencode",
     Path(".config") / "opencode" / "commands" / "duet.md", _opencode_command),
]


def _install_derived(home: Path, invocation: str, force: bool, installed, skipped, failed) -> None:
    for label, harness, relative, build in DERIVED_TARGETS:
        if not (home / harness).is_dir():
            continue
        target = home / relative
        body = build(invocation)
        if target.is_file():
            current = target.read_text(encoding="utf-8", errors="replace")
            if current == body:
                skipped.append((label, target, "already up to date"))
                continue
            if not force and DERIVED_MARK not in current:
                failed.append((label, target, "a different version is already there"))
                continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as exc:
            failed.append((label, target, str(exc)))
            continue
        installed.append((label, target, "so you can type /duet in %s" % label.split(" /")[0]))


def _duet_plugin_installed(home: Path) -> bool:
    """Is the duet Claude Code plugin (`/plugin install duet@duet`) installed?"""
    try:
        data = json.loads((home / ".claude" / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return False
    return any(key.split("@", 1)[0] == "duet" for key in (data.get("plugins") or {}))


def cmd_skill(args: argparse.Namespace) -> int:
    """Install `/duet` into Claude Code and Codex, plus the Claude Code skill."""
    source_dir = skill_dir()
    invocation, invocation_verified = duet_invocation_checked()
    home = Path(args.dir).expanduser() if args.dir else Path.home()

    if args.action in ("default", "undefault"):
        return cmd_skill_default(args, enable=args.action == "default")
    if args.action == "path":
        print(source_dir)
        return 0
    if args.action == "show":
        for label, filename, _, _ in SKILL_TARGETS:
            print(ui.bold("--- %s (%s) ---" % (label, filename)))
            print((source_dir / filename).read_text(encoding="utf-8").replace("{{DUET}}", invocation))
        return 0

    installed, skipped, failed = [], [], []
    plugin = _duet_plugin_installed(home)
    for label, filename, relative, why in SKILL_TARGETS:
        source = source_dir / filename
        target = home / relative
        if plugin and relative.parts[0] == ".claude":
            # The plugin already gives Claude Code /duet and the skill; a second
            # copy here would put two of each in front of the model.
            skipped.append((label, target, "provided by the duet plugin"))
            continue
        if not source.is_file():
            failed.append((label, target, "packaged file missing: %s" % source))
            continue
        template = source.read_text(encoding="utf-8")
        body = template.replace("{{DUET}}", invocation)
        if target.is_file():
            current = target.read_text(encoding="utf-8", errors="replace")
            if current == body:
                skipped.append((label, target, "already up to date"))
                continue
            if not args.force and not _is_stale_duet_file(current, template):
                failed.append((label, target, "a different version is already there"))
                continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as exc:
            failed.append((label, target, str(exc)))
            continue
        installed.append((label, target, why))

    _install_derived(home, invocation, args.force, installed, skipped, failed)

    for label, target, why in installed:
        print(ui.green("✓ ") + "%-20s %s" % (label, ui.dim(str(target))))
    for label, target, note in skipped:
        print(ui.dim("· %-20s %s" % (label, note)))
    for label, target, note in failed:
        print(ui.red("✗ ") + "%-20s %s" % (label, note))
        print(ui.dim("    " + str(target)))

    if failed:
        if any("already there" in note for _, _, note in failed):
            print()
            print(ui.dim("overwrite them with: ") + ui.bold("duet skill install --force"))
        return 1

    if not invocation_verified:
        # The files are in the right places, so they stay — but every way of
        # running duet from somewhere that is not this process failed, and the
        # next thing printed used to be "In a new Claude Code session: /duet ...".
        # That is the project's own failure class: a tick nothing has earned.
        bindir = Path(sys.executable).resolve().parent
        print()
        print(ui.red("✗ ") + "the files are written, but /duet will not work yet.")
        print("  They have to call duet as " + ui.bold(invocation) + ",")
        print("  and that command does not answer " + ui.bold("--version") + " from another")
        print("  directory — so a Claude Code or Codex session could not run it either.")
        print("  " + ui.yellow("fix: ") + "put duet on your PATH and install again:")
        print("    " + ui.bold('export PATH="%s:$PATH"' % bindir))
        print("    " + ui.bold("duet skill install --force"))
        return 1

    if installed:
        print()
        print("In a new " + ui.bold("Claude Code") + " session:")
        print(ui.dim("  /duet ") + "add retry with backoff to the fetch client")
        print(ui.dim("  /duet ") + "running       " + ui.dim("— check on a session already going"))
        print(ui.dim("  /duet ") + "review        " + ui.dim("— second opinion on the current diff"))
        print()
        print("In a new " + ui.bold("Codex") + " session, duet is a skill rather than a slash")
        print("command, so name it or just ask:")
        print(ui.dim("  $duet ") + "add retry with backoff to the fetch client")
        print(ui.dim("  ") + '"get a second opinion on this from Claude"')
        print()
        print(ui.dim("Either way it carries your conversation across, so you do not start cold."))
        if not shutil.which("duet"):
            print()
            print(ui.yellow("note: ") + "`duet` is not on your PATH, so the installed files")
            print("  call it as " + ui.bold(invocation) + " instead.")
            print(ui.dim("  Re-run `duet skill install --force` if that ever moves."))
    return 0


AGENT_PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
}


def _missing_agent_clis() -> List[str]:
    """Which agent CLIs duet cannot already reach.

    Asks the adapters rather than PATH. They also resolve DUET_CLAUDE_BIN,
    ~/.claude/local/claude and ~/.local/bin/codex, so a PATH-only check called
    a CLI missing on a machine where duet runs it perfectly well — and since
    setup gained a hard stop on declining the install, that mistake could abort
    setup outright.
    """
    from duet.adapters.claude_code import DEFAULT_CANDIDATES as CLAUDE_CANDIDATES
    from duet.adapters.claude_code import ClaudeCodeAdapter
    from duet.adapters.codex_cli import DEFAULT_CANDIDATES as CODEX_CANDIDATES
    from duet.adapters.codex_cli import CodexCliAdapter

    missing = []
    for name, cls, candidates in (("claude", ClaudeCodeAdapter, CLAUDE_CANDIDATES),
                                  ("codex", CodexCliAdapter, CODEX_CANDIDATES)):
        _, binary, globally = cls.resolve_launch(candidates)
        if not binary or not globally:
            missing.append(name)
    return missing


def _npm_global_bin() -> Optional[str]:
    """Where `npm install -g` puts commands, if npm will say.

    `npm bin -g` was removed in npm 9, so fall back to `npm prefix -g`, which
    has been there throughout and prints the directory whose `bin` holds them.
    """
    for argv, suffix in ((["npm", "bin", "-g"], None), (["npm", "prefix", "-g"], "bin")):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            path = out.splitlines()[-1].strip()
            return str(Path(path) / suffix) if suffix else path
    return None


def _ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input("%s [y/N] " % question).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def cmd_setup(args: argparse.Namespace) -> int:
    """Everything between `pip install` and a working `/duet`, in one command.

    Four steps used to be four things to remember, and the one people skipped
    was whichever came last. This runs them in order, skips what is already
    done, and stops at the first thing it cannot do for you.
    """
    step = 0

    def heading(text: str) -> None:
        nonlocal step
        step += 1
        print()
        print(ui.bold("%d. %s" % (step, text)))

    print(ui.bold("duet setup") + ui.dim("  — agent CLIs, sign-ins, and /duet"))

    # 1 -----------------------------------------------------------------
    heading("the two agent CLIs")
    missing = _missing_agent_clis()
    if not missing:
        print(ui.green("   ✓ ") + "claude and codex are both installed")
    else:
        packages = [AGENT_PACKAGES[name] for name in missing]
        command = "npm install -g %s" % " ".join(packages)
        has_npx, has_npm = Adapter.which("npx"), Adapter.which("npm")
        print("   not installed globally: %s" % ", ".join(missing))

        if not has_npx and not has_npm:
            print(ui.red("   ✗ ") + "neither npx nor npm is available, so I cannot run them.")
            print("     Install Node, which brings both, then run:")
            print("       " + ui.bold(command))
            return 1

        # duet can drive both CLIs through npx, so installing them globally is
        # a speed choice rather than a requirement — and it is the only step of
        # setup that changes anything outside duet's own directory.
        if has_npx:
            print(ui.green("   ✓ ") + "duet can run them with npx, so this is optional.")
            print(ui.dim("     A global install makes every turn start faster:"))
        else:
            print(ui.dim("   this installs them globally with npm:"))
        print("       " + ui.bold(command))
        # Asked on every path. This is the one step of setup that changes
        # anything outside duet's own directory, and for a while it did so
        # unprompted whenever npx happened to be missing.
        wants_global = bool(has_npm) and _ask("   install them globally?", args.yes)
        if not wants_global:
            if has_npx:
                print(ui.dim("   continuing with npx — nothing installed globally."))
            else:
                print(ui.yellow("   skipped") + " — run that yourself, then `duet setup` again.")
                return 1

        if wants_global:
            if not has_npm:
                print(ui.red("   ✗ ") + "npm is not installed, so I cannot install them.")
                print("     Install Node, then run:  " + ui.bold(command))
                return 1
            code = subprocess.call(command, shell=True)
            still_missing = _missing_agent_clis()
            if code == 0 and still_missing:
                # npm said it worked, and almost certainly did — into a
                # directory that is not on this PATH. "Run it yourself and try
                # again" is the wrong instruction: it lands in the same place.
                print(ui.red("   ✗ ") + "npm reported success, but %s is still not on your PATH."
                      % " or ".join(still_missing))
                npm_bin = _npm_global_bin()
                if npm_bin:
                    print("     npm installs global commands into " + ui.bold(npm_bin) + ".")
                    print("     Add it to your shell profile:")
                    print("       " + ui.bold('export PATH="%s:$PATH"' % npm_bin))
                else:
                    print("     Run " + ui.bold("npm prefix -g") + " to find where it put them, and")
                    print("     add that directory's " + ui.bold("bin") + " to your PATH.")
                if has_npx:
                    print(ui.dim("     duet will use npx meanwhile, so this is not fatal."))
                else:
                    print("     Then run " + ui.bold("duet setup") + " again.")
                    return 1
            elif code != 0 or still_missing:
                if has_npx:
                    print(ui.yellow("   ! ") + "that did not finish cleanly — continuing with npx.")
                else:
                    print(ui.red("   ✗ ") + "that did not finish cleanly. Run it yourself and re-run setup.")
                    return 1
            else:
                print(ui.green("   ✓ ") + "installed")

    # 2 -----------------------------------------------------------------
    heading("sign in to both")
    print(ui.dim("   your Claude and ChatGPT plans — no API keys. Each CLI opens its"))
    print(ui.dim("   own browser sign-in; duet never sees a credential."))
    login_args = argparse.Namespace(root=args.root, quiet=args.quiet, json=False,
                                    agent=None, force=False, skip_summary=True)
    cmd_login(login_args)

    # Ask the narrow question directly. `duet login` exits on readiness, which
    # is right for scripting but wrong here: an exhausted allowance is not a
    # failed sign-in, and reading it as one stopped setup before installing
    # /duet — the step that needs no quota at all.
    cfg_now = load_config(str(Path(args.root).expanduser().resolve()))
    not_signed_in = []
    for spec in cfg_now.agents:
        cls = REGISTRY.get(spec.backend)
        if cls is None:
            continue
        probe = cls.probe(spec.options)
        # None means the probe did not answer the narrow question, so fall back
        # to readiness — the same reading `duet login` uses.
        signed = probe.signed_in if probe.signed_in is not None else probe.ok
        if not signed:
            not_signed_in.append((spec.name, probe))
    if not_signed_in:
        print()
        for name, probe in not_signed_in:
            print(ui.red("   ✗ ") + "%s: %s" % (name, probe.detail))
            if probe.fix:
                print("     " + ui.yellow("fix: ") + probe.fix)
        print()
        print(ui.yellow("   finish the sign-ins above, then run ") + ui.bold("duet setup")
              + ui.yellow(" again."))
        return 1
    for spec in cfg_now.agents:
        cls = REGISTRY.get(spec.backend)
        probe = cls.probe(spec.options) if cls else None
        if probe and probe.signed_in is True and not probe.ok:
            print(ui.yellow("   ! ") + "%s: %s" % (spec.name, probe.detail))
            print(ui.dim("     signed in, so setup continues — this does not affect /duet."))

    # 3 -----------------------------------------------------------------
    heading("install /duet into Claude Code and Codex")
    skill_args = argparse.Namespace(root=args.root, quiet=args.quiet, json=False,
                                    action="install", dir=None, force=args.force)
    if cmd_skill(skill_args) != 0:
        return 1

    # 4 -----------------------------------------------------------------
    print()
    print(ui.green(ui.bold("done.")))
    print("Open a new Claude Code session and type " + ui.bold("/duet")
          + ", or in Codex " + ui.bold("$duet") + ".")
    print(ui.dim("Nothing to remember: it takes the conversation you are already in."))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the whole loop with scripted agents: no keys, no network."""
    from duet.adapters.mock import MockAdapter, envelope

    root = args.root if args.root != "." else tempfile.mkdtemp(prefix="duet-demo-")
    root = str(Path(root).expanduser().resolve())
    Path(root).mkdir(parents=True, exist_ok=True)

    task = "Write greet.py: a function greet(name) returning 'Hello, <name>!' that rejects an empty name."
    gate = "%s greet.py" % shlex.quote(sys.executable)

    good = (
        "def greet(name):\n"
        "    if not name or not name.strip():\n"
        "        raise ValueError('name must not be empty')\n"
        "    return 'Hello, %s!' % name.strip()\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    assert greet('Ada') == 'Hello, Ada!'\n"
        "    try:\n"
        "        greet('  ')\n"
        "    except ValueError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('empty name must raise')\n"
        "    print('ok')\n"
    )
    weak = "def greet(name):\n    return 'Hello, ' + name + '!'\n"

    claude = MockAdapter(name="claude", cwd=root, config={"script": [
        envelope("First pass at greet.py.", "CONTINUE",
                 patches=[{"path": "greet.py", "action": "write", "content": weak}],
                 summary="initial greet()"),
        envelope("Fair catch — empty names now raise, and I added a self-check so the gate proves it.",
                 "CONTINUE", resolves=["no-empty-name-validation"],
                 patches=[{"path": "greet.py", "action": "write", "content": good}],
                 summary="validate empty names"),
        envelope("Checked it against the task: both requirements are covered and the gate is green. Shipping.",
                 "DONE", confidence=0.9),
    ]})
    gpt = MockAdapter(name="gpt", cwd=root, config={"script": [
        envelope("This ignores half the task: an empty name returns 'Hello, !' instead of being rejected.",
                 "CONTINUE", issues=[{"id": "no-empty-name-validation",
                                      "title": "empty name is accepted",
                                      "severity": "blocker",
                                      "detail": "The task says an empty name must be rejected. Raise ValueError on empty or whitespace-only input."}]),
        envelope("Verified: whitespace-only input raises and the gate runs the check. I agree this is done.",
                 "DONE", confidence=0.9),
    ]})

    cfg = Config(task=task, root=root, gate=gate, max_rounds=8,
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")], start="claude")
    orch = Orchestrator(cfg, reporter=make_reporter(["claude", "gpt"], verbose=not args.quiet, as_json=args.json),
                        adapters={"claude": claude, "gpt": gpt})
    result = orch.run()
    if not args.json:
        print()
        print(ui.dim("that was the real orchestrator with scripted peers — "
                     "same envelopes, same gate, same double sign-off."))
        print(ui.dim("demo workspace: %s" % root))
        print(ui.dim("see the whole argument:  ") + ui.bold("duet report --transcript -C %s" % root))
    return 0 if result.status == STATUS_CONSENSUS else 1


def _sessions(root: str) -> List[Path]:
    base = Path(root).resolve() / ".duet" / "sessions"
    if not base.is_dir():
        return []
    return sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.name)


def _has_any_source(root: str) -> bool:
    """Is there anything here that a session could be about?"""
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        return False
    for path in base.iterdir():
        if path.name.startswith(".") or path.name == "__pycache__":
            continue
        return True
    return False


def cmd_sessions(args: argparse.Namespace) -> int:
    sessions = _sessions(args.root)
    if not sessions:
        print("no sessions yet in %s" % (Path(args.root).resolve() / ".duet" / "sessions"))
        return 0
    for path in sessions:
        status = "?"
        try:
            data = json.loads((path / "state.json").read_text(encoding="utf-8"))
            result = data.get("result") or {}
            status = result.get("status") or "incomplete"
        except (OSError, ValueError):
            pass
        mark = ui.green(status) if status == STATUS_CONSENSUS else ui.yellow(status)
        print("%s  %s" % (path.name, mark))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    sessions = _sessions(args.root)
    if args.session:
        sessions = [p for p in sessions if p.name == args.session] or sessions
    if not sessions:
        print("no sessions found")
        return 1
    target = sessions[-1] if not args.session else sessions[0]
    name = "transcript.md" if args.transcript else "report.md"
    path = target / name
    if not path.is_file():
        print("no %s for session %s" % (name, target.name))
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


# --------------------------------------------------------------------------
# verify: does the last sign-off still describe this workspace?
# --------------------------------------------------------------------------
def _read_events(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    try:
        with (path / "events.jsonl").open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue          # a half-written line from a live session
    except OSError:
        return []
    return events


def cmd_status(args: argparse.Namespace) -> int:
    """Where is the session up to, right now.

    A session runs for many minutes and the person who started it walked away.
    This answers "is it still going, and what are they arguing about" without
    reading a transcript — and works while the session is still running, from
    the event log rather than the final report.
    """
    sessions = _sessions(args.root)
    if args.session:
        sessions = [p for p in sessions if p.name == args.session]
        if not sessions:
            print(ui.red("no session %r in %s" % (args.session, Path(args.root).resolve() / ".duet" / "sessions")))
            return 2
    if not sessions:
        print("no sessions yet in %s" % (Path(args.root).resolve() / ".duet" / "sessions"))
        # In an empty directory `duet run` is the wrong suggestion: there is
        # nothing to detect a gate from, which is the case `duet build` exists
        # for. Pointing at the command that cannot work here is a small lie
        # told at the moment someone is deciding whether the tool is for them.
        if build_cmd.has_tests(args.root) or _has_any_source(args.root):
            print(ui.dim("start one with ") + ui.bold('duet run "..."'))
        else:
            print(ui.dim("nothing here yet — start from scratch with "))
            print("  " + ui.bold('duet build "what you want to build"'))
        return 2

    path = sessions[-1]
    events = _read_events(path)
    if not events:
        print(ui.yellow("session %s has not recorded anything yet" % path.name))
        return 2

    start = next((e for e in events if e.get("kind") == "session_start"), {})
    end = next((e for e in events if e.get("kind") == "session_end"), None)
    turns = [e for e in events if e.get("kind") == "turn_done"]
    agents = list((start.get("agents") or {}).keys()) or ["?"]

    last_event = events[-1]
    age = max(0, int(time.time() - float(last_event.get("t") or time.time())))
    running = end is None

    if args.json:
        print(json.dumps({
            "session": path.name,
            "running": running,
            "status": (end or {}).get("status"),
            "reason": (end or {}).get("reason"),
            "rounds": (turns[-1].get("round") if turns else 0),
            "max_rounds": start.get("max_rounds"),
            "task": start.get("task"),
            "verdicts": {a: next((t.get("verdict") for t in reversed(turns) if t.get("agent") == a), None)
                         for a in agents},
            "waiting_on": (last_event.get("agent") if running and last_event.get("kind") == "turn_start" else None),
            "seconds_since_last_event": age,
            "gate_ok": next((t.get("gate_ok") for t in reversed(turns)), None),
        }, default=str))
        return 0 if not running and (end or {}).get("status") == STATUS_CONSENSUS else (0 if running else 1)

    print(ui.bold("duet status") + "  " + ui.dim(path.name))
    task = (start.get("task") or "").strip().splitlines()
    if task:
        print("  task:    " + task[0][:78] + ("…" if len(task[0]) > 78 else ""))
    print("  agents:  " + ", ".join(agents))

    if running:
        waiting = last_event.get("agent") if last_event.get("kind") == "turn_start" else None
        round_no = last_event.get("round") or (turns[-1].get("round") if turns else 0)
        if waiting:
            print("  " + ui.yellow("running") + "   round %s of %s — waiting on %s for %dm %02ds"
                  % (round_no, start.get("max_rounds", "?"), ui.bold(waiting), age // 60, age % 60))
        else:
            print("  " + ui.yellow("running") + "   round %s of %s — last event %dm %02ds ago"
                  % (round_no, start.get("max_rounds", "?"), age // 60, age % 60))
    else:
        status = (end or {}).get("status")
        mark = ui.green("finished") if status == STATUS_CONSENSUS else ui.yellow(str(status))
        print("  " + mark + "  " + str((end or {}).get("reason") or ""))

    for agent in agents:
        last = next((t for t in reversed(turns) if t.get("agent") == agent), None)
        if last is None:
            print("  %-8s %s" % (agent, ui.dim("no turn yet")))
            continue
        print("  %-8s %s %s" % (agent, ui.verdict_tag(str(last.get("verdict"))),
                                ui.dim("round %s" % last.get("round"))))
        message = (last.get("message") or "").strip().replace("\n", " ")
        if message and not args.quiet:
            print(ui.wrap(message[:240] + ("…" if len(message) > 240 else ""), indent="           "))

    open_ids: List[str] = []
    for turn in turns:
        for issue_id in turn.get("issues") or []:
            if issue_id not in open_ids:
                open_ids.append(issue_id)
        for issue_id in turn.get("resolves") or []:
            if issue_id in open_ids:
                open_ids.remove(issue_id)
    if open_ids:
        print("  arguing about: " + ", ".join(open_ids[:6]))

    last_gated = next((t for t in reversed(turns) if t.get("gate_ok") is not None), None)
    if last_gated is not None:
        if last_gated.get("gate_ok"):
            shown = ui.green("passing")
        elif last_gated.get("gate_timed_out"):
            shown = ui.yellow("ran out of time — not a test failure (--gate-timeout MINUTES)")
        else:
            shown = ui.red("failing")
        print("  gate:    " + shown)
    if running:
        print()
        print(ui.dim("  follow it live: ") + ui.bold("duet report --transcript"))
    return 0 if running or (end or {}).get("status") == STATUS_CONSENSUS else 1


VERIFY_OK = 0        # signed state still present, gate still green
VERIFY_STALE = 1     # a sign-off exists but no longer holds
VERIFY_NOTHING = 2   # nothing to verify against (no session, or none recorded)


def _session_state(path: Path) -> Optional[Dict[str, Any]]:
    """The saved state of one session, or None if it never wrote one."""
    try:
        return json.loads((path / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _last_recorded_session(root: str) -> Tuple[Optional[Path], Optional[Dict[str, Any]], List[str]]:
    """The newest session that actually saved state, plus the ones skipped.

    A session killed before its first save leaves a directory with nothing but
    events in it. There is no sign-off in such a directory to check, so verify
    walks back to the newest one that has a state file and says which it used.
    """
    skipped: List[str] = []
    for path in reversed(_sessions(root)):
        data = _session_state(path)
        if data is not None:
            return path, data, skipped
        skipped.append(path.name)
    return None, None, skipped


def _signed_state(data: Dict[str, Any]) -> Tuple[str, Dict[str, Dict[str, Any]], str]:
    """The state id both agents signed, the per-agent sign-offs, and why not.

    Returns ("", signoffs, reason) unless every agent signed off on one and the
    same digest — a single DONE is not a sign-off, and two DONEs against
    different states are not either.
    """
    state = data.get("state") or {}
    signoffs = {k: v for k, v in (state.get("signoffs") or {}).items() if isinstance(v, dict)}
    agents = list(state.get("agents") or [])
    if not agents:
        agents = [a.get("name") for a in (data.get("config") or {}).get("agents") or []]
    missing = [a for a in agents if a not in signoffs]
    if not signoffs and not agents:
        return "", signoffs, "the session recorded no sign-off"
    if not signoffs or missing:
        return "", signoffs, "%s never signed off" % ", ".join(missing or agents)
    digests = {str(s.get("digest") or "") for s in signoffs.values()}
    if len(digests) > 1:
        detail = ", ".join("%s %s" % (a, str(s.get("digest") or "?")[:8]) for a, s in sorted(signoffs.items()))
        return "", signoffs, "the agents signed off on different states (%s)" % detail
    only = digests.pop()
    if not only:
        return "", signoffs, "the recorded sign-off has no state id"
    return only, signoffs, ""


def cmd_verify(args: argparse.Namespace) -> int:
    from duet.workspace import Workspace

    root = str(Path(args.root).expanduser().resolve())
    as_json = getattr(args, "json", False)
    out: Dict[str, Any] = {"kind": "verify", "root": root}

    def finish(code: int, reason: str) -> int:
        if as_json:
            out["reason"] = reason
            out["ok"] = code == VERIFY_OK
            out["exit_code"] = code
            print(json.dumps(out, default=str), flush=True)
        return code

    sessions = _sessions(root)
    skipped: List[str] = []
    if getattr(args, "session", None):
        match = [p for p in sessions if p.name == args.session]
        out["session"] = args.session
        if not match:
            if not as_json:
                print(ui.red("no session %r in %s" % (args.session, Path(root) / ".duet" / "sessions")))
            return finish(VERIFY_NOTHING, "no such session")
        path = match[0]
        data = _session_state(path)
        if data is None:
            if not as_json:
                print(ui.yellow("session %s saved no state.json — there is no sign-off in it to check" % path.name))
            return finish(VERIFY_NOTHING, "session recorded no state")
    else:
        path, data, skipped = _last_recorded_session(root)

    if data is None or path is None:
        where = Path(root) / ".duet" / "sessions"
        if not as_json:
            if skipped:
                print(ui.yellow("no session here recorded a sign-off") +
                      ui.dim(" (%d session dir(s) saved no state.json)" % len(skipped)))
            else:
                print("no sessions yet in %s" % where)
            print(ui.dim("nothing has been signed off in this workspace — run ")
                  + ui.bold('duet run "..."') + ui.dim(" first."))
        out["session"] = None
        return finish(VERIFY_NOTHING, "no session has recorded a sign-off here")

    signed, signoffs, why_not = _signed_state(data)
    cfg_data = data.get("config") or {}
    result = data.get("result") or {}
    gate_cmd = args.gate if getattr(args, "gate", None) is not None else (cfg_data.get("gate") or "")
    try:
        timeout = int(cfg_data.get("gate_timeout") or DEFAULT_GATE_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_GATE_TIMEOUT

    ws = Workspace(root, gate=gate_cmd or None, gate_timeout=timeout)
    # Digest first, gate second: a gate is free to write files (coverage data,
    # build output), and hashing after it ran would compare the signed state
    # against a workspace the gate itself had already changed.
    now = ws.digest()
    matches = bool(signed) and signed == now
    gate = ws.run_gate()

    out.update(
        session=path.name,
        outcome=result.get("status") or "incomplete",
        signed_digest=signed or None,
        signoffs={a: {"round": s.get("round"), "digest": s.get("digest")} for a, s in sorted(signoffs.items())},
        current_digest=now,
        match=matches,
        skipped_sessions=skipped,
        gate={
            "command": gate.command,
            "skipped": gate.skipped,
            "ok": gate.ok,
            "exit_code": gate.exit_code,
        },
    )

    if not as_json:
        print(ui.bold("duet verify") + "  " + ui.dim(root))
        if skipped:
            print(ui.dim("  (skipped %s — saved no state.json)" % ", ".join(skipped)))
        print("  %s %s  %s" % (ui.dim("session:  "), path.name,
                               ui.dim("outcome: %s" % (result.get("status") or "incomplete"))))
        if signed:
            who = ", ".join("%s r%s" % (a, s.get("round", "?")) for a, s in sorted(signoffs.items()))
            print("  %s %s  %s" % (ui.dim("signed:   "), signed, ui.dim("(%s)" % who)))
        else:
            print("  %s %s" % (ui.dim("signed:   "), ui.yellow("none — " + why_not)))
        print("  %s %s" % (ui.dim("now:      "), now))
        print("  %s %s" % (ui.dim("match:    "),
                           ui.green("yes") if matches else ui.red("no") if signed else ui.yellow("n/a")))
        if gate.skipped:
            print("  %s %s" % (ui.dim("gate:     "), ui.yellow("none recorded for this session")
                              + ui.dim(" — re-run with --gate \"...\" to check one")))
        else:
            print("  %s %s  %s" % (ui.dim("gate:     "), gate.command,
                                   ui.green("passed") if gate.ok
                                   else ui.yellow(gate.headline()) if gate.timed_out
                                   else ui.red(gate.headline())))
        print()

    if not signed:
        if not as_json:
            print(ui.red("that session never produced a double sign-off") + " — " + why_not + ".")
            print(ui.dim("nothing here was agreed, so there is nothing to still hold."))
        return finish(VERIFY_STALE, why_not)

    problems: List[str] = []
    if not matches:
        problems.append("the workspace has changed since sign-off")
    if not gate.ok:
        problems.append("the acceptance gate no longer passes")

    if not problems:
        if not as_json:
            print(ui.green(ui.bold("the sign-off still holds.")) + " " +
                  ("gate re-run and green." if not gate.skipped else "no gate was recorded, so none was re-run."))
        return finish(VERIFY_OK, "sign-off still holds")

    if not as_json:
        print(ui.red(ui.bold("the sign-off no longer holds.")))
        for line in problems:
            print("  " + ui.red("✗ ") + line)
        if not matches:
            print(ui.dim("  the state both agents approved was %s; this workspace is %s." % (signed, now)))
            print(ui.dim("  see what changed with: ") + ui.bold("git diff") +
                  ui.dim("   then re-run: ") + ui.bold('duet run "..."'))
        if not gate.ok and not gate.skipped:
            print()
            print(gate.render(2000))
    return finish(VERIFY_STALE, "; ".join(problems))


# --------------------------------------------------------------------------
# resume: carry on an interrupted session instead of starting it over
# --------------------------------------------------------------------------
RESUME_NOTHING = 2     # nothing here can be carried on


def _resume_blocker(data: Dict[str, Any], session: str, max_rounds: int) -> Tuple[str, List[str]]:
    """Why this session cannot be carried on, and what to type instead.

    Returns ("", []) when it can be. A session that agreed is finished, and one
    that used its whole budget needs a bigger one — neither is a case where
    quietly doing nothing helps anybody.
    """
    result = data.get("result") or {}
    if (result.get("status") or "") == STATUS_CONSENSUS:
        return (
            "session %s already reached consensus — both agents signed off, so there "
            "is no argument left to carry on" % session,
            ["check that the sign-off still holds:  duet verify %s" % session,
             "or start a fresh session:             duet run \"...\""],
        )
    # A session with no task recorded is not a session anyone can carry on, and
    # resuming one would hand two agents an empty brief for twenty minutes of
    # paid time. The only way to get one is a truncated or hand-written file.
    if not str((data.get("config") or {}).get("task") or "").strip():
        return (
            "session %s recorded no task, so there is nothing to carry on" % session,
            ["its state.json is truncated or was written by hand;",
             "start a fresh session with:  duet run \"...\""],
        )
    taken = rounds_taken(data)
    if taken >= max_rounds:
        return (
            "session %s has no rounds left — it has taken %d, and the budget is %d"
            % (session, taken, max_rounds),
            # Said plainly because --rounds reads as "extra rounds" to about half
            # the people who type it, and that misreading lands them right here.
            ["--rounds sets the new total, not rounds on top of the ones used;",
             "give it a bigger one:  duet resume %s --rounds %d" % (session, taken + 4)],
        )
    return "", []


def _resumed_session_id(previous: str) -> str:
    """An id for the resumed session that sorts after the one it continues.

    Sessions are found and ordered by name, and a name carries only whole
    seconds. Resume a session in the same second it last saved — a short one,
    or a scripted one — and the new session sorts *before* it, so the next
    `duet resume` would pick the dead one again and fork the argument.
    """
    session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    if session_id <= previous:
        session_id = previous + "-r" + uuid.uuid4().hex[:3]
    return session_id


def cmd_resume(args: argparse.Namespace) -> int:
    """Pick up a session that died mid-argument, at the round after its last.

    A session is 20-40 minutes of two paid subscriptions. Losing one to a
    timeout and starting over pays for the same argument twice, so this rebuilds
    the orchestrator from the state file the dead session was already writing
    after every turn.
    """
    nested = refuse_nested("resume", args)
    if nested is not None:
        return nested

    root = str(Path(args.root).expanduser().resolve())
    as_json = getattr(args, "json", False)
    where = Path(root) / ".duet" / "sessions"

    def refuse(reason: str, fixes: Sequence[str] = (), code: int = RESUME_NOTHING,
               session: Optional[str] = None) -> int:
        if as_json:
            print(json.dumps({"kind": "resume", "ok": False, "resumed": False, "root": root,
                              "session": session, "error": reason, "fix": list(fixes),
                              "exit_code": code}, default=str), flush=True)
        else:
            print(ui.red("✗ ") + reason)
            for fix in fixes:
                print(ui.dim("  " + fix))
        return code

    load_env_file(root)

    skipped: List[str] = []
    finished: List[Tuple[Path, Dict[str, Any]]] = []
    if getattr(args, "session", None):
        match = [p for p in _sessions(root) if p.name == args.session]
        if not match:
            return refuse(
                "no session %r in %s" % (args.session, where),
                ["see what is there:  duet sessions -C %s" % root],
                session=args.session,
            )
        path = match[0]
        data = _session_state(path)
        if data is None:
            return refuse(
                "session %s saved no state.json, so there is nothing to carry on from" % path.name,
                ["it died before finishing a single turn — there is no argument to keep;",
                 "start it again with:  duet run \"...\""],
                session=path.name,
            )
    else:
        path = None
        data = None
        for candidate in reversed(_sessions(root)):
            candidate_data = _session_state(candidate)
            if candidate_data is None:
                skipped.append(candidate.name)
                continue
            # A session that agreed is over, and stepping past it is what "the
            # newest resumable session" means. One that merely ran out of rounds
            # is a different thing: it is exactly what you meant to resume, and
            # it needs one flag. Carrying on an older argument instead of saying
            # so would answer a question nobody asked.
            if ((candidate_data.get("result") or {}).get("status") or "") == STATUS_CONSENSUS:
                finished.append((candidate, candidate_data))
                continue
            path, data = candidate, candidate_data
            break
        if path is None or data is None:
            if finished:
                newest, newest_data = finished[0]
                reason, fixes = _resume_blocker(
                    newest_data, newest.name,
                    Config.from_dict(newest_data.get("config") or {}).max_rounds,
                )
                return refuse(reason, fixes, session=newest.name)
            if skipped:
                return refuse(
                    "no session in %s saved any state — every one of them died before "
                    "finishing a turn (%s)" % (where, ", ".join(skipped[:6])),
                    ["start one with:  duet run \"...\""],
                )
            return refuse(
                "no sessions yet in %s" % where,
                ["start one with:  duet run \"...\""],
            )

    cfg = Config.from_dict(data.get("config") or {})
    cfg.root = root                       # the session may have been copied or moved
    if getattr(args, "gate", None) is not None:
        cfg.gate = args.gate
    if getattr(args, "rounds", None) is not None:
        cfg.max_rounds = args.rounds
    # Not carried over silently: the session that died on its turn limit is
    # exactly the one being resumed, and raising the limit was the reason
    # hand-editing state.json was the only way out.
    turn_timeout = turn_timeout_seconds(args)
    if turn_timeout is not None:
        cfg.turn_timeout = turn_timeout
    # Same for the gate: a session that kept running out of gate time is the
    # one most likely to be resumed, and it needs a way to raise the limit.
    gate_timeout = gate_timeout_seconds(args)
    if gate_timeout is not None:
        cfg.gate_timeout = gate_timeout

    reason, fixes = _resume_blocker(data, path.name, cfg.max_rounds)
    if reason:
        return refuse(reason, fixes, session=path.name)

    if cfg.gate:
        unusable = gate_detect.why_unusable(cfg.gate, cfg.root)
        if unusable:
            print(ui.red("✗ ") + "the acceptance gate cannot run: %s" % unusable)
            print(ui.dim("  gate: ") + cfg.gate)
            print()
            print("  A gate that cannot start fails every turn, and a failing gate")
            print("  vetoes both agents — so the session could never finish.")
            print()
            print(ui.dim("  fix: ") + "install it, give a command that works with "
                  + ui.bold("--gate") + ", or")
            print(ui.dim("       ") + "run without one using " + ui.bold("--no-gate")
                  + ui.dim(" (they can then only agree by argument)"))
            return 3

    problems = preflight(cfg)
    if problems:
        return refuse("cannot reach both agents:\n  " + "\n  ".join(problems),
                      ["fix both sides in one step:  duet login"], code=3, session=path.name)

    # A resumed session gets its own directory: `restore` brings the argument
    # forward but not the turns already taken, and writing this session's
    # shorter transcript over the dead one's would destroy the record of them.
    orch = Orchestrator(
        cfg,
        reporter=make_reporter(cfg.agent_names, verbose=not args.quiet, as_json=as_json),
        session_id=_resumed_session_id(path.name),
    )
    try:
        notes = orch.restore(data)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return refuse(
            "session %s cannot be carried on: %s" % (path.name, exc),
            ["its state.json has been edited, or was written by a duet that paired "
             "differently;", "start a fresh session with:  duet run \"...\""],
            session=path.name,
        )
    open_issues = orch.state.open_issues()

    if as_json:
        print(json.dumps({
            "kind": "resume", "ok": True, "resumed": True, "root": root,
            "session": path.name, "continues_as": orch.session_id,
            "from_round": orch.start_round, "max_rounds": cfg.max_rounds,
            "open_issues": [i.id for i in open_issues],
            "signoffs": sorted(orch.state.signoffs),
            "task": cfg.task,
            "skipped_sessions": skipped,
            "skipped_not_resumable": [p.name for p, _ in finished], "notes": notes,
        }, default=str), flush=True)
    else:
        print(ui.bold("duet resume") + "  " + ui.dim(root))
        if skipped:
            print(ui.dim("  (skipped %s — saved no state.json)" % ", ".join(skipped)))
        if finished:
            print(ui.dim("  (skipped %s — already agreed)" % ", ".join(p.name for p, _ in finished)))
        print("  %s %s  %s" % (ui.dim("continuing:"), path.name,
                               ui.dim("as %s" % orch.session_id)))
        # With no id this can step back past newer sessions, so say what the
        # argument was actually about. An id alone is not something anyone
        # recognises, and resuming the wrong one costs a whole session.
        headline = (cfg.task or "").strip().splitlines()
        if headline:
            print("  %s %s" % (ui.dim("task:      "),
                               headline[0][:78] + ("…" if len(headline[0]) > 78 else "")))
        print("  %s round %d of %d" % (ui.dim("from:      "), orch.start_round, cfg.max_rounds))
        print("  %s %s" % (ui.dim("still open:"),
                           ", ".join(i.id for i in open_issues[:6]) if open_issues
                           else ui.dim("nothing")))
        print("  %s %s" % (ui.dim("signed off:"),
                           ", ".join(sorted(orch.state.signoffs)) or ui.dim("nobody yet")))
        for note in notes:
            print("  " + ui.yellow("note: ") + note)
        print(ui.dim("  the gate runs again before the first new turn — the workspace can "
                     "have changed while the session was dead."))

    result = orch.run()
    return 0 if result.status == STATUS_CONSENSUS else 1


# --------------------------------------------------------------------------
# review: one agent, one pass over the working-tree diff, no debate
# --------------------------------------------------------------------------
REVIEW_CLEAN = 0       # the reviewer raised nothing that blocks the change
REVIEW_FINDINGS = 1    # at least one blocker or major finding
REVIEW_FAILED = 2      # the review is not trustworthy: bad flags, backend error,
                       # no envelope, or the reviewer edited the tree it reviewed

# What Workspace.diff() says when there is nothing to look at.
NOTHING_TO_REVIEW = ("(git: no changes against HEAD)", "(workspace is empty)")

# One review is one agent call, so it can afford a far bigger view of the change
# than a turn in a 12-round session can.
REVIEW_DIFF_CHARS = 60000
NEW_FILE_CHARS = 20000       # per untracked file
NEW_FILE_BUDGET = 60000      # across all of them
NEW_FILE_COUNT = 25
# `GateResult.render` keeps the head and the tail and drops the middle. On a
# failing test run the middle is the traceback — the one thing the reviewer was
# given the gate output for. A single call can afford the whole thing.
REVIEW_GATE_CHARS = 20000


def _file_bodies(ws: Any, paths: Sequence[str]) -> Tuple[Dict[str, str], List[str], List[str]]:
    """The contents of files that appear in no diff: (bodies, cut short, left out).

    A change that is mostly new files — a new module and its tests, the most
    ordinary shape there is — reaches a diff as a list of names and byte counts.
    A reviewer given only that is reviewing a filename.
    """
    bodies: Dict[str, str] = {}
    cut: List[str] = []
    skipped: List[str] = []
    budget = NEW_FILE_BUDGET
    for rel in paths:
        if len(bodies) >= NEW_FILE_COUNT or budget <= 0:
            skipped.append(rel)
            continue
        allowed = min(NEW_FILE_CHARS, budget)
        body = ws.read_or_none(rel, limit=allowed + 1)
        if body is None:                  # unreadable, or gone since the scan
            skipped.append(rel)
            continue
        if len(body) > allowed:
            # Cut here and say so. A body that stops mid-definition with no
            # marker is read as the whole file, which is how a reviewer comes to
            # report a missing symbol that is right there on the next line.
            body = body[:allowed] + "\n...[%s cut off at %d chars]..." % (rel, allowed)
            cut.append(rel)
        budget -= len(body)
        bodies[rel] = body
    return bodies, cut, skipped


def _recorded_task(root: str) -> Tuple[str, str, str]:
    """The task, acceptance text and id of the newest session that recorded one."""
    for path in reversed(_sessions(root)):
        data = _session_state(path)
        if not data:
            continue
        cfg_data = data.get("config") or {}
        task = str(cfg_data.get("task") or "").strip()
        if task:
            return task, str(cfg_data.get("acceptance") or "").strip(), path.name
    return "", "", ""


def _age_of(session_id: str) -> str:
    """ " (3 days ago)" for a session id, or "" if it does not parse."""
    try:
        when = time.mktime(time.strptime(session_id[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, TypeError):
        return ""
    seconds = max(0, time.time() - when)
    if seconds < 3600:
        return " (%d minutes ago)" % (seconds // 60)
    if seconds < 86400:
        return " (%d hours ago)" % (seconds // 3600)
    return " (%d days ago)" % (seconds // 86400)


def _review_task(args: argparse.Namespace, root: str) -> Tuple[str, str, str]:
    """(task, acceptance, where it came from).

    A review with no task is still worth having, but a review that knows what the
    change was *for* can say "this does not do it", which is the finding people
    most want. So: take it from the command line, else from the last session
    recorded in this workspace, else go without and say so.
    """
    if getattr(args, "file", None):
        try:
            text = Path(args.file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("could not read --file %s: %s" % (args.file, exc))
        return text, "", "--file %s" % args.file
    text = " ".join(getattr(args, "task", None) or []).strip()
    if text == "-":
        return sys.stdin.read().strip(), "", "stdin"
    if text:
        return text, "", "the command line"
    task, acceptance, session = _recorded_task(root)
    if task:
        # Named, not just described. A review borrowing an old session's task
        # judges the change against the wrong brief and reports it as the
        # headline finding — which happened, and cost a whole review. Seeing
        # which session it came from is what makes that catchable.
        return task, acceptance, "session %s%s" % (session, _age_of(session))
    return "", "", ""


def _pick_reviewer(cfg: Config, chosen: str) -> str:
    """Who reviews: by default whoever does *not* normally lead."""
    names = cfg.agent_names
    if chosen:
        if chosen not in names:
            raise ValueError(
                "--reviewer %r is not one of this pair: %s" % (chosen, ", ".join(names))
            )
        return chosen
    order = cfg.order()
    return order[1] if len(order) > 1 else order[0]


def _render_findings(issues: List[Any]) -> List[str]:
    lines: List[str] = []
    for severity in SEVERITIES:
        group = [i for i in issues if i.severity == severity]
        if not group:
            continue
        colour = ui.red if severity in BLOCKING_SEVERITIES else ui.yellow
        lines.append(colour(ui.bold("%s (%d)" % (severity, len(group)))))
        for issue in group:
            lines.append("  %s %s" % (ui.dim("[%s]" % issue.id), issue.title))
            if issue.detail:
                lines.append("      " + issue.detail.strip().replace("\n", "\n      "))
        lines.append("")
    return lines


def cmd_review(args: argparse.Namespace) -> int:
    """A second opinion in one agent call: no loop, no consensus, no session."""
    from duet.workspace import Workspace, changed_paths

    as_json = getattr(args, "json", False)
    # Seeded up front so every --json exit has the same shape, including the
    # ones that fail before a reviewer is ever reached.
    out: Dict[str, Any] = {
        "kind": "review", "reviewed": False, "issues": [],
        "counts": {s: 0 for s in SEVERITIES}, "notes": [],
    }

    def finish(code: int, error: str = "") -> int:
        if as_json:
            out["ok"] = code == REVIEW_CLEAN
            out["exit_code"] = code
            out["error"] = error or None
            print(json.dumps(out, default=str), flush=True)
        return code

    def fail(message: str) -> int:
        if not as_json:
            print(ui.red("✗ ") + message)
        return finish(REVIEW_FAILED, message)

    # Everything that can reject the invocation, in one place: a misconfiguration
    # must not escape as exit 1, which is this command's "blocking findings".
    try:
        cfg = build_config(args)
        root = cfg.root
        out["root"] = root
        task, acceptance, task_source = _review_task(args, root)
        reviewer = _pick_reviewer(cfg, getattr(args, "reviewer", "") or "")
        spec = cfg.agent(reviewer)
        adapter = build_adapter(
            spec.backend, name=reviewer, cwd=root, model=spec.model, config=spec.options
        )
        # A review is one call, and it is held to the same limit a turn is: a
        # project that pinned `turn_timeout` because its reviews are long should
        # not have to pin it twice.
        if cfg.turn_timeout:
            adapter.set_turn_timeout(cfg.turn_timeout)
    except (SystemExit, ValueError) as exc:   # unknown pair or backend, bad --file
        return fail(str(exc))

    out.update(task=task, task_source=task_source or None,
               reviewer=reviewer, backend=spec.backend, agent=adapter.describe())

    ws = Workspace(root, gate=cfg.gate or None, gate_timeout=cfg.gate_timeout)
    # The change is read before the gate runs. A gate is free to write files
    # (coverage data, build output), and a view taken afterwards would show the
    # reviewer artefacts nobody wrote.
    since = getattr(args, "since", None)
    change = ws.diff_since(since, limit=REVIEW_DIFF_CHARS) if since else ws.diff(limit=REVIEW_DIFF_CHARS)
    is_git = ws.is_git_repo
    # Two different kinds of incomplete, kept apart: the diff itself hit the
    # character limit, or some new file did not fit. `truncated` is the union —
    # it is what the reviewer is warned about — but the human is told which one
    # actually happened, because "the change is larger than 60000 chars" printed
    # over one unreadable filename sends the reader looking for the wrong thing.
    diff_truncated = ws.TRIM_MARKER in change
    truncated = diff_truncated
    out.update(git=is_git, truncated=truncated)

    if change.strip() in NOTHING_TO_REVIEW:
        if not as_json:
            print(ui.bold("duet review") + "  " + ui.dim(root))
            if since:
                print(ui.yellow("nothing to review") + " — nothing on this branch that %s does not have." % since)
                print(ui.dim("  try another ref:  ") + ui.bold("duet review --since <branch-or-commit>"))
                return REVIEW_NOTHING
            print(ui.yellow("nothing to review") + " — " + (
                "the working tree matches HEAD." if is_git else "the workspace is empty."))
            print(ui.dim("make a change first, or point at another directory with -C."))
        return finish(REVIEW_CLEAN)

    # Untracked files are in no diff; in a workspace with no git at all, no file
    # is. Either way the reviewer needs the text, not a listing of names.
    unseen = ws.untracked_files() if is_git else [
        p.relative_to(ws.root).as_posix() for p in ws.tracked_files()
    ]
    new_files, cut_files, skipped_files = _file_bodies(ws, unseen)
    out["new_files"] = sorted(new_files)
    if cut_files:
        truncated = True
        out["notes"].append("sent only the first %d chars of: %s"
                            % (NEW_FILE_CHARS, ", ".join(cut_files[:10])))
    if skipped_files:
        truncated = True
        out["notes"].append("not sent at all (too many, too large, or unreadable): %s"
                            % ", ".join(skipped_files[:10]))
    out["truncated"] = truncated

    # The fingerprint is taken either side of duet's own gate run, so that the
    # files the gate writes are known to be the gate's doing. The reviewer is
    # granted the gate on purpose; it must not then be blamed for running it.
    before_gate = ws.fingerprint()
    gate = ws.run_gate()
    after_gate = ws.fingerprint()
    gate_touched = set(changed_paths(before_gate, after_gate))
    out["gate"] = {
        "command": gate.command,
        "skipped": gate.skipped,
        "ok": gate.ok,
        "exit_code": gate.exit_code,
    }

    if cfg.gate:
        # A reviewer that cannot re-run the gate is reviewing on hearsay.
        adapter.allow_gate(cfg.gate)
    # Asking in the prompt is not a control. Where the backend can enforce it,
    # make the review read-only; either way the tree is hashed and checked below.
    enforced = adapter.read_only()
    out["read_only"] = enforced or None

    prompt = prompts.review_prompt(
        task=task,
        acceptance=acceptance,
        change_title=(
            "THE CHANGE UNDER REVIEW (working tree against HEAD)"
            if is_git
            else "THE WORKSPACE UNDER REVIEW (not a git repo — file listing)"
        ),
        change=change,
        gate_text="" if gate.skipped else gate.render(REVIEW_GATE_CHARS),
        new_files=new_files,
        truncated=truncated,
    )
    system = prompts.review_system_prompt(
        name=reviewer,
        display=adapter.display,
        root=root,
        edits_workspace=adapter.edits_workspace,
    )

    if not as_json:
        print(ui.bold("duet review") + "  " + ui.dim(root))
        print("  %s %s" % (ui.dim("reviewer: "), ui.agent_tag(reviewer, cfg.agent_names)
                           + "  " + ui.dim(adapter.describe())
                           + (ui.dim(", " + enforced) if enforced else "")))
        print("  %s %s" % (ui.dim("task:     "),
                           ui.dim("from %s" % task_source) if task_source
                           else ui.yellow("none given — reviewing the change on its own terms")))
        if new_files:
            print("  %s %s" % (ui.dim("new files:"),
                               ui.dim("%d included in full" % len(new_files))))
        if not gate.skipped:
            print("  %s %s  %s" % (ui.dim("gate:     "), gate.command,
                                   ui.green("passed") if gate.ok
                                   else ui.yellow(gate.headline()) if gate.timed_out
                                   else ui.red(gate.headline())))
        if diff_truncated:
            print("  " + ui.yellow("the change is larger than %d chars and was cut short — "
                                   "the reviewer is told so, but it is not seeing all of it"
                                   % REVIEW_DIFF_CHARS))
        elif truncated:
            print("  " + ui.yellow("some of the new files did not fit — see the notes below; "
                                   "the reviewer is told, but it is not seeing all of it"))
        print(ui.dim("  reading the change..."), flush=True)

    reply = adapter.send(prompt, system=system, round_no=1)
    touched = [p for p in changed_paths(after_gate, ws.fingerprint()) if p not in gate_touched]

    if not reply.ok and not reply.text.strip():
        return fail("%s could not review this: %s" % (reviewer, reply.error))

    env = parse_envelope(reply.text, agent=reviewer, round_no=1)
    if reply.error:
        # A reply that arrived alongside an error is a degraded reply. It has to
        # reach the terminal too, not only --json.
        env.notes.append("backend reported: %s" % reply.error)
    out.update(
        reviewed=True,
        message=env.message,
        summary=env.summary,
        confidence=env.confidence,
        issues=[i.to_dict() for i in env.issues],
        counts={s: len([i for i in env.issues if i.severity == s]) for s in SEVERITIES},
        workspace_changed=bool(touched),
        workspace_changed_paths=touched,
    )
    out["notes"].extend(env.notes)

    if not env.parse_ok:
        # No envelope means no findings list — which is not the same as no
        # findings. Saying "clean" here would be a lie with an exit code on it.
        if not as_json:
            print()
            print(ui.wrap(env.message.strip()[:PREVIEW_CHARS]))
            print()
        return fail("%s replied without a JSON envelope, so nothing could be read back "
                    "as a finding — and silence is not the same as approval. Re-run it."
                    % reviewer)

    blocking = [i for i in env.issues if i.severity in BLOCKING_SEVERITIES]

    if not as_json:
        print()
        if env.message.strip() and not getattr(args, "quiet", False):
            print(ui.wrap(env.message.strip()))
            print()
        for line in _render_findings(env.issues):
            print(line)
        if not env.issues:
            print(ui.green(ui.bold("no findings.")) + " "
                  + ui.dim("%s reviewed the change and raised nothing." % reviewer))
        else:
            counts = ", ".join("%d %s" % (out["counts"][s], s) for s in SEVERITIES if out["counts"][s])
            print(("%s %s" % (ui.bold("%d finding%s" % (len(env.issues),
                                                        "" if len(env.issues) == 1 else "s")),
                              ui.dim("(%s)" % counts))))
            if blocking:
                print(ui.red("blocking: this change should not ship as it is."))
            else:
                print(ui.dim("nothing blocking — every finding is minor."))
        if not gate.skipped and not gate.ok and not blocking:
            # The exit code answers one question — did anyone raise something
            # blocking — and a gate that failed is not an answer to it. But the
            # gate line is printed before the review, and a long review buries
            # it; a reader who scrolls to "no findings" and stops would take a
            # red gate for a green one. So say it again where the verdict is.
            if gate.timed_out:
                print(ui.yellow("but %s: " % gate.timeout_note())
                      + ui.bold(gate.command)
                      + ui.dim(" — nothing was shown to pass. Re-run it yourself, "
                               "or give it longer with --gate-timeout MINUTES."))
            else:
                print(ui.red("but the gate failed (exit %d): " % gate.exit_code)
                      + ui.bold(gate.command)
                      + ui.dim(" — that is a defect in this change whichever way the "
                               "review went. Re-run it yourself."))
        for note in out["notes"]:
            print(ui.yellow("note: ") + note)

    if touched:
        # The findings may still be worth reading, but they no longer describe
        # what is on disk, so this cannot exit as a clean or merely-noisy review.
        #
        # Deliberately not phrased as "the reviewer edited these": duet sees the
        # workspace change, not who changed it, and anyone can be editing in
        # another window while a review runs. Saying the reviewer did it is an
        # accusation duet cannot support — and it made one, wrongly, the first
        # time this fired.
        return fail("the workspace changed while %s was reviewing it (%s), so these "
                    "findings no longer describe what is on disk. If that was you "
                    "editing in another window, re-run the review; if not, %s is "
                    "held read-only and should not have written anything. "
                    "Check `git status` either way."
                    % (reviewer, ", ".join(touched[:8]), reviewer))

    return finish(REVIEW_FINDINGS if blocking else REVIEW_CLEAN)


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="duet",
        description="Claude Code and ChatGPT on the same task, debating until both sign off.",
    )
    parser.add_argument("--version", action="version", version="duet %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-C", "--root", default=".", help="workspace directory (default: .)")
        p.add_argument("-q", "--quiet", action="store_true", help="verdicts only, no message previews")
        p.add_argument("--json", action="store_true", help="emit machine-readable events on stdout")

    p_run = sub.add_parser("run", help="run a session")
    p_run.add_argument("task", nargs="*", help="what the two agents should build or solve")
    common(p_run)
    p_run.add_argument("-f", "--file", help="read the task from a file")
    p_run.add_argument("--context", help="the conversation this task came out of, so the pair does not start cold")
    p_run.add_argument("--context-file", metavar="PATH", help="read that context from a file, or - for stdin")
    p_run.add_argument("--accept", help="acceptance criteria, in prose")
    p_run.add_argument("--accept-file", help="read acceptance criteria from a file")
    p_run.add_argument("--gate", help="command that must pass before either agent may finish, "
                                      "e.g. \"pytest -q\" (auto-detected when you omit it)")
    p_run.add_argument("--no-gate", action="store_true",
                       help="run without a gate — the two of them can then only agree by argument")
    p_run.add_argument("--rounds", type=int, help="maximum rounds (default 12)")
    p_run.add_argument("--turn-timeout", type=int, metavar="MINUTES", help=TURN_TIMEOUT_HELP)
    p_run.add_argument("--gate-timeout", type=int, metavar="MINUTES", help=GATE_TIMEOUT_HELP)
    p_run.add_argument("--max-debate", type=int, help="rounds an issue may stay open before arbitration (default 3)")
    p_run.add_argument(
        "--pair",
        metavar="A+B",
        help="which two agents, and who leads (default: %s). "
        "A and B are any of: %s. Add a model with a colon, e.g. claude:opus+codex."
        % (DEFAULT_PAIR, ", ".join(sorted(set(BACKEND_ALIASES)))),
    )
    p_run.add_argument("--start", help="who takes the first turn (defaults to the left of --pair)")
    p_run.add_argument("--decider", help="who rules on deadlocked issues")
    p_run.add_argument("--swap", type=int, help="swap lead/reviewer every N rounds (0 = never)")
    p_run.add_argument("--commit", action="store_true", help="git-commit the result when both sign off")
    p_run.add_argument("--allow-nested", action="store_true",
                       help="permit starting this from inside another duet session")
    p_run.set_defaults(func=cmd_run)

    def session_args(p: argparse.ArgumentParser, positional: str, helptext: str) -> None:
        """The flags every session-starting command shares with `duet run`.

        They all delegate to cmd_run, which reads these by name — so one list,
        rather than five copies that drift apart the first time a flag is added.
        """
        p.add_argument(positional, nargs="*", help=helptext)
        common(p)
        p.add_argument("--context", help="the conversation this came out of")
        p.add_argument("--context-file", metavar="PATH",
                       help="read that context from a file, or - for stdin")
        p.add_argument("--accept", help="acceptance criteria, in prose")
        p.add_argument("--accept-file", help="read acceptance criteria from a file")
        p.add_argument("--gate", help="the command that decides done "
                                      "(default: this project's test command)")
        p.add_argument("--no-gate", action="store_true",
                       help="run without a gate — most of these refuse to")
        p.add_argument("--rounds", type=int, help="maximum rounds (default 12)")
        p.add_argument("--turn-timeout", type=int, metavar="MINUTES", help=TURN_TIMEOUT_HELP)
        p.add_argument("--gate-timeout", type=int, metavar="MINUTES", help=GATE_TIMEOUT_HELP)
        p.add_argument("--max-debate", type=int,
                       help="rounds an issue may stay open before arbitration (default 3)")
        p.add_argument("--pair", metavar="A+B", help="which two agents, and who leads")
        p.add_argument("--start", help="who takes the first turn")
        p.add_argument("--decider", help="who rules on deadlocked issues")
        p.add_argument("--swap", type=int, help="swap lead/reviewer every N rounds (0 = never)")
        p.add_argument("--commit", action="store_true", help="git-commit the result when both sign off")
        p.add_argument("--allow-nested", action="store_true",
                       help="permit starting this from inside another duet session")

    def jury_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--jury", action="store_true",
                       help="when both sign off, a simulated stakeholder panel sits before "
                            "the session may end; its verified blockers become open issues")
        p.add_argument("--panel", metavar="NAMES",
                       help="with --jury: which jurors sit (default: all of them)")

    jury_args(p_run)

    p_build = sub.add_parser("build", help="start a project from nothing, with a gate from round one")
    session_args(p_build, "idea", "what to build, in a sentence")
    jury_args(p_build)
    p_build.set_defaults(func=build_cmd.run)

    for name, helptext, what in (
        ("fix", "fix a bug — its tests must fail on the original code, and pass now",
         "the bug, as you would describe it to a colleague"),
        ("add", "add a feature — a test must fail without it",
         "the feature, in a sentence"),
        ("refactor", "restructure without changing behaviour — existing tests may not be edited",
         "what to restructure"),
        ("plan", "argue out a plan into PLAN.md — no code may change",
         "what to plan"),
    ):
        p_flow = sub.add_parser(name, help=helptext)
        session_args(p_flow, "what", what)
        if name in ("add", "plan"):
            jury_args(p_flow)
        if name == "plan":
            p_flow.add_argument(
                "--quick", action="store_true",
                help="two turns: a full draft, then one review that may edit it — "
                     "minutes, not rounds; reported as reviewed, not as signed off by both")
        p_flow.set_defaults(func=lambda a, _n=name: workflow_commands.run(a, _n))

    p_page = sub.add_parser(
        "page",
        help="check or screenshot a web page in a real browser — the gate a website can have",
    )
    page_sub = p_page.add_subparsers(dest="page_command")

    def page_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("page", nargs="?",
                       help="the file or URL to render (default: \"page\" in %s)" % page_check.PAGE_RULES)
        p.add_argument("-C", "--root", default=".", help="project directory (default: .)")
        p.add_argument("--width", default="",
                       help="viewport widths in px (default: %s). Anything under %d is "
                            "emulated as a phone at exactly that width."
                            % (",".join(str(w) for w in page_check.DEFAULT_WIDTHS),
                               page_check.PHONE_MAX_WIDTH))
        p.add_argument("--allow-network", action="store_true",
                       help="let the page reach other hosts (they are blocked by default, "
                            "so a gate's answer does not depend on the network)")

    p_page_check = page_sub.add_parser(
        "check", help="render the page and print one line per fault (exit 1 if any)")
    page_args(p_page_check)
    p_page_check.add_argument("--rules", metavar="PATH",
                              help="this project's own rules (default: %s, if it is there)"
                                   % page_check.PAGE_RULES)
    p_page_check.set_defaults(func=page_check.check_cmd)

    p_page_shot = page_sub.add_parser(
        "shot", help="write one full-page PNG per width and print the paths")
    page_args(p_page_shot)
    p_page_shot.add_argument("--out", metavar="DIR",
                             help="where to write them (default: %s)" % page_check.SHOT_DIR)
    p_page_shot.set_defaults(func=page_check.shot_cmd)
    p_page.set_defaults(func=page_check.run)

    p_from = sub.add_parser("from-ecc",
                            help="see what you actually use from ECC, keep it, and switch")
    p_from.add_argument("--keep", metavar="NAME",
                        help="copy this ECC agent or skill into ~/.claude before removing ECC")
    p_from.add_argument("--dir", metavar="HOME", help="treat this directory as home (for testing)")
    p_from.set_defaults(func=lambda a: from_ecc.run(a))

    p_doctor = sub.add_parser("doctor", help="check that both agents are reachable")
    common(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub.add_parser("init", help="write .duet/config.json and check the setup")
    common(p_init)
    p_init.add_argument("--pair", metavar="A+B", help="which two agents to pin (default: %s)" % DEFAULT_PAIR)
    p_init.add_argument("--gpt-backend", choices=["openai-api", "codex-cli"], help=argparse.SUPPRESS)
    p_init.add_argument("--gate")
    p_init.add_argument("--rounds", type=int)
    p_init.set_defaults(func=cmd_init)

    p_login = sub.add_parser("login", help="sign both agents in (no API key involved)")
    common(p_login)
    p_login.add_argument("agent", nargs="?", choices=["claude", "gpt"], help="sign in just one side")
    p_login.add_argument("--force", action="store_true", help="re-run the sign-in even if it looks connected")
    p_login.set_defaults(func=cmd_login)

    p_setup = sub.add_parser("setup", help="one command: agent CLIs, sign-ins, and /duet")
    common(p_setup)
    p_setup.add_argument("-y", "--yes", action="store_true",
                         help="do not ask before installing the agent CLIs")
    p_setup.add_argument("--force", action="store_true",
                         help="overwrite an existing /duet that has been edited")
    p_setup.set_defaults(func=cmd_setup)

    p_skill = sub.add_parser("skill", help="install /duet into Claude Code and Codex")
    common(p_skill)
    p_skill.add_argument("action", nargs="?", default="install",
                         choices=["install", "path", "show", "default", "undefault"],
                         help="install it (the default action), print its source path, print it, "
                              "or make duet the default for real code changes in every "
                              "session (default) and undo that (undefault)")
    p_skill.add_argument("--dir", metavar="HOME", help="treat this directory as home (for testing)")
    p_skill.add_argument("--force", action="store_true", help="overwrite an existing copy")
    p_skill.add_argument("--project", action="store_true",
                         help="with default/undefault: this project's AGENTS.md instead of your "
                              "home directory — for Cursor, and any agent that reads AGENTS.md")
    p_skill.set_defaults(func=cmd_skill)

    p_demo = sub.add_parser("demo", help="run the full loop with scripted agents (no keys, no network)")
    common(p_demo)
    p_demo.set_defaults(func=cmd_demo)

    p_sessions = sub.add_parser("sessions", help="list past sessions")
    common(p_sessions)
    p_sessions.set_defaults(func=cmd_sessions)

    p_status = sub.add_parser("status", help="what is the session doing right now (works while it runs)")
    common(p_status)
    p_status.add_argument("session", nargs="?", help="session id (default: the newest)")
    p_status.set_defaults(func=cmd_status)

    p_resume = sub.add_parser(
        "resume",
        help="carry on an interrupted session from where it stopped, keeping the argument",
    )
    common(p_resume)
    p_resume.add_argument("session", nargs="?",
                          help="session id (default: the newest one that has not agreed yet)")
    p_resume.add_argument("--turn-timeout", type=int, metavar="MINUTES",
                          help=TURN_TIMEOUT_HELP + " Without it, the limit the dead "
                               "session was started with is kept.")
    p_resume.add_argument("--gate-timeout", type=int, metavar="MINUTES",
                          help=GATE_TIMEOUT_HELP + " Without it, the limit the dead "
                               "session was started with is kept.")
    p_resume.add_argument("--rounds", type=int, metavar="N",
                          help="new total round budget, counting the rounds already used")
    p_resume.add_argument("--gate", metavar="CMD",
                          help="use this gate instead of the one the session recorded")
    p_resume.add_argument("--allow-nested", action="store_true",
                          help="permit starting this from inside another duet session")
    p_resume.set_defaults(func=cmd_resume)

    p_report = sub.add_parser("report", help="print the report for a session (default: the last one)")
    common(p_report)
    p_report.add_argument("session", nargs="?", help="session id")
    p_report.add_argument("--transcript", action="store_true", help="print the full transcript instead")
    p_report.set_defaults(func=cmd_report)

    p_review = sub.add_parser(
        "review",
        help="one agent, one pass over the working-tree diff — a second opinion, no debate",
    )
    p_review.add_argument("task", nargs="*",
                          help="what the change was meant to do (default: the last session's task)")
    common(p_review)
    p_review.add_argument("-f", "--file", help="read the task from a file")
    p_review.add_argument("--reviewer", metavar="NAME",
                          help="who reviews (default: the one that does not normally lead)")
    p_review.add_argument("--pair", metavar="A+B",
                          help="which two agents to choose the reviewer from (default: %s)" % DEFAULT_PAIR)
    p_review.add_argument("--since", metavar="REF",
                          help="review everything this branch has that REF does not, "
                               "instead of the uncommitted working tree (e.g. --since main)")
    p_review.add_argument("--no-gate", action="store_true",
                          help="do not run a gate before reviewing (it is otherwise found for you)")
    p_review.add_argument("--gate", metavar="CMD",
                          help="run this first and show the reviewer its output, e.g. \"pytest -q\"")
    p_review.add_argument("--gate-timeout", type=int, metavar="MINUTES", help=GATE_TIMEOUT_HELP)
    p_review.set_defaults(func=cmd_review)

    p_jury = sub.add_parser(
        "jury",
        help="a simulated panel — user, client, investor, judge, skeptic — judges the "
             "product, and every objection is checked against it",
    )
    common(p_jury)
    p_jury.add_argument("--stage", choices=list(jury.STAGES),
                        help="what is being judged (default: read off the workspace)")
    p_jury.add_argument("--panel", metavar="NAMES",
                        help="comma-separated jurors (default: all of them — %s, and any "
                             "in %s)" % (",".join(p.name for p in jury.DEFAULT_PERSONAS),
                                         jury.JURY_CONFIG))
    p_jury.add_argument("--idea", metavar="TEXT", help="the idea, for an idea-stage jury")
    p_jury.add_argument("--rounds-of-questions", type=int, default=0, metavar="N",
                        help="rounds in which each juror sees the others' findings and may "
                             "revise its verdict (default 0)")
    p_jury.add_argument("--out", metavar="PATH",
                        help="where to write JURY.md (default: .duet/jury/<time>/JURY.md)")
    p_jury.add_argument("--pair", metavar="A+B",
                        help="the two agents the jurors alternate between (default: %s)" % DEFAULT_PAIR)
    p_jury.set_defaults(func=jury.run)

    p_verify = sub.add_parser(
        "verify",
        help="re-run the gate and check the last sign-off still describes this workspace",
    )
    common(p_verify)
    p_verify.add_argument("session", nargs="?", help="session id (default: the last one that saved state)")
    p_verify.add_argument("--gate", help="check against this command instead of the one the session recorded")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # Sessions are long and mostly waiting on an agent. Piped or redirected runs
    # block-buffer by default, which makes a working session look hung.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # pragma: no cover - very old interpreters
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        print("\n" + ui.dim("first time?  ") + ui.bold("duet setup")
              + ui.dim("  does the lot, then type ") + ui.bold("/duet")
              + ui.dim(" in Claude Code or ") + ui.bold("$duet") + ui.dim(" in Codex"))
        print(ui.dim("not sure it works?  ") + ui.bold("duet demo")
              + ui.dim("  runs the whole loop offline, no keys, no network"))
        return 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n" + ui.yellow("interrupted"))
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
