"""`duet page`: a gate for a website, and eyes for the two agents.

A project with a test suite gets a gate for free — the harness runs it and
neither agent can argue with the output. A website usually has no test command
at all, so a duet session on one is two models agreeing about CSS that neither
of them has seen rendered. That is the failure this closes.

`duet page check` renders the page in headless Chrome at each width and prints
one fault per line, prefixed by the width. Every built-in rule is objective —
something measured on the rendered page, not an opinion about the design:

  overflow           the document is wider than the viewport
  script-error       an uncaught exception or rejection while loading
  contrast           visible text below WCAG AA against its real background
  collapsed-control  a control smaller than the 24px WCAG 2.5.8 target size
  empty-strip        a tall visible block that paints nothing at all
  dead-link          href="#x" with no element x
  broken-image       a local image that did not load
  no-viewport-meta   no <meta name="viewport">, so phones get a desktop layout

Those were not chosen from a standards list. A prototype of this found two real
bugs in a page whose author had read the CSS a dozen times: a blank strip under
a grid on every load, and an email field that collapsed to 20px tall on a phone.
Both are in the list above, and both are invisible to anything that reads markup
instead of rendering it.

Anything marked `data-duet-ignore` is exempt, itself and its descendants, so a
deliberate spacer stays deliberate.

The brand-kit half — this heading font, that minimum tap target, never this
colour, never that word — is not built in, because it is different for every
project. It lives in `duet-page.json`; see RULES_DOC below for the format.

Exit codes: 0 clean, 1 the page has faults, 3 duet could not run the check at
all (no Chrome, no such page, a rules file that does not make sense). The third
is held apart from the second on purpose: a session that reads "no Chrome" as "a
broken page" spends its rounds hunting a bug that is not there.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from duet import ui
from duet.chrome import SLOW_RETRY_FACTOR, Browser, SetupError, unsupported_platform

# The project's own rules, next to the project's own files.
PAGE_RULES = "duet-page.json"
# 1440 is a laptop; 375 is the phone every collapsed control shows up on.
DEFAULT_WIDTHS = (1440, 375)
# Headless Chrome will not make a window narrower than this, which is why
# widths below it are emulated as a phone rather than sized as a window.
PHONE_MAX_WIDTH = 500
PHONE_HEIGHT = 812
DESKTOP_HEIGHT = 900
# Where shots go when nobody says otherwise: beside the session's own files.
SHOT_DIR = os.path.join(".duet", "shots")
# Chrome refuses to paint a canvas much taller than this, and a phone-width
# page can easily exceed it.
MAX_SHOT_HEIGHT = 16000
# How long to wait for the load event, and how long to let the page settle
# afterwards — two animation frames plus this, per settle step.
LOAD_TIMEOUT = 25.0
SETTLE_MS = 60


RULES_DOC = """\
duet-page.json — this project's own page rules, on top of the built-in ones.

{
  "page": "site/index.html",

  "styles": [
    {"selector": "h1, h2", "property": "font-family", "equals": "Metro Sans"},
    {"selector": ".cta",   "property": "min-height",  "at_least": 44},
    {"selector": "body",   "property": "font-size",   "one_of": [16, 17, 18]},
    {"selector": ".badge", "property": "letter-spacing", "at_most": "0.5px"}
  ],

  "forbid_text_colors": ["#7a0000", "rgb(122, 0, 0)"],

  "forbid_text": ["\\\\bcoming soon\\\\b", "lorem ipsum"]
}

page                 the file or URL `duet page check` uses when none is given,
                     and the one gate detection picks for this project.
styles               a CSS selector, a computed style property, and exactly one
                     of: equals, one_of, at_least, at_most. equals and one_of
                     compare text (case-insensitively, quotes stripped, and a
                     font stack matches on its first family, so "Metro Sans"
                     matches a computed `"Metro Sans", sans-serif`). at_least
                     and at_most compare the leading number, in px.
forbid_text_colors   colours that must never carry text. Written any way CSS
                     accepts; compared as the browser resolves them.
                     `forbid_text_colours` is accepted too.
forbid_text          regular expressions (Python syntax) that must never appear
                     in the page's rendered text. Matched without regard to
                     case, because a headline is written "Coming Soon" and a
                     rule that missed it would look like it was checking;
                     `(?-i:Beta)` when the case is the point.

Unknown keys are an error, not a comment: a mistyped rule that is silently
ignored is worse than no rule, because it looks like it is being checked.
"""

STYLE_COMPARATORS = ("equals", "one_of", "at_least", "at_most")
STYLE_KEYS = ("selector", "property") + STYLE_COMPARATORS
TOP_LEVEL_KEYS = ("page", "styles", "forbid_text",
                  "forbid_text_colors", "forbid_text_colours")


class RulesError(SetupError):
    """The rules file does not make sense. A setup fault, so exit 3."""


class Rules(object):
    """This project's own rules, already validated."""

    def __init__(self, page: str = "", styles: Optional[List[Dict[str, Any]]] = None,
                 text_colors: Optional[List[str]] = None,
                 forbid_text: Optional[List[str]] = None, source: str = ""):
        self.page = page
        self.styles = styles or []
        self.text_colors = text_colors or []
        self.forbid_text = forbid_text or []
        self.source = source

    def __bool__(self) -> bool:
        return bool(self.styles or self.text_colors or self.forbid_text)

    __nonzero__ = __bool__      # pragma: no cover - Python 2 shaped habit

    def payload(self) -> Dict[str, Any]:
        """What the probe needs, as plain data."""
        return {"styles": self.styles, "text_colors": self.text_colors}


