"""The first five minutes, on a machine that is not this one.

Everything here is a failure that only shows up on someone else's laptop: a
home directory with a space in it, npm installing into a directory nobody's
PATH mentions, a second `duet setup` over a first one, and — the one this
project exists for — a command that prints a tick it has not earned.
"""
import argparse
import stat
import subprocess
import sys
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# /duet is only installed if duet can actually be run


def test_skill_install_does_not_claim_duet_works_when_nothing_runs(tmp_path, monkeypatch, capsys):
    """Every candidate invocation failed, and it printed four green ticks and
    "In a new Claude Code session: /duet ..." and exited 0. Typing /duet then
    fails on a machine that was told it was ready. That is the whole failure
    class this project is about."""
    from duet.cli import main

    monkeypatch.setattr("duet.cli._invocation_works", lambda command: False)
    monkeypatch.setattr("duet.cli.shutil.which", lambda name: None)

    home = tmp_path / "home"
    assert main(["skill", "install", "--dir", str(home)]) == 1
    out = capsys.readouterr().out
    assert "/duet will not work yet" in out
    assert "export PATH=" in out                  # names the fix, not just the problem
    assert "duet skill install --force" in out
    # and it does not go on to say it is ready
    assert "In a new " not in out


def test_setup_fails_when_duet_cannot_be_invoked(tmp_path, monkeypatch, capsys):
    """`duet setup` ends with "done. Open a new Claude Code session and type
    /duet". It must not reach that line when /duet cannot call duet."""
    from duet.adapters.base import Probe
    from duet.cli import main

    for module in ("claude_code.ClaudeCodeAdapter", "codex_cli.CodexCliAdapter"):
        monkeypatch.setattr("duet.adapters.%s.probe" % module,
                            classmethod(lambda cls, config=None: Probe(ok=True, detail="signed in")))
    monkeypatch.setattr("duet.cli.Adapter.which", staticmethod(lambda *a: "/usr/bin/true"))
    monkeypatch.setattr("duet.cli._invocation_works", lambda command: False)
    monkeypatch.setattr("duet.cli.shutil.which", lambda name: None)
    monkeypatch.setattr("duet.cli.Path.home", staticmethod(lambda: tmp_path / "home"))

    assert main(["setup", "-C", str(tmp_path), "--yes"]) == 1
    assert "done." not in capsys.readouterr().out


def test_a_verified_invocation_still_reports_success(tmp_path, monkeypatch, capsys):
    """The honest failure must not fire when things are fine."""
    from duet.cli import main

    monkeypatch.setattr("duet.cli._invocation_works", lambda command: True)
    monkeypatch.setattr("duet.cli.shutil.which", lambda name: None)

    home = tmp_path / "home"
    assert main(["skill", "install", "--dir", str(home)]) == 0
    out = capsys.readouterr().out
    assert "In a new " in out
    assert "will not work yet" not in out


# --------------------------------------------------------------------------
# a home directory with a space in it


def test_the_invocation_survives_a_space_in_the_path(tmp_path, monkeypatch):
    """`/Users/Ada Lovelace` is an ordinary macOS home. Unquoted, the fallback
    invocation becomes `PYTHONPATH=/Users/Ada Lovelace/x python -m duet`, and
    the shell runs `Lovelace/x` as a command: "et: command not found"."""
    import shlex

    from duet.cli import _candidate_invocations

    spaced = tmp_path / "Ada Lovelace" / "bin"
    spaced.mkdir(parents=True)
    fake = spaced / "duet"
    fake.write_text("#!/bin/sh\necho 'duet 0.1.0'\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr("duet.cli.shutil.which",
                        lambda name: str(fake) if name == "duet" else None)
    monkeypatch.setattr("duet.cli.sys.executable", str(fake))

    for candidate in _candidate_invocations():
        # Every candidate must survive being handed to a shell: the first word
        # after any VAR= prefix has to be the whole path, not its first word.
        words = shlex.split(candidate)
        program = next(w for w in words if "=" not in w.split("/")[0] or w.startswith("/"))
        assert Path(program).exists(), "%r splits into a path that is not there" % candidate


