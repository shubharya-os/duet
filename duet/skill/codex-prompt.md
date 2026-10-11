---
description: Hand this to ChatGPT and Claude Code together — they build it and check each other until both sign off
argument-hint: <what you want built> | running | review
---

The user typed `/duet $ARGUMENTS` (if that placeholder is not substituted, use whatever
text followed the command).

{{ROUTING}}The user has been talking to you and should not have to repeat any of it.

**1. Write the handoff note** to `/tmp/duet-context.md`: what the user is trying to
achieve in their words, decisions already made and ruled out, constraints they stated,
what you already tried and what happened, and which files matter. Short and specific —
this goes to two engineers who cannot see this conversation. Omit anything you are unsure
of.

**2. The gate** — duet finds the project's test command itself and says what it picked.
Pass `--gate` only to override it. If it found none, say so: "done" is then just two
models agreeing.

**3. Run it, detached.** This tool call is itself a shell that can die — the turn gets
cancelled, the terminal closes — and that used to take the whole session down with it,
silently. `--detach` starts it as its own process, immune to that, and hands back a
session id and a log path at once instead of streaming:

```bash
{{DUET}} run "$ARGUMENTS" --context-file /tmp/duet-context.md --pair codex+claude --detach
```

Poll it with `{{DUET}} status <id>` every so often (it also takes no id, for the newest
session) until it reports consensus or a problem. `tail -f` the printed log if you want
to watch it live while you wait.

If the workspace is empty — no source, no tests, nothing to detect — use `build`
instead. It sets the gate before any code exists, so it starts red, and orders the work:
acceptance criteria first, failing tests second, implementation third.

```bash
{{DUET}} build "$ARGUMENTS" --context-file /tmp/duet-context.md --pair codex+claude --detach
```

**Pick the command by what the user is asking for** — each one holds the pair to a
rule the harness checks, so the right one matters more than the wording:

| the user wants to… | run | what the harness refuses to accept |
|---|---|---|
| build something new in an empty directory | `build` | a gate that is green before any code exists |
| fix a bug | `fix` | tests that would have passed on the buggy code too |
| add a feature to an existing project | `add` | tests that would have passed without the feature |
| restructure without changing behaviour | `refactor` | any edit to an existing test; a suite red at the start |
| decide how to do something, not do it | `plan` | any change except `PLAN.md` |
| anything else | `run` | — |

```bash
{{DUET}} fix "$ARGUMENTS" --context-file /tmp/duet-context.md --pair codex+claude --detach
```

`fix`, `add` and `refactor` need the project's test command; if duet cannot find one,
it says so and stops, and you should pass `--gate`. Tell the user which rule the
session was held to when you report back — "both agreed, and the tests were replayed
against the original code and caught the bug" is a stronger claim than "both agreed".


`--pair codex+claude` puts you (ChatGPT) in the lead and Claude Code as reviewer, which
matches the conversation the user is already in. Use `claude+codex` if they would rather
Claude lead.

**4. Report back properly.** Whether both signed off and on what; what actually changed,
read from the diff rather than relayed; the disagreement and how it resolved (see
`{{DUET}} report`); and your own view. If they agreed on something you think is wrong, say
so — you are the third opinion, not a courier.

## Rules

- Never run this over uncommitted work the user cares about without saying so first —
  both agents edit the workspace. Offer a branch.
- Do not re-run to get a nicer answer. Fix what was raised.
- **If one side is unavailable** — not signed in, out of quota — duet stops and prints
  a command under "Only have one of the two?" that pairs two models from the side that
  works. Run that command rather than giving up, **and tell the user plainly, before the
  result, that this session was two ChatGPT models, not ChatGPT and Claude, and why.** Two models
  from one family share more blind spots than two vendors do; the user is owed that
  before they weigh the verdict. Never present it as a cross-vendor second opinion.
- If duet itself is missing, or *neither* side works, run `{{DUET}} doctor` and give the
  user the fix it prints. Do not quietly review your own work and call it a second
  opinion.
