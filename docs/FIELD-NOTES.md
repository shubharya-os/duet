# Field notes: 422 sessions of two AI models working one task

What duet's own session logs show after seven weeks of daily use, and what changed
because of them.

**What this data is, and isn't.** Every number below comes from the event logs duet
writes for each session (`.duet/sessions/<id>/events.jsonl`), on one developer's
machine, from 23 September to 9 November 2026. It covers 422 sessions across a dozen
real projects: iOS apps, a game, web backends, and duet itself. Most sessions paired two
Claude models (Opus leading, Sonnet reviewing); only a handful paired Claude with Codex.
So these numbers describe how the harness behaves in practice. They are not a benchmark
of any model.

## 1 in 10 "done" agreements tested nothing

`duet fix` and `duet add` each hold the pair to one rule, and the harness checks it
itself once both models have said the work is finished. For `fix`, the new tests are
replayed against the original, unfixed code and must fail there. For `add`, a test must
fail against the code as it was before the feature.

In 212 sessions the rule was checked. In **22 of them, both models had already agreed
the work was done, and the rule said no**, 40 times in all:

| what the harness found after both models signed off | times |
|---|---|
| a feature's tests all passed on the code *without* the feature | 16 |
| a feature with no test added or extended at all | 13 |
| a bug fix whose tests all passed on the *unfixed* code | 10 |
| a bug the harness could not see reproduced at any point | 1 |

Each time, both sign-offs were cleared and the pair was told exactly what was missing.
8 of the 22 sessions then wrote a test that really failed without the change, and
finished. The other 14 ran out of rounds or stopped, which is the right outcome: none of
them could say "done" on work their tests did not cover.

That is the case for checking the rule rather than asking about it. Two capable models,
reviewing each other in good faith, still agreed on a test that would have passed
anyway, about one time in ten.

## The gate was most of the wall clock, then less of it

The gate is your test command, run by the harness rather than by either model. On
projects with slow gates (iOS builds with UI tests), it dominated:

- **Before 0.15** (89 sessions, 223 hours): the gate took 37% of all session time.
  65.7 of those hours were runs right after a turn whose verdict was CONTINUE. The agent
  had just said "not finished", so the gate was reporting on half-done work that nobody
  could sign off.
- **0.15 deferred the gate after CONTINUE turns.** Over the next 242 sessions, gate time
  after CONTINUE fell from 65.7 hours to 4.6. Those 4.6 hours are the `fix` sessions that
  still run it every turn until the bug has been seen failing.

The gate is still 47% of session time on 0.15, because these projects' gates are slow
(median run 9 minutes, 90th percentile 69 minutes). The largest remaining cost is the
opening gate before round one: 257 runs, 131 hours. In 47 of those runs, the same files
had already been checked by the same command within the previous 24 hours. duet already
reuses a remembered pass, and it is being taught to reuse a remembered failure for that
opening look as well, never as evidence for a rule or a sign-off.

## Deferring the gate taught the agents a bad habit

On 0.15, 67 of 242 sessions (28%) ended BLOCKED. Reading the final messages, 24 of those
votes were not real blockers. They were requests for a gate run, because the gate now
ran only after DONE or BLOCKED. In iOS projects the agents may not run `xcodebuild`
themselves, so the gate is their only compile check. One wrote: *"I couldn't run swift
test, swift build or fastgate.sh. I'm voting BLOCKED so the harness runs the gate on this
state: it is our only compile check."* BLOCKED stops the session, so a 0.15 saving
quietly turned into lost sessions.

Separately, in 61 turns agents complained that the gate output was cut before the
failure names. The harness kept the first and last parts of a long log, and xcodebuild
and pytest put the failing test names in the middle.

Both are being fixed together. An agent can ask for a gate run (`"run_gate": true`)
without ending anything. Failure lines are pulled out of the whole log and shown first,
and the full log is saved where agents can read it.

## Sessions died with whatever launched them

In the first weeks, sessions in other projects kept stopping with no record of why. The
cause was ordinary: a session belonged to the terminal or tool shell that started it,
and closing that, or quitting the app, killed both agents mid-turn. duet now detaches
(`--detach`), writes an "interrupted" record with the exact `duet resume` command when it
is stopped, and `duet status` tells a dead session from a running one.

The same logs showed a quieter problem: every project except duet's own repository had
been running an install five releases old, launched by path from an old virtualenv.
Nothing showed it. Since 0.14 every session records which duet ran it, and `duet status`
warns when a running session is on another version.

## What this does not show

- Whether duet makes a given model better at coding. Nothing here compares the same
  task with and without duet.
- How Claude and Codex compare as a pair. Almost every session here was Claude with
  Claude, partly because a retired model name in the local Codex config made Codex
  fail. duet now falls back to a model the login serves, instead of quietly running
  without a second vendor.
- Anything about teams. One developer, one machine.

The analysis is a few dozen lines of Python over `events.jsonl`. Every duet session
writes the same records, so the same questions can be asked of anyone's own logs.
