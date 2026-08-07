"""BeautifulSoup content extraction.

Pure functions from HTML to markdown, so these run without a browser and are the
fastest place to catch a regression in what the model reads.
"""

from __future__ import annotations

from chamber.dom.reader import read, read_links

ARTICLE = """
<html>
<head><title>Why Cats Sit In Boxes</title></head>
<body>
  <nav class="site-nav">
    <a href="/">Home</a><a href="/about">About</a><a href="/contact">Contact</a>
    <a href="/a">A</a><a href="/b">B</a><a href="/c">C</a><a href="/d">D</a>
  </nav>
  <div id="cookie-banner">We use cookies. <button>Accept all</button></div>
  <main>
    <article>
      <h1>Why Cats Sit In Boxes</h1>
      <p>Cats seek enclosed spaces because they reduce stress and conserve heat.</p>
      <h2>The thermal argument</h2>
      <p>A cardboard box insulates well, and the thermoneutral zone for a cat is
         higher than most homes are kept.</p>
      <ul><li>Insulation</li><li>Security</li><li>Ambush position</li></ul>
    </article>
  </main>
  <aside class="related-posts">
    <a href="/x">Ten more cat facts</a><a href="/y">Dogs also sit in boxes</a>
  </aside>
  <footer><a href="/privacy">Privacy</a><a href="/terms">Terms</a></footer>
  <script>window.analytics = 1;</script>
</body></html>
"""

PRICES = """
<html><body><main>
<h1>Wireless mice</h1>
<table>
  <tr><th>Model</th><th>Price</th><th>Stock</th></tr>
  <tr><td>Logitech M185</td><td>£12.99</td><td>In stock</td></tr>
  <tr><td>Anker 2.4G</td><td>£9.49</td><td>2 left</td></tr>
</table>
</main></body></html>
"""


class TestBoilerplateRemoval:
    def test_keeps_the_article(self):
        result = read(ARTICLE, base_url="https://cats.example")
        assert "thermal argument" in result.text.lower()
        assert "reduce stress" in result.text

    def test_drops_nav_footer_and_cookie_banner(self):
        text = read(ARTICLE, base_url="https://cats.example").text
        assert "Accept all" not in text
        assert "Privacy" not in text
        assert "Ten more cat facts" not in text

    def test_drops_scripts(self):
        assert "window.analytics" not in read(ARTICLE).text

    def test_reports_where_the_content_came_from(self):
        # Diagnosability: when a model says it cannot see something, the trace has
        # to show which block was chosen.
        result = read(ARTICLE)
        assert result.main_selector
        assert result.chars_in > result.chars_out
        assert result.compression > 0.5


class TestStructure:
    def test_headings_keep_their_level(self):
        text = read(ARTICLE).text
        assert "# Why Cats Sit In Boxes" in text
        assert "## The thermal argument" in text

    def test_lists_become_bullets(self):
        text = read(ARTICLE).text
        assert "- Insulation" in text
        assert "- Ambush position" in text

    def test_headings_are_listed_separately(self):
        result = read(ARTICLE)
        assert "Why Cats Sit In Boxes" in result.headings

    def test_links_survive_as_markdown(self):
        html = '<main><p>See <a href="/deep">the study</a> for detail.</p></main>'
        text = read(html, base_url="https://x.example/a/b").text
        assert "[the study](https://x.example/deep)" in text

    def test_links_can_be_stripped(self):
        html = '<main><p>See <a href="/deep">the study</a>.</p></main>'
        text = read(html, base_url="https://x.example", keep_links=False).text
        assert "the study" in text
        assert "](" not in text


class TestTables:
    """Tables carry the row/column relationship that price comparison depends on."""

    def test_renders_a_markdown_table(self):
        text = read(PRICES).text
        assert "| Model | Price | Stock |" in text
        assert "| Logitech M185 | £12.99 | In stock |" in text

    def test_single_column_table_is_not_forced_into_a_grid(self):
        html = "<main><table><tr><td>only</td></tr><tr><td>one</td></tr></table></main>"
        text = read(html).text
        assert "|---" not in text

    def test_infobox_becomes_key_value_lines_not_a_ragged_grid(self):
        # A Wikipedia infobox: a caption spanning the table, then key/value rows.
        # Forced into a grid this renders as `| Developer | Google | | | | | |`.
        html = """
        <main><table>
          <tr><th colspan="12">Google Chrome</th></tr>
          <tr><td>Developer</td><td>Google</td></tr>
          <tr><td>Licence</td><td>BSD</td></tr>
          <tr><td>Engine</td><td>Blink</td></tr>
        </table></main>
        """
        text = read(html).text
        assert "**Developer**: Google" in text
        assert "**Licence**: BSD" in text
        assert "| | |" not in text
        assert "|---" not in text

    def test_empty_rows_are_dropped(self):
        html = """
        <main><table>
          <tr><th>A</th><th>B</th><th>C</th></tr>
          <tr><td></td><td></td><td></td></tr>
          <tr><td>1</td><td>2</td><td>3</td></tr>
        </table></main>
        """
        lines = [line for line in read(html).text.splitlines() if line.startswith("|")]
        assert len(lines) == 3  # header, separator, one data row

    def test_ragged_rows_do_not_pad_the_grid_with_pipes(self):
        html = """
        <main><table>
          <tr><th colspan="3">Specs</th></tr>
          <tr><td>Weight</td><td>90g</td><td>light</td></tr>
          <tr><td>Battery</td><td>18mo</td><td>AA</td></tr>
          <tr><td>DPI</td><td>1000</td><td>fixed</td></tr>
        </table></main>
        """
        text = read(html).text
        # Three consistent 3-cell rows outvote the caption: a real grid.
        assert "| Weight | 90g | light |" in text
        assert "Specs" in text


