"""What the agent says is not what the CLI reports.

Found live: a session ended "chatgpt failed twice: codex reported a sign-in
problem, but `codex login status` says Logged in using ChatGPT" — after turns
that had exited 0 and written their answer. The reviewer was reviewing
login-handling code and wrote "not logged in" in that answer, and the adapter
searched stdout+stderr for the phrase before it looked at the exit status.
codex also echoes the prompt into its output, so the task's own words counted.

These hold the adapters to: a turn that exited 0 with an answer is a success,
whatever the answer says; on a failed turn, only the CLI's own error output —
not the echoed prompt, not the agent's reply — names the failure.
"""

from fakebin import fake_cli
from duet.adapters.claude_code import ClaudeCodeAdapter
from duet.adapters.codex_cli import CodexCliAdapter, read_usage_limit

WORDS = ("Reviewed the sign-in handling.\n"
         "Not logged in\n"
         "ERROR: You've hit your usage limit. Try again at Nov 9th, 2026 11:00 PM.\n"
         "ERROR: The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account.")

# A codex that echoes its prompt the way the real one does (a banner, "user",
# then the prompt, on stderr), records every call, and then does $OUTCOME.
FAKE_CODEX = r'''
if [ "$1" = "login" ]; then
  echo login >> "$CALLS"
  echo "$LOGIN_STATUS"
  exit 0
fi
echo exec >> "$CALLS"
prompt="${@: -1}"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
echo "OpenAI Codex v0.167.0" >&2
echo "user" >&2
printf '%s\n' "$prompt" >&2
case "$OUTCOME" in
  answer)
    echo "codex" >&2
    printf '%s\n' "$WORDS" >&2
    printf '%s\n' "$WORDS"
    printf '%s' "$WORDS" > "$out"
    exit 0 ;;
  network)
    echo "ERROR: stream disconnected before completion: error sending request" >&2
    exit 1 ;;
  signed-out)
    echo "Error: Not logged in. Run \`codex login\` to sign in." >&2
    exit 1 ;;
esac
'''


def codex(tmp_path, monkeypatch, outcome, status="Logged in using ChatGPT"):
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("OUTCOME", outcome)
    monkeypatch.setenv("LOGIN_STATUS", status)
    monkeypatch.setenv("WORDS", WORDS)
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = fake_cli(tmp_path / "bin", "codex", FAKE_CODEX)
    return agent, calls


def calls_made(calls):
    return calls.read_text(encoding="utf-8").split() if calls.exists() else []


def test_a_codex_turn_that_answered_is_a_success_whatever_the_answer_says(tmp_path, monkeypatch):
    agent, calls = codex(tmp_path, monkeypatch, "answer")
    reply = agent.send("Review the sign-in code. Quote these if you find them:\n" + WORDS)
    assert reply.ok, reply.error
    assert "Not logged in" in reply.text and "usage limit" in reply.text
    assert calls_made(calls) == ["exec"]          # no sign-in re-check, no retry
    assert read_usage_limit() is None             # nothing written down as a quota hit
    assert "model_fallback" not in reply.meta     # and no model swapped


def test_a_failed_codex_turn_is_judged_by_codex_s_error_not_the_echoed_prompt(tmp_path, monkeypatch):
    """The prompt quotes a sign-out, a usage limit and a model refusal, each on
    its own ERROR line; the turn fails of something else entirely."""
    agent, calls = codex(tmp_path, monkeypatch, "network")
    reply = agent.send("The bug report says:\n" + WORDS)
    assert not reply.ok
    assert "stream disconnected" in reply.error
    assert "sign-in" not in reply.error and "not signed in" not in reply.error
    assert "does not serve" not in reply.error
    assert "login" not in calls_made(calls)
    assert read_usage_limit() is None


def test_a_real_codex_sign_out_is_still_reported(tmp_path, monkeypatch):
    agent, calls = codex(tmp_path, monkeypatch, "signed-out", status="Not logged in")
    reply = agent.send("go")
    assert not reply.ok
    assert "codex is not signed in" in reply.error and "codex login" in reply.error
    assert calls_made(calls) == ["exec", "login"]


def test_a_failed_claude_turn_is_not_called_a_sign_out_because_of_the_agent_s_answer(tmp_path):
    """claude exited non-zero after the agent answered (not flagged is_error):
    the answer mentioning /login is the agent's, not claude's."""
    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.bin = fake_cli(tmp_path / "bin", "claude", r"""
echo '{"type":"result","subtype":"success","is_error":false,"result":"The not logged in branch tells you to please run /login.","session_id":"s1"}'
echo "Killed: turn interrupted" >&2
exit 2
""")
    reply = agent.send("go")
    assert not reply.ok
    assert "not signed in" not in reply.error
