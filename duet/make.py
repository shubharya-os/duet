# Adapted from replica-skill (https://github.com/Jakeschincariol/replica-skill):
# the recon → plan → build → parity → sweep pipeline, the features.csv ledger
# and its weighting, and the clean-room sweep.
#
# MIT License
#
# Copyright (c) 2026 Jake Schincariol
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""`duet make "<what to build>" [--like URL_OR_APP ...]`.

From an idea, and the reference apps the user has in mind: research what exists,
plan, build, score parity against the references, and sweep the result clean of
them. Each stage reads what the one before wrote under `make/`, so any of them
can be run on its own with `--stage`.

What the agents say about parity is not what is scored. The harness reads
make/features.csv itself: a source has to be a public URL, a "done" row has to
point at a test or a file that exists, and the sweep greps the product for the
references' names, domains and colours. Each of those is a check, not a request.
"""

from __future__ import annotations

import copy
import csv
import io
import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from duet import ui
from duet.workspace import SKIP_DIRS as WORKSPACE_SKIP_DIRS

STAGES = ("recon", "plan", "build", "parity", "sweep")
MAKE_DIR = "make"
RECON_FILE = "recon.md"
FEATURES_FILE = "features.csv"
PARITY_FILE = "parity.md"
BRAND_FILE = "brand.json"

COLUMNS = ("feature", "area", "priority", "source", "status", "evidence", "notes")
PRIORITIES = ("must", "should", "could")
STATUSES = ("todo", "done", "partial", "skip")
WEIGHTS = {"must": 3, "should": 2, "could": 1}
CREDIT = {"done": 1.0, "partial": 0.5, "todo": 0.0}
# A row the references do not have: something the idea needs that none of them
# does. Its notes start with this, and it is listed rather than scored — parity
# with the references cannot be raised or lowered by features they lack.
EXTRA_MARK = "extra:"

MAKE_OK = 0
MAKE_NOT_DONE = 1      # not shippable, sweep dirty, a stage's check failed
MAKE_USAGE = 2
MAKE_CANNOT = 3        # a stage's input is missing, or a backend could not answer

FINE_PRINT = """\
The fine print. duet make rebuilds what an app does, never what it owns: its
features and flows, clean-room. Never its source code, logos, trademarks, copy,
content or private APIs. Research uses public sources and your own account only
— never behind someone else's login, a paywall, or against a site's terms.
Rebrand before launch. This is not legal advice."""

JURY_POINTER = ("what the references' users complain about is a question for "
                "`duet jury`, not for parity — run it on the result.")

USAGE = 'duet make "a shared reading list for book clubs" --like https://bookly.example'


# --------------------------------------------------------------------------
# features.csv
# --------------------------------------------------------------------------
@dataclass
class Feature:
    feature: str
    area: str = ""
    priority: str = ""
    source: str = ""
    status: str = ""
    evidence: str = ""
    notes: str = ""
    line: int = 0
    problems: List[str] = field(default_factory=list)

    @property
    def counts(self) -> bool:
        """A row with a problem is reported, and does not count."""
        return not self.problems

    @property
    def extra(self) -> bool:
        return self.notes.strip().lower().startswith(EXTRA_MARK)

    @property
    def skipped(self) -> bool:
        """A skip only counts as one with a reason; a bare "skip" is a todo."""
        return self.status == "skip" and bool(self.notes.strip())


_PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")


def source_problem(url: str) -> str:
    """Why this is not a well-formed public http(s) source, or "" if it is.

    Only what can be told from the URL itself: the scheme, a real host name,
    no credentials and no private address. Whether the page sits behind a login
    is the research pass's to respect — the prompt says so — and not something
    a URL shows.
    """
    url = (url or "").strip()
    if not url:
        return "no source"
    if any(ch.isspace() for ch in url):
        return "source %r is not a URL" % url
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return "source %r is not a URL" % url
    if parsed.scheme not in ("http", "https"):
        return "source %r is not an http(s) URL" % url
    if not host:
        return "source %r has no host" % url
    if parsed.username or parsed.password:
        return "source %r carries credentials, so it is not a public page" % url
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return "source %r is a private address" % url
        return ""
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES) or "." not in host:
        return "source %r is not a public host" % url
    return ""


def parse_features(text: str) -> Tuple[List[Feature], List[str]]:
    """(rows, problems with the file as a whole).

    Every row is returned, the bad ones with their `problems` filled in, so a
    report can show what was dropped and why rather than silently losing it.
    """
    reader = csv.DictReader(io.StringIO(text or ""))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        return [], ["%s is missing the column(s): %s (it needs %s)"
                    % (FEATURES_FILE, ", ".join(missing), ",".join(COLUMNS))]
    rows: List[Feature] = []
    for number, raw in enumerate(reader, start=2):
        cells = {str(k).strip().lower(): (v or "").strip()
                 for k, v in raw.items() if k is not None and isinstance(v, str)}
        if not any(cells.values()):
            continue
        row = Feature(
            feature=cells.get("feature", ""), area=cells.get("area", ""),
            priority=cells.get("priority", "").lower(), source=cells.get("source", ""),
            status=cells.get("status", "").lower() or "todo",
            evidence=cells.get("evidence", ""), notes=cells.get("notes", ""), line=number,
        )
        if not row.feature:
            row.problems.append("no feature name")
        if row.priority not in PRIORITIES:
            row.problems.append("priority %r is not one of %s"
                                % (row.priority, "|".join(PRIORITIES)))
        if row.status not in STATUSES:
            row.problems.append("status %r is not one of %s" % (row.status, "|".join(STATUSES)))
        problem = source_problem(row.source)
        if problem:
            row.problems.append(problem)
        rows.append(row)
    return rows, []


def read_features(root: str) -> Tuple[List[Feature], List[str]]:
    path = make_dir(root) / FEATURES_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], ["%s/%s does not exist — run `duet make --stage recon` first"
                    % (MAKE_DIR, FEATURES_FILE)]
    return parse_features(text)


def must_haves(rows: Sequence[Feature]) -> List[Feature]:
    return [r for r in rows if r.counts and r.priority == "must" and not r.extra
            and not r.skipped]


# --------------------------------------------------------------------------
# parity
# --------------------------------------------------------------------------
def evidence_found(evidence: str, root: str, names: Optional[Sequence[str]] = None) -> str:
    """What in the workspace backs this evidence, or "" when nothing does.

    A test name that exists (as `test_x`, or `path::test_x` with the file
    holding it), or a file path that exists inside the workspace. make/ does
    not count: the ledger cannot be its own evidence.
    """
    from duet.workflows import test_names

    base = Path(root).expanduser().resolve()
    known = list(names) if names is not None else None
    for token in re.split(r"[\s,;]+", (evidence or "").strip()):
        token = token.strip("`'\"()[]")
        if not token:
            continue
        path_part, _, name = token.partition("::")
        candidate = (base / path_part).resolve()
        inside = candidate == base or base in candidate.parents
        if inside and candidate != base and candidate.is_file():
            rel = candidate.relative_to(base).parts
            if rel and rel[0] == MAKE_DIR:
                continue
            if not name:
                return path_part
            try:
                body = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            leaf = name.split("::")[-1].split("[")[0]
            if leaf and re.search(r"\b%s\b" % re.escape(leaf), body):
                return token
            continue
        if not name and re.match(r"^[A-Za-z_][\w.]*$", token):
            if known is None:
                known = test_names(root)
            if token.split(".")[-1] in known:
                return token
    return ""


@dataclass
class Parity:
    scored: List[Tuple[Feature, str]] = field(default_factory=list)   # (row, effective status)
    skipped: List[Feature] = field(default_factory=list)
    extra: List[Feature] = field(default_factory=list)
    rejected: List[Feature] = field(default_factory=list)
    demoted: List[Feature] = field(default_factory=list)   # done without checkable evidence
    problems: List[str] = field(default_factory=list)

    @property
    def earned(self) -> float:
        return sum(WEIGHTS[r.priority] * CREDIT[s] for r, s in self.scored)

    @property
    def possible(self) -> int:
        return sum(WEIGHTS[r.priority] for r, _ in self.scored)

    @property
    def percent(self) -> float:
        return round(100.0 * self.earned / self.possible, 1) if self.possible else 0.0

    @property
    def musts_left(self) -> List[Feature]:
        return [r for r, s in self.scored if r.priority == "must" and s != "done"]

    @property
    def shippable(self) -> bool:
        return not self.problems and bool(self.scored) and not self.musts_left


def score(rows: Sequence[Feature], root: str) -> Parity:
    """Weights must 3, should 2, could 1; done 1, partial 0.5, todo 0."""
    result = Parity()
    names: Optional[List[str]] = None
    for row in rows:
        if not row.counts:
            result.rejected.append(row)
            continue
        if row.extra:
            result.extra.append(row)
            continue
        if row.skipped:
            result.skipped.append(row)
            continue
        status = row.status if row.status in CREDIT else "todo"
        if status == "done":
            if names is None:
                from duet.workflows import test_names
                names = test_names(root)
            if not evidence_found(row.evidence, root, names):
                result.demoted.append(row)
                status = "todo"
        result.scored.append((row, status))
    return result


def render_parity(result: Parity) -> str:
    def fmt(value: float) -> str:
        return ("%.1f" % value).rstrip("0").rstrip(".")

    lines = ["# Parity", ""]
    verdict = ("shippable — every must-have is done" if result.shippable
               else "NOT shippable — %d must-have(s) not done" % len(result.musts_left)
               if result.scored else "NOT shippable — nothing to score")
    lines += ["**%s / %d (%s%%)** — %s" % (fmt(result.earned), result.possible,
                                             fmt(result.percent), verdict), ""]
    lines += ["Scored by the harness from make/features.csv: must 3, should 2, could 1;",
              "done 1, partial 0.5, todo 0. A done row counts only with evidence that",
              "exists in the workspace — a test name or a file path.", ""]
    for problem in result.problems:
        lines.append("- problem: %s" % problem)
    if result.scored:
        lines += ["| feature | priority | status | counted as | evidence |",
                  "|---|---|---|---|---|"]
        for row, status in result.scored:
            lines.append("| %s | %s | %s | %s | %s |" % (
                row.feature, row.priority, row.status, status, row.evidence or ""))
        lines.append("")
    if result.musts_left:
        lines += ["## Must-haves not done", ""]
        lines += ["- %s (%s)" % (r.feature, r.source) for r in result.musts_left]
        lines.append("")
    if result.demoted:
        lines += ["## Marked done, counted as todo", "",
                  "The evidence names no test or file that exists in the workspace.", ""]
        lines += ["- %s — evidence: %s" % (r.feature, r.evidence or "(none)") for r in result.demoted]
        lines.append("")
    if result.skipped:
        lines += ["## Skipped (not scored)", ""]
        lines += ["- %s — %s" % (r.feature, r.notes) for r in result.skipped]
        lines.append("")
    if result.extra:
        lines += ["## Not in the references (not scored)", ""]
        lines += ["- %s — %s" % (r.feature, r.notes) for r in result.extra]
        lines.append("")
    if result.rejected:
        lines += ["## Rows that do not count", ""]
        lines += ["- line %d: %s — %s" % (r.line, r.feature or "(no name)", "; ".join(r.problems))
                  for r in result.rejected]
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the clean-room sweep
# --------------------------------------------------------------------------
SWEEP_SKIP_DIRS = set(WORKSPACE_SKIP_DIRS) | {
    MAKE_DIR, "out", "coverage", ".turbo", ".svelte-kit", ".parcel-cache", ".gradle",
    ".cache", ".nuxt", ".output", "bower_components", "vendor",
}
LOCK_FILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "pipfile.lock", "cargo.lock", "gemfile.lock", "composer.lock",
    "go.sum", "bun.lockb", "uv.lock", "pdm.lock", "flake.lock",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".pdf",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".jar", ".war", ".class", ".so",
    ".dylib", ".dll", ".exe", ".o", ".a", ".pyc", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".mp3", ".mp4", ".mov", ".wav", ".ogg", ".webm", ".sqlite", ".db",
}
_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@dataclass
class Brand:
    names: List[str] = field(default_factory=list)
    domains: List[str] = field(default_factory=list)
    colors: List[str] = field(default_factory=list)     # #rrggbb, lower case

    def empty(self) -> bool:
        return not (self.names or self.domains or self.colors)

    def to_dict(self) -> Dict[str, List[str]]:
        return {"avoid": list(self.names), "domains": list(self.domains),
                "colors": list(self.colors)}


def _add(items: List[str], value: str) -> None:
    value = (value or "").strip()
    if value and value.lower() not in [i.lower() for i in items]:
        items.append(value)


def normalise_color(value: str) -> str:
    """"#ABC", "abc", "#aabbcc" → "#aabbcc"; anything else → ""."""
    match = _HEX.match((value or "").strip())
    if not match:
        return ""
    digits = match.group(1).lower()
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return "#" + digits


def name_from_domain(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(".")[0] if "." in host else host


def brand_from(likes: Sequence[str], data: Optional[Dict[str, Any]] = None) -> Brand:
    """The references' names, domains and colours, from --like and brand.json."""
    brand = Brand()
    for like in likes or ():
        like = (like or "").strip()
        if not like:
            continue
        candidate = like if "://" in like else ("https://" + like if re.match(
            r"^[\w-]+(\.[\w-]+)+(/.*)?$", like) else "")
        host = ""
        if candidate:
            try:
                host = (urlparse(candidate).hostname or "").lower()
            except ValueError:
                host = ""
        if host:
            domain = host[4:] if host.startswith("www.") else host
            _add(brand.domains, domain)
            _add(brand.names, name_from_domain(domain))
        else:
            _add(brand.names, like)
    data = data if isinstance(data, dict) else {}
    for name in data.get("avoid") or []:
        if isinstance(name, str):
            _add(brand.names, name)
    for domain in data.get("domains") or []:
        if isinstance(domain, str):
            domain = domain.strip().lower()
            _add(brand.domains, domain[4:] if domain.startswith("www.") else domain)
    for color in data.get("colors") or []:
        if isinstance(color, str) and normalise_color(color):
            _add(brand.colors, normalise_color(color))
    return brand


