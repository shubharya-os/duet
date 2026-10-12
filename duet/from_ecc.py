"""`duet from-ecc`: find out what you actually use from ECC before you switch.

The hard part of leaving a large harness is not installing the new one, it is
not knowing what you would lose. So this reads your own Claude Code session
logs — locally, nothing leaves the machine — and counts which ECC agents,
skills and commands were really invoked, and where. On the machine it was
built on, that answer was two agents out of 454 skills and agents — one of which
the hand-run audit it replaces had missed — and everything else had never
been called.

It also keeps what you use: `--keep NAME` copies that one agent or skill out
of ECC's plugin cache into your own Claude Code directory, licence notice
included, so removing the plugin does not break the project that relied on it.

It never uninstalls ECC. It prints the command, and you run it.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from duet import ui

# What people use ECC's orchestration commands for, and the duet command that
# does the same job with its rule checked rather than asked.
EQUIVALENTS: Tuple[Tuple[str, str, str], ...] = (
    ("orch-build-mvp", 'duet build "<idea>"', "the gate is red before any code exists"),
    ("orch-fix-defect", 'duet fix "<bug>"', "tests replayed against the original code"),
    ("orch-add-feature", 'duet add "<feature>"', "a test must fail without the feature"),
    ("orch-refine-code", 'duet refactor "<what>"', "existing tests may not change"),
    ("plan / multi-plan", 'duet plan "<goal>"', "only PLAN.md may change"),
    ("code-review / review-pr", "duet review", "a second model, held read-only"),
)


def _project_name(directory: Path) -> str:
    """`-Users-me-code-gallery` → `gallery`, for a readable report."""
    name = directory.name.strip("-")
    return name.rsplit("-", 1)[-1] if "-" in name else name


def _invocations(entry: dict) -> List[Tuple[str, str, Optional[str]]]:
    """(kind, component, call id) for every ECC invocation in one log entry.

    Deliberately narrow. A command that merely *mentions* `ecc:` — a grep in a
    Bash call, a tool result quoting one — is not a use of ECC, and counting
    it is how a hand-run version of this audit first reported two phantom
    slash commands.
    """
    found: List[Tuple[str, str, Optional[str]]] = []
    message = entry.get("message") or {}
    content = message.get("content")
    role = message.get("role")

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                data = block.get("input") or {}
                if block.get("name") in ("Agent", "Task"):
                    agent = str(data.get("subagent_type") or "")
                    if agent.startswith("ecc:"):
                        found.append(("agent", agent[4:], block.get("id")))
                elif block.get("name") == "Skill":
                    skill = str(data.get("skill") or "")
                    if skill.startswith("ecc:"):
                        found.append(("skill", skill[4:], block.get("id")))

    # A slash command the user typed arrives as their own text, wrapped by
    # Claude Code in <command-name>. Tool results also have role "user", so
    # only plain text counts — never a tool_result block.
    if role == "user" and entry.get("type") == "user":
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or []
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        for text in texts:
            marker = "<command-name>/ecc:"
            start = text.find(marker)
            if start != -1:
                rest = text[start + len(marker):]
                name = rest.split("<", 1)[0].split()[0] if rest.strip() else ""
                if name:
                    found.append(("command", name, entry.get("uuid")))
    return found


def audit(home: Path) -> Tuple[Dict[Tuple[str, str], Dict[str, int]], int]:
    """({(kind, component): {project: count}}, sessions scanned)."""
    usage: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sessions = 0
    # One call can be written to the log more than once; count it once. A raw
    # string count is worse still — every session lists ECC's agents in its
    # system context, so the name appears in logs that never used it. The
    # hand-run audit this replaces reported one agent "4x" and missed another.
    seen = set()
    root = home / ".claude" / "projects"
    if not root.is_dir():
        return {}, 0
    for log in root.glob("*/*.jsonl"):
        sessions += 1
        project = _project_name(log.parent)
        try:
            with log.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "ecc:" not in line:          # most lines; skip the JSON parse
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    # The folder name encodes the path with "/" turned into "-",
                    # so `Gallery-repo` would read back as `repo`. Each entry
                    # records its real working directory; prefer that.
                    where = Path(str(entry.get("cwd") or "")).name or project
                    for kind, component, call_id in _invocations(entry):
                        if call_id:
                            if call_id in seen:
                                continue
                            seen.add(call_id)
                        usage[(kind, component)][where] += 1
        except OSError:
            continue
    return {k: dict(v) for k, v in usage.items()}, sessions


def ecc_cache(home: Path) -> Optional[Path]:
    """The newest ECC version in Claude Code's plugin cache, if any is there."""
    base = home / ".claude" / "plugins" / "cache" / "ecc" / "ecc"
    if not base.is_dir():
        return None
    versions = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
    return versions[-1] if versions else None


