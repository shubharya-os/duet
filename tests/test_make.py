"""`duet make`: recon, plan, build, parity and a clean-room sweep.

Every agent here is a mock and nothing reaches a network. The reference app is
invented: "Bookly", at bookly.example.
"""

import json
import sys
from pathlib import Path

import pytest

from duet import make
from duet.adapters.claude_code import ClaudeCodeAdapter
from duet.adapters.codex_cli import CodexCliAdapter
from duet.adapters.mock import MockAdapter, envelope
from duet.cli import main
from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator, SessionResult, STATUS_CONSENSUS

HEADER = ",".join(make.COLUMNS)
SOURCE = "https://bookly.example/help/shelves"


def csv_of(*rows):
    return "\n".join([HEADER] + [",".join(r) for r in rows]) + "\n"


def write_ledger(root, text):
    folder = Path(root) / "make"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "features.csv").write_text(text)


# -- features.csv -----------------------------------------------------------
def test_a_well_formed_row_counts():
    rows, problems = make.parse_features(csv_of(
        ("shared shelf", "library", "must", SOURCE, "todo", "", "")))
    assert problems == []
    assert len(rows) == 1 and rows[0].counts and rows[0].priority == "must"


@pytest.mark.parametrize("source", [
    "", "bookly.example/help", "ftp://bookly.example/x", "javascript:alert(1)",
    "https://localhost/help", "http://192.168.1.4/help", "http://10.0.0.2/x",
    "https://intranet/help", "https://user:secret@bookly.example/help",
    "https://app.internal/help", "not a url at all",
])
def test_a_row_without_a_public_http_source_does_not_count(source):
    rows, _ = make.parse_features(csv_of(
        ("shared shelf", "library", "must", source, "todo", "", "")))
    assert len(rows) == 1 and not rows[0].counts
    assert make.must_haves(rows) == []


def test_bad_priority_or_status_is_reported():
    rows, _ = make.parse_features(csv_of(
        ("a", "x", "nice", SOURCE, "todo", "", ""),
        ("b", "x", "must", SOURCE, "finished", "", "")))
    assert "priority" in rows[0].problems[0]
    assert "status" in rows[1].problems[0]


def test_missing_columns_are_a_problem_with_the_file():
    rows, problems = make.parse_features("feature,priority\nshelf,must\n")
    assert rows == [] and "missing the column" in problems[0]


def test_recon_check_reports_dropped_rows_and_fails_when_none_count(tmp_path, capsys):
    write_ledger(tmp_path, csv_of(("shelf", "x", "must", "", "todo", "", "")))
    assert make.check_features(str(tmp_path)) == make.MAKE_NOT_DONE
    out = capsys.readouterr().out
    assert "dropped" in out and "no source" in out


# -- parity ------------------------------------------------------------------
def product(root):
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_shelf.py").write_text(
        "def test_shared_shelf():\n    assert True\n")
    (root / "shelf.py").write_text("def add(): pass\n")


def test_parity_arithmetic(tmp_path):
    product(tmp_path)
    rows, _ = make.parse_features(csv_of(
        ("shared shelf", "a", "must", SOURCE, "done", "test_shared_shelf", ""),
        ("reading goals", "a", "should", SOURCE, "partial", "", ""),
        ("export", "a", "could", SOURCE, "done", "shelf.py", ""),
        ("dark mode", "a", "could", SOURCE, "todo", "", ""),
        ("gift cards", "a", "should", SOURCE, "skip", "", "not for a club"),
        ("offline club", "a", "must", SOURCE, "todo", "", "extra: none of them do it"),
    ))
    result = make.score(rows, str(tmp_path))
    # must 3*1 + should 2*0.5 + could 1*1 + could 1*0 = 5 of 7
    assert result.earned == 5 and result.possible == 7
    assert result.percent == round(500 / 7, 1)
    assert [r.feature for r in result.skipped] == ["gift cards"]
    assert [r.feature for r in result.extra] == ["offline club"]
    assert result.shippable


def test_a_skip_without_a_reason_is_a_todo(tmp_path):
    rows, _ = make.parse_features(csv_of(("gift cards", "a", "must", SOURCE, "skip", "", "")))
    result = make.score(rows, str(tmp_path))
    assert result.skipped == [] and result.possible == 3 and result.earned == 0
    assert not result.shippable