def read_brand(root: str) -> Tuple[Dict[str, Any], str]:
    """(make/brand.json as a dict, a problem reading it or "")."""
    path = make_dir(root) / BRAND_FILE
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, "%s/%s is not readable JSON: %s" % (MAKE_DIR, BRAND_FILE, exc)
    if not isinstance(data, dict):
        return {}, "%s/%s must be an object with avoid, domains and colors" % (MAKE_DIR, BRAND_FILE)
    return data, ""


def save_brand(root: str, brand: Brand) -> None:
    """Remember --like in make/brand.json, so a later --stage needs no --like."""
    folder = make_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / BRAND_FILE).write_text(json.dumps(brand.to_dict(), indent=2) + "\n",
                                     encoding="utf-8")


def _patterns(brand: Brand) -> List[Tuple[str, "re.Pattern[str]"]]:
    """(what it is, pattern) for each term — all case-insensitive.

    Names match as substrings on purpose: "BooklyShelf", "bookly_id" and
    "getBookly" all carry the name, and an identifier is exactly where a
    rebuild leaks it. A name of several words also matches with its spaces
    run together or replaced by _ or -.
    """
    out: List[Tuple[str, "re.Pattern[str]"]] = []
    for name in brand.names:
        words = [w for w in re.split(r"[\s_\-.]+", name.strip()) if w]
        if not words:
            continue
        joined = r"[\s_\-.]?".join(re.escape(w) for w in words)
        out.append(("name %r" % name, re.compile(joined, re.IGNORECASE)))
    for domain in brand.domains:
        out.append(("domain %r" % domain, re.compile(re.escape(domain), re.IGNORECASE)))
    for color in brand.colors:
        digits = color[1:]
        forms = [r"#" + digits, r"0x" + digits]
        if all(digits[i] == digits[i + 1] for i in (0, 2, 4)):
            forms.append(r"#" + digits[0] + digits[2] + digits[4])
        pattern = r"(?:%s)(?![0-9a-f])" % "|".join(forms)
        out.append(("colour %s" % color, re.compile(pattern, re.IGNORECASE)))
    return out