def _one_of_type(value: Any, types: Tuple[type, ...]) -> bool:
    return isinstance(value, types) and not isinstance(value, bool)


def parse_rules(data: Any, source: str = PAGE_RULES) -> Rules:
    """Validate the rules file's contents, or raise RulesError saying why."""
    if not isinstance(data, dict):
        raise RulesError("%s must hold a JSON object, not %s"
                         % (source, type(data).__name__))
    unknown = [k for k in data if k not in TOP_LEVEL_KEYS]
    if unknown:
        raise RulesError(
            "%s: unknown key%s %s. Known keys: %s"
            % (source, "" if len(unknown) == 1 else "s",
               ", ".join(repr(k) for k in sorted(unknown)), ", ".join(TOP_LEVEL_KEYS)))

    page = data.get("page", "")
    if not isinstance(page, str):
        raise RulesError("%s: \"page\" must be a file or URL, not %s"
                         % (source, type(page).__name__))

    styles: List[Dict[str, Any]] = []
    raw_styles = data.get("styles", [])
    if not isinstance(raw_styles, list):
        raise RulesError("%s: \"styles\" must be a list of rules" % source)
    for index, rule in enumerate(raw_styles):
        styles.append(_parse_style(rule, index, source))

    colors: List[str] = []
    for key in ("forbid_text_colors", "forbid_text_colours"):
        values = data.get(key, [])
        if not isinstance(values, list):
            raise RulesError("%s: \"%s\" must be a list of colours" % (source, key))
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise RulesError("%s: \"%s\" holds %r, which is not a colour"
                                 % (source, key, value))
            if value not in colors:
                colors.append(value)

    patterns: List[str] = []
    raw_text = data.get("forbid_text", [])
    if not isinstance(raw_text, list):
        raise RulesError("%s: \"forbid_text\" must be a list of regular expressions" % source)
    for pattern in raw_text:
        if not isinstance(pattern, str) or not pattern:
            raise RulesError("%s: \"forbid_text\" holds %r, which is not a regular "
                             "expression" % (source, pattern))
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RulesError("%s: \"forbid_text\" pattern %r is not a valid regular "
                             "expression: %s" % (source, pattern, exc))
        patterns.append(pattern)

    return Rules(page=page, styles=styles, text_colors=colors,
                 forbid_text=patterns, source=source)


def _parse_style(rule: Any, index: int, source: str) -> Dict[str, Any]:
    at = "%s: styles[%d]" % (source, index)
    if not isinstance(rule, dict):
        raise RulesError("%s must be an object with a selector and a property" % at)
    unknown = [k for k in rule if k not in STYLE_KEYS]
    if unknown:
        raise RulesError("%s: unknown key%s %s. Known keys: %s"
                         % (at, "" if len(unknown) == 1 else "s",
                            ", ".join(repr(k) for k in sorted(unknown)),
                            ", ".join(STYLE_KEYS)))
    for field in ("selector", "property"):
        value = rule.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RulesError("%s needs a non-empty %r" % (at, field))
    present = [c for c in STYLE_COMPARATORS if c in rule]
    if len(present) != 1:
        raise RulesError(
            "%s must say exactly one of %s — it has %s"
            % (at, ", ".join(STYLE_COMPARATORS),
               ", ".join(present) if present else "none"))
    comparator = present[0]
    value = rule[comparator]
    if comparator == "one_of":
        if not isinstance(value, list) or not value:
            raise RulesError("%s: \"one_of\" must be a non-empty list" % at)
        for item in value:
            if not _one_of_type(item, (str, int, float)):
                raise RulesError("%s: \"one_of\" holds %r, which is not a value" % (at, item))
        value = [str(item) for item in value]
    elif comparator in ("at_least", "at_most"):
        if _number(value) is None:
            raise RulesError("%s: \"%s\" must be a number of pixels, not %r"
                             % (at, comparator, value))
        value = _number(value)
    else:
        if not _one_of_type(value, (str, int, float)):
            raise RulesError("%s: \"equals\" must be a value, not %r" % (at, value))
        value = str(value)
    return {"selector": rule["selector"], "property": rule["property"],
            "comparator": comparator, "value": value}


