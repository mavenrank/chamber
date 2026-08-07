# chamber — architecture

Why the pieces are shaped the way they are, and what was deliberately left out.

---

## 1. The central bet: structure over pixels

Computer-use agents from OpenAI and Anthropic screenshot the screen and predict
coordinates. That is the general solution, and it is the right one when the target
is an arbitrary desktop application. For the web it is a bad trade: every step costs
a vision call, and most of the model's reasoning goes on working out what it is
looking at rather than what to do.

Chamber reads the page structurally instead. The model sees:

```
## Controls on screen (14)
  [e3]  textbox   "Search"  value="wireless mouse"
  [e7]  button    "Go"
  [e12] link      "Logitech M185"  → /dp/B004  ⚠ covered by div#promo

## Page content
# Wireless mice
| Model | Price | Stock |
|---|---|---|
| Logitech M185 | £12.99 | In stock |
```

and acts by naming `e12`. Cheaper, faster, and much more accurate — a click on a
named element cannot land 20 pixels off.

The cost is honest: this cannot drive a `<canvas>` game or a Flash-style app that
paints its own widgets. `screenshot` exists for when the structure is not enough,
and the loop attaches one automatically when it detects the agent is stuck. That is
the right split — vision as the fallback, not the substrate.

### Controls and content are separate products

The model needs an indexed list to *act* and readable prose to *understand*. A flat
accessibility dump conflates them, which is why those dumps are simultaneously so
long and so hard to act on. Splitting them halves the token count and makes both
halves usable.

---

## 2. Extraction: a deliberate split across the process boundary

**In the page** (`dom/extract.js`) goes anything that needs live layout: bounding
boxes, computed styles, `elementFromPoint`, shadow-root traversal, what is actually
on screen. None of that survives serialization.

**In Python** (`dom/reader.py`) goes everything that is a pure function of HTML:
boilerplate removal, main-content detection, markdown rendering. Which means it is
unit-tested against fixtures without launching a browser, and tuning it is a
five-second feedback loop instead of a thirty-second one.

### Occlusion is the single highest-value check

The largest category of agent failure is confidently clicking an element that a
cookie banner is sitting on top of. The click "succeeds" — something got clicked —
and the agent proceeds on a false belief, which is far worse than an error.

So extraction tests the click point against `elementFromPoint`, following shadow
roots. If the centre is covered it tries four inset points, because a large element
can be centre-covered while perfectly clickable near its edge, and reports *which*
point works. If nothing works, the model is told what is covering it by name:
`⚠ covered by div#cookie-banner`. That phrasing leads to dismissing the banner. A
generic failure leads to retrying the same click.

### Refs are durable, not just labels

Every element carries both a live DOM handle and a re-locatable path with
shadow-root hops. The handle is the fast path; the path is what survives a
re-render. The executor re-resolves immediately before every action, because a
snapshot is a photograph and a framework can replace every node between the
photograph and the click.

### Reading is where the tokens are

`reader.py` strips, locates, renders:

1. **Strip** what is never content, plus elements whose class or id says outright
   what they are (`cookie-banner`, `related-posts`).
2. **Locate** the main block: `<main>`/`<article>` when the page says so, otherwise
   score candidates on text length discounted by **link density** — the classic
   readability signal, and the one that separates prose from a nav rail.
3. **Render** to markdown, keeping heading levels, list structure, and tables.

Tables get real effort because price comparison and spec sheets *are* tables, and
prose-flattening one destroys the row/column relationship the task depends on. A
table whose rows have inconsistent widths is treated as layout, not data, and
rendered as key/value lines — that is what a Wikipedia infobox is, and forcing it
into a grid spends most of the budget on empty pipes.

### Budget is a design decision, and it was measured

The control list and the readable content compete for the same prompt. Measured
across four very different pages, at a 220-element budget:

| page | candidates | prompt | content share |
|---|---|---|---|
| Wikipedia article | 3,859 | 14.6k chars | 42% |
| Hacker News | 293 | 13.4k | 31% |
| DuckDuckGo results | 366 | 9.0k | **6%** |
| BBC News | 212 | 10.9k | 55% |

The model was reading more navigation than article. Two changes — a 120-element
budget, and the whole-page fallback below — moved content to 50/42/63/65% and cut
every prompt. On the task that motivated it, that was the difference between
12 steps / 116k tokens / no answer and **3 steps / 21k tokens / an answer sourced
from a page it actually read**.

