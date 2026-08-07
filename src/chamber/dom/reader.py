"""HTML → readable markdown, with the page furniture removed.

The model does not need the page; it needs the *content* of the page. A raw
accessibility dump or an `innerText` grab buries the three sentences that matter
under cookie notices, nav rails, "you might also like" carousels and footer link
farms — and every one of those tokens is paid for on every step of the loop.

Three passes, in order:

1. **Strip** what is never content: scripts, styles, SVG, hidden nodes, and
   elements whose class or id says outright what they are (`cookie-banner`,
   `sidebar`, `related-posts`).
2. **Locate** the main block. `<main>`/`<article>` when the page says so; otherwise
   score candidates on text length discounted by *link density* — the classic
   readability signal, and the one that separates an article body from a nav rail,
   because navigation is mostly anchor text and prose is mostly not.
3. **Render** to markdown. Headings keep their level, lists keep their bullets,
   tables become real markdown tables (which matters more than it sounds: price
   comparison and spec sheets are tables, and flattening them to prose destroys
   the row/column relationship the model needs).

Everything here is a pure function of an HTML string, so it is tested against
fixtures in `tests/test_reader.py` without launching a browser.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from chamber.urls import shorten

# Never content, under any circumstances.
_DROP_TAGS = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "canvas", "map", "area",
        "link", "meta", "base", "object", "embed", "applet", "param", "track",
    }
)

# Structural furniture. Dropped only when we are *not* already inside a chosen
# main block — an <aside> inside an article can be a pull quote worth keeping.
_FURNITURE_TAGS = frozenset({"nav", "footer", "aside", "form"})

# Largest share of a page's text one junk-pattern match is allowed to remove.
_MAX_JUNK_SHARE = 0.4

# Smallest share of the page a chosen "main" block may hold before we conclude the
# page has no main article and read the whole layout instead.
_MIN_MAIN_SHARE = 0.3

# Class/id substrings that reliably mean chrome rather than content. Matched on a
# word-ish boundary so `social` does not eat `social-science-article`.
_JUNK_PATTERN = re.compile(
    r"(?:^|[-_\s])(?:"
    r"nav|navbar|navigation|menu|sidebar|side-bar|breadcrumb|"
    r"footer|header-ad|masthead|"
    r"cookie|consent|gdpr|privacy-banner|"
    r"advert|advertisement|ads|adslot|adsense|sponsor|promo|promotion|"
    r"popup|modal|overlay|lightbox|interstitial|paywall|"
    r"newsletter|subscribe|signup-prompt|"
    r"share|sharing|social|follow-us|"
    r"related|recommend|recirc|more-stories|read-next|trending|popular|"
    r"comment|comments|disqus|"
    r"skip-link|screen-reader|visually-hidden|sr-only|"
    r"toolbar|pagination|pager|tag-list|tags|breadcrumbs"
    r")(?:[-_\s]|$)",
    re.I,
)

_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "li", "td", "th", "dd", "dt", "blockquote", "pre"}
)
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# Blocks whose own rendering already includes the text of inline children, so an
# anchor inside one must not be emitted a second time.
_INLINE_OWNERS = _HEADINGS | frozenset(
    {"p", "li", "blockquote", "pre", "td", "th", "dt", "dd", "figcaption", "caption"}
)


@dataclass(slots=True)
class ReadResult:
    """Rendered content plus the numbers behind the decision.

    `main_selector` and `link_density` exist so a bad extraction can be diagnosed
    from a trace without re-running the page — when the model complains it cannot
    see something, the first question is always "what did we actually give it".
    """

    text: str
    title: str
    main_selector: str
    chars_in: int
    chars_out: int
    link_density: float
    truncated: bool
    headings: list[str]

    @property
    def compression(self) -> float:
        return 1 - (self.chars_out / self.chars_in) if self.chars_in else 0.0


# ------------------------------------------------------------------ stripping


# Never junk-matched, whatever their classes say. Wikipedia ships
# `<html class="… vector-feature-language-alert-in-sidebar-enabled …">`, which
# contains the token "sidebar" — matching that removed the entire document.
# Structural elements are containers for content by definition; their class names
# describe the page, not their own role.
_STRUCTURAL = frozenset({"html", "body", "head", "main", "article"})


def _looks_like_junk(tag: Tag) -> bool:
    if tag.name in _STRUCTURAL:
        return False
    ident = " ".join(
        filter(
            None,
            [
                " ".join(tag.get("class") or []),
                tag.get("id") or "",
                tag.get("data-testid") or "",
                tag.get("role") or "",
            ],
        )
    )
    if not ident:
        return False
    if tag.get("role") in ("navigation", "banner", "complementary", "contentinfo", "search"):
        return True
    return bool(_JUNK_PATTERN.search(ident))


def _strip(soup: BeautifulSoup, *, aggressive: bool) -> None:
    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()

    # Author-hidden content. `hidden`, `aria-hidden` and inline display:none are
    # the three that show up constantly; anything subtler needs computed style,
    # which is the in-page script's job, not ours.
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        # An aria-hidden wrapper around real content happens; only drop it if it
        # is small enough to be decoration.
        if len(tag.get_text(strip=True)) < 200:
            tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)):
        tag.decompose()

    if aggressive:
        for tag in soup.find_all(list(_FURNITURE_TAGS)):
            tag.decompose()

        # A class-name heuristic will eventually match something it should not, and
        # when it does the damage is total and silent. The guard: no single junk
        # match may remove most of the page. Real chrome — a nav rail, a related-
        # posts box — is a small fraction of the text; anything that is not is a
        # misfire, and keeping some boilerplate costs far less than losing the
        # article.
        total = len((soup.body or soup).get_text(" ", strip=True))
        budget = max(int(total * _MAX_JUNK_SHARE), 400)
        for tag in soup.find_all(True):
            # decompose() detaches descendants too, so a tag reached after its
            # parent was removed has no parent left — skip it rather than crash.
            if not isinstance(tag, Tag) or tag.parent is None:
                continue
            if not _looks_like_junk(tag):
                continue
            if tag.find(["main", "article"]) is not None:
                continue
            if len(tag.get_text(" ", strip=True)) > budget:
                continue
            tag.decompose()


# ------------------------------------------------------- main-block detection


def _link_density(tag: Tag) -> float:
    """Fraction of visible text that sits inside anchors.

    Above ~0.5 and you are almost certainly looking at navigation, a link farm, or
    a tag cloud rather than prose.
    """
    text_len = len(tag.get_text(" ", strip=True))
    if text_len == 0:
        return 1.0
    link_len = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
    return min(link_len / text_len, 1.0)


def _score(tag: Tag) -> float:
    text = tag.get_text(" ", strip=True)
    n = len(text)
    if n < 120:
        return 0.0
    density = _link_density(tag)
    score = n * (1.0 - density)
    # Paragraph count is a strong prose signal; a div of 4000 characters with no
    # <p> is usually a script blob or a comma-joined data dump.
    score *= 1 + min(len(tag.find_all("p")), 20) * 0.08
    if tag.name in ("article", "main"):
        score *= 1.5
    if _looks_like_junk(tag):
        score *= 0.2
    return score


def _find_main(soup: BeautifulSoup) -> tuple[Tag, str]:
    body = soup.body or soup
    body_len = len(body.get_text(" ", strip=True))

    def accept(candidate: Tag, label: str) -> tuple[Tag, str] | None:
        """Take the candidate unless it is too small a slice of the page.

        Main-content detection assumes the page *has* a main article. On a search
        results page, a dashboard or a product grid it does not, and every
        candidate is one card out of thirty — DuckDuckGo's `<article>` is a single
        result, and scoping to it threw away the other nine. When the winner holds
        only a sliver of the page's text, the page is a list and the whole layout
        is the content.
        """
        chosen_len = len(candidate.get_text(" ", strip=True))
        if body_len > 400 and chosen_len < body_len * _MIN_MAIN_SHARE:
            return None
        return candidate, label

    for selector in ("main", "[role=main]", "article", "#content", "#main", ".post-content"):
        found = soup.select_one(selector)
        if found and len(found.get_text(strip=True)) > 200:
            taken = accept(found, selector)
            if taken:
                return taken

    best: Tag | None = None
    best_score = 0.0
    for tag in soup.find_all(["div", "section", "article", "td"]):
        s = _score(tag)
        if s > best_score:
            # Prefer the innermost block of a nested pair with similar scores; the
            # outer one drags in siblings that are not content.
            best, best_score = tag, s

    if best is not None:
        taken = accept(best, f"<{best.name}> (density-scored)")
        if taken:
            return taken
        return body, "body (main block was only a slice — page reads as a list)"

    return body, "body (fallback)"


# ------------------------------------------------------------------ rendering


def _inline(tag: Tag, base_url: str, *, keep_links: bool) -> str:
    """Render inline content, preserving links as markdown."""
    parts: list[str] = []
    for node in tag.children:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif isinstance(node, Tag):
            if node.name == "a" and keep_links:
                label = node.get_text(" ", strip=True)
                href = node.get("href", "")
                if label and href and not href.startswith(("javascript:", "#")):
                    parts.append(f"[{label}]({shorten(urljoin(base_url, href))})")
                elif label:
                    parts.append(label)
            elif node.name in ("code", "kbd", "samp"):
                inner = node.get_text(" ", strip=True)
                parts.append(f"`{inner}`" if inner else "")
            elif node.name in ("strong", "b"):
                inner = _inline(node, base_url, keep_links=keep_links).strip()
                parts.append(f"**{inner}**" if inner else "")
            elif node.name in ("em", "i"):
                inner = _inline(node, base_url, keep_links=keep_links).strip()
                parts.append(f"*{inner}*" if inner else "")
            elif node.name == "br":
                parts.append("\n")
            elif node.name == "img":
                # Alt text is worth having but is sometimes a full paragraph
                # describing a screenshot; cap it so one image cannot outweigh the
                # prose around it.
                alt = (node.get("alt") or "").strip()
                if alt:
                    parts.append(f"![{alt[:100]}]")
            else:
                parts.append(_inline(node, base_url, keep_links=keep_links))
    return re.sub(r"[ \t]+", " ", "".join(parts))


def _render_table(table: Tag, base_url: str) -> str:
    """A markdown table when it is data; key/value lines when it is layout.

    Worth the code: comparison shopping, spec sheets and pricing pages are tables,
    and prose-flattening them loses which number belongs to which row — exactly
    the relationship the task depends on.

    But a great many "tables" are layout, not data. A Wikipedia infobox is a
    caption row spanning twelve columns, then a stack of two-cell key/value rows;
    forcing that into a grid produces `| Developer | Google | | | | | | | | | |`
    for every row and spends most of the budget on pipes. The tell is row shape:
    a data table has rows of consistent width, a layout table does not.
    """
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        rendered = [_inline(c, base_url, keep_links=False).strip() for c in cells]
        if any(rendered):
            rows.append(rendered)
    if not rows:
        return ""

    # The modal row width is the table's real shape; colspan/rowspan noise shows
    # up as rows that do not match it.
    widths = Counter(len(r) for r in rows)
    modal_width, modal_count = widths.most_common(1)[0]
    consistent = modal_count / len(rows)

    if modal_width <= 2 or consistent < 0.6 or len(rows) < 2:
        return _render_table_as_lines(rows)

    grid = [r for r in rows if len(r) == modal_width]
    odd = [r for r in rows if len(r) != modal_width]

    header, body = grid[0], grid[1:]
    out: list[str] = []
    if odd:
        # Caption and section rows, kept above the grid rather than discarded.
        out.append(_render_table_as_lines(odd[:3]))
    out.append("| " + " | ".join(c or " " for c in header) + " |")
    out.append("|" + "---|" * modal_width)
    for r in body[:60]:
        out.append("| " + " | ".join((c or " ").replace("|", "\\|") for c in r) + " |")
    if len(body) > 60:
        out.append(f"_… {len(body) - 60} more rows_")
    return "\n".join(p for p in out if p)


def _render_table_as_lines(rows: list[list[str]]) -> str:
    """Key/value rendering for layout tables — infoboxes, spec panels, HN rows."""
    out: list[str] = []
    for row in rows[:80]:
        cells = [c for c in row if c]
        if not cells:
            continue
        if len(cells) == 1:
            out.append(cells[0])
        elif len(cells) == 2:
            out.append(f"- **{cells[0]}**: {cells[1]}")
        else:
            out.append("- " + " · ".join(cells))
    if len(rows) > 80:
        out.append(f"_… {len(rows) - 80} more rows_")
    return "\n".join(out)


def _render(root: Tag, base_url: str, *, keep_links: bool) -> list[str]:
    lines: list[str] = []
    seen_tables: set[int] = set()

    def emit(block: str) -> None:
        """Append a block as individual lines.

        Splitting here rather than storing whole blocks is what lets `_truncate`
        degrade gracefully: a 4,000-character infobox is one block, and a budget
        that cannot fit it whole should keep the first rows, not abandon the entire
        page — which is exactly what happened when blocks were atomic.
        """
        for raw in block.strip("\n").split("\n"):
            line = raw.rstrip()
            if not line.strip():
                # Collapse runs of blank lines; never lead with one.
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            if lines and lines[-1] == line:
                continue  # consecutive duplicates add nothing
            lines.append(line)

    render_tags = list(
        _HEADINGS | {"p", "li", "blockquote", "pre", "table", "dt", "dd", "figcaption", "a"}
    )

    for tag in root.find_all(render_tags):
        if not isinstance(tag, Tag):
            continue
        # Skip anything already consumed as part of a rendered table.
        if any(id(p) in seen_tables for p in tag.parents):
            continue

        if tag.name == "a":
            # A link inside a paragraph or list item is already carried by that
            # block's inline rendering. A link that is *not* inside one is the
            # shape every search result and product card uses —
            # `<div><a>Title</a><p>snippet</p></div>` — and dropping it would lose
            # exactly the titles a research or shopping task is looking for.
            if any(p.name in _INLINE_OWNERS for p in tag.parents):
                continue
            label = tag.get_text(" ", strip=True)
            if not label or len(label) < 2:
                continue
            href = tag.get("href", "")
            if keep_links and href and not href.startswith(("javascript:", "#")):
                emit(f"- [{label}]({shorten(urljoin(base_url, href))})")
            else:
                emit(f"- {label}")
            continue

        if tag.name in _HEADINGS:
            level = int(tag.name[1])
            t = _inline(tag, base_url, keep_links=False).strip()
            if t:
                emit(f"\n{'#' * level} {t}")
        elif tag.name == "table":
            seen_tables.add(id(tag))
            rendered = _render_table(tag, base_url)
            if rendered:
                emit("\n" + rendered)
        elif tag.name == "pre":
            code = tag.get_text("\n", strip=False).strip("\n")
            if code.strip():
                emit(f"\n```\n{code[:2000]}\n```")
        elif tag.name == "li":
            t = _inline(tag, base_url, keep_links=keep_links).strip()
            # Nested lists re-emit their parent's text; keep only the leaf.
            if t and not tag.find(["ul", "ol"]):
                emit(f"- {t}")
        elif tag.name == "blockquote":
            t = _inline(tag, base_url, keep_links=keep_links).strip()
            if t:
                emit("> " + t.replace("\n", "\n> "))
        else:
            t = _inline(tag, base_url, keep_links=keep_links).strip()
            if t and len(t) > 1:
                emit(t)

    return lines


def _truncate(lines: list[str], budget: int) -> tuple[str, bool]:
    """Fill the budget, but never cut mid-structure.

    When the budget runs out the tail is dropped whole and a marker is left, so the
    model knows it is looking at a prefix and can scroll rather than concluding the
    content simply is not there — silent truncation is how agents end up insisting
    a page does not contain something it does.
    """
    out: list[str] = []
    total = 0
    for i, line in enumerate(lines):
        if total + len(line) + 1 > budget:
            remaining = len(lines) - i
            return (
                "\n".join(out).rstrip()
                + f"\n\n_[content truncated — {remaining} more lines below; "
                "scroll or call read_page with a larger budget]_",
                True,
            )
        out.append(line)
        total += len(line) + 1
    return "\n".join(out), False


# --------------------------------------------------------------------- public


def read(
    html: str,
    *,
    base_url: str = "",
    budget: int = 6000,
    keep_links: bool = True,
    aggressive: bool = True,
    whole_page: bool = False,
) -> ReadResult:
    """Turn page HTML into markdown the model can read.

    `whole_page` skips main-block detection — right for a search results page or an
    app dashboard, where the "content" genuinely is the whole layout and density
    scoring would throw away the result list.
    """
    chars_in = len(html)
    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    _strip(soup, aggressive=aggressive)

    if whole_page:
        root = soup.body or soup
        selector = "body (whole-page mode)"
    else:
        root, selector = _find_main(soup)

    density = _link_density(root)
    lines = _render(root, base_url, keep_links=keep_links)
    text, truncated = _truncate(lines, budget)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    headings = [line.lstrip("# ").strip() for line in lines if line.startswith("#")][:20]

    return ReadResult(
        text=text,
        title=title,
        main_selector=selector,
        chars_in=chars_in,
        chars_out=len(text),
        link_density=round(density, 3),
        truncated=truncated,
        headings=headings,
    )


def read_links(html: str, *, base_url: str = "", limit: int = 100) -> list[dict[str, str]]:
    """Every outbound link with its label, deduplicated by target.

    Separate from `read` because research needs the full link set — including the
    nav links `read` deliberately discards — while comprehension does not.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        label = a.get_text(" ", strip=True)
        if not label:
            label = (a.get("aria-label") or a.get("title") or "").strip()
        if not label:
            continue
        seen.add(url)
        out.append({"url": url, "text": label[:150]})
        if len(out) >= limit:
            break
    return out