@dataclass
class Finding:
    path: str
    line: int          # 0 for a file name
    term: str
    text: str

    def where(self) -> str:
        return self.path if not self.line else "%s:%d" % (self.path, self.line)


def _skipped_file(rel: Path) -> bool:
    if SWEEP_SKIP_DIRS & set(rel.parts[:-1]):
        return True
    name = rel.name.lower()
    if name in LOCK_FILES or name.endswith(".lock"):
        return True
    return rel.suffix.lower() in BINARY_SUFFIXES


def sweep(root: str, brand: Brand) -> List[Finding]:
    """Every place a reference's name, domain or colour appears in the product."""
    base = Path(root).expanduser().resolve()
    patterns = _patterns(brand)
    findings: List[Finding] = []
    if not patterns or not base.is_dir():
        return findings
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if SWEEP_SKIP_DIRS & set(rel.parts):
            continue
        for term, pattern in patterns:
            if pattern.search(rel.name):
                findings.append(Finding(rel.as_posix(), 0, term, rel.name))
        if not path.is_file() or _skipped_file(rel):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for term, pattern in patterns:
                if pattern.search(line):
                    findings.append(Finding(rel.as_posix(), number, term, line.strip()[:160]))
    return findings


def sweep_veto(root: str, state: Dict[str, Any]) -> Optional[str]:
    """Why both sign-offs do not count while the sweep is dirty, or None.

    Set up by `duet make` for its build stage (state["clean_room"] is a
    brand.json-shaped dict), and asked by the orchestrator at consensus the way
    a workflow's veto is.
    """
    data = state.get("clean_room") if isinstance(state, dict) else None
    if not data:
        return None
    brand = brand_from((), data)
    findings = sweep(root, brand)
    if not findings:
        return None
    shown = "; ".join("%s (%s)" % (f.where(), f.term) for f in findings[:10])
    more = " and %d more" % (len(findings) - 10) if len(findings) > 10 else ""
    return ("The clean-room sweep is dirty: the references' names, domains or colours "
            "are still in the product — %s%s. Rename, re-word or re-colour them, "
            "file names and identifiers included. Both sign-offs are cleared until "
            "`duet make --stage sweep` is clean." % (shown, more))


