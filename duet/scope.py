"""Whether a task needs files outside the workspace — checked before round 1.

The agents may only touch files inside the workspace. A session started in a
subfolder of a repository (`-C repo/studio`) whose task names a file beside it
(`../deploy/.env.example`) cannot be done there, and both agents find that out
the slow way: they spend their rounds being refused, then vote BLOCKED because
granting access is the user's call. Two of four real sessions on 0.19.1 ended
exactly so. Naming the problem before anything runs costs nothing.

What counts is deliberately narrow, so a task is not refused for words that
only look like paths: a mention counts only when it resolves to something that
exists, outside the workspace and not inside it. Relative to the workspace for
everything; for a bare `a/b` also relative to the enclosing git repository's
top level, since that is how people name files in the repository they are in.
Absolute paths count only inside that repository — `/usr/bin/python` is a tool
the task uses, not a file it needs to edit.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# What separates one mention from the next: whitespace, quotes, backticks,
# brackets and the punctuation prose wraps a path in. A backticked path, a
# quoted one and the target of a markdown link all come out as plain tokens.
_SPLIT = re.compile(r"[\s()<>\"'`,;\[\]{}|]+")
# A trailing `:12` or `:12:5` is a line (and column) in that file.
_LINE = re.compile(r"(:\d+)+$")
_GLOB = re.compile(r"[*?]")


def git_top(root: str) -> Optional[str]:
    """The top level of the git repository `root` is in, or None."""
    try:
        proc = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    top = proc.stdout.strip()
    if proc.returncode != 0 or not top:
        return None
    return str(Path(top).resolve())


def mentions(text: str) -> List[str]:
    """Every word of `text` that could be a path, cleaned of the prose around it.

    Over-collecting is harmless here: a mention only matters once it resolves
    to something that exists, so `e.g.`, `v1.2` and `node.js` fall away there.
    URLs are dropped now, because `https://x/y` would otherwise read as a path.
    """
    found: List[str] = []
    for token in _SPLIT.split(text or ""):
        if not token or "://" in token or token.startswith(("www.", "mailto:")):
            continue
        token = _LINE.sub("", token.rstrip(".:!?"))
        if not token or token in found:
            continue
        if "/" in token or os.sep in token or "." in token or _GLOB.search(token):
            found.append(token)
    return found


def _inside(path: str, folder: str) -> bool:
    path, folder = os.path.normcase(path), os.path.normcase(folder)
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # different drives on Windows
        return False


def _existing(base: str, mention: str) -> List[str]:
    """What `mention` names relative to `base`: the paths that exist, normalised."""
    joined = os.path.normpath(os.path.join(base, mention))
    if _GLOB.search(mention):
        return sorted(os.path.normpath(p) for p in glob.glob(joined))
    return [joined] if os.path.exists(joined) else []


def outside(text: str, root: str, top: Optional[str] = None) -> List[Tuple[str, str]]:
    """(mention, path) for each mention in `text` that is out of scope of `root`.

    `root` is the resolved workspace; `top` the git top level it is inside, if
    any. A mention that names something inside the workspace is in scope even
    when the same words also name something at the top level.
    """
    out: List[Tuple[str, str]] = []
    seen = set()
    for mention in mentions(text):
        name = os.path.basename(os.path.normpath(mention.replace("\\", "/")))
        if name in ("", ".", ".."):
            continue   # `../` alone is a direction, not a file the task needs
        if ".git" in re.split(r"[\\/]", mention):
            continue   # git's own files: no agent may touch them from any workspace
        expanded = os.path.expanduser(mention)
        if os.path.isabs(expanded):
            if not top:
                continue
            hits = [p for p in _existing("", expanded) if _inside(p, top)]
        else:
            if any(_inside(p, root) for p in _existing(root, mention)):
                continue
            hits = _existing(root, mention)
            bare = not mention.startswith(("./", "../", ".\\", "..\\"))
            if not hits and bare and top and top != root and ("/" in mention or "\\" in mention):
                hits = _existing(top, mention)
        hits = [p for p in hits if not _inside(p, root)]
        if hits and hits[0] not in seen:
            seen.add(hits[0])
            out.append((mention, hits[0]))
    return out


def suggested_root(root: str, paths: Sequence[str]) -> str:
    """The nearest directory holding the workspace and every one of `paths`."""
    return os.path.commonpath([root] + list(paths))


def with_root(argv: Sequence[str], new_root: str) -> List[str]:
    """`argv` (a duet command line, subcommand first) with `-C` set to `new_root`."""
    out: List[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in ("-C", "--root"):
            skip = True
            continue
        if arg.startswith("--root=") or (arg.startswith("-C") and len(arg) > 2):
            continue
        out.append(arg)
    return out[:1] + ["-C", new_root] + out[1:]