def test_a_spaced_path_invocation_actually_runs(tmp_path, monkeypatch):
    """The end-to-end version of the above: the chosen invocation is executed."""
    from duet.cli import _candidate_invocations, _invocation_works

    from fakebin import fake_cli

    fake = fake_cli(tmp_path / "Ada Lovelace", "duet", "echo 'duet 0.1.0'\n", shebang="#!/bin/sh")

    monkeypatch.setattr("duet.cli.shutil.which",
                        lambda name: str(fake) if name == "duet" else None)
    assert _invocation_works(_candidate_invocations()[0])


# --------------------------------------------------------------------------
# a second run over a previous install


def test_reinstall_updates_a_file_duet_wrote_with_an_older_invocation(tmp_path, monkeypatch, capsys):
    """duet moves — a `pip --user` install replaced by a virtualenv, or `duet`
    finally reaching PATH. The files then hold the old invocation, and
    refusing them as "a different version is already there" failed a plain
    `duet setup` over a working install, for a file duet wrote itself."""
    from duet.cli import main

    monkeypatch.setattr("duet.cli._invocation_works", lambda command: True)
    home = tmp_path / "home"

    monkeypatch.setattr("duet.cli.shutil.which", lambda name: "/old/path/duet")
    assert main(["skill", "install", "--dir", str(home)]) == 0
    command = home / ".claude" / "commands" / "duet.md"
    assert "/old/path/duet" in command.read_text(encoding="utf-8")
    capsys.readouterr()

    monkeypatch.setattr("duet.cli.shutil.which", lambda name: "/new/path/duet")
    assert main(["skill", "install", "--dir", str(home)]) == 0, capsys.readouterr().out
    text = command.read_text(encoding="utf-8")
    assert "/new/path/duet" in text
    assert "/old/path/duet" not in text


def test_a_hand_edited_file_is_still_protected(tmp_path, monkeypatch, capsys):
    """The rule above must not turn into "overwrite whatever is there"."""
    from duet.cli import main

    monkeypatch.setattr("duet.cli._invocation_works", lambda command: True)
    monkeypatch.setattr("duet.cli.shutil.which", lambda name: "/old/path/duet")
    home = tmp_path / "home"
    assert main(["skill", "install", "--dir", str(home)]) == 0
    capsys.readouterr()

    command = home / ".claude" / "commands" / "duet.md"
    command.write_text(command.read_text(encoding="utf-8") + "\n## my own section\nDo not lose this.\n", encoding="utf-8")

    assert main(["skill", "install", "--dir", str(home)]) == 1
    out = capsys.readouterr().out
    assert "already there" in out
    assert "--force" in out
    assert "Do not lose this." in command.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# npm installed them somewhere nobody's PATH mentions


def test_setup_names_the_npm_directory_when_the_clis_are_still_missing(tmp_path, monkeypatch, capsys):
    """npm exits 0 and `claude` is still not found: it installed into a prefix
    that is not on PATH. "Run it yourself and re-run setup" is the wrong
    instruction — running it again puts them in the same place."""
    from duet.cli import main

    monkeypatch.setattr("duet.cli.Adapter.which",
                        staticmethod(lambda *c: "/usr/bin/npm" if "npm" in c else None))
    monkeypatch.setattr("duet.cli.subprocess.call", lambda *a, **k: 0)
    monkeypatch.setattr("duet.cli._npm_global_bin", lambda: "/opt/node/bin")
    monkeypatch.setattr("duet.cli.Path.home", staticmethod(lambda: tmp_path / "home"))

    assert main(["setup", "-C", str(tmp_path), "--yes"]) == 1
    out = capsys.readouterr().out
    assert "/opt/node/bin" in out
    assert 'export PATH="/opt/node/bin:$PATH"' in out
    assert "did not finish cleanly" not in out


def test_setup_says_how_to_find_the_npm_directory_when_npm_will_not_say(tmp_path, monkeypatch, capsys):
    from duet.cli import main

    monkeypatch.setattr("duet.cli.Adapter.which",
                        staticmethod(lambda *c: "/usr/bin/npm" if "npm" in c else None))
    monkeypatch.setattr("duet.cli.subprocess.call", lambda *a, **k: 0)
    monkeypatch.setattr("duet.cli._npm_global_bin", lambda: None)
    monkeypatch.setattr("duet.cli.Path.home", staticmethod(lambda: tmp_path / "home"))

    assert main(["setup", "-C", str(tmp_path), "--yes"]) == 1
    assert "npm prefix -g" in capsys.readouterr().out


