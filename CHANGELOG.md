# Changelog

## Unreleased

- Fixed: `/duet running` from the skill waited on a permission prompt before it could
  run `duet status`, because the skill, unlike the command, did not pre-approve
  running duet. The skill now carries the same allowed tools.
- Fixed: agents who agreed kept a session from ending. Measured over 139 real
  sessions, a quarter of the turns in the ones that agreed came after the first
  DONE, and some sessions with green gates, no open issues and ten DONE votes
  still ran out of rounds — because each DONE came with a small edit, so the two
  signatures were never on the same files. Now:
  - once the peer has voted DONE on the current workspace, the agent's prompt says
    so plainly and sets the rule: agree by voting DONE without changing any file;
    change files only to fix something you would block on, raised as a blocker or
    major issue; anything nicer-to-have goes in as a minor issue, not an edit.
  - when an agent votes DONE and changes files after its peer's DONE anyway, both
    agents are told in their next prompt that the edit restarted the agreement and
    which files it changed, so the peer re-checks only those. The terminal shows it
    too, the timeline records it, and the event log has an `agreement_restarted`
    event. It survives `duet resume`.
  - minor issues still do not block consensus, and those still open at the end are
    listed in the session report under "Left for later".

## 0.10.0

- Fixed: sessions used to die silently. The usual way duet is launched — an agent's
  tool shell, or a terminal someone closes — killed the process with no SIGTERM or
  SIGHUP handler installed, so there was no `session_end`, no reason, and `duet status`
  read the missing event as "still running" forever. duet now:
  - catches SIGTERM, SIGHUP and SIGINT: it stops the running agent or gate child,
    saves state, and writes a `session_end` with status "interrupted" and a reason
    naming what was running ("stopped by SIGTERM during claude's turn in round 3"),
    plus `duet resume <id>` to continue. A second signal during that shutdown exits
    at once.
  - adds `--detach` to `run`, `build`, `fix`, `add`, `refactor`, `plan` and `resume`:
    it starts the session as its own process group, immune to the shell that launched
    it going away, writes its output to the session's log, and returns at once with
    the session id, the log path, and how to follow it (`duet status <id>`,
    `tail -f <log>`). Not supported on Windows. The `/duet` skill and command now
    start sessions this way and poll `duet status` instead of streaming output,
    since an agent's own tool shell is exactly the kind of launcher that dies.
  - `duet status` with no session lists recent sessions and recognises a dead one —
    no `session_end`, no live process behind it, silent longer than its turn and gate
    limits allow — as "stopped without finishing (process gone)", with what it was
    doing and the `duet resume <id>` command, instead of reporting it as running.
- Fixed: `duet make`'s clean-room sweep could never come back clean when recon
  listed generic colours. A brand.json with white, light grey and near-black
  next to the one hue that identifies the reference flagged every use of
  white. The sweep, and the build's sweep check, now ignore neutral colours:
  white, black and greys, meaning red, green and blue within 16 of each other.
  `--stage sweep` names the colours it ignored and says why. The recon prompt
  now asks only for the references' distinctive brand hues.
- Fixed: a comma in a feature name silently dropped the feature. Recon wrote
  features.csv by hand without quoting, so "Grouping and layering (z-order,
  ...)" shifted its columns and was rejected as malformed. The research pass
  now returns the rows as a `features` list in its JSON envelope, and duet
  writes make/features.csv with the csv module. A features.csv block from an
  agent that still sends one, and a hand-edited features.csv, are read and
  checked as before.
- Fixed: `duet make --stage parity` named rows that do not count only in
  make/parity.md. It now prints on the terminal every row that is dropped
  (e.g. "line 4: reminder emails — no source") or left out of the score
  (skipped with a reason, or `extra:`), with its line and the reason.
- Fixed: `/duet running` in Claude Code asked what to build instead of
  reporting the running session. Claude Code lets a skill answer `/<its name>`
  and the skill wins over the command, but only the command handled
  `running`, `status` and `review`. That handling now lives once, in
  `duet/skill/routing.md`, and is spliced into the skill, the command, the
  Codex skill and prompt, the Gemini and OpenCode commands and both plugin
  files, at the top of each. The plugin skill also checks that the duet CLI is
  installed, as the plugin command does.