# --------------------------------------------------------------------------
# recon
# --------------------------------------------------------------------------
RECON_SYSTEM = """\
You are the research pass of `duet make`, working read-only in {root}. You may
search and read the public web for this pass and nothing else. You do not write
files; the harness writes what you return."""

RECON_PROMPT = """\
{goal}

Public sources only: the product's own site, its help centre or docs, its
pricing page, its app-store listing, and public walkthroughs or reviews. Private,
login-only or paywalled content is out of bounds — do not sign in to anything,
do not get round a paywall, and do not use a page against its site's terms. Do
not copy source code, logos, copy or content; describe what the product does.

Return exactly these blocks, and nothing the harness should write outside them:

--- BEGIN make/recon.md ---
# Recon
(screens, flows, components, and an inferred data model — or, with no
references, the alternatives, what they lack, and what users of the idea need)
--- END make/recon.md ---

--- BEGIN make/features.csv ---
{columns}
(one row per feature: priority must|should|could; status todo; source the
public http(s) URL you saw it on — a row without one does not count; a feature
none of the references have gets notes starting "extra:")
--- END make/features.csv ---

--- BEGIN make/brand.json ---
{{"avoid": [names to keep out of the product], "domains": [their domains],
 "colors": [their brand colours as #rrggbb]}}
--- END make/brand.json ---

Then end with the JSON envelope, verdict DONE."""

