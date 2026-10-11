"""`/duet running`, `/duet status` and `/duet review` reach the same handling
whichever file answers them.

Claude Code lets a skill answer `/<its name>`, and the skill wins over the
command of the same name. Observed with Claude Code 2.1.283: `/duet running`
was answered by the duet skill, which had no argument handling, so it asked
what to build instead of reporting the session that was running.
"""

import re

from duet import plugin_files
from duet.cli import main

STATUS = "## If the argument is `running` or `status`"
REVIEW = "## If the argument is `review`"
OTHERWISE = "## Otherwise"


def _every_surface(tmp_path, monkeypatch):
    """{label: text} of every installed or generated file that can answer /duet."""
    monkeypatch.setattr("duet.cli._candidate_invocations", lambda: ["duet"])
    monkeypatch.setattr("duet.cli._invocation_works", lambda command: True)
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)                 # derived files are written
    (home / ".config" / "opencode").mkdir(parents=True)    # only for harnesses in use
    assert main(["skill", "install", "--dir", str(home)]) == 0

    surfaces = {
        "Claude Code skill": home / ".claude" / "skills" / "duet" / "SKILL.md",
        "Claude Code command": home / ".claude" / "commands" / "duet.md",
        "Codex skill": home / ".codex" / "skills" / "duet" / "SKILL.md",
        "Codex prompt": home / ".codex" / "prompts" / "duet.md",
        "Gemini command": home / ".gemini" / "commands" / "duet.toml",
        "OpenCode command": home / ".config" / "opencode" / "commands" / "duet.md",
    }
    texts = {label: path.read_text(encoding="utf-8") for label, path in surfaces.items()}
    rendered = plugin_files.render()
    texts["plugin command"] = rendered["plugin/commands/duet.md"]
    texts["plugin skill"] = rendered["plugin/skills/duet/SKILL.md"]
    return texts


def _routing(label, text):
    """The running/status and review sections, as the host will read them."""
    assert STATUS in text, "%s does not handle `/duet running` or `/duet status`" % label
    assert REVIEW in text, "%s does not handle `/duet review`" % label
    start = text.index(STATUS)
    end = text.index(OTHERWISE, start)
    block = text[start:end]
    # The other model in Codex is Claude, so its review names it; nothing else differs.
    return re.sub(r" --reviewer claude\b", "", block)


def test_every_surface_routes_running_status_and_review_the_same_way(tmp_path, monkeypatch):
    texts = _every_surface(tmp_path, monkeypatch)
    blocks = {label: _routing(label, text) for label, text in texts.items()}

    reference = blocks["Claude Code command"]
    assert "Run `duet status`" in reference
    assert "Do not start a new session" in reference
    assert "Run `duet review" in reference
    for label, block in blocks.items():
        assert block == reference, "%s handles /duet arguments differently from the command" % label


def test_every_surface_reads_the_argument_before_anything_else(tmp_path, monkeypatch):
    """Routing that comes after the instructions for starting a session comes too late."""
    texts = _every_surface(tmp_path, monkeypatch)
    for label, text in texts.items():
        headings = [h for h in re.findall(r"^## .*$", text, re.M)
                    if h != "## First: is the duet CLI installed?"]   # the plugin's prelude
        assert headings[:3] == [STATUS, REVIEW, "## Otherwise — hand the work over"], (
            "%s does not open with the argument handling: %s" % (label, headings[:3]))