## 0.9.0

- Added: `duet resume --pair A+B` carries a session on with a different pair,
  replacing the agents by position. Rounds, issues (with their raisers renamed
  consistently), sign-offs, the workflow state and its baseline all stay; only
  a replaced side's saved adapter thread is dropped, since it belongs to the
  agent that is gone. When a turn fails because a side is out of usage quota,
  the error now names this command with a pair that would work, e.g.
  `duet resume <id> --pair claude:opus+claude:sonnet`.
- Fixed: `duet login gpt` did not work with the default setup, because the
  default ChatGPT agent is named "chatgpt" and the parser only offered `claude`
  and `gpt` as choices. `duet login <side>` now accepts a side by backend
  family (`claude`, `gpt`, `chatgpt`, `codex`) as well as by the agent's own
  name.
- Fixed: `duet page` reported a setup fault for a Chrome that was only slow,
  not broken — "Google Chrome did not answer within 90s" could happen on a
  heavily loaded machine even though Chrome was still alive. A step that times
  out while Chrome is still running now gets one retry with a longer
  allowance before it counts as a setup fault; a Chrome that never starts (or
  dies) still fails at once with exit 3.
- Added: `duet make "<what to build>" [--like URL_OR_APP ...]` — recon, plan, build,
  parity and a clean-room sweep, run in order or one at a time with `--stage`, each
  reading what the previous wrote under `make/`. Recon is the only pass given web
  tools (Claude Code's WebSearch/WebFetch, Codex's web search); every features.csv row
  needs a public http(s) source or it does not count. The plan workflow refuses a
  PLAN.md that misses a must-have. Parity is scored by the harness (must 3, should 2,
  could 1; done 1, partial 0.5), a done row without a test or file that exists counts
  as todo, and any must-have not done exits 1. The sweep finds the references' names,
  domains and colours in contents, identifiers and file names, and a build session run
  by `duet make` cannot reach consensus while it is dirty. Inspired by replica-skill
  (MIT, Copyright (c) 2026 Jake Schincariol).
- Added: gate passes are remembered across sessions. Every session and every
  `duet resume` used to run the whole gate before round 1, even over files that had
  just passed that same gate — 5 to 15 minutes on a loaded machine before an agent
  did anything. A pass is now stored in `.duet/gate-cache.json` under the workspace
  digest and the exact gate command, and the opening check reuses one younger than
  24 hours, saying "gate passed (remembered from …, same files, same command;
  --fresh-gate to rerun)" to the user and in both prompts. Failures and time-outs
  are never stored or reused, and sign-offs are still counted only against a gate
  run during the session. `--fresh-gate` on every session command and on
  `duet resume` ignores the cache; `duet verify` always runs the gate fresh. A
  corrupt or unreadable cache file is ignored.
- Fixed: new files could vanish from what the reviewer is shown. The view was
  `git diff HEAD` (which never contains an untracked file) with a list of new file
  names after it, trimmed at 12,000 characters — so in a change whose main code is
  new files the list was the first thing cut. Every new file is now named first,
  with its size; the diff and the new files' text then share the rest of the budget
  fairly; binary and very large files are listed, not inlined; and when anything is
  trimmed, the view says exactly which files were not shown in full. Nothing is
  staged.
- Fixed: one bad gate run blocked a session until someone edited a file. A failure
  (or time-out) on files that a whole turn then left unchanged is re-run once before
  it is believed. A pass on the re-run is used and reported to the user and both
  agents as "the gate failed once and passed on a re-run with no file changes —
  possibly flaky: <failing tests>"; a failure that repeats stays a failure.
- Tests: the install.sh tests that assume no terminal now run the installer in a
  new session. Redirecting stdin was not enough — the installer reopens
  `/dev/tty` — so run from a terminal they took the interactive path and failed.

## 0.8.0

- Fixed: signing Codex in again with a different ChatGPT account still reported the
  old account's "out of usage quota until …" for another week. A successful
  `codex login` run by duet now forgets the recorded limit; a failed or cancelled one
  leaves it alone.
