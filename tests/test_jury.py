"""`duet jury`: a simulated stakeholder panel, held to evidence.

Every juror here is a mock backend, so nothing reaches a network. The command
tests drive the real CLI with the pair pinned in `.duet/config.json`, the way
`duet review`'s tests do; the session tests drive the real orchestrator.
"""

import json
import sys
from pathlib import Path

import pytest

from duet import jury
from duet.adapters.mock import MockAdapter, envelope
from duet.cli import main
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator

README = """\
# household-ledger (an example project)

Keeps a household budget in a plain text file.

## How to run it

```
{command}
```

Pricing is not decided yet.
"""

REAL_QUOTE = "Pricing is not decided yet."
INVENTED_QUOTE = "Requires a paid licence before first use."


def juror(decision="no", score=3, objections=None, **extra):
    body = dict(decision=decision, score=score, objections=objections or [])
    body.update(extra)
    return envelope("As this juror I have a view.", **body)


def objection(quote, severity="blocker", path="README.md", oid="no-pricing", walkthrough=False):
    evidence = {"quote": quote}
    if walkthrough:
        evidence["walkthrough"] = True
    else:
        evidence["path"] = path
    return {"id": oid, "title": "nobody can tell what it costs", "severity": severity,
            "detail": "a buyer needs a price before a trial", "evidence": evidence}


def project(tmp_path, command="%s -c \"print('ledger ready')\"" % sys.executable):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(README.format(command=command))
    (tmp_path / "ledger.py").write_text("def total(rows):\n    return sum(rows)\n")
    return tmp_path


def pin(tmp_path, script=None, peer_script=None, writes=None, error=None):
    """Both agents are mocks; jurors alternate between them."""
    def options(s, w=None, e=None):
        out = {}
        if s is not None:
            out["script"] = s
        if w is not None:
            out["writes"] = w
        if e is not None:
            out["error"] = e
        return out
    cfg = {
        "agents": [
            {"name": "claude", "backend": "mock", "model": "",
             "options": options(script, writes, error)},
            {"name": "chatgpt", "backend": "mock", "model": "",
             "options": options(peer_script if peer_script is not None else script, writes, error)},
        ],
        "start": "claude",
    }
    (tmp_path / ".duet").mkdir(exist_ok=True)
    (tmp_path / ".duet" / "config.json").write_text(json.dumps(cfg))
    return tmp_path


def sit(tmp_path, *extra):
    out = tmp_path / ".duet" / "out" / "JURY.md"
    code = main(["jury", "-C", str(tmp_path), "--out", str(out), *extra])
    return code, (out.read_text() if out.exists() else "")


# -- personas and the config format -----------------------------------------
def test_the_default_panel_is_the_five_stakeholders():
    names = [p.name for p in jury.DEFAULT_PERSONAS]
    assert names == ["user", "client", "investor", "judge", "skeptic"]
    for persona in jury.DEFAULT_PERSONAS:
        assert persona.role and persona.cares_about and persona.decision.endswith("?")


def test_a_custom_persona_joins_the_panel(tmp_path):
    (tmp_path / jury.JURY_CONFIG).write_text(json.dumps({"personas": [
        {"name": "ops-lead", "role": "runs a small warehouse team",
         "cares_about": "setup time", "decision": "Would you roll this out?"}]}))
    config = jury.load_config(str(tmp_path))
    assert config.names()[-1] == "ops-lead"
    assert [p.name for p in jury.choose_panel(config, "ops-lead,user")] == ["ops-lead", "user"]


