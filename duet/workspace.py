"""The shared ground truth.

The two agents run in separate processes and cannot see each other's context.
The workspace is the only thing they both observe, so the orchestrator treats
it as the arbiter of fact: what changed, and whether the gate passes on it.
"""

from __future__ import annotations

import hashlib
import os
import shlex
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

    def headline(self) -> str:
        """"PASSED", "FAILED (exit 1)", or that the gate never finished."""
        if self.ok and self.remembered:
            return "PASSED (%s)" % self.remembered_note()
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
                text=True,
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

    def diff(self, limit: int = 12000) -> str:
        """What the peer needs to review: the change, not the whole repo.

        This deliberately does not run `git add -AN` to make untracked files
        show up in the diff. duet is a guest in someone's repository and
        staging their files behind their back is not its business; new files
        are listed separately instead.
        """
        if not self.is_git_repo:
            return self.tree(limit=limit)

        parts: List[str] = []
        code, out = self._git("--no-pager", "diff", "--stat", "HEAD")
        if code == 0 and out.strip():
            parts.append(out.strip())
        code, body = self._git("--no-pager", "diff", "HEAD")
        if code == 0 and body.strip():
            parts.append(body.strip())

        new_files = self.untracked_files()
        if new_files:
            listing = ["NEW FILES (untracked, not shown in the diff above):"]
            for rel in new_files[:80]:
                try:
                    size = (self.root / rel).stat().st_size
                except OSError:
                    size = -1
                listing.append("  %8d  %s" % (size, rel))
            if len(new_files) > 80:
                listing.append("  ...and %d more" % (len(new_files) - 80))
            parts.append("\n".join(listing))

        if not parts:
            return "(git: no changes against HEAD)"
        joined = "\n\n".join(parts)
        if len(joined) > limit:
            joined = joined[:limit] + "\n...[trimmed at %d chars]..." % limit
        return joined

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
            proc = subprocess.run(
                self.gate,
                cwd=str(self.root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.gate_timeout,
                env={**os.environ, "DUET_GATE": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            # Keep what it printed before it was stopped: how far the run got
            # is the first thing anyone deciding on a longer limit wants to see.
            partial = "".join(
                part.decode("utf-8", "replace") if isinstance(part, bytes) else part
                for part in (exc.stdout, exc.stderr) if part
            )
            return GateResult(
                command=self.gate,
                ok=False,
                exit_code=124,
                output=partial,
                timed_out=True,
                timeout=self.gate_timeout,
            )
        except OSError as exc:
            return GateResult(command=self.gate, ok=False, exit_code=127, output=str(exc))
        output = (proc.stdout or "") + (proc.stderr or "")
        return GateResult(
            command=self.gate,
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            output=output,
            duration=time.monotonic() - started,
        )


def changed_paths(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Every path added, removed or rewritten between two fingerprints."""
    both = set(before) & set(after)
    return sorted(set(before) ^ set(after) | {p for p in both if before[p] != after[p]})


def quote(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(c) for c in command)
