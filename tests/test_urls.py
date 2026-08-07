"""URL cleaning.

Small module, disproportionate effect: tracking parameters were consuming most of
the content budget on commerce pages, which is how an agent ends up never seeing
the buy box.
"""

from __future__ import annotations

import pytest

from chamber.urls import canonical, shorten, strip_tracking

# The exact link that motivated this, from an Amazon India product page.
AMAZON = (
    "https://www.amazon.in/ASUS-3050-4GB-Upgradeable-Graphite-FA506NCQ-HN010WS/dp/"
    "B0GX4W73SM/ref=dp_prsubs_d_sccl_1/524-5380686-6107114?pd_rd_w=QftBT&"
    "content-id=amzn1.sym.d73752bb-96cd-462e-bb04-0d81f2ec0fd3&"
    "pf_rd_p=d73752bb-96cd-462e-bb04-0d81f2ec0fd3&pf_rd_r=06DGKFXB1EK0E2X5MA1A&"
    "pd_rd_wg=mFoAr&pd_rd_r=2f7e6b30-e0f7-40de-a723-fc7e5a3b7a12&"
    "pd_rd_i=B0GX4W73SM&psc=1"
)


class TestStripTracking:
    def test_the_amazon_case(self):
        out = strip_tracking(AMAZON)
        # ~360 characters of link carrying about 85 of meaning: a 4x reduction,
        # and there are dozens of these on a single product page.
        assert len(AMAZON) > 350
        assert len(out) < 100
        # The product id survives — the URL still resolves to the same page.
        assert "B0GX4W73SM" in out
        assert "amazon.in" in out
        assert "ref=" not in out

    def test_drops_path_embedded_referral_segments(self):
        # Amazon puts the referral trail in the path, where query stripping
        # cannot reach it.
        out = strip_tracking("https://e.com/dp/ABC123/ref=zg_bs_g_1375424031_/524-5380686")
        assert out == "https://e.com/dp/ABC123"

    def test_leaves_ordinary_paths_alone(self):
        url = "https://e.com/blog/2026/why-refactoring-matters"
        assert strip_tracking(url) == url

    @pytest.mark.parametrize(
        "param",
        ["utm_source", "fbclid", "gclid", "pd_rd_w", "pf_rd_p", "content-id", "psc", "ref"],
    )
    def test_drops_known_trackers(self, param: str):
        assert param not in strip_tracking(f"https://e.com/p?{param}=x&id=7")

    def test_keeps_meaningful_parameters(self):
        # ?q= and ?page= change which page you are on.
        out = strip_tracking("https://e.com/s?q=mice&page=2&utm_source=x")
        assert "q=mice" in out
        assert "page=2" in out
        assert "utm_source" not in out

    def test_drops_the_fragment(self):
        assert strip_tracking("https://e.com/a#section") == "https://e.com/a"

    def test_leaves_relative_and_odd_input_alone(self):
        assert strip_tracking("/local/path") == "/local/path"
        assert strip_tracking("") == ""

    def test_preserves_case_and_host_for_display(self):
        # Unlike canonical(), this is for clicking — do not normalise the host.
        assert strip_tracking("https://www.Example.com/A") == "https://www.Example.com/A"


class TestShorten:
    def test_short_urls_pass_through_cleaned(self):
        assert shorten("https://e.com/a?utm_source=x") == "https://e.com/a"

    def test_elides_the_middle_of_a_long_one(self):
        long = "https://e.com/" + "segment/" * 40
        out = shorten(long, limit=80)
        assert len(out) <= 80
        assert "…" in out
        # Host and tail survive — you can still tell what it is.
        assert out.startswith("https://e.com/")

    def test_cleaning_alone_usually_avoids_elision(self):
        assert "…" not in shorten(AMAZON)


class TestCanonical:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://Example.COM/a/b/", "https://example.com/a/b"),
            ("https://www.example.com/a", "https://example.com/a"),
            ("https://example.com/a?utm_source=x&id=7", "https://example.com/a?id=7"),
            ("https://example.com", "https://example.com/"),
        ],
    )
    def test_normalises(self, raw: str, expected: str):
        assert canonical(raw) == expected

    def test_two_routes_to_one_page_agree(self):
        a = canonical("https://www.News.com/story/1?utm_campaign=twitter")
        b = canonical("https://news.com/story/1/")
        assert a == b
