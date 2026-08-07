# How chamber works, and how well

Written from the trace database, not from memory. Every number here comes from
`~/.chamber/chamber.sqlite` and you can re-derive any of it with `chamber trace`.

Measured across **7 runs, 144 actions** on Wikipedia, DuckDuckGo, Hacker News,
amazon.in, amazon.com, xe.com and chatgpt.com.

---

## 1. One step, end to end

Every step is the same five phases. This is the whole system.

```
        ┌─ PLAN ──────────── orchestrator (gpt-5.6-luna)
        │   only when the plan changes: start, stage done, stuck, every 8 steps
        │   → "stage 2 of 5: record the 4 US laptops"
        │
        ├─ OBSERVE ───────── extract.js in the page  +  BeautifulSoup in Python
        │   → 120 controls with refs, ~6k chars of readable content
        │
        ├─ DECIDE ────────── step model (mimo-v2.5), every step
        │   → {"thought": "...", "actions": [{"action":"click","ref":"e12"}]}
        │
        ├─ REPAIR ────────── parse.py, before anything touches the browser
        │   → fixes format near-misses, or sends a typed error back
        │
        └─ ACT ───────────── executor.py
            → re-resolve the ref, scroll it into view, move the cursor, click,
              report what happened in words the model can act on
```

### PLAN — the expensive model, run rarely

The orchestrator never sees the control list and never emits an action. It keeps a
list of stages and a `notes` field carrying everything gathered so far.

The step model sees **one stage**, not the whole task. "Record the 4 US laptops with
prices" is a job a small model does well; "comparison-shop two countries then rank
by value" is not. On the market run the planner ran a handful of times against the
step model's 24 — that ratio is the point.

### OBSERVE — two extractors, split by what they need

**In the page** (`dom/extract.js`) — anything requiring live layout: bounding boxes,
computed styles, `elementFromPoint`, shadow roots. Produces the control list:

```
[e12] link       "HP Victus, 13th Gen Intel Core i5-13420H…"  → /dp/B0DTYZ…
[e34] button     "Add to cart"
[e51] textbox    "Chat with ChatGPT"
[e79] link       "Search domain dev.to"  (icon)
```

**In Python** (`dom/reader.py`) — a pure function from HTML to markdown, so it is
unit-tested without a browser. Strips boilerplate, finds the main block by link
density, keeps tables as tables.

The two are separate sections in the prompt because the model *acts* on the first
and *reasons over* the second.

### DECIDE — the cheap model, every step

Sees the stage, the last result, and the page. Emits one action, usually.

### REPAIR — where format problems die

Nothing reaches the browser unvalidated. But rejection is expensive, so anything
with exactly one reading is repaired instead:

| the model said | chamber understood | why it is safe |
|---|---|---|
| `{"click": 42}` | `{"action":"click","ref":"e42"}` | only one reading |
| `{"action":"click","index":7}` | `ref: "e7"` | field alias |
| `{"action":"click","parameters":{"ref":"e1"}}` | flattened | tool-calling habit |
| `read_page` with `budget: 500` | clamped to 1000 | the bound is *our* detail |
| `ask_human` with `reason:"stuck"` | `reason:"blocked"` | never reject the escape hatch |

**The rule is: repair syntax, never guess intent.** A model naming a button that is
not in the list is *not* repaired — that comes back as feedback.

### ACT — where page problems die

```
ref → re-resolve on the live page   (a snapshot is a photograph)
    → disabled?      → typed error, with a hint
    → off screen?    → scroll it in, report the repair
    → covered?       → re-centre, re-check, name the blocker if still covered
    → move cursor, halo the element, click via CDP
    → report in words
```

---

## 2. Failure rates — the real numbers

### Actions that reached the browser

| outcome | count | share |
|---|---|---|
| ok | 142 | **98.6%** |
| unknown_ref | 1 | 0.7% |
| js_error | 1 | 0.7% |
| **total** | **144** | |

Two failures in 144 actions. Both were recoverable and both were reported to the
model with a hint rather than raised.