_BLOCK = re.compile(r"^-{3}\s*BEGIN\s+make/([\w.\-]+)\s*-{3}\s*$(.*?)^-{3}\s*END\s+make/\1\s*-{3}\s*$",
                    re.MULTILINE | re.DOTALL)


def recon_goal(idea: str, likes: Sequence[str]) -> str:
    if likes:
        return ("Reverse-engineer these reference products, from public pages only, "
                "for this idea: %s\n\nReferences:\n%s\n\nFor each: its screens, its "
                "flows step by step, its components, and the data model those imply."
                % (idea, "\n".join("- " + like for like in likes)))
    return ("Research what already exists for this idea: %s\n\nThe alternatives people "
            "use now, what each lacks, and what users of the idea need from it." % idea)


def recon_blocks(text: str) -> Dict[str, str]:
    """{"recon.md": ..., "features.csv": ..., "brand.json": ...} as returned."""
    blocks: Dict[str, str] = {}
    for match in _BLOCK.finditer(text or ""):
        name = match.group(1)
        if name in (RECON_FILE, FEATURES_FILE, BRAND_FILE):
            body = match.group(2).strip("\n")
            # A model fond of fences may wrap a block's body in one.
            fenced = re.match(r"^```[\w.\-]*\n(.*)\n```\s*$", body.strip(), re.DOTALL)
            blocks[name] = (fenced.group(1) if fenced else body).strip() + "\n"
    return blocks


