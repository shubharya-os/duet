---
description: Hand this to Claude Code and ChatGPT together — they build it and check each other until both sign off
argument-hint: <what you want built> | running | review
allowed-tools: Bash({{DUET}}:*), Bash(duet:*), Bash(git:*), Bash(ls:*), Bash(cat:*), Read, Write
---

The user typed `/duet $ARGUMENTS`.

{{ROUTING}}The user is mid-conversation with you. They should not have to re-explain any of it, and
they should not lose the thread. So:

**1. Write down what you already know.** Create a scratch file (`/tmp/duet-context.md`)
containing the parts of *this* conversation that the two agents would otherwise have to
rediscover:

- what the user is actually trying to achieve, in their words where possible
- decisions already made and explicitly ruled out, and why
- constraints they stated: style, libraries they will not add, files not to touch
- what you have already tried in this session, and what happened
- the files that matter and what you know about them

Be specific and short. This is a handoff note to two engineers who cannot see your
screen, not a summary for the user. Leave out anything you are unsure of — a confident
wrong note is worse than a missing one.

**2. The gate.** duet finds the project's test command itself and prints what it picked.
Only pass `--gate` when it would pick wrong, or when there is something better than the
obvious one — a subset that runs fast, or a lint step worth including. If it reports
finding none, say so in your reply: "done" is then only the two of them agreeing.

**3. Run it, detached.** This tool call is itself a shell that can die — the turn gets
cancelled, the terminal closes — and that used to take the whole session down with it,
silently. `--detach` starts it as its own process, immune to that, and hands back a
session id and a log path at once instead of streaming:

```bash
{{DUET}} run "$ARGUMENTS" --context-file /tmp/duet-context.md --detach
```

Poll it with `{{DUET}} status <id>` every so often (it also takes no id, for the newest
session) until it reports consensus or a problem. `tail -f` the printed log if you want
to watch it live while you wait.

If the workspace is empty — no source, no tests, nothing to detect — use `build`
instead. It sets the gate before any code exists, so it starts red, and orders the work:
acceptance criteria first, failing tests second, implementation third.

```bash
{{DUET}} build "$ARGUMENTS" --context-file /tmp/duet-context.md --detach
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
{{DUET}} fix "$ARGUMENTS" --context-file /tmp/duet-context.md --detach
```

`fix`, `add` and `refactor` need the project's test command; if duet cannot find one,
it says so and stops, and you should pass `--gate`. Tell the user which rule the
session was held to when you report back — "both agreed, and the tests were replayed
against the original code and caught the bug" is a stronger claim than "both agreed".


Add `--pair codex+claude` if the user wants ChatGPT to lead.

**4. Report back like a colleague, not a log.** When it finishes:

- Say whether both agents signed off, and on what.
- Say what actually changed — read the diff yourself, do not just relay their summary.
- Pull out the disagreement. The interesting part of any session is what one of them
  objected to and how it got resolved; that is in `{{DUET}} report`.
- Say what you think. If they agreed on something you believe is wrong, say that
  plainly. Two agents agreeing is not proof, and you are the third opinion.
- If it ended without consensus, the report's "what is left" section is the useful part.

## Rules

- **Never run this on uncommitted work the user cares about without telling them first.**
  Both agents edit the workspace directly. Offer a branch.
- **Do not re-run it to get a nicer answer.** If the reviewer raised something real, fix
  it. Re-rolling until it approves is exactly the failure this tool exists to prevent.
- **If one side is unavailable** — not signed in, out of quota — duet stops and prints
  a command under "Only have one of the two?" that pairs two models from the side that
  works. Run that command rather than giving up, **and tell the user plainly, before the
  result, that this session was two Claude models, not Claude and ChatGPT, and why.** Two models
  from one family share more blind spots than two vendors do; the user is owed that
  before they weigh the verdict. Never present it as a cross-vendor second opinion.
- If duet itself is missing, or *neither* side works, run `{{DUET}} doctor` and give the
  user the fix it prints. Do not quietly review your own work and call it a second
  opinion.
