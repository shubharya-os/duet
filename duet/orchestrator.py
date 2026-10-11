"""The loop.

One agent moves, the workspace changes, the gate runs, the other agent answers.
Nothing ends until both of them sign the same state.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from duet import gate_cache, page, prompts, workflows
from duet.adapters import build as build_adapter
from duet.adapters.base import (
    DEFAULT_TURN_TIMEOUT,
    Adapter,
    human_duration,
    transient_failure,
)
from duet.config import Config
from duet.consensus import DebateState, Decision
from duet.protocol import Envelope, Issue, parse_envelope
from duet.workspace import GateResult, Workspace, failing_tests

Reporter = Callable[[Dict[str, Any]], None]

STATUS_CONSENSUS = "consensus"
STATUS_EXHAUSTED = "exhausted"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"
# `--quick`: a draft and one approving review, held to the workflow's rule, but
# without the lead re-checking whatever the reviewer changed. Not a consensus,
# and reported as something else so nobody reads it as one.
STATUS_REVIEWED = "reviewed"

# A refused call that would have changed a file. An agent whose edit was
# refused can still write "fixed it"; in a live `duet plan` run that cost two
# rounds, each spent by the peer grepping for a change that was never made.
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_HARMLESS_REDIRECT = re.compile(r"\d*>&\d|&?\d*>\s*/dev/null")
_WRITING_COMMAND = re.compile(
    r">|\btee\b|\bsed\s+-i|\bperl\s+-\w*i|\b(?:mv|cp|rm|touch|mkdir|patch|truncate)\s"
    r"|\bgit\s+(?:apply|checkout|restore|am)\b|write_text|\bopen\([^)]*['\"][wax]"
)


# How long to wait between re-sending a turn that died on a transient backend
# failure, and how many times. Five waits, a little over four minutes in total:
# long enough to sit out the kind of outage that killed a live session (a few
# minutes without DNS), short enough that a backend which is genuinely down is
# reported rather than waited on.
RETRY_BASE_DELAY = 8
RETRY_MAX_DELAY = 120
RETRY_ATTEMPTS = 5

# Indirected so the test suite can run the retry path without sleeping through
# it; the schedule itself is a pure function, tested as one.
SLEEP = time.sleep


def retry_delays(attempts: int = RETRY_ATTEMPTS) -> List[int]:
    """The backoff schedule: 8s, 16s, 32s, 64s, 120s."""
    return [min(RETRY_BASE_DELAY * 2 ** i, RETRY_MAX_DELAY) for i in range(max(0, attempts))]


def refused_writes(refused: Sequence[str]) -> List[str]:
    """The refused calls, among `Tool: target` lines, that would have written a file."""
    out: List[str] = []
    for line in refused:
        tool, _, target = line.partition(": ")
        if tool in WRITE_TOOLS:
            out.append(line)
        elif tool == "Bash" and _WRITING_COMMAND.search(_HARMLESS_REDIRECT.sub("", target)):
            out.append(line)
    return out


def rounds_taken(data: Dict[str, Any]) -> int:
    """How many rounds a saved session got through, from its state.json.

    `rounds` is written by `_save`; the fallbacks read it out of the rounds
    stamped on the last envelopes and sign-offs, so a state file written by an
    older duet still resumes at the right number instead of re-running turns.
    """
    candidates = [0]
    try:
        candidates.append(int(data.get("rounds") or 0))
    except (TypeError, ValueError):
        pass
    state = data.get("state") or {}
    records = list((data.get("last_envelope") or {}).values())
    records += list((state.get("signoffs") or {}).values())
    records += [{"round": a.get("round")} for a in state.get("arbitrations") or []]
    for record in records:
        try:
            candidates.append(int((record or {}).get("round") or 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return max(candidates)


@dataclass
class SessionResult:
    status: str
    rounds: int
    digest: str
    reason: str = ""
    gate: Optional[GateResult] = None
    session_dir: str = ""
    report: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_CONSENSUS


@dataclass
class TurnRecord:
    round: int
    agent: str
    role: str
    envelope: Envelope
    digest: str
    patch_log: List[str] = field(default_factory=list)
    gate: Optional[GateResult] = None
    ingest: str = ""
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    # A turn stopped at its time limit *after* it had changed the workspace. The
    # work is on disk; only the report is missing. Progress, not failure.
    cut_off: bool = False
    # A turn that never reached the model: a transient backend failure that
    # survived the whole backoff, with nothing changed in the workspace. It does
    # not get to spend a round of the budget.
    never_happened: bool = False


class Orchestrator:
    def __init__(
        self,
        config: Config,
        reporter: Optional[Reporter] = None,
        session_id: Optional[str] = None,
        adapters: Optional[Dict[str, Adapter]] = None,
    ):
        self.config = config
        self.reporter = reporter or (lambda event: None)
        self.workspace = Workspace(config.root, gate=config.gate or None, gate_timeout=config.gate_timeout)
        self.state = DebateState(
            config.agent_names, max_debate=config.max_debate, stall_limit=config.stall_limit
        )
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.session_dir = Path(config.root).resolve() / ".duet" / "sessions" / self.session_id
        self.turns: List[TurnRecord] = []
        self.history: List[str] = []
        # Where the round counter stands. `restore` moves both forward so a
        # resumed session carries on counting instead of re-running rounds.
        self.start_round = 1
        self.round_no = 0
        self.resumed_from = ""
        self.pending_directive: Dict[str, str] = {}
        self.pending_reads: Dict[str, List[str]] = {}
        self.pending_patch_log: Dict[str, List[str]] = {}
        self.pending_refused: Dict[str, List[str]] = {}
        self.last_envelope: Dict[str, Envelope] = {}
        self._gate_cache: Dict[str, GateResult] = {}
        # Passes from earlier sessions, shown for the opening check and kept
        # apart from _gate_cache so no sign-off is ever counted against one.
        self._remembered: Dict[str, GateResult] = {}
        # Digests whose failed gate has had its one re-run; see gate_for.
        self._rechecked: Set[str] = set()
        # One warning per session is enough; see _check_gate_is_read_only.
        self._gate_mutation_warned = False
        # The rule this session is held to, if any, beyond the double sign-off.
        self.workflow = workflows.get(config.workflow)
        self.adapters: Dict[str, Adapter] = adapters or {}
        if not self.adapters:
            for spec in config.agents:
                self.adapters[spec.name] = build_adapter(
                    spec.backend,
                    name=spec.name,
                    cwd=config.root,
                    model=spec.model,
                    config=spec.options,
                )
        # An agent that cannot run the gate is reviewing on hearsay. `allow_run`
        # is for sessions with no gate that still have tests worth running —
        # a plan checked against the suite rather than traced by hand.
        for command in (config.gate, config.allow_run):
            if command:
                for adapter in self.adapters.values():
                    adapter.allow_gate(command)
        # The turn limit the session was started (or resumed) with. Applied to
        # every adapter, including ones handed in ready-made, so there is one
        # answer to "how long does a turn get" and the agents can be told it.
        if config.turn_timeout:
            for adapter in self.adapters.values():
                adapter.set_turn_timeout(config.turn_timeout)
        self.sleep = SLEEP
        self.retry_delays = retry_delays()
        # `--jury`: who builds the jurors' adapters (the tests hand one in),
        # and one line on what the panel last decided, for the report.
        self.jury_adapter: Optional[Callable[[str], Adapter]] = None
        self.jury_note = ""

    # -- plumbing ---------------------------------------------------------
    def emit(self, kind: str, **payload: Any) -> None:
        event = dict(kind=kind, t=time.time(), **payload)
        self.reporter(event)
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            with (self.session_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError:
            pass

    def role_of(self, agent: str, round_no: int) -> str:
        order = self.config.order()
        lead = order[0]
        if self.config.swap_every:
            swaps = (round_no - 1) // max(1, self.config.swap_every)
            if swaps % 2:
                lead = order[1]
        return prompts.ROLE_LEAD if agent == lead else prompts.ROLE_REVIEWER

    def decider(self) -> str:
        if self.config.decider and self.config.decider in self.config.agent_names:
            return self.config.decider
        for spec in self.config.agents:
            if self.adapters[spec.name].edits_workspace:
                return spec.name
        return self.config.agent_names[0]

    def turn_budget(self, agent: str) -> int:
        """Seconds this agent's turn may take before the harness cuts it off.

        Whatever the session was told, else whatever the adapter enforces, else
        the shared default — the number the agent is shown has to be the number
        that will actually stop it.
        """
        adapter = self.adapters.get(agent)
        return int(
            self.config.turn_timeout
            or getattr(adapter, "timeout", 0)
            or DEFAULT_TURN_TIMEOUT
        )

    def gate_for(self, digest: str, recheck: bool = False) -> GateResult:
        """This session's gate result for `digest`, run once and then cached.

        `recheck` is passed after a turn, and only matters when the cached
        result is a failure: a whole turn has gone by without anyone changing a
        file, so the failure is re-run once before it is treated as settled. A
        failure that was the machine and not the code — a test that timed out
        under load — otherwise stayed red until an agent edited a file just to
        get the gate run again. A failure that repeats is a failure; a pass on
        the re-run is used, and said to be possibly flaky to everyone.
        """
        if digest not in self._gate_cache:
            self._gate_cache[digest] = self._run_gate(digest)
        elif recheck and digest not in self._rechecked:
            first = self._gate_cache[digest]
            if not first.ok and not first.skipped:
                self._rechecked.add(digest)
                self.emit("gate_rerun", digest=digest, timed_out=first.timed_out)
                again = self._run_gate(digest, remember=False)
                if again.ok:
                    names = failing_tests(first.output)
                    again.flaky = (
                        "the gate %s once and passed on a re-run with no file changes — "
                        "possibly flaky%s" % (
                            "ran out of time" if first.timed_out else "failed",
                            ": " + ", ".join(names) if names else ""))
                    self.emit("gate_flaky", digest=digest, note=again.flaky, tests=names)
                self._gate_cache[digest] = again
        return self._gate_cache[digest]

    def _run_gate(self, digest: str, remember: bool = True) -> GateResult:
        if self.workspace.gate:
            self.emit("gate_start", command=self.workspace.gate, digest=digest)
        result = self.workspace.run_gate()
        # A pass that followed a failure on the same files is not one to carry
        # into the next session's opening check as if it were settled.
        if remember:
            gate_cache.remember(self.config.root, digest, result)
        if self.workflow:
            self.workflow.on_gate(self.config.workflow_state, digest, result.ok)
        if self.workspace.gate:
            self.emit("gate_done", ok=result.ok, exit_code=result.exit_code, digest=digest,
                      timed_out=result.timed_out, timeout=result.timeout)
            self._check_gate_is_read_only(digest)
        return result

    def opening_gate(self, digest: str) -> GateResult:
        """The gate before round 1: a pass remembered from another session, if any.

        Same digest, same command, under a day old, and not `--fresh-gate` —
        otherwise the gate runs as it always has. The remembered result is shown
        to the agents and in the terminal for what it is, and is never put in
        `_gate_cache`: the turn's own gate, the one sign-offs are counted
        against, still runs during this session.
        """
        if digest in self._gate_cache or digest in self._remembered:
            return self.shown_gate(digest)
        if self.workspace.gate and not self.config.fresh_gate:
            remembered = gate_cache.recall(self.config.root, digest, self.workspace.gate)
            if remembered is not None:
                self._remembered[digest] = remembered
                if self.workflow:
                    self.workflow.on_gate(self.config.workflow_state, digest, True)
                self.emit("gate_remembered", command=remembered.command, digest=digest,
                          when=remembered.remembered, duration=remembered.duration)
                return remembered
        return self.gate_for(digest)

    def shown_gate(self, digest: str) -> GateResult:
        """What the agents are shown: this session's run, else a remembered pass."""
        if digest in self._gate_cache:
            return self._gate_cache[digest]
        if digest in self._remembered:
            return self._remembered[digest]
        return self.gate_for(digest)

    def _check_gate_is_read_only(self, before: str) -> None:
        """Say so if running the gate changes the workspace.

        Sign-offs are counted against a digest of the files, and the digest is
        taken before the gate runs. A gate that writes into the workspace —
        a test that leaves its database behind, a formatter, a build that
        emits artefacts — therefore moves the state out from under every
        sign-off the moment it is checked, and the pair can agree forever
        without ever agreeing on the same bytes. Worth one warning rather than
        a session nobody can explain.
        """
        if self._gate_mutation_warned:
            return
        after = self.workspace.digest()
        if after == before:
            return
        self._gate_mutation_warned = True
        self.emit("gate_mutates", before=before, after=after)

    # -- one turn ---------------------------------------------------------
    def _build_prompt(self, agent: str, round_no: int, directive: str, digest: str) -> str:
        peer = self.state.peer_of(agent)
        peer_env = self.last_envelope.get(peer)
        gate = self.shown_gate(digest)
        files: Dict[str, str] = {}
        for path in self.pending_reads.pop(agent, [])[:12]:
            files[path] = self.workspace.read(path)
        return prompts.turn_prompt(
            task=self.config.task,
            acceptance=self.config.acceptance,
            context=self.config.context,
            round_no=round_no,
            max_rounds=self.config.max_rounds,
            role=self.role_of(agent, round_no),
            peer=peer,
            peer_message=peer_env.message if peer_env else "",
            peer_verdict=peer_env.verdict if peer_env else "",
            digest=digest,
            workspace_view=self.workspace.diff(),
            gate_text=gate.render(),
            # When the gate is a page check, both agents can also see the page.
            page_shot=page.shot_command_for(self.config.gate),
            against_you=self.state.issues_against(agent),
            yours=[i for i in self.state.open_issues() if i.raised_by == agent],
            history=self.history,
            why_open=self.state.why_not_done(digest, gate.ok, gate.problem()),
            directive=directive,
            files=files or None,
            patch_log=self.pending_patch_log.pop(agent, None),
            peer_refused=self.pending_refused.pop(agent, None),
            turn_timeout=self.turn_budget(agent),
        )

    def _system_prompt(self, agent: str, round_no: int) -> str:
        adapter = self.adapters[agent]
        peer = self.state.peer_of(agent)
        peer_adapter = self.adapters[peer]
        return prompts.system_prompt(
            name=agent,
            display=adapter.display,
            peer=peer,
            peer_display=peer_adapter.display,
            role=self.role_of(agent, round_no),
            root=self.config.root,
            edits_workspace=adapter.edits_workspace,
        )

    def _send(self, agent: str, prompt: str, system: str, round_no: int):
        """One turn's call, re-sent through a backoff while the failure is the
        network's fault.

        Returns (reply, envelope, attempts, transient reason that survived).

        A turn that cannot reach the API has not happened: nothing was read,
        nothing was written, and what comes back is the CLI's own banner. Live,
        a few minutes without DNS failed three turns in a row this way and ended
        a session that was making progress — so duet waits the outage out here
        instead of spending rounds on it.
        """
        adapter = self.adapters[agent]
        reply = adapter.send(prompt, system=system, round_no=round_no)
        # Parsed before the failure is judged, because a backend can fail
        # *after* the agent has answered and that answer is still worth having.
        # The reverse is what this guards: a CLI's own error banner ("API
        # Error: Can't reach the API server") is prose, and prose defaults to
        # CONTINUE — so a turn that never happened was being written down as a
        # considered verdict, spending the round instead of retrying it.
        env = parse_envelope(reply.text, agent=agent, round_no=round_no)
        attempts = 1
        reason = ""
        for delay in self.retry_delays:
            if reply.ok or env.parse_ok or reply.timed_out:
                break
            reason = transient_failure(reply.error) or transient_failure(reply.text)
            if not reason:
                break
            self.emit("backend_retry", round=round_no, agent=agent, attempt=attempts,
                      of=len(self.retry_delays) + 1, wait=delay, reason=reason,
                      error=reply.error[:200])
            self.sleep(delay)
            reply = adapter.send(prompt, system=system, round_no=round_no)
            env = parse_envelope(reply.text, agent=agent, round_no=round_no)
            attempts += 1
        if reply.ok or env.parse_ok or reply.timed_out:
            reason = ""
        else:
            reason = transient_failure(reply.error) or transient_failure(reply.text)
        return reply, env, attempts, reason

    def take_turn(self, agent: str, round_no: int, directive: str = "", ingest: bool = True) -> TurnRecord:
        adapter = self.adapters[agent]
        digest_before = self.workspace.digest()
        prompt = self._build_prompt(agent, round_no, directive, digest_before)
        role = self.role_of(agent, round_no)

        self.emit("turn_start", round=round_no, agent=agent, role=role, backend=adapter.backend)
        reply, env, attempts, transient = self._send(
            agent, prompt, self._system_prompt(agent, round_no), round_no
        )
        if attempts > 1:
            # On the record either way: a turn that took four goes to get through
            # is worth seeing in the transcript, whether or not it then worked.
            reply.meta["attempts"] = attempts

        # Caught here, before the peer's turn, because afterwards it costs one:
        # the peer reads "fixed", checks, finds nothing, and says so.
        writes = refused_writes(reply.meta.get("refused") or []) if reply.text.strip() else []
        if writes:
            changed = self.workspace.digest() != digest_before
            self.emit("refused_writes", round=round_no, agent=agent, calls=writes)
            retry = adapter.send(
                prompts.REFUSED_WRITES_DIRECTIVE.format(
                    calls="\n".join("- %s" % c for c in writes),
                    state=("Some files did change this turn, so the workspace may hold only part "
                           "of what you meant.") if changed else
                          "The workspace is byte-identical to how it was when your turn began.",
                    commands=("The only commands you can run are the project's own: %s."
                              % " / ".join(c for c in (self.config.gate, self.config.allow_run) if c))
                             if (self.config.gate or self.config.allow_run) else
                             "You cannot run shell commands here.",
                ),
                system=self._system_prompt(agent, round_no),
                round_no=round_no,
            )
            retry_env = parse_envelope(retry.text, agent=agent, round_no=round_no)
            if retry.text.strip() and (retry.ok or retry_env.parse_ok):
                first_cost = reply.meta.get("cost_usd")
                reply, env = retry, retry_env
                if first_cost and reply.meta.get("cost_usd") is not None:
                    reply.meta["cost_usd"] = reply.meta["cost_usd"] + first_cost

        if not reply.ok and not env.parse_ok:
            digest_now = self.workspace.digest()
            changed = digest_now != digest_before
            peer = self.state.peer_of(agent)
            record = TurnRecord(
                round=round_no, agent=agent, role=role, envelope=Envelope(agent=agent, round=round_no),
                digest=digest_now, error=reply.error, meta=dict(reply.meta),
                # A timeout that left work behind is progress: the peer picks it
                # up from the files. A timeout that left nothing is a turn that
                # achieved nothing, and still counts as a failure.
                cut_off=bool(reply.timed_out and changed),
                # Nothing reached the model and nothing changed, so there is no
                # round to charge for it.
                never_happened=bool(transient and not changed),
            )
            self.turns.append(record)
            if record.cut_off:
                budget = human_duration(reply.meta.get("timeout_s") or self.turn_budget(agent))
                self.pending_directive[peer] = prompts.PEER_CUT_OFF_DIRECTIVE.format(
                    peer=agent, budget=budget, normal=prompts.NORMAL_DIRECTIVE
                )
                self.history.append(
                    "R%-2d %-6s CUT OFF   at %s — workspace changed, no report; %s takes it on"
                    % (round_no, agent, budget, peer)
                )
                self.emit("turn_cut_off", round=round_no, agent=agent, budget=budget,
                          digest=digest_now, peer=peer, error=reply.error)
            elif record.never_happened:
                self.history.append(
                    "R%-2d %-6s NO TURN   %s after %d attempt(s) — the round is not spent"
                    % (round_no, agent, transient, attempts)
                )
                self.emit("turn_never_happened", round=round_no, agent=agent,
                          reason=transient, attempts=attempts, error=reply.error)
            # Counted only now. `round_no` means rounds *finished*: a Ctrl-C
            # during the adapter call used to leave the round marked as taken,
            # so resume started one round late, gave the turn to the other
            # agent, and dropped the directive this one was owed. A turn that
            # never happened is the same case: charging it a round would hand a
            # resumed session a budget it has not used.
            if not record.never_happened:
                self.round_no = max(self.round_no, round_no)
            self.emit("turn_error", round=round_no, agent=agent, error=reply.error,
                      cut_off=record.cut_off, never_happened=record.never_happened)
            self._save()
            return record

        if reply.error:
            env.notes.append("backend reported: %s" % reply.error)

        patch_log: List[str] = []
        if env.patches:
            patch_log = self.workspace.apply_patches(env.patches)
            self.emit("patches", round=round_no, agent=agent, log=patch_log)
            peer = self.state.peer_of(agent)
            self.pending_patch_log[peer] = patch_log
            self.pending_patch_log[agent] = patch_log

        reads = env.meta.get("reads") or []
        if reads:
            self.pending_reads[agent] = list(reads)

        refused = list(reply.meta.get("refused") or [])
        if refused:
            self.pending_refused[self.state.peer_of(agent)] = refused

        digest_after = self.workspace.digest()
        # recheck: if this turn changed nothing and the gate was red, the red
        # gets one re-run before it is believed.
        gate = self.gate_for(digest_after, recheck=True)

        record = TurnRecord(
            round=round_no, agent=agent, role=role, envelope=env, digest=digest_after,
            patch_log=patch_log, gate=gate, meta=reply.meta,
        )
        if ingest:
            report = self.state.ingest(env, digest_after)
            record.ingest = report.render()
        self.last_envelope[agent] = env
        self.turns.append(record)
        self.round_no = max(self.round_no, round_no)

        self.history.append(
            "R%-2d %-6s %-8s %s%s%s%s"
            % (
                round_no,
                agent,
                env.verdict,
                record.ingest or "no issue changes",
                " | %d file(s) written" % len(patch_log) if patch_log else "",
                " | %d call(s) refused" % len(refused) if refused else "",
                " | gate %s" % ("PASS on a re-run, possibly flaky" if gate.ok and gate.flaky
                                else "PASS" if gate.ok else "TIMED OUT" if gate.timed_out
                                else "FAIL")
                if self.workspace.gate else "",
            )
        )
        self.emit(
            "turn_done",
            round=round_no,
            agent=agent,
            verdict=env.verdict,
            confidence=env.confidence,
            parse_ok=env.parse_ok,
            issues=[i.id for i in env.issues],
            resolves=list(env.resolves),
            ingest=record.ingest,
            digest=digest_after,
            gate_ok=gate.ok,
            gate_timed_out=gate.timed_out,
            message=env.message,
            notes=env.notes,
            refused=refused,
        )
        self._save()
        return record

    # -- arbitration ------------------------------------------------------
    def arbitrate(self, issue_id: str, round_no: int) -> None:
        issue = self.state.issues.get(issue_id)
        if issue is None:
            return
        self.emit("arbitration_start", round=round_no, issue=issue_id, title=issue.title)

        positions: Dict[str, str] = {}
        for agent in self.config.order():
            directive = prompts.ARBITRATION_POSITION_DIRECTIVE.format(
                issue_id=issue.id, issue_title=issue.title, rounds=issue.rounds_open
            )
            record = self.take_turn(agent, round_no, directive=directive, ingest=False)
            positions[agent] = record.envelope.message or record.error or "(no position given)"
            self.last_envelope[agent] = record.envelope

        decider = self.decider()
        rendered = "\n\n".join(
            "--- %s's final position ---\n%s" % (name, text) for name, text in positions.items()
        )
        directive = prompts.ARBITRATION_RULING_DIRECTIVE.format(
            issue_id=issue.id, issue_title=issue.title, positions=rendered
        )
        record = self.take_turn(decider, round_no, directive=directive, ingest=True)
        ruling = record.envelope.meta.get("ruling")
        if isinstance(ruling, dict):
            text = "%s — %s" % (ruling.get("decision", "ruled"), ruling.get("rationale", ""))
        else:
            text = record.envelope.summary or record.envelope.message[:300]

        if record.error or not record.envelope.parse_ok or not text.strip():
            reason = record.error or "the decider gave no readable ruling"
            self.state.record_failed_arbitration(issue.id, reason)
            self.history.append("R%-2d ARBITRATION on %s FAILED: %s" % (round_no, issue.id, reason[:70]))
            self.emit("arbitration_failed", round=round_no, issue=issue.id, decider=decider, reason=reason)
            return

        self.state.record_arbitration(issue.id, decider, text, round_no)
        self.history.append("R%-2d ARBITRATION on %s -> %s (by %s)" % (round_no, issue.id, text[:80], decider))
        self.emit("arbitration_done", round=round_no, issue=issue.id, decider=decider, ruling=text)

    # -- the jury ---------------------------------------------------------
    def convene_jury(self, round_no: int) -> bool:
        """Sit the panel on a state both agents signed. True if it reopened the session.

        Verified blocker objections become open issues raised by
        `jury:<persona>`, and the sign-offs are cleared: the pair fixes them or
        argues them down like any other issue. The panel sits at most
        `jury_max` times, so it cannot keep a session open forever.
        """
        from duet import jury

        cfg = self.config
        if cfg.jury_convened >= cfg.jury_max:
            return False
        try:
            panel = jury.choose_panel(jury.load_config(cfg.root), cfg.jury_panel)
        except jury.JuryError as exc:
            # Checked before the session started; edited since. Say so and let
            # the session end on the pair's agreement rather than on a typo.
            self.emit("jury_failed", reason=str(exc))
            self.jury_note = "the jury could not sit: %s" % exc
            return False
        cfg.jury_convened += 1
        self.emit("jury_start", round=round_no, convening=cfg.jury_convened, of=cfg.jury_max,
                  panel=[p.name for p in panel])
        result = jury.convene(
            root=cfg.root, agents=cfg.order(), panel=panel,
            stage=jury.detect_stage(cfg.root, cfg.task), idea=cfg.task,
            out=str(self.session_dir / jury.JURY_FILE),
            make_adapter=self.jury_adapter or jury.adapter_factory(cfg),
            shots_dir=str(self.session_dir / "jury-shots"),
        )
        self.jury_note = "sat %d of %d time(s); last: %s" % (
            cfg.jury_convened, cfg.jury_max, result.summary_line())
        opened: List[str] = []
        for verdict, objection in result.blockers():
            raised_by = "jury:%s" % verdict.persona.name
            issue_id = "jury-%s-%s" % (verdict.persona.name, objection.id)
            detail = "%s\n\nEvidence (%s): \"%s\"" % (
                objection.detail, objection.where(), jury.normalise(objection.quote)[:400])
            existing = self.state.issues.get(issue_id)
            if existing is not None and existing.status in ("open", "claimed_fixed"):
                continue
            self.state.issues[issue_id] = Issue(
                id=issue_id, title=objection.title, severity="blocker", detail=detail,
                raised_by=raised_by, opened_round=round_no,
            )
            opened.append(issue_id)
        self.emit("jury_done", round=round_no, convening=cfg.jury_convened, opened=opened,
                  summary=result.summary_line(), report=result.path, exit_code=result.exit_code())
        self.history.append("R%-2d JURY      %s%s" % (
            round_no, result.summary_line(), " | opened: %s" % ", ".join(opened) if opened else ""))
        if not opened:
            self._save()
            return False
        self.state.signoffs.clear()
        for name in cfg.agent_names:
            self.pending_directive[name] = prompts.JURY_DIRECTIVE.format(
                issues="\n".join("- `%s`" % i for i in opened),
                report=result.path or "(not written)",
            ) + prompts.NORMAL_DIRECTIVE
        self._save()
        return True

    # -- resuming ---------------------------------------------------------
    def restore(self, data: Dict[str, Any]) -> List[str]:
        """Rebuild the argument from a session's saved state.json.

        Everything the next prompt is built out of: the issue ledger with each
        objection's age, both last verdicts and sign-offs, the arbitration
        record, the round history, the peer messages, and each adapter's own
        thread with its backend. Returns any notes worth printing.

        What is deliberately *not* restored is the workspace digest and the gate
        result. Both are recomputed on the first turn, because the tree may have
        been edited — or fixed — while the session was dead.

        Raises ValueError if the saved argument was had between agents this pair
        does not contain. Every issue in the ledger is attributed to a name, and
        resuming under different names would hand one agent the other's
        objections — so this refuses rather than guessing.
        """
        notes: List[str] = []
        saved = data.get("state") or {}
        if saved:
            recorded = [a for a in (saved.get("agents") or []) if a]
            if recorded and sorted(recorded) != sorted(self.config.agent_names):
                raise ValueError(
                    "the saved argument was between %s, but this session's config builds "
                    "%s — the issue ledger names agents who would not be in the room"
                    % (", ".join(recorded), ", ".join(self.config.agent_names))
                )
            self.state = DebateState.from_dict(
                saved, max_debate=self.config.max_debate, stall_limit=self.config.stall_limit
            )
            if not self.state.agents:
                self.state.agents = list(self.config.agent_names)
        self.history = [str(line) for line in data.get("history") or []]
        for name, raw in (data.get("last_envelope") or {}).items():
            if name in self.adapters and isinstance(raw, dict):
                self.last_envelope[name] = Envelope.from_dict(raw)
        for name, raw in (data.get("adapters") or {}).items():
            if name in self.adapters and isinstance(raw, dict):
                self.adapters[name].restore(raw)

        # A directive is owed to an agent, not said to it yet: "your last reply
        # had no envelope, re-send it", or the instruction to break a stall.
        # Dropping the stall one costs the most — detecting a stall also zeroes
        # the counter, so losing the directive erases the finding as well, and
        # the pair has to grind out another full stall window to notice again.
        for name, directive in (data.get("pending_directive") or {}).items():
            if name in self.adapters and str(directive or "").strip():
                self.pending_directive[name] = str(directive)

        # Two more inputs the next prompt is built out of. A file an agent asked
        # to see is owed to it: dropping the request means its prompt silently
        # omits the file and it spends a turn asking again.
        for name, paths in (data.get("pending_reads") or {}).items():
            if name in self.adapters and isinstance(paths, list):
                self.pending_reads[name] = [str(p) for p in paths if str(p).strip()]
        for name, log in (data.get("pending_patch_log") or {}).items():
            if name in self.adapters and isinstance(log, list):
                self.pending_patch_log[name] = [str(line) for line in log]

        # A BLOCKED verdict is why that session stopped. The human has since
        # asked for it to carry on, so it is history, not a live position:
        # leaving it in place would end the resumed session after one turn,
        # before its author ever got to speak again.
        for agent in [a for a, v in self.state.last_verdict.items() if v == "BLOCKED"]:
            self.state.last_verdict.pop(agent)
            notes.append("%s's BLOCKED verdict is cleared — resuming gives them a fresh turn" % agent)

        self.resumed_from = str(data.get("session_id") or "")
        self.round_no = rounds_taken(data)
        self.start_round = self.round_no + 1
        return notes

    # -- the loop ---------------------------------------------------------
    def run(self) -> SessionResult:
        cfg = self.config
        order = cfg.order()
        # Children inherit this, and the CLI refuses to start a session when it
        # is set. An agent that invokes duet spawns another pair of agents, and
        # the outer turn then waits on the whole nested session: in a real run
        # that cost a 30-minute adapter timeout and produced no envelope.
        os.environ["DUET_SESSION"] = self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.emit(
            "session_start",
            session=self.session_id,
            task=cfg.task,
            root=cfg.root,
            agents={a.name: self.adapters[a.name].describe() for a in cfg.agents},
            order=order,
            gate=cfg.gate,
            # Records that a handoff note arrived, and its size, without
            # copying it into the log. Whether context crossed is a claim
            # duet makes, so it should be checkable from the record.
            context_chars=len((cfg.context or "").strip()),
            max_rounds=cfg.max_rounds,
            turn_timeout={name: self.turn_budget(name) for name in cfg.agent_names},
            resumed_from=self.resumed_from or None,
            start_round=self.start_round,
        )

        if self.workflow:
            # Before the first gate runs: the baseline is the workspace as the
            # pair found it. setdefault inside begin() keeps a resumed session's
            # original baseline rather than retaking it from wherever it died.
            # The gate goes in too — a replay against the baseline runs it.
            cfg.workflow_state.setdefault("gate", cfg.gate)
            # Not setdefault: `duet resume --gate-timeout` raises it for the
            # replay as well as for the gate.
            cfg.workflow_state["gate_timeout"] = cfg.gate_timeout
            self.workflow.begin(cfg.root, cfg.workflow_state)

        status, reason = STATUS_EXHAUSTED, "reached the %d-round limit" % cfg.max_rounds
        round_no = self.start_round - 1
        errors: Dict[str, int] = {name: 0 for name in cfg.agent_names}
        agent = directive = ""

        try:
            # A while loop, not a range: a turn that never reached the model has
            # to be able to give its round back, and `range` cannot be rewound.
            while round_no < cfg.max_rounds:
                round_no += 1
                agent = order[(round_no - 1) % len(order)]
                peer = self.state.peer_of(agent)
                digest = self.workspace.digest()
                # Only the opening check may be remembered from another
                # session; every later one is this session's own run.
                if round_no == self.start_round:
                    gate = self.opening_gate(digest)
                else:
                    gate = self.gate_for(digest)

                directive = self.pending_directive.pop(agent, "")
                if not directive:
                    peer_signoff = self.state.signoffs.get(peer)
                    if peer_signoff and peer_signoff.digest == digest and gate.ok and not self.state.blocking_issues():
                        directive = prompts.FINAL_CHECK_DIRECTIVE

                record = self.take_turn(agent, round_no, directive=directive)

                if record.cut_off:
                    # The clock ran out, not the backend. The work is in the
                    # workspace and the peer has been told to pick it up; holding
                    # this against the agent is what ended a live session that
                    # was 2,500 lines into the job.
                    continue
                if record.error:
                    if record.never_happened:
                        # The directive was popped for a turn that did not
                        # happen; the agent is still owed it.
                        if directive:
                            self.pending_directive[agent] = directive
                        round_no -= 1
                    errors[agent] += 1
                    if errors[agent] >= 2:
                        status, reason = STATUS_ERROR, "%s failed twice: %s" % (agent, record.error)
                        break
                    continue
                errors[agent] = 0

                if not record.envelope.parse_ok:
                    self.pending_directive[agent] = (
                        "Your last reply had no JSON envelope, so the harness could not "
                        "record your verdict or your issues — your peer saw only prose. "
                        "Re-send your position this turn and end the reply with the "
                        "envelope.\n\n" + prompts.NORMAL_DIRECTIVE
                    )

                decision = self.state.decide(record.digest, (record.gate or gate).ok, cfg.on_blocked)
                if decision.kind == "consensus" and self.workflow:
                    # Both agents agree; the workflow's rule gets the last word,
                    # because it is checked against what happened rather than
                    # against what either of them says happened.
                    veto = self.workflow.veto(cfg.root, cfg.workflow_state)
                    if veto:
                        self.emit("workflow_veto", workflow=self.workflow.name, reason=veto)
                        self.state.signoffs.clear()
                        for name in cfg.agent_names:
                            self.pending_directive[name] = (
                                "THE `%s` RULE IS NOT MET, SO THE SIGN-OFFS DO NOT COUNT\n\n%s\n\n"
                                % (self.workflow.name, veto)
                            ) + prompts.NORMAL_DIRECTIVE
                        self._save()
                        continue
                    proven = self.workflow.proven(cfg.workflow_state)
                    if proven:
                        self.emit("workflow_proven", workflow=self.workflow.name, detail=proven)
                if decision.kind == "consensus" and cfg.jury and self.convene_jury(round_no):
                    continue
                if decision.kind == "consensus":
                    status, reason = STATUS_CONSENSUS, decision.reason
                    break
                if decision.kind == "blocked":
                    status, reason = STATUS_BLOCKED, decision.reason
                    break
                if decision.kind == "arbitrate":
                    self.emit("decision", decision=decision.kind, reason=decision.reason)
                    self.arbitrate(decision.issue_id, round_no)
                    continue
                if decision.kind == "stalled":
                    self.emit("decision", decision=decision.kind, reason=decision.reason)
                    stall = prompts.STALL_DIRECTIVE.format(rounds=self.state.stall_rounds)
                    self.pending_directive[agent] = stall
                    self.pending_directive[peer] = stall
                    self.state.stall_rounds = 0
        except KeyboardInterrupt:
            status, reason = STATUS_INTERRUPTED, "interrupted by the user"
            # The turn never happened, so give back the directive that was
            # popped for it — otherwise a stall nudge or an "envelope missing"
            # correction is lost across the resume.
            if agent and directive:
                self.pending_directive[agent] = directive

        if status == STATUS_EXHAUSTED and self.workflow and cfg.workflow_state.get("quick"):
            last = self.turns[-1] if self.turns else None
            veto = self.workflow.veto(cfg.root, cfg.workflow_state)
            if veto:
                reason = "quick: the `%s` rule is not met — %s" % (self.workflow.name, veto)
            elif not last or last.error or last.envelope.verdict != "DONE":
                reason = "quick: the one review did not approve it — see the open issues in the report"
            else:
                status = STATUS_REVIEWED
                reason = ("quick: drafted by %s, approved in one review by %s; what %s changed "
                          "in that review was not re-checked by %s" % (order[0], last.agent,
                                                                      last.agent, order[0]))

        digest = self.workspace.digest()
        gate = self.gate_for(digest)
        result = SessionResult(
            status=status,
            rounds=round_no,
            digest=digest,
            reason=reason,
            gate=gate,
            session_dir=str(self.session_dir),
        )
        os.environ.pop("DUET_SESSION", None)
        result.report = self.write_report(result)
        self._save(result)
        if status == STATUS_CONSENSUS and cfg.commit:
            self.commit(result)
        self.emit("session_end", status=status, reason=reason, rounds=round_no, digest=digest, report=result.report)
        return result

    # -- persistence ------------------------------------------------------
    def _save(self, result: Optional[SessionResult] = None) -> None:
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_id": self.session_id,
                "resumed_from": self.resumed_from or None,
                "rounds": self.round_no,
                "pending_directive": dict(self.pending_directive),
                "config": self.config.to_dict(),
                "state": self.state.to_dict(),
                "history": self.history,
                "adapters": {name: a.state() for name, a in self.adapters.items()},
                "last_envelope": {k: v.to_dict() for k, v in self.last_envelope.items()},
                "pending_reads": {k: list(v) for k, v in self.pending_reads.items()},
                "pending_patch_log": {k: list(v) for k, v in self.pending_patch_log.items()},
                "result": (
                    {"status": result.status, "rounds": result.rounds, "reason": result.reason}
                    if result
                    else None
                ),
            }
            (self.session_dir / "state.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
            (self.session_dir / "transcript.md").write_text(self.transcript(), encoding="utf-8")
        except OSError:
            pass

    def transcript(self) -> str:
        lines = ["# duet session %s" % self.session_id, ""]
        if self.resumed_from:
            # This file holds only the turns this process took. The earlier ones
            # are still in the session they were said in, and overwriting them
            # here with a shorter file is how a resume would lose the argument.
            lines += ["Resumed from `%s`: this transcript starts at round %d, and rounds "
                      "1–%d are in that session's record."
                      % (self.resumed_from, self.start_round, self.start_round - 1), ""]
        lines += ["## Task", "", self.config.task, ""]
        if self.config.acceptance:
            lines += ["## Acceptance", "", self.config.acceptance, ""]
        for record in self.turns:
            env = record.envelope
            lines.append("---")
            lines.append(
                "## Round %d — %s (%s) — %s" % (record.round, record.agent, record.role, env.verdict)
            )
            lines.append("")
            if record.error:
                lines += ["> backend error: %s" % record.error, ""]
            if env.message:
                lines += [env.message, ""]
            if env.issues:
                lines.append("**Issues raised:** " + ", ".join("`%s` (%s)" % (i.id, i.severity) for i in env.issues))
            if env.resolves:
                lines.append("**Claims fixed:** " + ", ".join("`%s`" % i for i in env.resolves))
            if record.patch_log:
                lines.append("**Files written:** " + "; ".join(record.patch_log))
            if record.gate and not record.gate.skipped:
                lines.append("**Gate:** %s" % (record.gate.flaky if record.gate.ok and record.gate.flaky
                                               else "passed" if record.gate.ok else "FAILED"))
            lines.append("**Workspace:** `%s`" % record.digest[:8])
            lines.append("")
        return "\n".join(lines)

    def write_report(self, result: SessionResult) -> str:
        lines = [
            "# duet report — %s" % self.session_id,
            "",
            "**Outcome:** %s — %s" % (result.status, result.reason),
            "**Rounds:** %d of %d%s" % (
                result.rounds, self.config.max_rounds,
                " (resumed from `%s` at round %d)" % (self.resumed_from, self.start_round)
                if self.resumed_from else "",
            ),
            "**Workspace state:** `%s`" % result.digest[:8],
            "",
            "## Sign-off",
            "",
        ]
        for name in self.config.agent_names:
            verdict = self.state.last_verdict.get(name, "—")
            signoff = self.state.signoffs.get(name)
            mark = "yes" if signoff and signoff.digest == result.digest else "no"
            lines.append("- **%s** (%s): last verdict `%s`, signed off on this state: **%s**"
                         % (name, self.adapters[name].describe(), verdict, mark))
        if result.gate and not result.gate.skipped:
            lines += ["", "## Acceptance gate", "", "```", result.gate.render(1500), "```"]
        open_issues = self.state.open_issues()
        lines += ["", "## Issues", ""]
        if not self.state.issues:
            lines.append("No issues were raised.")
        else:
            for issue in self.state.issues.values():
                lines.append(
                    "- `%s` **%s** (%s, from %s) — %s%s"
                    % (issue.id, issue.title, issue.severity, issue.raised_by, issue.status,
                       ": " + issue.resolution if issue.resolution else "")
                )
        if self.config.jury:
            lines += ["", "## Jury", ""]
            if self.config.jury_convened:
                lines.append("- The simulated panel %s." % (self.jury_note or "sat %d time(s)"
                                                            % self.config.jury_convened))
                lines.append("- Its last report: `%s`" % (self.session_dir / "JURY.md"))
                if self.config.jury_convened >= self.config.jury_max:
                    lines.append("- It may not sit again this session (cap of %d reached)."
                                 % self.config.jury_max)
            else:
                lines.append("- The panel never sat: %s" % (
                    self.jury_note or "the pair never both signed off."))
            lines.append("- A simulated panel finds gaps; it is not market evidence.")
        if self.state.arbitrations:
            lines += ["", "## Arbitration rulings", ""]
            for item in self.state.arbitrations:
                lines.append("- round %s, `%s` decided by %s: %s"
                             % (item.get("round"), item.get("issue_id"), item.get("decider"), item.get("ruling")))
        if result.status != STATUS_CONSENSUS:
            lines += ["", "## What is left", ""]
            if result.gate:
                reasons = self.state.why_not_done(result.digest, result.gate.ok,
                                                  result.gate.problem())
            else:
                reasons = self.state.why_not_done(result.digest, False)
            lines += ["- %s" % r for r in reasons] or ["- nothing recorded"]
            if open_issues:
                lines.append("")
        lines += ["", "## Timeline", "", "```", "\n".join(self.history) or "(no turns)", "```", ""]
        path = self.session_dir / "report.md"
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            return ""
        return str(path)

    def commit(self, result: SessionResult) -> None:
        if not self.workspace.is_git_repo:
            return
        message = "duet: %s\n\nBoth agents signed off on workspace %s after %d rounds.\nSession: %s\n" % (
            (self.config.task.strip().splitlines() or ["work"])[0][:60],
            result.digest[:8],
            result.rounds,
            self.session_id,
        )
        self.workspace._git("add", "-A")
        # `.duet` is duet's own bookkeeping, and the workspace digest excludes
        # it for that reason — so committing it contradicts duet's own account
        # of what the workspace is. Left in, a session where the pair changed
        # nothing produced a commit whose entire content was duet's session
        # log, under a message claiming both agents had signed off on the work.
        self.workspace._git("reset", "-q", "--", ".duet")
        # `git diff --cached --quiet` exits 0 when nothing is staged.
        staged, _ = self.workspace._git("diff", "--cached", "--quiet")
        if staged == 0:
            self.emit("commit", ok=False,
                      output="nothing to commit: no file changed outside .duet")
            return
        code, out = self.workspace._git("commit", "-m", message)
        self.emit("commit", ok=code == 0, output=out.strip()[:400])