def test_npm_global_bin_falls_back_when_npm_bin_g_is_gone(monkeypatch):
    """`npm bin -g` was removed in npm 9 and errors there; `npm prefix -g` is
    the one that has always worked."""
    from duet import cli

    class Result:
        def __init__(self, code, out):
            self.returncode, self.stdout = code, out

    def run(argv, **kwargs):
        if argv[1] == "bin":
            return Result(1, "Unknown command: \"bin\"\n")
        return Result(0, "/opt/homebrew\n")

    # On Windows npm resolves to a shim that runs node on npm's script, which
    # moves "bin" out of argv[1]; the fake should see the command as written.
    monkeypatch.setattr("duet.cli.launchable", list)
    monkeypatch.setattr("duet.cli.winlaunch.WINDOWS", False)
    monkeypatch.setattr("duet.cli.subprocess.run", run)
    assert cli._npm_global_bin() == str(Path("/opt/homebrew") / "bin")


def test_a_still_broken_npm_run_keeps_the_old_message(tmp_path, monkeypatch, capsys):
    """npm itself failing is a different problem and keeps its own advice."""
    from duet.cli import main

    monkeypatch.setattr("duet.cli.Adapter.which",
                        staticmethod(lambda *c: "/usr/bin/npm" if "npm" in c else None))
    monkeypatch.setattr("duet.cli.subprocess.call", lambda *a, **k: 1)
    monkeypatch.setattr("duet.cli.Path.home", staticmethod(lambda: tmp_path / "home"))

    assert main(["setup", "-C", str(tmp_path), "--yes"]) == 1
    assert "did not finish cleanly" in capsys.readouterr().out


def test_one_subscription_is_offered_a_pair_that_works(tmp_path, monkeypatch, capsys):
    """Someone with a Claude plan and no ChatGPT plan was told duet was not
    ready and given no way forward — when two Claude models pair perfectly well.
    A reviewer with no memory of writing the code is a real reviewer."""
    from duet.adapters.base import Probe
    from duet.cli import main

    monkeypatch.setattr("duet.adapters.claude_code.ClaudeCodeAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(ok=True, detail="signed in")))
    monkeypatch.setattr("duet.adapters.codex_cli.CodexCliAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(
                            ok=False, signed_in=False, detail="not signed in", fix="codex login")))

    assert main(["doctor", "-C", str(tmp_path)]) == 1      # still honestly not ready
    out = capsys.readouterr().out
    assert "claude:opus+claude:sonnet" in out
    assert "Only have one of the two?" in out
    # and it does not oversell it
    assert "blind spots" in out


