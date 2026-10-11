---
name: duet
description: Hand a task to Claude Code and ChatGPT together, so they build it and check each other until both sign off. Use when the user asks for a second opinion from another model, a cross-check, "what would ChatGPT say", or wants two agents to work a task jointly. Also use when the user is about to ship something risky and wants an independent reviewer in the loop.
---

# duet — a second model in the loop

You are Claude Code. `duet` lets you bring ChatGPT in: either for a one-shot review of
what you just did, or for a full build where the two of you take turns until you both
sign off.

The point is that a single agent grades its own homework. duet puts something in the
loop that is allowed to say no, and gives that objection weight.

## Running it

```bash
duet run "the task" --gate "pytest -q" --detach
```

Run it with `--detach`: a session is many minutes of two agents taking turns, and the
shell that started it — your own tool shell, a terminal someone closes — can die long
before they finish. Without `--detach`, that kills the session with it, silently.
`--detach` starts it as its own process and returns at once with a session id and a log
path; poll progress with `duet status` (an id, or none for the newest) and `tail -f`
the log if you want to watch it live.

Both agents take turns in the workspace until they agree. It never ends on one agent's
say-so: both must vote DONE on the same workspace state, with no blocking objection open
and the gate passing.

If the project does not exist yet, use `build` instead of `run`: it sets the gate
before any code exists, so it starts red, and makes the pair agree the acceptance
criteria and write the failing tests before they implement anything.

```bash
duet build "a CLI that renames photos by the date in their EXIF" --detach
```

Use `build` only in a directory that is empty or nearly so. In a project that already
has tests, `run --gate` is the right command — its real test command is a better gate
than a starter one.

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
duet fix "slugify keeps punctuation: 'Hello, World!' gives 'hello,-world!'" --detach
```

**A website has a gate too.** When the project is a page rather than a test suite, use
`duet page check index.html` as the gate: it renders the page in headless Chrome at a
laptop width and a phone width and fails on what actually rendered — overflow, script
errors, contrast below WCAG AA, collapsed controls, blank strips, dead anchors, broken
local images. duet detects it by itself for a project with an `index.html` and no test
command. `duet page shot index.html` writes one full-page PNG per width; open them
rather than reading the CSS and imagining it.

**Market fit, not only passing tests.** `duet jury` sits a simulated panel — a user
who actually runs the product in a throwaway copy, a paying client, an investor, a
demo-day judge and a skeptic — and checks that every objection quotes something really
in the workspace or the walkthrough; unverified ones do not count. Exit 1 means a
verified blocker. Add `--jury` to `build`, `plan`, `add` or `run` and the panel sits when
both agents sign off, its verified blockers reopening the session as issues (at most
twice). Tell the user plainly it is models playing stakeholders: it finds gaps, it is
not market evidence.

**Build from references.** `duet make "<what to build>" --like https://app.example`
runs recon (public pages only, the one pass given web tools) into `make/recon.md` and
`make/features.csv`, then `plan`, then `build` (or `add`), then scores parity itself —
a done row needs a test or file that exists — and sweeps the product for the
references' names, domains and colours; exit 1 while a must-have is not done or the
sweep is dirty, and a build session cannot finish dirty. `--stage recon|plan|build|parity|sweep`
runs one. Tell the user the fine print: it rebuilds what an app does, never what it
owns — no code, logos, trademarks, copy, content or private APIs; public sources only;
rebrand before launch; not legal advice.

`fix`, `add` and `refactor` need the project's test command; if duet cannot find one,
it says so and stops, and you should pass `--gate`. Tell the user which rule the
session was held to when you report back — "both agreed, and the tests were replayed
against the original code and caught the bug" is a stronger claim than "both agreed".

To have the other model check work that already exists, give it that as the task:

```bash
duet run "review the current diff against the task; fix what is actually wrong" \
  --gate "pytest -q" --rounds 4 --detach
```

## Rules for you

- **Always pass `--gate`** with the project's real test or build command. It is what
  keeps "done" honest — the harness runs it and it overrides both agents' opinions. A
  session without a gate is two models agreeing by argument alone.
- **Never run `duet run` on uncommitted work the user cares about** without saying so
  first. Both agents edit the workspace directly. Suggest a branch.
- **Report the findings, not the exit code.** The user wants to know what ChatGPT
  objected to and whether it was right — say which findings you agree with and which
  you would push back on. You are allowed to disagree with the reviewer.
- **Do not re-run it to get a better answer.** If the reviewer raised something real,
  fix it. Running again until it approves is the exact failure this tool exists to
  prevent.
- **If one side is unavailable** — not signed in, out of quota — duet stops and prints
  a command under "Only have one of the two?" that pairs two models from the side that
  works. Run that command rather than giving up, **and tell the user plainly, before the
  result, that this session was two Claude models, not Claude and ChatGPT, and why.** Two models
  from one family share more blind spots than two vendors do; the user is owed that
  before they weigh the verdict. Never present it as a cross-vendor second opinion.
- If duet itself is missing, or *neither* side works, run `duet doctor` and give the
  user the fix it prints. Do not quietly review your own work and call it a second
  opinion.

## Reading the result

`duet run` writes `.duet/sessions/<id>/report.md` (outcome, who signed off on what,
every issue and its fate) and `transcript.md` (everything both sides said). When a
session ends without consensus, the report's "what is left" section is the useful part.

Both take `--json` if you need to parse rather than read.

## Picking the pair

`--pair` decides who is involved and who leads. The left one takes the first turn.

| pair | meaning |
|---|---|
| `claude+codex` | default — Claude Code leads, ChatGPT reviews |
| `codex+claude` | ChatGPT leads, Claude Code reviews |
| `claude+gpt` | ChatGPT via an API key instead of a ChatGPT login |
| `claude:opus+claude:sonnet` | two Claude models against each other |

If the user does not say, use the default.