@pytest.mark.parametrize("data, needle", [
    ({"personas": [], "jurors": []}, "unknown key jurors"),
    ({"personas": [{"name": "ops", "role": "r", "cares_about": "c", "decision": "d",
                    "mood": "grumpy"}]}, "unknown key mood"),
    ({"personas": [{"name": "ops", "role": "r", "decision": "d"}]}, "cares_about"),
    ({"personas": [{"name": "Ops Lead", "role": "r", "cares_about": "c", "decision": "d"}]},
     "lowercase"),
    ({"max_convenings": 0}, "max_convenings"),
    ([], "JSON object"),
])
def test_a_bad_config_is_an_error_not_ignored(data, needle):
    with pytest.raises(jury.JuryError) as caught:
        jury.parse_config(data)
    assert needle in str(caught.value)


def test_an_unknown_juror_is_named():
    with pytest.raises(jury.JuryError) as caught:
        jury.choose_panel(jury.JuryConfig(personas=list(jury.DEFAULT_PERSONAS)), "user,landlord")
    assert "landlord" in str(caught.value)


# -- evidence ---------------------------------------------------------------
def test_a_real_quote_counts_whitespace_normalised(tmp_path):
    project(tmp_path)
    found = jury.verify(jury.Objection("a", "t", "blocker", path="README.md",
                                       quote="  Pricing   is not\n decided yet. "), str(tmp_path))
    assert found.verified


def test_an_invented_quote_is_unverified(tmp_path):
    project(tmp_path)
    found = jury.verify(jury.Objection("a", "t", "blocker", path="README.md",
                                       quote=INVENTED_QUOTE), str(tmp_path))
    assert not found.verified and "not in README.md" in found.why_unverified


def test_a_quote_from_outside_the_workspace_is_unverified(tmp_path):
    inside = project(tmp_path / "proj")
    (tmp_path / "secret.txt").write_text("a quote that lives outside the project")
    found = jury.verify(jury.Objection("a", "t", "blocker", path="../secret.txt",
                                       quote="a quote that lives outside"), str(inside))
    assert not found.verified


def test_a_tiny_quote_proves_nothing(tmp_path):
    project(tmp_path)
    found = jury.verify(jury.Objection("a", "t", "blocker", path="README.md", quote="the"),
                        str(tmp_path))
    assert not found.verified


def test_a_walkthrough_quote_is_checked_against_the_recorded_output(tmp_path):
    record = "$ ledger\nTraceback: no such file budget.txt\n[exit 1]"
    real = jury.verify(jury.Objection("a", "t", "blocker", walkthrough=True,
                                      quote="no such file budget.txt"), str(tmp_path), record)
    fake = jury.verify(jury.Objection("a", "t", "blocker", walkthrough=True,
                                      quote="segmentation fault at start"), str(tmp_path), record)
    assert real.verified and not fake.verified


def test_a_juror_envelope_is_read_and_checked(tmp_path):
    project(tmp_path)
    text = juror("maybe", 6, [objection(REAL_QUOTE), objection(INVENTED_QUOTE, oid="licence")],
                 price="a small monthly fee")
    verdict = jury.parse_verdict(text, jury.DEFAULT_PERSONAS[1], str(tmp_path))
    assert verdict.answered and verdict.decision == "maybe" and verdict.score == 6
    assert verdict.price == "a small monthly fee"
    assert [o.id for o in verdict.verified()] == ["no-pricing"]


def test_a_score_outside_one_to_ten_is_not_taken():
    verdict = jury.parse_verdict(juror("yes", 14), jury.DEFAULT_PERSONAS[0], ".")
    assert verdict.score is None and any("score" in n for n in verdict.notes)


# -- the command and its exit codes -----------------------------------------
def test_a_verified_blocker_exits_1_and_is_reported(tmp_path, capsys):
    pin(project(tmp_path), script=[juror("no", 3, [objection(REAL_QUOTE)])])
    code, report = sit(tmp_path, "--panel", "client")
    assert code == 1
    assert "nobody can tell what it costs" in report and "verified" in report
    assert jury.DISCLAIMER in report
    assert "not market evidence" in report