def grant_web(adapter: Any) -> str:
    """Give the research pass web search and fetch — this pass only.

    Every other stage builds its adapters fresh through `duet run`, which never
    calls this, so they keep duet's usual permissions.
    """
    allow = getattr(adapter, "allow_web", None)
    return allow() if callable(allow) else ""


def run_recon(root: str, idea: str, likes: Sequence[str], make_adapter: Any,
              agent: str) -> int:
    from duet.workspace import Workspace, changed_paths

    try:
        adapter = make_adapter(agent)
    except (SystemExit, ValueError, KeyError) as exc:
        print(ui.red("✗ ") + "the research pass could not start: %s" % exc)
        return MAKE_CANNOT
    enforced = adapter.read_only()
    web = grant_web(adapter)
    print(ui.dim("  recon  ") + "%s — %s; %s" % (
        agent, enforced or "not enforced read-only", web or "no web tools on this backend"))
    ws = Workspace(root)
    before = ws.fingerprint()
    prompt = RECON_PROMPT.format(goal=recon_goal(idea, likes), columns=",".join(COLUMNS))
    reply = adapter.send(prompt, system=RECON_SYSTEM.format(root=root), round_no=1)
    touched = changed_paths(before, ws.fingerprint())
    if touched:
        print(ui.red("✗ ") + "the research pass changed the workspace (%s); it is "
              "read-only, so nothing it returned is used" % ", ".join(touched[:8]))
        return MAKE_NOT_DONE
    blocks = recon_blocks(reply.text)
    if not reply.ok and not blocks:
        print(ui.red("✗ ") + "the research pass did not answer: %s" % (reply.error or "no reply"))
        return MAKE_CANNOT
    missing = [n for n in (RECON_FILE, FEATURES_FILE) if n not in blocks]
    if missing:
        print(ui.red("✗ ") + "the research pass returned no %s" % " or ".join(missing))
        return MAKE_NOT_DONE
    folder = make_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / RECON_FILE).write_text(blocks[RECON_FILE], encoding="utf-8")
    (folder / FEATURES_FILE).write_text(blocks[FEATURES_FILE], encoding="utf-8")
    found: Dict[str, Any] = {}
    if BRAND_FILE in blocks:
        try:
            found = json.loads(blocks[BRAND_FILE])
        except ValueError:
            found = {}
    existing, _problem = read_brand(root)
    merged = brand_from(likes, existing)
    extra = brand_from((), found if isinstance(found, dict) else {})
    for name in extra.names:
        _add(merged.names, name)
    for domain in extra.domains:
        _add(merged.domains, domain)
    for color in extra.colors:
        _add(merged.colors, color)
    if not merged.empty():
        save_brand(root, merged)
    return check_features(root)


def check_features(root: str) -> int:
    """Report the rows that do not count. Exit 1 when none do."""
    rows, problems = read_features(root)
    for problem in problems:
        print(ui.red("✗ ") + problem)
    if problems:
        return MAKE_NOT_DONE
    bad = [r for r in rows if not r.counts]
    for row in bad:
        print(ui.yellow("  dropped ") + "line %d %s — %s" % (
            row.line, row.feature or "(no name)", "; ".join(row.problems)))
    good = [r for r in rows if r.counts]
    print(ui.dim("  recon  ") + "%d feature(s), %d must-have(s); %d row(s) do not count"
          % (len(good), len(must_haves(rows)), len(bad)))
    if not good:
        print(ui.red("✗ ") + "no row in %s/%s has a public source, so there is nothing to build to"
              % (MAKE_DIR, FEATURES_FILE))
        return MAKE_NOT_DONE
    return MAKE_OK


