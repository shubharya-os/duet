"""`duet page` as part of a session, not as a command you type.

The three joins between a page check and the rest of duet: the gate a website
gets detected for it, the page rules counting as that website's test suite, and
the prompt that tells both agents to look at the screenshots. A website session
is exactly as good as these — a page check nobody runs is a script in a
repository, not a gate.

Nothing here imports `duet.page` or `duet.chrome`, and that is deliberate: every
case below is a claim about behaviour reachable through duet's long-standing
surface — `gate.detect`, `workflows.test_files`, `prompts.turn_prompt`, an
Orchestrator run — so against the code as it was before this feature each one
fails on what it asserts rather than on a module that is not there yet. duet's
own replay says an import error proves the test needs the new code, not that it
checks anything; these do not lean on one. The filename `duet-page.json` is
spelled out for the same reason.
"""

import json
import sys
from pathlib import Path

import pytest

from duet import gate as gate_detect, prompts, workflows

PAGE_RULES = "duet-page.json"


def test_the_prompt_tells_the_pair_to_look_at_the_page():
    shot = "duet page shot index.html"
    prompt = _turn_prompt(page_shot=shot)
    assert shot in prompt
    assert "LOOK AT THE PAGE" in prompt
    assert "data-duet-ignore" in prompt
    assert "LOOK AT THE PAGE" not in _turn_prompt(page_shot="")


def _turn_prompt(**kwargs):
    fields = dict(
        task="t", acceptance="a", round_no=1, max_rounds=8, role="lead",
        peer="gpt", peer_message="", peer_verdict="", digest="abc",
        workspace_view="", gate_text="PASSED", against_you=[], yours=[],
        history=[], why_open=[],
    )
    fields.update(kwargs)
    return prompts.turn_prompt(**fields)


def test_a_page_gate_also_lets_the_agents_take_the_screenshots(tmp_path):
    """Being told to look at the page is worth nothing if running the command is
    refused. Under `acceptEdits` an agent may write files and run nothing, and
    the Bash it is granted comes from the gate — which for a website is `duet`
    itself, so `duet page shot` is inside the same grant as `duet page check`.
    """
    from duet.adapters.claude_code import ClaudeCodeAdapter

    agent = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    agent.allow_gate("duet page check site/index.html")
    assert agent.allowed_tools == ["Bash(duet:*)"]

    venv = ClaudeCodeAdapter(name="claude", cwd=str(tmp_path))
    venv.allow_gate("/venv/bin/python -m duet page check index.html")
    assert venv.allowed_tools == ["Bash(/venv/bin/python:*)"]


# -- a website's gate --------------------------------------------------------

@pytest.mark.parametrize("where", ["", "site", "public", "docs", "dist"])
def test_a_site_with_no_tests_gets_its_page_as_the_gate(tmp_path, where):
    directory = tmp_path / where if where else tmp_path
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    found = gate_detect.detect(str(tmp_path))
    expected = "%sindex.html" % (where + "/" if where else "")
    assert found is not None
    assert found.endswith("page check %s" % expected)
    assert "no test command, but there is a page to check" in gate_detect.describe(found)