- Fixed: on a heavily loaded machine, a sign-in check that merely ran out of time
  refused to start the session and told a signed-in user to log in. The check is
  retried with three times the time, and if it still cannot answer the session starts
  with a warning; a definite "not signed in" still refuses.
- Fixed: a gate that ran out of time was reported as failing tests ("gate failed
  (exit 124)", "gate FAILING"), so both agents went looking for a failure that did
  not exist. It is now reported, to the user and in both prompts, as "the gate ran out
  of time; this is not a test failure", and still does not count as green.
  `--gate-timeout MINUTES` on every session command and on `duet resume`, saved with
  the session.
- Fixed: a command in an indented Markdown code block under "How to run it" was
  invisible, so `duet jury`'s user juror could not try the product and `duet build`
  showed no run line. Indented blocks now count; prose under the heading still does not.
- Added: `duet jury` — a simulated panel of stakeholders (user, paying client, investor,
  demo-day judge, skeptic, plus any in `duet-jury.json`) that judges the product at the
  idea, plan, build or launch stage. Each juror is one read-only pass, alternating
  between the pair's two backends. The user juror tries the product: the README's run
  command runs in a throwaway copy of the workspace with a time limit, or a website is
  rendered and screenshotted. Every objection must quote a workspace file or the
  walkthrough output, and the quote is checked; one that is not really there is shown
  as unverified and does not count. Writes `JURY.md`; exits 0 with no verified blocker,
  1 with one, 3 when it cannot sit. `--jury` on `run`, `build`, `plan` and `add` sits
  the panel when both agents sign off, turns its verified blockers into open issues
  raised by `jury:<persona>` (checked, once claimed fixed, by the claimant's peer), and
  sits at most twice per session. Every report says it is a simulated panel, not market
  evidence.
- Fixed: `duet page shot` got the size wrong on the pages it matters most on. A page
  with a fixed 900px panel, shot at `--width 375`, came out 375x1984 and nine parts
  blank under a document 147px tall — and the 542px of panel running off the right
  edge, the exact fault `duet page check` reports as `overflow`, was in no image
  either agent ever looked at. Under phone emulation a page wider than the viewport
  makes Chrome zoom the layout out to fit it on the screen, and `Page.getLayoutMetrics`
  reports *that* scrolling area rather than the document: 917x1984 for a page laid out
  at 375px and 147px tall. The shot now measures the document on the page itself —
  `documentElement.clientWidth` is still the box the layout was done in, so the numbers
  there are honest — and a page wider than that box is shot at its full width, with the
  reason next to the path: `wide-375w.png — page is 917px wide at 375px, so the image
  is too`. "Wider" is the `overflow` rule's own comparison, 1px tolerance included, so
  a shot is widened exactly when `duet page check` says the page scrolls sideways: a
  page with no viewport meta is laid out at Chrome's 980px desktop fallback on a phone
  and measures 981 against that 980px box, and that pixel is rounding rather than a
  page anyone can scroll. A document with no `<body>` at all — an SVG opened on its own
  — is measured by its root element's box, 1200x120 where the metrics said 2599. A page
  that fits is shot exactly as before, byte for byte (checked on this project's own
  `docs/index.html` at 1440 and 375 and on a page with no viewport meta), blank canvas
  under a short page included, because that is what a visitor's screen has.
  `MAX_SHOT_HEIGHT` still stops a very tall page and now stops a runaway-wide one too,
  saying which dimension it cut rather than turning the shot into a failure.

- Fixed: a session making real progress ended in ERROR on failures that had nothing to
  do with the work. Live, one feature session lost the lead's turn to the adapter's
  hard-coded 1800s limit twice — 1,500 lines of implementation, then 1,060 lines of
  tests, both on disk — and the "failed twice" rule ended it with "claude timed out
  after 1800s"; the limit could not be set from any command line, so recovering meant
  editing `.duet/sessions/<id>/state.json` by hand. Resumed, the machine lost DNS for a
  few minutes and three turns in a row failed instantly with "Can't reach the API
  server", which ended the session again and spent rounds 4–6 of the budget on turns
  that never reached a model. Now: `--turn-timeout MINUTES` on every session command and
  on `duet resume`, saved with the session so a resume keeps it, and stated to both
  agents in their prompt as their budget with the instruction to leave the workspace
  consistent and hand over before it runs out. A turn cut off *after* it changed the
  workspace is progress, not failure — the peer takes the next turn on the changed
  files, is told the previous turn was stopped and never reported, and the cut-off turn
  does not count toward "failed twice" (one that changed nothing still does). A turn
  that cannot reach the backend — DNS, connection reset, HTTP 5xx, overloaded, rate
  limited — is re-sent through a backoff totalling about four minutes before it counts
  as failed, and a turn that never happened gives its round back. Real failures (not
  signed in, CLI missing, an exhausted allowance, a reply with no envelope) are still
  reported at once. "Cannot reach the backend" is recognised from the status code as
  well as from prose: Claude Code prints `API Error: 429 {…}` and `API Error: 503` with
  a JSON body and spells nothing out, so a 429 or a 5xx attached to an error/status
  word counts, while a 400 or a 401 stays a real failure — waiting does not shorten a
  prompt or validate a key. codex's dropped stream ("stream disconnected before
  completion") counts too; its allowance message still wins over all of it. The
  words are matched as words, not as substrings: when a CLI reports `is_error` the
  reply's error is the agent's own message, so `tests/test_dns_resolver.py` or
  `app/dns.py` in a traceback used to read as a dead network and would have re-run
  a genuinely failed turn five more times before reporting it.

- Fixed: an ordinary Python project got "no test command was found" whenever duet's own
  interpreter had no pytest — which is exactly how duet is installed by pipx or by
  `install.sh` into `~/.duet/venv`. Detection only ever tried `pytest` on PATH and then
  `<duet's python> -m pytest`; it never looked at the project's own virtualenv, where the
  project's pytest and dependencies actually live. It now prefers `.venv/`, `venv/` or
  `env/` (`bin/pytest`, else `bin/python -m pytest`; `Scripts\` on Windows) over every
  other spelling — duet's virtualenv shares nothing with the project's, so its pytest
  could not import the code under test even when it existed — and falls back to a
  `python3` on PATH that has pytest. Nothing is handed back that has not been seen to
  answer `--version`, and the interpreter path is now shell-quoted, so a project under
  `~/My Projects/` no longer yields a gate whose first word is half a path.
- `duet page`: the gate a website can have. `duet page check <file-or-url>` renders the
  page in headless Chrome at 1440 and 375 (the phone one emulated at exactly that
  viewport width, which a Chrome window cannot be) and prints one line per fault —
  horizontal overflow, script errors, text below WCAG AA against the background it really
  sits on, controls under the 24px target size, tall blocks that paint nothing, dead
  in-page links, local images that did not load, a missing viewport meta. Exit 1 for page
  faults, exit 3 for "could not run", so a machine with no Chrome never looks like a
  broken page. `duet page shot` writes one full-page PNG per width, so both agents can
  see the page instead of reading its CSS. The brand-kit half — this heading font, that
  minimum tap target, never this colour, never that word — lives in `duet-page.json`,
  where an unknown key is an error rather than a rule that quietly is not checked.
  A project with no test command but an `index.html` (root, `site/`, `public/`, `docs/`
  or `dist/`) now gets a page check as its detected gate, `duet-page.json` counts as that
  site's test suite for `duet add` and `duet fix`, and a session whose gate is a page
  check tells both agents to look at the screenshots. Chrome is driven over the DevTools
  protocol on file descriptors 3 and 4 with nothing but the standard library, on a
  throwaway profile, with outside hosts blocked unless `--allow-network`; it is found via
  `DUET_CHROME`, PATH, then where it installs. Not supported on Windows, which it says.
- Fixed: duet could not see a Swift test. Test directories were matched
  case-sensitively, so Xcode's conventional `Tests/` never counted, and no glob knew
  about `*Tests.swift` — an iOS repo looked like a repo with no tests at all, so `add`
  said no test had been added and `fix` had nothing to replay. Three live sessions in
  one ended EXHAUSTED with a green gate and ~90 XCTest cases between them. Test target
  directories (`GalleryTests/`, `MyAppUITests/`) count too, and `Contests/` still does
  not. The same pass adds C#, Elixir, Scala, PHP and Dart suffixes.
- Under an `xcodebuild` gate, a test file written during the session is not in the
  original project.pbxproj, so replaying it against the original code would build the
  old target and pass without ever running it. That now reports "could not run" and
  says why, rather than "your tests pass on the original".
- `duet build`'s "is this gate green with nothing here to have passed" check asked a
  second, hand-copied list of test conventions, which had drifted the same way: no
  Swift, and `tests/` only at the top level. An iOS repo whose gate duet detected
  itself — a Makefile running `xcodebuild` — was refused with exit 3 for having no
  tests, next to its ninety XCTest cases. Both questions now go to one detector.
- Fixed: gate detection handed iOS projects `pytest -q`. macOS matches filenames
  without regard to case, so the test for "does this project have Python tests"
  — `(root / "tests").is_dir()` — was also true of Xcode's `Tests/`. The detected
  gate then collected nothing, exited 5 and was red every round, which blocks both
  sign-offs for the whole session. A directory is now read before it is believed;
  an empty `tests/` still means pytest, since that is where a greenfield project is
  about to write them.

## 0.7.0

- A Claude Code plugin, served from this repository: `/plugin marketplace add
  shubharya-os/duet`, then `/plugin install duet@duet`. About 160 tokens per session.
  Its files are generated from the same sources `duet skill install` writes, and
  `duet skill install` leaves Claude Code alone when the plugin is already there.
- `duet plan --quick`: one full draft, one review that may edit it. About ECC's planning
  time (5 min 59 s against 5 min 23 s, run side by side, tied 4/5 on a blind-graded key
  written first). Reported as *reviewed*, not as signed off by both, unless the reviewer
  changed nothing.
- Refused tool calls are seen by the harness. Claude Code's own `permission_denials`
  are read every turn; a refused edit goes back to the same agent before its peer sees
  the claim, and any other refused call reaches the peer marked unverified. In a live
  plan session that failure cost two rounds.
- `duet plan` agents may run the project's own test command (and nothing else), so a
  claim about what the tests do is checked rather than traced by hand.
- Fixed: the plan rule counted `.pytest_cache/` as a changed file. It now shares the
  workspace's ignore list.

## Earlier

- `duet doctor` no longer calls the ChatGPT side ready on the strength of a sign-in.
  An account can be signed in and out of Codex usage allowance at once, and
  `codex login status` only answers the first question, so the line now says what was
  actually checked. Quota is not guessed and never probed with a model call: duet
  records the moment the account itself reports the limit, reports it until the stated
  reset passes, and forgets it on the first turn that succeeds.
- `/duet` in Claude Code and Codex, installed by `duet skill install`, carrying the
  conversation you are already in across to both agents as context.
- `duet resume` — continue an interrupted session instead of starting over, keeping
  the issue ledger, both sign-offs and the round numbering.
- `duet review --since <ref>` — review everything a branch adds, not just uncommitted
  changes, which is what "review my branch" actually means.
- `duet status` — what a session is doing right now, read from the event log so it
  works mid-run.
- `duet review` — a one-shot second opinion on the working-tree diff, with the reviewer
  held read-only by its backend rather than by the prompt.
- `duet verify` — re-run the gate and check the last sign-off still describes this
  workspace.
- `duet resume` — carry on a session that died mid-argument instead of paying for the
  same 20–40 minutes twice. It rebuilds the orchestrator from the state file the dead
  session was already writing after every turn, so the open objections, both sign-offs,
  the arbitration rulings and the round numbering all survive.
- `--pair` — any two agents in either order: `claude+codex`, `codex+claude`,
  `claude+gpt`, `claude:opus+claude:sonnet`.
- Subscription sign-in on both sides. No API keys by default.
- Refuses to start a session from inside one, after an agent testing its own command
  nested a session and lost the turn to a 30-minute timeout.

`duet verify`, `duet review` and `duet resume` were written by duet itself. The bugs
each of those sessions exposed are recorded in [docs/QA.md](docs/QA.md).
