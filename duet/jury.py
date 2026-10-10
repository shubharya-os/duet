"""`duet jury`: a simulated panel of stakeholders that judges what is being built.

Tests say whether the thing works. They do not say whether anyone would use it,
pay for it, back it or pick it over what they already have. The jury puts that
question to a panel of personas — each one a single read-only pass by an agent —
and holds every objection to evidence: a quote that is really in a workspace
file, or really in the output of the user's walkthrough. An objection whose
evidence is not there is shown as unverified and does not count.

It is a simulated panel — models playing stakeholders. It finds gaps. It is not
market evidence and not a replacement for talking to real users, and every
report says so.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from duet.protocol import SEVERITIES, iter_json_objects, slugify
from duet.workspace import SKIP_DIRS, Workspace, changed_paths

JURY_CONFIG = "duet-jury.json"
JURY_FILE = "JURY.md"

STAGES = ("idea", "plan", "build", "launch")
DECISIONS = ("yes", "no", "maybe")
JUDGE_CRITERIA = ("originality", "execution", "impact", "clarity")

# The exit codes of `duet jury`.
JURY_CLEAR = 0        # no verified blocker
JURY_BLOCKED = 1      # at least one verified blocker
JURY_FAILED = 3       # could not run: bad config, bad panel, no juror answered

# How many times the panel may sit in one session, unless duet-jury.json says.
DEFAULT_MAX_CONVENINGS = 2

# A user who cannot get the thing to start in a minute has a finding already.
WALKTHROUGH_TIMEOUT = 60
WALKTHROUGH_CHARS = 8000

# "Little code": a plan with at most this many source files is still a plan.
PLAN_STAGE_MAX_SOURCE_FILES = 2

# A quote shorter than this proves nothing: "the" is in every file.
MIN_QUOTE_CHARS = 8
EVIDENCE_READ_LIMIT = 2_000_000

FILE_CHARS = 12000
FILE_BUDGET = 48000
FILE_COUNT = 12

SOURCE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".kt",
    ".swift", ".c", ".cc", ".cpp", ".h", ".cs", ".php", ".html", ".css", ".vue",
    ".svelte", ".sh",
}

DISCLAIMER = (
    "This is a simulated panel — models playing stakeholders. It finds gaps worth "
    "closing; it is not market evidence, and it is not a replacement for putting "
    "the product in front of real users."
)


class JuryError(Exception):
    """The jury cannot run as configured. Always exit 3, never a verdict."""


# --------------------------------------------------------------------------
# personas
# --------------------------------------------------------------------------
@dataclass
class Persona:
    name: str
    role: str
    cares_about: str
    decision: str

    def to_dict(self) -> Dict[str, str]:
        return dict(name=self.name, role=self.role, cares_about=self.cares_about,
                    decision=self.decision)


DEFAULT_PERSONAS: Tuple[Persona, ...] = (
    Persona(
        name="user",
        role="someone who has the problem this product says it solves",
        cares_about="whether it works for them on the first try, how long it takes to "
                    "get value, and whether it beats what they use today; you must try "
                    "it, not read about it — the walkthrough below is what happened "
                    "when you did",
        decision="Would you use this instead of what you use today?",
    ),
    Persona(
        name="client",
        role="a paying buyer deciding whether to spend money on this",
        cares_about="trust, onboarding, support, what is missing before you would buy, "
                    "and what you would pay",
        decision="Would you pay for this — and if so, how much?",
    ),
    Persona(
        name="investor",
        role="an early-stage investor",
        cares_about="who has the problem, how many of them there are, why now, the "
                    "alternatives and what makes this defensible, the business model, "
                    "and the reason you would say no",
        decision="Would you invest?",
    ),
    Persona(
        name="judge",
        role="a demo-day judge",
        cares_about="originality, execution, impact and clarity — score each 1-10 in "
                    "`scores`, and say whether it wins in `win`",
        decision="Does this win?",
    ),
    Persona(
        name="skeptic",
        role="a skeptic who has seen many products like this",
        cares_about="the existing alternatives by kind (never by brand), and why "
                    "someone would stay with them rather than switch",
        decision="Would people switch to this from what they already use?",
    ),
)

PERSONA_KEYS = ("name", "role", "cares_about", "decision")
CONFIG_KEYS = ("personas", "max_convenings")


@dataclass
class JuryConfig:
    personas: List[Persona]
    max_convenings: int = DEFAULT_MAX_CONVENINGS
    source: str = ""

    def names(self) -> List[str]:
        return [p.name for p in self.personas]

    def get(self, name: str) -> Persona:
        for persona in self.personas:
            if persona.name == name:
                return persona
        raise JuryError("no juror called %r — the panel can be: %s"
                        % (name, ", ".join(self.names())))


def parse_config(data: Any, source: str = JURY_CONFIG) -> JuryConfig:
    """The panel from duet-jury.json. Unknown keys are an error, not ignored:
    a misspelt `cares_abuot` silently dropped is a persona nobody configured."""
    if not isinstance(data, dict):
        raise JuryError("%s must be a JSON object" % source)
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise JuryError("%s: unknown key%s %s (allowed: %s)"
                        % (source, "s" if len(unknown) > 1 else "", ", ".join(unknown),
                           ", ".join(CONFIG_KEYS)))
    personas = {p.name: p for p in DEFAULT_PERSONAS}
    raw = data.get("personas", [])
    if not isinstance(raw, list):
        raise JuryError("%s: \"personas\" must be a list" % source)
    for index, item in enumerate(raw):
        where = "%s: personas[%d]" % (source, index)
        if not isinstance(item, dict):
            raise JuryError("%s must be an object" % where)
        unknown = sorted(set(item) - set(PERSONA_KEYS))
        if unknown:
            raise JuryError("%s: unknown key%s %s (allowed: %s)"
                            % (where, "s" if len(unknown) > 1 else "", ", ".join(unknown),
                               ", ".join(PERSONA_KEYS)))
        for key in PERSONA_KEYS:
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise JuryError("%s: \"%s\" is required and must be non-empty text" % (where, key))
        name = item["name"].strip()
        if slugify(name, fallback="") != name:
            raise JuryError("%s: name %r must be lowercase letters, digits and dashes"
                            % (where, name))
        # A custom persona with a default's name replaces that default.
        personas[name] = Persona(name=name, role=item["role"].strip(),
                                 cares_about=item["cares_about"].strip(),
                                 decision=item["decision"].strip())
    cap = data.get("max_convenings", DEFAULT_MAX_CONVENINGS)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise JuryError("%s: \"max_convenings\" must be a whole number of at least 1" % source)
    return JuryConfig(personas=list(personas.values()), max_convenings=cap, source=source)


def load_config(root: str) -> JuryConfig:
    path = Path(root).expanduser().resolve() / JURY_CONFIG
    if not path.is_file():
        return JuryConfig(personas=list(DEFAULT_PERSONAS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JuryError("could not read %s: %s" % (JURY_CONFIG, exc))
    return parse_config(data, source=JURY_CONFIG)


def choose_panel(config: JuryConfig, panel: str = "") -> List[Persona]:
    """`--panel user,judge` → those personas, in that order; default: all of them."""
    if not str(panel or "").strip():
        return list(config.personas)
    chosen: List[Persona] = []
    for name in [n.strip() for n in str(panel).split(",") if n.strip()]:
        persona = config.get(name)
        if persona not in chosen:
            chosen.append(persona)
    return chosen


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------
def _files(root: Path) -> List[Path]:
    return Workspace(str(root)).tracked_files()


def detect_stage(root: str, idea: str = "") -> str:
    """idea → plan → build, read off the workspace. `launch` is only ever asked for.

    - idea: an idea was given and the workspace holds nothing yet;
    - plan: PLAN.md exists and there is little code beside it;
    - build: everything else.
    """
    base = Path(root).expanduser().resolve()
    files = _files(base) if base.is_dir() else []
    if not files and idea.strip():
        return "idea"
    if (base / "PLAN.md").is_file():
        source = [p for p in files if p.suffix.lower() in SOURCE_SUFFIXES]
        if len(source) <= PLAN_STAGE_MAX_SOURCE_FILES:
            return "plan"
    return "build"


# --------------------------------------------------------------------------
# the user's walkthrough
# --------------------------------------------------------------------------
@dataclass
class Walkthrough:
    command: str = ""
    ran: bool = False
    exit_code: Optional[int] = None
    timed_out: bool = False
    output: str = ""
    note: str = ""
    shots: List[str] = field(default_factory=list)

    def transcript(self) -> str:
        """What is recorded, and what a walkthrough quote is checked against."""
        lines: List[str] = []
        if self.command:
            lines.append("$ %s" % self.command)
        if self.output:
            lines.append(self.output.rstrip())
        if self.ran:
            if self.timed_out:
                lines.append("[still running after the time limit — stopped]")
            else:
                lines.append("[exit %s]" % self.exit_code)
        if self.note:
            lines.append("[%s]" % self.note)
        for shot in self.shots:
            lines.append("[screenshot: %s]" % shot)
        return "\n".join(lines) or "(no walkthrough)"


def run_command_for(root: str) -> str:
    """The README's "How to run it" command, else one the project declares."""
    from duet.build import run_command

    command = run_command(root)
    if command:
        return command
    package = Path(root) / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict) and scripts.get("start"):
            return "npm start"
    return ""


