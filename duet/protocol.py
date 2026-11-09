"""The wire format both agents speak.

Every turn ends with one JSON envelope. Everything the orchestrator decides —
who is right, whether work happened, whether the session may end — is read out
of these envelopes, never out of prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

VERDICTS = ("CONTINUE", "DONE", "BLOCKED")
SEVERITIES = ("blocker", "major", "minor")
BLOCKING_SEVERITIES = ("blocker", "major")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, fallback: str = "issue") -> str:
    slug = _SLUG_RE.sub("-", str(text).lower()).strip("-")
    return (slug or fallback)[:60]


@dataclass
class Issue:
    """A concrete objection. Issues are the unit of debate."""

    id: str
    title: str
    severity: str = "major"
    detail: str = ""
    raised_by: str = ""
    opened_round: int = 0
    status: str = "open"  # open | resolved | arbitrated
    rounds_open: int = 0
    resolution: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == "open" and self.severity in BLOCKING_SEVERITIES

    def to_dict(self) -> Dict[str, Any]:
        return dict(
            id=self.id,
            title=self.title,
            severity=self.severity,
            detail=self.detail,
            raised_by=self.raised_by,
            opened_round=self.opened_round,
            status=self.status,
            rounds_open=self.rounds_open,
            resolution=self.resolution,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Issue":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Patch:
    """A file mutation requested by an agent that has no tools of its own."""

    path: str
    action: str = "write"  # write | delete
    content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(path=self.path, action=self.action, content=self.content)


@dataclass
class Envelope:
    agent: str = ""
    round: int = 0
    message: str = ""
    verdict: str = "CONTINUE"
    issues: List[Issue] = field(default_factory=list)
    resolves: List[str] = field(default_factory=list)
    patches: List[Patch] = field(default_factory=list)
    confidence: float = 0.5
    summary: str = ""
    raw: str = ""
    parse_ok: bool = True
    notes: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    # Set on a CONTINUE that asks the harness to run the gate on the state this
    # turn leaves, without voting. See Orchestrator.gate_after_turn.
    run_gate: bool = False

    @property
    def blockers(self) -> List[Issue]:
        return [i for i in self.issues if i.severity in BLOCKING_SEVERITIES]

    def to_dict(self) -> Dict[str, Any]:
        return dict(
            agent=self.agent,
            round=self.round,
            message=self.message,
            verdict=self.verdict,
            issues=[i.to_dict() for i in self.issues],
            resolves=list(self.resolves),
            patches=[p.to_dict() for p in self.patches],
            confidence=self.confidence,
            summary=self.summary,
            parse_ok=self.parse_ok,
            notes=list(self.notes),
            meta=dict(self.meta),
            run_gate=self.run_gate,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Envelope":
        """Rebuild an envelope saved by `to_dict`, for resuming a session.

        Never raises: a state file can be hand-edited or half-written, and a
        resumed session losing one peer message is a far smaller loss than the
        whole argument. `raw` is not persisted, so it comes back empty.
        """
        data = dict(data or {})
        env = cls(
            agent=str(data.get("agent") or ""),
            message=str(data.get("message") or ""),
            verdict=str(data.get("verdict") or "CONTINUE"),
            summary=str(data.get("summary") or ""),
            parse_ok=bool(data.get("parse_ok", True)),
            run_gate=_truthy(data.get("run_gate")),
        )
        for field_name, caster in (("round", int), ("confidence", float)):
            try:
                setattr(env, field_name, caster(data.get(field_name)))
            except (TypeError, ValueError):
                pass
        env.issues = [Issue.from_dict(i) for i in data.get("issues") or [] if isinstance(i, dict)]
        env.patches = [
            Patch(**{k: v for k, v in p.items() if k in Patch.__dataclass_fields__})
            for p in data.get("patches") or []
            if isinstance(p, dict) and p.get("path")
        ]
        env.resolves = [str(r) for r in data.get("resolves") or []]
        env.notes = [str(n) for n in data.get("notes") or []]
        meta = data.get("meta")
        env.meta = dict(meta) if isinstance(meta, dict) else {}
        return env


def iter_json_objects(text: str) -> List[Tuple[Dict[str, Any], int]]:
    """Every top-level JSON object in `text`, with its start offset.

    Models wrap their envelope in fences, in prose, or in nothing at all, and
    sometimes emit a JSON example earlier in the same reply. Scanning for all
    of them and choosing later lets the caller pick the real one.
    """
    decoder = json.JSONDecoder()
    found: List[Tuple[Dict[str, Any], int]] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            return found
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            found.append((obj, start))
        index = max(end, start + 1)


def _coerce_issues(value: Any, agent: str, round_no: int) -> Tuple[List[Issue], List[str]]:
    issues: List[Issue] = []
    notes: List[str] = []
    if value in (None, "", []):
        return issues, notes
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        notes.append("issues was not a list; ignored")
        return issues, notes
    seen = set()
    for raw in value:
        if isinstance(raw, str):
            raw = {"title": raw}
        if not isinstance(raw, dict):
            notes.append("dropped a non-object issue")
            continue
        title = str(raw.get("title") or raw.get("claim") or raw.get("summary") or "").strip()
        detail = str(raw.get("detail") or raw.get("description") or "").strip()
        if not title and detail:
            title, detail = detail[:120], detail
        if not title:
            notes.append("dropped an issue with no title")
            continue
        issue_id = slugify(raw.get("id") or title)
        while issue_id in seen:
            issue_id += "-b"
        seen.add(issue_id)
        severity = str(raw.get("severity") or "major").lower().strip()
        if severity in ("critical", "high", "fatal"):
            severity = "blocker"
        elif severity in ("medium", "moderate"):
            severity = "major"
        elif severity in ("low", "nit", "trivial", "nitpick"):
            severity = "minor"
        if severity not in SEVERITIES:
            notes.append("unknown severity %r on %s; treated as major" % (severity, issue_id))
            severity = "major"
        issues.append(
            Issue(
                id=issue_id,
                title=title,
                severity=severity,
                detail=detail,
                raised_by=agent,
                opened_round=round_no,
            )
        )
    return issues, notes


def _coerce_patches(value: Any) -> Tuple[List[Patch], List[str]]:
    patches: List[Patch] = []
    notes: List[str] = []
    if not value:
        return patches, notes
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        notes.append("patches was not a list; ignored")
        return patches, notes
    for raw in value:
        if not isinstance(raw, dict):
            notes.append("dropped a non-object patch")
            continue
        path = str(raw.get("path") or raw.get("file") or "").strip()
        if not path:
            notes.append("dropped a patch with no path")
            continue
        action = str(raw.get("action") or "write").lower().strip()
        if action in ("create", "update", "replace", "overwrite"):
            action = "write"
        if action not in ("write", "delete"):
            notes.append("unknown patch action %r on %s; ignored" % (action, path))
            continue
        content = raw.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = json.dumps(content, indent=2)
        patches.append(Patch(path=path, action=action, content=content))
    return patches, notes


def _coerce_str_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [slugify(v) for v in value if str(v).strip()]


def _truthy(value: Any) -> bool:
    """A yes from a model, however it spelled it: true, "yes", "True", 1.

    Anything else — false, "no", a string that is not a yes, a nested object —
    is not a request. Asking for the gate costs a run, so only a clear yes does.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "on", "please")
    return False


