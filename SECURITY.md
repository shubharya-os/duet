# Security

duet runs two coding agents in your working directory and runs your test command.
This is what that means in practice, stated precisely, because "it uses your
existing subscriptions" is not a complete answer to "what leaves my machine".

## What leaves your machine, and to whom

duet has no server. It collects nothing, and there is no telemetry, analytics or
crash reporting anywhere in the codebase — `grep` for it.

Your code reaches exactly the vendors whose CLIs you are already running:

| backend | what it is | where your code goes |
|---|---|---|
| `claude-code` | the `claude` CLI, your Claude subscription | Anthropic, exactly as if you ran `claude` yourself |
| `codex-cli` | the `codex` CLI, your ChatGPT subscription | OpenAI, exactly as if you ran `codex` yourself |
| `openai-api` | opt-in, needs an API key | `https://api.openai.com/v1` — or `OPENAI_BASE_URL` if you set it |
| `mock` | scripted replies for tests | nowhere; no network at all |

duet adds no third party to that list. It is a harness around CLIs you have already
installed and signed into, and it holds no credentials of its own — each CLI owns
its own sign-in, in its own config.

What is sent is the task, the workspace diff, the contents of files an agent asks
to read, and the gate's output. That is the same material you would paste in by
hand; duet's contribution is that it stops being you doing the pasting.

## What runs on your machine

- **Both agents write to your working directory.** Run duet on a branch you can
  throw away. Patches that resolve outside the workspace are refused, as are
  writes into `.git/`.
- **Your gate runs as a shell command**, with `DUET_GATE=1` in its environment.
  Whatever you pass to `--gate` executes on your machine. duet auto-detects a gate
  when you do not pass one, and prints what it picked, precisely so an unexpected
  command cannot run unnoticed.
- Claude Code runs under `acceptEdits`, not a blanket bypass, and is granted Bash
  scoped to your gate's own commands, `duet gate` (which runs that same gate), and
  the read-only `cat`, `head`, `tail`, `grep`, `wc`, `ls` and `git status`. Not
  `git diff`, `git show` or `git log`, whose `--output=<file>` writes a file.
  "The gate's own commands" means each part of the gate's program run the way
  the gate runs it, and narrower variants of it: `make test` allows `make ...`,
  but for a shell or interpreter the rule runs through the script or module —
  `bash scripts/gate.sh` allows `bash scripts/gate.sh ...` and `python -m pytest
  -q` allows `python -m pytest ...` — never the bare `bash` or `python`, which
  would run any code at all. A part that is only inline code (`bash -c "..."`)
  grants nothing; the agent runs it through `duet gate`.
- `duet build` writes `.duet/gate_unittest.py` when your machine has no test
  runner. It is duet's own file, in duet's own directory, and it only discovers
  and runs test files under the workspace.

## What is written to disk

Everything duet records stays in `.duet/sessions/<id>/` inside the workspace:
the transcript, the event log, the saved state and the report. These contain the
task, both agents' messages and the gate output — so treat them as you would the
code itself. `.duet` is excluded from the workspace digest, and `--commit`
excludes it from commits.

## Residual risks worth naming

- **Two models can agree and still be wrong.** The gate is what keeps a session
  honest, and it only proves what it runs. See `docs/QA.md` for a session that
  ended in consensus on a program that crashed on an older interpreter.
- **Content in your repository is input to a model.** A file containing
  instructions aimed at an agent is a prompt-injection surface, the same as it
  would be if you opened that repository in Claude Code directly.
- **A task is not a sandbox.** duet constrains where patches land; it does not
  sandbox what your gate command does.
- **duet guards against careless agents, not hostile ones.** Its checks catch a
  pair that agrees too easily: tests that test nothing, a gate that was never run,
  a sign-off on files that changed since. An agent set on cheating could edit the
  gate script in your workspace, or forge the record of a `duet gate` run that the
  harness keeps in the session folder. duet only trusts a forged record of that
  kind for untracked, non-test files the agent has not edited by hand, and it never
  counts the agent's own gate run toward a sign-off. Still, review what a session
  changed before you ship it, as you would any contributor's work.

## Reporting a vulnerability

Open a GitHub issue for anything already public. For something that should not be
public yet, use GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability). Please include the duet version
(`duet --version`), your OS, and the smallest reproduction you can manage.