def test_an_invented_blocker_does_not_count(tmp_path):
    pin(project(tmp_path), script=[juror("no", 3, [objection(INVENTED_QUOTE)])])
    code, report = sit(tmp_path, "--panel", "client")
    assert code == 0
    assert "UNVERIFIED" in report and "not counted" in report
    assert "**Verified blockers:** 0" in report


def test_the_report_has_the_panel_summary(tmp_path):
    judge = juror("yes", 7, scores={"originality": 6, "execution": 8, "impact": 7, "clarity": 9},
                  win=True)
    pin(project(tmp_path), script=[judge])
    code, report = sit(tmp_path, "--panel", "judge")
    assert code == 0
    for heading in ("Would use it (user)", "Would pay (client)", "Would invest (investor)",
                    "Judges' score:** 7.5/10 — win", "three objections most worth fixing",
                    "## User walkthrough"):
        assert heading in report


def test_a_bad_config_exits_3(tmp_path):
    pin(project(tmp_path), script=[juror()])
    (tmp_path / jury.JURY_CONFIG).write_text(json.dumps({"persona": []}))
    assert sit(tmp_path)[0] == 3


def test_an_unknown_panel_member_exits_3(tmp_path):
    pin(project(tmp_path), script=[juror()])
    assert sit(tmp_path, "--panel", "landlord")[0] == 3


def test_no_juror_answering_exits_3(tmp_path):
    pin(project(tmp_path), error="not signed in")
    code, report = sit(tmp_path, "--panel", "client,judge")
    assert code == 3
    assert "Did not answer" in report


def test_jurors_alternate_between_the_two_agents(tmp_path):
    pin(project(tmp_path), script=[juror("yes", 8)])
    _, report = sit(tmp_path, "--panel", "client,investor,judge,skeptic")
    sat = [line for line in report.splitlines() if line.startswith("Sat as ")]
    assert [line.split()[2].rstrip(",.") for line in sat] == ["claude", "chatgpt", "claude", "chatgpt"]


def test_rounds_of_questions_let_a_juror_change_its_mind(tmp_path):
    first = juror("no", 3, [objection(REAL_QUOTE)])
    second = juror("yes", 7, [])
    pin(project(tmp_path), script=[first, second])
    code, report = sit(tmp_path, "--panel", "client", "--rounds-of-questions", "1")
    assert code == 0
    assert "**Decision:** yes" in report


# -- jurors are read-only ---------------------------------------------------
def test_every_juror_is_made_read_only(tmp_path):
    calls = []

    class Recording(MockAdapter):
        def read_only(self):
            calls.append(self.name)
            return "held read-only"

    project(tmp_path)
    result = jury.convene(
        root=str(tmp_path), agents=["claude", "chatgpt"], panel=jury.DEFAULT_PERSONAS[1:3],
        stage="build", make_adapter=lambda name: Recording(
            name=name, cwd=str(tmp_path), config={"script": [juror("yes", 8)]}),
    )
    assert calls == ["claude", "chatgpt"]
    assert all(v.read_only == "held read-only" for v in result.verdicts)


def test_a_juror_that_writes_anyway_is_not_counted(tmp_path, capsys):
    pin(project(tmp_path), script=[juror("no", 2, [objection(REAL_QUOTE)])],
        writes={"ledger.py": "hacked\n"})
    code, report = sit(tmp_path, "--panel", "client")
    assert code == 3
    assert "workspace changed during this juror's pass" in report


# -- stage detection --------------------------------------------------------
def test_stage_is_idea_for_an_empty_workspace_with_an_idea(tmp_path):
    assert jury.detect_stage(str(tmp_path), "a ledger for households") == "idea"


def test_stage_is_plan_when_there_is_a_plan_and_little_code(tmp_path):
    (tmp_path / "PLAN.md").write_text("# Plan\n")
    (tmp_path / "main.py").write_text("")
    assert jury.detect_stage(str(tmp_path)) == "plan"


