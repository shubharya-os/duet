from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class AgentReply:
    text: str
    meta: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def timed_out(self) -> bool:
        """True when the harness cut this turn off at its time limit.

        A timeout is not a backend failure: the agent was working, and whatever
        it had already written to the workspace is still there. The caller has to
        be able to tell the two apart, so every adapter that enforces a turn
        limit marks the reply instead of only saying so in prose.
        """
        return bool(self.meta.get("timed_out"))


# How long one turn may take when nothing says otherwise. Both CLI adapters used
# to hard-code this, and it could not be changed from the command line at all —
# so a lead doing a large, legitimate piece of work hit the same 1800s twice and
# the session died on it. One default, one place, and `--turn-timeout` overrides.
DEFAULT_TURN_TIMEOUT = 1800


def human_duration(seconds: Any) -> str:
    """"30 minutes", "1 hour 15 minutes", "90 seconds" — for prompts and errors."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "an unknown time"
    if total < 120:
        return "%d second%s" % (total, "" if total == 1 else "s")
    minutes, hours = total // 60, total // 3600
    if hours < 1:
        return "%d minute%s" % (minutes, "" if minutes == 1 else "s")
    rest = minutes - hours * 60
    out = "%d hour%s" % (hours, "" if hours == 1 else "s")
    if not rest:
        return out
    return out + " %d minute%s" % (rest, "" if rest == 1 else "s")


# A failure that is the network's or the backend's, not the work's. A turn that
# died on one of these never happened: nothing was read, nothing was written, and
# what came back is a banner rather than an answer. Observed live: a few minutes
# without DNS ended a session that was making progress, because every turn
# failed instantly with "Can't reach the API server ... (ENOTFOUND)" and two of
# them in a row tripped the "failed twice" rule.
TRANSIENT_MARKERS = (
    "can't reach the api", "cannot reach the api", "could not reach the api",
    "unable to reach the api", "check your internet",
    "enotfound", "eai_again", "econnreset", "econnaborted", "etimedout",
    "connection reset", "connection aborted", "connection closed",
    "network error", "network is unreachable", "socket hang up",
    "temporary failure in name resolution", "dns",
    "http 429", "http 500", "http 502", "http 503", "http 504",
    "status 429", "status 500", "status 502", "status 503", "status 504",
    "429 too many requests", "too many requests",
    "bad gateway", "service unavailable", "gateway timeout",
    "internal server error", "overloaded", "server error",
    "temporarily unavailable", "please try again",
    # codex's half of the same failure: when the SSE stream drops mid-turn it
    # says this and nothing else, and reqwest underneath it says the second. A
    # usage limit can also end a stream, which is why DURABLE_MARKERS is
    # consulted first — codex's allowance message always carries the words
    # "allowance" and "quota" by the time duet reports it. A bare "stream error"
    # is deliberately not here: it is not always the network, and this function
    # would rather report a failure it does not know than wait on a real one.
    "stream disconnected", "error sending request",
)

# The prose above only fires when the backend spells the status out in words, and
# the one duet runs most does not: Claude Code prints "API Error: 429 {…}" and
# "API Error: 503" with a JSON body, so the number is the only readable part. A
# 429 or a 5xx matched this way is exactly the "rate limited / HTTP 5xx" case
# that has to be waited out rather than reported as the agent's failure. The
# status has to sit next to an error/status/http/code word, so a message that
# merely contains "503" somewhere is not mistaken for one, and 4xx other than
# 429 is left alone: a 400 or a 401 does not improve by waiting.
TRANSIENT_STATUS = re.compile(
    r"(?:error|status|code|http)[\s:=#-]{0,3}(429|500|502|503|504|529)(?!\d)"
)


def _standalone(markers: Sequence[str]) -> "re.Pattern[str]":
    """Match each marker as its own token, not as a substring of a name.

    The same mistake the status matcher above already guards against, one tier
    up: `503` had to be kept from matching `test_error_503_handling.py`, and
    `dns` has to be kept from matching `tests/test_dns_resolver.py`,
    `dns_cache.py` and `app/dns.py`. It matters because what gets classified is
    not only a banner — when the CLI reports `is_error`, the reply's error *is*
    the agent's own message, truncated — so a turn that really failed while the
    pair happened to be working on networking code would be re-sent five more
    times, at a full turn's cost each, before duet said what had gone wrong.

    The lookarounds refuse a marker that sits inside a path or an identifier;
    the last one refuses a file extension, so `dns.py` is a file and `or DNS.`
    at the end of a sentence is still the network. Only the transient list is
    matched this way. DURABLE_MARKERS stays a plain substring search on purpose:
    over-matching there reports a failure duet is unsure about instead of
    waiting on it, and `insufficient_quota` — the shape the OpenAI backend
    actually returns — is a durable marker glued into an identifier.
    """
    joined = "|".join(re.escape(m) for m in markers)
    return re.compile(r"(?<![\w/-])(%s)(?![\w/-])(?!\.[A-Za-z0-9]{1,4}\b)" % joined)

# These win over any marker above. A signed-out CLI, a missing binary, a rejected
# key and an exhausted allowance all describe a state that waiting will not
# change — retrying them for four minutes only delays telling the truth. The
# allowance case matters most: codex reports it with the words "rate limit", and
# duet already has a place it writes that verdict down.
DURABLE_MARKERS = (
    "usage limit", "allowance", "quota", "try again at", "try again on",
    "try again after", "not signed in", "not logged in", "sign in", "login",
    "api key", "was not found", "install it with", "insufficient",
)

TRANSIENT_RE = _standalone(TRANSIENT_MARKERS)


def transient_failure(text: str) -> str:
    """The marker that makes this failure worth retrying, or "" if it is real.

    Deliberately conservative: a failure duet does not recognise is reported to
    the pair as it always was. Getting this wrong in the other direction is
    worse — it would sit in a backoff loop waiting for a sign-in to fix itself.
    """
    low = " ".join((text or "").lower().split())
    if not low:
        return ""
    for marker in DURABLE_MARKERS:
        if marker in low:
            return ""
    found = TRANSIENT_RE.search(low)
    if found:
        return found.group(1)
    status = TRANSIENT_STATUS.search(low)
    if status:
        return "http %s" % status.group(1)
    return ""


# A CLI that cannot start says nothing about whether you are signed in. These
# are what the shell and the loader say when the binary is there but unusable —
# usually a broken or missing `node` shadowing the working one on PATH.
BROKEN_RUNTIME_MARKERS = (
    "bad cpu type",
    "exec format error",
    "cannot execute",
    "command not found",
    "no such file or directory",
    "env: node",
    "dyld",
    "symbol not found",
)


def looks_unrunnable(output: str) -> str:
    """The reason this CLI could not start, or "" if it did start.

    Reporting "not signed in" for a CLI that never ran sends people to a login
    command that will fail the same way, with nothing to tell them why.
    """
    low = (output or "").lower()
    for marker in BROKEN_RUNTIME_MARKERS:
        if marker in low:
            first = next((line.strip() for line in (output or "").splitlines() if line.strip()), "")
            return first or marker
    return ""


def env_with_sibling_path(binary: str, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """A copy of the environment with the CLI's own directory first on PATH.

    When `claude` lives in /opt/homebrew/bin, the `node` it was installed with
    is almost certainly there too — and the reason it failed to start is some
    other, broken `node` earlier on PATH. Putting the CLI's own directory first
    uses the toolchain it shipped beside, which is what a working install would
    have done anyway.
    """
    env = dict(env if env is not None else os.environ)
    directory = os.path.dirname(os.path.abspath(binary))
    if directory:
        current = env.get("PATH", "")
        if current.split(os.pathsep)[:1] != [directory]:
            env["PATH"] = directory + (os.pathsep + current if current else "")
    return env


# How a status reader says "this check ran out of time". A slow answer is not
# a "no": on a machine under heavy load `claude auth status` has taken longer
# than 45 seconds for an account that was signed in, and reading that as signed
# out refused the session and sent the user to a login they did not need.
TIMED_OUT = "timed out"


def run_status_check(argv: List[str], timeout: int, env: Optional[Dict[str, str]] = None):
    """Run a sign-in status command, giving it one longer second chance.

    Raises subprocess.TimeoutExpired only when the retry ran out of time too;
    the caller reports that as unknown, never as signed out.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout * 3, env=env)