def ecc_installed(home: Path) -> bool:
    try:
        data = json.loads((home / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(key.startswith("ecc@") for key in (data.get("plugins") or {}))


def keep(home: Path, name: str) -> Tuple[bool, str]:
    """Copy one ECC agent or skill into ~/.claude, with its licence notice."""
    cache = ecc_cache(home)
    if cache is None:
        return False, "ECC's plugin cache is not on this machine, so there is nothing to copy from"
    notice = ""
    try:
        licence = (cache / "LICENSE").read_text(encoding="utf-8").splitlines()
        copyright_line = next((l for l in licence if l.lower().startswith("copyright")), "")
        notice = ("Kept from ECC %s (github.com/affaan-m/ECC) by `duet from-ecc --keep`. "
                  "%s License, %s." % (cache.name, licence[0].split()[0] if licence else "MIT",
                                        copyright_line.strip()))
    except OSError:
        notice = "Kept from ECC %s (github.com/affaan-m/ECC) by `duet from-ecc --keep`." % cache.name

    agent = cache / "agents" / ("%s.md" % name)
    skill = cache / "skills" / name
    if agent.is_file():
        target = home / ".claude" / "agents" / ("%s.md" % name)
        if target.exists():
            return False, "%s already exists; not overwriting it" % target
        text = agent.read_text(encoding="utf-8")
        head, sep, body = text.partition("\n---\n")          # end of front matter
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(head + sep + "\n<!-- %s -->\n" % notice + body if sep
                          else "<!-- %s -->\n%s" % (notice, text), encoding="utf-8")
        return True, "agent kept at %s — call it as `%s`, not `ecc:%s`" % (target, name, name)
    if skill.is_dir():
        target = home / ".claude" / "skills" / name
        if target.exists():
            return False, "%s already exists; not overwriting it" % target
        shutil.copytree(str(skill), str(target))
        (target / "KEPT_FROM_ECC.md").write_text(notice + "\n", encoding="utf-8")
        return True, "skill kept at %s" % target
    return False, "no ECC agent or skill called %r in %s" % (name, cache)


def run(args) -> int:
    home = Path(args.dir).expanduser() if getattr(args, "dir", None) else Path.home()

    if getattr(args, "keep", None):
        ok, message = keep(home, args.keep)
        print((ui.green("✓ ") if ok else ui.red("✗ ")) + message)
        return 0 if ok else 1

    usage, sessions = audit(home)
    installed = ecc_installed(home)
    print(ui.bold("duet from-ecc"))
    print(ui.dim("  ECC:      ") + ("installed" if installed else "not installed")
          + (ui.dim("  (plugin cache present)") if ecc_cache(home) else ""))
    print(ui.dim("  scanned:  ") + "%d Claude Code sessions on this machine — read locally, "
          "nothing sent anywhere" % sessions)
    print()

    if usage:
        print(ui.bold("ECC components you actually invoked"))
        rows = sorted(usage.items(), key=lambda kv: -sum(kv[1].values()))
        for (kind, component), where in rows:
            total = sum(where.values())
            places = ", ".join("%s (%d)" % (p, n) for p, n in sorted(where.items(), key=lambda x: -x[1]))
            print("  %-26s %-8s %3d×  %s" % (component, kind, total, ui.dim(places)))
        print(ui.dim("  Everything else ECC installs was never invoked in these sessions."))
    else:
        print(ui.bold("No ECC component was ever invoked in these sessions."))
        print(ui.dim("  Removing it loses nothing you have used here."))
    print()

    print(ui.bold("What duet does instead — with the rule checked, not asked"))
    for ecc_name, duet_cmd, rule in EQUIVALENTS:
        print("  %-24s → %-26s %s" % (ecc_name, duet_cmd, ui.dim(rule)))
    print(ui.dim("  ECC's domain skills (frameworks, languages, industries) have no duet"))
    print(ui.dim("  equivalent and need none: duet's agents load whatever skills you have."))
    print()

    kept = [c for (kind, c) in usage if kind in ("agent", "skill")] if usage else []
    print(ui.bold("To switch"))
    step = 1
    for component in kept:
        print("  %d. %s   %s" % (step, ui.bold("duet from-ecc --keep %s" % component),
                                  ui.dim("keep what you use")))
        step += 1
    if installed:
        print("  %d. %s   %s" % (step, ui.bold("claude plugin uninstall ecc@ecc"),
                                  ui.dim("you run this; duet never removes it")))
        step += 1
    print("  %d. %s   %s" % (step, ui.bold("duet skill default"),
                              ui.dim("make duet what your agent reaches for")))
    return 0
