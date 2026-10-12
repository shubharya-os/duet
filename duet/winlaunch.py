"""Starting a program the way Windows needs it started.

`npm install -g` does not put a program on PATH on Windows: it puts a batch
file, `claude.cmd` or `codex.cmd`, that runs node on the package's script.
CreateProcess cannot execute a batch file, so `subprocess.run(["claude", ...])`
fails with "[WinError 193] %1 is not a valid Win32 application" — or, when the
bare name is given, does not find it at all, because only shutil.which reads
PATHEXT.

So on Windows a command is resolved with shutil.which, and a `.cmd`/`.bat` it
lands on is not executed as is:

- a shim whose last line runs a program on a script with `%*` (what npm writes,
  and what the test suite's fakes write) is read, and that program is started
  directly on that script. No cmd.exe is involved, so an argument may hold any
  character, newlines included — and a prompt always holds newlines.
- any other batch file goes through `cmd.exe /d /s /c` with every argument
  escaped for cmd's own parser. cmd cannot carry a newline inside an argument,
  so a multi-line argument is refused with a clear error rather than cut short.

Everywhere else the command is returned unchanged.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from typing import List, Optional, Sequence, Union

WINDOWS = os.name == "nt"

BATCH_SUFFIXES = (".cmd", ".bat")

# The characters cmd.exe treats specially; each is escaped with a caret.
_CMD_META = re.compile(r'([()\][%!^"`<>&|;, *?])')

# "<program>" ["<script>"] %*  — the line a shim ends on. Quoted or bare tokens.
_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def _expand(token: str, folder: str) -> str:
    """Resolve the few variables npm's shims use for their own folder."""
    for name in ("%~dp0", "%dp0%"):
        if token.startswith(name):
            rest = token[len(name):].lstrip("\\/")
            return os.path.join(folder, rest) if rest else folder
    if token == "%_prog%":
        local = os.path.join(folder, "node.exe")
        return local if os.path.isfile(local) else (shutil.which("node") or "node")
    return token


def read_shim(path: str) -> Optional[List[str]]:
    """The argv a shim forwards `%*` to, or None if it is not that simple.

    Only the last line that mentions `%*` is read; everything after `%*` on it
    must be absent, so a shim that does more than forward is not second-guessed.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    line = next((l for l in reversed(lines) if "%*" in l), None)
    if line is None:
        return None
    head, _, tail = line.partition("%*")
    if tail.strip():
        return None
    # npm's shim reaches its command after `... || title %COMSPEC% & `
    head = head.rsplit("& ", 1)[-1].strip().lstrip("@")
    folder = os.path.dirname(os.path.abspath(path))
    tokens = [m.group(1) if m.group(1) is not None else m.group(2) for m in _TOKEN.finditer(head)]
    tokens = [_expand(t, folder) for t in tokens if t]
    if not tokens or any("%" in t for t in tokens):
        return None
    return tokens


def quote_for_cmd(arg: str, double: bool = True) -> str:
    """One argument, quoted for the program and then escaped for cmd.exe.

    The quoting follows the Microsoft C runtime rules (backslashes doubled
    before a quote and at the end); the carets protect it from cmd's own
    parsing. A batch file that forwards `%*` to another program goes through
    cmd twice, hence `double`.
    """
    if "\n" in arg or "\r" in arg:
        raise OSError("a batch file cannot be given an argument that spans lines: %r" % arg[:60])
    arg = re.sub(r'(\\*)"', r'\1\1\\"', arg)
    arg = re.sub(r'(\\*)$', r'\1\1', arg)
    arg = '"%s"' % arg
    arg = _CMD_META.sub(r'^\1', arg)
    if double:
        arg = _CMD_META.sub(r'^\1', arg)
    return arg


def quote_for_shell(path: str, windows: Optional[bool] = None) -> str:
    """One path, quoted for the shell duet's own command strings run through.

    `shlex.quote` wraps a path in single quotes the moment it holds a
    character outside its POSIX-safe set — and a backslash, which every
    Windows path has, is one of them. cmd.exe does not strip single quotes as
    quoting at all, so a gate built that way tried to run a program literally
    named `'C:\\Users\\...\\pytest.exe'`, quote marks included, and failed to
    find it. cmd.exe only needs quoting when a path holds a space, and only
    understands double quotes for it.
    """
    if WINDOWS if windows is None else windows:
        return '"%s"' % path if " " in path else path
    return shlex.quote(path)


def command(argv: Sequence[str], windows: Optional[bool] = None) -> Union[List[str], str]:
    """`argv` in a form subprocess can start on this platform.

    Returns a list except for the cmd.exe fallback, which has to be one command
    line passed to CreateProcess verbatim (a list would be re-quoted by
    subprocess.list2cmdline, which cmd.exe does not understand).
    """
    argv = [str(a) for a in argv]
    if not (WINDOWS if windows is None else windows) or not argv:
        return argv
    program = argv[0]
    found = shutil.which(program) or program
    if not found.lower().endswith(BATCH_SUFFIXES):
        return [found] + argv[1:]
    forwarded = read_shim(found)
    if forwarded:
        return forwarded + argv[1:]
    shell = os.environ.get("COMSPEC") or "cmd.exe"
    inner = " ".join([_CMD_META.sub(r'^\1', found)] + [quote_for_cmd(a) for a in argv[1:]])
    return '"%s" /d /s /c "%s"' % (shell, inner)