def _number(value: Any) -> Optional[float]:
    """The pixel count in 44, 44.0 or "44px" — or None if there is not one."""
    if _one_of_type(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*(px)?\s*$", value)
        if match:
            return float(match.group(1))
    return None


def load_rules(path: Optional[str], root: str = ".") -> Rules:
    """Read the rules file. A path given explicitly must exist; the default need not."""
    if path:
        target = Path(path).expanduser()
        if not target.is_file():
            raise RulesError("no rules file at %s" % path)
    else:
        target = Path(root).expanduser() / PAGE_RULES
        if not target.is_file():
            return Rules()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesError("could not read %s: %s" % (target, exc))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RulesError("%s is not valid JSON: %s" % (target, exc))
    return parse_rules(data, source=str(target))


# --------------------------------------------------------------------------
# The probe. Everything it reports is measured on the rendered page.
#
# It runs as one expression and hands back JSON, because one round trip that
# returns everything is easier to reason about than fifteen that each return a
# piece — and because a rule that needs a second look at the DOM after the
# first has already changed it is not a rule, it is a race.
PROBE_JS = r"""
(function (RULES) {
  var faults = [];
  function add(rule, detail) { faults.push({rule: rule, detail: detail}); }

  function ignored(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (n.hasAttribute && n.hasAttribute('data-duet-ignore')) return true;
    }
    return false;
  }
  function styleOf(el) {
    try { return window.getComputedStyle(el); } catch (e) { return null; }
  }
  function visible(el) {
    var s = styleOf(el);
    if (!s) return false;
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    if (parseFloat(s.opacity) === 0) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function where(el) {
    var name = el.tagName.toLowerCase();
    if (el.id) return name + '#' + el.id;
    var cls = (typeof el.className === 'string') ? el.className.trim().split(/\s+/)[0] : '';
    return cls ? name + '.' + cls : name;
  }
  function snippet(el) {
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    return t.length > 40 ? (t.slice(0, 40) + '…') : t;
  }
  function round1(n) { return Math.round(n * 10) / 10; }

  // -- colour ------------------------------------------------------------
  function rgba(text) {
    var m = /rgba?\(([^)]+)\)/.exec(text || '');
    if (!m) return null;
    var parts = m[1].split(/[,\/\s]+/).filter(function (x) { return x !== ''; });
    var c = {r: parseFloat(parts[0]), g: parseFloat(parts[1]), b: parseFloat(parts[2]),
             a: parts.length > 3 ? parseFloat(parts[3]) : 1};
    if (isNaN(c.r) || isNaN(c.g) || isNaN(c.b)) return null;
    if (isNaN(c.a)) c.a = 1;
    return c;
  }
  function over(top, bottom) {
    var a = top.a;
    return {r: top.r * a + bottom.r * (1 - a),
            g: top.g * a + bottom.g * (1 - a),
            b: top.b * a + bottom.b * (1 - a), a: 1};
  }
  function luminance(c) {
    function channel(v) {
      v = v / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
  }
  function contrast(fg, bg) {
    var l1 = luminance(fg), l2 = luminance(bg);
    if (l2 > l1) { var t = l1; l1 = l2; l2 = t; }
    return (l1 + 0.05) / (l2 + 0.05);
  }
  function show(c) {
    return 'rgb(' + Math.round(c.r) + ', ' + Math.round(c.g) + ', ' + Math.round(c.b) + ')';
  }
  // The background text actually sits on, or null when it cannot be known:
  // any background image or gradient in the way and duet says nothing rather
  // than guessing a number it cannot defend.
  function backgroundOf(el) {
    var layers = [];
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      var s = styleOf(n);
      if (!s) break;
      if (s.backgroundImage && s.backgroundImage !== 'none') return null;
      var c = rgba(s.backgroundColor);
      if (c && c.a > 0) {
        layers.push(c);
        if (c.a >= 1) break;
      }
    }
    var base = {r: 255, g: 255, b: 255, a: 1};
    for (var i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
    return base;
  }
  var probeHost = document.createElement('span');
  probeHost.setAttribute('data-duet-ignore', '');
  probeHost.style.display = 'none';
  (document.body || document.documentElement).appendChild(probeHost);
  function resolveColor(text) {
    probeHost.style.color = '';
    probeHost.style.color = text;
    if (!probeHost.style.color) return null;       // not a colour the browser knows
    var resolved = styleOf(probeHost);
    return resolved ? rgba(resolved.color) : null;
  }

  // -- which elements carry text ----------------------------------------
  var NOT_TEXT = {SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TITLE: 1, TEMPLATE: 1,
                  HEAD: 1, META: 1, LINK: 1, OPTION: 1, BR: 1};
  function ownText(el) {
    if (NOT_TEXT[el.tagName]) return '';
    for (var i = 0; i < el.childNodes.length; i++) {
      var node = el.childNodes[i];
      if (node.nodeType === 3 && node.nodeValue && node.nodeValue.trim()) {
        return node.nodeValue.trim();
      }
    }
    return '';
  }

  var all = document.querySelectorAll('*');
  var textual = [];
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (ignored(el) || !ownText(el) || !visible(el)) continue;
    textual.push(el);
  }

  // -- 1. horizontal overflow -------------------------------------------
  // The layout viewport, which is *not* window.innerWidth. Under phone
  // emulation Chrome widens innerWidth to the widest thing on the page, so a
  // 900px panel on a 375px phone reports innerWidth 917 and measures as
  // fitting exactly — the rule that matters most, silently never firing.
  // documentElement.clientWidth is the box the layout was actually done in,
  // and comparing scrollWidth against it is what "does it scroll sideways"
  // means.
  var viewport = document.documentElement.clientWidth || window.innerWidth;
  var docWidth = Math.max(document.documentElement.scrollWidth,
                          document.body ? document.body.scrollWidth : 0);
  if (docWidth > viewport + 1) {
    var widest = '', over_ = 0;
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (ignored(el) || !visible(el)) continue;
      var r = el.getBoundingClientRect();
      var right = r.right + window.scrollX;
      if (right > viewport + 1 && (right - viewport) > over_ && el.children.length === 0) {
        over_ = right - viewport; widest = where(el);
      }
    }
    add('overflow', 'the page is ' + Math.round(docWidth) + 'px wide in a ' +
        Math.round(viewport) + 'px viewport, so it scrolls sideways' +
        (widest ? ' (widest: ' + widest + ', ' + Math.round(over_) + 'px past the edge)' : ''));
  }

  // -- 2. viewport meta --------------------------------------------------
  var metas = document.getElementsByTagName('meta'), hasViewport = false;
  for (var i = 0; i < metas.length; i++) {
    if ((metas[i].getAttribute('name') || '').toLowerCase() === 'viewport') hasViewport = true;
  }
  if (!hasViewport) {
    add('no-viewport-meta', 'no <meta name="viewport">, so a phone lays the page out ' +
        'at desktop width and scales it down');
  }

  // -- 3. contrast -------------------------------------------------------
  for (var i = 0; i < textual.length; i++) {
    var el = textual[i], s = styleOf(el);
    var fg = rgba(s.color);
    if (!fg || fg.a === 0) continue;
    var bg = backgroundOf(el);
    if (!bg) continue;                       // behind an image; not measurable
    if (fg.a < 1) fg = over(fg, bg);
    var size = parseFloat(s.fontSize) || 16;
    var weight = parseInt(s.fontWeight, 10);
    if (isNaN(weight)) weight = (s.fontWeight === 'bold' || s.fontWeight === 'bolder') ? 700 : 400;
    var large = size >= 24 || (size >= 18.66 && weight >= 700);
    var need = large ? 3 : 4.5;
    var got = contrast(fg, bg);
    if (got + 0.05 < need) {
      add('contrast', where(el) + ' has ' + round1(got) + ':1 (needs ' + need + ':1' +
          (large ? ' for large text' : '') + '): ' + show(fg) + ' on ' + show(bg) +
          ' — "' + snippet(el) + '"');
    }
  }

  // -- 4. collapsed controls --------------------------------------------
  var controls = document.querySelectorAll('input, select, textarea, button');
  for (var i = 0; i < controls.length; i++) {
    var el = controls[i];
    if (ignored(el) || !visible(el)) continue;
    var r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 24) {
      add('collapsed-control', where(el) + ' is ' + round1(r.width) + 'x' + round1(r.height) +
          'px, under the 24px minimum target size');
    }
  }

  // -- 5. empty strips ---------------------------------------------------
  var MEDIA = {IMG: 1, SVG: 1, VIDEO: 1, CANVAS: 1, IFRAME: 1, OBJECT: 1, EMBED: 1,
               AUDIO: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1, BUTTON: 1, HR: 1,
               PICTURE: 1, MAP: 1, PROGRESS: 1, METER: 1};
  function paints(el) {
    if (MEDIA[el.tagName]) return true;
    var s = styleOf(el);
    if (!s) return false;
    if (s.backgroundImage && s.backgroundImage !== 'none') return true;
    var bg = rgba(s.backgroundColor);
    if (bg && bg.a > 0) return true;
    var sides = ['Top', 'Right', 'Bottom', 'Left'];
    for (var i = 0; i < sides.length; i++) {
      if (parseFloat(s['border' + sides[i] + 'Width']) > 0 &&
          s['border' + sides[i] + 'Style'] !== 'none') return true;
    }
    if (s.boxShadow && s.boxShadow !== 'none') return true;
    if (s.outlineStyle && s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) return true;
    return false;
  }
  function blank(el) {
    if ((el.textContent || '').trim()) return false;
    if (paints(el)) return false;
    var inside = el.querySelectorAll('*');
    for (var i = 0; i < inside.length; i++) {
      if (paints(inside[i])) return false;
    }
    return true;
  }
  var strips = [];
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (el === document.body || el === document.documentElement) continue;
    if (ignored(el) || !visible(el)) continue;
    var s = styleOf(el);
    if (!s || s.display === 'inline') continue;
    var r = el.getBoundingClientRect();
    if (r.height < 48) continue;
    if (!blank(el)) continue;
    var nested = false;
    for (var j = 0; j < strips.length; j++) {
      if (strips[j].contains(el)) { nested = true; break; }
    }
    if (nested) continue;                    // report the outermost only
    strips.push(el);
    add('empty-strip', where(el) + ' is ' + round1(r.width) + 'x' + round1(r.height) +
        'px and paints nothing: no text, no image, no background, no border');
  }

  // -- 6. dead in-page links --------------------------------------------
  var seenHref = {};
  var links = document.querySelectorAll('a[href]');
  for (var i = 0; i < links.length; i++) {
    var el = links[i];
    if (ignored(el)) continue;
    var href = el.getAttribute('href') || '';
    if (href.charAt(0) !== '#') continue;
    var id = href.slice(1);
    try { id = decodeURIComponent(id); } catch (e) { /* leave it as written */ }
    if (!id || id === 'top') continue;       // "#" and "#top" mean the top of the page
    if (document.getElementById(id)) continue;
    if (document.getElementsByName(id).length) continue;
    if (seenHref[href]) continue;
    seenHref[href] = 1;
    add('dead-link', where(el) + ' points at "' + href + '", and nothing on the page has that id');
  }

  // -- 7. broken local images -------------------------------------------
  var local = location.protocol === 'file:' ? 'file:' : location.origin;
  var images = document.getElementsByTagName('img');
  for (var i = 0; i < images.length; i++) {
    var el = images[i];
    if (ignored(el)) continue;
    var written = el.getAttribute('src');
    if (!written) continue;
    var src = el.currentSrc || el.src || '';
    if (src.indexOf('data:') === 0) continue;
    if (src.indexOf(local) !== 0) continue;  // another host: blocked on purpose
    if (el.complete && el.naturalWidth === 0) {
      add('broken-image', where(el) + ' did not load: src="' + written + '"');
    }
  }

  // -- 8. this project's own style rules --------------------------------
  function first(value) { return String(value).split(',')[0]; }
  function normalise(value) {
    return String(value).replace(/["']/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
  }
  function leading(value) {
    var m = /-?\d+(\.\d+)?/.exec(String(value));
    return m ? parseFloat(m[0]) : null;
  }
  var styleRules = RULES.styles || [];
  for (var i = 0; i < styleRules.length; i++) {
    var rule = styleRules[i];
    var matched;
    try { matched = document.querySelectorAll(rule.selector); } catch (e) {
      add('style-rule', 'selector ' + JSON.stringify(rule.selector) + ' is not valid CSS');
      continue;
    }
    for (var j = 0; j < matched.length; j++) {
      var el = matched[j];
      if (ignored(el)) continue;
      var s = styleOf(el);
      if (!s) continue;
      var got = s.getPropertyValue(rule.property);
      if (got === null || got === undefined || got === '') {
        add('style-rule', where(el) + ' has no computed ' + rule.property +
            ' — is that a real property?');
        continue;
      }
      var detail = where(el) + ' ' + rule.property + ' is ' + String(got).trim();
      if (rule.comparator === 'equals' || rule.comparator === 'one_of') {
        var wanted = rule.comparator === 'equals' ? [rule.value] : rule.value;
        var ok = false;
        for (var k = 0; k < wanted.length; k++) {
          var want = normalise(wanted[k]);
          if (normalise(got) === want || normalise(first(got)) === want) { ok = true; break; }
        }
        if (!ok) {
          add('style-rule', detail + ', not ' +
              (rule.comparator === 'equals' ? JSON.stringify(rule.value)
                                            : 'one of ' + JSON.stringify(rule.value)));
        }
      } else {
        var number = leading(got);
        if (number === null) {
          add('style-rule', detail + ', which is not a number of pixels');
        } else if (rule.comparator === 'at_least' && number < rule.value) {
          add('style-rule', detail + ', under the required ' + rule.value + 'px');
        } else if (rule.comparator === 'at_most' && number > rule.value) {
          add('style-rule', detail + ', over the allowed ' + rule.value + 'px');
        }
      }
    }
  }

  // -- 9. colours that must never carry text ----------------------------
  var forbidden = [];
  var wantedColors = RULES.text_colors || [];
  for (var i = 0; i < wantedColors.length; i++) {
    var c = resolveColor(wantedColors[i]);
    if (!c) {
      add('text-color', JSON.stringify(wantedColors[i]) + ' is not a colour this browser knows');
      continue;
    }
    forbidden.push({written: wantedColors[i], color: c});
  }
  for (var i = 0; i < textual.length; i++) {
    var el = textual[i], s = styleOf(el);
    var c = rgba(s.color);
    if (!c) continue;
    for (var j = 0; j < forbidden.length; j++) {
      var f = forbidden[j].color;
      if (Math.round(c.r) === Math.round(f.r) && Math.round(c.g) === Math.round(f.g) &&
          Math.round(c.b) === Math.round(f.b)) {
        add('text-color', where(el) + ' carries text in ' + forbidden[j].written +
            ' — "' + snippet(el) + '"');
      }
    }
  }

  probeHost.parentNode.removeChild(probeHost);
  return JSON.stringify({
    faults: faults,
    viewport: viewport,
    text: document.body ? document.body.innerText : ''
  });
})
"""

# Applied from the first byte of the document, not after load: a reveal-on-scroll
# section caught mid-fade measures as invisible, or as text at 40% opacity that
# fails contrast for a reason no visitor ever sees. Durations go to zero rather
# than `animation: none`, which would *cancel* a reveal and leave the element
# stuck at the opacity it starts from.
SETTLE_CSS = (
    "*,*::before,*::after{"
    "transition-duration:0s!important;transition-delay:0s!important;"
    "animation-duration:0.001s!important;animation-delay:0s!important;"
    "animation-iteration-count:1!important;}"
    "html{scroll-behavior:auto!important;}"
)
SETTLE_JS = """
(function () {
  var style = document.createElement('style');
  style.setAttribute('data-duet-ignore', '');
  style.textContent = %s;
  function attach() { (document.head || document.documentElement).appendChild(style); }
  if (document.head || document.documentElement) attach();
  else document.addEventListener('DOMContentLoaded', attach);
})();
""" % json.dumps(SETTLE_CSS)

# Scrolled through once, because a reveal driven by IntersectionObserver never
# fires in a viewport that never moves, and then back to the top so the shot and
# the measurements are of the page as it first appears.
SCROLL_THROUGH_JS = """
(function () {
  var height = Math.max(document.body ? document.body.scrollHeight : 0,
                        document.documentElement.scrollHeight);
  window.scrollTo(0, height);
  return height;
})()
"""

# What the document actually measures, asked of the page rather than of Chrome.
# Page.getLayoutMetrics reports the *scrolling* area, and under phone emulation
# a page wider than the viewport makes Chrome zoom the layout out to fit, which
# stretches that area in both directions: a 917px-wide page on a 375px phone
# reports a content size of 917x1984 for a document 147px tall. The layout
# itself is still done at the emulated width — documentElement.clientWidth is
# still 375 — so the honest numbers are the ones measured here, from the box
# the content really occupies.
DOC_SIZE_JS = """
(function () {
  var doc = document.documentElement, body = document.body;
  // The box the layout was done in, which is *not* window.innerWidth: Chrome
  // widens that to the widest thing on the page under phone emulation.
  var viewport = doc.clientWidth || window.innerWidth;
  var width = Math.max(doc.scrollWidth, body ? body.scrollWidth : 0, viewport);
  var bottom = 0;
  if (body) {
    var rect = body.getBoundingClientRect();
    var style = window.getComputedStyle(body);
    bottom = rect.top + window.scrollY
             + Math.max(rect.height, body.scrollHeight)
             + (parseFloat(style.marginBottom) || 0);
  } else if (doc && doc.getBoundingClientRect) {
    // An SVG or XML document has no body at all. The root element's own box is
    // the document then, and it is still the honest height: a 1200x120 logo
    // measures 120 here and 2599 in the metrics.
    var root = doc.getBoundingClientRect();
    bottom = root.top + window.scrollY + root.height;
  }
  return JSON.stringify({width: Math.ceil(width), height: Math.ceil(bottom),
                         viewport: Math.ceil(viewport)});
})()
"""

# Two frames and a beat: long enough for a reveal to land and for layout to
# stop moving, short enough that a gate over a dozen pages is still quick.
# Waiting on the page's own frames rather than on the clock also means the
# events Chrome sends meanwhile — a thrown exception — are read on the way.
SETTLE_JS_WAIT = (
    "new Promise(function (done) {"
    " requestAnimationFrame(function () {"
    " requestAnimationFrame(function () { setTimeout(done, %d); }); }); })" % SETTLE_MS
)


def settle(page: Any) -> None:
    """Let the page finish moving before anything is measured or shot."""
    page.browser.drain(0.02)
    try:
        page.evaluate(SETTLE_JS_WAIT, timeout=10)
    except SetupError:
        # A page whose frame callbacks never run is still worth measuring, and
        # saying "duet could not run" about one would be a lie about the page.
        pass


def fault_line(width: int, rule: str, detail: str) -> str:
    """One fault, one line, the width first. Stable enough to grep and to test."""
    return "%d: %s: %s" % (width, rule, detail)


def viewport_for(width: int) -> Tuple[int, int, bool]:
    """Viewport size and whether to emulate a phone, for a requested width."""
    if width < PHONE_MAX_WIDTH:
        return width, PHONE_HEIGHT, True
    return width, DESKTOP_HEIGHT, False


def parse_widths(text: str) -> List[int]:
    widths: List[int] = []
    for part in str(text).replace(" ", "").split(","):
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            raise SetupError("--width takes pixel numbers, e.g. 1440,375 — not %r" % part)
        if value < 120 or value > 6000:
            raise SetupError("--width %d is not a viewport anyone has; use 120-6000" % value)
        if value not in widths:
            widths.append(value)
    if not widths:
        raise SetupError("--width needs at least one width, e.g. --width 1440,375")
    return widths


def resolve_target(target: str, root: str = ".") -> str:
    """A file path or a URL, as a URL Chrome will open. Raises SetupError."""
    target = (target or "").strip()
    if not target:
        raise SetupError("which page? give a file or a URL, e.g. duet page check index.html")
    if re.match(r"^(https?|file|about|data):", target):
        return target
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = Path(root).expanduser().resolve() / path
    if not path.is_file():
        raise SetupError("no page at %s" % target)
    return path.resolve().as_uri()


def render(page: Any, url: str, width: int, rules: Rules) -> Dict[str, Any]:
    """Load the page at this width, let it settle, and measure it once.

    `page` is a duet.chrome.PageTarget. Everything that could differ between
    widths — the load itself, the scripts that ran, what the layout came out
    as — is redone per width rather than measured once and reported twice.
    """
    viewport_width, height, mobile = viewport_for(width)
    page.call("Runtime.enable")
    page.call("Page.enable")
    page.emulate(viewport_width, height, mobile)
    page.call("Page.addScriptToEvaluateOnNewDocument", {"source": SETTLE_JS})
    page.call("Page.navigate", {"url": url})
    # The same second chance a slow protocol answer gets: on a loaded machine a
    # load that is merely late is not a page that never loads.
    longer = LOAD_TIMEOUT * SLOW_RETRY_FACTOR
    if (page.browser.wait_for("Page.loadEventFired", page.session_id, timeout=LOAD_TIMEOUT) is None
            and page.browser.wait_for("Page.loadEventFired", page.session_id, timeout=longer) is None):
        raise SetupError("%s did not finish loading within %.0fs" % (url, LOAD_TIMEOUT + longer))
    settle(page)
    page.evaluate(SCROLL_THROUGH_JS)
    settle(page)
    page.evaluate("window.scrollTo(0, 0)")
    settle(page)
    raw = page.evaluate("(%s)(%s)" % (PROBE_JS.strip(), json.dumps(rules.payload())))
    if not isinstance(raw, str):
        raise SetupError("the page probe returned %s instead of a result"
                         % type(raw).__name__)
    try:
        measured = json.loads(raw)
    except ValueError as exc:
        raise SetupError("the page probe returned something unreadable: %s" % exc)
    measured["exceptions"] = page.exceptions()
    return measured


def faults_for_width(measured: Dict[str, Any], width: int, rules: Rules) -> List[str]:
    """Every fault line for one width, in a fixed order, without duplicates.

    Split out from the rendering so the whole judgement is testable without a
    browser: what the page measured is data, and this turns it into the lines a
    session reads.
    """
    lines: List[str] = []

    def emit(rule: str, detail: str) -> None:
        line = fault_line(width, rule, detail)
        if line not in lines:
            lines.append(line)

    for message in measured.get("exceptions") or []:
        emit("script-error", str(message))
    for fault in measured.get("faults") or []:
        if not isinstance(fault, dict):
            continue
        emit(str(fault.get("rule") or "fault"), str(fault.get("detail") or ""))
    text = measured.get("text") or ""
    for pattern in rules.forbid_text:
        # Case-insensitively: the page says "Coming Soon" and the rule says
        # "coming soon", and a rule that quietly missed that would be worse than
        # no rule. `(?-i:...)` is there for the cases where the case is the point.
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            emit("forbidden-text", "the page says %r, which %s forbids (/%s/)"
                 % (match.group(0)[:60], rules.source or PAGE_RULES, pattern))
    return lines


def check_page(target: str, widths: Sequence[int], rules: Optional[Rules] = None,
               allow_network: bool = False, browser: Optional[Browser] = None,
               root: str = ".") -> List[str]:
    """Render the page at each width and return every fault line.

    Pass `browser` to reuse one Chrome across several pages — which is what the
    integration tests do, because starting Chrome costs more than every check
    in this file put together.
    """
    rules = rules or Rules()
    url = resolve_target(target, root)
    own = browser is None
    if own:
        browser = Browser(allow_network=allow_network)
    lines: List[str] = []
    try:
        for width in widths:
            page = browser.new_page()
            try:
                measured = render(page, url, width, rules)
                lines.extend(faults_for_width(measured, width, rules))
            finally:
                page.close()
                browser.forget_events()
    finally:
        if own:
            browser.close()
    return lines


def shoot_page(target: str, widths: Sequence[int], out_dir: str,
               allow_network: bool = False, browser: Optional[Browser] = None,
               root: str = ".") -> List[str]:
    """One full-page PNG per width. Returns the paths written, in order."""
    url = resolve_target(target, root)
    directory = Path(out_dir).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SetupError("could not write to %s: %s" % (out_dir, exc))
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(target).stem or "page").strip("-") or "page"
    own = browser is None
    if own:
        browser = Browser(allow_network=allow_network)
    written: List[str] = []
    try:
        for width in widths:
            page = browser.new_page()
            try:
                written.append(_shoot(page, url, width, directory, stem))
            finally:
                page.close()
                browser.forget_events()
    finally:
        if own:
            browser.close()
    return written