**Tracking parameters were eating the budget.** A single Amazon product link ran
~360 characters carrying about 85 of meaning — the rest was `pd_rd_w`, `pf_rd_p`,
`content-id`, and a `/ref=…` trail embedded in the *path* where query stripping
cannot reach it. Twenty such links exceed a 6,000-character budget on their own,
and on the page where this was measured they did: the budget ran out inside the
recommendation carousel and never reached the buy box. `chamber/urls.py` now strips
them — links average 103 characters, and twelve prices are visible where almost none
were. Stripping is always safe: those parameters identify the *referral*, not the
resource.

**Size is signal.** On a results page the headline is a 628×26 link and the favicon
beside it is 32×32, both correctly labelled — and a model with no size cue clicks
the icon and lands on a filtered search. Elements at or under 44px (the standard
minimum touch target) are marked `(icon)`. Marked, not hidden: a close button is
32×32 too.

**A class-name heuristic will eventually match the wrong thing.** Wikipedia ships
`<html class="… vector-feature-language-alert-in-sidebar-enabled">`; matching
`sidebar` there removed the entire document and the extractor returned zero
characters, silently. Two guards now: structural elements are never junk-matched,
and no single junk match may remove more than 40% of the page's text. The heuristic
is allowed to be wrong; it is not allowed to be catastrophically wrong.

The same shape of guard applies to main-content detection. It assumes the page
*has* a main article, and a search results page, a dashboard or a product grid does
not — DuckDuckGo's `<article>` is a single result, and scoping to it threw away the
other nine. When the winning block holds less than 30% of the page's text, the page
is a list and the whole layout is the content.

---

## 3. The action layer is the durability boundary

Between "the model said click e12" and "the browser clicked something" there are a
dozen ways to be wrong. The executor closes each one *before* the click:

| Situation | What happens |
|---|---|
| Ref not in the current list | `unknown_ref`, with the valid refs listed |
| Element re-rendered away | one re-observation, then re-resolve; reported as a repair |
| Off screen | scrolled into view, reported as a repair |
| Centre covered | re-centred and re-checked; if still covered, `occluded` naming the blocker |
| Disabled | `disabled` — the model is told something else must happen first |
| Playwright raised | classified into a typed outcome, never propagated |

Two rules make this work.

**Repair what is mechanical, refuse what is not.** Scrolling to an element is not a
decision the model should spend a step on. Choosing a different element when the
intended one is gone *is*.

**Every failure carries a hint naming a different action.** `"Timeout 30000ms
exceeded"` tells a model nothing it can act on, so it retries identically and the
loop stalls until the budget runs out. `"the button is covered by a cookie banner —
dismiss that first"` changes what happens next. There is a test asserting every
outcome in the enum has one.

Repairs are surfaced, not hidden. An agent that is quietly carried never learns the
page's behaviour.

---

## 4. Format tolerance as a first-class feature

The earlier prototype's clearest finding: cheap models know *what* to do long before
they can reliably say it in the required shape.

```
{"click": 42}                            ← the actual failure mode
{"action": "click", "index": 42}
{"action": "click", "ref": 42}
{"action": "click", "ref": "e42"}        ← what was specified
```

Every one of those is a formatting slip with exactly one reading. Rejecting them
costs a full round trip to fix something the model never got wrong. So `agent/parse.py`
normalises near-misses — action-name-as-key, bare integer refs, field aliases,
fenced JSON, prose wrapped around it, trailing commas — and records each repair so
the model is told and converges.

The rule is **repair syntax, never guess intent.** `{"click": 42}` → `ref: "e42"` is
safe. A model naming a button that is not in the list is not, and that comes back as
feedback.

The system prompt is *generated from the schema*, so the instructions and the
validator cannot drift. A prompt that disagrees with its validator is a machine for
producing errors the model cannot fix, because it is doing exactly what it was told.

---

## 5. Readiness: neither `load` nor `networkidle`

`load` fires before content exists on a client-rendered app — you get an empty
shell. `networkidle` never fires on a page with a websocket, an analytics heartbeat,
or a polling widget, so it always times out.

What correlates with "ready to read" is the DOM going quiet. `browser/ready.js`
tracks the last *meaningful* mutation, filtering spinner churn — attribute rewrites
on animating elements — so a loading animation cannot read as continuous activity
forever. Combined with in-flight fetch/XHR counted at the API level (so cache hits
count) and a content-volume check, because a quiet, complete page with 40 characters
of text is a shell whose render has not run.

A timeout is never an error. The page is returned as-is with a note saying it never
settled; an agent can usually work with a half-loaded page, and refusing to look at
one helps nobody.

---

## 6. Being watchable is a feature, not decoration

The overlay lives in a shadow root, is entirely `pointer-events: none`, is marked so
extraction and occlusion checks skip it, and slides out of the way when the agent
targets something underneath it.