def timed_out_detail(what: str, timeout: int) -> str:
    return "%s: `%s` did not answer in %ds, nor in %ds on a retry" % (
        TIMED_OUT, what, timeout, timeout * 3)


def timed_out_warning(detail: str) -> str:
    return ("the sign-in check could not finish (%s). That is usually a heavily "
            "loaded machine, not a sign-out, so the session starts anyway; if it "
            "really is signed out, the first turn will say so." % detail)


RUNTIME_FIX = (
    "that CLI could not start at all, so its sign-in state is unknown. It is "
    "usually a broken `node` earlier on PATH than the working one — check "
    "`node -v`, and if it fails, put a working node first (e.g. "
    "/opt/homebrew/bin before /usr/local/bin)."
)


@dataclass
class Probe:
    """What `duet doctor` reports for one backend.

    `signed_in` is separate from `ok` because they are separate questions. A
    backend can be signed in and still not ready — an exhausted allowance is
    the case that prompted this — and `duet login` must not answer a quota
    problem by opening a browser and then reporting "still not signed in".
    None means the probe could not tell.
    """

    ok: bool
    detail: str
    fix: str = ""
    signed_in: Optional[bool] = None
    # Something worth saying that does not stop a session: `ok` stays True and
    # this is shown beside it. A sign-in check that only ran out of time is the
    # case that needs it.
    warning: str = ""


