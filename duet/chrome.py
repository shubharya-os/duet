"""Driving a real browser with nothing but the standard library.

A website usually has no test command, so a duet session on one has no gate —
two models agreeing by argument about CSS neither of them has seen rendered.
This is the half of the answer that talks to Chrome: find it, start it headless
on a throwaway profile, speak the DevTools protocol over a pipe, and always
kill it again. What to *check* once a page is rendered lives in page.py.

Why a pipe and not a websocket: `--remote-debugging-pipe` hands Chrome file
descriptors 3 and 4 and speaks the same protocol over them, NUL-terminated,
which json and os.read can do between them. `--remote-debugging-port` would
mean a websocket client, which the standard library does not have.

Every flag here is the answer to something observed rather than a precaution:

- Headless Chrome will not make a window narrower than 500px, so a phone width
  cannot be a window size. `Emulation.setDeviceMetricsOverride` sets the exact
  viewport instead, and is the only reason this drives the protocol at all.
- A fresh profile on macOS can block on a Keychain prompt that headless Chrome
  never shows anyone: `--use-mock-keychain --password-store=basic`. The profile
  is always a throwaway directory, so nobody's real browser is ever touched.
- Pages that fetch from other hosts stall or flake, and a flaky gate is worse
  than no gate: every outside host is mapped to nothing unless the caller opts
  out for a page served from a local dev server.
- duet runs gates with a minimal PATH, so Chrome is looked for in the places it
  is actually installed as well as on PATH — and not finding it is reported as
  a setup fault, never as a fault in the page.
- `os.killpg` is the right cleanup, because Chrome's helper processes live in
  its process group. Under duet's own agent sandbox it raises EPERM, so the
  group is attempted, the child is killed regardless, and teardown never raises
  on a run that already produced its answer.
"""

from __future__ import annotations

import json
import os
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:             # pragma: no cover - Windows, where page is refused
    fcntl = None                # type: ignore[assignment]


class SetupError(Exception):
    """duet page could not run at all.

    Held apart from everything in page.py on purpose: "no Chrome on this
    machine" and "this page has a broken layout" are different answers, and a
    session that reads the first as the second spends its rounds hunting a bug
    that is not there. This is exit 3; page faults are exit 1.
    """



class NoAnswer(SetupError):
    """Chrome is running but has not answered yet: slow, not broken.

    Its own type because the two want different handling. A Chrome that exited
    or broke the pipe will not answer however long duet waits; one that is
    merely slow on a loaded machine usually will, and calling that a setup
    fault failed a gate under a load average of 200 that passed alone.
    """


# What the browser is called on PATH, in the order worth trying.
PATH_NAMES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "chrome", "chrome-browser",
)

# Where it is installed when it is not on PATH — a gate runs with a minimal
# PATH, and "no Chrome found" on a machine that has Chrome is a bad answer.
INSTALL_PATHS = (
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/usr/local/bin/chrome",
    "/opt/google/chrome/chrome",
)

ENV_VAR = "DUET_CHROME"

# Flags that make a run reproducible and leave no trace. --remote-debugging-pipe
# is the one that matters; the rest exist to stop a fresh profile asking a
# question nobody can answer in a headless browser.
BASE_FLAGS = (
    "--remote-debugging-pipe",
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-component-update",
    "--disable-background-networking",
    "--disable-client-side-phishing-detection",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-service-autorun",
    "--mute-audio",
    "--use-mock-keychain",
    "--password-store=basic",
    "--window-size=1280,900",
)

# Every host that is not the page itself resolves to nothing. A homepage that
# pulls a font or an analytics script from outside would otherwise make the
# gate's answer depend on the network.
OFFLINE_FLAG = "--host-resolver-rules=MAP * ~NOTFOUND"

# Chrome is being asked simple questions; anything slower than this is stuck.
CALL_TIMEOUT = 30.0
# Except the first one: a cold Chrome on a loaded machine has a profile to
# build before it says anything, and calling that "Chrome did not answer" is a
# setup fault reported for a browser that was only slow.
START_TIMEOUT = 90.0
# A step that ran out of time while Chrome was still alive is waited on once
# more, this many times as long, before it counts as a setup fault.
SLOW_RETRY_FACTOR = 2.0