def parse_envelope(text: str, agent: str = "", round_no: int = 0) -> Envelope:
    """Read an agent's reply into an Envelope, never raising.

    A reply that carries no usable envelope is not an error — it becomes a
    CONTINUE with `parse_ok=False`, and the orchestrator asks for the envelope
    again on the next turn. Silence is never mistaken for agreement.
    """
    text = text or ""
    env = Envelope(agent=agent, round=round_no, raw=text)

    candidates = [obj for obj, _ in iter_json_objects(text) if "verdict" in obj or "message" in obj]
    if not candidates:
        env.parse_ok = False
        env.verdict = "CONTINUE"
        env.message = text.strip()
        env.notes.append("no JSON envelope found; treated as CONTINUE")
        return env

    data = candidates[-1]
    env.message = str(data.get("message") or data.get("to_peer") or "").strip()
    if not env.message:
        env.message = text.strip()
        env.notes.append("envelope had no message; used the full reply")

    verdict = str(data.get("verdict") or "CONTINUE").upper().strip()
    if verdict in ("COMPLETE", "FINISHED", "SHIP", "APPROVE", "APPROVED", "YES"):
        verdict = "DONE"
    elif verdict in ("CONTINUING", "WORKING", "NOT_DONE", "NO"):
        verdict = "CONTINUE"
    elif verdict in ("STUCK", "BLOCK", "HELP"):
        verdict = "BLOCKED"
    if verdict not in VERDICTS:
        env.notes.append("unknown verdict %r; treated as CONTINUE" % verdict)
        verdict = "CONTINUE"
    env.verdict = verdict

    issues, issue_notes = _coerce_issues(data.get("issues"), agent, round_no)
    env.issues = issues
    env.notes.extend(issue_notes)

    patches, patch_notes = _coerce_patches(data.get("patches"))
    env.patches = patches
    env.notes.extend(patch_notes)

    env.resolves = _coerce_str_list(data.get("resolves") or data.get("resolved"))
    env.summary = str(data.get("summary") or "").strip()

    env.run_gate = any(_truthy(data.get(key)) for key in ("run_gate", "request_gate", "gate"))

    ruling = data.get("ruling")
    if ruling is not None:
        env.meta["ruling"] = ruling

    reads = data.get("reads") or data.get("read")
    if reads:
        if isinstance(reads, str):
            reads = [reads]
        if isinstance(reads, list):
            env.meta["reads"] = [str(r).strip() for r in reads if str(r).strip()][:12]

    try:
        env.confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        env.confidence = 0.5
        env.notes.append("confidence was not a number; defaulted to 0.5")

    # An agent cannot vote DONE while naming something that blocks DONE.
    if env.verdict == "DONE" and env.blockers:
        env.verdict = "CONTINUE"
        env.notes.append(
            "voted DONE while raising %d blocking issue(s); downgraded to CONTINUE"
            % len(env.blockers)
        )
    return env