class Adapter:
    """A peer that can take a turn.

    `edits_workspace` is the important one: an agent with its own tools changes
    files itself, and an agent without them ships `patches` in its envelope for
    the harness to apply. Either way both sides can build, so either side can
    lead.
    """

    backend = "base"
    display = "agent"
    edits_workspace = False

    def __init__(self, name: str = "agent", cwd: str = ".", model: str = "", config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.model = model
        self.config = dict(config or {})
        self.session_id: Optional[str] = None

    # -- interface --------------------------------------------------------
    def send(self, prompt: str, system: str = "", round_no: int = 0) -> AgentReply:
        raise NotImplementedError

    def set_turn_timeout(self, seconds: int) -> None:
        """Give this agent `seconds` for one turn, whatever its backend's default.

        Set on the adapter rather than only in the spec it was built from, so an
        adapter handed to the orchestrator ready-made obeys the same limit as one
        duet built itself.
        """
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return
        if seconds <= 0:
            return
        self.config["timeout"] = seconds
        self.timeout = seconds

    def allow_gate(self, gate: str) -> None:
        """Let this agent run the acceptance gate itself, if it needs permission.

        An agent that cannot run the gate is taking the harness's word for it,
        which is exactly the second-hand knowledge this project exists to avoid.
        Default: nothing to do.
        """
        return None

    def read_only(self) -> str:
        """Stop this agent writing to the workspace, if the backend can.

        `duet review` asks for an opinion, not an edit, and asking politely in
        the prompt is not a control. Returns a short description of what was
        actually enforced, or "" when the backend cannot enforce anything — the
        caller checks the workspace afterwards either way.
        """
        return "" if self.edits_workspace else "no tools in this workspace"

    @classmethod
    def probe(cls, config: Optional[Dict[str, Any]] = None) -> Probe:
        return Probe(ok=False, detail="no probe implemented for %s" % cls.backend)

    @classmethod
    def after_login(cls, launch: Sequence[str], exit_code: int) -> None:
        """Called by `duet login` once this backend's own sign-in has run.

        Anything duet remembered about the account signed in before may not be
        true of the one signed in now.
        """

    def describe(self) -> str:
        return "%s (%s%s)" % (self.display, self.backend, ", " + self.model if self.model else "")

    def state(self) -> Dict[str, Any]:
        return {"session_id": self.session_id}

    def restore(self, state: Dict[str, Any]) -> None:
        self.session_id = (state or {}).get("session_id")

    # -- launching --------------------------------------------------------
    npm_package = ""          # set by adapters that ship as an npm CLI

    @property
    def bin(self) -> str:
        """The command this adapter runs.

        Assigning to it is how a caller says "run exactly this instead", which
        is what the tests do with a stub, so it has to rewrite the argv prefix
        too. Keeping the two as independent attributes meant a test could set
        one and silently execute the other — and the real CLI.
        """
        return getattr(self, "_bin", "")

    @bin.setter
    def bin(self, value: str) -> None:
        self._bin = value
        self.launch = [value]

    @classmethod
    def resolve_launch(cls, candidates) -> "tuple":
        """(argv prefix, binary, installed_globally).

        Prefer a real binary on PATH. Falling back to `npx -y <package>` means
        duet works with nothing installed globally — the sign-in lives in the
        agent's own config directory, so an npx-run CLI sees the same account —
        which removes the one step of setup that changes the machine outside
        duet's own directory.
        """
        found = cls.which(*candidates)
        if found:
            return [found], found, True
        npx = cls.which("npx")
        if npx and cls.npm_package:
            return [npx, "-y", cls.npm_package], npx, False
        # Nothing resolved. The bare name is still worth returning so an error
        # message can say what was looked for, but the binary is reported empty
        # so a probe can tell "found" from "gave up and guessed".
        fallback = next((c for c in candidates if c and os.sep not in c), "")
        return [fallback], "", True

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def which(*candidates: str) -> Optional[str]:
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.sep in candidate:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
                continue
            found = shutil.which(candidate)
            if found:
                return found
        return None
