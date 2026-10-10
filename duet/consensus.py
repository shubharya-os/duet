"""Who is right, and when is it over.

Two rules carry this whole project:

1. A fix is not fixed because the fixer says so. It is fixed when the agent who
   raised the objection sees the change and drops it.
2. "Done" is not a vote, it is an agreement. Both agents must vote DONE against
   the *same* workspace state, with no blocking objection open and the
   acceptance gate green.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from duet.protocol import BLOCKING_SEVERITIES, Envelope, Issue

OPEN_STATES = ("open", "claimed_fixed")


@dataclass
class Signoff:
    agent: str
    round: int
    digest: str


@dataclass
class IngestReport:
    """What one turn changed about the state of the argument."""

    opened: List[str] = field(default_factory=list)
    reopened: List[str] = field(default_factory=list)
    claimed: List[str] = field(default_factory=list)
    verified: List[str] = field(default_factory=list)
    withdrawn: List[str] = field(default_factory=list)

    @property
    def moved(self) -> bool:
        return bool(self.opened or self.reopened or self.claimed or self.verified or self.withdrawn)

    def render(self) -> str:
        bits = []
        for label, ids in (
            ("opened", self.opened),
            ("re-raised", self.reopened),
            ("claimed fixed", self.claimed),
            ("verified fixed", self.verified),
            ("withdrawn", self.withdrawn),
        ):
            if ids:
                bits.append("%s: %s" % (label, ", ".join(ids)))
        return "; ".join(bits) or "no change to open issues"


@dataclass
class Decision:
    """The orchestrator's read of the session after a turn."""

    kind: str  # continue | consensus | arbitrate | blocked | stalled | exhausted
    reason: str = ""
    issue_id: str = ""


