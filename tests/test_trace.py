"""The trace store — what makes "show me your sources" answerable."""

from __future__ import annotations

import pytest

from chamber.trace.store import canonical_url, open_store


class TestCanonicalUrl:
    """Without this, deduplication finds almost nothing: the same article reached
    from three places carries three different tracking parameters."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://Example.COM/a/b/", "https://example.com/a/b"),
            ("https://www.example.com/a", "https://example.com/a"),
            ("https://example.com/a?utm_source=x&id=7", "https://example.com/a?id=7"),
            ("https://example.com/a?fbclid=abc", "https://example.com/a"),
            ("https://example.com/a#section", "https://example.com/a"),
            ("https://example.com", "https://example.com/"),
        ],
    )
    def test_normalises(self, raw: str, expected: str):
        assert canonical_url(raw) == expected

    def test_keeps_meaningful_query_parameters(self):
        # ?q= and ?page= change which page you are on; dropping them would merge
        # genuinely different results.
        assert canonical_url("https://e.com/s?q=mice&page=2") == "https://e.com/s?q=mice&page=2"

    def test_two_urls_for_the_same_page_agree(self):
        a = canonical_url("https://www.News.com/story/1?utm_campaign=twitter")
        b = canonical_url("https://news.com/story/1/")
        assert a == b


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "t.sqlite") as s:
        s.start_run("run-1", "find the cheapest mouse", profile="test", model="m/x")
        yield s


class TestRecording:
    def test_round_trips_a_run(self, store):
        store.end_run(success=True, summary="Logitech M185 at £12.99", input_tokens=10, output_tokens=5)
        run = store.run("run-1")
        assert run["success"] == 1
        assert "12.99" in run["summary"]
        assert run["input_tokens"] == 10

    def test_steps_and_actions_keep_their_order(self, store):
        for n in (1, 2, 3):
            store.record_step(n, thought=f"t{n}", url=f"https://e.com/{n}")
            store.record_action(n, 0, name="click", args={"ref": "e1"}, why="", outcome="ok")
        assert [s["n"] for s in store.steps("run-1")] == [1, 2, 3]
        assert len(store.actions("run-1")) == 3

    def test_records_repairs(self, store):
        store.record_action(
            1, 0, name="click", args={}, why="", outcome="ok",
            repairs=["scrolled it into view"],
        )
        assert "scrolled" in store.timeline("run-1")

    def test_failures_are_kept_not_pruned(self, store):
        # A rejected page is evidence about what the agent saw.
        store.record_step(1, thought="try", url="https://e.com/a")
        store.record_action(1, 0, name="click", args={}, why="", outcome="stale_ref", message="gone")
        assert any(a["outcome"] == "stale_ref" for a in store.actions("run-1"))


class TestSources:
    def test_groups_repeat_visits(self, store):
        store.record_visit("https://e.com/a", step_n=1, title="A")
        store.record_visit("https://e.com/a?utm_source=x", step_n=3, title="A")
        store.record_visit("https://e.com/b", step_n=4, title="B")
        found = store.sources("run-1")
        assert len(found) == 2
        first = next(s for s in found if s["canonical"].endswith("/a"))
        assert first["visits"] == 2

    def test_ordered_by_first_seen(self, store):
        store.record_visit("https://e.com/second", step_n=1)
        store.record_visit("https://e.com/first", step_n=2)
        order = [s["canonical"] for s in store.sources("run-1")]
        assert order[0].endswith("/second")

    def test_used_flag_separates_cited_from_merely_opened(self, store):
        store.record_visit("https://e.com/used", step_n=1)
        store.record_visit("https://e.com/rejected", step_n=2)
        store.mark_used(["https://e.com/used"])
        used = store.sources("run-1", used_only=True)
        assert len(used) == 1
        assert used[0]["canonical"].endswith("/used")
        # The rejected one is still on the record.
        assert len(store.sources("run-1")) == 2

    def test_mark_used_matches_through_canonicalisation(self, store):
        store.record_visit("https://www.e.com/x/", step_n=1)
        store.mark_used(["https://e.com/x?utm_source=chat"])
        assert len(store.sources("run-1", used_only=True)) == 1


class TestTimeline:
    def test_reads_as_a_tree(self, store):
        store.record_step(1, thought="looking at the results", url="https://e.com/s")
        store.record_action(1, 0, name="click", args={"ref": "e2"}, why="open it", outcome="ok",
                            message="clicked link")
        store.record_visit("https://e.com/s", step_n=1, title="Search")
        store.end_run(success=True, summary="done")

        text = store.timeline("run-1")
        assert "step 1" in text
        assert "looking at the results" in text
        assert "click" in text
        assert "sources" in text

    def test_missing_run_says_so(self, store):
        assert "no such run" in store.timeline("nope")
