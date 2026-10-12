"""Driving a real browser with nothing but the standard library.

A website usually has no test command, so a duet session on one has no gate —
two models agreeing by argument about CSS neither of them has seen rendered.
This is the half of the answer that talks to Chrome: find it, start it headless
on a throwaway profile, speak the DevTools protocol over a pipe (or, where
there is no pipe to be had, a WebSocket), and always kill it again. What to *check* once a page is rendered lives in page.py.

Why a pipe first: `--remote-debugging-pipe` hands Chrome file descriptors 3
and 4 and speaks the same protocol over them, NUL-terminated, which json and
os.read can do between them — no port, so nothing else on the machine can
connect. Windows cannot pass a child descriptors 3 and 4, so there Chrome gets
`--remote-debugging-port=0` instead, and duet speaks the same protocol over a
WebSocket (duet/websocket.py, standard library only). DUET_CHROME_TRANSPORT
picks either one anywhere the pipe works, which is how both are tested.

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
import ntpath
import os
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from duet.websocket import Closed, WebSocket, WebSocketError

try:
    import fcntl
except ImportError:             # pragma: no cover - Windows, which uses the WebSocket
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

# How duet talks to Chrome (see choose_transport), and the flag for each.
TRANSPORT_ENV = "DUET_CHROME_TRANSPORT"
PIPE = "pipe"
WEBSOCKET = "websocket"
TRANSPORTS = (PIPE, WEBSOCKET)
PIPE_FLAG = "--remote-debugging-pipe"
# Port 0: Chrome picks a free one and writes it to ACTIVE_PORT_FILE.
PORT_FLAG = "--remote-debugging-port=0"
ACTIVE_PORT_FILE = "DevToolsActivePort"

# Flags that make a run reproducible and leave no trace, on top of the
# transport's own: they exist to stop a fresh profile asking a question nobody
# can answer in a headless browser.
BASE_FLAGS = (
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


def _windows() -> bool:
    return sys.platform.startswith("win")


def choose_transport(env: Optional[Dict[str, str]] = None,
                     windows: Optional[bool] = None) -> str:
    """Which way to talk to Chrome: the pipe, or a WebSocket on a local port.

    The pipe where it can work, because it needs no port and nothing else on
    the machine can connect to it; the WebSocket on Windows, which cannot hand
    a child file descriptors 3 and 4. DUET_CHROME_TRANSPORT overrides either
    way, so the WebSocket can be tested where the pipe is the default — but a
    pipe asked for on Windows, or a name that is neither, is a setup fault
    rather than something quietly swapped for the other.
    """
    environ = os.environ if env is None else env
    windows = _windows() if windows is None else windows
    chosen = (environ.get(TRANSPORT_ENV) or "").strip().lower()
    if not chosen:
        return WEBSOCKET if windows else PIPE
    if chosen not in TRANSPORTS:
        raise SetupError("%s is set to %r; it can be %s, or unset."
                         % (TRANSPORT_ENV, chosen, " or ".join(TRANSPORTS)))
    if chosen == PIPE and windows:
        raise SetupError("%s=pipe cannot work on Windows: the pipe transport hands "
                         "Chrome file descriptors 3 and 4, which Windows cannot pass "
                         "to a child. Unset it to use the WebSocket transport."
                         % TRANSPORT_ENV)
    return chosen


def unsupported_platform() -> Optional[str]:
    """Why `duet page` cannot run as configured here, or None.

    Asked before anything starts, so a transport that cannot work says so as a
    setup fault instead of failing halfway in a way that reads like a broken page.
    """
    try:
        choose_transport()
    except SetupError as exc:
        return str(exc)
    return None


def read_active_port(path: str) -> Optional[Tuple[int, str]]:
    """(port, browser endpoint path) from a DevToolsActivePort file, or None
    while it is missing or only half written."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].strip().isdigit() or not lines[1].strip().startswith("/"):
        return None
    port = int(lines[0].strip())
    if not 0 < port < 65536:
        return None
    return port, lines[1].strip()


