"""Claude Code, headless.

Runs the real `claude` CLI in the workspace, so this side of the duet has
genuine tools: it reads, edits, runs commands and tests. Session continuity
comes from `--resume`, but every prompt the orchestrator builds is
self-contained, so losing the session id costs context, not correctness.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from duet.winlaunch import command as launchable

from duet.adapters.base import (
    DEFAULT_TURN_TIMEOUT,
    RUNTIME_FIX,
    Adapter,
    AgentReply,
    TIMED_OUT,
    Probe,
    env_with_sibling_path,
    human_duration,
    looks_unrunnable,
    run_status_check,
    timed_out_detail,
    timed_out_warning,
)

DEFAULT_CANDIDATES = (
    os.environ.get("DUET_CLAUDE_BIN", ""),
    "claude",
    # expanduser, not Path.home(): this runs at import, and Path.home() raises
    # where no home directory is known (Windows without USERPROFILE), which
    # took every duet command down with it.
    os.path.expanduser(os.path.join("~", ".claude", "local", "claude")),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser(os.path.join("~", ".local", "bin", "claude")),
)

INSTALL_HINT = "npm install -g @anthropic-ai/claude-code"
LOGIN_HINT = "claude auth login   (sign in with your Claude account — no API key needed)"


def read_auth_status(binary, timeout: int = 45) -> Tuple[Optional[bool], str]:
    """(logged_in, description). None means it could not be determined.

    `claude auth status` prints JSON and costs nothing, which is the only honest
    way to answer "is this connected?" — a successful `--version` says only that
    the binary exists, and that false green is exactly what sends someone into a
    session that fails on its first turn.
    """
    first = binary[0] if isinstance(binary, (list, tuple)) else binary
    out = ""
    for env in (None, env_with_sibling_path(first)):
        try:
            argv = list(binary) if isinstance(binary, (list, tuple)) else [binary]
            proc = run_status_check(argv + ["auth", "status"], timeout, env)
        except subprocess.TimeoutExpired:
            return None, timed_out_detail("claude auth status", timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, str(exc)
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if not looks_unrunnable(out):
            break
    broken = looks_unrunnable(out)
    if broken:
        return None, "could not run claude: %s" % broken
    try:
        start = out.index("{")
        data = json.loads(out[start : out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        low = out.lower()
        if "not logged in" in low or "please run /login" in low:
            return False, "not logged in"
        return None, out.splitlines()[0] if out else "no output from `claude auth status`"
    if not isinstance(data, dict) or "loggedIn" not in data:
        return None, "unrecognised output from `claude auth status`"
    method = str(data.get("authMethod") or "unknown")
    if data.get("loggedIn"):
        return True, "signed in (%s)" % method
    return False, "not signed in"


class ClaudeCodeAdapter(Adapter):
    backend = "claude-code"
    display = "Claude Code"
    edits_workspace = True
    npm_package = "@anthropic-ai/claude-code"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        launch, binary, self.installed_globally = self.resolve_launch(DEFAULT_CANDIDATES)
        self.bin = binary or launch[0]   # sets launch to one element, then
        self.launch = launch             # widen it for the npx form
        self.timeout = int(self.config.get("timeout", DEFAULT_TURN_TIMEOUT))
        self.permission_mode = self.config.get("permission_mode", "acceptEdits")
        self.extra_args: List[str] = list(self.config.get("extra_args") or [])
        self.allowed_tools: List[str] = list(self.config.get("allowed_tools") or [])
        self.disallowed_tools: List[str] = list(self.config.get("disallowed_tools") or [])

    WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

    def read_only(self) -> str:
        """Deny the editing tools outright, rather than trusting the prompt."""
        for tool in self.WRITE_TOOLS:
            if tool not in self.disallowed_tools:
                self.disallowed_tools.append(tool)
        return "edit tools denied (%s)" % ", ".join(self.WRITE_TOOLS)

    WEB_TOOLS = ("WebSearch", "WebFetch")

    def allow_web(self) -> str:
        """Allow the web tools by name; nothing else changes."""
        for tool in self.WEB_TOOLS:
            if tool not in self.allowed_tools:
                self.allowed_tools.append(tool)
        return "web tools allowed (%s)" % ", ".join(self.WEB_TOOLS)

    # Plainly read-only commands, allowed in every session. Measured on 262
    # sessions on duet 0.15+, `cat` alone was refused 42 times, and the agent
    # then either guessed or spent a call on the Read tool for the same bytes.
    #
    # How Claude Code matches these (code.claude.com/docs/en/permissions):
    # `Bash(cat:*)` is `Bash(cat *)` — the space is a word boundary, so it does
    # not match `catdoc`. A compound command is split on `&&`, `||`, `;`, `|`
    # and the rest, and every part must be allowed on its own, so `cat a | sh`
    # or `cat a; rm b` is still refused. An output redirection is checked as a
    # file write whatever rule allowed the command — under `acceptEdits` that
    # is the working directory only, which the Edit tool can already write — so
    # `cat a > /tmp/x` stays refused. None of these commands has an option that
    # writes a file of its own.
    #
    # Left out on purpose:
    # - `git diff`, `git show` and `git log`: each takes `--output=<file>` and
    #   writes its output there itself — any path, outside the workspace too,
    #   with content an agent controls through a commit message — and no shell
    #   redirection is involved for Claude Code to check. A rule's wildcard
    #   only works at its end, so no rule can allow them without `--output`.
    #   The agents are shown the diff in every prompt.
    # - `git:*`, or any git subcommand beyond `status`: push, commit, checkout
    #   and reset are all one prefix away.
    # - `find` (`-delete`, `-exec`) and `sed` (`-i`, `w`): both write.
    READ_ONLY_RULES = (
        "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)",
        "Bash(wc:*)", "Bash(ls:*)", "Bash(git status:*)",
    )

    def permitted_tools(self) -> List[str]:
        """What `--allowedTools` is given: the session's grants, then the
        read-only commands. `allowed_tools` stays what was granted, so it still
        says what this session added."""
        return self.allowed_tools + [r for r in self.READ_ONLY_RULES if r not in self.allowed_tools]

    def _command(self, prompt: str, system: str) -> List[str]:
        cmd = list(self.launch) + ["-p", prompt, "--output-format", "json"]
        if self.permission_mode == "bypass":
            cmd.append("--dangerously-skip-permissions")
        elif self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.model:
            cmd += ["--model", self.model]
        if system:
            cmd += ["--append-system-prompt", system]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        tools = self.permitted_tools()
        if tools:
            cmd += ["--allowedTools", " ".join(tools)]
        if self.disallowed_tools:
            cmd += ["--disallowedTools", " ".join(self.disallowed_tools)]
        cmd += self.extra_args
        return cmd

    def allow_gate(self, gate: str) -> None:
        """Permit exactly the gate's own commands under `acceptEdits`.

        `acceptEdits` lets the agent write files but not run anything, so it had
        to trust the harness's report of the test run instead of seeing it. This
        grants Bash for the gate's programs only — `pytest`, `npm`, whatever the
        gate actually invokes — and nothing else.

        For a shell or interpreter the program is the script or module it runs,
        not the interpreter: `Bash(bash:*)` or `Bash(python:*)` would let an
        agent run `bash -c "<anything>"`, so `bash scripts/gate.sh` grants
        `Bash(bash scripts/gate.sh:*)` and `python -m pytest -q` grants
        `Bash(python -m pytest:*)`. A segment this cannot narrow — inline code,
        or an option it cannot read — grants nothing; the agent still has
        `duet gate`.
        """
        for segment in re.split(r"&&|\|\||;|\|", gate or ""):
            prefix = self._gate_prefix(self._words(segment))
            if not prefix:
                continue
            rule = "Bash(%s:*)" % prefix
            if rule not in self.allowed_tools:
                self.allowed_tools.append(rule)

    # A shell or interpreter runs whatever code it is handed, so its name alone
    # is never the rule. Matched on the program's name, whatever its directory
    # and with any Windows extension dropped: `/usr/bin/python3`, `node.exe`,
    # `npx.cmd`.
    INTERPRETER = re.compile(
        r"(sh|bash|zsh|dash|ksh|fish|python(\d+(\.\d+)*)?|pythonw|pypy\d*"
        r"|node|ruby|perl|php|deno|bun|bunx|npx)$")
    # Run a program of their own choosing as their first argument.
    RUNNERS = ("uv", "poetry")
    # Commands of theirs that run inline code: `deno eval`, `bun x`.
    INLINE_SUBCOMMANDS = ("eval", "repl", "x", "exec")

    @staticmethod
    def _words(segment: str) -> List[Tuple[str, str]]:
        """(as written, unquoted) for each shell word of one gate segment.

        As written is what goes in the rule, since Claude Code matches the
        command text the agent types: a quoted Windows path stays quoted and its
        backslashes stay (`shlex.split` would drop them). Unquoted is what names
        the program. Quotes group words, so `SIM="iPhone 17"` is one word; an
        unbalanced quote falls back to splitting on spaces.
        """
        words: List[Tuple[str, str]] = []
        written, plain, quote, started = "", "", "", False
        for ch in segment:
            if quote:
                written += ch
                if ch == quote:
                    quote = ""
                else:
                    plain += ch
            elif ch in "\"'":
                quote, started = ch, True
                written += ch
            elif ch.isspace():
                if started:
                    words.append((written, plain))
                written, plain, started = "", "", False
            else:
                written += ch
                plain += ch
                started = True
        if quote:
            return [(w, w) for w in segment.split()]
        if started:
            words.append((written, plain))
        return words

    def _gate_prefix(self, words: List[Tuple[str, str]]) -> str:
        """The command prefix one gate segment may be run with, or "" for none."""
        def name(i: int) -> str:
            base = re.split(r"[\\/]", words[i][1])[-1].lower()
            return re.sub(r"\.(exe|cmd|bat)$", "", base)

        def through(end: int) -> str:
            return " ".join(w for w, _ in words[start:end + 1])

        # Step over leading VAR=value assignments and `env` (with `-i` or
        # `-u NAME`) to reach the command.
        i = 0
        while i < len(words):
            if "=" in words[i][1] and not words[i][1].startswith("/"):
                i += 1
            elif name(i) == "env":
                i += 1
                while i < len(words) and words[i][1] in ("-i", "-", "--ignore-environment", "-u"):
                    i += 2 if words[i][1] == "-u" else 1
                if i < len(words) and words[i][1].startswith("-"):
                    return ""   # `env -S '<anything>'` and the rest: not narrowed
                # `env` accepts assignments between its options and the command.
                while i < len(words) and "=" in words[i][1] and not words[i][1].startswith("/"):
                    i += 1
            else:
                break
        if i >= len(words):
            return ""
        start = i
        program = name(i)
        if program in self.RUNNERS:
            # `uv run pytest`, `poetry run python -m pytest`: what follows `run`
            # is a command in its own right; `uv sync` is just that subcommand.
            if i + 1 >= len(words) or words[i + 1][1].startswith("-"):
                return ""
            if words[i + 1][1] != "run":
                return through(i + 1)
            i += 2
            if i >= len(words) or words[i][1].startswith("-"):
                return ""
            program = name(i)
        if not self.INTERPRETER.match(program):
            return through(i)
        # An interpreter: grant it through the script, module or subcommand.
        nxt = i + 1
        if nxt >= len(words):
            return ""
        arg = words[nxt][1]
        if program.startswith("python") or program.startswith("pypy"):
            if arg == "-m" and nxt + 1 < len(words):
                return through(nxt + 1)
        if program in ("deno", "bun") and not arg.startswith("-"):
            if arg in self.INLINE_SUBCOMMANDS:
                return ""
            if arg == "run":
                # `bun run test`, `deno run x.ts`: through what it runs
                if nxt + 1 < len(words) and not words[nxt + 1][1].startswith("-"):
                    return through(nxt + 1)
                return ""
        if arg.startswith("-"):
            # `-c`, `-e`, `--eval`, or an option that takes a value this cannot
            # tell from the script: nothing narrower than the interpreter is
            # certain, so grant nothing.
            return ""
        return through(nxt)

    def allow_agent_gate(self, command: str) -> None:
        """Permit `duet gate` — the whole command, as the agent is told to type it.

        The rule is for that exact prefix, never `duet:*`: `duet run` starts a
        whole second pair of agents. (A page gate already grants `duet:*`
        through allow_gate; that is the gate's own command, not this.)
        """
        rule = "Bash(%s:*)" % command.strip()
        if command.strip() and rule not in self.allowed_tools:
            self.allowed_tools.append(rule)

    @staticmethod
    def _extract(payload: Any) -> Dict[str, Any]:
        """Tolerate both the single result object and a list of events."""
        if isinstance(payload, list):
            for item in reversed(payload):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
            return payload[-1] if payload and isinstance(payload[-1], dict) else {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _refused(denials: Any) -> List[str]:
        """`Bash: pytest -q`, `Edit: PLAN.md` — one line per call the permission mode stopped.

        The CLI reports these itself. Without them the harness only sees the
        agent's account of its turn, and an agent whose edit was refused can
        still write "fixed it" — which its peer then spends a whole turn
        discovering is not in the file.
        """
        lines: List[str] = []
        for item in denials if isinstance(denials, list) else []:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool_name") or "tool")
            data = item.get("tool_input") if isinstance(item.get("tool_input"), dict) else {}
            target = data.get("command") or data.get("file_path") or data.get("notebook_path") or ""
            target = " ".join(str(target).split())
            lines.append(("%s: %s" % (tool, target) if target else tool)[:240])
        return lines

    def send(self, prompt: str, system: str = "", round_no: int = 0) -> AgentReply:
        cmd = self._command(prompt, system)
        try:
            proc = subprocess.run(
                launchable(cmd),
                cwd=self.cwd,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout,
                # the prompt is passed with -p; an inherited stdin pipe would
                # only give the CLI something else to wait on
                stdin=subprocess.DEVNULL,
                env=env_with_sibling_path(self.bin),
            )
        except FileNotFoundError:
            return AgentReply(
                text="",
                error="the `claude` CLI was not found. Install it with "
                "`%s`, then `claude auth login` to sign in. "
                "You can also point duet at it with DUET_CLAUDE_BIN." % INSTALL_HINT,
            )
        except subprocess.TimeoutExpired:
            # Marked, not just described: whatever this turn had already written
            # is still in the workspace, so the orchestrator treats a timeout
            # that changed files as progress rather than as a dead backend.
            return AgentReply(
                text="",
                error="claude timed out after %s"
                % human_duration(self.timeout),
                meta={"backend": self.backend, "timed_out": True, "timeout_s": self.timeout},
            )
        except OSError as exc:
            # Found but could not be started, e.g. a batch file Windows cannot
            # execute directly: say so rather than crash the session.
            return AgentReply(text="", error="could not run claude: %s" % exc)

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if not stdout:
            hint = stderr or "no output"
            if self.session_id and "resume" in hint.lower():
                self.session_id = None
                hint += " (dropping the session id; the next turn starts fresh)"
            return AgentReply(text="", error="claude produced no output: %s" % hint[:800])

        try:
            data = self._extract(json.loads(stdout))
        except ValueError:
            # A plain-text reply is still a reply.
            return AgentReply(text=stdout, meta={"backend": self.backend, "raw_text": True})

        text = data.get("result") or data.get("text") or ""
        session_id = data.get("session_id")
        if session_id:
            self.session_id = session_id
        meta = {
            "backend": self.backend,
            "session_id": session_id,
            "model": data.get("model") or self.model,
            "cost_usd": data.get("total_cost_usd"),
            "duration_ms": data.get("duration_ms"),
            "num_turns": data.get("num_turns"),
        }
        refused = self._refused(data.get("permission_denials"))
        if refused:
            meta["refused"] = refused
        if data.get("is_error") or proc.returncode != 0:
            detail = str(data.get("error") or text or stderr or "claude exited %d" % proc.returncode)
            # Only claude's own words name a sign-in problem: its error field,
            # stderr, or a result it flagged is_error. A result that is not
            # flagged is the agent's answer, and can say anything.
            own = " ".join(str(part) for part in (
                data.get("error") or "", stderr, text if data.get("is_error") else "")).lower()
            if "not logged in" in own or "/login" in own:
                detail = "Claude Code is not signed in. Run: %s" % LOGIN_HINT
            return AgentReply(text=str(text), meta=meta, error=detail[:800])
        return AgentReply(text=str(text), meta=meta)

    @classmethod
    def probe(cls, config: Optional[Dict[str, Any]] = None) -> Probe:
        # The probe has to resolve the command the same way a turn does, or
        # doctor reports "not found" for a CLI that runs perfectly well through
        # npx — and the two disagree about the same machine.
        launch, binary, globally = cls.resolve_launch(DEFAULT_CANDIDATES)
        # Computed here, before any return: the error branches were printing
        # the npx path as the install location too, which is the same misreport
        # in the messages people see when something is already wrong.
        where = binary if globally else "via npx, nothing installed globally"
        if not binary:
            return Probe(
                ok=False,
                detail="`claude` CLI not found",
                fix="%s   then:  claude auth login" % INSTALL_HINT,
            )
        logged_in, detail = read_auth_status(launch)
        if logged_in is False:
            return Probe(ok=False, detail="installed but %s" % detail, fix=LOGIN_HINT)
        if logged_in is None and detail.startswith(TIMED_OUT):
            # Out of time is unknown, not signed out: refusing here stopped a
            # signed-in session and advised a login that could not help.
            return Probe(ok=True, detail="installed (%s), sign-in not confirmed" % where,
                         warning=timed_out_warning(detail))
        if logged_in is None:
            # A CLI that cannot start is not a CLI that is signed out, and this
            # has to be decided before falling back to --version, which fails
            # the same way and would send the user to a login that cannot work.
            if detail.startswith("could not run"):
                return Probe(ok=False, detail="installed at %s, but %s" % (where, detail),
                             fix=RUNTIME_FIX)
            try:
                proc = subprocess.run(launchable(list(launch) + ["--version"]), capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                return Probe(ok=False, detail="found %s but could not run it: %s" % (where, exc),
                             fix=RUNTIME_FIX)
            if proc.returncode != 0:
                version_noise = (proc.stdout or "") + (proc.stderr or "")
                broken = looks_unrunnable(version_noise)
                if broken:
                    return Probe(ok=False,
                                 detail="installed at %s, but could not run claude: %s" % (where, broken),
                                 fix=RUNTIME_FIX)
                return Probe(ok=False, detail="%s --version exited %d" % (where, proc.returncode),
                             fix=LOGIN_HINT)
            return Probe(
                ok=False,
                detail="installed, but the login state could not be read (%s)" % detail,
                fix=LOGIN_HINT,
            )
        return Probe(ok=True, detail="%s (%s)" % (detail, where))