class TestBudget:
    def test_truncation_is_announced(self):
        html = "<main>" + "<p>" + ("word " * 60) + "</p>" * 1 + "".join(
            f"<p>paragraph number {i} with a reasonable amount of filler text in it</p>"
            for i in range(200)
        ) + "</main>"
        result = read(html, budget=500)
        assert result.truncated
        # Silent truncation is how an agent concludes something is not on a page.
        assert "truncated" in result.text

    def test_within_budget_is_not_truncated(self):
        result = read(ARTICLE, budget=100_000)
        assert not result.truncated
        assert "truncated" not in result.text


class TestWholePageMode:
    def test_whole_page_keeps_list_heavy_layouts(self):
        # A search results page is mostly links; density scoring would discard the
        # very list the task is about.
        html = """
        <html><body>
          <div class="results">
            <div><a href="/1">First result about mice</a><p>A description here.</p></div>
            <div><a href="/2">Second result about mice</a><p>Another description.</p></div>
          </div>
        </body></html>
        """
        scoped = read(html, whole_page=True, aggressive=False).text
        assert "First result about mice" in scoped
        assert "Second result about mice" in scoped


class TestLinks:
    def test_collects_and_absolutises(self):
        links = read_links(ARTICLE, base_url="https://cats.example/blog/")
        urls = {link["url"] for link in links}
        assert "https://cats.example/about" in urls
        assert any("privacy" in u for u in urls)

    def test_deduplicates(self):
        html = '<a href="/x">one</a><a href="/x">two</a><a href="/y">three</a>'
        links = read_links(html, base_url="https://e.example")
        assert len(links) == 2

    def test_skips_javascript_and_anchors(self):
        html = '<a href="javascript:void(0)">no</a><a href="#top">no</a><a href="/yes">yes</a>'
        links = read_links(html, base_url="https://e.example")
        assert len(links) == 1
        assert links[0]["text"] == "yes"


class TestJunkHeuristicIsFailSafe:
    """A class-name heuristic will eventually match the wrong thing. When it does,
    the failure must be small and visible, not total and silent."""

    def test_junk_token_on_the_html_tag_does_not_wipe_the_page(self):
        # Exactly what Wikipedia ships: the token "sidebar" appears in a feature
        # flag on <html>, and matching it removed the entire document.
        html = """
        <html class="client-nojs vector-feature-language-alert-in-sidebar-enabled">
          <body><main><h1>Real Article</h1>
            <p>This is the content that must survive the stripper.</p>
          </main></body>
        </html>
        """
        text = read(html).text
        assert "Real Article" in text
        assert "must survive" in text

    def test_junk_token_on_body_does_not_wipe_the_page(self):
        html = '<html><body class="page-with-sidebar"><main><p>Kept content here.</p></main></body></html>'
        assert "Kept content here." in read(html).text

    def test_a_wrapper_containing_main_is_never_removed(self):
        html = """
        <html><body>
          <div class="nav-wrapper">
            <main><p>The article body lives inside a badly named wrapper.</p></main>
          </div>
        </body></html>
        """
        assert "badly named wrapper" in read(html).text

    def test_a_huge_junk_match_is_refused(self):
        # 95% of the text sits under a "related" class — that is a misfire, not a
        # recommendations box.
        body = "".join(f"<p>Substantial paragraph number {i} of the actual article.</p>" for i in range(40))
        html = f'<html><body><div class="related-wrap">{body}</div></body></html>'
        text = read(html).text
        assert "Substantial paragraph number 0" in text

    def test_a_genuinely_small_junk_block_is_still_removed(self):
        html = """
        <html><body><main>
          <p>The real article text goes on for a while and carries the meaning.</p>
          <p>A second real paragraph, also carrying meaning and length.</p>
          <div class="newsletter-signup">Subscribe now</div>
        </main></body></html>
        """
        text = read(html).text
        assert "real article text" in text
        assert "Subscribe now" not in text


class TestRobustness:
    def test_empty_html(self):
        assert read("").text == ""

    def test_malformed_html_does_not_raise(self):
        result = read("<html><body><main><p>text<div>unclosed</main>")
        assert "text" in result.text

    def test_no_body(self):
        assert read("<html><head><title>t</title></head></html>").title == "t"