def windows_install_paths(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Where Chrome and Chromium install themselves on Windows.

    Per machine under Program Files (either one, by the installer's bitness),
    per user under LocalAppData. Read from the environment, because neither
    folder is at the same place on every machine.
    """
    environ = os.environ if env is None else env
    paths = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LocalAppData"):
        base = (environ.get(var) or environ.get(var.upper()) or "").strip()
        if not base:
            continue
        for vendor in (("Google", "Chrome"), ("Google", "Chrome SxS"), ("Chromium",)):
            path = ntpath.join(base, *vendor)
            path = ntpath.join(path, "Application", "chrome.exe")
            if path not in paths:
                paths.append(path)
    return paths


def find_chrome(env: Optional[Dict[str, str]] = None, windows: Optional[bool] = None) -> str:
    """The browser to drive: $DUET_CHROME, then PATH, then where it installs.

    Raises SetupError — never a page fault — when there is none, and when
    DUET_CHROME points at something that is not there, because silently
    ignoring an explicit choice is how you end up debugging the wrong browser.
    """
    environ = os.environ if env is None else env
    windows = _windows() if windows is None else windows
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
    for path in windows_install_paths(environ) if windows else INSTALL_PATHS:
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
                 timeout: float = CALL_TIMEOUT, extra_flags: Optional[List[str]] = None,
                 transport: Optional[str] = None):
        self.transport = transport or choose_transport()
        if self.transport not in TRANSPORTS:
            raise SetupError("unknown DevTools transport %r" % self.transport)
        self.chrome = chrome or find_chrome()
        self.timeout = timeout
        self._next_id = 0
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._events: List[Dict[str, Any]] = []
        self._closed = False
        self._link: Optional[Any] = None
        self._profile = tempfile.mkdtemp(prefix="duet-chrome-")
        self._log = open(os.path.join(self._profile, "chrome.log"), "w+b")

        flags = list(BASE_FLAGS) + ["--user-data-dir=%s" % self._profile]
        if not allow_network:
            flags.append(OFFLINE_FLAG)
        flags += list(extra_flags or [])
        started = time.time()
        if self.transport == PIPE:
            self._start_over_pipe(flags)
        else:
            self._start_over_websocket(flags, started)
        try:
            self.version = self.call(
                "Browser.getVersion",
                timeout=max(self.timeout, START_TIMEOUT - (time.time() - started), 1.0)
            ).get("product", "")
        except SetupError:
            self.close()
            raise

    def _start_over_pipe(self, flags: List[str]) -> None:
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()

        def child() -> None:               # pragma: no cover - runs post-fork
            _dup_onto(to_chrome_r, 3)
            _dup_onto(from_chrome_w, 4)

        try:
            self.proc = subprocess.Popen(
                [self.chrome, PIPE_FLAG] + flags,
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
            self._cleanup_fds([to_chrome_r, from_chrome_w, to_chrome_w, from_chrome_r])
            self._discard_profile()
            raise SetupError("could not start %s: %s" % (self.chrome, exc))
        self._cleanup_fds([to_chrome_r, from_chrome_w])
        self._link = _PipeLink(to_chrome_w, from_chrome_r)

    def _start_over_websocket(self, flags: List[str], started: float) -> None:
        """Port 0 lets Chrome pick a free port; it writes the one it picked,
        and the browser endpoint's path, to DevToolsActivePort in the profile."""
        try:
            self.proc = subprocess.Popen(
                [self.chrome, PORT_FLAG] + flags,
                stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
                close_fds=True,
                # A process group to kill on the way out, where there is one.
                start_new_session=not _windows(),
            )
        except OSError as exc:
            self._discard_profile()
            raise SetupError("could not start %s: %s" % (self.chrome, exc))
        try:
            port, path = self._wait_for_port(started + max(self.timeout, START_TIMEOUT))
            self._link = _SocketLink(WebSocket.connect("127.0.0.1", port, path,
                                                       timeout=self.timeout))
        except (OSError, WebSocketError) as exc:
            self.close()
            raise SetupError("could not connect to %s's DevTools port: %s"
                             % (self._name(), exc))
        except SetupError:
            self.close()
            raise

    def _wait_for_port(self, deadline: float) -> Tuple[int, str]:
        marker = os.path.join(self._profile, ACTIVE_PORT_FILE)
        while True:
            found = read_active_port(marker)
            if found:
                return found
            if self.proc.poll() is not None:
                raise SetupError(self._died())
            if time.time() >= deadline:
                raise NoAnswer("%s did not report a DevTools port within %.0fs"
                               % (self._name(), max(self.timeout, START_TIMEOUT)))
            time.sleep(0.05)

    # -- context manager --------------------------------------------------
    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport --------------------------------------------------------
    def _send(self, message: Dict[str, Any]) -> None:
        if self._link is None:
            raise SetupError("%s is not connected" % self._name())
        try:
            self._link.send(json.dumps(message))
        except (OSError, EOFError, WebSocketError) as exc:
            raise SetupError("%s stopped listening: %s" % (self._name(), exc))

    def _receive(self, deadline: float, budget: Optional[float] = None) -> Dict[str, Any]:
        if self._link is None:
            raise SetupError("%s is not connected" % self._name())
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise NoAnswer("%s did not answer within %.0fs"
                               % (self._name(), budget or self.timeout))
            try:
                raw = self._link.read(min(0.25, left))
            except EOFError:
                raise SetupError(self._died())
            except (OSError, ValueError, WebSocketError) as exc:
                raise SetupError("%s %s broke: %s" % (self._name(), self.transport, exc))
            if raw is not None:
                break
            if self.proc.poll() is not None:
                raise SetupError(self._died())
        try:
            message = json.loads(raw)
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
        if self._link is not None:
            self._link.close()
        proc = getattr(self, "proc", None)
        if proc is not None:
            if _windows() and proc.poll() is None:
                # No process group there: the tree is Chrome's helpers.
                try:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=30)
                except (OSError, subprocess.SubprocessError):
                    pass
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


class _PipeLink:
    """DevTools over --remote-debugging-pipe: NUL-terminated JSON on two fds."""

    def __init__(self, to_chrome: int, from_chrome: int):
        self.to_chrome = to_chrome
        self.from_chrome = from_chrome
        self._buf = b""

    def send(self, text: str) -> None:
        raw = text.encode("utf-8") + b"\0"
        while raw:
            written = os.write(self.to_chrome, raw)
            raw = raw[written:]

    def read(self, timeout: float) -> Optional[str]:
        """One message, None if none is whole within `timeout`; EOFError at the end."""
        if b"\0" not in self._buf:
            ready, _, _ = select.select([self.from_chrome], [], [], timeout)
            if not ready:
                return None
            chunk = os.read(self.from_chrome, 1 << 16)
            if not chunk:
                raise EOFError()
            self._buf += chunk
            if b"\0" not in self._buf:
                return None
        raw, _, self._buf = self._buf.partition(b"\0")
        return raw.decode("utf-8", "replace")

    def close(self) -> None:
        Browser._cleanup_fds([self.to_chrome, self.from_chrome])


class _SocketLink:
    """DevTools over --remote-debugging-port: the same JSON, one per text frame."""

    def __init__(self, ws: WebSocket):
        self.ws = ws

    def send(self, text: str) -> None:
        self.ws.send_text(text)

    def read(self, timeout: float) -> Optional[str]:
        try:
            return self.ws.receive(timeout)
        except Closed:
            raise EOFError()

    def close(self) -> None:
        self.ws.close()


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
