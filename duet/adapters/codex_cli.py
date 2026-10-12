"""ChatGPT, through OpenAI's Codex CLI.

This is the default way duet talks to ChatGPT, because it signs in with a
ChatGPT account — no API key, no per-token billing. It also gives this side its
own tools, so both peers can read, edit and run things directly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
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
    os.environ.get("DUET_CODEX_BIN", ""),
    "codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    os.path.expanduser(os.path.join("~", ".local", "bin", "codex")),   # not Path.home(): see claude_code
)

INSTALL_HINT = "npm install -g @openai/codex"
LOGIN_HINT = "codex login   (sign in with your ChatGPT account — no API key needed)"


USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "quota")

OTHER_PAIRS = (
    "`--pair claude+gpt` uses an OpenAI API key instead, and "
    "`--pair claude:opus+claude:sonnet` uses two Claude models."
)


def usage_limit_line(output: str) -> str:
    """The line where codex says the account is out of allowance, or "".

    Only ERROR lines count. codex echoes the prompt back into its own output,
    and duet prompts are full of words like "quota" — matching anywhere would
    let a task description about usage limits convince duet it had hit one.
    """
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line.upper().startswith("ERROR"):
            continue
        if any(marker in line.lower() for marker in USAGE_LIMIT_MARKERS):
            return line
    return ""


def explain_usage_limit(line: str) -> str:
    return (
        "%s\n\nThat is the ChatGPT account's Codex allowance, not duet. "
        "Wait for the reset, upgrade the plan, or run this pair another "
        "way: %s\n\nduet has written this down: `duet doctor` will report the "
        "account as out of quota until the reset passes or a ChatGPT turn "
        "succeeds again." % (line, OTHER_PAIRS)
    )


def explain_failure(output: str) -> str:
    """Pull the actual error out of codex's output.

    codex prints a banner and echoes the prompt before it fails, and the real
    line is at the *end*. Reporting the first N characters means reporting the
    banner — which is how a plain usage limit got misdiagnosed as a stdin bug
    and cost a code change. Look for the error, then fall back to the tail.

    A usage limit wins over any other error in the same output. It is what duet
    writes down and what doctor will report afterwards, so reporting something
    else here would leave the user with a mystery error on screen and a quota
    verdict in `duet doctor` that nothing on screen accounts for.
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    limit = usage_limit_line(output)
    if limit:
        return explain_usage_limit(limit)
    errors = [line for line in lines if line.upper().startswith("ERROR")]
    if errors:
        return errors[0]
    return "\n".join(lines[-6:]) or "codex produced no output"


# -- what duet knows about the account's Codex allowance ---------------------
#
# Nothing codex offers reports remaining quota without spending a model call:
# `codex login status` answers "am I signed in", which is a different question,
# and the only cheap way to learn the answer is to be told it. So duet does not
# guess. It remembers the one moment the account itself said the allowance was
# gone, and it forgets that the moment a turn succeeds.

QUOTA_NOTE = "codex-quota.json"

_RESET_AT = re.compile(r"try again (?:at|on|after)\s+(.+?)\s*$", re.IGNORECASE)

# Parsed by hand rather than with strptime, because `%b` and `%p` read the
# process locale: on a machine with a non-English LC_TIME, `strptime` would
# refuse "Oct" and "PM" — and codex prints English whatever the locale is.
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_STAMP = re.compile(
    r"^(?P<month>[a-z]{3,9})\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})"
    r"(?:[\s,]+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>am|pm)?)?$",
    re.IGNORECASE,
)


def state_dir() -> Path:
    """Where duet keeps what it has learned about the account, not the project.

    Quota belongs to the ChatGPT account, so it cannot live in one workspace.
    DUET_STATE_DIR exists so tests never touch the real home directory.
    """
    override = os.environ.get("DUET_STATE_DIR")
    return Path(os.path.expanduser(override or os.path.join("~", ".duet")))