def document_size(page: Any) -> Tuple[int, int, int]:
    """(document width, document height, layout viewport width), in CSS pixels.

    Measured on the page itself rather than read off Page.getLayoutMetrics; see
    DOC_SIZE_JS for why the two disagree on a page that overflows sideways.
    """
    raw = page.evaluate(DOC_SIZE_JS)
    try:
        measured = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SetupError("could not measure the page: %s" % exc)
    return (int(measured.get("width") or 0), int(measured.get("height") or 0),
            int(measured.get("viewport") or 0))


def overflows(doc_width: int, layout_width: int) -> bool:
    """Does the document run past the box it was laid out in?

    The `overflow` rule's own comparison, 1px tolerance included (see PROBE_JS),
    so a shot is widened exactly when `duet page check` says the page scrolls
    sideways. The tolerance is not a fudge: a page with no viewport meta is laid
    out at Chrome's 980px desktop fallback on a 375px phone and measures 981
    against a 980px box, and widening the shot for that rounding would change
    every such page's image while showing nothing.
    """
    return doc_width > layout_width + 1


def shot_box(doc_width: int, full_height: int, layout_width: int,
             viewport_width: int) -> Tuple[int, int]:
    """The clip for one shot: how wide and how tall the PNG should be.

    A page that fits is shot exactly as it always was — the viewport's width,
    and the height Chrome reports — so nothing changes for the pages that are
    fine. A page wider than the box it was laid out in is shot at its own full
    width, because that overflow is the whole reason to look at it.

    "Wider" is `overflows` above, which is the `overflow` rule's comparison.

    Both dimensions stop at MAX_SHOT_HEIGHT: that is Chrome's canvas limit, not
    a fact about height, and a shot clipped and said so beats a call that fails.
    """
    if overflows(doc_width, layout_width):
        return min(doc_width, MAX_SHOT_HEIGHT), max(min(full_height, MAX_SHOT_HEIGHT), 1)
    return viewport_width, min(full_height, MAX_SHOT_HEIGHT)