# --------------------------------------------------------------------------
# plan and build: duet's own sessions, given the ledger
# --------------------------------------------------------------------------
def ledger_context(root: str) -> str:
    folder = make_dir(root)
    parts = []
    for name in (RECON_FILE, FEATURES_FILE):
        try:
            parts.append("make/%s:\n\n%s" % (name, (folder / name).read_text(encoding="utf-8")))
        except OSError:
            continue
    return "\n\n".join(parts)


def acceptance_for(rows: Sequence[Feature]) -> str:
    musts = must_haves(rows)
    lines = ["Every must-have in make/features.csv is built, each with a test that "
             "exercises it, and its row set to done with that test's name (or the "
             "file that implements it) in the evidence column:"]
    lines += ["- %s%s" % (r.feature, " (%s)" % r.area if r.area else "") for r in musts]
    lines.append("Nothing of the references' own — names, domains, colours, copy, "
                 "logos — appears in the product; `duet make --stage sweep` must be clean.")
    return "\n".join(lines)


def has_code(root: str) -> bool:
    """Is there a product here already, beyond the ledger and the plan?"""
    base = Path(root).expanduser().resolve()
    for path in base.iterdir() if base.is_dir() else ():
        if path.name.startswith(".") or path.name in (MAKE_DIR, "PLAN.md", "__pycache__"):
            continue
        return True
    return False


def run_plan(args: Any, root: str, idea: str, rows: Sequence[Feature]) -> int:
    from duet import workflow_commands

    musts = must_haves(rows)
    what = ("%s\n\nThe plan must cover every must-have in make/features.csv, each by "
            "its feature name: %s" % (idea, "; ".join(r.feature for r in musts) or "(none)"))
    args.what = [what]
    args.context = "\n\n".join(p for p in (getattr(args, "context", "") or "",
                                            ledger_context(root)) if p)
    # Checked by the plan workflow's own veto, not left to the prompt.
    args.must_cover = [r.feature for r in musts]
    return workflow_commands.run(args, "plan")


def run_build(args: Any, root: str, idea: str, rows: Sequence[Feature], brand: Brand) -> int:
    from duet import build as build_cmd, workflow_commands

    args.accept = "\n\n".join(p for p in (getattr(args, "accept", "") or "",
                                           acceptance_for(rows)) if p)
    plan = Path(root) / "PLAN.md"
    context = [getattr(args, "context", "") or "", ledger_context(root)]
    if plan.is_file():
        context.append("PLAN.md:\n\n" + plan.read_text(encoding="utf-8", errors="replace"))
    args.context = "\n\n".join(p for p in context if p)
    # The orchestrator refuses consensus while the sweep is dirty.
    args.clean_room = brand.to_dict()
    task = "%s\n\nBuild it to make/features.csv and PLAN.md. %s" % (idea, FINE_PRINT)
    if has_code(root):
        args.what = [task]
        return workflow_commands.run(args, "add")
    args.idea = [task]
    return build_cmd.run(args)


# --------------------------------------------------------------------------
# `duet make`
# --------------------------------------------------------------------------
def make_dir(root: str) -> Path:
    return Path(root).expanduser().resolve() / MAKE_DIR


def run_parity(root: str) -> int:
    rows, problems = read_features(root)
    result = score(rows, root)
    result.problems.extend(problems)
    folder = make_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / PARITY_FILE).write_text(render_parity(result), encoding="utf-8")
    print(ui.dim("  parity ") + "%s / %d (%s%%)" % (
        ("%.1f" % result.earned).rstrip("0").rstrip("."), result.possible, result.percent))
    for problem in result.problems:
        print(ui.red("✗ ") + problem)
    for row in result.demoted:
        print(ui.yellow("  counted as todo ") + "%s — no test or file in the workspace matches %r"
              % (row.feature, row.evidence))
    for row in result.musts_left:
        print(ui.red("  must-have not done ") + row.feature)
    print(ui.dim("  report: ") + str(folder / PARITY_FILE))
    if result.shippable:
        print(ui.green("shippable: every must-have is done."))
        return MAKE_OK
    print(ui.red("not shippable") + (": %d must-have(s) not done." % len(result.musts_left)
                                     if result.musts_left else "."))
    return MAKE_NOT_DONE