ENVELOPE_SPEC = """\
Every reply you send MUST end with exactly one fenced JSON envelope. Prose above
it is for your peer to read; the envelope is what the harness acts on.

```json
{
  "message": "What you are saying to your peer this turn: what you did, what you
              want them to do, and your answer to each objection they raised.",
  "verdict": "CONTINUE | DONE | BLOCKED",
  "issues": [
    {"id": "kebab-case-id", "title": "one line", "severity": "blocker|major|minor",
     "detail": "why this is wrong and what would fix it"}
  ],
  "resolves": ["id-of-a-peer-issue-you-have-now-fixed"],
  "patches": [],
  "summary": "one line for the changelog",
  "confidence": 0.0,
  "run_gate": false
}
```

Rules that the harness enforces:
- `DONE` means "I have checked the current state of the workspace and I would
  ship it." Voting DONE while listing a blocker or major issue is rejected.
- Set `"run_gate": true` with CONTINUE to have the harness run the gate on the
  state your turn leaves and show you both the result next turn. It is not a
  vote and the session goes on. Once per turn; a state already gated is answered
  from that run.
- `BLOCKED` stops the session. Use it only for what neither of you can fix: a
  missing credential, a broken machine, a decision only the user can make.
  Never to get the gate run — that is what `run_gate` is for.
- The session ends only when BOTH of you vote DONE on the SAME workspace state
  with no open blocker or major issues and the acceptance gate passing. One
  side alone can never end it.
- Raise an issue only if you can say what would fix it. Vague unease is noise.
- `resolves` is a claim your peer will verify. Claiming a fix you did not make
  costs you the next round.
- Disagreeing is expected. Repeating yourself is not: if an issue survives the
  debate limit, the harness forces an arbitration round and records the ruling.
"""
