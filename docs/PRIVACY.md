# Privacy

duet is a command-line tool that runs on your computer. It has no server, no account
and no telemetry, and it collects nothing about you or your code.

**What leaves your machine.** duet starts the coding agents you already use: Claude Code
(`claude`), Codex (`codex`), or the OpenAI API if you configure that. Those tools send
your prompts and code to their own vendors, under their own terms and your own sign-in.
duet adds no other party. It never sees or stores your credentials, because each tool
keeps its own sign-in.

**What stays on your machine.** Each session's record (events, transcript, report and
gate logs) is written to `.duet/` inside your project. When you run `duet skill install` or
`duet skill default`, they write their instruction files into the agents' own folders
(`~/.claude`, `~/.codex`, `~/.gemini`), and `duet skill undefault` takes them out again.
Nothing is uploaded. Delete a project's `.duet/` folder and its records are gone.

**The plugin.** The Claude Code plugin adds the `/duet` command and a skill. It runs the
local `duet` command and reads files in your project. It makes no network requests of its own.

[SECURITY.md](../SECURITY.md) lists precisely what each agent is sent, what runs, what is
written to disk, and which risks remain.

Questions: https://github.com/shubharya-os/duet/issues
