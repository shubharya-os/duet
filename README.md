# duet

[![ci](https://github.com/shubharya-os/duet/actions/workflows/ci.yml/badge.svg)](https://github.com/shubharya-os/duet/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![website](https://img.shields.io/badge/website-duet-1f7a6d)](https://shubharya-os.github.io/duet/)

**Read in:** English · [简体中文](docs/i18n/zh-CN.md) · [日本語](docs/i18n/ja.md) · [한국어](docs/i18n/ko.md) · [Español](docs/i18n/es.md) · [Português (BR)](docs/i18n/pt-BR.md)

**Two AI models build it. The harness checks the rules.**

<img src="docs/session.svg" alt="A duet session: Claude Code and ChatGPT taking turns until both sign off" width="760">

> **In one sentence:** AI writes most of the code now, and the same AI decides when
> it is finished. duet makes two AIs from two different companies work the same task
> and requires both to approve the identical result before it counts as done.
>
> **Why that matters:** an agent grading its own homework will tell you it passed.
> Two agents that must agree, with your tests run by neither of them, cannot quietly
> settle for "good enough" — one of them has to be convinced.
>
> **Does it work?** Given one sentence and an empty directory, it built an expense-tracker
> web app — single-page UI, JSON API, SQLite, CSV export — in **35 minutes**. Then it was
> attacked by hand, in a real browser as well as over HTTP: SQL injection stored as plain
> text, stored XSS rendered harmless, CSV formula injection defused, `0.1 + 0.2` totalled
> to exactly `0.30`. Every run is written down in [docs/QA.md](docs/QA.md) — including the
> ones where both agents agreed on something broken, and what caught it.
>
> *duet is a developer tool: it runs in a terminal and drives Claude Code and ChatGPT
> through your existing subscriptions.*

```bash
duet build "an expense tracker web app"      # from nothing: criteria, then tests, then code
duet fix "export drops rows with commas"     # its tests must fail on the original code
duet add "a --json flag on report"           # a test must fail without the feature
duet refactor "split parser.py in two"       # existing tests may not change by a byte
duet plan "move storage to Postgres"         # both argue it out; only PLAN.md may change
                                             # --quick: one draft, one review, ~5 min
duet review                                  # a second model reads your diff, read-only
```

Each one states a rule and **checks it** against what actually happened — the gate the
harness ran, the files on disk — rather than asking the agents whether they followed it.

To have Claude Code (and Codex) reach for these on their own — for bug fixes, features
and refactors, while questions and one-line edits stay direct — run it once:

```bash
duet skill default        # undo with: duet skill undefault
```

It writes one marked block into each harness's global instructions, touches nothing
else in them, and says before each session that it is handing the work to duet:

| harness | where | `/duet` command |
|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | ✓ |
| Codex | `~/.codex/AGENTS.md` | `$duet` |
| Gemini CLI and Google Antigravity | `~/.gemini/GEMINI.md` (they share it) | ✓ Gemini CLI |
| OpenCode | its own `AGENTS.md` if you have one — otherwise it already reads `CLAUDE.md` | ✓ |
| Cursor, and anything else that reads `AGENTS.md` | `duet skill default --project` writes the repository's | — |

Only harnesses you already use get files. OpenCode's `AGENTS.md` is never *created*,
because OpenCode reads `~/.claude/CLAUDE.md` only while that file is absent — creating it
would quietly cut OpenCode off from the rest of your rules.

You already do this by hand. Ask ChatGPT for a plan. Paste it into Claude Code. Copy
what Claude built. Paste it back to ChatGPT. "Looks good, but you missed the error
case." Paste that into Claude Code. Repeat until you get bored and ship it.

You are the messenger. duet is the messenger — and unlike you, it never gets bored,
never loses track of what was said four messages ago, and never lets the two of them
quietly agree they're finished.

```bash
curl -fsSL https://raw.githubusercontent.com/shubharya-os/duet/main/install.sh | sh
```

That does the whole thing: installs duet, then runs `duet setup`, which installs
the two agent CLIs if you do not have them, signs you in to both (your Claude and
ChatGPT plans — **no API keys**), and adds `/duet` to Claude Code and Codex.

Piped from `curl` the installer's own stdin *is* the script, so it reconnects to
your terminal to ask the questions setup needs. Where there is no terminal at all
— CI, a container — it installs `/duet` anyway and prints the one command left:

```bash
duet setup
```

Everything skips whatever is already done, so re-running is safe and is how you
update.

**Only have one of them?** duet still runs. `--pair claude:opus+claude:sonnet` puts
two Claude models against each other; `--pair codex+codex` uses two ChatGPT sessions
with fresh context each. Two vendors is the stronger pair — models from different
labs fail differently — but nothing in the mechanism depends on it, and when a side
is missing duet prints the one-subscription command with your own arguments already
in it.

**Or, from inside Claude Code, as a plugin** — the way ECC installs, in two commands:

```
/plugin marketplace add shubharya-os/duet
/plugin install duet@duet
```

That gives you `/duet` and the skill at about 160 tokens per session (ECC loads about
41,500). The first `/duet` checks for the CLI and, if it is missing, offers the one-line
`pipx install` and waits for your yes.

<details>
<summary>or with pip, if you would rather not pipe a script to a shell</summary>

```bash
pip install git+https://github.com/shubharya-os/duet
duet setup
```

On a system where Python is "externally managed" (Homebrew, modern Debian), plain
`pip install` is refused — use `pipx install git+…`, or the installer above, which
falls back to a virtualenv it owns at `~/.duet/venv`.
</details>

Then, in the middle of any Claude Code session:

```
/duet add retry with backoff to the fetch client
```

or in Codex, where it is a skill rather than a slash command:

```
$duet add retry with backoff to the fetch client
```

It takes the conversation you are already in — what you are building, what you ruled
out, what you already tried — hands it to both agents, and they work it until they both
sign off. You never leave the thread, and you never re-explain anything.

```
/duet running     check on a session in progress
/duet review      second opinion on the current diff
```

Starting a project rather than changing one? `duet build "<idea>"` sets the gate
**before any code exists**, so it is red from round one — the pair has to agree what
done means and write the failing tests before they are allowed to build anything.

```bash
duet build "a CLI that renames photos by the date in their EXIF"
```

---

## `/duet` — the part you actually use

`duet skill install` places four files, each in the exact directory its host scans:

| file | what it gives you |
|---|---|
| `~/.claude/commands/duet.md` | `/duet` in Claude Code |
| `~/.claude/skills/duet/SKILL.md` | Claude Code reaching for duet on its own |
| `~/.codex/skills/duet/SKILL.md` | duet in Codex — say `$duet`, or just ask for a second opinion |
| `~/.codex/prompts/duet.md` | `/duet` on Codex versions that read that directory |

Codex has skills rather than slash commands, so there it is `$duet` or plain English
("get a second opinion on this from Claude"). You can confirm Codex picked it up with
`codex debug prompt-input x | grep duet` — that is how the location above was verified,
after `~/.codex/prompts` alone turned out not to be enough.

The handoff is the point. Before it runs anything, your assistant writes down what it
already knows — the goal in your words, decisions already made and ruled out, what it
tried and what happened, which files matter — and passes that to the pair as **context,
not instructions**. The task still wins: nothing in the thread can quietly redefine what
you asked for.

Then it reports back like a colleague. Not "exit code 0" — what changed, what one of
them objected to, how it resolved, and whether *it* agrees. It is allowed to tell you the
two of them were wrong.

---

## What it does

### Coming from ECC?

Both install with one command. The difference is what you carry afterwards, and what
actually gets checked.

| | ECC 2.2.2, full install | duet |
|---|---|---|
| what there is to learn | 386 skills and 68 agents (as `claude plugin details` counts them), hooks, install profiles | 6 commands, one rule each |
| loaded into every session | ~41,500 tokens — ECC offers smaller profiles | ~545 tokens |
| README | 2,039 lines | ~600 lines |
| "write the failing test first" | an instruction to the agent | **checked**: the tests are replayed against the original code |
| the reviewer | a subagent of the same model | a second model, held read-only by its backend |
| domain skills | ~300 included | none shipped — duet's agents load whichever skills you install |
| harnesses | Claude Code, Codex, Kimi Code, OpenCode, Cursor | Claude Code, Codex, Gemini CLI, Antigravity, OpenCode, Cursor |

**Run against each other**, same brief, same model, ECC installed with its own agents —
full method and every caveat in [docs/QA.md](docs/QA.md):

| | duet | ECC | |
|---|---|---|---|
| build an app from one spec | **finished, 35 min** | not finished at 82 min (cut off by the non-interactive harness, not by ECC) | |
| security + validation attacks on the result | all defended | all defended | tie |
| 100 simultaneous writes, x3 | **300/300** | 218/300 | duet |
| its own test suite | passes | hangs after 31 tests | duet |
| planning, 5 planted pitfalls, graded blind | 4/5 and 5/5 | 4/5 and 5/5 | tie |
| planning time, full debate | 8–10 min | **3–4 min** | **ECC** |
| planning, `duet plan --quick`, fresh key, run side by side | 4/5 in 5 min 59 s | 4/5 in **5 min 23 s** | ECC by 36 s |

`--quick` is one draft and one review, reported as *reviewed* unless both sign the same file. Details, including a bug that run exposed, in [QA.md](docs/QA.md).

Switching without losing anything you rely on:

```bash
duet from-ecc                    # what you actually used from ECC, read from your own session logs
duet from-ecc --keep <name>      # keep that agent or skill, licence notice intact
claude plugin uninstall ecc@ecc  # you run this; duet never removes anything
duet skill default               # make duet what your agent reaches for
```

`duet from-ecc` reads your local Claude Code logs and sends nothing anywhere. On the
machine it was built on it found two ECC agents in use, once each, out of 454
skills and agents. The hand-run audit it replaced had got the same question wrong three ways —
an inflated count, a resumed session counted twice, and a missed agent that removing
ECC then broke. That is [written up](docs/QA.md) too.

### `duet review` — a second opinion, one command

The cheap one. It shows the other model your working-tree diff, the contents of any new
files git has not seen, and — with `--gate` — your test output, then prints what it
objects to. One call, no loop. The reviewer is held read-only by its backend, not asked
politely: `--disallowedTools Edit Write …` on the Claude side, `--sandbox read-only` on
the ChatGPT side.

```
$ duet review --gate "pytest -q"
duet review  ~/code/loader
  reviewer:  chatgpt  ChatGPT (Codex) (codex-cli), sandbox read-only
  gate:      pytest -q  passed

major (1)
  [clear-expired-mutates-during-iteration] clear_expired raises while deleting entries
      clear_expired() iterates self._entries.items() and deletes from self._entries
      inside that same loop, so Python raises RuntimeError: dictionary changed size
      during iteration. Iterate over a snapshot, e.g. list(self._entries.items()).

1 finding (1 major)
blocking: this change should not ship as it is.
```

That is a real finding from a real run — exit 1, because something blocking was raised.
Use it before you push.

### Market fit: `duet jury`

Tests say whether it works. They do not say whether anyone would use it, pay for it,
back it, or pick it over what they already have. `duet jury` puts that to a panel of
simulated stakeholders, each one a single read-only pass by an agent — held read-only
by the backend, the same way `duet review` is — alternating between your pair's two
models so the panel is not one model's opinion:

| juror | cares about | the one decision |
|---|---|---|
| `user` | does it work on the first try, does it beat what they use today | would you use it? |
| `client` | trust, onboarding, what is missing before buying | would you pay, and how much? |
| `investor` | who has the problem, how many, why now, alternatives, defensibility, model | would you invest? |
| `judge` | originality, execution, impact, clarity — each scored 1–10 | win or no win? |
| `skeptic` | the existing alternatives, and why people stay with them | would people switch? |

The `user` does not read about the product, it tries it: the command under the README's
"How to run it" runs in a throwaway copy of the workspace with a one-minute limit, and
what it printed is what the user juror judges. For a website it is rendered the way
`duet page check` and `duet page shot` do, and the user is shown the screenshots.

Every objection has to carry evidence — a quote from a workspace file with its path, or
from the walkthrough output — and **duet checks every quote** (whitespace aside). One
that is not really there is shown as `UNVERIFIED` and does not count.

```
duet jury                                  # stage read off the workspace
duet jury --stage idea --idea "a ledger for shared households"
duet jury --stage launch --panel user,client --rounds-of-questions 1
```

The stage defaults to `idea` for an empty workspace with `--idea`, `plan` when PLAN.md
exists beside little code, and `build` otherwise; `launch` also shows the panel the
README, any landing page and anything about pricing. `--rounds-of-questions N` gives
each juror N more passes in which it sees what the others found and may revise.

The report is `JURY.md` (in `.duet/jury/<time>/`, or `--out`): each juror's decision,
score and objections, the walkthrough transcript, and a summary — who would use it, who
would pay, who would invest, the judges' score, and the three objections most worth
fixing. Exit 0 when no verified blocker remains, 1 when one does, 3 when the panel
cannot sit (a bad `duet-jury.json`, an unknown juror, no juror answering).

Add your own jurors in `duet-jury.json` at the project root. Unknown keys are an error,
not ignored:

```json
{
  "personas": [
    {"name": "ops-lead", "role": "runs a small warehouse team",
     "cares_about": "setup time and who supports it", "decision": "Would you roll this out?"}
  ],
  "max_convenings": 2
}
```

**In a session:** `--jury` (and `--panel`) on `duet run`, `build`, `plan` and `add`.
When both agents sign off, the panel sits before the session may end. Each verified
blocker becomes an open issue raised by `jury:<persona>`, and the session carries on:
the pair fixes it — the fix is checked by the peer of whoever claims it, on the jury's
behalf — or argues it down like any other issue. The panel sits at most twice per
session (`max_convenings`), so it cannot keep one open forever; its last `JURY.md` is in
the session directory and the session report says what it decided.

It is a simulated panel — models playing stakeholders. It finds gaps worth closing. It is
not market evidence, and it does not replace putting the thing in front of real people;
every report says so.

### Build from references: `duet make`

```
duet make "a shared reading list for book clubs" --like https://bookly.example --like "Page Turner"
duet make "a shared reading list for book clubs"          # no references: research what exists
duet make --stage parity                                   # one stage, from what make/ already holds
```

From an idea — with reference apps if you have them — `duet make` runs five stages in
order, each reading what the one before wrote under `make/`. `--stage recon|plan|build|parity|sweep`
runs one on its own.

1. **recon** — one read-only research pass, and the only pass in duet given web search
   and fetch. With `--like` it reverse-engineers each reference from public pages (its
   site, help centre, pricing page, store listing, public walkthroughs) into
   `make/recon.md` — screens, flows, components, an inferred data model — and
   `make/features.csv`. Without `--like` it researches what already exists for the idea:
   the alternatives, what they lack, what users need. The features come back as data in
   the pass's JSON envelope, and the harness writes features.csv from them with Python's
   csv module, so a comma in a feature name cannot shift a row. A hand-edited
   features.csv is read and checked the same way. Every row is checked: `feature, area,
   priority (must|should|could), source, status (todo|done|partial|skip), evidence, notes`. A row whose source is not a public http(s)
   URL — none, a private address, credentials in it — is reported and does not count.
2. **plan** — `duet plan`, with recon.md and features.csv as context. The plan workflow's
   own check refuses a PLAN.md that does not name every must-have.
3. **build** — `duet build`, or `duet add` when the workspace already has code, with
   acceptance criteria generated from the must-haves. A website gets `duet page check` as
   its gate, as it would anyway.
4. **parity** — scored by the harness, not reported by the agents: must 3, should 2,
   could 1; done 1, partial 0.5, todo 0. A `done` row counts only when its evidence is a
   test name or a file path that exists in the workspace (`test_shared_shelf`,
   `tests/test_shelf.py::test_shared_shelf`, `src/shelf.py`); otherwise it is a todo.
   `skip` with a reason in notes, and features the references do not have (notes
   starting `extra:`), are listed separately and left out of the score. Writes
   `make/parity.md`, and prints every row that is dropped or left out of the score, with
   its line and the reason; exit 1 while any must-have is not done.
5. **sweep** — the clean room. The references' names, domains and brand colours (from
   `--like`, and `make/brand.json`: `{"avoid": [...], "domains": [...], "colors": [...]}`,
   which recon fills in and `--like` is remembered in) must not appear anywhere in the
   product: contents, identifiers (`booklyShelf`, `get_bookly_id`), file names,
   case-insensitively, colours in short hex form too (`#f36` for `#ff3366`). Neutral
   colours are ignored, and the sweep says which: white, black and greys (red, green and
   blue within 16 of each other) are in nearly every product, so they do not identify a
   reference. Recon is asked for the distinctive brand hues only. `.git`,
   `node_modules`, build output, lock files, binaries and `make/` are skipped. Exit 1
   while anything is found — and while `duet make` runs a build session, the pair's
   consensus is refused while the sweep is dirty, the way a workflow's rule is.

The references' users' complaints are `duet jury`'s job; `duet make` points to it at the
end rather than duplicating it.

**The fine print.** `duet make` rebuilds what an app does, never what it owns: features
and flows, clean-room. Never source code, logos, trademarks, copy, content or private
APIs. Public sources and your own account only — never behind someone else's login, a
paywall, or against a site's terms. Rebrand before launch. This is not legal advice.

Inspired by [replica-skill](https://github.com/Jakeschincariol/replica-skill) by Jake
Schincariol (MIT License); the code adapted from it carries its copyright and licence
notice.

### `duet build` — start from nothing, with a gate from round one

An empty directory is where duet's own mechanism used to lapse: there are no tests, so
nothing is detected, and the header reads `gate: none — they can only agree by
argument`. That is also where people build fastest and check least.

```bash
duet build "a CLI that renames photos by the date in their EXIF"
```

The gate is set **before any code exists**, so it starts red, and a red gate blocks both
sign-offs. The pair has to agree what done means and write the tests first — the order
is in the task, not left to their judgement, and the task names the exact gate command
so they cannot write tests it will not run. If the idea names a language, the gate
follows it; on a machine with no test runner at all, duet writes one into
`.duet/gate_unittest.py`, which needs nothing installed.

**Whatever gate is chosen is run once before round one, and refused if it is already
green.** That check exists because the obvious candidates all pass an empty directory:
`unittest discover` exits 0 with no tests before Python 3.12, and `go test ./...` exits
0 printing `[no test files]` for any package without them. A gate like that is not a
weak check — it is the absence of one, wearing the same clothes, and it is precisely the
false proof the double sign-off exists to rule out. Go and Cargo gates are wrapped in
the assertion duet actually makes: at least one test passed.

**Measured, not asserted.** Given "a URL shortener HTTP service … production quality:
input validation, correct status codes, no crash on any malformed request" in an empty
directory, `claude:opus+claude:sonnet` reached a double sign-off in **32 minutes, 6
rounds, 46 tests**. Checked afterwards against the running server rather than its own
suite: correct status codes on every unhappy path, codes surviving a restart, and 40
parallel identical POSTs producing exactly one `201` and thirty-nine `200`s. It also
passes on Python 3.9, which the previous session's artifact did not.

**An app, not just a tool:** a personal expense tracker — stdlib server, single-page UI,
JSON API, SQLite, CSV export, "safe against SQL injection and HTML injection" — reached a
double sign-off in **34m 57s**: 22 files, 2,963 lines, 10 test files. Attacked afterwards
by hand, in a real browser as well as over HTTP: SQL injection stored as text, stored XSS
rendered as literal text with zero injected elements, CSV formula injection defused,
`0.1 + 0.2` totalled to exactly `0.30`, 30 concurrent writes all surviving a restart, and
every malformed request answered with the right status code.

The same URL-shortener brief given to `claude:sonnet+claude:sonnet` finished in **13m 37s** — and
wrote a spec a third the size, with sequential (enumerable) short codes and no URL
deduplication, both of which its own criteria permit. Neither pair broke its rules.
The double sign-off guarantees the criteria are met; it does not choose the criteria,
and the faster run is the one that stated less. Both runs, and what the gate still
cannot catch, are written up in [docs/QA.md](docs/QA.md).

### `duet fix`, `add`, `refactor`, `plan` — the rule is checked, not asked

Agent workflows are usually prompts: *write a failing test first, don't change
behaviour, only plan.* The agent is told the rule and trusted to follow it, and an agent
grading its own work reports that it did. duet has a harness that runs the gate itself
and a veto over consensus, so each of these states its rule to the pair **and checks it
against what actually happened**. When both agents sign off and the rule isn't met, the
sign-offs are cleared and both are told which rule, and why.

| command | the rule | how it is checked |
|---|---|---|
| `duet fix "<bug>"` | the tests must catch the bug | today's tests are **replayed against the original code**; they must fail there and pass now. Deleting a test that existed is refused |
| `duet add "<feature>"` | something must test the feature | a test was added or changed, **and** the tests fail against the code as it was before |
| `duet refactor "<change>"` | behaviour must not change | every test that existed at the start ends **byte-for-byte unchanged**; refused outright if the suite is red before it starts |
| `duet plan "<goal>"` | plan, don't build | only `PLAN.md` may change, and it must not be empty. `--quick`: two turns, reported as *reviewed*, not signed off by both |

```bash
duet fix "slugify('Hello, World!') returns 'hello,-world!' — punctuation should be stripped"
```

The replay is the part worth explaining. The first version of `fix` asked only whether
the gate had ever gone red. The first live run beat it twice over: the pair wrote the
test and the fix in one turn, so the harness never saw red and vetoed a correct fix —
and one agent then **re-broke working code on purpose** to show it a red gate. The final
state was byte-identical to the one it had rejected. A replay runs the finished tests
against a snapshot of the original code instead, so it accepts that fix on the first
sign-off and rejects the one it should: a "fix" whose tests would have passed on the
buggy code too.

What each cannot prove is written in [`workflows.py`](duet/workflows.py) beside the
check. `fix` cannot tell that the failure on the original code is *this* bug — and when
the only failure is a test importing a helper the fix added, it says so in its verdict
rather than calling that proof. `refactor` cannot see behaviour no test covers. The
tasks tell the reviewer to check exactly those.

### `duet run` — both of them, until they agree

They take turns in your repo: one builds, the other reviews, they argue, they fix. It
ends when **both** vote DONE on the **same state of the workspace**, with no open
objection and your tests passing.

Either one can lead. Either one can say no. Neither can finish alone.

```bash
duet run "add retry with backoff to src/fetch.py, and a test that proves it" \
  --gate "pytest -q"
```

**duet finds your test command itself** and prints what it picked, so the flag people
most often forget is not required. Pass `--gate` to override it, `--no-gate` to run
without. It is the important part: your real test command, run by the harness
after every turn. Neither agent may finish while it fails, and neither is ever *asked*
whether it passed — they are both shown the actual output. Without it, the two of them
can only agree by argument.

### Websites: `duet page`

A website usually has no test command, which is exactly the case where two agents agree
by argument about CSS neither of them has seen rendered. `duet page` is the gate a
website can have: it opens the page in headless Chrome at a laptop width and a phone
width and prints one line per fault, measured on what actually rendered.

```bash
duet page check index.html              # 1440 and 375 by default
duet page check https://127.0.0.1:8080/ --allow-network --width 1440,414,375
duet page shot index.html               # one full-page PNG per width, into .duet/shots/
```

Exit 0 when the page is clean, 1 when it has faults, 3 when duet could not run the check
at all — no Chrome, no such page, a rules file that does not parse. That third code
matters: a session that reads "no Chrome on this machine" as "the page is broken" spends
its rounds hunting a bug that is not there.

The built-in rules are the ones that can be measured rather than argued about:

| rule | what it means |
|---|---|
| `overflow` | the document is wider than the viewport, so the page scrolls sideways |
| `script-error` | an uncaught exception or unhandled rejection while the page loaded |
| `contrast` | visible text below WCAG AA (4.5:1, or 3:1 for large text) against the background it really sits on |
| `collapsed-control` | an input, select, textarea or button under the 24px WCAG 2.5.8 target size |
| `empty-strip` | a visible block 48px or taller with no text, no media, no background, no border |
| `dead-link` | `href="#x"` where nothing on the page has id `x` |
| `broken-image` | a local image that did not load |
| `no-viewport-meta` | no `<meta name="viewport">`, so a phone lays the page out at desktop width |

Two of those are there because a prototype of this found them in a page whose author had
read the CSS a dozen times: a blank strip under a grid on every load, and an email field
that collapsed to 20px tall on phones and nowhere else. Anything you meant to leave empty
or tiny carries `data-duet-ignore`, which exempts it and everything inside it.

Your brand kit is not built in, because it is different for every project. It goes in
`duet-page.json` at the project root (or `--rules PATH`):

```json
{
  "page": "site/index.html",
  "styles": [
    {"selector": "h1, h2", "property": "font-family", "equals": "Metro Sans"},
    {"selector": ".cta",   "property": "min-height",  "at_least": 44},
    {"selector": "body",   "property": "font-size",   "one_of": [16, 17, 18]}
  ],
  "forbid_text_colors": ["#7a0000"],
  "forbid_text": ["\\bcoming soon\\b"]
}
```

Every check is against the *computed* style, so a font stack matches on its first family
and `min-height` is whatever the cascade ended up at. An unknown key is an error (exit 3),
not a comment: a mistyped rule that is silently ignored is worse than no rule, because it
looks like it is being checked. `duet page` with no subcommand prints the whole format.

This is wired into sessions, not just the command line. A project with no test command
but an `index.html` (at the root, or in `site/`, `public/`, `docs/` or `dist/`) gets
`duet page check <that page>` as its detected gate, and a `duet-page.json` says which
page it means. When the gate is a page check, both agents are told to run `duet page shot`
and **look at the screenshots**, because a page can pass every rule above and still be a
mess. And `duet-page.json` counts as the site's test suite, so `duet add` and `duet fix`
on a website are held to the same rule as anywhere else: the page rule is replayed against
the page as it was, and must fail there.

Chrome is driven over the DevTools protocol on a throwaway profile, with no Node and no
Playwright — the standard library only. Outside hosts are blocked unless you pass
`--allow-network`, so a gate's answer never depends on someone else's CDN being up.
Chrome is found via `DUET_CHROME`, then PATH, then where it installs on macOS and Linux.
Windows is not supported and says so rather than failing halfway.

---

## Install

**Requirements:** Python 3.9+, Node, a Claude plan and a ChatGPT plan.

```bash
curl -fsSL https://raw.githubusercontent.com/shubharya-os/duet/main/install.sh | sh
```

It is short and does nothing you could not do by hand — read it first if you
like. If you would rather:

```bash
npm install -g @anthropic-ai/claude-code @openai/codex
duet login
duet skill install
```

<details>
<summary>or from a clone</summary>

```bash
git clone https://github.com/shubharya-os/duet && cd duet
pip install .
```

Verified down to Python 3.9 with pip 21.2 and setuptools 58. If `duet` is not on your
PATH afterwards, `python3 -m duet` is always equivalent.
</details>

`duet login` runs each CLI's own browser sign-in. **No API keys** — both sides bill to
the subscription you already pay for. duet never touches a credential; it shells out
and afterwards asks each CLI whether it worked.

`duet login <side>` signs in just one side — useful after switching to a new ChatGPT
account, say. Name it by backend family (`claude`, `gpt`, `chatgpt`, `codex` — the
three ChatGPT words all mean the same sign-in backend) or by the agent's own name:

```bash
duet login gpt       # the ChatGPT side, however its agent is named
duet login chatgpt   # same side, the other common name for it
```

```bash
duet doctor
```

```
claude   ✓ signed in (claude.ai)
chatgpt  ✓ Logged in using ChatGPT — Codex usage quota not checked

ready. try: duet run "your task here"
```

`doctor` asks each CLI for its real login state rather than checking a binary exists —
a tool that runs but is signed out is the most common way this kind of thing wastes
your afternoon.

It also says what it has *not* checked. A ChatGPT account can be signed in and out of
Codex usage allowance at the same time, and nothing codex offers reports the remaining
allowance without spending a model call — which would bill your quota to tell you about
your quota, on every `doctor` run. So `doctor` claims only what `codex login status`
proves, and duet writes down the one moment the account itself says the allowance is
gone:

```
chatgpt  ✗ signed in, but out of Codex usage quota until 2026-10-18 23:18
             (the account said so at 2026-10-18 11:02)
         fix: wait for 2026-10-18 23:18, upgrade the plan at
              https://chatgpt.com/explore/plus, or run this pair another way:
              `--pair claude+gpt` uses an OpenAI API key instead, and
              `--pair claude:opus+claude:sonnet` uses two Claude models.
```

The note lives in `~/.duet/codex-quota.json`, it is dropped as soon as the stated reset
passes, and the first ChatGPT turn that succeeds clears it — because a turn that ran is
the only cheap proof the allowance is back.

<details>
<summary>Prefer an API key?</summary>

```bash
duet run "..." --pair claude+gpt
export OPENAI_API_KEY=sk-...
```

That backend has no tools of its own, so it builds by returning complete files which
the harness writes. Works fine — just billed per token instead of covered by a plan.
</details>

---

## Pick your pair

`--pair` decides who's involved and who goes first. The left one leads.

| pair | who does what |
|---|---|
| `claude+codex` *(default)* | Claude Code builds, ChatGPT reviews |
| `codex+claude` | ChatGPT builds, Claude Code reviews |
| `claude+gpt` | ChatGPT via API key instead of a login |
| `claude:opus+claude:sonnet` | two Claude models against each other |
| `codex+codex` | two ChatGPT sessions, fresh context each |

```bash
duet run "refactor the parser" --pair codex+claude --gate "pytest -q"
```

Cross-model is the interesting case: two models fail *differently*, so one catches what
the other's blind spot produced. Same-model pairs still work — a reviewer with no
memory of writing the code is a real reviewer — they just share more blind spots.

---

## What a real session looks like

This is duet writing a feature **inside its own repository** — real Claude Code, real
ChatGPT, gated by the test suite:

```
round 1 claude (lead) thinking...
     gate passed
  claude CONTINUE  state 5e7a9342
     Implemented `duet verify` in duet/cli.py, added tests/test_verify.py
     (11 tests), turned the README command block into a table.

round 2 chatgpt (reviewer) thinking...
  chatgpt DONE  state 5e7a9342
     Ran the gate myself: 87 passed. Checked --help and the empty-workspace
     path (exits 2 with guidance). I would ship this.

round 3 claude (lead) thinking...
     gate passed
  claude DONE  state d35aa23b
     Final check found a sentence of mine that was not true: verify exits 0 on
     a session recorded without a gate, having run nothing, and CI reads exit
     codes not prose. Fixed the docs. My edit moved the digest, so you need to
     re-sign.

round 4 chatgpt (reviewer) thinking...
  chatgpt DONE  state d35aa23b
     Re-reviewed the new state. Caveat matches the actual behavior. 87 passed.

BOTH AGENTS SIGNED OFF  both agents signed off on d35aa23b
```

Round 3 is why this exists. ChatGPT had **already approved**. Claude's final-check turn
then found a false claim in its own README, and fixing it changed the workspace — which
**voided ChatGPT's sign-off** and forced a fourth round. The second signature wasn't a
formality, and it wasn't free.

---

## How "done" is decided

Four conditions. All enforced by the harness. None negotiable by an agent.

1. **Both agents voted DONE.** One DONE is an opinion.
2. **Both votes point at the same workspace.** Every file hashes to a state id
   (`d35aa23b` above). A sign-off records the id it was cast against — touch any file
   afterwards and that sign-off is dropped. You cannot approve a version that no longer
   exists.
3. **No blocking objection is open.** Vote DONE while naming a blocker and your vote is
   downgraded automatically. You cannot approve and object in one breath.
4. **Your tests pass**, run by the harness on the real files. Neither agent is ever
   *asked* whether they passed.

### Objections belong to whoever raised them

```
chatgpt raises  "empty name is accepted"     → open
claude fixes it, lists it in `resolves`      → claimed_fixed   (still blocks)
chatgpt looks, agrees, drops it              → resolved
chatgpt looks, disagrees, re-raises          → open again
```

The fixer never closes its own ticket. This one rule is what stops two models from
politely agreeing their way to a broken result.

### Deadlocks get ruled on

If an objection survives `--max-debate` rounds, the harness stops the loop, takes a
final ≤200-word position from each side, and makes the decider rule and implement it.
The ruling goes in the report and can't be reopened. If they stop making progress at
all, the stall is detected and both are told to break it.

---

## Commands

```bash
duet review          # one-shot second opinion on the current diff
duet review --since main   # ...or on everything this branch adds
duet run "task"      # the full loop until both sign off
duet build "idea"    # start a project from nothing, gated from round one
duet fix "bug"       # no sign-off until the tests catch the bug in the original code
duet add "feature"   # no sign-off until a test fails without the feature
duet refactor "..."  # existing tests may not be edited; refused on a red suite
duet plan "goal"     # argue out PLAN.md; no code may change
duet page check ...  # the gate a website can have: render it, print every fault
duet page shot ...   # one full-page PNG per width, so both agents can look
duet jury            # a simulated stakeholder panel; every objection checked
duet make "idea" --like URL   # recon, plan, build, parity, clean-room sweep
duet resume          # carry on a session that died, keeping the argument
duet status          # what is the session doing right now (works mid-run)
duet verify          # does the last sign-off still hold? re-runs the gate
duet login           # sign both sides in
duet doctor          # real connection check, with the fix for each side
duet skill install   # use duet from inside Claude Code
duet skill default   # ...and make it the default for real code changes
duet from-ecc        # what you used from ECC, and how to switch without losing it
duet demo            # the whole loop offline — no keys, no network
duet report          # the last session's report (--transcript for everything said)
duet sessions        # every session in this workspace
```

Useful flags for `run`: `--gate` (your tests — the most valuable one), `--pair`,
`--rounds`, `--turn-timeout MINUTES`, `--gate-timeout MINUTES`, `--fresh-gate`, `--accept "what done means"`, `--swap N` to
trade places, `--commit`, `--json`.

### The clock, and the network

A turn gets 30 minutes by default. `--turn-timeout MINUTES` changes it on every
session command and on `duet resume`, and it is saved with the session, so a
resumed one keeps it. Both agents are told the budget in their prompt, with the
instruction to leave the workspace consistent and hand over before it runs out —
a lead that does not know it is on a clock tries to do everything in one turn.

The gate has a clock too: 15 minutes by default, `--gate-timeout MINUTES` to change
it, saved with the session like the turn limit. A gate that runs out of time is
reported as exactly that — "the gate ran out of time; this is not a test failure",
to you and to both agents — so nobody goes looking for a failing test that does not
exist. It still is not a pass: a session cannot end on a gate that never finished.

A gate that passes is remembered in `.duet/gate-cache.json`, keyed by the digest of
the workspace and the exact gate command, with when it ran, how long it took and its
output. A session — new or resumed — that opens on the same files with the same
command within 24 hours skips the opening run and says so: "gate passed (remembered
from 2026-10-10 09:12 (2 hours ago), same files, same command; --fresh-gate to
rerun)". Both agents see the remembered output, labelled the same way. That is all
it does: a failure or a time-out is never remembered, other files or another command
never match, and the gate a sign-off is counted against is always one run during the
session, so the pair still cannot finish without a fresh green run. `--fresh-gate`
on any session command or on `duet resume` ignores the cache for that run;
`duet verify` always runs the gate fresh. A cache file that is corrupt or unreadable
is treated as empty.

If a turn is cut off after it has changed the workspace, that is progress and not
a failure: the peer takes the next turn on the files it left, and is told the
previous turn was stopped and never reported. A turn cut off having changed
nothing still counts as a failed turn.

A turn that cannot reach the backend at all — DNS gone, connection reset, HTTP
5xx, overloaded, rate-limited — is re-sent through a backoff that waits out about
four minutes before the turn counts as failed, and a turn that never reached a
model does not spend a round of the budget. The status code counts as well as the
words: Claude Code prints `API Error: 429 {…}` with a JSON body and spells nothing
out, so a 429 or a 5xx is waited out, while a 400 or a 401 is not. Real failures
(not signed in, CLI missing, an exhausted allowance, a reply with no envelope) are
still reported at once, because waiting does not fix them.

A session is 20–40 minutes of two subscriptions, and an adapter timeout or a
Ctrl-C used to throw the whole argument away. `duet resume` picks it up from the
round after the last one that finished, with the open objections, both sign-offs,
the arbitration rulings and each agent's own thread with its backend intact:

```bash
duet resume                        # the newest unfinished session
duet resume 20250104-142211-9f3a   # or a named one
duet resume --rounds 20            # a new total budget, counting rounds already used
duet resume --turn-timeout 60      # longer turns than the session that died had
duet resume --gate "pytest -q"     # a different gate than the one it recorded
duet resume --pair claude:opus+claude:sonnet   # carry it on with a different pair
```

`--pair` on `resume` replaces the agents by position; everything else about the
session — rounds, open issues, sign-offs, the workflow state and its baseline —
stays. A side whose backend or model changed loses its saved adapter thread
(it belongs to the agent that is gone); a side that stayed the same keeps it.
This is the way out when a side runs out of usage quota mid-session: the error
duet prints when that happens names the exact command to run, with a pair that
does not lean on the same account.

With no id it takes the newest session that has not agreed yet, saying which
newer ones it stepped over and why. It refuses, and names what to type instead,
when that session already agreed, has no rounds left, saved no state, or does
not exist. The gate is checked again before the first new turn, because the
workspace can have changed while the session was dead — from the gate cache when
the files and the command are exactly those of a pass in the last day, otherwise
by running it (`--fresh-gate` to always run it).

---

## Cost and safety

- **The agents write to your working directory.** Run it on a branch you can throw away.
  duet refuses to write outside the workspace, or into `.git/` and `.duet/`.
- Claude Code runs under `acceptEdits`, not a blanket bypass, and is granted Bash for
  your gate's own commands and nothing wider.
- **Two agents cost roughly twice one agent, times the rounds.** `--rounds` is the
  throttle; the default of 12 is deliberately modest. `duet review` is a single call.
- duet holds no credentials. Each CLI owns its own sign-in.
- **duet has no server, and no telemetry.** Your code reaches exactly the vendors whose
  CLIs you already run — Anthropic through `claude`, OpenAI through `codex` — and no
  third party is added. [SECURITY.md](SECURITY.md) says precisely what is sent, what
  runs, what is written to disk, and which risks remain.

---

## Does it actually work?

[docs/QA.md](docs/QA.md) is the honest record: what ran live, what's only covered by
stubs, and the **thirty-one real bugs** the live runs found — including a signed-out CLI
that reported itself ready, and a `codex exec` that hung forever whenever stdin was a
pipe.

Four of those were found by pointing duet's own ChatGPT side at duet's core and asking
for correctness bugs. All four were real. That's the premise working on its author.

804 tests, no network or credentials needed. CI on Python 3.9, 3.11 and 3.13.

```bash
pytest -q
duet demo     # the real orchestrator against scripted peers
```

---

## "Why not just…"

**…use one agent and read the diff yourself?** Do, when you have time. duet is for the
changes you would otherwise skim. The reviewer's job is to be awake for the boring parts
you stopped checking around file nine.

**…ask the same model to review its own work?** You can, and it helps a little. But it
reviews with the same blind spot that produced the bug, and it is agreeable about its
own output in a way it is not about someone else's. Every bug in
[docs/QA.md](docs/QA.md) that ChatGPT found in this codebase was in code Claude had
already reviewed and been happy with.

**…just use your CI?** CI tells you a test failed. It does not tell you the test never
covered the case, that the fix papered over the cause, or that the README now claims
something untrue. duet runs your CI *and* has something read the change.

**…copy-paste between the two yourself?** That is exactly what this replaces, and you
already know how it goes: by round three you are summarising instead of pasting, you
drop the objection you did not fully understand, and both sides lose the thread. The
machine does not get bored at round three.

**Isn't this just two models agreeing with each other?** It would be, without the rules.
An objection closes only when the agent who *raised* it drops it. A sign-off dies the
moment the workspace changes. Your gate outranks both of them. Take those away and yes,
you would have two chatbots nodding — which is why they are enforced by the harness and
not by the prompt.

**What does it cost?** Roughly double one agent, times the rounds. `duet review` is a
single call. Both bill to subscriptions you already have, not per-token API keys.

---

## Limitations

- **Two models can be wrong together.** The double sign-off raises the floor; it isn't
  proof. The gate is what keeps them honest — always pass `--gate`.
- **Turns are sequential.** A 12-round session is 12 agent invocations.
- **Arbitration is a tiebreak, not an oracle.** It ends circular arguments by recording
  a decision. The report always says who ruled and why.

MIT licensed. Not affiliated with Anthropic or OpenAI.