class DebateState:
    def __init__(self, agents: Sequence[str], max_debate: int = 3, stall_limit: int = 3):
        self.agents = list(agents)
        self.max_debate = max_debate
        self.stall_limit = stall_limit
        self.issues: Dict[str, Issue] = {}
        self.last_verdict: Dict[str, str] = {}
        self.signoffs: Dict[str, Signoff] = {}
        self.arbitrations: List[Dict[str, Any]] = []
        self.failed_arbitrations: Dict[str, int] = {}
        self.arbitration_retry_at: Dict[str, int] = {}
        self.stall_rounds = 0
        self.last_digest: Optional[str] = None
        # Who claimed each fix. Only needed for an issue no agent raised — a
        # jury objection — whose fix is checked by the claimant's peer.
        self.claimed_by: Dict[str, str] = {}

    # -- reading ----------------------------------------------------------
    def open_issues(self) -> List[Issue]:
        return [i for i in self.issues.values() if i.status in OPEN_STATES]

    def blocking_issues(self) -> List[Issue]:
        return [i for i in self.open_issues() if i.severity in BLOCKING_SEVERITIES]

    def issues_against(self, agent: str) -> List[Issue]:
        """Open issues raised by the other side, i.e. this agent's homework."""
        return [i for i in self.open_issues() if i.raised_by != agent]

    def verifier_of(self, issue: Issue) -> str:
        """Who may verify a claimed fix: whoever raised the issue.

        An issue raised by `jury:<persona>` was raised by nobody who takes
        turns, so the jury cannot drop it — the peer of the agent who claimed
        the fix checks it on the jury's behalf. Otherwise a jury blocker, once
        claimed, would wait on a verifier who never speaks again.
        """
        if issue.raised_by in self.agents:
            return issue.raised_by
        claimant = self.claimed_by.get(issue.id, "")
        return self.peer_of(claimant) if claimant else issue.raised_by

    def peer_of(self, agent: str) -> str:
        for other in self.agents:
            if other != agent:
                return other
        return agent

    # -- writing ----------------------------------------------------------
    def ingest(self, env: Envelope, digest: str) -> IngestReport:
        report = IngestReport()
        agent = env.agent

        # 1. This agent verifies fixes that were claimed against *its* issues.
        #
        # Only a reply we could actually read counts. A malformed or empty reply
        # has no issues in it, and treating that absence as "they looked and
        # agreed" would close a blocker on silence — the one thing this protocol
        # promises never to do.
        re_raised = {i.id for i in env.issues}
        for issue in (self.issues.values() if env.parse_ok else []):
            if issue.status != "claimed_fixed" or self.verifier_of(issue) != agent:
                continue
            if issue.id in re_raised:
                issue.status = "open"
                issue.resolution = ""
                report.reopened.append(issue.id)
            else:
                issue.status = "resolved"
                issue.resolution = "verified fixed by %s in round %d" % (agent, env.round)
                report.verified.append(issue.id)

        # 2. New or re-stated objections.
        for issue in env.issues:
            existing = self.issues.get(issue.id)
            if existing is None:
                self.issues[issue.id] = issue
                report.opened.append(issue.id)
                continue
            if existing.id in report.reopened:
                existing.severity = issue.severity
                existing.detail = issue.detail or existing.detail
                continue
            if existing.status in ("resolved", "arbitrated"):
                existing.status = "open"
                existing.detail = issue.detail or existing.detail
                existing.severity = issue.severity
                existing.raised_by = agent
                existing.opened_round = env.round
                existing.resolution = ""
                report.reopened.append(existing.id)
            else:
                existing.detail = issue.detail or existing.detail
                existing.severity = issue.severity

        # 3. Claims of repair, and withdrawals of the agent's own objections.
        for issue_id in env.resolves:
            issue = self.issues.get(issue_id)
            if issue is None or issue.status in ("resolved", "arbitrated"):
                continue
            if issue.raised_by == agent:
                issue.status = "resolved"
                issue.resolution = "withdrawn by %s in round %d" % (agent, env.round)
                report.withdrawn.append(issue_id)
            else:
                issue.status = "claimed_fixed"
                self.claimed_by[issue_id] = agent
                issue.resolution = "claimed fixed by %s in round %d; awaiting %s" % (
                    agent,
                    env.round,
                    self.verifier_of(issue),
                )
                report.claimed.append(issue_id)

        # 4. Age every objection that survived the turn.
        for issue in self.open_issues():
            issue.rounds_open += 1

        self.last_verdict[agent] = env.verdict
        if env.verdict == "DONE":
            self.signoffs[agent] = Signoff(agent=agent, round=env.round, digest=digest)
        else:
            self.signoffs.pop(agent, None)

        # 5. Stall detection: nothing built, nothing argued.
        if digest == self.last_digest and not report.moved:
            self.stall_rounds += 1
        else:
            self.stall_rounds = 0
        self.last_digest = digest
        return report

    def record_failed_arbitration(self, issue_id: str, reason: str) -> None:
        """The decider could not rule. The issue stays open — an outage is not a
        verdict — and waits out another debate window before being retried."""
        self.failed_arbitrations[issue_id] = self.failed_arbitrations.get(issue_id, 0) + 1
        issue = self.issues.get(issue_id)
        if issue is not None:
            issue.resolution = "arbitration failed: %s" % reason
            self.arbitration_retry_at[issue_id] = issue.rounds_open + self.max_debate

    def record_arbitration(self, issue_id: str, decider: str, ruling: str, round_no: int) -> None:
        issue = self.issues.get(issue_id)
        if issue is not None:
            issue.status = "arbitrated"
            issue.resolution = ruling
        self.arbitrations.append(
            dict(issue_id=issue_id, decider=decider, ruling=ruling, round=round_no)
        )

    # -- judging ----------------------------------------------------------
    def arbitration_candidate(self) -> Optional[Issue]:
        # An issue whose arbitration failed has to age another full debate
        # window past the point it failed at, or a broken decider spins the
        # session by being asked again every single round.
        stuck = [
            i
            for i in self.open_issues()
            if i.rounds_open >= self.arbitration_retry_at.get(i.id, self.max_debate)
            and i.severity in BLOCKING_SEVERITIES
        ]
        if not stuck:
            return None
        stuck.sort(key=lambda i: (-i.rounds_open, i.id))
        return stuck[0]

    def consensus_reached(self, digest: str, gate_ok: bool) -> bool:
        if len(self.signoffs) < len(self.agents):
            return False
        if any(s.digest != digest for s in self.signoffs.values()):
            return False
        if self.blocking_issues():
            return False
        return gate_ok

    def decide(self, digest: str, gate_ok: bool, on_blocked: str = "stop") -> Decision:
        if self.consensus_reached(digest, gate_ok):
            return Decision(kind="consensus", reason="both agents signed off on %s" % digest[:8])
        if on_blocked == "stop" and "BLOCKED" in self.last_verdict.values():
            who = [a for a, v in self.last_verdict.items() if v == "BLOCKED"]
            return Decision(kind="blocked", reason="%s reported BLOCKED" % ", ".join(who))
        candidate = self.arbitration_candidate()
        if candidate is not None:
            return Decision(
                kind="arbitrate",
                reason="'%s' has been open %d rounds" % (candidate.title, candidate.rounds_open),
                issue_id=candidate.id,
            )
        if self.stall_rounds >= self.stall_limit:
            return Decision(
                kind="stalled",
                reason="%d rounds with no change to the workspace or the issue list"
                % self.stall_rounds,
            )
        return Decision(kind="continue")

    # -- reporting --------------------------------------------------------
    def why_not_done(self, digest: str, gate_ok: bool,
                     gate_problem: str = "the acceptance gate is failing") -> List[str]:
        """Plain-language reasons the session is still open. Shown to agents.

        `gate_problem` is what to say when the gate is not green: a gate that
        ran out of time is not "failing", and saying so sends both agents after
        a test failure that does not exist.
        """
        reasons: List[str] = []
        for agent in self.agents:
            verdict = self.last_verdict.get(agent)
            if verdict != "DONE":
                reasons.append("%s has not voted DONE (last: %s)" % (agent, verdict or "no turn yet"))
        for agent, signoff in self.signoffs.items():
            if signoff.digest != digest:
                reasons.append(
                    "%s signed off on workspace %s, which has since changed to %s"
                    % (agent, signoff.digest[:8], digest[:8])
                )
        for issue in self.blocking_issues():
            reasons.append(
                "open %s from %s: %s (%d rounds)"
                % (issue.severity, issue.raised_by, issue.title, issue.rounds_open)
            )
        if not gate_ok:
            reasons.append(gate_problem)
        return reasons

    def to_dict(self) -> Dict[str, Any]:
        return dict(
            agents=list(self.agents),
            issues=[i.to_dict() for i in self.issues.values()],
            last_verdict=dict(self.last_verdict),
            signoffs={a: dict(agent=s.agent, round=s.round, digest=s.digest) for a, s in self.signoffs.items()},
            arbitrations=list(self.arbitrations),
            failed_arbitrations=dict(self.failed_arbitrations),
            arbitration_retry_at=dict(self.arbitration_retry_at),
            stall_rounds=self.stall_rounds,
            last_digest=self.last_digest,
            claimed_by=dict(self.claimed_by),
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any], max_debate: int = 3, stall_limit: int = 3) -> "DebateState":
        state = cls(data.get("agents", []), max_debate=max_debate, stall_limit=stall_limit)
        for raw in data.get("issues", []):
            if not isinstance(raw, dict):
                continue
            issue = Issue.from_dict(raw)
            state.issues[issue.id] = issue
        state.last_verdict = dict(data.get("last_verdict", {}))
        for agent, raw in (data.get("signoffs") or {}).items():
            if not isinstance(raw, dict):
                continue
            # A hand-edited or half-written state file is the expected input
            # here, so take the fields that exist and ignore the rest rather
            # than raising out of a command whose job is to refuse politely.
            known = {k: v for k, v in raw.items() if k in ("agent", "round", "digest")}
            known.setdefault("agent", agent)
            known.setdefault("round", 0)
            known.setdefault("digest", "")
            state.signoffs[agent] = Signoff(**known)
        state.arbitrations = list(data.get("arbitrations", []))
        state.failed_arbitrations = dict(data.get("failed_arbitrations", {}))
        state.arbitration_retry_at = dict(data.get("arbitration_retry_at", {}))
        state.stall_rounds = int(data.get("stall_rounds", 0))
        state.last_digest = data.get("last_digest")
        claimed = data.get("claimed_by")
        state.claimed_by = {str(k): str(v) for k, v in claimed.items()} if isinstance(claimed, dict) else {}
        return state