def test_a_rules_file_says_which_page_it_means(tmp_path):
    (tmp_path / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    (tmp_path / "landing.html").write_text("<p>hi</p>", encoding="utf-8")
    (tmp_path / PAGE_RULES).write_text(
        json.dumps({"page": "landing.html"}), encoding="utf-8")
    assert gate_detect.detect(str(tmp_path)).endswith("page check landing.html")


def test_a_rules_file_naming_a_page_that_is_not_there_falls_back(tmp_path):
    (tmp_path / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    (tmp_path / PAGE_RULES).write_text(
        json.dumps({"page": "gone.html"}), encoding="utf-8")
    assert gate_detect.detect(str(tmp_path)).endswith("page check index.html")


def test_a_rules_file_may_name_a_dev_server(tmp_path):
    (tmp_path / PAGE_RULES).write_text(
        json.dumps({"page": "http://localhost:4000/"}), encoding="utf-8")
    assert gate_detect.detect(str(tmp_path)).endswith("page check http://localhost:4000/")


def test_a_broken_rules_file_does_not_stop_gate_detection(tmp_path):
    (tmp_path / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    (tmp_path / PAGE_RULES).write_text("{not json", encoding="utf-8")
    assert gate_detect.detect(str(tmp_path)).endswith("page check index.html")


def test_a_project_with_real_tests_is_still_judged_by_them(tmp_path, monkeypatch):
    """A page check is the gate a website can have, not one that outranks a suite."""
    monkeypatch.setattr("duet.gate.shutil.which", lambda name: "/usr/bin/" + name)
    (tmp_path / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    assert gate_detect.detect(str(tmp_path)) == "pytest -q"


def test_a_directory_with_no_page_and_no_tests_still_gets_nothing(tmp_path):
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
    assert gate_detect.detect(str(tmp_path)) is None


def test_a_page_in_some_other_directory_is_not_guessed_at(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    assert gate_detect.detect(str(tmp_path)) is None


def test_the_detected_gate_can_actually_start(tmp_path):
    """A gate that cannot start fails every round and blocks both sign-offs, so
    the detected command names this interpreter when `duet` is not on PATH."""
    (tmp_path / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    found = gate_detect.detect(str(tmp_path))
    assert gate_detect.why_unusable(found, str(tmp_path)) is None


def test_the_gate_names_duet_by_path_when_the_name_is_not_on_path(monkeypatch):
    monkeypatch.setattr("duet.gate.shutil.which", lambda name: None)
    assert gate_detect.duet_command().endswith("-m duet")
    assert sys.executable.split("/")[-1] in gate_detect.duet_command()


# -- the page rules are a website's test suite ------------------------------

def test_the_rules_file_counts_as_a_test_file(tmp_path):
    """On a site the page rules *are* the suite: without this, `duet add` on a
    website has nothing to replay and every finish reports "no replay was
    possible"."""
    (tmp_path / PAGE_RULES).write_text("{}", encoding="utf-8")
    (tmp_path / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    found = workflows.test_files(str(tmp_path))
    assert PAGE_RULES in found
    assert "index.html" not in found              # the page itself is the code
    assert workflows.any_test_file(str(tmp_path)) is True


def test_some_other_json_is_not_a_test_file(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "duet-page.json.bak").write_text("{}", encoding="utf-8")
    assert workflows.test_files(str(tmp_path)) == {}


# A stand-in for `duet page check` with the same contract — exit 0 clean, exit 1
# on faults — so the workflow test below can prove the replay machinery treats
# page rules as the suite without paying for a browser twice. The version whose
# gate is the real `duet page check` is in test_page_chrome.py.
RULE_CHECK = '''\
import json, pathlib, re, sys

rules = {}
path = pathlib.Path("duet-page.json")
if path.is_file():
    rules = json.loads(path.read_text())
html = pathlib.Path("landing.html").read_text()
faults = []
for rule in rules.get("styles", []):
    found = re.search(re.escape(rule["property"]) + r":\\s*(\\d+)px", html)
    if not found or int(found.group(1)) < rule["at_least"]:
        faults.append("375: style-rule: .cta %s is too small" % rule["property"])
for line in faults:
    print(line)
sys.exit(1 if faults else 0)
'''

ORIGINAL_PAGE = '<style>.cta{display:block;min-height: 20px}</style><a class="cta">Go</a>'
FIXED_PAGE = '<style>.cta{display:block;min-height: 44px}</style><a class="cta">Go</a>'


def test_a_website_feature_is_proven_by_a_page_rule(tmp_path):
    """`duet add` on a website, end to end.

    The pair enlarges the tap target and writes the rule that catches it. The
    harness replays that rule against the page as it was, where it fails — which
    is the same proof a unit test gives on a project that has one. Without
    duet-page.json counting as a test file there is nothing to replay, and the
    workflow vetoes the sign-off instead.
    """
    from duet.adapters.mock import MockAdapter, envelope
    from duet.config import AgentSpec, Config
    from duet.orchestrator import Orchestrator

    (tmp_path / "landing.html").write_text(ORIGINAL_PAGE, encoding="utf-8")
    (tmp_path / "rulecheck.py").write_text(RULE_CHECK, encoding="utf-8")
    gate = "%s rulecheck.py" % sys.executable

    done = envelope("I would ship this", "DONE", confidence=0.9)
    work = envelope("bigger tap target, and the rule that catches it", "DONE", patches=[
        {"path": "landing.html", "content": FIXED_PAGE},
        {"path": PAGE_RULES, "content": json.dumps({
            "page": "landing.html",
            "styles": [{"selector": ".cta", "property": "min-height", "at_least": 44}],
        })},
    ])
    cfg = Config(task="a bigger tap target", root=str(tmp_path),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=6, workflow="add", gate=gate)
    orch = Orchestrator(cfg, adapters={
        "claude": MockAdapter(name="claude", cwd=str(tmp_path),
                              config={"script": [work, done, done]}),
        "gpt": MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": [done] * 3}),
    })
    result = orch.run()
    assert result.status == "consensus"
    state = cfg.workflow_state
    assert state.get("replay") == "fails on original"
    assert "fails against the code as it was" in workflows.get("add").proven(state)


def test_the_same_session_without_the_rule_is_vetoed(tmp_path):
    """The other half of the claim: the page alone proves nothing. Fixing the
    markup and writing no rule leaves a green gate and no test, which is exactly
    what `duet add` refuses."""
    from duet.adapters.mock import MockAdapter, envelope
    from duet.config import AgentSpec, Config
    from duet.orchestrator import Orchestrator

    (tmp_path / "landing.html").write_text(ORIGINAL_PAGE, encoding="utf-8")
    (tmp_path / "rulecheck.py").write_text(RULE_CHECK, encoding="utf-8")

    done = envelope("I would ship this", "DONE", confidence=0.9)
    work = envelope("bigger tap target", "DONE",
                    patches=[{"path": "landing.html", "content": FIXED_PAGE}])
    cfg = Config(task="a bigger tap target", root=str(tmp_path),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=6, workflow="add",
                 gate="%s rulecheck.py" % sys.executable)
    orch = Orchestrator(cfg, adapters={
        "claude": MockAdapter(name="claude", cwd=str(tmp_path),
                              config={"script": [work] + [done] * 5}),
        "gpt": MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": [done] * 6}),
    })
    result = orch.run()
    assert result.status != "consensus"
    events = [json.loads(line) for line in
              (Path(result.session_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
    veto = [e for e in events if e.get("kind") == "workflow_veto"]
    assert veto and "No test was added" in veto[0].get("reason", "")
