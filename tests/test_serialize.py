"""What the model reads.

The serializer is behaviour, not formatting: an omission here becomes a wasted step
or a confidently wrong answer. These tests pin the properties that matter rather
than the exact layout, so the wording can be tuned without breaking them.
"""

from __future__ import annotations

import pytest

from chamber.dom.model import Element, Scrollable, Snapshot, Viewport
from chamber.dom.serialize import fingerprint, render, render_compact


def el(ref: str, role: str = "button", name: str = "Go", **kw) -> Element:
    defaults = dict(
        tag=role, box=(10, 20, 80, 30), point=(50, 35), path=[{"kind": "css", "sel": "button"}]
    )
    defaults.update(kw)
    return Element(ref=ref, role=role, name=name, **defaults)


def snap(elements: list[Element], **kw) -> Snapshot:
    defaults = dict(
        url="https://shop.example/search",
        title="Search — Shop",
        viewport=Viewport(w=1440, h=900, scroll_x=0, scroll_y=0, doc_w=1440, doc_h=900),
        elements=elements,
        scrollables=[],
        content="",
    )
    defaults.update(kw)
    return Snapshot(**defaults)


class TestIdentity:
    def test_shows_url_and_title(self):
        out = render(snap([el("e1")]))
        assert "https://shop.example/search" in out
        assert "Search — Shop" in out

    def test_shows_how_much_page_is_below(self):
        s = snap([el("e1")], viewport=Viewport(1440, 900, 0, 0, 1440, 4000))
        out = render(s)
        assert "more below" in out

    def test_says_when_the_whole_page_fits(self):
        assert "whole page fits" in render(snap([el("e1")]))

    def test_flags_a_page_still_loading(self):
        out = render(snap([el("e1")], ready_state="loading"))
        assert "still loading" in out


class TestControls:
    def test_lists_refs_and_labels(self):
        out = render(snap([el("e1", "button", "Add to cart")]))
        assert "[e1]" in out
        assert "Add to cart" in out

    def test_shows_an_example_of_how_to_act(self):
        # A model that has never seen the format needs one worked example inline.
        out = render(snap([el("e1")]))
        assert '"action": "click"' in out

    def test_separates_offscreen_controls(self):
        s = snap([el("e1"), el("e2", name="Below", in_viewport=False)])
        out = render(s)
        assert "on screen (1)" in out
        assert "off screen (1)" in out
        assert "scroll to reach" in out

    def test_marks_disabled(self):
        assert "DISABLED" in render(snap([el("e1", disabled=True)]))

    def test_marks_occluded_with_the_blocker(self):
        out = render(snap([el("e1", occluded=True, occluded_by="div#cookie-banner")]))
        assert "covered" in out
        assert "div#cookie-banner" in out

    def test_shows_input_state(self):
        out = render(
            snap([el("e1", "textbox", "Search", input_type="search", value="mouse", tag="input")])
        )
        assert 'value="mouse"' in out

    def test_shows_select_options(self):
        out = render(
            snap([el("e1", "combobox", "Sort", options=["Relevance", "Price"], selected="Price")])
        )
        assert "Relevance" in out
        assert "Price" in out

    def test_shows_link_targets(self):
        out = render(snap([el("e1", "link", "Deals", href="/deals")]))
        assert "/deals" in out

    def test_marks_icon_sized_controls(self):
        # On a DuckDuckGo results page the article headline is a 628×26 link and
        # the "Search domain dev.to" favicon beside it is 32×32. Both are links,
        # both are labelled; without the size cue a model clicks the icon and lands
        # on a filtered search instead of the article. Observed, then fixed.
        icon = el("e1", "link", "Search domain dev.to", box=(10, 20, 32, 32))
        headline = el("e2", "link", "uBlock Origin in Chrome", box=(10, 60, 628, 26))
        out = render(snap([icon, headline]))
        icon_line = next(line for line in out.splitlines() if "[e1]" in line)
        headline_line = next(line for line in out.splitlines() if "[e2]" in line)
        assert "(icon)" in icon_line
        assert "(icon)" not in headline_line

    def test_icons_are_marked_not_hidden(self):
        # A close button is 32×32 too, and is often exactly what you want.
        out = render(snap([el("e1", "button", "Close", box=(10, 20, 32, 32))]))
        assert "[e1]" in out
        assert "Close" in out

    def test_empty_page_says_why(self):
        out = render(snap([]))
        assert "none" in out.lower()
        assert "iframe" in out.lower() or "rendering" in out.lower()


class TestHonesty:
    """A model that thinks it saw everything reports absence with confidence."""

    def test_announces_dropped_controls(self):
        s = snap([el("e1")], stats={"dropped": 42})
        out = render(s)
        assert "42" in out
        assert "omitted" in out

    def test_surfaces_notices(self):
        s = snap([el("e1")], notices=["A captcha is on the page."])
        assert "captcha" in render(s)

    def test_lists_scroll_panels(self):
        s = snap(
            [el("e1")],
            scrollables=[
                Scrollable(label="Results", box=(0, 0, 400, 600), scroll_top=0,
                           scroll_height=3000, client_height=600)
            ],
        )
        out = render(s)
        assert "Results" in out
        assert "independently" in out


class TestTabs:
    def test_hidden_for_a_single_tab(self):
        assert "## Tabs" not in render(snap([el("e1")]))

    def test_listed_when_several_are_open(self):
        s = snap(
            [el("e1")],
            tab_id="t1",
            open_tabs=[
                {"id": "t1", "url": "https://a.example", "title": "A"},
                {"id": "t2", "url": "https://b.example", "title": "B"},
            ],
        )
        out = render(s)
        assert "## Tabs (2 open)" in out
        assert "t2" in out


class TestBudget:
    def test_caps_the_control_list_and_says_so(self):
        s = snap([el(f"e{i}") for i in range(200)])
        out = render(s, max_controls=20)
        assert "not listed" in out
        assert out.count("[e") < 60


class TestFingerprint:
    def test_stable_for_identical_pages(self):
        a, b = snap([el("e1", name="Go")]), snap([el("e9", name="Go")])
        # Refs change every observation; the fingerprint must not.
        assert fingerprint(a) == fingerprint(b)

    def test_changes_when_controls_change(self):
        a = snap([el("e1", name="Go")])
        b = snap([el("e1", name="Cancel")])
        assert fingerprint(a) != fingerprint(b)

    def test_changes_on_navigation(self):
        a = snap([el("e1")])
        b = snap([el("e1")], url="https://shop.example/other")
        assert fingerprint(a) != fingerprint(b)

    @pytest.mark.parametrize("scroll", [0, 50, 99])
    def test_ignores_tiny_scroll_jitter(self, scroll: int):
        base = snap([el("e1")], viewport=Viewport(1440, 900, 0, 0, 1440, 4000))
        moved = snap([el("e1")], viewport=Viewport(1440, 900, 0, scroll, 1440, 4000))
        assert fingerprint(base) == fingerprint(moved)

    def test_notices_a_real_scroll(self):
        base = snap([el("e1")], viewport=Viewport(1440, 900, 0, 0, 1440, 4000))
        moved = snap([el("e1")], viewport=Viewport(1440, 900, 0, 800, 1440, 4000))
        assert fingerprint(base) != fingerprint(moved)


def test_compact_summary_fits_a_line():
    line = render_compact(snap([el("e1"), el("e2", in_viewport=False)]))
    assert "\n" not in line
    assert "1/2" in line
