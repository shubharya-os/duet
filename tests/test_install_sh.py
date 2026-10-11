"""install.sh, run for real.

It is piped straight into `sh` on a machine we have never seen, so two things
have to hold: it must parse under whatever `sh` is there (dash on Debian,
busybox on Alpine — not bash), and it must not report an install it has not
got. Both are checked by running it, because reading it is what missed the
last four of these.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"

# The POSIX shells worth trying, plus the strict ones. Missing ones are skipped
# rather than failed: this suite runs on machines that have only bash.
SHELLS = ["sh", "dash", "posh", "ksh", "bash", "zsh", "busybox"]


def _available():
    """Shells that are present *and* can actually run.

    A binary for the wrong architecture is on PATH, executable, and useless —
    this machine has an x86 /usr/local/bin/zsh that raises OSError on exec. The
    same 'present but unusable' shape this project keeps finding in the wild,
    so the discovery here has to run the thing rather than trust which().
    """
    found = []
    for name in SHELLS:
        path = shutil.which(name)
        if not path:
            continue
        try:
            proc = subprocess.run([path, '-c', 'exit 0'], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            found.append((name, path))
    return found


@pytest.mark.parametrize("name,path", _available(), ids=lambda v: v if isinstance(v, str) else "")
def test_install_sh_parses(name, path):
    argv = [path, "sh", "-n", str(INSTALL)] if name == "busybox" else [path, "-n", str(INSTALL)]
    proc = subprocess.run(argv, capture_output=True, text=True)
    assert proc.returncode == 0, "%s cannot parse install.sh:\n%s" % (name, proc.stderr)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_install_sh_passes_shellcheck_as_posix_sh():
    proc = subprocess.run(["shellcheck", "--shell=sh", str(INSTALL)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


BASHISMS = [
    ("[[", "`[[` is a bash keyword; use `[`"),
    ("function ", "`function name()` is a bashism; use `name()`"),
    ("local ", "`local` is not in POSIX sh"),
    ("source ", "`source` is a bashism; use `.`"),
    ("$'", "$'...' ANSI-C quoting is a bashism"),
    ("+=", "`+=` on a variable is a bashism"),
    ("==", "`==` inside `[` is a bashism; use `=`"),
]


def test_install_sh_has_no_bashisms():
    """The parse check above only catches syntax errors — dash accepts `[ a ==
    b ]` at parse time and then fails at runtime. This catches the rest, and it
    runs even on a machine with nothing but bash."""
    text = INSTALL.read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                     for line in text.splitlines())
    for token, why in BASHISMS:
        assert token not in code, why


# --------------------------------------------------------------------------
# running it against a fake Python, on a machine with no pipx and a PEP 668 pip

FAKE_PY = """#!/bin/sh
# Stands in for python3. It models the parts of the real thing the installer
# depends on, and no more:
#   * the >=3.9 check passes
#   * `pip install --user` is refused the way an externally-managed (PEP 668)
#     interpreter refuses it, unless FAKE_PIP_USER_OK says otherwise
#   * `venv` builds a directory with an interpreter in it and nothing else —
#     it is pip, not venv, that writes the `duet` console script, which is why
#     a pip that declines to act leaves a venv with no duet in it
#   * FAKE_PIP_ALREADY_SATISFIED makes a plain (non---upgrade) install of the
#     repo exit 0 without writing anything, which is what pip does when it
#     thinks the package is already there
case "$1" in
  -c) exit 0 ;;
  -m)
    case "$2" in
      site) printf '%s\\n' "$FAKE_USER_BASE"; exit 0 ;;
      venv)
        mkdir -p "$3/bin" || exit 1
        cp "$0" "$3/bin/python" || exit 1
        chmod +x "$3/bin/python"
        exit 0 ;;
      pip)
        dest="$(dirname "$0")"
        target=""
        upgrade=""
        for a in "$@"; do
          case "$a" in
            --user)
              if [ -z "$FAKE_PIP_USER_OK" ]; then exit 1; fi
              dest="$FAKE_USER_BASE/bin" ;;
            --upgrade) upgrade=1 ;;
            git+*) target="$a" ;;
          esac
        done
        if [ -z "$target" ]; then exit 0; fi
        if [ -n "$FAKE_PIP_ALREADY_SATISFIED" ] && [ -z "$upgrade" ]; then
          exit 0
        fi
        mkdir -p "$dest" || exit 1
        cp "$FAKE_DUET" "$dest/duet" || exit 1
        chmod +x "$dest/duet"
        exit 0 ;;
    esac ;;
