"""`duet page` against a real browser: one fixture pair per built-in rule.

Every case here writes two pages that differ only in the fault under test — the
broken one has to produce that rule's line, and the fixed one has to be clean.
Both halves matter: a rule that fires on everything is as useless as one that
never fires, and only rendering can tell the difference. That is the whole
argument for this feature, so it is the whole argument for these tests.

They skip, with a reason, when there is no Chrome to drive, so CI without a
browser still passes rather than reporting a fault in the page.
"""

import base64
import json
import struct
import sys
from pathlib import Path

import pytest

from duet import chrome, page
from duet.chrome import SetupError
from duet.cli import main

# A page that is clean under every built-in rule, so the one fault each case
# introduces is the only thing there is to find. Invented names throughout.
TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Landing</title>
<style>
  body {{ margin: 0; padding: 16px; background: #fff; color: #111;
          font: 16px/1.5 system-ui, sans-serif; }}
  a {{ color: #0b4ea2; }}
{styles}
</style>
</head>
<body>
<h1>Widgets</h1>
<p class="lede">Widgets for everyone, delivered on a Tuesday.</p>
{body}
</body></html>
"""

# A page with no viewport meta at all, for the one rule that is about its absence.
NO_VIEWPORT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Landing</title></head>
<body style="margin:0;background:#fff;color:#111"><h1>Widgets</h1></body></html>
"""

# The smallest real PNG, so "this image loads" is not itself a guess.
ONE_PIXEL_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF"
    b"zBRqmAAAAAElFTkSuQmCC")


@pytest.fixture(scope="module", params=[chrome.PIPE, chrome.WEBSOCKET])
def browser(request):
    """One Chrome per transport for the whole module: starting it costs more
    than every check. Every test runs over both, and the transport is set in
    the environment too, so the ones that go through main() use it as well."""
    with pytest.MonkeyPatch.context() as env:
        env.setenv(chrome.TRANSPORT_ENV, request.param)
        problem = chrome.unsupported_platform()
        if problem:
            pytest.skip(problem)
        try:
            found = chrome.find_chrome()
        except SetupError as exc:
            pytest.skip("no browser to drive: %s" % exc)
        started = chrome.Browser(chrome=found)
        assert started.transport == request.param
        try:
            yield started
        finally:
            started.close()


def write_page(tmp_path, name, body="", styles="", raw=""):
    path = tmp_path / name
    path.write_text(raw or TEMPLATE.format(body=body, styles=styles), encoding="utf-8")
    return path


def check(browser, path, widths=(375,), rules=None):
    return page.check_page(str(path), list(widths), rules, browser=browser)


def lines_for(faults, rule):
    return [line for line in faults if ": %s: " % rule in line]


def only(faults, rule, width=375):
    """Assert this rule fired and nothing else did, and hand back its line."""
    assert faults, "expected a %s fault, got a clean page" % rule
    mine = lines_for(faults, rule)
    assert mine, "expected a %s fault, got: %s" % (rule, faults)
    assert len(faults) == len(mine), "other faults came with it: %s" % faults
    assert mine[0].startswith("%d: %s: " % (width, rule))
    return mine[0]


# -- the page that is meant to be clean --------------------------------------

def test_the_template_itself_is_clean_at_both_widths(browser, tmp_path):
    """The baseline every case below measures against. If this ever goes red,
    every "the fixed page passes" assertion in this file is worthless."""
    path = write_page(tmp_path, "landing.html")
    assert check(browser, path, widths=(1440, 375)) == []


# -- 1. horizontal overflow --------------------------------------------------

def test_a_page_wider_than_the_phone_scrolls_sideways(browser, tmp_path):
    broken = write_page(tmp_path, "wide.html",
                        body='<div class="panel">Specifications</div>',
                        styles=".panel { width: 900px; }")
    line = only(check(browser, broken), "overflow")
    assert "375px viewport" in line
    assert "panel" in line                       # and which element did it

    fixed = write_page(tmp_path, "narrow.html",
                       body='<div class="panel">Specifications</div>',
                       styles=".panel { max-width: 100%; }")
    assert check(browser, fixed) == []


def test_the_same_page_can_be_clean_wide_and_broken_narrow(browser, tmp_path):
    """Why two widths rather than one: this is the shape of nearly every real
    phone bug — the desktop layout is fine and nobody looked at the other one."""
    path = write_page(tmp_path, "responsive.html",
                      body='<div class="panel">Specifications</div>',
                      styles=".panel { width: 700px; }")
    faults = check(browser, path, widths=(1440, 375))
    assert [f.split(":")[0] for f in faults] == ["375"]


# -- 2. script errors -------------------------------------------------------

def test_an_uncaught_exception_while_loading_is_a_fault(browser, tmp_path):
    broken = write_page(tmp_path, "throws.html",
                        body='<script>window.missingHelper();</script>')
    line = only(check(broken and browser, broken), "script-error")
    assert "missingHelper" in line

    fixed = write_page(tmp_path, "works.html",
                       body='<script>window.helper = function () { return 1; };</script>')
    assert check(browser, fixed) == []


def test_an_unhandled_rejection_is_a_fault_too(browser, tmp_path):
    broken = write_page(
        tmp_path, "rejects.html",
        body='<script>Promise.reject(new Error("the pricing fetch failed"));</script>')
    assert lines_for(check(browser, broken), "script-error"), \
        "an unhandled rejection is exactly the bug that leaves a section empty"


# -- 3. contrast ------------------------------------------------------------

def test_text_below_wcag_aa_is_a_fault(browser, tmp_path):
    broken = write_page(tmp_path, "faint.html",
                        body='<p class="note">Terms apply to every order.</p>',
                        styles=".note { color: #bbbbbb; }")
    line = only(check(browser, broken), "contrast")
    assert "needs 4.5:1" in line
    assert "rgb(255, 255, 255)" in line           # the background it was measured on

    fixed = write_page(tmp_path, "read.html",
                       body='<p class="note">Terms apply to every order.</p>',
                       styles=".note { color: #595959; }")
    assert check(browser, fixed) == []


def test_large_text_is_held_to_three_to_one(browser, tmp_path):
    """The AA rule is not one number, and treating it as one would make every
    large heading a fault the standard does not call a fault."""
    path = write_page(tmp_path, "heading.html",
                      body='<p class="big">Built for Tuesdays</p>',
                      styles=".big { color: #767676; font-size: 32px; }")
    assert check(browser, path) == []

    worse = write_page(tmp_path, "heading-worse.html",
                       body='<p class="big">Built for Tuesdays</p>',
                       styles=".big { color: #b0b0b0; font-size: 32px; }")
    assert "needs 3:1 for large text" in only(check(browser, worse), "contrast")


def test_contrast_is_measured_against_the_real_background(browser, tmp_path):
    """White on a dark section is not white on the body's white, and a checker
    that read the element's own (transparent) background would say it was."""
    path = write_page(tmp_path, "banded.html",
                      body='<section class="dark"><p class="on-dark">Overnight delivery</p></section>',
                      styles=".dark { background: #10243a; padding: 24px; }"
                             ".on-dark { color: #f2f6fb; }")
    assert check(browser, path) == []


def test_text_over_a_background_image_is_not_guessed_at(browser, tmp_path):
    """There is no defensible number for text on an image, so duet says nothing
    rather than a number it cannot defend."""
    path = write_page(tmp_path, "hero.html",
                      body='<div class="hero"><p class="over">Widgets</p></div>',
                      styles=".hero { background-image: linear-gradient(#fff, #fff);"
                             " padding: 24px; } .over { color: #eeeeee; }")
    assert lines_for(check(browser, path), "contrast") == []


# -- 4. collapsed controls --------------------------------------------------

def test_a_control_under_the_target_size_is_a_fault(browser, tmp_path):
    """The prototype's real find: an email field that was 20px tall on a phone
    and nowhere else, on a page whose author had read the CSS a dozen times."""
    # border-box, so the heights below are the heights a visitor's finger gets:
    # an input's default padding and border are otherwise added to them, and
    # `height: 20px` would render as a 24px box that is not a fault at all.
    box = ".email { box-sizing: border-box; width: 240px; height: 44px;" \
          " border: 1px solid #555; }"
    broken = write_page(
        tmp_path, "signup.html",
        body='<form><input class="email" type="email" value="you@example.com"></form>',
        styles=box + "@media (max-width: 500px) { .email { height: 20px; } }")
    faults = check(browser, broken, widths=(1440, 375))
    line = only(faults, "collapsed-control")
    assert "input.email is 240x20px" in line
    assert "24px" in line

    fixed = write_page(
        tmp_path, "signup-fixed.html",
        body='<form><input class="email" type="email" value="you@example.com"></form>',
        styles=box)
    assert check(browser, fixed, widths=(1440, 375)) == []


def test_a_hidden_control_is_not_a_collapsed_one(browser, tmp_path):
    path = write_page(tmp_path, "hidden.html",
                      body='<form><input type="hidden" name="plan" value="pro">'
                           '<button class="go" style="height:44px;width:120px">Go</button></form>')
    assert check(browser, path) == []


# -- 5. empty strips --------------------------------------------------------

def test_a_tall_block_that_paints_nothing_is_a_fault(browser, tmp_path):
    """The prototype's other real find: a strip under a grid, on every load."""
    broken = write_page(tmp_path, "strip.html", body='<div class="filler"></div>',
                        styles=".filler { height: 64px; }")
    line = only(check(browser, broken), "empty-strip")
    assert "div.filler" in line
    assert "paints nothing" in line

    for name, styles in [("strip-bg.html", ".filler { height: 64px; background: #eef; }"),
                         ("strip-short.html", ".filler { height: 24px; }")]:
        fixed = write_page(tmp_path, name, body='<div class="filler"></div>', styles=styles)
        assert check(browser, fixed) == [], name


def test_a_deliberate_spacer_can_say_so(browser, tmp_path):
    """The exemption is the difference between a rule and a nuisance — and it
    covers descendants, so a whole deliberate region can be marked once."""
    path = write_page(
        tmp_path, "spacer.html",
        body='<div class="filler" data-duet-ignore></div>'
             '<div data-duet-ignore><div class="filler"></div>'
             '<img src="gone.png" alt="" width="40" height="40"></div>',
        styles=".filler { height: 64px; }")
    assert check(browser, path) == []


# -- 6. dead in-page links --------------------------------------------------

def test_an_anchor_pointing_at_nothing_is_a_fault(browser, tmp_path):
    broken = write_page(tmp_path, "nav.html", body='<a href="#pricing">Pricing</a>')
    line = only(check(browser, broken), "dead-link")
    assert '"#pricing"' in line

    fixed = write_page(tmp_path, "nav-fixed.html",
                       body='<a href="#pricing">Pricing</a><h2 id="pricing">Pricing</h2>')
    assert check(browser, fixed) == []


def test_the_top_of_the_page_and_other_hosts_are_not_dead_links(browser, tmp_path):
    path = write_page(tmp_path, "links.html",
                      body='<a href="#">Top</a> <a href="#top">Top</a> '
                           '<a href="https://example.invalid/x">Away</a>')
    assert check(browser, path) == []


# -- 7. broken local images -------------------------------------------------

def test_a_local_image_that_did_not_load_is_a_fault(browser, tmp_path):
    broken = write_page(tmp_path, "logo-broken.html",
                        body='<img src="logo.png" alt="Widgets" width="80" height="60">')
    line = only(check(browser, broken), "broken-image")
    assert 'src="logo.png"' in line

    (tmp_path / "logo.png").write_bytes(ONE_PIXEL_PNG)
    fixed = write_page(tmp_path, "logo-ok.html",
                       body='<img src="logo.png" alt="Widgets" width="80" height="60">')
    assert check(browser, fixed) == []


def test_an_image_on_another_host_is_not_reported(browser, tmp_path):
    """Outside hosts are blocked on purpose, so calling them broken would be
    duet reporting its own offline switch as a fault in the page."""
    path = write_page(tmp_path, "remote.html",
                      body='<img src="https://example.invalid/logo.png" alt=""'
                           ' width="80" height="60">')
    assert check(browser, path) == []


# -- 8. the viewport meta ---------------------------------------------------

def test_a_page_with_no_viewport_meta_is_a_fault(browser, tmp_path):
    broken = write_page(tmp_path, "desktop-only.html", raw=NO_VIEWPORT)
    line = only(check(browser, broken), "no-viewport-meta")
    assert "desktop width" in line
    assert check(browser, write_page(tmp_path, "landing2.html")) == []


# -- the project's own rules ------------------------------------------------

def test_a_style_rule_is_measured_on_the_computed_style(browser, tmp_path):
    rules = page.parse_rules({"styles": [
        {"selector": ".cta", "property": "min-height", "at_least": 44}]})
    broken = write_page(tmp_path, "cta-small.html", body='<a class="cta">Order one</a>',
                        styles=".cta { display: block; min-height: 20px; }")
    line = only(check(browser, broken, rules=rules), "style-rule")
    assert "min-height is 20px" in line
    assert "under the required 44px" in line

    fixed = write_page(tmp_path, "cta-big.html", body='<a class="cta">Order one</a>',
                       styles=".cta { display: block; min-height: 44px; }")
    assert check(browser, fixed, rules=rules) == []


def test_a_font_stack_matches_on_its_first_family(browser, tmp_path):
    """A brand kit says "Metro Sans"; the computed value is the whole stack."""
    rules = page.parse_rules({"styles": [
        {"selector": "h1", "property": "font-family", "equals": "Metro Sans"}]})
    broken = write_page(tmp_path, "wrong-font.html")
    assert "not \"Metro Sans\"" in only(check(browser, broken, rules=rules), "style-rule")

    fixed = write_page(tmp_path, "right-font.html",
                       styles='h1 { font-family: "Metro Sans", Helvetica, sans-serif; }')
    assert check(browser, fixed, rules=rules) == []


def test_a_forbidden_text_colour_is_found_however_it_is_written(browser, tmp_path):
    rules = page.parse_rules({"forbid_text_colors": ["#7a0000"]})
    broken = write_page(tmp_path, "old-red.html",
                        body='<p class="warn">Orders close at noon.</p>',
                        styles=".warn { color: rgb(122, 0, 0); }")
    line = only(check(browser, broken, rules=rules), "text-color")
    assert "#7a0000" in line
    assert "Orders close at noon." in line

    fixed = write_page(tmp_path, "new-red.html",
                       body='<p class="warn">Orders close at noon.</p>',
                       styles=".warn { color: #8b1a1a; }")
    assert check(browser, fixed, rules=rules) == []


def test_forbidden_text_is_read_off_the_rendered_page(browser, tmp_path):
    """Rendered, not the source: a word hidden in a comment is not on the page,
    and a word a stylesheet uppercases still is."""
    rules = page.parse_rules({"forbid_text": [r"\bcoming soon\b"]})
    broken = write_page(tmp_path, "soon.html",
                        body='<p class="shout">Coming Soon</p>',
                        styles=".shout { text-transform: uppercase; }")
    assert lines_for(check(browser, broken, rules=rules), "forbidden-text")

    fixed = write_page(tmp_path, "shipped.html", body='<!-- coming soon -->'
                                                      '<p>Shipping today</p>')
    assert check(browser, fixed, rules=rules) == []


# -- the command, end to end ------------------------------------------------

def test_a_faulty_page_exits_1_and_a_clean_one_exits_0(browser, tmp_path, capsys):
    """Through main(), with a real browser and no monkeypatching: the exit code a
    gate actually sees."""
    write_page(tmp_path, "broken.html", body='<div class="filler"></div>',
               styles=".filler { height: 64px; }")
    assert main(["page", "check", "broken.html", "--width", "375",
                 "-C", str(tmp_path)]) == 1
    assert "375: empty-strip: div.filler" in capsys.readouterr().out

    write_page(tmp_path, "clean.html")
    assert main(["page", "check", "clean.html", "--width", "375",
                 "-C", str(tmp_path)]) == 0
    assert "clean" in capsys.readouterr().out


def test_the_rules_file_is_picked_up_from_the_project_root(browser, tmp_path, capsys):
    write_page(tmp_path, "index.html", body='<a class="cta">Order one</a>',
               styles=".cta { display: block; min-height: 20px; }")
    (tmp_path / page.PAGE_RULES).write_text(json.dumps({
        "page": "index.html",
        "styles": [{"selector": ".cta", "property": "min-height", "at_least": 44}],
    }), encoding="utf-8")
    assert main(["page", "check", "--width", "375", "-C", str(tmp_path)]) == 1
    assert "style-rule" in capsys.readouterr().out


# -- screenshots ------------------------------------------------------------

def png_size(path):
    raw = Path(path).read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", raw[16:24])


def test_one_full_page_shot_per_width(browser, tmp_path, capsys):
    write_page(tmp_path, "landing.html",
               body='<div class="tall">Specifications</div>',
               styles=".tall { height: 1400px; }")
    assert main(["page", "shot", "landing.html", "--width", "1440,375",
                 "-C", str(tmp_path)]) == 0
    written = [line for line in capsys.readouterr().out.splitlines() if line.endswith(".png")]
    assert len(written) == 2
    for path, width in zip(written, (1440, 375)):
        assert page.SHOT_DIR.replace("/", "%s" % "/") in path
        shot_width, shot_height = png_size(path)
        assert shot_width == width
        # Full page, not the viewport: the 1400px block is in the shot.
        assert shot_height > 1400


def test_a_page_taller_than_chrome_will_paint_is_clipped(browser, tmp_path, capsys):
    """Chrome refuses a canvas much past 16,000px and a phone-width page gets
    there easily. A shot that stops and says so beats a call that fails."""
    write_page(tmp_path, "endless.html", body='<div class="tall">Specifications</div>',
               styles=".tall { height: 4000px; }")
    monkeypatched = 1200
    original = page.MAX_SHOT_HEIGHT
    page.MAX_SHOT_HEIGHT = monkeypatched
    try:
        paths = page.shoot_page(str(tmp_path / "endless.html"), [375],
                               str(tmp_path / "shots"), browser=browser)
    finally:
        page.MAX_SHOT_HEIGHT = original
    assert png_size(paths[0]) == (375, monkeypatched)
    assert "as far as Chrome will paint" in capsys.readouterr().out


def test_a_shot_of_a_sideways_page_is_the_document_not_the_zoomed_out_layout(
        browser, tmp_path, capsys):
    """The page `duet page check` calls an overflow, shot.

    Under phone emulation a page wider than the viewport makes Chrome zoom the
    layout out to fit it on the screen, and Page.getLayoutMetrics then reports
    that zoomed-out layout: 917x1984 for a document 917px wide and ~150px tall.
    Taking the height from there gave a 375x1984 PNG that was nine parts blank,
    and clipping it to the viewport left the 542px of panel hanging off the
    right edge — the whole reason to look at the page — out of the image.
    """
    write_page(tmp_path, "wide.html", body='<div class="panel">Specifications</div>',
               styles=".panel { width: 900px; background: #eee; }")
    assert only(check(browser, tmp_path / "wide.html"), "overflow")   # it does overflow

    assert main(["page", "shot", "wide.html", "--width", "375", "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    written = [line for line in out.splitlines() if line.endswith(".png")]
    shot_width, shot_height = png_size(written[0])
    # Wide enough to hold the panel that runs off the edge...
    assert shot_width > 900
    # ...and no taller than the document, rather than the 1984px Chrome's
    # content size claims. The heading and the panel, and nothing below them.
    assert shot_height < 500, "the shot is mostly blank canvas: %dpx" % shot_height
    assert "wide-375w.png — page is %dpx wide at 375px, so the image is too" % shot_width in out


def test_a_page_with_no_viewport_meta_is_still_shot_at_the_width_asked_for(
        browser, tmp_path, capsys):
    """Chrome lays a page with no viewport meta out at its 980px desktop
    fallback on a 375px phone, and the content then measures a pixel past that
    box. A pixel is rounding, not overflow: widening the shot for it would move
    the image of every such page and show nothing. This is the page the
    `no-viewport-meta` rule is about, so it is worth holding still."""
    write_page(tmp_path, "bare.html", raw=NO_VIEWPORT)
    assert main(["page", "shot", "bare.html", "--width", "375", "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    written = [line for line in out.splitlines() if line.endswith(".png")]
    assert png_size(written[0])[0] == 375
    assert "note" not in out


def test_a_document_with_no_body_is_measured_by_its_root_element(browser, tmp_path, capsys):
    """An SVG opened on its own has no <body> to measure. Its root element's
    box is the document — 1200x120 — where Chrome's content size is the
    zoomed-out 2599px, so this is the same bug in a document with no body."""
    (tmp_path / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="120">'
        '<rect width="1200" height="120" fill="#eee"/></svg>', encoding="utf-8")
    assert main(["page", "shot", "logo.svg", "--width", "375", "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    written = [line for line in out.splitlines() if line.endswith(".png")]
    shot_width, shot_height = png_size(written[0])
    assert shot_width == 1200
    assert shot_height < 200, "the shot is mostly blank canvas: %dpx" % shot_height


def test_a_shot_of_a_page_that_fits_is_the_width_it_was_asked_for(browser, tmp_path, capsys):
    """The other half: nothing moves for a page that does not overflow. A short
    page still gets the viewport's height under it, as a visitor's screen does."""
    write_page(tmp_path, "landing.html")
    assert main(["page", "shot", "landing.html", "--width", "375", "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    written = [line for line in out.splitlines() if line.endswith(".png")]
    assert png_size(written[0]) == (375, page.PHONE_HEIGHT)
    assert "note" not in out


def test_shots_go_where_they_are_asked_to(browser, tmp_path, capsys):
    write_page(tmp_path, "landing.html")
    out = tmp_path / "look"
    assert main(["page", "shot", "landing.html", "--width", "375",
                 "--out", str(out), "-C", str(tmp_path)]) == 0
    shots = sorted(out.glob("*.png"))
    assert [p.name for p in shots] == ["landing-375w.png"]


# -- a website session, with the real page check as its gate ----------------

def test_duet_add_on_a_website_is_proven_by_a_page_rule(browser, tmp_path, monkeypatch):
    """Item by item what a website session is meant to do: the pair fixes the
    page and writes the rule that catches it; the harness replays that rule,
    with the real `duet page check`, against the page as it was — where it fails.

    `browser` is only requested so this skips with the rest when there is no
    Chrome; the session starts its own.
    """
    from duet.adapters.mock import MockAdapter, envelope
    from duet.config import AgentSpec, Config
    from duet.orchestrator import Orchestrator

    small = ".cta { display: block; min-height: 20px; }"
    big = ".cta { display: block; min-height: 44px; }"
    write_page(tmp_path, "index.html", body='<a class="cta">Order one</a>', styles=small)
    rules = json.dumps({"page": "index.html", "styles": [
        {"selector": ".cta", "property": "min-height", "at_least": 44}]})

    # PYTHONPATH, so `<python> -m duet` finds the duet under test and not
    # whichever one is installed in this interpreter's site-packages. Set in
    # the environment the gate inherits, not as a `PYTHONPATH=... cmd` prefix:
    # that is bash syntax, and cmd.exe runs Windows gates.
    from duet.winlaunch import quote_for_shell
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    gate = "%s -m duet page check index.html --width 375" % quote_for_shell(sys.executable)
    done = envelope("I would ship this", "DONE", confidence=0.9)
    work = envelope("a real tap target, and the rule that holds it there", "DONE", patches=[
        {"path": "index.html", "content": TEMPLATE.format(
            body='<a class="cta">Order one</a>', styles=big)},
        {"path": page.PAGE_RULES, "content": rules},
    ])
    cfg = Config(task="a tap target anyone can hit", root=str(tmp_path),
                 agents=[AgentSpec("claude", "mock"), AgentSpec("gpt", "mock")],
                 start="claude", max_rounds=4, workflow="add", gate=gate)
    orch = Orchestrator(cfg, adapters={
        "claude": MockAdapter(name="claude", cwd=str(tmp_path),
                              config={"script": [work, done, done]}),
        "gpt": MockAdapter(name="gpt", cwd=str(tmp_path), config={"script": [done] * 3}),
    })
    result = orch.run()
    assert result.status == "consensus"
    assert cfg.workflow_state.get("replay") == "fails on original"
