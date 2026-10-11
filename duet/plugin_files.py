"""The Claude Code plugin, rendered from the same sources `duet skill install` uses.

ECC installs in two commands from inside Claude Code; duet needed pip and then
`duet skill install`. The plugin closes that gap:

    /plugin marketplace add shubharya-os/duet
    /plugin install duet@duet

It carries the `/duet` command and the skill. The harness itself is still the
Python CLI, so the command checks for it first and offers the one-line install
rather than running it unasked.

The files under `plugin/` are generated, never edited by hand: a test compares
them with `render()`, so the plugin cannot drift from what `duet skill install`
writes. After changing anything in `duet/skill/`, run

    python -m duet.plugin_files --write
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict

from duet import __version__

REPO = "https://github.com/shubharya-os/duet"
PLUGIN_DIR = "plugin"
INSTALL_LINE = "pipx install git+%s" % REPO

DESCRIPTION = ("Two AI models build it and check each other; the harness runs your tests "
               "and checks the rule each workflow is held to (fix, add, refactor, build, plan).")
KEYWORDS = ["claude-code", "codex", "second-opinion", "code-review", "tdd", "workflow",
            "multi-agent", "consensus"]

# Said once, before anything else in the command. The plugin can be installed
# without the CLI; without this, the first /duet fails on "command not found"
# and the agent improvises an install.
PRELUDE = """\
## First: is the duet CLI installed?

This plugin carries the instructions; the harness is a small Python CLI. Run
`command -v duet`. If it prints nothing, tell the user duet itself is not installed
yet, and offer to install it with this one line (Python 3.9+):

```bash
%s
```

Run it only once they say yes, then carry on below. If they have no `pipx`,
`python3 -m pip install --user git+%s` works too.

""" % (INSTALL_LINE, REPO)


def skill_dir() -> Path:
    return Path(__file__).resolve().parent / "skill"


ROUTING = "{{ROUTING}}"
# Who `/duet review` asks, by host: in Codex the other model is Claude; in
# Claude Code `duet review` already picks whoever does not normally lead.
REVIEWER = {"codex-skill.md": " --reviewer claude", "codex-prompt.md": " --reviewer claude"}


def skill_template(filename: str, prelude: str = "") -> str:
    """A packaged skill or command source with its argument handling spliced in.

    Claude Code lets the skill answer `/duet` as well as the command, so
    `/duet running` and `/duet review` must be handled the same by every file
    that can receive them. That handling lives once, in routing.md, and every
    source carries a `{{ROUTING}}` marker where it goes. `{{DUET}}` is left in
    for the caller to fill.
    """
    text = (skill_dir() / filename).read_text(encoding="utf-8")
    if ROUTING not in text:
        return text
    routing = (skill_dir() / "routing.md").read_text(encoding="utf-8")
    routing = routing.replace("{{REVIEWER}}", REVIEWER.get(filename, ""))
    return text.replace(ROUTING, prelude + routing, 1)


def _command() -> str:
    text = skill_template("claude-command.md", PRELUDE).replace("{{DUET}}", "duet")
    _, _, rest = text.partition("---\n")
    front, _, body = rest.partition("---\n")
    # `command -v` is read-only, so it needs no prompt; the install line is
    # left out on purpose, so Claude Code asks the user before it runs.
    front = front.replace("allowed-tools: ", "allowed-tools: Bash(command -v duet), ", 1)
    front = front.replace("Bash(duet:*), Bash(duet:*)", "Bash(duet:*)")   # both placeholders became `duet`
    return "---\n" + front + "---\n" + body


def _skill() -> str:
    # The skill answers `/duet` too, so it checks for the CLI the same way.
    return skill_template("SKILL.md", PRELUDE).replace("{{DUET}}", "duet")


def _plugin_json() -> str:
    return json.dumps({
        "name": "duet",
        "version": __version__,
        "description": DESCRIPTION,
        "author": {"name": "shubharya-os", "url": "https://github.com/shubharya-os"},
        "homepage": "https://shubharya-os.github.io/duet/",
        "repository": REPO,
        "license": "MIT",
        "keywords": KEYWORDS,
    }, indent=2) + "\n"


def _marketplace_json() -> str:
    return json.dumps({
        "name": "duet",
        "owner": {"name": "shubharya-os", "url": "https://github.com/shubharya-os"},
        "metadata": {"description": "duet: two models, one checked rule per workflow"},
        "plugins": [{
            "name": "duet",
            "source": "./%s" % PLUGIN_DIR,
            "description": DESCRIPTION,
            "version": __version__,
            "homepage": "https://shubharya-os.github.io/duet/",
            "repository": REPO,
            "license": "MIT",
            "category": "workflow",
            "keywords": KEYWORDS,
        }],
    }, indent=2) + "\n"


def render() -> Dict[str, str]:
    """{path relative to the repository root: content} for every plugin file."""
    return {
        ".claude-plugin/marketplace.json": _marketplace_json(),
        "%s/.claude-plugin/plugin.json" % PLUGIN_DIR: _plugin_json(),
        "%s/commands/duet.md" % PLUGIN_DIR: _command(),
        "%s/skills/duet/SKILL.md" % PLUGIN_DIR: _skill(),
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(__file__).resolve().parent.parent
    stale = [p for p, text in render().items()
             if not (root / p).is_file() or (root / p).read_text(encoding="utf-8") != text]
    if "--write" in argv:
        for path, text in render().items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text, encoding="utf-8")
        print("wrote %d plugin files" % len(render()))
        return 0
    for path in stale:
        print("stale: %s" % path)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