def parse_reset_at(message: str) -> Optional[float]:
    """The epoch second codex named as the reset, or None if it named none.

    None is a real answer here: it means "still out of quota, reset unknown",
    which doctor reports differently from a deadline it can actually check.
    """
    match = _RESET_AT.search((message or "").strip().rstrip("."))
    if not match:
        return None
    raw = match.group(1).strip().rstrip(".")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    stamp = _STAMP.match(raw)
    if not stamp:
        return None
    month = _MONTHS.get(stamp.group("month")[:3].lower())
    if month is None:
        return None
    hour = int(stamp.group("hour") or 0)
    meridiem = (stamp.group("meridiem") or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    try:
        return datetime(int(stamp.group("year")), month, int(stamp.group("day")),
                        hour, int(stamp.group("minute") or 0)).timestamp()
    except (ValueError, OverflowError, OSError):   # a date codex could not mean
        return None


def record_usage_limit(message: str, note_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Write down that the account said it was out of Codex allowance."""
    note = {
        "seen_at": time.time(),
        "message": message,
        "resets_at": parse_reset_at(message),
    }
    directory = note_dir or state_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / QUOTA_NOTE).write_text(json.dumps(note), encoding="utf-8")
    except OSError:
        pass                    # a note we cannot write is not worth a crash
    return note


def clear_usage_limit(note_dir: Optional[Path] = None) -> None:
    """A turn that ran is proof the allowance is back. Forget the note."""
    try:
        ((note_dir or state_dir()) / QUOTA_NOTE).unlink()
    except OSError:
        pass


def read_usage_limit(note_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The note, if there is one that is still in force; otherwise None.

    A note whose stated reset has passed is deleted rather than reported: it
    describes a limit that is over, and doctor must not carry it forward.
    """
    path = (note_dir or state_dir()) / QUOTA_NOTE
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(note, dict):
        return None
    resets_at = note.get("resets_at")
    if isinstance(resets_at, (int, float)) and resets_at <= time.time():
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return note


def _when(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "an unknown time"


def read_login_status(binary, timeout: int = 45) -> Tuple[Optional[bool], str]:
    """(logged_in, human description). None means it could not be determined."""
    first = binary[0] if isinstance(binary, (list, tuple)) else binary
    out = ""
    for env in (None, env_with_sibling_path(first)):
        try:
            argv = list(binary) if isinstance(binary, (list, tuple)) else [binary]
            proc = run_status_check(argv + ["login", "status"], timeout, env)
        except subprocess.TimeoutExpired:
            return None, timed_out_detail("codex login status", timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, str(exc)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        # A broken interpreter earlier on PATH is worth one retry with the
        # CLI's own directory first; anything else is a real answer.
        if not looks_unrunnable(out):
            break
    broken = looks_unrunnable(out)
    if broken:
        return None, "could not run codex: %s" % broken
    low = out.lower()
    if "not logged in" in low or "please log in" in low or "run `codex login`" in low:
        return False, out.splitlines()[0] if out else "not logged in"
    if "logged in" in low:
        return True, out.splitlines()[0]
    if proc.returncode != 0:
        return False, out.splitlines()[0] if out else "codex login status exited %d" % proc.returncode
    return None, out.splitlines()[0] if out else "could not determine login state"


class CodexCliAdapter(Adapter):
    backend = "codex-cli"
    display = "ChatGPT (Codex)"
    edits_workspace = True
    npm_package = "@openai/codex"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        launch, binary, self.installed_globally = self.resolve_launch(DEFAULT_CANDIDATES)
        self.bin = binary or launch[0]   # sets launch to one element, then
        self.launch = launch             # widen it for the npx form
        self.timeout = int(self.config.get("timeout", DEFAULT_TURN_TIMEOUT))
        self.sandbox = str(self.config.get("sandbox", "workspace-write"))
        self.extra_args: List[str] = list(self.config.get("extra_args") or [])
        # Continuity is cheap when it works and costs nothing when it does not:
        # every prompt duet builds is self-contained, so a failed resume falls
        # back to a fresh session without losing anything that matters.
        self.use_resume = bool(self.config.get("use_resume", True))
        self.turns = 0

    def read_only(self) -> str:
        """Codex has a sandbox for exactly this; use it instead of asking."""
        self.sandbox = "read-only"
        return "sandbox read-only"

    WEB_SEARCH = ["-c", "tools.web_search=true"]

    def allow_web(self) -> str:
        """Turn on Codex's web search for this adapter's runs only."""
        if self.WEB_SEARCH[1] not in self.extra_args:
            self.extra_args += self.WEB_SEARCH
        return "web search on (%s)" % " ".join(self.WEB_SEARCH)

    def _base(self, last_message_file: str) -> List[str]:
        # --color never keeps ANSI escapes out of anything we parse; -o gives us
        # the agent's final message verbatim instead of scraped from the log.
        cmd = list(self.launch) + [
            "exec",
            "--skip-git-repo-check",
            "--color", "never",
            "-C", self.cwd,
            "-o", last_message_file,
        ]
        if self.sandbox:
            cmd += ["--sandbox", self.sandbox]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + self.extra_args

    def send(self, prompt: str, system: str = "", round_no: int = 0) -> AgentReply:
        full = ("%s\n\n---\n\n%s" % (system, prompt)) if system else prompt
        handle, last_path = tempfile.mkstemp(prefix="duet-codex-", suffix=".txt")
        os.close(handle)
        try:
            attempts: List[List[str]] = []
            if self.turns and self.use_resume:
                attempts.append(self._base(last_path) + ["resume", "--last", full])
            attempts.append(self._base(last_path) + [full])

            last_error = ""
            blip_retried = False
            index = 0
            while index < len(attempts):
                args = attempts[index]
                index += 1
                try:
                    proc = subprocess.run(
                        launchable(args),
                        cwd=self.cwd,
                        capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=self.timeout,
                        # `codex exec` appends piped stdin to the prompt, so an
                        # inherited pipe makes it block forever waiting for EOF.
                        # duet is almost never run from a tty, so close it.
                        stdin=subprocess.DEVNULL,
                        env=env_with_sibling_path(self.bin),
                    )
                except FileNotFoundError:
                    return AgentReply(
                        text="",
                        error="the `codex` CLI was not found. Install it with `%s`, then "
                        "`codex login` to sign in with your ChatGPT account." % INSTALL_HINT,
                    )
                except subprocess.TimeoutExpired:
                    # See the same branch in the claude adapter: a turn cut off at
                    # its limit leaves its work behind, so say so in the reply.
                    return AgentReply(
                        text="",
                        error="codex timed out after %s"
                        % human_duration(self.timeout),
                        meta={"backend": self.backend, "timed_out": True,
                              "timeout_s": self.timeout},
                    )
                except OSError as exc:
                    return AgentReply(text="", error="could not run codex: %s" % exc)

                stdout = (proc.stdout or "").strip()
                combined = (stdout + "\n" + (proc.stderr or "")).strip()

                if "not logged in" in combined.lower():
                    # Confirm it before believing it. A blip that happens to say
                    # this once cost a whole round in a real session, and the
                    # login was fine before and after.
                    # self.launch, not self.bin: under the npx fallback the
                    # binary is npx, and `npx login status` produces exactly
                    # the false sign-out this re-check exists to rule out.
                    logged_in, detail = read_login_status(self.launch)
                    if logged_in is False:
                        return AgentReply(
                            text="", error="codex is not signed in. Run: %s" % LOGIN_HINT
                        )
                    last_error = ("codex reported a sign-in problem, but `codex login "
                                  "status` says %s. Treating it as transient: %s"
                                  % (detail, combined[:300]))
                    if not blip_retried:
                        blip_retried = True
                        attempts.append(args)      # one more go at the same call
                    continue

                final = ""
                try:
                    final = Path(last_path).read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    final = ""
                text = final or stdout

                if proc.returncode == 0 and text:
                    self.turns += 1
                    # A turn that ran is the only cheap proof the allowance is
                    # there, so it is also the only thing that clears the note.
                    clear_usage_limit()
                    return AgentReply(
                        text=text,
                        meta={"backend": self.backend, "model": self.model,
                              "used_last_message_file": bool(final)},
                    )
                limit = usage_limit_line(combined)
                if limit:
                    record_usage_limit(limit)
                last_error = explain_failure(combined) or "codex exited %d" % proc.returncode
            return AgentReply(text="", error=last_error)
        finally:
            try:
                os.unlink(last_path)
            except OSError:
                pass

    def state(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "turns": self.turns}

    def restore(self, state: Dict[str, Any]) -> None:
        # `turns` is the whole of this adapter's memory, and the only thing it
        # decides is whether the next call says `resume --last`. That is off by
        # default now (see __init__), so for most pairs this restores nothing at
        # all — but someone who set `use_resume` asked for continuity, and a duet
        # session resumed without this would silently start codex from scratch.
        super().restore(state)
        try:
            self.turns = int((state or {}).get("turns") or 0)
        except (TypeError, ValueError):
            self.turns = 0

    @classmethod
    def after_login(cls, launch, exit_code: int) -> None:
        # The usage-limit note belongs to whichever account hit the limit, and
        # duet cannot tell whether this sign-in was that account or another one.
        # Keeping it reported a freshly signed-in account as out of quota until
        # the old account's reset; a turn that is really limited writes it back.
        # Only a sign-in that worked: a failed or cancelled one changed nothing.
        if exit_code == 0 and read_login_status(launch)[0] is True:
            clear_usage_limit()

    @classmethod
    def probe(cls, config: Optional[Dict[str, Any]] = None) -> Probe:
        # The probe has to resolve the command the same way a turn does, or
        # doctor reports "not found" for a CLI that runs perfectly well through
        # npx — and the two disagree about the same machine.
        launch, binary, globally = cls.resolve_launch(DEFAULT_CANDIDATES)
        where = binary if globally else "via npx, nothing installed globally"
        if not binary:
            return Probe(
                ok=False,
                detail="`codex` CLI not found",
                fix="%s   then:  codex login" % INSTALL_HINT,
            )
        logged_in, detail = read_login_status(launch)
        if logged_in is False:
            return Probe(ok=False, detail="installed but not signed in (%s)" % detail,
                         fix=LOGIN_HINT, signed_in=False)
        signed_in: Optional[bool] = True
        warning = ""
        if logged_in is None and detail.startswith(TIMED_OUT):
            # Out of time is unknown, not signed out — the same mistake that
            # refused a signed-in Claude under load. Go on to the quota note,
            # which is still worth reporting, but never advise a login.
            signed_in, warning, detail = None, timed_out_warning(detail), "sign-in not confirmed"
        elif logged_in is None:
            if detail.startswith("could not run"):
                return Probe(ok=False, detail="installed at %s, but %s" % (where, detail),
                             fix=RUNTIME_FIX)
            return Probe(ok=False, detail="could not read the login state: %s" % detail,
                         fix=LOGIN_HINT)

        # Signed in is not the same as able to run a turn. `codex login status`
        # answers the first question only, so this is as far as it can honestly
        # be taken — except where the account has already told us otherwise.
        note = read_usage_limit()
        if note:
            resets_at = note.get("resets_at")
            seen = _when(note.get("seen_at"))
            path = state_dir() / QUOTA_NOTE
            if isinstance(resets_at, (int, float)) and signed_in is not None:
                return Probe(
                    ok=False,
                    signed_in=signed_in,     # so `duet login` does not open a browser
                    warning=warning,
                    detail="signed in, but out of Codex usage quota until %s "
                           "(the account said so at %s)" % (_when(resets_at), seen),
                    fix="wait for %s, upgrade the plan at "
                        "https://chatgpt.com/explore/plus, or run this pair another "
                        "way: %s\n    If that reset has already passed, delete %s."
                        % (_when(resets_at), OTHER_PAIRS, path),
                )
            if isinstance(resets_at, (int, float)):
                # The sign-in check itself timed out, so "signed in" is not
                # something duet actually knows — refusing on the old note
                # would be the same mistake as reading a timeout as signed out,
                # just dressed up as a quota message instead of a login hint.
                return Probe(
                    ok=True,
                    signed_in=signed_in,
                    warning="%s Also, a usage limit was recorded until %s (at %s); if the "
                            "first turn stops again, %s  (clears itself on the next "
                            "turn that succeeds; or delete %s)"
                            % (warning, _when(resets_at), seen, OTHER_PAIRS, path),
                    detail="%s (%s) — Codex usage quota not checked" % (detail, where),
                )
            # Unknown is not the same as exhausted, and a hard failure here would
            # block the one thing that can settle it: the note only ever clears
            # on a turn that succeeds, and `duet run` refuses on a failed probe.
            return Probe(
                ok=True,
                signed_in=signed_in,
                warning=warning,
                detail="%s (%s) — Codex usage quota not checked.\n"
                       "    duet hit the usage limit at %s and codex named no reset "
                       "time, so it may still be in force: if the first ChatGPT turn "
                       "stops again, %s  (clears itself on the next turn that "
                       "succeeds; or delete %s)"
                       % (detail, where, seen, OTHER_PAIRS, path),
            )
        return Probe(
            ok=True,
            signed_in=signed_in,
            warning=warning,
            detail="%s (%s) — Codex usage quota not checked "
                   "(reading it would cost a model call)" % (detail, where),
        )