def _shoot(page: Any, url: str, width: int, directory: Path, stem: str) -> str:
    viewport_width, height, mobile = viewport_for(width)
    page.call("Page.enable")
    page.emulate(viewport_width, height, mobile)
    page.call("Page.addScriptToEvaluateOnNewDocument", {"source": SETTLE_JS})
    page.call("Page.navigate", {"url": url})
    # The same second chance a slow protocol answer gets: on a loaded machine a
    # load that is merely late is not a page that never loads.
    longer = LOAD_TIMEOUT * SLOW_RETRY_FACTOR
    if (page.browser.wait_for("Page.loadEventFired", page.session_id, timeout=LOAD_TIMEOUT) is None
            and page.browser.wait_for("Page.loadEventFired", page.session_id, timeout=longer) is None):
        raise SetupError("%s did not finish loading within %.0fs" % (url, LOAD_TIMEOUT + longer))
    settle(page)
    page.evaluate(SCROLL_THROUGH_JS)
    settle(page)
    page.evaluate("window.scrollTo(0, 0)")
    settle(page)
    doc_width, doc_height, measured_viewport = document_size(page)
    metrics = page.call("Page.getLayoutMetrics")
    content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
    metrics_height = int(content.get("height") or height)
    # The box the layout was really done in, as the page itself reports it —
    # narrower than the viewport when a classic scrollbar takes a slice. The
    # requested width is the fallback for a page too broken to measure.
    layout_width = measured_viewport or viewport_width
    wide = overflows(doc_width, layout_width)
    # The document's own height where it is the honest one; Chrome's where it
    # is the only one there is, for a document with no body to measure.
    full_height = doc_height if (wide and doc_height > 0) else metrics_height
    shot_width, shot_height = shot_box(doc_width, full_height, layout_width, viewport_width)
    data = page.call("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": True,
        "clip": {"x": 0, "y": 0, "width": shot_width, "height": shot_height, "scale": 1},
    }, timeout=60)
    raw = base64.b64decode(data.get("data") or "")
    if not raw:
        raise SetupError("Chrome returned an empty screenshot for %dpx" % width)
    path = directory / ("%s-%dw.png" % (stem, width))
    try:
        path.write_bytes(raw)
    except OSError as exc:
        raise SetupError("could not write %s: %s" % (path, exc))
    if wide and shot_width < doc_width:
        print(ui.dim("   note  %s is %dpx wide; the shot stops at %dpx, which is as far "
                     "as Chrome will paint" % (path.name, doc_width, shot_width)))
    elif wide:
        # Said next to the path because the image is not the width that was
        # asked for, and an agent looking at it should know why.
        print(ui.dim("   note  %s — page is %dpx wide at %dpx, so the image is too"
                     % (path.name, doc_width, width)))
    if shot_height < full_height:
        print(ui.dim("   note  %s is %dpx tall; the shot stops at %dpx, which is as far "
                     "as Chrome will paint" % (path.name, full_height, shot_height)))
    return str(path)


