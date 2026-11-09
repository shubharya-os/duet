"""A Codex config that names a model the ChatGPT login no longer serves.

Found live: ~/.codex/config.toml said `model = "gpt-5.5"`, every codex turn
failed with "The 'gpt-5.5' model is not supported when using Codex with a
ChatGPT account", and the session ended "chatgpt failed twice" — duet quietly
became Claude alone. Codex keeps the models the login *can* use in
models_cache.json, and these tests hold duet to falling back to one of them.

Only names that existed before the fix are imported, so on the old code each
test fails on what it asserts rather than on an import.
"""

import json
from pathlib import Path

from fakebin import fake_cli
from duet.adapters.base import transient_failure
from duet.adapters.codex_cli import CodexCliAdapter
from duet.adapters.mock import MockAdapter, envelope
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator

REFUSAL = "ERROR: The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."

# A codex that serves only $GOOD_MODEL. Anything else — including no --model at
# all, which means "whatever config.toml says" — fails the way the real one did.
# Every call's model is appended to $CALLS, one line each ("-" for none).
FAKE_CODEX = r'''
out=""
model=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -m|--model) model="$2"; shift ;;
  esac
  shift
done
echo "${model:--}" >> "$CALLS"
if [ "$model" != "$GOOD_MODEL" ]; then
  echo "OpenAI Codex v0.167.0" >&2
  echo "model: ${model:-gpt-5.5}" >&2
  echo "WARN codex_core::models: Model metadata for \`${model:-gpt-5.5}\` not found" >&2
  echo "ERROR: The '${model:-gpt-5.5}' model is not supported when using Codex with a ChatGPT account." >&2
  exit 1
fi
printf '%s' "$REPLY" > "$out"
'''

CACHE = {"models": [
    {"slug": "gpt-reserve", "visibility": "hide", "priority": 1},
    {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 3},
    {"slug": "gpt-6-luna", "visibility": "list", "priority": 2},
]}


def codex_home(folder: Path, model="gpt-5.5", cache=CACHE) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if model is not None:
        (folder / "config.toml").write_text(
            'model = "%s"\nmodel_reasoning_effort = "high"\n\n[projects."/x"]\nmodel = "other"\n' % model,
            encoding="utf-8")
    if cache is not None:
        (folder / "models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    return folder


def setup(tmp_path, monkeypatch, good="gpt-6-luna", home=None, reply='{"message":"ok","verdict":"CONTINUE"}'):
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("GOOD_MODEL", good)
    monkeypatch.setenv("REPLY", reply)
    if home is None:
        home = codex_home(tmp_path / "codex-home")
    monkeypatch.setenv("CODEX_HOME", str(home))
    binary = fake_cli(tmp_path / "bin", "codex", FAKE_CODEX)
    return binary, calls


def models_called(calls: Path):
    return calls.read_text(encoding="utf-8").split() if calls.exists() else []


def test_a_refused_config_model_falls_back_to_the_best_listed_one_and_keeps_it(tmp_path, monkeypatch):
    binary, calls = setup(tmp_path, monkeypatch)
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = binary

    reply = agent.send("do the work")
    assert reply.ok, reply.error
    # the configured model first (no --model), then the listed one with the
    # lowest priority number: gpt-6-luna (2), not gpt-reserve (1, hidden)
    assert models_called(calls) == ["-", "gpt-6-luna"]
    fallback = reply.meta["model_fallback"]
    assert fallback["from"] == "gpt-5.5" and fallback["to"] == "gpt-6-luna"
    assert fallback["line"] == ("Codex: your config asks for gpt-5.5, which this ChatGPT "
                                "login does not serve; using gpt-6-luna")

    # later turns go straight to the model that works
    second = agent.send("next turn")
    assert second.ok
    assert models_called(calls) == ["-", "gpt-6-luna", "gpt-6-luna"]
    assert "model_fallback" not in second.meta          # said once, not every turn


def test_the_fallback_survives_a_resume(tmp_path, monkeypatch):
    binary, calls = setup(tmp_path, monkeypatch)
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = binary
    assert agent.send("first").ok

    resumed = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    resumed.bin = binary
    resumed.restore(json.loads(json.dumps(agent.state())))     # as it goes to disk
    assert resumed.send("after the resume").ok
    assert models_called(calls) == ["-", "gpt-6-luna", "gpt-6-luna"]


def test_codex_home_is_where_the_cache_is_read_from(tmp_path, monkeypatch):
    """CODEX_HOME moves all of Codex's files; ~/.codex is only the default."""
    decoy = codex_home(tmp_path / "home" / ".codex", cache={"models": [
        {"slug": "gpt-6-luna", "visibility": "list", "priority": 1}]})
    monkeypatch.setenv("HOME", str(decoy.parent))
    monkeypatch.setenv("USERPROFILE", str(decoy.parent))
    real = codex_home(tmp_path / "elsewhere", cache={"models": [
        {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 1}]})
    binary, calls = setup(tmp_path, monkeypatch, good="gpt-5.6-terra", home=real)
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = binary

    reply = agent.send("go")
    assert reply.ok, reply.error
    assert models_called(calls) == ["-", "gpt-5.6-terra"]


def test_hidden_models_and_the_refused_model_itself_are_never_picked(tmp_path, monkeypatch):
    home = codex_home(tmp_path / "codex-home", cache={"models": [
        {"slug": "gpt-reserve", "visibility": "hide", "priority": 0},
        {"slug": "gpt-5.5", "visibility": "list", "priority": 1},       # listed, but just refused
        {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 3},
    ]})
    binary, calls = setup(tmp_path, monkeypatch, good="gpt-5.6-terra", home=home)
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = binary

    assert agent.send("go").ok
    assert models_called(calls) == ["-", "gpt-5.6-terra"]


def test_an_explicit_model_falls_back_too_and_says_it_was_asked_for(tmp_path, monkeypatch):
    binary, calls = setup(tmp_path, monkeypatch)
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path), model="gpt-5.5")
    agent.bin = binary

    reply = agent.send("go")
    assert reply.ok, reply.error
    assert models_called(calls) == ["gpt-5.5", "gpt-6-luna"]
    line = reply.meta["model_fallback"]["line"]
    assert "--pair codex:gpt-5.5" in line and "using gpt-6-luna" in line


