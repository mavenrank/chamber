"""Chamber's internal clipboard.

The point of this thing is that copied text never enters the model's context — it
goes page → buffer → page. These tests pin that property, plus the label handling
the paste action depends on.
"""

from __future__ import annotations

from chamber.clipboard import Clip, Clipboard

TITLE = (
    "ASUS TUF A15, AMD Ryzen 7 170, RTX 3050-4GB, 16GB RAM (Upgradeable), 512GB SSD, "
    'FHD, 15.6"(39.6 cm), Windows 11 Home, Graphite Black, 2.3 Kg, FA506NCQ-HN006W'
)


class TestClip:
    def test_preview_is_short_and_single_line(self):
        clip = Clip(text="a\n  b\tc   d")
        assert clip.preview() == "a b c d"

    def test_preview_truncates_long_text(self):
        clip = Clip(text=TITLE)
        preview = clip.preview(40)
        assert len(preview) <= 40
        assert preview.endswith("…")

    def test_preview_never_leaks_the_whole_thing(self):
        # This is the saving: the model is told what it copied, not the content.
        clip = Clip(text=TITLE)
        assert len(clip.preview()) < len(clip.text)


class TestClipboard:
    def test_add_and_render_in_order(self):
        cb = Clipboard()
        cb.add("first")
        cb.add("second")
        assert len(cb) == 2
        assert cb.render(list(cb)) == "first\nsecond"

    def test_custom_separator(self):
        cb = Clipboard()
        cb.add("a")
        cb.add("b")
        assert cb.render(list(cb), " | ") == "a | b"

    def test_text_is_preserved_exactly(self):
        # Character-exactness is the whole reason to copy rather than transcribe.
        cb = Clipboard()
        cb.add(TITLE, label="asus")
        assert cb.clips[0].text == TITLE

    def test_relabelling_replaces_rather_than_duplicates(self):
        # Re-copying after a correction must not leave both versions to be pasted.
        cb = Clipboard()
        cb.add("wrong price", label="price")
        cb.add("right price", label="price")
        assert len(cb) == 1
        assert cb.clips[0].text == "right price"

    def test_unlabelled_clips_accumulate(self):
        cb = Clipboard()
        cb.add("one")
        cb.add("two")
        assert len(cb) == 2

    def test_select_all_when_no_labels_given(self):
        cb = Clipboard()
        cb.add("a", label="x")
        cb.add("b")
        chosen, missing = cb.select([])
        assert len(chosen) == 2
        assert missing == []

    def test_select_by_label(self):
        cb = Clipboard()
        cb.add("a", label="x")
        cb.add("b", label="y")
        chosen, missing = cb.select(["y"])
        assert [c.text for c in chosen] == ["b"]
        assert missing == []

    def test_unknown_labels_are_reported_not_dropped(self):
        # Silently pasting nothing would leave the model with no idea what it got
        # wrong; naming the invented label is what lets it correct itself.
        cb = Clipboard()
        cb.add("a", label="x")
        chosen, missing = cb.select(["x", "nope"])
        assert [c.text for c in chosen] == ["a"]
        assert missing == ["nope"]

    def test_clear_reports_how_many_went(self):
        cb = Clipboard()
        cb.add("a")
        cb.add("b")
        assert cb.clear() == 2
        assert not cb

    def test_summary_shows_labels_and_sizes_not_content(self):
        cb = Clipboard()
        cb.add(TITLE, label="asus")
        line = cb.summary()[0]
        assert "asus" in line
        assert str(len(TITLE)) in line
        assert TITLE not in line

    def test_source_url_is_recorded(self):
        cb = Clipboard()
        cb.add("x", source_url="https://amazon.in/dp/B0")
        assert cb.clips[0].source_url == "https://amazon.in/dp/B0"

    def test_empty_clipboard_is_falsy(self):
        assert not Clipboard()
        assert Clipboard(clips=[Clip(text="a")])