# --------------------------------------------------------------------------
# Being part of a session, not only a command.
SHOT_HINT = """\
This gate renders the page in a real browser, so you can see it too:

    %s

writes one full-page PNG per width and prints the paths. Open them and look.
A page passes every rule in the list and still looks broken; reading the CSS is
not the same as seeing what it rendered, and neither of you has any business
signing off on a page you have only read."""

_SHELL_OPERATORS = ("&&", "||", ";", "|")


def shot_command_for(gate: str) -> str:
    """The `duet page shot` that matches this gate, or "" if it is not one.

    Same executable, same page, same widths — so a session whose gate is a page
    check can look at exactly what the gate looked at. --rules is dropped
    because `shot` has no such flag; --allow-network is kept because a page that
    needs its own origin needs it to be shot as well.
    """
    gate = (gate or "").strip()
    if not gate or any(op in gate for op in _SHELL_OPERATORS):
        return ""
    try:
        parts = shlex.split(gate)
    except ValueError:
        return ""
    if "page" not in parts:
        return ""
    index = parts.index("page")
    if index + 1 >= len(parts) or parts[index + 1] != "check":
        return ""
    out = parts[:index + 1] + ["shot"]
    skip = False
    for token in parts[index + 2:]:
        if skip:
            skip = False
            continue
        if token == "--rules":
            skip = True
            continue
        if token.startswith("--rules="):
            continue
        out.append(token)
    return " ".join(shlex.quote(token) for token in out)