@pytest.mark.parametrize("evidence", [
    "test_does_not_exist", "nowhere.py", "tests/test_shelf.py::test_missing",
    "make/features.csv", "../outside.py", "",
])
def test_done_without_checkable_evidence_counts_as_todo(tmp_path, evidence):
    product(tmp_path)
    (tmp_path.parent / "outside.py").write_text("x = 1\n")
    write_ledger(tmp_path, HEADER + "\n")
    rows, _ = make.parse_features(csv_of(("shared shelf", "a", "must", SOURCE, "done", evidence, "")))
    result = make.score(rows, str(tmp_path))
    assert result.earned == 0 and [r.feature for r in result.demoted] == ["shared shelf"]
    assert not result.shippable


@pytest.mark.parametrize("evidence", [
    "test_shared_shelf", "shelf.py", "tests/test_shelf.py",
    "tests/test_shelf.py::test_shared_shelf",
])
def test_done_with_real_evidence_counts(tmp_path, evidence):
    product(tmp_path)
    rows, _ = make.parse_features(csv_of(("shared shelf", "a", "must", SOURCE, "done", evidence, "")))
    result = make.score(rows, str(tmp_path))
    assert result.earned == 3 and result.shippable


def test_parity_stage_writes_the_report_and_exits_1_with_a_must_left(tmp_path):
    product(tmp_path)
    write_ledger(tmp_path, csv_of(
        ("shared shelf", "a", "must", SOURCE, "done", "test_shared_shelf", ""),
        ("reading goals", "a", "must", SOURCE, "done", "test_reading_goals", "")))
    assert main(["make", "--stage", "parity", "-C", str(tmp_path)]) == 1
    report = (tmp_path / "make" / "parity.md").read_text()
    assert "NOT shippable" in report and "reading goals" in report
    assert "counted as todo" in report.lower()