### The real failure mode is upstream: output format

Actions only reach the executor after parsing. The **repair rate** is where the cost
actually lives, and it depends almost entirely on the step model.

The same market-comparison task, three configurations:

| config | steps | repairs | rate | time | tokens in |
|---|---|---|---|---|---|
| no tools, full prompt | 24 | 9 | 37% | 578s | 504k |
| tools, prompt over-trimmed | 33 | 15 | **45%** | 864s | 932k |
| **tools, prompt corrected** | **21** | **5** | **24%** | **403s** | **458k** |

For comparison, gpt-5.6-luna ran 49 steps of a similar task with **0 repairs**.

### The measurement that explains it

Native tool calling ought to make malformed actions impossible — the API validates
arguments against the schema. It does not, because **the model does not always use
the tools**. Runs now report this:

```
decisions: 15 via tool call, 11 via text — 58% tool rate
```

mimo-v2.5 chooses prose **42% of the time** even with 27 tools offered and a prompt
telling it to call one. Every one of those 5 repairs came from a *text* reply, not a
malformed tool call.

That distinction matters and was worth instrumenting: "the endpoint does not support
tools" and "the model is ignoring them" look identical in the error log and need
opposite fixes.

### A mistake worth recording

The middle row above is a regression I introduced. Having seen tool calling produce
0 repairs on a **5-step** run, I trimmed the JSON format teaching out of the
tool-mode prompt as redundant — then the model fell back to prose anyway and had
*less* guidance than before. Repairs went 37% → 45%.

Two lessons, both now built into the design:

- **Never let the fallback path degrade when optimising the happy path.** The
  tool-mode prompt keeps worked JSON examples, and `repair_prompt` carries the
  *full* format spec — it fires rarely, so the tokens are cheap exactly where the
  model has just demonstrated it needs them.
- **A short run cannot validate a claim about long runs.** The failure only appears
  once the observation is large and the context is deep.

**What each layer actually fixes:** the planner fixed *task* drift — nothing was
forgotten across 8 laptops and two regions. Tool calling plus a correct prompt cut
*format* drift roughly in half and stopped it growing with context (repairs by step
range went 2/4/3 → 2/2/1). Neither eliminates it. Only a stronger step model does.

---

## 3. Is it recovering? Yes — at four layers

Recovery is layered so that a problem is handled at the cheapest level that can see
it. Each layer only escalates what it cannot fix.

**Layer 1 — the parser.** Format near-misses are silently repaired and the repair is
*reported*, so the model converges instead of being carried. Result: 15 repairs
across all runs, 0 failed steps from format.

**Layer 2 — the executor.** Mechanical page problems are fixed before the action:
off-screen elements scrolled in, stale refs re-resolved after a re-render. Reported
as `(chamber adjusted: scrolled it into view)`.

**Layer 3 — typed feedback.** What cannot be repaired comes back naming a *different*
thing to try:

```
✗ click failed [occluded]
  button "Add to cart" is covered by div#cookie-banner.
  → Dismiss it first — cookie banners and modals all have a close control in the list.
```

Every outcome in the enum has a hint. There is a test asserting that.

**Layer 4 — stuck detection.** If the page fingerprint has not moved:
- **3 steps** → nudge the model to read more of the page (text is usually enough, and free)
- **5 steps** → ask the vision model what is actually on screen

Both fired correctly on the market run. The 3-step nudge came at step 12 and the
model recovered without ever needing vision.

**And when nothing works: `ask_human`.** Captcha, login wall, payment form — detected
structurally, the tab is fronted, a banner explains what is needed, and the agent
waits. It never solves a challenge and never types a credential.

### Real recoveries, from the trace