# --------------------------------------------------------------------------
def _widths_from(args: Any) -> List[int]:
    given = getattr(args, "width", "") or ""
    if not str(given).strip():
        return list(DEFAULT_WIDTHS)
    return parse_widths(given)


def _target_from(args: Any, rules: Rules) -> str:
    target = (getattr(args, "page", "") or "").strip()
    if target:
        return target
    if rules.page:
        return rules.page
    raise SetupError(
        "which page? give a file or a URL — duet page %s index.html — or put "
        "\"page\" in %s" % (getattr(args, "page_command", "check"), PAGE_RULES))


def _setup_failed(exc: SetupError) -> int:
    print(ui.red("✗ ") + str(exc), file=sys.stderr)
    return 3


def check_cmd(args: Any) -> int:
    """`duet page check` — exit 0 clean, 1 page faults, 3 could not run."""
    problem = unsupported_platform()
    if problem:
        return _setup_failed(SetupError(problem))
    root = str(Path(getattr(args, "root", ".") or ".").expanduser().resolve())
    try:
        rules = load_rules(getattr(args, "rules", None), root=root)
        widths = _widths_from(args)
        target = _target_from(args, rules)
        lines = check_page(target, widths, rules,
                           allow_network=bool(getattr(args, "allow_network", False)),
                           root=root)
    except SetupError as exc:
        return _setup_failed(exc)
    for line in lines:
        print(line)
    if lines:
        print(ui.red("✗ ") + "%d fault%s across %s"
              % (len(lines), "" if len(lines) == 1 else "s",
                 ", ".join("%dpx" % w for w in widths)), file=sys.stderr)
        return 1
    print(ui.green("✓ ") + "%s is clean at %s%s"
          % (target, ", ".join("%dpx" % w for w in widths),
             " (+ %s)" % rules.source if rules else ""))
    return 0


def shot_cmd(args: Any) -> int:
    """`duet page shot` — write the PNGs, print their paths."""
    problem = unsupported_platform()
    if problem:
        return _setup_failed(SetupError(problem))
    root = str(Path(getattr(args, "root", ".") or ".").expanduser().resolve())
    out = getattr(args, "out", "") or os.path.join(root, SHOT_DIR)
    try:
        rules = load_rules(getattr(args, "rules", None), root=root)
        widths = _widths_from(args)
        target = _target_from(args, rules)
        paths = shoot_page(target, widths, out,
                           allow_network=bool(getattr(args, "allow_network", False)),
                           root=root)
    except SetupError as exc:
        return _setup_failed(exc)
    for path in paths:
        print(path)
    return 0


def run(args: Any) -> int:
    """`duet page` with no subcommand: say what it does rather than nothing."""
    command = getattr(args, "page_command", "") or ""
    if command == "check":
        return check_cmd(args)
    if command == "shot":
        return shot_cmd(args)
    print("usage: duet page check <file-or-url> [--width 1440,375] [--rules %s]" % PAGE_RULES)
    print("       duet page shot  <file-or-url> [--width 1440,375] [--out DIR]")
    print()
    print(RULES_DOC)
    return 2