def test_no_cache_fails_with_the_fix_not_a_mystery_and_not_as_a_blip(tmp_path, monkeypatch):
    home = codex_home(tmp_path / "codex-home", cache=None)
    binary, calls = setup(tmp_path, monkeypatch, home=home)
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = binary

    reply = agent.send("go")
    assert not reply.ok
    assert models_called(calls) == ["-"]                # nothing to retry with
    assert "gpt-5.5" in reply.error
    assert "does not serve" in reply.error
    assert "config.toml" in reply.error
    assert "--pair claude+codex:<model>" in reply.error
    # the orchestrator's backoff must not wait this out: it never improves
    assert transient_failure(reply.error) == ""


def test_an_unreadable_cache_or_one_with_nothing_listed_gives_the_same_fix(tmp_path, monkeypatch):
    for cache in ("not json", {"models": [{"slug": "gpt-reserve", "visibility": "hide", "priority": 1}]}):
        home = tmp_path / ("home-%d" % len(str(cache)))
        codex_home(home, cache=None)
        (home / "models_cache.json").write_text(
            cache if isinstance(cache, str) else json.dumps(cache), encoding="utf-8")
        binary, calls = setup(tmp_path, monkeypatch, home=home)
        agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
        agent.bin = binary
        reply = agent.send("go")
        assert not reply.ok and "does not serve" in reply.error and "config.toml" in reply.error


def test_a_fallback_that_is_refused_too_is_tried_once_and_not_kept(tmp_path, monkeypatch):
    binary, calls = setup(tmp_path, monkeypatch, good="nothing-this-login-serves")
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = binary

    reply = agent.send("go")
    assert not reply.ok
    assert models_called(calls) == ["-", "gpt-6-luna"]           # one retry, not a walk
    assert "gpt-5.5" in reply.error and "gpt-6-luna also failed" in reply.error
    assert "config.toml" in reply.error
    assert agent.model == ""                                      # back to the config's model


def test_metadata_warning_then_a_400_is_the_same_failure(tmp_path, monkeypatch):
    """The other shape codex reports it in: no metadata for the model, then a
    400 model_not_found, without the ChatGPT-account sentence."""
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("CODEX_HOME", str(codex_home(tmp_path / "codex-home")))
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = fake_cli(tmp_path / "bin", "codex", r'''
out=""; model=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; -m|--model) model="$2"; shift ;; esac
  shift
done
echo "${model:--}" >> "$CALLS"
if [ -z "$model" ]; then
  echo "WARN Model metadata for \`gpt-5.5\` not found" >&2
  echo "ERROR: unexpected status 400 Bad Request: {\"error\":{\"type\":\"invalid_request_error\",\"code\":\"model_not_found\",\"message\":\"model unavailable\"}}" >&2
  exit 1
fi
printf ok > "$out"
''')
    reply = agent.send("go")
    assert reply.ok, reply.error
    assert models_called(calls) == ["-", "gpt-6-luna"]