| what went wrong | how it recovered | cost |
|---|---|---|
| Item "Currently unavailable", no Add to Cart button | recognised and recorded as unavailable | 1 step |
| amazon.com hid USD prices (delivery set to India) | opened location picker, set zip 10001, confirmed | 4 steps |
| US listings with no featured offer | recorded, searched for a purchasable equivalent | 1 step each |
| Google rate-limited the currency lookup | switched to xe.com unprompted | 1 step |
| Clicked the wrong listing (ref was for #3) | noticed, diagnosed why, corrected | 1 step |

---

## 4. The run that just happened, step by step

`chamber trace 20260807-101041-ae4686` · **24 steps, 27 actions, 0 failures, 578s**

```
 1-8   amazon.in     four bestsellers: open → read specs → copy title → go_back
                     step 2  copy "ASUS TUF A15…"  (174 chars, never entered the model's context)
                     step 8  "India data is complete"  → open_tab t2

 9-16  amazon.com    THE INTERESTING PART
                     step 10 price not visible; scrolls
                     step 11 "shows 'Sign in to continue' and 'High price'"
                     step 12 "the delivery location is set to India"      ← diagnosis
                     step 13 clicks "Deliver to India"
                     step 14 types 10001, clicks Apply
                     step 15 clicks Continue
                     step 16 "I found the MacBook Neo price: $689.99"     ← solved

17-21  three tabs    t3 = US bestsellers, t5 = xe.com
                     step 19 "1 INR = 0.010497 USD"  → switch_tab back to t3
                             (the US page was still loaded; nothing re-navigated)

22-24  finish        remaining laptops, then done with the full comparison
```

The geo-gated pricing was not in the task and I had not anticipated it. It was
diagnosed from the page text and cleared in four steps with no help.

### What made it work

- **Tabs as memory** — 3 tabs open; `switch_tab` returned to a loaded page instead
  of re-navigating. Nothing had to be remembered.
- **Clipboard for exact text** — a 174-character title went page → buffer → page
  without passing through the model. No tokens, no transcription errors.
- **`go_back` over re-navigation** — 4 uses, each saving a page load.
- **Only 1 `evaluate_js`** in 27 actions. Earlier runs drowned in it; that was a
  symptom of the model not being able to see what it needed.

---

## 5. What is still broken

Stated plainly rather than left to be discovered.

**Format discipline on cheap models — 37% and rising with context.** Fully recovered,
never wrong, but it is the dominant cost. Fix: a better step model.

```bash
uv run chamber run "…" --model glm-5.2
```

**No tab cap.** The market run reached `t5`. Nothing stops a longer run reaching
twenty, and on 16 GB with Brave that would hurt. A bounded pool with LRU eviction is
the next real piece of work.

**Document order is not importance order.** Amazon's buy box sits below the
recommendation carousel in the DOM, so a truncated read can miss the price — which
is exactly what happened at step 10 above. URL stripping bought back most of the
budget and vision covers the rest, but importance-ordered rendering is not built.

**Cross-origin iframes** are not traversed. Same-origin frames are.

**Firefox** has no CDP, so no network log, no `getEventListeners`, no extensions. The
adapter seam exists; realistic scope is a rendering-check target.

---

## 6. Reading a run yourself

```bash
chamber trace                      # list runs
chamber trace <run-id>             # full tree: steps, thoughts, actions, repairs, sources
chamber trace <run-id> --sources   # just the pages, ★ marks the ones cited
```

Rows are never deleted. A page the agent opened and rejected stays in the trail,
because what it chose *not* to use is evidence about what it saw.

---

## 7. Resource behaviour

The local vision model is loaded at run start (a cold load costs ~90s, so paying it
up front beats paying it the first time the agent is stuck) and held with
`keep_alive=30m` for the duration.

**On exit it is released immediately** — `keep_alive: 0` — so 2.16 GB of an 8 GB card
comes back the moment the run ends rather than sitting idle for half an hour.
Verified: `2.16 GB resident → 0 after release`.

Check any time:

```bash
uv run chamber doctor
```

```
✓  vision GPU   2.16 GB, 100% resident in VRAM on NVIDIA GeForce RTX 4060 Laptop GPU
```

That line asks `nvidia-smi` which device is running inference. Ollama's own
`size_vram` distinguishes GPU memory from system RAM but *not which GPU*, which on a
laptop with a discrete card and an iGPU is exactly the ambiguity that matters.
