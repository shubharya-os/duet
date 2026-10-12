# The duet protocol

Two agents in separate processes cannot see each other's context. They share exactly
two things: the workspace, and the envelope. Everything the harness decides is read
out of envelopes and out of the filesystem — never out of prose, and never out of an
agent's assurance about itself.

## The envelope

Every turn ends with exactly one fenced JSON object.

```json
{
  "message": "string — what you are telling your peer this turn",
  "verdict": "CONTINUE | DONE | BLOCKED",
  "issues": [
    {"id": "kebab-case", "title": "one line", "severity": "blocker|major|minor",
     "detail": "why it is wrong and what would fix it"}
  ],
  "resolves": ["issue-id"],
  "patches": [{"path": "rel/path.py", "action": "write|delete", "content": "complete file"}],
  "reads": ["path/you/want/to/see.py"],
  "summary": "one line",
  "confidence": 0.0,
  "ruling": {"issue_id": "...", "decision": "uphold|overrule|compromise",
             "rationale": "...", "action": "..."}
}
```

Only `message` and `verdict` are required. `ruling` appears only on an arbitration
turn.

### Parsing is deliberately forgiving

Models wrap JSON in fences, in prose, or in nothing; they sometimes show the schema
as an example before the real thing. The parser scans the whole reply for top-level
JSON objects and takes **the last one** carrying `verdict` or `message`. It also
normalises what models actually emit:

| written | read as |
|---|---|
| `"approved"`, `"ship"`, `"yes"`, `"complete"` | `DONE` |
| `"working"`, `"not_done"`, `"no"` | `CONTINUE` |
| `"stuck"`, `"help"` | `BLOCKED` |
| `"critical"`, `"high"`, `"fatal"` | `blocker` |
| `"medium"`, `"moderate"` | `major` |
| `"low"`, `"nit"`, `"trivial"` | `minor` |
| an unknown verdict | `CONTINUE`, with a note |
| `confidence: "very"` or `2` | `0.5` / clamped to `1.0` |
| an issue given as a bare string | an issue with that title |

Two rules are enforced at parse time, before any agent sees the result:

- **A reply with no envelope is `CONTINUE`, never `DONE`.** Silence, enthusiasm and
  malformed output are all treated as "not finished". The agent is asked for the
  envelope again on its next turn.
- **`DONE` alongside a `blocker` or `major` issue is downgraded to `CONTINUE`.** An
  agent cannot approve and object in the same breath.

## Issue lifecycle

Issues are the unit of debate. They are owned by whoever raised them.

```
            raised by B
                 │
                 ▼
              [open] ──────────── B withdraws ──────────▶ [resolved]
                 │
        A lists it in `resolves`
                 │
                 ▼
         [claimed_fixed]  ← still blocks a finish
                 │
         B's next turn:
         ├─ B does not re-raise it ─────────────────────▶ [resolved]
         ├─ B re-raises it ────────────────────────────▶ [open]
         └─ open too long (--max-debate) ──────────────▶ [arbitrated]
```

`open` and `claimed_fixed` both block a finish when the severity is `blocker` or
`major`. `minor` issues never block — they are recorded and shown, and the agents can
take them or leave them.

Verification happens at the *start* of the raiser's next turn, before anything else
in that turn is processed. So by the time an agent casts a `DONE` vote, it has
already implicitly ruled on every fix claimed against its own objections.

## Workspace state ids

The harness hashes every file in the workspace — path and contents, skipping `.git`,
`.duet`, `node_modules`, `__pycache__`, virtualenvs and build output — into a short
state id like `809799b2`.

A `DONE` vote records the state id it was cast against. The consensus check requires
every sign-off to point at the *current* id. Any file change invalidates every
sign-off that came before it, including the voter's own. This is what makes
"both agents said yes" mean something: they said yes to the same bytes.

## The acceptance gate

`--gate "<shell command>"` runs in the workspace before round one, after every turn
that votes DONE or BLOCKED, before any sign-off is counted, and once at the end, with
results cached per state id so it is not re-run needlessly. After a turn that says
CONTINUE it is deferred: the next agent is told it did not run on that state, and is
shown the most recent result that did run and which state it was for.
`--gate-every-turn` runs it after every turn. Both agents are shown the real command,
the real exit code and the real output (trimmed at both ends if long).

The gate is not advisory. `consensus_reached()` returns false while it fails or has
not run on the state being signed, no matter what either agent votes. Neither agent is
ever asked whether the gate passed.

## Turn structure

Each turn the active agent is sent, in this order: its role this round, the task, the
acceptance criteria, its peer's last message verbatim with their verdict, every
objection open against it, its own open objections, the workspace diff, any files it
asked to `read`, the gate output, a compact timeline of the session, a plain-language
list of why the session is still open, and a directive.

Directives are how the harness steers without editing anyone's words:

| directive | when |
|---|---|
| normal | the usual turn |
| final check | the peer signed off on the current state and the gate is green — this agent is the last signature, and is told not to rubber-stamp |
| arbitration position | an issue hit the debate limit; state a final position on it only, no edits |
| arbitration ruling | the decider rules and implements |
| stall | nothing changed in the workspace or the issue list for several rounds |
| missing envelope | the last reply had no envelope |

## Tools, or patches

An adapter declares `edits_workspace`:

- **true** (Claude Code, Codex CLI) — the agent has its own tools and edits the
  workspace directly. Its prompt tells it to run things and report what it saw.
- **false** (OpenAI API) — the agent builds through `patches`, sending complete file
  contents which the harness writes, and can request files it has not been shown via
  `reads`.

Both default adapters are subscription-authenticated CLIs: duet shells out to
`claude` and `codex`, which own their own OAuth tokens. duet holds no credential of
any kind, and its probes ask those CLIs directly (`claude auth status`,
`codex login status`) rather than inferring from the presence of a binary.

Both paths lead to real files, so either side can lead. Patch paths are resolved
against the workspace root and refused if they escape it or target `.git/` or
`.duet/`.

## Termination

| outcome | meaning |
|---|---|
| `consensus` | both signed off on the same state, nothing blocking, gate green |
| `exhausted` | hit the round limit — the report lists what was still open |
| `blocked` | an agent reported `BLOCKED`; it needs something a human has to provide |
| `error` | one backend failed twice on its own turns |
| `interrupted` | Ctrl-C |

Only `consensus` exits `0`.