def test_parity_stage_prints_every_row_that_does_not_count(tmp_path, capsys):
    product(tmp_path)
    write_ledger(tmp_path, csv_of(
        ("shared shelf", "a", "must", SOURCE, "done", "test_shared_shelf", ""),
        ("gift cards", "a", "should", SOURCE, "skip", "", "not for a club"),
        ("reminder emails", "a", "should", "", "todo", "", ""),
        ("offline club", "a", "could", SOURCE, "todo", "", "extra: none of them do it")))
    assert main(["make", "--stage", "parity", "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "line 4: reminder emails — no source" in out
    assert "line 3: gift cards" in out and "not for a club" in out
    assert "line 5: offline club" in out and "none of them do it" in out


def test_parity_stage_exits_0_when_every_must_is_done(tmp_path):
    product(tmp_path)
    write_ledger(tmp_path, csv_of(
        ("shared shelf", "a", "must", SOURCE, "done", "test_shared_shelf", "")))
    assert main(["make", "--stage", "parity", "-C", str(tmp_path)]) == 0
    assert "shippable" in (tmp_path / "make" / "parity.md").read_text()


# -- the sweep ---------------------------------------------------------------
BRAND = make.brand_from(["https://www.bookly.example/app"],
                        {"avoid": ["Page Turner"], "colors": ["#FF3366", "#a1b2c3"]})


def test_brand_comes_from_like_and_brand_json():
    assert "bookly" in BRAND.names and "Page Turner" in BRAND.names
    assert BRAND.domains == ["bookly.example"]
    assert BRAND.colors == ["#ff3366", "#a1b2c3"]
    assert make.brand_from(["Bookly"]).names == ["Bookly"]


@pytest.mark.parametrize("line", [
    "const booklyShelf = 1;", "BOOKLY_API = 2", "def get_bookly_id(): pass",
    "class MyBooklyThing: pass", "see https://bookly.example/x",
    "color: #f36;", "color: #FF3366;", "fill = 0xff3366", "label = 'page_turner'",
    "PageTurner()",
])
def test_the_sweep_finds_references_in_contents_and_identifiers(tmp_path, line):
    (tmp_path / "app.py").write_text("x = 1\n" + line + "\n")
    found = make.sweep(str(tmp_path), BRAND)
    assert [(f.path, f.line) for f in found][:1] == [("app.py", 2)]


def test_a_six_digit_colour_with_no_short_form_is_only_found_long(tmp_path):
    (tmp_path / "a.css").write_text("a { color: #abc; }\nb { color: #A1B2C3; }\n")
    found = make.sweep(str(tmp_path), BRAND)
    assert [f.line for f in found] == [2]


def test_a_longer_hex_is_not_a_colour_match(tmp_path):
    (tmp_path / "a.txt").write_text("hash #ff3366aa99\nshort #f36a\n")
    assert make.sweep(str(tmp_path), BRAND) == []


def test_the_sweep_finds_file_names(tmp_path):
    (tmp_path / "src" / "BooklyClient").mkdir(parents=True)
    (tmp_path / "src" / "BooklyClient" / "index.js").write_text("export {}\n")
    (tmp_path / "src" / "bookly_utils.py").write_text("x = 1\n")
    paths = {f.path for f in make.sweep(str(tmp_path), BRAND) if f.line == 0}
    # each name is reported where it is, not again for everything beneath it
    assert paths == {"src/BooklyClient", "src/bookly_utils.py"}


def test_the_sweep_skips_what_is_not_the_product(tmp_path):
    for rel in (".git/config", "node_modules/bookly/index.js", "dist/app.js",
                "build/app.js", "package-lock.json", "yarn.lock", "make/recon.md",
                ".duet/state.json"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bookly #ff3366\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\0bookly")
    (tmp_path / "blob.dat").write_bytes(b"\0\0bookly")
    assert make.sweep(str(tmp_path), BRAND) == []


def test_sweep_stage_exits_1_while_dirty_and_0_when_clean(tmp_path):
    (tmp_path / "app.py").write_text("booklyShelf = 1\n")
    assert main(["make", "--stage", "sweep", "-C", str(tmp_path),
                 "--like", "https://bookly.example"]) == 1
    # --like was remembered in make/brand.json; a later sweep needs no --like
    assert main(["make", "--stage", "sweep", "-C", str(tmp_path)]) == 1
    (tmp_path / "app.py").write_text("shelf = 1\n")
    assert main(["make", "--stage", "sweep", "-C", str(tmp_path)]) == 0


GENERIC_PALETTE = ["#1e1e1e", "#7a5cf0", "#f5f5f5", "#ffffff"]


def test_the_sweep_ignores_neutral_colours_and_says_why(tmp_path, capsys):
    # A recon pass that listed white, light grey and near-black with the one hue
    # that identifies the reference: the sweep could never come back clean.
    folder = tmp_path / "make"
    folder.mkdir()
    (folder / "brand.json").write_text(json.dumps(
        {"avoid": [], "domains": [], "colors": GENERIC_PALETTE}))
    (tmp_path / "style.css").write_text(
        "body { background: #fff; color: #1E1E1E; }\n.card { background: #f5f5f5; }\n")
    assert main(["make", "--stage", "sweep", "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "ignored" in out and "neutral" in out
    for color in ("#1e1e1e", "#f5f5f5", "#ffffff"):
        assert color in out
    # the distinctive hue is still swept
    (tmp_path / "accent.css").write_text("a { color: #7A5CF0; }\n")
    assert main(["make", "--stage", "sweep", "-C", str(tmp_path)]) == 1
    assert "accent.css:1" in capsys.readouterr().out


@pytest.mark.parametrize("color, neutral", [
    ("#ffffff", True), ("#000", True), ("#1e1e1e", True), ("#f5f5f5", True),
    ("#f8f9fa", True), ("#7a5cf0", False), ("#ff3366", False), ("#e0e0ff", False),
])
def test_neutral_is_channels_within_a_small_tolerance(color, neutral):
    assert make.is_neutral(color) is neutral


def test_the_build_veto_ignores_neutral_colours(tmp_path):
    (tmp_path / "style.css").write_text("body { background: #ffffff; color: #1e1e1e; }\n")
    state = {"clean_room": {"avoid": [], "domains": [], "colors": GENERIC_PALETTE}}
    assert make.sweep_veto(str(tmp_path), state) is None
    (tmp_path / "accent.css").write_text("a { color: #7a5cf0; }\n")
    assert "accent.css" in make.sweep_veto(str(tmp_path), state)


def test_brand_json_colours_are_swept(tmp_path):
    folder = tmp_path / "make"
    folder.mkdir()
    (folder / "brand.json").write_text(json.dumps({"avoid": [], "domains": [], "colors": ["#00ff99"]}))
    (tmp_path / "style.css").write_text("a { color: #0F9; }\n")
    assert main(["make", "--stage", "sweep", "-C", str(tmp_path)]) == 1


# -- the stages, in order, and the web granted to recon only -----------------
RECON_REPLY = """Here is what the public pages show.

--- BEGIN make/recon.md ---
# Recon
Screens: shelf, club page. Flow: create a club, add a book, vote.
--- END make/recon.md ---

--- BEGIN make/features.csv ---
%s
shared shelf,library,must,https://bookly.example/help/shelves,todo,,
reading goals,profile,should,https://bookly.example/pricing,todo,,
member chat,club,could,,todo,,
--- END make/features.csv ---

--- BEGIN make/brand.json ---
{"avoid": ["Bookly"], "domains": ["bookly.example"], "colors": ["#ff3366"]}
--- END make/brand.json ---
""" % HEADER


def pin_mocks(root, reply, **fields):
    """Both agents are mocks; the first one is the research pass."""
    (root / ".duet").mkdir(parents=True, exist_ok=True)
    spec = {"script": [reply + "\n\n" + envelope("research done", "DONE", **fields)]}
    (root / ".duet" / "config.json").write_text(json.dumps({
        "agents": [{"name": "claude", "backend": "mock", "model": "", "options": spec},
                   {"name": "chatgpt", "backend": "mock", "model": "", "options": spec}],
        "start": "claude",
    }))


@pytest.fixture
def sessions(monkeypatch):
    """Stand in for the plan and build sessions: record them, do their work."""
    seen = []

    def fake_run(self):
        cfg = self.config
        seen.append({"workflow": cfg.workflow, "gate": cfg.gate,
                     "state": dict(cfg.workflow_state),
                     "web": [getattr(a, "web", False) for a in self.adapters.values()]})
        root = Path(cfg.root)
        if cfg.workflow == "plan":
            (root / "PLAN.md").write_text("1. shared shelf\n2. reading goals\n")
        else:
            product(root)
            write_ledger(root, csv_of(
                ("shared shelf", "library", "must", SOURCE, "done", "test_shared_shelf", ""),
                ("reading goals", "profile", "should", SOURCE, "partial", "", "")))
        return SessionResult(status=STATUS_CONSENSUS, rounds=2, digest="")

    monkeypatch.setattr(Orchestrator, "run", fake_run)
    return seen


def test_the_whole_pipeline_runs_in_order_and_only_recon_gets_the_web(tmp_path, monkeypatch, sessions):
    pin_mocks(tmp_path, RECON_REPLY)
    granted = []
    original = MockAdapter.allow_web

    def spy(self):
        granted.append(len(sessions))       # how many sessions had run by then
        return original(self)

    monkeypatch.setattr(MockAdapter, "allow_web", spy)
    gate = "%s -c \"print('ok')\"" % sys.executable
    code = main(["make", "a shared reading list for book clubs", "-C", str(tmp_path),
                 "--like", "https://bookly.example", "--gate", gate])
    assert code == 0
    # recon wrote the ledger; the row with no source was dropped, not counted
    assert (tmp_path / "make" / "recon.md").read_text().startswith("# Recon")
    assert "#ff3366" in json.loads((tmp_path / "make" / "brand.json").read_text())["colors"]
    # recon, then plan, then build, then parity
    assert [s["workflow"] for s in sessions] == ["plan", ""]
    assert sessions[0]["state"]["must_cover"] == ["shared shelf"]
    assert sessions[1]["gate"] == gate and sessions[1]["state"]["clean_room"]["avoid"]
    assert (tmp_path / "make" / "parity.md").is_file()
    # the web went to the research pass, before any session, and to nothing else
    assert granted == [0]
    assert all(not any(s["web"]) for s in sessions)


RECON_NOTES_ONLY = """--- BEGIN make/recon.md ---
# Recon
Screens: canvas, toolbar. Flow: draw, group, export.
--- END make/recon.md ---
"""

CANVAS_FEATURES = [
    {"feature": "Grouping and layering (z-order, bring to front, send back)",
     "area": "tools", "priority": "must", "source": "https://sketchpad.example/docs/layers",
     "status": "todo", "notes": ""},
    {"feature": "Export to PNG, SVG, or a shareable link", "area": "sharing",
     "priority": "should", "source": "https://sketchpad.example/docs/export",
     "status": "todo", "notes": "quotes \"as-is\", commas, too"},
    {"feature": "freehand pen", "area": "tools", "priority": "must",
     "source": "https://sketchpad.example/docs/pen"},
]


def test_recon_writes_features_csv_from_the_envelope_so_commas_cannot_drop_a_row(tmp_path, capsys):
    pin_mocks(tmp_path, RECON_NOTES_ONLY, features=CANVAS_FEATURES)
    assert main(["make", "an infinite canvas", "--stage", "recon", "-C", str(tmp_path)]) == 0
    rows, problems = make.read_features(str(tmp_path))
    assert problems == []
    assert [r.feature for r in rows] == [f["feature"] for f in CANVAS_FEATURES]
    assert all(r.counts for r in rows), [r.problems for r in rows]
    assert rows[0].area == "tools" and rows[0].priority == "must"
    assert rows[1].notes == "quotes \"as-is\", commas, too"
    assert rows[2].status == "todo"
    assert "do not count" in capsys.readouterr().out
    assert [r.feature for r in make.must_haves(rows)] == [
        CANVAS_FEATURES[0]["feature"], "freehand pen"]


def test_the_recon_prompt_asks_for_data_and_distinctive_hues():
    prompt = " ".join(make.RECON_PROMPT.split())
    assert '"features"' in prompt and "distinctive" in prompt
    assert "never white, black or a grey" in prompt.lower()


def test_a_dirty_product_stops_the_pipeline_at_the_sweep(tmp_path, sessions):
    pin_mocks(tmp_path, RECON_REPLY)
    (tmp_path / "brand_assets.txt").write_text("made like Bookly\n")
    code = main(["make", "a shared reading list", "-C", str(tmp_path),
                 "--like", "https://bookly.example",
                 "--gate", "%s -c \"print('ok')\"" % sys.executable])
    assert code == 1


def test_stage_runs_one_stage_only(tmp_path, sessions):
    product(tmp_path)
    write_ledger(tmp_path, csv_of(
        ("shared shelf", "a", "must", SOURCE, "done", "test_shared_shelf", "")))
    assert main(["make", "--stage", "parity", "-C", str(tmp_path)]) == 0
    assert sessions == []
    assert not (tmp_path / "make" / "recon.md").exists()


def test_a_stage_without_its_input_refuses(tmp_path, sessions):
    assert main(["make", "a reading list", "--stage", "plan", "-C", str(tmp_path)]) == 3
    assert sessions == []


def test_plan_stage_hands_the_must_haves_to_the_plan_veto(tmp_path, sessions):
    write_ledger(tmp_path, csv_of(("shared shelf", "a", "must", SOURCE, "todo", "", "")))
    (tmp_path / "make" / "recon.md").write_text("# Recon\n")
    assert main(["make", "a reading list", "--stage", "plan", "-C", str(tmp_path)]) == 0
    assert [s["workflow"] for s in sessions] == ["plan"]
    assert sessions[0]["state"]["must_cover"] == ["shared shelf"]


def test_no_idea_is_a_usage_error(tmp_path):
    assert main(["make", "-C", str(tmp_path)]) == 2


# -- web permissions on the real adapters ------------------------------------
def test_claude_gets_web_tools_only_when_granted(tmp_path):
    adapter = ClaudeCodeAdapter(name="c", cwd=str(tmp_path))
    assert "WebSearch" not in " ".join(adapter._command("hi", ""))
    adapter.allow_web()
    command = adapter._command("hi", "")
    allowed = command[command.index("--allowedTools") + 1]
    assert "WebSearch" in allowed and "WebFetch" in allowed


def test_codex_gets_web_search_only_when_granted(tmp_path):
    adapter = CodexCliAdapter(name="x", cwd=str(tmp_path))
    assert "tools.web_search=true" not in adapter._base("/tmp/out")
    adapter.allow_web()
    assert "tools.web_search=true" in adapter._base("/tmp/out")


def test_the_fine_print_is_in_help(capsys):
    with pytest.raises(SystemExit):
        main(["make", "--help"])
    out = capsys.readouterr().out
    for phrase in ("never what it owns", "clean-room", "paywall", "Rebrand before launch",
                   "not legal advice", "duet jury"):
        assert phrase in out.replace("\n", " ")
