"""Hosts where the overlay stays off the page entirely.

A strict CSP can let the bar's markup into the shadow root while blocking the
stylesheet that makes it a bar, so the contents render as a line of unstyled text
across the top of the page with the layout pushed down to fit. That is worse than
no bar, and it is the *page* that ends up looking broken.

The list is empty by default — the overlay is the point of this project, and
staying off a page is an exception a caller opts into, because which sites behave
this way is a property of the sites rather than of chamber.
"""

from __future__ import annotations

import re
from pathlib import Path

from chamber.config import ChamberConfig
from chamber.overlay import DEFAULT_SKIP_HOSTS, _skips, source

OVERLAY_JS = Path(__file__).resolve().parent.parent / "src" / "chamber" / "overlay" / "overlay.js"

SKIP = ("a.test", "b.test")


class TestHostMatching:
    def test_exact_host_is_skipped(self):
        assert _skips("https://a.test/x", SKIP)

    def test_subdomains_are_skipped(self):
        for url in ("https://www.a.test/x", "https://in.a.test/x", "https://www.b.test/y"):
            assert _skips(url, SKIP), url

    def test_unrelated_hosts_are_not_skipped(self):
        for url in ("https://example.com", "https://www.amazon.in/dp/B0", "about:blank"):
            assert not _skips(url, SKIP), url

    def test_a_host_merely_containing_the_name_is_not_skipped(self):
        """`nota.test` and `a.test.evil.test` must not match — the second is the
        one that matters, since a substring check would hand an attacker's domain
        the same treatment as yours."""
        assert not _skips("https://nota.test/x", SKIP)
        assert not _skips("https://a.test.evil.test/x", SKIP)

    def test_empty_skip_list_skips_nothing(self):
        assert not _skips("https://a.test/x", ())

    def test_malformed_url_does_not_raise(self):
        assert not _skips("not a url", SKIP)


class TestConfig:
    def test_default_is_empty(self):
        """chamber ships domain-free: it knows no sites by name."""
        assert DEFAULT_SKIP_HOSTS == ()
        assert ChamberConfig.from_env().browser.overlay_skip_hosts == ()

    def test_env_sets_the_list(self, monkeypatch):
        monkeypatch.setenv("CHAMBER_OVERLAY_SKIP_HOSTS", "example.com, Foo.test")
        cfg = ChamberConfig.from_env()
        assert cfg.browser.overlay_skip_hosts == ("example.com", "foo.test")

    def test_env_empty_string_means_inject_everywhere(self, monkeypatch):
        monkeypatch.setenv("CHAMBER_OVERLAY_SKIP_HOSTS", "")
        assert ChamberConfig.from_env().browser.overlay_skip_hosts == ()

    def test_caller_can_set_it_directly(self):
        cfg = ChamberConfig.from_env(browser__overlay_skip_hosts=SKIP)
        assert cfg.browser.overlay_skip_hosts == SKIP


class TestScript:
    def test_the_guard_runs_before_anything_is_created(self):
        """The early return has to come before `mount()` can touch the DOM —
        a guard placed after it would still push the page down."""
        text = source()
        guard = text.index("__chamberSkipHosts")
        first_dom_write = min(text.index("document.createElement"), text.index("appendChild"))
        assert guard < first_dom_write

    def test_python_and_javascript_agree_on_matching(self):
        """Both sides implement `host === h || host.endsWith("." + h)`. If one
        drifts, the overlay appears on a page the config says it should not."""
        js = OVERLAY_JS.read_text(encoding="utf-8")
        assert re.search(r'hostname === h \|\| hostname\.endsWith\("\." \+ h\)', js)