def run_sweep(root: str, brand: Brand) -> int:
    if brand.empty():
        print(ui.yellow("  sweep  ") + "nothing to sweep for: no --like, and no names, "
              "domains or colours in make/brand.json")
        return MAKE_OK
    findings = sweep(root, brand)
    print(ui.dim("  sweep  ") + "for %s" % ", ".join(brand.names + brand.domains + brand.colors))
    for finding in findings[:50]:
        print(ui.red("  found ") + "%s  %s  %s" % (finding.where(), ui.dim(finding.term),
                                                  finding.text if finding.line else ""))
    if len(findings) > 50:
        print(ui.dim("  … and %d more" % (len(findings) - 50)))
    if findings:
        print(ui.red("the clean room is dirty: ") + "%d place(s) still carry a reference."
              % len(findings))
        return MAKE_NOT_DONE
    print(ui.green("clean: ") + "none of the references' names, domains or colours are in the product.")
    return MAKE_OK


def stages_to_run(stage: str) -> List[str]:
    if not stage:
        return list(STAGES)
    if stage not in STAGES:
        raise ValueError("--stage %r is not one of %s" % (stage, "|".join(STAGES)))
    return [stage]


def run(args: Any) -> int:
    """`duet make` — the stages in order, or one with --stage."""
    from duet import cli
    from duet.jury import adapter_factory

    try:
        stages = stages_to_run(getattr(args, "stage", "") or "")
    except ValueError as exc:
        print(ui.red("✗ ") + str(exc))
        return MAKE_USAGE
    if any(s in ("plan", "build") for s in stages):
        nested = cli.refuse_nested("make", args)
        if nested is not None:
            return nested
    idea = " ".join(getattr(args, "what", None) or []).strip()
    likes = [l for l in (getattr(args, "like", None) or []) if l and l.strip()]
    if not idea and any(s in ("recon", "plan", "build") for s in stages):
        print(ui.red("nothing to make."))
        print("usage: " + USAGE)
        return MAKE_USAGE
    root = str(Path(args.root).expanduser().resolve())

    data, problem = read_brand(root)
    if problem:
        print(ui.red("✗ ") + problem)
        return MAKE_CANNOT
    brand = brand_from(likes, data)
    if likes:
        save_brand(root, brand)

    print(ui.bold("duet make") + "  " + ui.dim(root))
    print(ui.dim("  stages: ") + " → ".join(stages))
    print(ui.dim("  " + FINE_PRINT.replace("\n", "\n  ")))

    for stage in stages:
        print()
        print(ui.bold("— %s" % stage))
        if stage == "recon":
            try:
                cfg = cli.build_config(_without_gate(args))
            except SystemExit as exc:
                print(ui.red("✗ ") + str(exc))
                return MAKE_CANNOT
            code = run_recon(root, idea, likes, adapter_factory(cfg), cfg.order()[0])
        elif stage in ("plan", "build"):
            rows, problems = read_features(root)
            if problems:
                for line in problems:
                    print(ui.red("✗ ") + line)
                return MAKE_CANNOT
            # Each session gets its own copy: `duet plan` turns the gate off and
            # names its workflow on the args it is handed, and the build after
            # it must not inherit either.
            session = copy.copy(args)
            if stage == "plan":
                code = run_plan(session, root, idea, rows)
            else:
                code = run_build(session, root, idea, rows,
                                 brand_from(likes, read_brand(root)[0]))
        elif stage == "parity":
            code = run_parity(root)
        else:
            code = run_sweep(root, brand_from(likes, read_brand(root)[0]))
        if code != MAKE_OK:
            print(ui.dim("  stopped at %s (exit %d)" % (stage, code)))
            return code
    if stages[-1] == "sweep" or len(stages) > 1:
        print()
        print(ui.dim("next: ") + JURY_POINTER)
    return MAKE_OK


def _without_gate(args: Any) -> Any:
    """A copy of args for the research pass: it runs no gate."""
    clone = copy.copy(args)
    clone.gate, clone.no_gate = None, True
    return clone
