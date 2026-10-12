"""The shared ground truth.

The two agents run in separate processes and cannot see each other's context.
The workspace is the only thing they both observe, so the orchestrator treats
it as the arbiter of fact: what changed, and whether the gate passes on it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from duet.adapters.base import human_duration
from duet.config import DEFAULT_GATE_TIMEOUT

SKIP_DIRS = {
    ".git", ".duet", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".tox",
    "dist", "build", ".next", "target", ".idea", ".vscode", ".DS_Store",
}
DIGEST_CHUNK_BYTES = 1_048_576
# A new file bigger than this is listed for the reviewer, never inlined: it is
# data or generated output, and pasting its head into a 12,000-character view
# would only crowd out the code.
NEW_FILE_INLINE_MAX = 200_000
# Fewer characters of a new file than this are not worth their header: when
# the view has no more room for a file, it is named as not shown instead.
NEW_FILE_MIN_SHOWN = 400

_DIFF_HEADER = re.compile(r"^diff --git ", re.MULTILINE)
# The new name first (a rename's section is about where the file is now), the
# old one for a deletion. A pure rename has no ---/+++ lines, only "rename to".
_DIFF_PATHS = (re.compile(r"^rename to (.+)$", re.MULTILINE),
               re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE),
               re.compile(r"^--- a/(.+)$", re.MULTILINE))


def _diff_sections(diff_text: str) -> List[Tuple[int, int, str]]:
    """(start, end, path) for each file's section of a `git diff`."""
    starts = [m.start() for m in _DIFF_HEADER.finditer(diff_text)]
    out: List[Tuple[int, int, str]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(diff_text)
        section = diff_text[start:end]
        # Only the header, before the first hunk: a body line can look like one.
        head_part = section.split("\n@@", 1)[0]
        found = next((m for m in (p.search(head_part) for p in _DIFF_PATHS) if m), None)
        if found:
            path = found.group(1)
        else:
            # No ---/+++ lines (a mode change, a binary file): "a/X b/X".
            head = section.split("\n", 1)[0][len("diff --git "):]
            path = head[2:2 + (len(head) - 5) // 2] if len(head) > 5 else head
        out.append((start, end, path.rstrip("\t")))
    return out


_FAILED_TEST = re.compile(r"^(?:FAILED|ERROR) +(\S+)", re.MULTILINE)


def failing_tests(output: str, limit: int = 10) -> List[str]:
    """The tests a run names as failed, when its output says so (pytest's
    "FAILED path::test" summary lines); otherwise an empty list."""
    names: List[str] = []
    for m in _FAILED_TEST.finditer(output or ""):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names[:limit]


def _fair_shares(needs: Sequence[int], room: int) -> List[int]:
    """Split `room` so nobody gets more than they need and the rest share equally.

    Smallest need first: each takes what it needs or an equal share of what is
    left, whichever is less, so what a small part leaves over goes to the big ones.
    """
    shares = [0] * len(needs)
    left = max(0, room)
    order = sorted(range(len(needs)), key=lambda i: needs[i])
    for n, i in enumerate(order):
        shares[i] = min(needs[i], left // (len(needs) - n))
        left -= shares[i]
    return shares


class PatchRejected(Exception):
    """A patch tried to leave the workspace or touch something protected."""


@dataclass
class GateResult:
    command: str
    ok: bool
    exit_code: int
    output: str
    skipped: bool = False
    # Killed by its time limit rather than finished. Not green — nothing was
    # shown to pass — but not a test failure either, and calling it "FAILED"
    # sends both agents hunting for a broken test that does not exist.
    timed_out: bool = False
    timeout: int = 0
    # Seconds the run took, kept with a pass in .duet/gate-cache.json.
    duration: float = 0.0
    # Set when this is a pass from an earlier session rather than a run made
    # now: when it happened, in words. Only ever the opening check — see
    # duet/gate_cache.py for why nothing is counted against it.
    remembered: str = ""
    # Set on a pass that came from re-running a failed gate over files nobody
    # had changed: the one-line warning that the earlier failure may have been
    # the machine, not the code. See Orchestrator.gate_for.
    flaky: str = ""

    def headline(self) -> str:
        """"PASSED", "FAILED (exit 1)", or that the gate never finished."""
        if self.ok and self.remembered:
            return "PASSED (%s)" % self.remembered_note()
        if self.ok and self.flaky:
            return "PASSED on a re-run — " + self.flaky
        if self.ok:
            return "PASSED"
        if self.timed_out:
            return "TIMED OUT — " + self.timeout_note()
        return "FAILED (exit %d)" % self.exit_code

    def remembered_note(self) -> str:
        return ("remembered from %s, same files, same command; --fresh-gate to rerun"
                % self.remembered)

    def timeout_note(self) -> str:
        return ("the gate ran out of time after %s; this is not a test failure"
                % human_duration(self.timeout))

    def problem(self) -> str:
        """Why this gate does not count as green, in the agents' words."""
        if self.timed_out:
            return (self.timeout_note() + ", but it does not count as green until "
                    "it finishes (the limit is set with --gate-timeout MINUTES)")
        return "the acceptance gate is failing"

    def render(self, limit: int = 3000) -> str:
        if self.skipped:
            return "No acceptance gate configured."
        head = self.headline()
        out = self.output.strip()
        if len(out) > limit:
            out = out[: limit // 2] + "\n...[trimmed]...\n" + out[-limit // 2 :]
        return "$ %s\n%s\n%s" % (self.command, head, out)


class Workspace:
    def __init__(self, root: str, gate: Optional[str] = None,
                 gate_timeout: int = DEFAULT_GATE_TIMEOUT):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.gate = gate
        self.gate_timeout = gate_timeout

    # -- identity ---------------------------------------------------------
    def resolve(self, rel_path: str) -> Path:
        """Resolve a peer-supplied path, refusing anything outside the root."""
        raw = str(rel_path).strip()
        if not raw:
            raise PatchRejected("empty path")
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(self.root)
            except ValueError:
                raise PatchRejected("absolute path outside the workspace: %s" % raw)
        target = (self.root / candidate).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise PatchRejected("path escapes the workspace: %s" % raw)
        parts = target.relative_to(self.root).parts
        if parts and parts[0] in (".git", ".duet"):
            raise PatchRejected("refusing to modify %s/" % parts[0])
        return target

    def tracked_files(self) -> List[Path]:
        """Every file that counts as workspace state, symlinks included.

        Symlinks are listed but never followed: retargeting one changes what the
        project does, so it has to change the state id too.
        """
        files: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if name == ".DS_Store":
                    continue
                path = Path(dirpath) / name
                if path.is_symlink() or path.is_file():
                    files.append(path)
        return files

    def digest(self) -> str:
        """A content hash of the workspace.

        Two DONE votes only count as agreement if they were cast against the
        same digest — otherwise an agent could approve a state the other one
        has already replaced.
        """
        h = hashlib.sha256()
        for path in self.tracked_files():
            rel = path.relative_to(self.root).as_posix()
            h.update(rel.encode("utf-8", "replace"))
            h.update(b"\0")
            try:
                if path.is_symlink():
                    h.update(b"symlink:")
                    h.update(os.readlink(str(path)).encode("utf-8", "replace"))
                else:
                    # Hashed in chunks rather than summarised by size: a large
                    # file edited in place keeps its length, and summarising by
                    # length let two different workspaces share a state id — so
                    # a sign-off, and a cached gate result, carried across a
                    # change neither agent had seen.
                    with path.open("rb") as fh:
                        while True:
                            chunk = fh.read(DIGEST_CHUNK_BYTES)
                            if not chunk:
                                break
                            h.update(chunk)
            except OSError as exc:
                h.update(("unreadable:%s" % exc).encode())
            h.update(b"\n")
        return h.hexdigest()[:16]

    def short_digest(self) -> str:
        return self.digest()[:8]

    def fingerprint(self) -> Dict[str, str]:
        """One hash per file, so a caller can tell *which* files moved.

        `digest()` answers "is this the same workspace"; this answers "and if
        not, what changed" — which is the difference between blaming an agent
        for a whole tree and naming the two files it touched.
        """
        out: Dict[str, str] = {}
        for path in self.tracked_files():
            rel = path.relative_to(self.root).as_posix()
            h = hashlib.sha256()
            try:
                if path.is_symlink():
                    h.update(b"symlink:")
                    h.update(os.readlink(str(path)).encode("utf-8", "replace"))
                else:
                    with path.open("rb") as fh:
                        while True:
                            chunk = fh.read(DIGEST_CHUNK_BYTES)
                            if not chunk:
                                break
                            h.update(chunk)
            except OSError as exc:
                h.update(("unreadable:%s" % exc).encode())
            out[rel] = h.hexdigest()[:16]
        return out

    # -- observation ------------------------------------------------------
    def _git(self, *args: str) -> Tuple[int, str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    @property
    def is_git_repo(self) -> bool:
        code, out = self._git("rev-parse", "--is-inside-work-tree")
        return code == 0 and out.strip() == "true"

    def untracked_files(self) -> List[str]:
        """Paths git has never seen. New work lives here and in no diff.

        duet's own `.duet/` bookkeeping is filtered out. A leftover session
        transcript is not part of anybody's change, and counting it as one made
        an unchanged working tree look like work.
        """
        if not self.is_git_repo:
            return []
        # -z, not lines: by default git escapes any path that is not plain ASCII
        # and wraps it in quotes, and an escaped name resolves to nothing on
        # disk — so the file reached a reviewer as a name with no body. The NUL
        # form is never quoted, and it survives spaces and newlines too.
        code, status = self._git("status", "--porcelain", "-z", "--untracked-files=all")
        if code != 0:
            return []
        paths = [entry[3:] for entry in status.split("\0") if entry.startswith("?? ")]
        return [p for p in paths if p.split("/")[0] not in (".duet", ".git")]

    TRIM_MARKER = "...[trimmed at"

    def diff_since(self, ref: str, limit: int = 12000) -> str:
        """Everything on this branch that `ref` does not have.

        Reviewing work you have already committed is the ordinary case — it is
        what "review my branch" means — and the working tree is empty by then.
        """
        if not self.is_git_repo:
            return "(not a git repository, so there is no %s to compare against)" % ref
        code, out = self._git("rev-parse", "--verify", "--quiet", "%s^{commit}" % ref)
        if code != 0:
            return "<no such ref: %s>" % ref
        parts: List[str] = []
        code, stat = self._git("--no-pager", "diff", "--stat", "%s...HEAD" % ref)
        if code == 0 and stat.strip():
            parts.append(stat.strip())
        code, body = self._git("--no-pager", "diff", "%s...HEAD" % ref)
        if code != 0:
            return "<could not diff against %s: %s>" % (ref, body.strip()[:200])
        if body.strip():
            parts.append(body.strip())
        if not parts:
            return "(nothing on this branch that %s does not already have)" % ref
        joined = "\n\n".join(parts)
        if len(joined) > limit:
            joined = joined[:limit] + "\n...[trimmed at %d chars]..." % limit
        return joined

    def _new_file_body(self, rel: str) -> Tuple[Optional[str], str]:
        """(text, "") for a new file worth inlining, else (None, why it is not)."""
        path = self.root / rel
        try:
            if path.is_symlink():
                return None, "symlink -> %s" % os.readlink(str(path))
            size = path.stat().st_size
            if size > NEW_FILE_INLINE_MAX:
                return None, "too large to inline"
            with path.open("rb") as fh:
                raw = fh.read(NEW_FILE_INLINE_MAX + 1)
        except OSError:
            return None, "unreadable"
        if b"\0" in raw[:8192]:
            return None, "binary"
        return raw.decode("utf-8", errors="replace"), ""

    def diff(self, limit: int = 12000, new_file_contents: bool = True) -> str:
        """What the peer needs to review: the change, not the whole repo.

        This deliberately does not run `git add -AN` to make untracked files
        show up in the diff. duet is a guest in someone's repository and
        staging their files behind their back is not its business; new files
        are read from disk instead.

        `git diff HEAD` never contains an untracked file, and a change whose
        main code is new files is the ordinary shape of a feature. So the view
        is built in this order, and only the last part is ever trimmed:

        1. every new file, by name and size — before anything that can be cut,
           so no budget ever hides that a file exists;
        2. which files are not shown in full, if any, so the reviewer knows
           exactly what to read rather than guessing where the view stopped;
        3. the diff of changed files and the text of the new ones, sharing what
           is left of `limit` — neither may starve the other.

        `new_file_contents=False` keeps the listing but leaves the text out,
        for a caller that sends the new files' contents its own way.
        """
        if not self.is_git_repo:
            return self.tree(limit=limit)

        diff_parts: List[str] = []
        code, out = self._git("--no-pager", "diff", "--stat", "HEAD")
        if code == 0 and out.strip():
            diff_parts.append(out.strip())
        code, body = self._git("--no-pager", "diff", "HEAD")
        if code == 0 and body.strip():
            diff_parts.append(body.strip())
        diff_text = "\n\n".join(diff_parts)

        new_files = self.untracked_files()
        if not diff_text and not new_files:
            return "(git: no changes against HEAD)"

        sizes: Dict[str, int] = {}
        bodies: Dict[str, str] = {}
        why_not: Dict[str, str] = {}
        for rel in new_files:
            try:
                sizes[rel] = (self.root / rel).lstat().st_size
            except OSError:
                sizes[rel] = -1
            if not new_file_contents:
                continue
            text, reason = self._new_file_body(rel)
            if text is None:
                why_not[rel] = reason
            else:
                bodies[rel] = text

        file_header = "--- NEW FILE %s ---\n"
        changed = [rel for _, _, rel in _diff_sections(diff_text)]
        listing_head = ("NEW FILES (untracked, so `git diff` never shows them; the text of "
                        "those that fit follows the diff below):" if new_file_contents else
                        "NEW FILES (untracked, so `git diff` never shows them):")
        listing_lines = [listing_head]
        for rel in new_files:
            listing_lines.append("  %8d  %s%s" % (sizes[rel], rel,
                                                  "  [%s, not shown]" % why_not[rel]
                                                  if rel in why_not else ""))
        listing = "\n".join(listing_lines) if new_files else ""

        def shares(inlined: Dict[str, str]) -> Tuple[int, Dict[str, int]]:
            # Headers, markers and the not-shown list cost characters too; they
            # are taken out of the budget up front, at the most they could need,
            # so the shares are shares of what can actually be shown and the
            # whole view stays inside `limit` whenever the listing does.
            fixed = (200 + len("\n...[trimmed at %d chars]..." % limit)
                     + sum(len(file_header % rel) + len(rel) + 80 for rel in inlined)
                     + sum(len(rel) + 3 for rel in changed + list(bodies) + list(why_not)))
            room = max(0, limit - len(listing) - fixed)
            # Fair shares: the diff and the new files split the room, and either
            # one's unused half goes to the other. Within the new files, the same
            # rule again — small files are shown whole, the big ones split the rest.
            if not inlined:
                return room, {}
            diff_room, files_room = _fair_shares(
                [len(diff_text), sum(map(len, inlined.values()))], room)
            return diff_room, dict(zip(inlined, _fair_shares(
                [len(b) for b in inlined.values()], files_room)))

        # A file whose share would be a scrap is not inlined at all — its
        # header and marker would only cost the others room — and is named as
        # not shown instead. Largest first, until every inlined file gets a
        # useful piece.
        inlined = dict(bodies)
        no_room: List[str] = []
        while True:
            diff_room, file_rooms = shares(inlined)
            starved = [rel for rel, text in inlined.items()
                       if file_rooms[rel] < min(len(text), NEW_FILE_MIN_SHOWN)]
            if not starved:
                break
            drop = max(starved, key=lambda rel: len(inlined[rel]))
            del inlined[drop]
            no_room.append(drop)

        not_in_full: List[str] = []
        shown_diff = diff_text
        if len(diff_text) > diff_room:
            shown_diff = (diff_text[:diff_room]
                          + "\n...[trimmed at %d chars]..." % diff_room)
            not_in_full.extend(rel for _, end, rel in _diff_sections(diff_text) if end > diff_room)
        shown_files = []
        for rel, text in inlined.items():
            allowed = file_rooms[rel]
            if len(text) > allowed:
                text = (text[:allowed]
                        + "\n...[trimmed at %d of %d chars — read %s for the rest]..."
                        % (allowed, len(text), rel))
                not_in_full.append(rel)
            shown_files.append((file_header % rel) + text)
        # Listed but never inlined counts as not shown in full, too.
        not_in_full.extend(sorted(no_room))
        not_in_full.extend(rel for rel in new_files if rel in why_not
                           and why_not[rel] != "binary" and not why_not[rel].startswith("symlink"))

        out: List[str] = []
        if listing:
            out.append(listing)
        if not_in_full:
            out.append("NOT SHOWN IN FULL — this view was trimmed to fit; read these "
                       "files yourself before judging them:\n"
                       + "\n".join("  %s" % rel for rel in not_in_full))
        if shown_diff:
            out.append(shown_diff)
        out.extend(shown_files)
        return "\n\n".join(out)

    def tree(self, limit: int = 12000, max_entries: int = 400) -> str:
        lines = []
        for path in self.tracked_files()[:max_entries]:
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            lines.append("%8d  %s" % (size, path.relative_to(self.root).as_posix()))
        if not lines:
            return "(workspace is empty)"
        body = "\n".join(lines)
        return body[:limit]

    def read(self, rel_path: str, limit: int = 20000) -> str:
        """The file's text, or a short description of why there is none.

        The answer goes straight into a prompt, so a failure has to read as
        prose. Callers that need to *decide* something use `read_or_none`: a
        file whose own contents look like one of these messages is otherwise
        indistinguishable from a missing one.
        """
        try:
            target = self.resolve(rel_path)
        except PatchRejected as exc:
            return "<%s>" % exc
        if not target.is_file():
            return "<no such file: %s>" % rel_path
        try:
            return target.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError as exc:
            return "<unreadable: %s>" % exc

    def read_or_none(self, rel_path: str, limit: int = 20000) -> Optional[str]:
        """The file's text, or None if it cannot be read. No prose in the answer."""
        try:
            target = self.resolve(rel_path)
        except PatchRejected:
            return None
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            return None

    # -- mutation ---------------------------------------------------------
    def apply_patches(self, patches: Sequence) -> List[str]:
        """Apply an agent's file writes. Returns one log line per patch."""
        log: List[str] = []
        for patch in patches:
            try:
                target = self.resolve(patch.path)
            except PatchRejected as exc:
                log.append("REJECTED %s: %s" % (patch.path, exc))
                continue
            rel = target.relative_to(self.root).as_posix()
            try:
                if patch.action == "delete":
                    if target.is_file():
                        target.unlink()
                        log.append("deleted %s" % rel)
                    else:
                        log.append("skipped delete %s (not present)" % rel)
                    continue
                existed = target.is_file()
                previous = target.read_text(encoding="utf-8", errors="replace") if existed else None
                if previous == patch.content:
                    log.append("unchanged %s" % rel)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(patch.content, encoding="utf-8")
                log.append("%s %s (%d bytes)" % ("wrote" if existed else "created", rel, len(patch.content)))
            except OSError as exc:
                log.append("FAILED %s: %s" % (rel, exc))
        return log

    # -- verification -----------------------------------------------------
    def run_gate(self) -> GateResult:
        """Run the acceptance command. Neither agent gets to skip this."""
        if not self.gate:
            return GateResult(command="", ok=True, exit_code=0, output="", skipped=True)
        started = time.monotonic()
        try:
            # Its own process group, so stopping the gate stops the test runner
            # under the shell too — killing only the shell left it running on.
            proc = subprocess.Popen(
                self.gate,
                cwd=str(self.root),
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "DUET_GATE": "1"},
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            return GateResult(command=self.gate, ok=False, exit_code=127, output=str(exc))
        try:
            stdout, stderr = proc.communicate(timeout=self.gate_timeout)
        except subprocess.TimeoutExpired:
            stop_process_group(proc)
            stdout, stderr = proc.communicate()
            # Keep what it printed before it was stopped: how far the run got
            # is the first thing anyone deciding on a longer limit wants to see.
            partial = (stdout or "") + (stderr or "")
            return GateResult(
                command=self.gate,
                ok=False,
                exit_code=124,
                output=partial,
                timed_out=True,
                timeout=self.gate_timeout,
            )
        except BaseException:
            # A stop signal (duet's handler raises) or Ctrl-C: the gate goes
            # down with the session instead of outliving it.
            stop_process_group(proc)
            raise
        output = (stdout or "") + (stderr or "")
        return GateResult(
            command=self.gate,
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            output=output,
            duration=time.monotonic() - started,
        )


def stop_process_group(proc: "subprocess.Popen[str]", grace: float = 3.0) -> None:
    """Stop a child started in its own session, and everything it started.

    SIGTERM to the group first so a test runner can clean up, SIGKILL if it is
    still there after `grace` seconds. Where the group cannot be signalled
    (a sandbox that refuses killpg) the child alone is stopped.

    Windows has no process groups to signal, and under `shell=True` the child
    is cmd.exe: stopping only it left the test runner it started running and
    holding the output pipes, so reading them after a timeout waited for the
    whole suite anyway. `taskkill /T` stops the tree.
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, AttributeError):
        # AttributeError: a "posix" os.name with no os.killpg at all —
        # exercised by the test suite, not a real platform, but the same
        # mistake as the Windows one this function exists to fix, so it
        # gets the same fallback rather than a crash.
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, AttributeError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def changed_paths(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Every path added, removed or rewritten between two fingerprints."""
    both = set(before) & set(after)
    return sorted(set(before) ^ set(after) | {p for p in both if before[p] != after[p]})


def quote(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(c) for c in command)