def unsupported_platform() -> Optional[str]:
    """Why `duet page` cannot run here at all, or None.

    Windows has neither the file descriptors this transport hands Chrome nor a
    process group to kill, so it says so instead of failing halfway through in
    a way that reads like a broken page.
    """
    if sys.platform.startswith("win"):
        return ("duet page is not supported on Windows: it drives Chrome over "
                "file descriptors 3 and 4, which is a POSIX-only arrangement. "
                "It works on macOS and Linux, including WSL.")
    return None


def find_chrome(env: Optional[Dict[str, str]] = None) -> str:
    """The browser to drive: $DUET_CHROME, then PATH, then where it installs.

    Raises SetupError — never a page fault — when there is none, and when
    DUET_CHROME points at something that is not there, because silently
    ignoring an explicit choice is how you end up debugging the wrong browser.
    """
    environ = os.environ if env is None else env
    chosen = (environ.get(ENV_VAR) or "").strip()
    if chosen:
        if os.path.isfile(chosen) and os.access(chosen, os.X_OK):
            return chosen
        found = shutil.which(chosen)
        if found:
            return found
        raise SetupError(
            "%s is set to %r, which is not an executable file. Unset it to let "
            "duet look for Chrome itself." % (ENV_VAR, chosen))
    for name in PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in INSTALL_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise SetupError(
        "no Chrome or Chromium found. duet page renders the page in a real "
        "browser, so it needs one: install Google Chrome or Chromium, or point "
        "%s at the binary." % ENV_VAR)


def _dup_onto(source: int, target: int) -> None:
    """Put `source` on exactly fd `target` in this (forked) process.

    Chrome's pipe transport is not configurable: it reads fd 3 and writes fd 4.
    The copy goes via a high descriptor first, because the two pipes duet just
    created might themselves be sitting on 3 and 4, and dup2 would then close
    the one it is about to need.
    """
    high = fcntl.fcntl(source, fcntl.F_DUPFD, 20)
    try:
        os.dup2(high, target)       # dup2 clears CLOEXEC, so exec keeps it
    finally:
        os.close(high)