esac
exit 1
"""

WORKING_DUET = "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'duet 0.1.0'; exit 0; fi\nexit 0\n"
BROKEN_DUET = "#!/bin/sh\necho 'ImportError: no module named duet' >&2\nexit 1\n"


def _sandbox(tmp_path, duet_body):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    duet_stub = tmp_path / "duet-stub"
    duet_stub.write_text(duet_body)
    duet_stub.chmod(0o755)

    py = bin_dir / "python3"
    py.write_text(FAKE_PY)
    py.chmod(0o755)

    env = {
        "PATH": "%s:/usr/bin:/bin" % bin_dir,     # no pipx, no real duet
        "HOME": str(home),
        "FAKE_DUET": str(duet_stub),
        "FAKE_USER_BASE": str(home / ".local"),
        "DUET_HOME": str(home / ".duet"),
    }
    return home, env


# `/bin/sh` is bash in POSIX mode on macOS and dash on Debian, and they differ
# at runtime, not just at parse time. Run the whole script under each one that
# is here, so a bashism that only bites on Alpine or Debian is caught locally
# and not only on the one CI leg that happens to have that shell.
POSIX_SHELLS = [p for p in (shutil.which(n) for n in ("sh", "dash", "posh", "ash")) if p]
POSIX_SHELLS = sorted(set(POSIX_SHELLS))


@pytest.fixture(params=POSIX_SHELLS, ids=lambda p: Path(p).name)
def shell(request):
    return request.param


def _run(env, shell="/bin/sh"):
    # A new session has no controlling terminal. stdin from /dev/null is not
    # enough: install.sh reopens /dev/tty when there is one, so run from a
    # terminal (as duet runs its gate) these tests saw a human to ask and took
    # the interactive path they were written to rule out.
    return subprocess.run([shell, str(INSTALL)], capture_output=True, text=True,
                          env=env, cwd=str(env["HOME"]), timeout=120, stdin=subprocess.DEVNULL,
                          start_new_session=True)


def test_install_sh_reaches_a_working_duet_through_the_venv_fallback(tmp_path, shell):
    home, env = _sandbox(tmp_path, WORKING_DUET)
    proc = _run(env, shell)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Installed." in proc.stdout
    installed = home / ".duet" / "venv" / "bin" / "duet"
    assert installed.is_file()

    # Piped from curl there is no terminal, so the script cannot run `duet
    # setup` and hands the user the command instead. That handoff is the whole
    # remaining path to a working /duet, so it has to name the duet that was
    # actually installed — and `setup` has to still be a real subcommand.
    assert '"%s" setup' % installed in proc.stdout, proc.stdout
    assert "setup" in subprocess.run(
        [sys.executable, "-m", "duet", "--help"], capture_output=True, text=True,
        cwd=str(REPO), timeout=60).stdout


def test_install_sh_does_not_say_installed_when_duet_cannot_run(tmp_path, shell):
    """It used to accept anything executable at the end of the install. A duet
    that exits non-zero the moment you call it — a half-finished earlier
    install, a venv whose interpreter was upgraded out from under it — passed
    `[ -x ]` and the script printed "Installed." over it."""
    home, env = _sandbox(tmp_path, BROKEN_DUET)
    proc = _run(env, shell)
    assert proc.returncode != 0, proc.stdout
    assert "Installed." not in proc.stdout
    assert "will not run" in proc.stderr
    assert "rm -rf" in proc.stderr          # names the fix, not just the problem


def test_install_sh_creates_local_bin_when_it_is_on_path_but_absent(tmp_path, shell):
    """Plenty of shell profiles put ~/.local/bin on PATH unconditionally, so it
    is on PATH and does not exist. Skipping the symlink there told the user to
    add a directory to a PATH that already had it."""
    home, env = _sandbox(tmp_path, WORKING_DUET)
    shutil.rmtree(home / ".local")
    env["PATH"] = "%s:%s" % (env["PATH"], home / ".local" / "bin")

    proc = _run(env, shell)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    link = home / ".local" / "bin" / "duet"
    assert link.is_symlink(), proc.stdout
    assert "not on your PATH" not in proc.stdout


def test_install_sh_does_not_link_a_duet_that_will_not_run(tmp_path, shell):
    """The symlink is only an improvement if what it points at works."""
    home, env = _sandbox(tmp_path, BROKEN_DUET)
    env["PATH"] = "%s:%s" % (env["PATH"], home / ".local" / "bin")
    proc = _run(env, shell)
    assert proc.returncode != 0
    assert not (home / ".local" / "bin" / "duet").exists()


def test_install_sh_warns_when_an_older_duet_shadows_the_new_one(tmp_path, shell):
    """The PATH check asked whether *a* duet answers `command -v`, not whether
    the one that answers is the one just installed. An older install earlier on
    PATH answers it perfectly well, so the script said nothing — and every
    `duet` typed afterwards was the old one. Silence there is the same lie as a
    green tick: the install is reported as finished and is not usable."""
    stale_dir = tmp_path / "old"
    stale_dir.mkdir()
    stale = stale_dir / "duet"
    stale.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'duet 0.0.1'; fi\nexit 0\n")
    stale.chmod(0o755)

    home, env = _sandbox(tmp_path, WORKING_DUET)
    env["PATH"] = "%s:%s" % (stale_dir, env["PATH"])

    proc = _run(env, shell)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "not the one just installed" in proc.stdout, proc.stdout
    assert str(stale) in proc.stdout                      # which one is in the way
    assert "duet 0.0.1" in proc.stdout                    # and that it is the old one
    assert str(home / ".duet" / "venv" / "bin" / "duet") in proc.stdout
    assert "export PATH=" in proc.stdout                  # and how to get past it


def test_install_sh_warns_when_the_duet_on_path_does_not_run(tmp_path, shell):
    """The same shadowing, but the thing in the way is broken rather than old.
    Saying nothing leaves `duet` meaning a command that fails."""
    stale_dir = tmp_path / "old"
    stale_dir.mkdir()
    stale = stale_dir / "duet"
    stale.write_text(BROKEN_DUET)
    stale.chmod(0o755)

    home, env = _sandbox(tmp_path, WORKING_DUET)
    env["PATH"] = "%s:%s" % (stale_dir, env["PATH"])

    proc = _run(env, shell)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "not the one just installed" in proc.stdout
    assert "does not run" in proc.stdout, proc.stdout


def test_install_sh_is_quiet_when_the_duet_on_path_is_the_one_installed(tmp_path, shell):
    """The warning above must not fire for the ordinary case, where the symlink
    it just made is what `command -v duet` finds."""
    home, env = _sandbox(tmp_path, WORKING_DUET)
    env["PATH"] = "%s:%s" % (env["PATH"], home / ".local" / "bin")
    proc = _run(env, shell)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "older install" not in proc.stdout
    assert "not on your PATH" not in proc.stdout


def test_a_broken_pip_user_install_falls_through_to_the_venv(tmp_path, shell):
    """`pip --user` can succeed and still leave a duet that will not start.
    The branch has to reject it and carry on rather than accept it, which is
    also why the "will not run" message can only ever be about the venv."""
    home, env = _sandbox(tmp_path, BROKEN_DUET)
    env["FAKE_PIP_USER_OK"] = "1"

    proc = _run(env, shell)
    assert (home / ".local" / "bin" / "duet").is_file()   # --user did install one
    assert "using a virtualenv" in proc.stdout            # and it was not trusted
    assert proc.returncode != 0                           # the venv one is broken too
    assert str(home / ".duet" / "venv") in proc.stderr


def test_a_second_run_repairs_a_half_installed_venv(tmp_path, shell):
    """A first install that died after creating the venv leaves pip believing
    duet is there. Without --upgrade the rerun gets "Requirement already
    satisfied", installs nothing, and the console script is still missing — so
    the rerun that was supposed to repair it cannot, however many times it is
    run. The --user branch already passed --upgrade; the venv branch did not."""
    home, env = _sandbox(tmp_path, WORKING_DUET)
    env["FAKE_PIP_ALREADY_SATISFIED"] = "1"

    proc = _run(env, shell)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (home / ".duet" / "venv" / "bin" / "duet").is_file()
    assert "Installed." in proc.stdout


# A stub that records what it was asked to do, so the installer's own calls can
# be asserted rather than inferred from side effects it cannot produce here.
RECORDING_DUET = (
    "#!/bin/sh\n"
    "if [ \"$1\" = \"--version\" ]; then echo 'duet 0.1.0'; exit 0; fi\n"
    "echo \"$@\" >> \"$DUET_CALLS\"\n"
    "exit 0\n"
)


def test_a_run_with_no_terminal_still_puts_duet_in_place(tmp_path, shell):
    """A CI box has no controlling tty, so nothing can be asked. Installing
    /duet needs no consent though — it writes into the caller's own Claude and
    Codex config directories, which is what running the installer asked for.
    Only the global npm install and two browser sign-ins need a human, and
    neither can be automated anyway."""
    home, env = _sandbox(tmp_path, RECORDING_DUET)
    calls = tmp_path / "calls.txt"
    env["DUET_CALLS"] = str(calls)

    proc = _run(env, shell)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    asked = calls.read_text() if calls.is_file() else ""
    assert "skill install" in asked, "installer did not place /duet: %r" % asked
    assert "setup" not in asked, "setup needs consent and must not run unasked"

    # and it says what is left rather than implying it finished
    assert "needs a terminal" in proc.stdout
    assert "setup" in proc.stdout
