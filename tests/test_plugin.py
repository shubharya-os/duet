"""The Claude Code plugin: generated from duet's own skill sources, never hand-edited."""

import json
from pathlib import Path

from duet import __version__, plugin_files

ROOT = Path(__file__).resolve().parent.parent


def test_the_committed_plugin_is_exactly_what_the_sources_render():
    """Edit duet/skill/, forget to regenerate, and this fails — not the user's /duet."""
    for path, text in plugin_files.render().items():
        assert (ROOT / path).read_text(encoding="utf-8") == text, (
            "%s is stale: run `python -m duet.plugin_files --write`" % path)


def test_the_plugin_version_is_the_package_version():
    plugin = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert plugin["version"] == market["plugins"][0]["version"] == __version__
    assert market["plugins"][0]["source"] == "./plugin"


def test_the_command_checks_for_the_cli_and_never_installs_unasked():
    command = plugin_files.render()["plugin/commands/duet.md"]
    front = command.split("---\n")[1]
    assert "Bash(command -v duet)" in front
    assert "pipx" not in front and "pip" not in front      # so Claude Code asks first
    assert "Run it only once they say yes" in command
    assert "{{DUET}}" not in command
    assert "{{DUET}}" not in plugin_files.render()["plugin/skills/duet/SKILL.md"]


def test_skill_install_leaves_claude_code_to_the_plugin_when_it_is_installed(tmp_path, monkeypatch, capsys):
    """Two /duet commands and two duet skills would both reach the model."""
    from duet.cli import main

    monkeypatch.setattr("duet.cli._invocation_works", lambda command: True)
    home = tmp_path / "home"
    plugins = home / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"duet@duet": [{"scope": "user"}]}}), encoding="utf-8")

    assert main(["skill", "install", "--dir", str(home)]) == 0
    assert not (home / ".claude" / "commands" / "duet.md").exists()
    assert not (home / ".claude" / "skills" / "duet" / "SKILL.md").exists()
    assert (home / ".codex" / "skills" / "duet" / "SKILL.md").exists()
    assert "provided by the duet plugin" in capsys.readouterr().out
