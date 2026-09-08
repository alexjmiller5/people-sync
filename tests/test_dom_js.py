"""The element predicates are strings of JavaScript. Every other test answers
them from a fake by substring, so this one executes them for real (node, a
stub DOM) - the only way to prove that a hidden duplicate ahead of the real
field is skipped, that the visibility options are actually passed, and that
focus means the element itself, not a descendant."""

import json
import shutil
import subprocess

import pytest

from people_sync.scrape import cdp, login

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node executes the generated predicates")

# A minimal DOM: elements carry a box, an explicit visibility answer, and
# separately what checkVisibility() would answer WITHOUT options (an element
# with opacity:0 is "visible" to the option-less call).
STUB = """
function el(o) {
  return Object.assign({
    w: 100, h: 20, visible: true, visibleWithoutOptions: null, value: "", scrolled: 0,
    getBoundingClientRect() { return {x: 1, y: 2, width: this.w, height: this.h}; },
    checkVisibility(opts) {
      var withOptions = !!(opts && opts.opacityProperty && opts.visibilityProperty);
      if (!withOptions && this.visibleWithoutOptions !== null) return this.visibleWithoutOptions;
      return this.visible;
    },
    scrollIntoView() { this.scrolled++; },
    contains(x) { return x === this.child; },
  }, o);
}
globalThis.document = {
  els: [], activeElement: null,
  querySelectorAll() { return this.els; },
  querySelector() { return this.els[0] || null; },
};
"""


def run(setup: str, expression: str):
    script = f"{STUB}\n{setup}\nconsole.log(JSON.stringify({{v: {expression}}}));"
    out = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["v"]


def test_hidden_duplicate_ahead_of_the_real_field_is_skipped():
    setup = 'document.els = [el({w: 0, h: 0}), el({value: "real"})];'
    assert run(setup, cdp.visible_js("input")) is True
    assert run(setup, cdp.rect_js("input")) == {"x": 1, "y": 2, "width": 100, "height": 20}
    assert run(setup + " document.activeElement = document.els[1];", login._focus_js("input"))
    assert run(setup, login._length_js("input")) == 4


def test_no_visible_match_is_a_miss():
    setup = "document.els = [el({w: 0, h: 0}), el({visible: false})];"
    assert run(setup, cdp.visible_js("input")) is False
    assert run(setup, cdp.rect_js("input")) is None
    assert run(setup, login._focus_js("input")) is False
    assert run(setup, login._empty_js("input")) is False


def test_visibility_is_checked_with_opacity_and_visibility_options():
    """opacity:0 passes an option-less checkVisibility(); the predicates must
    pass the options that make it fail."""
    setup = "document.els = [el({visible: false, visibleWithoutOptions: true})];"
    assert run(setup, cdp.visible_js("input")) is False


def test_focus_means_the_element_itself_not_a_descendant():
    setup = (
        "var child = el({}); document.els = [el({child: child})]; document.activeElement = child;"
    )
    assert run(setup, login._focus_js("input")) is False


def test_rect_scrolls_only_the_element_it_returns():
    setup = "document.els = [el({w: 0, h: 0}), el({})];"
    assert run(setup, cdp.rect_js("input") + " && document.els.map(e => e.scrolled)") == [0, 1]


def test_text_selector_picks_the_first_visible_element_whose_text_starts_with_the_label():
    setup = (
        "var hidden = el({w: 0, innerText: 'Try another way'});"
        " var other = el({innerText: 'Continue'});"
        " var real = el({innerText: 'Try another way\\nsecond line'});"
        " document.els = [hidden, other, real]; document.querySelectorAll = () => document.els;"
    )
    assert run(setup, cdp.visible_js("text=Try another way")) is True
    assert run(
        setup, cdp.rect_js("text=Try another way") + " && document.els.map(e => e.scrolled)"
    ) == [0, 0, 1]
    assert run(setup, cdp.visible_js("text=Nowhere")) is False


def test_text_selector_with_a_tag_filter_skips_same_text_elements_of_other_tags():
    setup = (
        "var link = el({innerText: 'Log in', tagName: 'A'});"
        " var button = el({innerText: 'Log in', tagName: 'BUTTON'});"
        " document.els = [link, button];"
        " document.querySelectorAll = (css) => css === 'button' ? [button] : document.els;"
    )
    assert run(
        setup, cdp.rect_js("text[button]=Log in") + " && document.els.map(e => e.scrolled)"
    ) == [0, 1]
    assert run(setup, cdp.rect_js("text=Log in") + " && document.els.map(e => e.scrolled)") == [
        1,
        0,
    ]


@pytest.mark.parametrize(
    "js",
    [
        pytest.param(getattr(mod, name), id=f"{mod.__name__.split('.')[-1]}.{name}")
        for mod in (
            __import__("people_sync.scrape.instagram", fromlist=["x"]),
            __import__("people_sync.scrape.linkedin", fromlist=["x"]),
            __import__("people_sync.scrape.facebook", fromlist=["x"]),
        )
        for name in ("EXTRACTOR_JS", "LIST_ENTRIES_JS")
        if hasattr(mod, name)
    ],
)
def test_every_extractor_script_parses_as_javascript(js):
    """A stray escape in one of these strings is a SyntaxError the first time
    the page is reached - catch it here."""
    r = subprocess.run(
        [NODE, "-e", "new Function(process.argv[1])", js], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr.strip().splitlines()[-1]