class Browser:
    """One headless Chrome, and the protocol conversation with it.

    Used as a context manager, or with close() in a finally: a Chrome that
    outlives its caller is a process holding a profile directory open forever.
    """

    def __init__(self, chrome: Optional[str] = None, allow_network: bool = False,
                 timeout: float = CALL_TIMEOUT, extra_flags: Optional[List[str]] = None):
        problem = unsupported_platform()
        if problem:
            raise SetupError(problem)
        self.chrome = chrome or find_chrome()
        self.timeout = timeout
        self._next_id = 0
        self._buf = b""
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._events: List[Dict[str, Any]] = []
        self._closed = False
        self._profile = tempfile.mkdtemp(prefix="duet-chrome-")
        self._log = open(os.path.join(self._profile, "chrome.log"), "w+b")

        to_chrome_r, self._to_chrome_w = os.pipe()
        self._from_chrome_r, from_chrome_w = os.pipe()

        def child() -> None:               # pragma: no cover - runs post-fork
            _dup_onto(to_chrome_r, 3)
            _dup_onto(from_chrome_w, 4)

        flags = list(BASE_FLAGS) + ["--user-data-dir=%s" % self._profile]
        if not allow_network:
            flags.append(OFFLINE_FLAG)
        flags += list(extra_flags or [])
        try:
            self.proc = subprocess.Popen(
                [self.chrome] + flags,
                stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
                # close_fds would close the descriptors child() just placed on
                # 3 and 4: the subprocess module's closing pass runs after
                # preexec_fn and only spares the numbers it was told about,
                # which are this process's, not the child's.
                close_fds=False, preexec_fn=child,
                # Its own session, so the process group duet kills on the way
                # out is Chrome's helpers and not duet itself.
                start_new_session=True,
            )
        except OSError as exc:
            self._cleanup_fds([to_chrome_r, from_chrome_w])
            self._discard_profile()
            raise SetupError("could not start %s: %s" % (self.chrome, exc))
        self._cleanup_fds([to_chrome_r, from_chrome_w])
        try:
            self.version = self.call(
                "Browser.getVersion", timeout=max(self.timeout, START_TIMEOUT)
            ).get("product", "")
        except SetupError:
            self.close()
            raise

    # -- context manager --------------------------------------------------
    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport --------------------------------------------------------
    def _send(self, message: Dict[str, Any]) -> None:
        raw = json.dumps(message).encode("utf-8") + b"\0"
        while raw:
            try:
                written = os.write(self._to_chrome_w, raw)
            except OSError as exc:
                raise SetupError("%s stopped listening: %s" % (self._name(), exc))
            raw = raw[written:]

    def _receive(self, deadline: float, budget: Optional[float] = None) -> Dict[str, Any]:
        while b"\0" not in self._buf:
            left = deadline - time.time()
            if left <= 0:
                raise NoAnswer("%s did not answer within %.0fs"
                               % (self._name(), budget or self.timeout))
            try:
                ready, _, _ = select.select([self._from_chrome_r], [], [], min(0.25, left))
            except (OSError, ValueError) as exc:
                raise SetupError("%s pipe broke: %s" % (self._name(), exc))
            if not ready:
                if self.proc.poll() is not None:
                    raise SetupError(self._died())
                continue
            chunk = os.read(self._from_chrome_r, 1 << 16)
            if not chunk:
                raise SetupError(self._died())
            self._buf += chunk
        raw, _, self._buf = self._buf.partition(b"\0")
        try:
            message = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise SetupError("%s sent something that is not JSON: %s" % (self._name(), exc))
        return message if isinstance(message, dict) else {}

    def _pump(self, wanted: Optional[int], deadline: float,
              budget: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Read until the answer to `wanted` arrives, filing events on the way."""
        while True:
            if wanted is not None and wanted in self._responses:
                return self._responses.pop(wanted)
            message = self._receive(deadline, budget)
            if "id" in message:
                self._responses[int(message["id"])] = message
                continue
            if message.get("method"):
                self._events.append(message)
                self._auto_answer(message)
                if wanted is None:
                    return message

    def _auto_answer(self, event: Dict[str, Any]) -> None:
        """Dismiss a dialog rather than let alert() hang the whole run.

        A page that calls alert() on load blocks its own load event, and every
        wait after it, until the timeout — reported as a page that never
        finished loading, which is true but useless.
        """
        if event.get("method") != "Page.javascriptDialogOpening":
            return
        self._next_id += 1
        try:
            self._send({"id": self._next_id, "method": "Page.handleJavaScriptDialog",
                        "params": {"accept": True},
                        "sessionId": event.get("sessionId")})
        except SetupError:
            pass

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             session_id: str = "", timeout: Optional[float] = None) -> Dict[str, Any]:
        """One protocol command, and its result. Raises SetupError on failure."""
        if self._closed:
            raise SetupError("this browser has already been closed")
        self._next_id += 1
        ident = self._next_id
        message: Dict[str, Any] = {"id": ident, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self._send(message)
        budget = timeout or self.timeout
        try:
            answer = self._pump(ident, time.time() + budget, budget) or {}
        except NoAnswer:
            # One more go at the same step, with a longer allowance. The
            # command is already with Chrome, so the retry is a longer wait for
            # its answer rather than a second copy of it — sending it again
            # would navigate twice or take a second screenshot. A Chrome that
            # died in the meantime still fails at once, on the poll in _receive.
            longer = budget * SLOW_RETRY_FACTOR
            try:
                answer = self._pump(ident, time.time() + longer, budget + longer) or {}
            except NoAnswer:
                raise NoAnswer("%s did not answer %s within %.0fs, waited on twice"
                               % (self._name(), method, budget + longer))
        if "error" in answer:
            error = answer["error"] or {}
            raise SetupError("%s failed: %s" % (method, error.get("message") or error))
        result = answer.get("result")
        return result if isinstance(result, dict) else {}

    def drain(self, seconds: float) -> None:
        """Read whatever Chrome has to say for a while, keeping the events.

        Used while a page settles: the messages that matter (a thrown
        exception) arrive unasked, and nothing collects them unless somebody
        reads the pipe.
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                # Returns on the next event and raises once the deadline is up,
                # so this blocks for the whole settle time and keeps what came.
                self._pump(None, deadline)
            except SetupError:
                return

    def wait_for(self, method: str, session_id: str = "",
                 timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """The next (or already-seen) event with this name, or None on timeout."""
        for event in self._events:
            if event.get("method") == method and (
                    not session_id or event.get("sessionId") == session_id):
                return event
        deadline = time.time() + (timeout or self.timeout)
        while True:
            try:
                event = self._pump(None, deadline)
            except SetupError:
                return None
            if not event:
                return None
            if event.get("method") == method and (
                    not session_id or event.get("sessionId") == session_id):
                return event

    def events(self, method: str = "", session_id: str = "") -> List[Dict[str, Any]]:
        return [e for e in self._events
                if (not method or e.get("method") == method)
                and (not session_id or e.get("sessionId") == session_id)]

    def forget_events(self) -> None:
        self._events = []

    # -- pages ------------------------------------------------------------
    def new_page(self) -> "PageTarget":
        """A fresh about:blank tab, attached, with its own session id."""
        target = self.call("Target.createTarget", {"url": "about:blank"}).get("targetId", "")
        session = self.call("Target.attachToTarget",
                            {"targetId": target, "flatten": True}).get("sessionId", "")
        if not session:
            raise SetupError("could not attach to a new tab")
        return PageTarget(self, target, session)

    # -- teardown ---------------------------------------------------------
    def _name(self) -> str:
        return os.path.basename(self.chrome) or "chrome"

    def _died(self) -> str:
        tail = ""
        try:
            self._log.flush()
            self._log.seek(0)
            text = self._log.read().decode("utf-8", "replace").strip()
            tail = text.splitlines()[-1] if text else ""
        except (OSError, ValueError):
            pass
        return "%s exited before answering%s" % (self._name(), (": " + tail) if tail else "")

    @staticmethod
    def _cleanup_fds(fds: List[int]) -> None:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def _discard_profile(self) -> None:
        try:
            self._log.close()
        except (OSError, ValueError):
            pass
        shutil.rmtree(self._profile, ignore_errors=True)

    def close(self) -> None:
        """Stop Chrome, whatever state it is in. Never raises.

        The group first, because Chrome's renderers and GPU helpers are in it
        and killing only the parent leaves them behind; then the child itself,
        because under duet's agent sandbox killpg raises EPERM and the group
        was never touched. A failure here must not lose a result that already
        came back.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._next_id += 1
            self._send({"id": self._next_id, "method": "Browser.close", "params": {}})
        except (SetupError, OSError):
            pass
        self._cleanup_fds([self._to_chrome_w, self._from_chrome_r])
        proc = getattr(self, "proc", None)
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, AttributeError):
                pass            # EPERM under a sandbox, ESRCH if it already went
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
        self._discard_profile()


class PageTarget:
    """One tab: the commands duet page needs, bound to its session."""

    def __init__(self, browser: Browser, target_id: str, session_id: str):
        self.browser = browser
        self.target_id = target_id
        self.session_id = session_id

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        return self.browser.call(method, params, session_id=self.session_id, timeout=timeout)

    def emulate(self, width: int, height: int, mobile: bool) -> None:
        """The exact viewport a visitor has — not the nearest window Chrome allows."""
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1,
            "mobile": mobile, "screenWidth": width, "screenHeight": height,
        })

    def evaluate(self, expression: str, timeout: Optional[float] = None) -> Any:
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout)
        details = result.get("exceptionDetails")
        if details:
            raise SetupError("the probe itself failed: %s"
                             % (details.get("text") or details))
        return (result.get("result") or {}).get("value")

    def exceptions(self) -> List[str]:
        """Uncaught exceptions and rejections this tab has reported, in order."""
        out: List[str] = []
        for event in self.browser.events("Runtime.exceptionThrown", self.session_id):
            details = (event.get("params") or {}).get("exceptionDetails") or {}
            exception = details.get("exception") or {}
            text = (exception.get("description") or exception.get("value")
                    or details.get("text") or "an exception with no message")
            first = str(text).strip().splitlines()[0]
            where = details.get("url") or ""
            line = details.get("lineNumber")
            if where and line is not None:
                first += " (%s:%s)" % (where.rsplit("/", 1)[-1], int(line) + 1)
            out.append(first)
        return out

    def close(self) -> None:
        try:
            self.browser.call("Target.closeTarget", {"targetId": self.target_id})
        except SetupError:
            pass
