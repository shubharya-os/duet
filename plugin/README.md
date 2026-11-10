# duet

Two AI models work the same task in your repository, and the harness checks the rules.
One model builds, the other reviews, and nothing counts as done until both have approved
the identical files and your tests, run by the harness rather than by either model, pass.

Each workflow states a rule and checks it against what actually happened:

- `duet fix "..."`: the new tests are replayed against the original code; they must fail
  there and pass now.
- `duet add "..."`: a test must fail against the code as it was before.
- `duet refactor "..."`: every existing test must end byte-for-byte unchanged.
- `duet plan "..."`: both argue it out, and only PLAN.md may change.
- `duet review`: a second model reads your diff, read-only.

This plugin adds the `/duet` command and a skill that hands real code changes to duet.
It needs the duet command-line tool; the first `/duet` checks for it and, if it is
missing, offers the one-line install and waits for your yes. duet drives Claude Code
and Codex (or two Claude models) through the subscriptions you already have, with no
API keys.

Where your code goes: duet has no server and no telemetry. It runs on your machine
and passes the task, and the code it needs, to the agent CLIs it drives: Claude Code
sends it to Anthropic, and Codex sends it to OpenAI, each under your own sign-in and its
own terms. Session records stay in your project's `.duet/` folder. The details are in
https://github.com/shubharya-os/duet/blob/main/docs/PRIVACY.md

Documentation, the full command list and the measured results:
https://github.com/shubharya-os/duet