def test_only_chatgpt_is_offered_the_other_pair(tmp_path, monkeypatch, capsys):
    from duet.adapters.base import Probe
    from duet.cli import main

    monkeypatch.setattr("duet.adapters.claude_code.ClaudeCodeAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(
                            ok=False, signed_in=False, detail="not signed in",
                            fix="claude auth login")))
    monkeypatch.setattr("duet.adapters.codex_cli.CodexCliAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(ok=True, detail="signed in")))

    assert main(["doctor", "-C", str(tmp_path)]) == 1
    assert "codex+codex" in capsys.readouterr().out


def test_no_fallback_is_offered_when_neither_side_works(tmp_path, monkeypatch, capsys):
    """Suggesting a pair that also cannot run would be noise."""
    from duet.adapters.base import Probe
    from duet.cli import main

    for module in ("claude_code.ClaudeCodeAdapter", "codex_cli.CodexCliAdapter"):
        monkeypatch.setattr("duet.adapters.%s.probe" % module,
                            classmethod(lambda cls, config=None: Probe(
                                ok=False, signed_in=False, detail="not signed in", fix="log in")))
    assert main(["doctor", "-C", str(tmp_path)]) == 1
    assert "Only have one of the two?" not in capsys.readouterr().out


# --- findings from reviewing the npx change ---------------------------------

def test_login_uses_the_launch_prefix_not_the_bare_binary(tmp_path, monkeypatch):
    """Under the npx fallback `.bin` is npx itself, so appending the agent's
    subcommands produced `npx auth login` — not a sign-in, and an invitation for
    npx to fetch whatever package is named `auth`. The headline capability of
    the npx change did not work end to end."""
    from duet.adapters.base import Probe
    from duet.cli import main

    ran = []
    monkeypatch.setattr("duet.cli.subprocess.call", lambda argv, *a, **k: ran.append(list(argv)) or 0)

    # One patch: duet.cli.Adapter and duet.adapters.base.Adapter are the same
    # class, so two setattrs would just overwrite each other. Resolve npx by
    # name and by absolute path, and nothing else — the no-global-install case.
    def only_npx(*candidates):
        return "/usr/bin/npx" if any(str(c).endswith("npx") for c in candidates) else None

    monkeypatch.setattr("duet.adapters.base.Adapter.which", staticmethod(only_npx))

    states = iter([Probe(ok=False, signed_in=False, detail="not signed in", fix="log in"),
                   Probe(ok=True, detail="signed in")])
    monkeypatch.setattr("duet.adapters.claude_code.ClaudeCodeAdapter.probe",
                        classmethod(lambda cls, config=None: next(states, Probe(ok=True, detail="signed in"))))
    monkeypatch.setattr("duet.adapters.codex_cli.CodexCliAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(ok=True, detail="signed in")))

    main(["login", "claude", "-C", str(tmp_path)])
    assert ran, "login never ran anything"
    argv = ran[0]
    assert argv[:3] == ["/usr/bin/npx", "-y", "@anthropic-ai/claude-code"], argv
    assert argv[-2:] == ["auth", "login"], argv
    assert argv[1] != "auth", "npx would resolve 'auth' as a package name"


def test_setup_asks_before_installing_globally_even_without_npx(tmp_path, monkeypatch, capsys):
    """The ask sat inside the `if has_npx:` branch, so on a machine with npm and
    no npx the global install ran unprompted — and without printing what it was
    about to do. It is the only step that changes anything outside duet."""
    from duet.adapters.base import Probe
    from duet.cli import main

    calls = []
    monkeypatch.setattr("duet.cli.subprocess.call", lambda cmd, **k: calls.append(cmd) or 0)
    monkeypatch.setattr("duet.cli.Adapter.which",
                        staticmethod(lambda *c: "/usr/bin/npm" if "npm" in c else None))
    monkeypatch.setattr("duet.adapters.claude_code.ClaudeCodeAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(ok=True, detail="signed in")))
    monkeypatch.setattr("duet.adapters.codex_cli.CodexCliAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(ok=True, detail="signed in")))
    monkeypatch.setattr("duet.cli._ask", lambda question, yes: False)   # user declines
    monkeypatch.setattr("duet.cli.Path.home", staticmethod(lambda: tmp_path / "home"))

    assert main(["setup", "-C", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "npm install -g" in out          # said what it would do
    assert not calls, "installed globally despite the user declining"
    assert "skipped" in out


def test_a_quota_exhausted_side_is_not_recommended_as_a_pair(tmp_path, monkeypatch, capsys):
    """doctor knows the allowance is gone two lines above; recommending
    codex+codex from that same information is a pair that cannot run a turn."""
    from duet.adapters.base import Probe
    from duet.cli import main

    monkeypatch.setattr("duet.adapters.claude_code.ClaudeCodeAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(
                            ok=False, signed_in=False, detail="not signed in", fix="log in")))
    monkeypatch.setattr("duet.adapters.codex_cli.CodexCliAdapter.probe",
                        classmethod(lambda cls, config=None: Probe(
                            ok=False, signed_in=True, detail="out of quota", fix="wait")))

    assert main(["doctor", "-C", str(tmp_path)]) == 1
    assert "codex+codex" not in capsys.readouterr().out


def test_a_cli_installed_off_path_is_not_called_missing(tmp_path, monkeypatch):
    """The check only looked at PATH while the adapters also resolve
    DUET_CLAUDE_BIN, ~/.claude/local/claude and ~/.local/bin/codex. On a machine
    where the CLI lives there, setup offered to npm-install something duet could
    already run — and after setup gained a hard stop on declining, that mistake
    could abort setup on a working machine."""
    from duet.cli import _missing_agent_clis

    installed = tmp_path / "claude"
    installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    installed.chmod(0o755)

    monkeypatch.setattr("duet.adapters.base.Adapter.which",
                        staticmethod(lambda *c: str(installed) if any("claude" in str(x) for x in c) else None))
    assert "claude" not in _missing_agent_clis()


def test_a_cli_only_reachable_through_npx_still_counts_as_not_installed(monkeypatch):
    """npx can run it, but "installed globally" is the question setup is asking,
    and the answer decides whether to offer the install."""
    from duet.cli import _missing_agent_clis

    monkeypatch.setattr("duet.adapters.base.Adapter.which",
                        staticmethod(lambda *c: "/usr/bin/npx" if any(str(x).endswith("npx") for x in c) else None))
    assert _missing_agent_clis() == ["claude", "codex"]


def test_the_rerun_command_keeps_the_arguments_you_typed(monkeypatch):
    from duet import cli

    monkeypatch.setattr(cli.sys, "argv",
                        ["duet", "build", "a thing with spaces", "-C", "/tmp/x"])
    line = cli.rerun_with_pair("claude:opus+claude:sonnet")
    assert line.startswith("duet build ")
    assert "'a thing with spaces'" in line
    assert line.endswith("--pair claude:opus+claude:sonnet")
    assert "-C /tmp/x" in line


def test_the_rerun_command_replaces_a_pair_rather_than_repeating_it(monkeypatch):
    from duet import cli

    for argv in (["duet", "run", "t", "--pair", "claude+codex"],
                 ["duet", "run", "t", "--pair=claude+codex"]):
        monkeypatch.setattr(cli.sys, "argv", argv)
        line = cli.rerun_with_pair("codex+codex")
        # Two --pair flags and argparse keeps the last, but a command printed
        # as advice has to be readable as well as correct.
        assert line.count("--pair") == 1
        assert "claude+codex" not in line


def test_an_empty_directory_is_pointed_at_build_not_run(tmp_path, capsys):
    """`duet run` cannot work here: there is nothing to detect a gate from.

    Suggesting it is a small lie told at the exact moment someone is deciding
    whether the tool is for them.
    """
    from duet import cli

    args = argparse.Namespace(root=str(tmp_path), session=None, json=False, quiet=False)
    cli.cmd_status(args)
    out = capsys.readouterr().out
    assert "duet build" in out
    assert 'duet run "..."' not in out

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    cli.cmd_status(args)
    out = capsys.readouterr().out
    assert 'duet run "..."' in out


def test_every_skill_discloses_a_same_vendor_fallback():
    """/duet silently ran two Claude models and reported it as normal.

    With the ChatGPT side out of quota, the model inside /duet found duet's
    one-subscription command and ran it — good — then told the user the
    session was running without mentioning it was Claude reviewing Claude.
    The user would weigh that verdict as a cross-vendor second opinion. The
    skill has to require saying so, not leave it to improvisation.
    """
    from pathlib import Path
    import duet

    skill_dir = Path(duet.__file__).parent / "skill"
    for name in ("SKILL.md", "claude-command.md", "codex-skill.md", "codex-prompt.md"):
        text = skill_dir.joinpath(name).read_text(encoding="utf-8")
        assert "Only have one of the two?" in text, name
        assert "Never present it as a cross-vendor second opinion" in text, name


def _home(tmp_path):
    return argparse.Namespace(action="default", dir=str(tmp_path), force=True,
                              root=str(tmp_path), quiet=False, json=False)


def test_default_writes_a_block_every_session_reads(tmp_path, capsys):
    from duet import cli

    assert cli.cmd_skill_default(_home(tmp_path), enable=True) == 0
    claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert cli.DEFAULT_START in claude_md and cli.DEFAULT_END in claude_md
    assert 'fix "<the bug>"' in claude_md and "{{DUET}}" not in claude_md
    assert "Do it directly, without duet" in claude_md          # it is scoped, not total
    # and the skill it points at is actually there
    assert (tmp_path / ".claude" / "skills" / "duet" / "SKILL.md").is_file()
    assert (tmp_path / ".codex" / "AGENTS.md").is_file()


def test_default_keeps_what_you_already_had_and_undo_restores_it_exactly(tmp_path, capsys):
    from duet import cli

    mine = "# my rules\n\nalways use tabs\n"
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text(mine, encoding="utf-8")

    cli.cmd_skill_default(_home(tmp_path), enable=True)
    both = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert both.startswith(mine.rstrip("\n")) and cli.DEFAULT_START in both

    cli.cmd_skill_default(_home(tmp_path), enable=True)          # twice: no second block
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8").count(cli.DEFAULT_START) == 1

    cli.cmd_skill_default(_home(tmp_path), enable=False)
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == mine


def test_undo_removes_a_file_duet_created_rather_than_leaving_it_empty(tmp_path, capsys):
    from duet import cli

    cli.cmd_skill_default(_home(tmp_path), enable=True)
    cli.cmd_skill_default(_home(tmp_path), enable=False)
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert not (tmp_path / ".codex" / "AGENTS.md").exists()


def test_the_default_block_tells_agents_inside_a_session_not_to_nest(tmp_path, capsys):
    """duet's own agents load ~/.claude/CLAUDE.md like any other session.

    Unchecked, the block that says "hand real changes to duet" would tell an
    agent already inside a duet session to start another one. The live run
    did not try — but that should not depend on luck.
    """
    from duet import cli

    cli.cmd_skill_default(_home(tmp_path), enable=True)
    text = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "already one of the two agents inside a duet session" in text


def test_gemini_and_opencode_get_duet_only_where_they_are_used(tmp_path, capsys):
    # tomllib is 3.11+, and duet supports 3.9 — this test imported it
    # unconditionally and turned CI red on both 3.9 jobs. The TOML is still
    # parsed for real on 3.11 and 3.13; on older Pythons the shape is checked.
    try:
        import tomllib
    except ModuleNotFoundError:
        tomllib = None
    from duet import cli

    args = argparse.Namespace(action="install", dir=str(tmp_path), force=False,
                              root=str(tmp_path), quiet=False, json=False)
    cli.cmd_skill(args)
    # neither harness has been used here, so neither gets a directory made for it
    assert not (tmp_path / ".gemini").exists()
    assert not (tmp_path / ".config" / "opencode").exists()

    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    cli.cmd_skill(args)
    raw = (tmp_path / ".gemini" / "commands" / "duet.toml").read_text(encoding="utf-8")
    if tomllib is not None:
        toml = tomllib.loads(raw)
        assert set(toml) == {"description", "prompt"}
        prompt = toml["prompt"]
    else:
        assert raw.count("\'\'\'") == 2 and "\ndescription = " in raw and "\nprompt = " in raw
        prompt = raw.split("\'\'\'")[1]
    assert "{{args}}" in prompt and "$ARGUMENTS" not in prompt
    opencode = (tmp_path / ".config" / "opencode" / "commands" / "duet.md").read_text(encoding="utf-8")
    assert opencode.startswith("---\ndescription: ") and "$ARGUMENTS" in opencode


def test_default_never_creates_the_opencode_file_that_would_shadow_claude_md(tmp_path, capsys):
    """OpenCode reads ~/.claude/CLAUDE.md only when its own AGENTS.md is absent.

    Creating that file to add duet's block would silently cut OpenCode off
    from every other rule in CLAUDE.md. Absent, it already sees duet's block
    through the fallback — so duet leaves it absent.
    """
    from duet import cli

    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    (tmp_path / ".gemini").mkdir()
    cli.cmd_skill_default(_home(tmp_path), enable=True)
    assert not (tmp_path / ".config" / "opencode" / "AGENTS.md").exists()
    assert cli.DEFAULT_START in (tmp_path / ".gemini" / "GEMINI.md").read_text(encoding="utf-8")

    (tmp_path / ".config" / "opencode" / "AGENTS.md").write_text("# my opencode rules\n", encoding="utf-8")
    cli.cmd_skill_default(_home(tmp_path), enable=True)
    assert cli.DEFAULT_START in (tmp_path / ".config" / "opencode" / "AGENTS.md").read_text(encoding="utf-8")


def test_project_mode_writes_the_repositorys_agents_md_for_cursor(tmp_path, capsys):
    from duet import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# house rules\n", encoding="utf-8")
    args = _home(tmp_path)
    args.project, args.root = True, str(repo)
    cli.cmd_skill_default(args, enable=True)
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# house rules") and cli.DEFAULT_START in text
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()   # the home dir untouched
    cli.cmd_skill_default(args, enable=False)
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "# house rules\n"