def test_stage_is_build_once_there_is_code(tmp_path):
    (tmp_path / "PLAN.md").write_text("# Plan\n")
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("")
    assert jury.detect_stage(str(tmp_path)) == "build"
    assert jury.detect_stage(str(project(tmp_path / "p"))) == "build"


def test_the_idea_stage_needs_the_idea(tmp_path):
    pin(tmp_path, script=[juror()])
    assert sit(tmp_path, "--stage", "idea")[0] == 3


def test_launch_shows_the_jurors_pricing_and_landing_page(tmp_path):
    project(tmp_path)
    (tmp_path / "pricing.md").write_text("Household plan: four coins a month.\n")
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<h1>Budget calmly</h1>")
    _, bodies = jury._material(str(tmp_path), "launch")
    assert "pricing.md" in bodies and "site/index.html" in bodies
    _, plan_bodies = jury._material(str(tmp_path), "plan")
    assert "pricing.md" not in plan_bodies


# -- the user's walkthrough -------------------------------------------------
def test_the_walkthrough_runs_in_a_throwaway_copy(tmp_path):
    project(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    command = "%s -c \"open('litter.txt','w').write('x'); print('ran in copy')\"" % sys.executable
    walk = jury.walkthrough(str(tmp_path), command, timeout=30)
    assert walk.ran and walk.exit_code == 0 and "ran in copy" in walk.output
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "litter.txt").exists()


def test_the_walkthrough_is_stopped_at_its_time_limit(tmp_path):
    project(tmp_path)
    walk = jury.walkthrough(str(tmp_path),
                            "%s -c \"import time; print('up', flush=True); time.sleep(30)\""
                            % sys.executable, timeout=1)
    assert walk.timed_out and "time limit" in walk.transcript()


def test_the_user_juror_is_shown_the_walkthrough_and_can_quote_it(tmp_path):
    command = "%s -c \"import sys; sys.exit('budget.txt not found')\"" % sys.executable
    pin(project(tmp_path, command=command),
        script=[juror("no", 2, [objection("budget.txt not found", walkthrough=True,
                                          oid="crashes-on-start")])])
    code, report = sit(tmp_path, "--panel", "user")
    assert code == 1
    assert "budget.txt not found" in report.split("## User walkthrough")[1]
    prompt_seen = json.loads((tmp_path / ".duet" / "config.json").read_text())
    assert prompt_seen   # the config is untouched by the walkthrough


def test_the_user_juror_is_shown_the_website_walkthrough(monkeypatch, tmp_path):
    project(tmp_path, command="")
    (tmp_path / "index.html").write_text("<h1>Household ledger</h1>")
    shots_dir = tmp_path / ".duet" / "shots"
    seen = {}

    from duet import gate as gate_detect
    from duet import page as page_check

    monkeypatch.setattr(gate_detect, "page_gate",
                        lambda root: "python -m duet page check index.html")
    monkeypatch.setattr(page_check, "check_page",
                        lambda target, widths, root="": ["button text overlaps at 375px"])
    monkeypatch.setattr(page_check, "shoot_page",
                        lambda target, widths, out_dir, root="": [str(Path(out_dir) / "index-375.png")])

    class Recording(MockAdapter):
        def send(self, prompt, system="", round_no=0):
            seen["prompt"] = prompt
            return super().send(prompt, system=system, round_no=round_no)

    result = jury.convene(
        root=str(tmp_path), agents=["claude"], panel=[jury.DEFAULT_PERSONAS[0]],
        stage="build", shots_dir=str(shots_dir),
        make_adapter=lambda name: Recording(
            name=name, cwd=str(tmp_path),
            config={"script": [juror("no", 2, [
                objection("button text overlaps at 375px", walkthrough=True,
                          oid="page-overlaps")
            ])]},
        ),
    )
    report = jury.render(result)
    assert result.exit_code() == 1
    assert "button text overlaps at 375px" in seen["prompt"]
    assert "Look at the screenshots above" in seen["prompt"]
    assert "index-375.png" in seen["prompt"]
    assert "button text overlaps at 375px" in report
    assert "index-375.png" in report