**The real mouse is never touched.** Playwright's mouse and CDP's Input domain
synthesize events inside the browser; the OS pointer does not move. The dot is a
read-out of where synthetic events are being sent, not a driver — which is why the
machine stays usable while the agent works.

This matters beyond aesthetics. An agent you cannot watch is one you cannot
supervise, cannot debug, and cannot trust with anything consequential. The cursor
glide is also a deliberate pause: it gives a human the half-second needed to see
what is about to happen and intervene.

---

## 7. DevTools as capability, not panel

"Let the agent open DevTools" resolves to: DevTools is a UI over CDP, so give the
agent CDP. Console and network are buffered from page creation — an error that fired
during load is exactly the error worth seeing, and by the time the agent thinks to
ask, it is gone. `inspect` returns computed styles, box model, attributes, outer HTML
and real event listeners via `DOMDebugger.getEventListeners`, which is genuinely
unavailable to page script.

The visible panel is still available (`devtools_panel=True`) but that is for the
human. An agent reading a screenshot of the Elements tree would be strictly worse at
this than one calling `DOM.getBoxModel`.

---

## 8. Handing back

Chamber never solves a challenge and never types a credential. That is a deliberate
limit: the human is sitting in front of the window anyway, so the handoff costs them
seconds and keeps the system on the right side of every site's terms.

Detection is structural rather than visual — challenge widgets are third-party
iframes with stable origins (`google.com/recaptcha`, `challenges.cloudflare.com`),
which survives translation, restyling and A/B tests in a way that text matching does
not. Login and payment walls are detected too; they are not bot checks but they need
the same response.

The handoff surfaces the tab, paints a banner *on the page* — console output is easy
to miss, the browser window is what the person is already looking at — and then
polls for the condition to clear rather than requiring a keypress in a terminal. It
always times out, and the timeout is always survivable.

---

## 9. Why Brave, and why that was measured rather than assumed

Real uBlock Origin needs Manifest V2's blocking `webRequest`. Chrome 151 removed the
last way to run MV2. The obvious conclusion is ungoogled-chromium, which carries an
explicit MV2 patch.

But Brave 151 was already installed, so it was tested rather than assumed: uBO 1.73
loads, enables, its background page runs, and blocking listeners are installed. The
only obstacle was that unpacked extensions stay toggled off behind developer mode —
a *profile preference*, and chamber owns the profile, so it is seeded before first
launch.

That is one fewer dependency on a patch maintained by goodwill. `discovery.py`
supports both and flags Google Chrome explicitly, because Chrome launches fine and
then silently refuses the extension, which is the worst possible failure mode.

---

## 9a. Vision as a sensor, not a substrate

Optional, and off unless `CHAMBER_VISION_MODEL` is set. When on, a **second, small,
local model** looks at screenshots and answers one narrow question; its answer *in
words* is what enters the planner's context. The planner itself never sees an image.

That split buys three things. The planner pays a hundred tokens for a description
instead of thousands for an image that then sits in context forever. It works with
text-only planners, which most cheap OpenAI-compatible endpoints are. And the sensor
can be tiny — "what is covering the button" is a far easier job than "plan a
shopping task", so 3B on a laptop GPU is genuinely enough.

It fires on stuck detection, which is the one moment a picture reliably beats the
DOM: "nothing changed for several steps" nearly always means something is in the
way that the control list did not convey.

### "OpenAI-compatible" is not one format

The spec says a vision message carries `{"type": "image_url", "image_url": {"url": …}}`,
and OpenAI itself requires exactly that. OpenCode Go's `gpt-5.6-luna` returns **HTTP
400** for it and accepts only a bare data-URI string in the same field. Measured:

| payload | result |
|---|---|
| text only | 200 |
| `image_url: {"url": "data:image/png;base64,…"}` | **400**, empty message |
| `image_url: "data:image/png;base64,…"` | 200, accurate description |
| Anthropic-style `{"type": "image", "source": …}` | 200, but "I can't see an image attached" |

That last row is the dangerous one: a **success response that silently saw no
image**. A client that only checked status codes would conclude vision worked and
then quietly reason about nothing.

So `LLM` starts with the spec-correct object form, and on a 400 for a request that
carried images, flips to the string form once and remembers it for the session.
Self-healing, no configuration, and still correct against real OpenAI.

### The sensor does not have to be local

The split-brain design says nothing about *where* the sensor runs — only that its
output is words. Pointing `CHAMBER_VISION_MODEL` at a hosted model works unchanged.
On the page that broke an earlier run:

| | time | answer |
|---|---|---|
| qwen2.5vl:3b (local, RTX 4060) | 18.7s | "no Add to Cart button visible *in this part of the screen*" |
| gpt-5.6-luna (hosted) | **7.0s** | "No, there is no Add to Cart button. The item is unavailable: *'Currently unavailable. We don't know when or if this item will be back in stock.'*" |

Faster and it quotes the buy box exactly. The local model remains the right answer
when cost or privacy matters; the hosted one is better when neither does. Both are
one environment variable.

### Three numbers that shaped it

All measured on an RTX 4060 Laptop (8 GB) running `qwen2.5vl:3b` under Ollama.

**A cold load costs ~90 seconds.** 107s cold against 19s warm. Ollama also evicts
after five minutes idle, and the gaps between vision calls in an agent loop
routinely exceed that — so the *first* time the agent got stuck it would wait a
minute and a half. Chamber preloads at run start via Ollama's native load endpoint
and sends `keep_alive=30m` on every request.

**Scaling the screenshot is a free win.** Vision cost scales with image *area*, and
Qwen2.5-VL tiles its input dynamically. Same page, same question:

| scale | time | size | answer |
|---|---|---|---|
| 1.0 | 59.9s | 667 KB | "Yes, there is an Add to Cart button… Currently unavailable" — self-contradictory |
| 0.6 | 18.7s | 319 KB | "There is **no** Add to Cart button… marked **Currently unavailable**" — correct |

3.2× faster *and* more accurate — the smaller image seems to be easier to reason
over, not just cheaper. The resize happens in the compositor via CDP
`Page.captureScreenshot`, so it costs no dependency and no second encode.

**Text gets two chances first.** `stuck_after` (3) nudges the planner to read more
of the page; `vision_after` (5) is where the sensor is actually consulted. The
structural view usually does hold the answer, and 19s is not free.

Worth stating: `size_vram` from Ollama distinguishes GPU memory from system RAM but
**not which GPU**, which on a laptop with a discrete card and an iGPU is exactly the
ambiguity that matters. `chamber doctor` shells out to `nvidia-smi` and names the
device rather than inferring it.

**This gap was found the hard way.** Before it existed, `screenshot` saved a PNG and
returned the *path* — so a planner that asked for one learned nothing, and asked
again. On a real Amazon run the agent took two screenshots it could not see while
hunting for an Add to Cart button on a product marked *Currently unavailable*.

### Ordering is the honest remaining limitation

Content is rendered in document order, and document order is not importance order.
On a product page the recommendation carousel precedes the buy box in the DOM, so a
truncated read can miss the price and availability entirely — which is exactly what
happened above. URL stripping bought back most of the budget, but the general fix
(importance-ordered rendering, or a `read_page` that can search for a region) is not
built. Until it is, the vision path is what covers the case.

## 10. What is not here

Stated plainly, so nobody has to discover it by reading the source.

**Network-layer ad blocking.** uBO blocks in the renderer. A Playwright `route()`
handler blocking before the request is issued would be cheaper and, more usefully,
*loggable* — what an agent chose not to load is evidence about what it saw.
`adblock` (Brave's `adblock-rust`, abi3 wheels) is the intended backend. Not built
because uBO already covers the blocking; this is about the audit trail.

**Parallel tabs with a bounded pool.** Tabs are tracked and capped only by the
model's willingness to open them. Multi-engine search scored across tabs, LRU
eviction under memory pressure, and RSS watchdogging all want a real pool with tab
leases. The single-tab path had to be right first.

**Cross-origin iframe extraction.** Same-origin frames are traversed. A cross-origin
frame needs per-frame evaluation with coordinate translation. Uncommon enough for
content, common enough for embedded checkout that it is worth doing.

**Firefox.** Playwright drives it, but it has no CDP — so no network log, no
performance trace, no `getEventListeners`, and no loadable extensions. A
`BrowserAdapter` seam exists. WebDriver BiDi is the eventual path to parity and is
not ready to build on. Realistic scope is a rendering-check target, not a
full-feature one.

**Context compaction beyond the last two observations.** Currently older turns
collapse to a line. A long research run would do better summarising what it learned
rather than dropping it.

**A live status view.** The trace store is written with WAL so a second process can
read a running session. Nothing reads it yet.

---

## 11. Build order, if you are extending this

1. `dom/reader.py` — pure, unit-tested, and where most quality lives. Tune here first.
2. `actions/result.py` — add a failure mode and its hint. Cheapest possible win.
3. `agent/prompt.py` — the system prompt is generated; add an action and it appears.
4. `demos/` — a task-specific paragraph appended to the prompt is a whole capability.
5. `browser/pool.py` — does not exist yet; it is the next real piece of work.