def test_an_unrelated_400_is_not_mistaken_for_it(tmp_path, monkeypatch):
    """A different bad request — and the refusal's words quoted in the echoed
    prompt, as they would be in a duet session about this very bug — must be
    reported as it is, not answered by swapping the model."""
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("CODEX_HOME", str(codex_home(tmp_path / "codex-home")))
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = fake_cli(tmp_path / "bin", "codex", r'''
echo x >> "$CALLS"
echo "user" >&2
echo "  The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account." >&2
echo "  Model metadata for \`gpt-5.5\` not found" >&2
echo "ERROR: unexpected status 400 Bad Request: context_length_exceeded" >&2
exit 1
''')
    reply = agent.send("go")
    assert not reply.ok
    assert models_called(calls) == ["x"]                 # one call: no model swap
    assert "context_length_exceeded" in reply.error
    assert "does not serve" not in reply.error


def test_model_not_found_without_a_400_is_not_treated_as_a_refusal(tmp_path, monkeypatch):
    """A code in an error line is not sufficient without the documented 400."""
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("CODEX_HOME", str(codex_home(tmp_path / "codex-home")))
    agent = CodexCliAdapter(name="chatgpt", cwd=str(tmp_path))
    agent.bin = fake_cli(tmp_path / "bin", "codex", r'''
echo x >> "$CALLS"
echo 'ERROR: model_not_found: local metadata lookup failed' >&2
exit 1
''')
    reply = agent.send("go")
    assert not reply.ok
    assert models_called(calls) == ["x"]
    assert "does not serve" not in reply.error


def _probe(tmp_path, monkeypatch, cache):
    codex_home(tmp_path / "codex-home", cache=cache)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    binary = fake_cli(tmp_path / "bin", "codex", 'echo "Logged in using ChatGPT"')
    monkeypatch.setattr("duet.adapters.codex_cli.Adapter.which", staticmethod(lambda *a: binary))
    return CodexCliAdapter.probe()


def test_doctor_flags_a_configured_model_the_login_does_not_list(tmp_path, monkeypatch):
    probe = _probe(tmp_path, monkeypatch, CACHE)
    assert probe.ok                       # a turn falls back, so it is not a refusal
    assert "gpt-5.5" in probe.warning
    assert "config.toml" in probe.warning and "--pair claude+codex:<model>" in probe.warning
    assert "gpt-6-luna" in probe.warning  # what turns will use instead


def test_doctor_is_quiet_when_the_configured_model_is_listed_or_unknown(tmp_path, monkeypatch):
    listed = {"models": [{"slug": "gpt-5.5", "visibility": "list", "priority": 1}]}
    assert _probe(tmp_path / "a", monkeypatch, listed).warning == ""
    assert _probe(tmp_path / "b", monkeypatch, None).warning == ""     # no cache: nothing known


def test_doctor_warns_when_cache_has_no_listed_models(tmp_path, monkeypatch):
    probe = _probe(tmp_path, monkeypatch, {"models": [
        {"slug": "gpt-reserve", "visibility": "hide", "priority": 1},
    ]})
    assert "gpt-5.5" in probe.warning
    assert "does not list" in probe.warning
    assert "no listed model is available" in probe.warning
    assert "config.toml" in probe.warning


def test_a_session_records_the_fallback_as_an_event_and_in_the_report(tmp_path, monkeypatch):
    done = envelope("ship it", "DONE", confidence=0.9)
    binary, calls = setup(tmp_path, monkeypatch, reply=done)
    work = tmp_path / "work"
    work.mkdir()
    codex = CodexCliAdapter(name="chatgpt", cwd=str(work))
    codex.bin = binary
    cfg = Config(task="build the thing", root=str(work),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("chatgpt", "codex-cli")],
                 start="claude", max_rounds=4)
    orch = Orchestrator(cfg, adapters={
        "claude": MockAdapter(name="claude", cwd=str(work), config={"script": [done, done]}),
        "chatgpt": codex,
    })
    result = orch.run()

    events = [json.loads(line) for line in
              (Path(result.session_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    fallbacks = [e for e in events if e.get("kind") == "backend_model_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["from"] == "gpt-5.5" and fallbacks[0]["to"] == "gpt-6-luna"
    assert fallbacks[0]["agent"] == "chatgpt"
    assert not [e for e in events if e.get("kind") == "turn_error"]
    report = (Path(result.session_dir) / "report.md").read_text(encoding="utf-8")
    assert ("Codex: your config asks for gpt-5.5, which this ChatGPT login does not "
            "serve; using gpt-6-luna") in report