def test_no_run_command_is_itself_reported(tmp_path):
    (tmp_path / "ledger.py").write_text("x = 1\n")
    walk = jury.walkthrough(str(tmp_path), jury.run_command_for(str(tmp_path)))
    assert not walk.ran and "no run command found" in walk.transcript()


# -- in a session -----------------------------------------------------------
DONE = envelope("I would ship this", "DONE", confidence=0.9)
BLOCKER = juror("no", 2, [objection(REAL_QUOTE)])


def session(tmp_path, claude_script, gpt_script, juror_script, jury_on=True, jury_max=2):
    project(tmp_path)
    cfg = Config(task="build a household ledger", root=str(tmp_path),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=12, jury=jury_on, jury_panel="client",
                 jury_max=jury_max)
    adapters = {
        "claude": MockAdapter(name="claude", cwd=str(tmp_path), config={"script": claude_script}),
        "gpt": MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": gpt_script}),
    }
    orch = Orchestrator(cfg, adapters=adapters)
    sat = []

    def make(name):
        sat.append(name)
        return MockAdapter(name=name, cwd=str(tmp_path), config={"script": juror_script})

    orch.jury_adapter = make
    return orch, orch.run(), sat


def test_a_verified_blocker_reopens_the_session_as_an_issue(tmp_path):
    fixed = envelope("fixed it", "DONE", resolves=["jury-client-no-pricing"])
    orch, result, sat = session(tmp_path, [DONE, fixed], [DONE], [BLOCKER], jury_max=1)
    issue = orch.state.issues["jury-client-no-pricing"]
    assert issue.raised_by == "jury:client" and issue.severity == "blocker"
    # claimed by claude, verified on the jury's behalf by gpt
    assert issue.status == "resolved" and "gpt" in issue.resolution
    assert result.status == "consensus"
    assert len(sat) == 1
    assert (Path(result.session_dir) / "JURY.md").is_file()
    assert "## Jury" in Path(result.report).read_text()


def test_the_jury_sits_at_most_twice(tmp_path):
    fixed = envelope("fixed it", "DONE", resolves=["jury-client-no-pricing"])
    orch, result, sat = session(tmp_path, [DONE, fixed], [DONE], [BLOCKER])
    assert len(sat) == 2
    assert orch.config.jury_convened == 2
    assert result.status == "consensus"
    report = Path(result.report).read_text()
    assert "cap of 2 reached" in report


def test_a_clear_jury_lets_the_session_end(tmp_path):
    orch, result, sat = session(tmp_path, [DONE], [DONE], [juror("yes", 8)])
    assert result.status == "consensus" and len(sat) == 1
    assert not [i for i in orch.state.issues if i.startswith("jury-")]


def test_without_jury_nothing_changes(tmp_path):
    orch, result, sat = session(tmp_path, [DONE], [DONE], [BLOCKER], jury_on=False)
    assert result.status == "consensus" and sat == []
    assert not (Path(result.session_dir) / "JURY.md").exists()
    assert "## Jury" not in Path(result.report).read_text()
    assert result.rounds == 2


def test_panel_without_jury_is_refused(tmp_path):
    project(tmp_path)
    assert main(["run", "-C", str(tmp_path), "--no-gate", "--panel", "user", "x"]) == 3


def test_a_bad_jury_config_stops_the_session_before_it_starts(tmp_path):
    project(tmp_path)
    (tmp_path / jury.JURY_CONFIG).write_text(json.dumps({"personas": [{"name": "x"}]}))
    assert main(["run", "-C", str(tmp_path), "--no-gate", "--jury", "--pair", "mock+mock",
                 "x"]) == 3
    assert not (tmp_path / ".duet" / "sessions").exists()