def _ignore(directory: str, names: List[str]) -> List[str]:
    return [n for n in names if n in SKIP_DIRS]


def walkthrough(root: str, command: str, timeout: int = WALKTHROUGH_TIMEOUT) -> Walkthrough:
    """Run `command` in a throwaway copy of the workspace, never in the workspace.

    The user juror is supposed to try the product. Trying it can write files,
    and a juror is read-only — so it tries a copy, which is deleted afterwards
    whatever happened.
    """
    result = Walkthrough(command=command)
    if not command.strip():
        result.note = ("no run command found: README.md has no \"How to run it\" section "
                       "with a command in it, and the project declares none")
        return result
    scratch = tempfile.mkdtemp(prefix="duet-jury-")
    copy = os.path.join(scratch, "workspace")
    try:
        shutil.copytree(str(Path(root).expanduser().resolve()), copy,
                        symlinks=True, ignore=_ignore)
        proc = subprocess.Popen(
            command, cwd=copy, shell=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        result.ran = True
        try:
            out, _ = proc.communicate(timeout=timeout)
            result.exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            result.timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            out, _ = proc.communicate()
        text = (out or b"").decode("utf-8", "replace")
        if len(text) > WALKTHROUGH_CHARS:
            text = text[:WALKTHROUGH_CHARS] + "\n...[output cut off at %d chars]..." % WALKTHROUGH_CHARS
        result.output = text
    except (OSError, shutil.Error) as exc:
        result.note = "could not run it: %s" % exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return result


def look_at_site(root: str, out_dir: str) -> Walkthrough:
    """For a website: render it in a real browser, the way `duet page` does."""
    from duet import gate as gate_detect
    from duet import page as page_check

    found = gate_detect.page_gate(root)
    result = Walkthrough()
    if not found:
        return result
    target = found.split(" page check ", 1)[1]
    result.command = "duet page check %s && duet page shot %s" % (target, target)
    try:
        faults = page_check.check_page(target, page_check.DEFAULT_WIDTHS, root=root)
        result.shots = page_check.shoot_page(target, page_check.DEFAULT_WIDTHS, out_dir, root=root)
        result.ran = True
        result.exit_code = 1 if faults else 0
        result.output = "\n".join(faults) or "no faults found"
    except Exception as exc:     # no Chrome, an unsupported platform: say so
        result.note = "could not render the page: %s" % exc
    return result


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------
def normalise(text: str) -> str:
    return " ".join(str(text or "").split())


@dataclass
class Objection:
    id: str
    title: str
    severity: str
    detail: str = ""
    path: str = ""          # the workspace file quoted, or "" for the walkthrough
    quote: str = ""
    walkthrough: bool = False
    verified: bool = False
    why_unverified: str = ""

    def where(self) -> str:
        return "the user walkthrough" if self.walkthrough else (self.path or "(nothing cited)")


def verify(objection: Objection, root: str, walkthrough_text: str = "") -> Objection:
    """Mark the objection verified only if its quote is really where it says."""
    quote = normalise(objection.quote)
    objection.verified = False
    if len(quote) < MIN_QUOTE_CHARS:
        objection.why_unverified = "no quote, or one too short to prove anything"
        return objection
    if objection.walkthrough:
        if quote in normalise(walkthrough_text):
            objection.verified = True
        else:
            objection.why_unverified = "that text is not in the recorded walkthrough output"
        return objection
    if not objection.path:
        objection.why_unverified = "no file cited"
        return objection
    body = Workspace(root).read_or_none(objection.path, limit=EVIDENCE_READ_LIMIT)
    if body is None:
        objection.why_unverified = "%s is not a readable file in this workspace" % objection.path
    elif quote in normalise(body):
        objection.verified = True
    else:
        objection.why_unverified = "that text is not in %s" % objection.path
    return objection


# --------------------------------------------------------------------------
# one juror
# --------------------------------------------------------------------------
@dataclass
class Verdict:
    persona: Persona
    agent: str = ""
    backend: str = ""
    read_only: str = ""
    answered: bool = False
    error: str = ""
    decision: str = ""
    score: Optional[int] = None
    message: str = ""
    price: str = ""
    scores: Dict[str, int] = field(default_factory=dict)
    win: Optional[bool] = None
    objections: List[Objection] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def verified(self, severity: str = "") -> List[Objection]:
        return [o for o in self.objections if o.verified and (not severity or o.severity == severity)]


def _int_1_10(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 10 and number == value else None


def parse_verdict(text: str, persona: Persona, root: str, walkthrough_text: str = "") -> Verdict:
    """Read a juror's envelope: the last JSON object in its reply with a decision."""
    verdict = Verdict(persona=persona)
    found = [obj for obj, _ in iter_json_objects(text or "") if "decision" in obj]
    if not found:
        verdict.error = "replied without a JSON envelope carrying a decision"
        verdict.message = (text or "").strip()[:2000]
        return verdict
    data = found[-1]
    verdict.answered = True
    verdict.message = str(data.get("message") or "").strip()
    decision = str(data.get("decision") or "").strip().lower()
    if decision in DECISIONS:
        verdict.decision = decision
    else:
        verdict.notes.append("decision %r is not one of %s" % (data.get("decision"), "/".join(DECISIONS)))
    verdict.score = _int_1_10(data.get("score"))
    if verdict.score is None:
        verdict.notes.append("score %r is not a whole number from 1 to 10" % (data.get("score"),))
    verdict.price = str(data.get("price") or "").strip()
    scores = data.get("scores")
    if isinstance(scores, dict):
        for key in JUDGE_CRITERIA:
            value = _int_1_10(scores.get(key))
            if value is not None:
                verdict.scores[key] = value
    if isinstance(data.get("win"), bool):
        verdict.win = data["win"]
    raw = data.get("objections") or []
    if not isinstance(raw, list):
        raw = []
        verdict.notes.append("\"objections\" was not a list")
    seen = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        oid = slugify(item.get("id") or title, fallback="objection-%d" % (index + 1))
        while oid in seen:
            oid += "-%d" % (index + 1)
        seen.add(oid)
        severity = str(item.get("severity") or "major").strip().lower()
        if severity not in SEVERITIES:
            severity = "major"
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        objection = Objection(
            id=oid, title=title or oid, severity=severity,
            detail=str(item.get("detail") or "").strip(),
            path=str(evidence.get("path") or "").strip(),
            quote=str(evidence.get("quote") or ""),
            walkthrough=bool(evidence.get("walkthrough")),
        )
        verdict.objections.append(verify(objection, root, walkthrough_text))
    return verdict


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------
JUROR_SYSTEM = """\
You are {display}, sitting on a simulated stakeholder panel as `{persona}`.

You are {role}. You care about: {cares_about}.
Your one decision: {decision}

You are looking at a product in the workspace at {root}. You get one pass, and
you may not change anything in it: this is a judgement, not an edit.

Judge it as that person would, not as an engineer would. Be specific and be
fair: a real strength is worth saying, and inventing a problem to look tough is
as useless as waving through a real one. Name kinds of alternatives, never real
products, companies or people.

Every objection must carry evidence the harness can check:
- a quote copied exactly from a file in the workspace, with its path:
  "evidence": {{"path": "README.md", "quote": "the exact words"}}
- or a quote copied exactly from the walkthrough output shown to you:
  "evidence": {{"walkthrough": true, "quote": "the exact words"}}
The harness checks every quote. One that is not really there is shown as
unverified and does not count. For something missing, quote the place that
shows the gap (the README section that never mentions it, the error the
walkthrough printed).

End your reply with exactly one fenced JSON envelope:

```json
{{
  "message": "your judgement in a few sentences, in your own voice",
  "decision": "yes | no | maybe",
  "score": 1,
  "objections": [
    {{"id": "kebab-case-id", "title": "one line", "severity": "blocker|major|minor",
     "detail": "why it matters to you and what would change your mind",
     "evidence": {{"path": "README.md", "quote": "exact words"}}}}
  ]{extra}
}}
```

- `score` is a whole number from 1 to 10.
- `blocker`: you would walk away over this; `major`: it would have to be fixed
  before you said yes; `minor`: worth fixing, not deciding.
"""

EXTRA_FIELDS = {
    "client": ',\n  "price": "what you would pay, and per what — or nothing"',
    "judge": (',\n  "scores": {"originality": 1, "execution": 1, "impact": 1, "clarity": 1},'
              '\n  "win": false'),
}

STAGE_NOTES = {
    "idea": "This is an idea. Nothing is built yet; judge whether it is worth building.",
    "plan": "This is a plan. Judge whether what it plans would be worth having, and what it leaves out.",
    "build": "This is a build in progress. Judge what exists now, as it runs now.",
    "launch": "This is a launch. Judge it as it would be put in front of people: the README, "
              "any landing page, and the pricing are part of the product now.",
}


def _material(root: str, stage: str) -> Tuple[str, Dict[str, str]]:
    """(file listing, file bodies) a juror at this stage is shown."""
    ws = Workspace(root)
    listing = ws.tree(limit=6000, max_entries=200)
    files = [p.relative_to(ws.root).as_posix() for p in ws.tracked_files()]
    wanted: List[str] = []
    first = ["README.md", "PLAN.md"]
    if stage == "launch":
        first += [f for f in files if Path(f).name.lower() in ("index.html", "landing.html")]
        first += [f for f in files if "pricing" in f.lower()]
    for rel in first:
        if rel in files and rel not in wanted:
            wanted.append(rel)
    if stage in ("build", "launch"):
        wanted += [f for f in files if Path(f).suffix.lower() in SOURCE_SUFFIXES
                   and f not in wanted and not f.startswith("tests/")]
    bodies: Dict[str, str] = {}
    budget = FILE_BUDGET
    for rel in wanted:
        if len(bodies) >= FILE_COUNT or budget <= 0:
            break
        allowed = min(FILE_CHARS, budget)
        body = ws.read_or_none(rel, limit=allowed + 1)
        if body is None:
            continue
        if len(body) > allowed:
            body = body[:allowed] + "\n...[%s cut off at %d chars]..." % (rel, allowed)
        budget -= len(body)
        bodies[rel] = body
    return listing, bodies


def juror_prompt(persona: Persona, stage: str, idea: str, listing: str,
                 bodies: Dict[str, str], walk: Optional[Walkthrough]) -> str:
    parts = ["=== YOU ARE ON THE PANEL AS `%s` — STAGE: %s ===\n%s"
             % (persona.name, stage, STAGE_NOTES.get(stage, ""))]
    if idea.strip():
        parts.append("\n=== THE IDEA, AS ITS AUTHORS PUT IT ===\n%s" % idea.strip())
    parts.append("\n=== WHAT IS IN THE WORKSPACE ===\n%s" % listing)
    for path, body in bodies.items():
        parts.append("\n=== FILE: %s ===\n%s" % (path, body))
    if walk is not None:
        parts.append(
            "\n=== YOUR WALKTHROUGH (run by the harness in a throwaway copy, not by the "
            "authors) ===\n%s" % walk.transcript()
        )
        if walk.shots:
            parts.append("Look at the screenshots above before you judge the page.")
    parts.append("\n=== YOUR JUDGEMENT ===\n%s\nAnswer it, then end with the envelope."
                 % persona.decision)
    return "\n".join(parts)


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------
@dataclass
class Panel:
    stage: str
    verdicts: List[Verdict]
    walkthrough: Optional[Walkthrough] = None
    path: str = ""

    @property
    def answered(self) -> List[Verdict]:
        return [v for v in self.verdicts if v.answered]

    def blockers(self) -> List[Tuple[Verdict, Objection]]:
        return [(v, o) for v in self.verdicts for o in v.verified("blocker")]

    def exit_code(self) -> int:
        if not self.answered:
            return JURY_FAILED
        return JURY_BLOCKED if self.blockers() else JURY_CLEAR

    def top_objections(self, n: int = 3) -> List[Tuple[Verdict, Objection]]:
        found = [(v, o) for v in self.verdicts for o in v.verified()]
        found.sort(key=lambda pair: SEVERITIES.index(pair[1].severity))
        return found[:n]

    def judge_score(self) -> Optional[float]:
        scores: List[int] = []
        for v in self.verdicts:
            if v.persona.name == "judge" and v.answered:
                scores += list(v.scores.values()) or ([v.score] if v.score else [])
        return round(sum(scores) / float(len(scores)), 1) if scores else None

    def summary_line(self) -> str:
        blockers = len(self.blockers())
        decided = ", ".join("%s %s" % (v.persona.name, v.decision or "?") for v in self.answered)
        return "%s stage; %s; %d verified blocker%s" % (
            self.stage, decided or "no juror answered", blockers, "" if blockers == 1 else "s")


def _decision_of(panel: Panel, name: str) -> str:
    for v in panel.verdicts:
        if v.persona.name == name:
            if not v.answered:
                return "no answer (%s)" % (v.error or "failed")
            extra = " — would pay: %s" % v.price if v.price else ""
            return "%s (score %s)%s" % (v.decision or "unclear", v.score or "?", extra)
    return "not on this panel"


def render(panel: Panel) -> str:
    lines = ["# Jury — %s stage" % panel.stage, "", "> " + DISCLAIMER, ""]
    lines += ["## Panel summary", ""]
    lines.append("- **Would use it (user):** %s" % _decision_of(panel, "user"))
    lines.append("- **Would pay (client):** %s" % _decision_of(panel, "client"))
    lines.append("- **Would invest (investor):** %s" % _decision_of(panel, "investor"))
    score = panel.judge_score()
    judge = [v for v in panel.verdicts if v.persona.name == "judge" and v.answered]
    lines.append("- **Judges' score:** %s%s" % (
        "%s/10" % score if score is not None else "not scored",
        "" if not judge or judge[0].win is None else (" — %s" % ("win" if judge[0].win else "no win"))))
    blockers = panel.blockers()
    lines.append("- **Verified blockers:** %d" % len(blockers))
    lines += ["", "### The three objections most worth fixing", ""]
    top = panel.top_objections()
    if not top:
        lines.append("None verified.")
    for verdict, objection in top:
        lines.append("1. **%s** (%s, from %s) — %s" % (objection.title, objection.severity,
                                                         verdict.persona.name, objection.detail))
    for verdict in panel.verdicts:
        lines += ["", "## %s — %s" % (verdict.persona.name, verdict.persona.role), ""]
        lines.append("_%s_" % verdict.persona.decision)
        lines.append("")
        lines.append("Sat as %s%s." % (verdict.agent or "?", ", " + verdict.read_only if verdict.read_only else ""))
        if not verdict.answered:
            lines += ["", "**Did not answer:** %s" % (verdict.error or "no reply")]
            continue
        lines.append("")
        lines.append("**Decision:** %s  **Score:** %s/10" % (verdict.decision or "unclear",
                                                             verdict.score or "?"))
        if verdict.price:
            lines.append("**Would pay:** %s" % verdict.price)
        if verdict.scores:
            lines.append("**Scores:** " + ", ".join("%s %d" % (k, verdict.scores[k])
                                                    for k in JUDGE_CRITERIA if k in verdict.scores))
        if verdict.win is not None:
            lines.append("**Win:** %s" % ("yes" if verdict.win else "no"))
        if verdict.message:
            lines += ["", verdict.message]
        if verdict.objections:
            lines += ["", "**Objections:**", ""]
        for o in verdict.objections:
            status = "verified" if o.verified else "UNVERIFIED — %s; not counted" % o.why_unverified
            lines.append("- `%s` **%s** (%s) — %s" % (o.id, o.title, o.severity, status))
            if o.detail:
                lines.append("  %s" % o.detail)
            lines.append("  > %s — %s" % (normalise(o.quote)[:300] or "(no quote)", o.where()))
        for note in verdict.notes:
            lines.append("- note: %s" % note)
    lines += ["", "## User walkthrough", ""]
    if panel.walkthrough is None:
        lines.append("Not run at the %s stage — there is nothing to run yet." % panel.stage)
    else:
        lines += ["```", panel.walkthrough.transcript(), "```"]
    lines += ["", "---", "", DISCLAIMER, ""]
    return "\n".join(lines)


AdapterFactory = Callable[[str], Any]

QUESTIONS_PROMPT = """\
=== A ROUND OF QUESTIONS ===
The rest of the panel has spoken. Their verdicts, and the objections of theirs the
harness could verify:

{others}

Question them as yourself: press on what they found if it matters to you, say
where you think they are wrong, and change your own verdict if they changed your
mind. Your question is still: {decision}

Send your whole verdict again — every objection you still hold, with its evidence
— and end with the envelope. This one replaces your last.
"""


def _brief(verdict: Verdict) -> str:
    if not verdict.answered:
        return "- %s: no answer" % verdict.persona.name
    lines = ["- %s: %s, score %s/10%s" % (
        verdict.persona.name, verdict.decision or "unclear", verdict.score or "?",
        " — " + normalise(verdict.message)[:300] if verdict.message else "")]
    for o in verdict.verified():
        lines.append("    [%s] %s — \"%s\" (%s)" % (o.severity, o.title,
                                                   normalise(o.quote)[:160], o.where()))
    return "\n".join(lines)


def convene(
    *,
    root: str,
    agents: Sequence[Any],
    panel: Sequence[Persona],
    stage: str,
    idea: str = "",
    out: str = "",
    make_adapter: AdapterFactory,
    timeout: int = WALKTHROUGH_TIMEOUT,
    shots_dir: str = "",
    rounds_of_questions: int = 0,
) -> Panel:
    """Sit the panel once: one read-only pass per juror, alternating agents."""
    root = str(Path(root).expanduser().resolve())
    ws = Workspace(root)
    listing, bodies = _material(root, stage)
    walk: Optional[Walkthrough] = None
    if stage in ("build", "launch") and any(p.name == "user" for p in panel):
        command = run_command_for(root)
        walk = walkthrough(root, command, timeout=timeout) if command else None
        site = look_at_site(root, shots_dir or os.path.join(root, ".duet", "jury-shots"))
        if walk is None:
            walk = site if site.command else walkthrough(root, "", timeout=timeout)
        elif site.command:
            walk.shots = site.shots
            walk.output += "\n$ %s\n%s" % (site.command, site.output)
    walk_text = walk.transcript() if walk else ""

    def sit(adapter: Any, persona: Persona, agent: str, enforced: str,
            prompt: str, system: str, round_no: int) -> Verdict:
        before = ws.fingerprint()
        reply = adapter.send(prompt, system=system, round_no=round_no)
        touched = changed_paths(before, ws.fingerprint())
        if not reply.ok and not reply.text.strip():
            verdict = Verdict(persona=persona, error=reply.error or "no reply")
        else:
            verdict = parse_verdict(reply.text, persona, root, walk_text)
        verdict.agent = agent
        verdict.backend = getattr(adapter, "backend", "")
        verdict.read_only = enforced
        if touched:
            # A juror is held read-only. One whose pass changed the workspace is
            # judging something that no longer exists, and does not count.
            verdict.answered = False
            verdict.error = ("the workspace changed during this juror's pass (%s), so "
                             "its verdict is not counted" % ", ".join(touched[:8]))
        return verdict

    verdicts: List[Verdict] = []
    seated: List[Tuple[Any, str, str, str]] = []     # adapter, agent, enforced, system
    for index, persona in enumerate(panel):
        agent = agents[index % len(agents)] if agents else ""
        try:
            adapter = make_adapter(agent)
        except (SystemExit, ValueError, KeyError) as exc:
            verdicts.append(Verdict(persona=persona, agent=agent, error=str(exc)))
            seated.append((None, agent, "", ""))
            continue
        enforced = adapter.read_only()
        system = JUROR_SYSTEM.format(
            display=adapter.display, persona=persona.name, role=persona.role,
            cares_about=persona.cares_about, decision=persona.decision, root=root,
            extra=EXTRA_FIELDS.get(persona.name, ""),
        )
        prompt = juror_prompt(persona, stage, idea, listing, bodies,
                              walk if persona.name == "user" else None)
        verdicts.append(sit(adapter, persona, agent, enforced, prompt, system, 1))
        seated.append((adapter, agent, enforced, system))

    # Rounds of questions: each juror sees what the others found and may press
    # on it, concede it, or change its own mind. Its last readable verdict counts.
    for round_no in range(2, 2 + max(0, int(rounds_of_questions or 0))):
        for index, persona in enumerate(panel):
            adapter, agent, enforced, system = seated[index]
            if adapter is None or not verdicts[index].answered:
                continue
            others = "\n".join(_brief(v) for i, v in enumerate(verdicts) if i != index)
            revised = sit(adapter, persona, agent, enforced,
                          QUESTIONS_PROMPT.format(others=others or "(nobody else sat)",
                                                  decision=persona.decision),
                          system, round_no)
            if revised.answered:
                verdicts[index] = revised
            else:
                verdicts[index].notes.append("round %d of questions: %s"
                                             % (round_no - 1, revised.error))

    result = Panel(stage=stage, verdicts=verdicts, walkthrough=walk)
    if out:
        path = Path(out).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render(result), encoding="utf-8")
            result.path = str(path)
        except OSError:
            result.path = ""
    return result


# --------------------------------------------------------------------------
# `duet jury`
# --------------------------------------------------------------------------
def adapter_factory(cfg: Any) -> AdapterFactory:
    """Fresh adapters, one per juror, from the session's own agent specs.

    Fresh, not the pair's: a juror must not inherit an author's thread with its
    backend, and each juror's verdict is its own.
    """
    from duet.adapters import build as build_adapter

    def make(name: str) -> Any:
        spec = cfg.agent(name)
        adapter = build_adapter(spec.backend, name=name, cwd=cfg.root,
                                model=spec.model, config=dict(spec.options))
        if cfg.turn_timeout:
            adapter.set_turn_timeout(cfg.turn_timeout)
        return adapter

    return make


def run(args: Any) -> int:
    """`duet jury` — exit 0 with no verified blocker, 1 with one, 3 if it cannot run."""
    import time

    from duet import cli, ui

    def fail(message: str) -> int:
        print(ui.red("✗ ") + message)
        return JURY_FAILED

    idea = (getattr(args, "idea", "") or "").strip()
    try:
        args.gate, args.no_gate = None, True     # a jury runs no gate
        cfg = cli.build_config(args)
        config = load_config(cfg.root)
        panel = choose_panel(config, getattr(args, "panel", "") or "")
    except (SystemExit, ValueError, JuryError) as exc:
        return fail(str(exc))
    if not panel:
        return fail("the panel is empty")
    stage = getattr(args, "stage", "") or detect_stage(cfg.root, idea)
    if stage == "idea" and not idea:
        return fail("the idea stage needs the idea: pass --idea \"...\"")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = Path(cfg.root) / ".duet" / "jury" / stamp
    out = getattr(args, "out", "") or str(folder / JURY_FILE)

    print(ui.bold("duet jury") + "  " + ui.dim(cfg.root))
    print("  %s %s" % (ui.dim("stage:"), stage))
    print("  %s %s" % (ui.dim("panel:"), ", ".join(p.name for p in panel)))
    print(ui.dim("  " + DISCLAIMER))
    print(ui.dim("  the panel is sitting..."), flush=True)

    result = convene(root=cfg.root, agents=cfg.order(), panel=panel, stage=stage,
                     idea=idea, out=out, make_adapter=adapter_factory(cfg),
                     shots_dir=str(folder / "shots"),
                     rounds_of_questions=getattr(args, "rounds_of_questions", 0) or 0)
    print()
    for verdict in result.verdicts:
        if not verdict.answered:
            print("  %-10s %s" % (verdict.persona.name, ui.red("no answer: " + verdict.error)))
            continue
        counted = verdict.verified()
        print("  %-10s %-5s %s/10  %s" % (
            verdict.persona.name, verdict.decision or "?", verdict.score or "?",
            ui.dim("%d objection(s), %d verified" % (len(verdict.objections), len(counted)))))
    print()
    for verdict, objection in result.blockers():
        print(ui.red("blocker ") + "%s %s" % (ui.dim("[jury:%s]" % verdict.persona.name),
                                             objection.title))
    if result.path:
        print(ui.dim("report: ") + result.path)
    code = result.exit_code()
    if code == JURY_FAILED:
        return fail("no juror gave a readable verdict, so the panel has nothing to say")
    print(ui.red("verified blockers remain.") if code == JURY_BLOCKED
          else ui.green("no verified blocker."))
    return code
