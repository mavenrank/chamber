# Changelog

All notable changes to chamber are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0, so the minor number moves when a capability lands and the patch number
moves for fixes. The public surface — `Chamber`, the action schema, the CLI, the
MCP tool names — is not yet frozen.

Numbers quoted below are measured, not estimated. Every one can be re-derived from
the trace database with `chamber trace`.

---

## [0.13.0] — Unreleased

### Liveness is a heartbeat; runs gain threads and memory

**Added**

- **Heartbeat liveness** — the loop writes `last_beat` every step (throttled
  while paused); a run counts as live only with an open row *and* a recent
  beat. Crashed runs never close their row, so `ended_at IS NULL` alone kept
  crowning corpses as live. Legacy databases migrate with one `ALTER TABLE`.
- **Thread model** — `parent_run` link plus a `note` table: claimed mailbox
  notes persist with the step that read them, and continuations link forward
  without resurrecting ids.
- **Run identity for supervisors** — `Chamber.open(run_id=…)` and
  `chamber run --run-id`, so a supervisor can name a run before its first
  step lands; graceful stop flag honoured at the step boundary.

---

## [0.12.0] — 2026-09-20

### Live runs and a human mailbox

Console stops polling blind. The server tails the WAL-mode trace store over
SSE, and a file mailbox connects the Console to the loop across processes:
file a note, hold/release the run, watch rows land as they are written.

**Added**

- **`/api/live` event stream** — stdlib SSE tailing steps, actions, model
  exchanges (slimmed to role/model/timing) and visits per run, with
  row-watermark cursors so reconnects are lossless. No new dependency:
  one-way server→browser sync doesn't need WebSockets.
- **File mailbox** (`inbox.py`) — `runs/<id>/inbox/new/*.json` claimed
  atomically into `done/`; `control.json` carries the pause flag. Path
  traversal rejected; empty notes rejected.
- **Loop drain** — pause holds at the step boundary (beside the `controlled`
  check); notes join the step's feedback so the model reads them with the
  fresh page. New `human_note` / `paused` display events (Desk shows them
  too) plus terminal transcript lines.
- **`POST /api/inbox`, `POST /api/pause`, `run_state`** — the write half of
  the Console, localhost-only like everything else here.
- **Console Live view** — run picker, streaming feed, pause/resume, note box
  with pending-notes list. Refresh stays as the durable fallback.

**Verified** — 148 tests green, `ruff check` clean, live POST/SSE pinged
against the real server and trace store.

---

## [0.11.0] — 2026-09-20

### Chamber Console: read-only sessions shell over the trace store

Desk watches one live run; Console reviews every run. `chamber console`
serves it on localhost with the built app plus a query API — finished and
in-flight sessions, per-role model calls, sources, steps, environment and
profiles.

**Added**

- **`chamber console`** — stdlib-only localhost server (`/` → console):
  static bundle plus `/api/query`
  (`list_runs`/`get_run`/`list_profiles`/`get_environment`), straight from
  the WAL-mode trace store. Never binds beyond localhost.
- **Console app** — React/Vite + Radix, Bun-managed, OS-aware light/dark
  theme with toggle: navbar (Sessions · Live · Environment · Profiles ·
  Models), session tabs (Overview · Sources · Steps), run search,
  auto-refresh with connection-status button, resizable split, settings view.
  Dashed placeholders mark every Phase 2/3 home.
- **`display/queries.py`** — the sole history API behind all three
  transports (direct import, Desk binding, HTTP). Redacts keys.
- **Desk session block** — `adapter.set_session()` attaches
  run/profile/browser/model identity once per window.
- **Desk `__chamberQuery` binding** — history inside the Desk window with no
  new server dependency.

**Desk × Console contract** (standing decision, locked here):

1. `DisplayAdapter.snapshot()` is the wire format — additive changes only.
2. `display/queries.py` is the only history API, over all three transports.
3. Desk stays server-free — its sole ingress is
   `window.__chamberDesk.apply()`.
4. Console owns URLs (`/` → console); Desk is never served over HTTP.

**Verified** — 139 tests green, `ruff check` clean, live ping of the
console routes (`/console.html`, `/api/query`) during review.

---

## [0.10.0] — 2026-09-20

### A live window over the agent loop

The browser was already visible; the run was not legible anywhere outside the
terminal. This release adds Chamber Desk — a small companion window fed by a
normalized event stream — and puts the loop's data flow behind it.

**Added**

- **Chamber Desk** (`display/`) — a 520×780 Chromium `--app` window rendering
  a React/Vite bundle (`desk.js`/`desk.css`, inlined into `about:blank` so it
  needs no server): current status, step counter and a grouped activity chain
  with model exchanges, vision results and screenshots. Opens with every run;
  closing it never ends the run.
- **`DisplayAdapter`** — the single seam between loop/model/vision dialects
  and the window, with pluggable `StatusGetter`/`StatusParser` pairs so a new
  status source needs no loop or renderer change.
- **Loop display fan-out** — `run_task` emits one ordered event chain
  (`chamber._display_event`) to the window, terminal, MCP progress and trace
  store; step/planner/vision roles tag every model call.
- **Trace model exchanges** — full prompts, outputs, tool calls and provider
  reasoning summaries per call (`chamber trace <run-id> --llm`).
- **Official OpenAI Responses API** path beside chat-completions, with
  `reasoning_effort`, spec-correct image encoding plus automatic fallback, and
  tool-calling fallback to JSON-in-text when an endpoint rejects tools.
- **Planner/vision configuration** — `CHAMBER_ORCHESTRATOR_MODEL`,
  `CHAMBER_VISION_FALLBACK_MODEL`, `CHAMBER_DISPLAY=window`.
- **Overlay rewrite** — top bar with prefs binding and per-host skip list
  (`CHAMBER_OVERLAY_SKIP_HOSTS`) for pages whose CSP breaks the bar.
- **`demos/display_fixture.py`** — deterministic Desk run with no LLM or
  website, for inspecting the UI.

---

## [0.9.0] — 2026-08-07

### Watchability and handing over

The run was already visible in the browser; it was not *legible*. You could see a
cursor move but not why, and there was no way to interrupt without killing the
process. This release makes the whole data flow observable and gives the human a
real stop button.

**Added**

- **`chamber watch`** — a live terminal view built from the event stream, with a
  panel per participant: what the planner decided, what the step model was asked
  and answered, whether it called a tool or wrote prose, what the vision model saw,
  every action and result, plus running tool-rate/repair/token counters. Purely a
  listener, so nothing in the agent knows it exists and it cannot slow a run or
  break one.
- **`llm_request` / `llm_response` events** carrying model, prompt size, tool
  count, latency, tool calls and token usage. Model traffic was previously
  invisible — the one part of the system most worth watching.
- **A top bar across every page**, replacing the small corner pill. Brand and step
  counter, the goal, and an auto-scrolling feed with colour-coded tags (planner,
  thinking, action, eyes, error). It *pushes the page down* rather than covering
  it; hiding the top of every site is the one thing this project exists to prevent.
- **A Take Control button.** The only element in the overlay with pointer events.
  Pressing it turns the bar amber, hides the cursor, and genuinely stops the agent
  — checked at the top of every step before it observes or acts, with a second
  guard in the executor so it also holds for MCP and script callers. Wired through
  `expose_binding` so it works on tabs opened later, not just the first one.
- **`chamber setup`** — a window opened with control already held: no model
  connected, nothing observing. Configure the browser however you like; it is saved
  to the profile every later run uses.
- **`chamber profiles`** — list profiles with sizes and which hold logins;
  `--prune` and `--remove` to reclaim disk. A working profile runs 100–300 MB and
  one-off `--profile` names accumulate fast (13 of them reached 1.7 GB here).
- Bloat-reduction defaults for **new** profiles: Brave Rewards, Wallet, VPN button,
  Brave News, Leo, sidebar, new-tab widgets, plus Chromium autofill and
  password-save prompts. First-run only, so anything you change by hand wins.
- `tests/test_injected_js.py` — syntax-checks injected JavaScript with `node
  --check` when available, plus a backtick-balance check that works without it.

**Fixed**

- **The renderer sandbox is on again.** Playwright disables it by default, which is
  reasonable for throwaway CI containers and wrong here: chamber drives a
  persistent profile holding real cookies and logins against arbitrary sites. Brave
  was showing a standing warning and it was right.
- The vision model is **released from VRAM on exit** (`keep_alive: 0`) instead of
  sitting resident for the full 30-minute window. Verified 2.16 GB → 0.
- `chamber open` was registered twice, so both `open` and a phantom `open-` showed
  in the command list.
- Ctrl+C no longer prints a stack trace. Interrupting tears down a live browser and
  Playwright's driver connection drops mid-teardown; that surfaced as
  `Connection closed while reading from the driver` *after* the command had already
  said it was closing.
- A backtick inside a CSS comment silently truncated `overlay.js` — the stylesheet
  lives in a template literal — and the entire overlay vanished with one line in
  the page console as the only evidence.
- The hidden handoff banner bled ~24px of amber under the top bar: a percentage
  `translateY` is relative to the element's own height and never fully cleared.

**Changed**

- CLI help now leads with worked examples grouped by intent rather than a bare
  command list, and `chamber run --help` shows real invocations.

---

## [0.8.0] — 2026-08-07

### Native tool calling

The dominant cost on cheap models was never the browser — it was getting a valid
action out of them at all. This release attacks that directly.

**Added**

- **Native tool calling, on by default**, with automatic fallback: an endpoint that
  rejects tools with a 400 drops to the JSON-in-text path and the choice sticks for
  the session. Every model tested on the OpenCode Go endpoint (mimo-v2.5,
  gpt-5.6-luna, glm-5.2, kimi-k2.7-code, qwen3.7-plus) calls tools correctly.
- **Decision tracking.** Runs report `15 via tool call, 11 via text — 58% tool
  rate`. This distinguishes "the endpoint does not support tools" from "the model is
  choosing prose", which look identical in an error log and need opposite fixes.
- Automatic image-encoding detection. `image_url` as an object is the spec and what
  OpenAI requires; OpenCode Go's `gpt-5.6-luna` returns **400** for it and accepts
  only a bare data-URI string. The client starts spec-correct and flips on a 400.
  Worth noting the dangerous case found while probing: an Anthropic-style payload
  returns **HTTP 200 while seeing no image at all**.

**Changed**

- The system prompt drops the generated action reference in tool mode — the tool
  schemas already carry every name and field. **5,783 → 2,871 characters (50%)**.
- `repair_prompt` now carries the *full* format specification rather than a
  one-line reminder. It fires rarely, so the tokens are cheap exactly where the
  model has just demonstrated it needs them.

**Fixed**

- A regression introduced and then corrected within this release, recorded because
  the lesson matters. Seeing 0 repairs on a *5-step* run, the JSON format teaching
  was trimmed from the tool-mode prompt as redundant — then the model replied in
  prose anyway and had *less* guidance than before. Repairs went **37% → 45%**.
  With the teaching restored: **24%**, and no longer growing with context
  (repairs by step range 2/4/3 → 2/2/1). Two rules came out of it: never let the
  fallback path degrade while optimising the happy path, and a short run cannot
  validate a claim about long ones.

---

## [0.7.0] — 2026-08-07

### A planner that thinks rarely

A single model doing both jobs — holding the whole task *and* picking the next
click — is expensive when it is good and unreliable when it is cheap. By step
thirty a small model has forgotten there were five laptops.

**Added**

- **`agent/orchestrator.py`** — a strong model called a handful of times per run
  (at the start, when a stage completes, when stuck, periodically) that maintains a
  short stage list and a `notes` field carrying everything gathered. It never sees
  the control list and never emits an action.
- The step model now sees **one stage**, not the whole task. "Record the 4 US
  laptops with prices" is a job a 3B model does well; "comparison-shop two countries
  then rank by value" is not.
- A **vision fallback chain**. A local answer that fails or hedges ("I can't see
  that in this part of the screen") falls through to a hosted model; a real answer
  does not. When the fallback answers it says so, because a silent, slower, billed
  fallback should not be invisible.
- Three-tier configuration: `CHAMBER_ORCHESTRATOR_MODEL`, `CHAMBER_MODEL`,
  `CHAMBER_VISION_MODEL` + `CHAMBER_VISION_FALLBACK_MODEL`.

**Result** — on an 8-laptop, two-region comparison the planner eliminated task
drift entirely: nothing forgotten, no stage redone, all eight tracked to the end.
It did nothing for output format; those are separate axes.

---

## [0.6.0] — 2026-08-07

### Memory that lives outside the model

Anything the model has to remember is something it can forget. Three places to put
state instead.

**Added**

- **`copy` / `paste` / `clipboard` actions** backed by a chamber-internal clipboard
  — deliberately not the operating system's, for the same reason the cursor is
  synthetic: the agent must not wipe what the person had copied. Text goes page →
  buffer → page **without entering the model's context**: no tokens, and
  character-exact where re-typing loses details. Pasting uses CDP `Input.insertText`,
  which is what Chrome itself does, so frameworks see the events they expect.
- **Tabs as memory.** `open_tab` takes a `purpose`, the tab list appears in every
  observation with those labels, and the prompt tells the model to keep pages open
  and `switch_tab` rather than re-navigate and re-remember.

**Verified** — three Amazon listing titles copied verbatim (124, 125, 174
characters), Amazon left loaded in one tab while ChatGPT was used in another, then
switched back with 120 controls still live and nothing reloaded.

---

## [0.5.0] — 2026-08-07

### Everything real runs broke

Nothing in this release was designed. Each item is a bug a live site found.

**Fixed**

- **`display: contents` stopped the DOM walk.** An element with no box was treated
  as invisible and its subtree skipped — but `display: contents` renders its
  children normally and is everywhere in modern component frameworks. This made
  **ChatGPT's composer invisible**: `#prompt-textarea` sits under two such
  wrappers. Only `display: none` and `content-visibility: hidden` actually remove a
  subtree; `visibility: hidden` does not, since a child can set it back.
- **`KeyError: 't1'` ended a 50-step run.** `page` healed when the current tab
  closed; `overlay` and `monitor` did a raw dict lookup. The throw happened in the
  executor's *cleanup* line, outside its try block — a run lost to a status dot.
- **1×1 pixel inputs** were offered as real fields. Sites hide them behind styled
  labels; they are visible by every CSS measure. A test typed into one and the text
  went nowhere.
- A nested `<p>` inside a contenteditable was reported as a second textbox —
  `isContentEditable` is inherited — giving two refs for one box.
- `copy` returned empty on Amazon's product cards, whose `<a>` carries an
  `aria-label` and no inner text.
- Numeric fields now **clamp** instead of rejecting. A `read_page` budget below the
  1000 minimum cost a full round trip to a bound that is chamber's implementation
  detail, not the model's mistake.
- An out-of-set enum falls back to the field default. This was rejecting
  `ask_human` — the escape hatch — at the exact moment the model was already stuck.
- The parser now unwraps function-calling style nesting
  (`{"action": "click", "parameters": {...}}`), which tool-trained models reach for
  even when asked for a flat shape.

---

## [0.4.0] — 2026-08-07

### Vision, as a sensor rather than a substrate

Chamber reads pages structurally and that stays the default — a measured research
run answered in 3 steps and 21k tokens with no image. But structure runs out on
canvas content, visual breakage, and "nothing I do changes anything".

**Added**

- **Split-brain vision.** A small model looks at the screenshot and answers one
  narrow question; its *answer in words* enters the planner's context. The planner
  never pays image tokens, works with no vision of its own, and the sensor can be
  tiny.
- Image support in the LLM client for both wire protocols.
- **`chamber see`** — screenshot a page and have the vision model describe it,
  reporting latency and image size so a model can be chosen on numbers.
- **Staged fallback.** At 3 stuck steps the model is nudged to read more of the
  page; only at 5 is vision consulted. Text usually holds the answer and is free.
- GPU verification in `doctor`. Ollama's `size_vram` distinguishes GPU memory from
  system RAM but **not which GPU**, which on a laptop with a discrete card and an
  iGPU is exactly the ambiguity that matters; `nvidia-smi` names the device.

**Measured on an RTX 4060 Laptop with qwen2.5vl:3b**

| | result |
|---|---|
| cold load | 107s vs 19s warm — hence preloading and `keep_alive=30m` |
| screenshot at 1.0 scale | 59.9s, 667 KB, self-contradictory answer |
| screenshot at 0.6 scale | **18.7s, 319 KB, correct answer** |

Scaling is 3.2× faster *and* more accurate; the resize happens in the compositor
via CDP, costing no dependency and no second encode.

---

## [0.3.0] — 2026-08-06

### Budget

The model was reading more navigation than article.

**Fixed**

- **Tracking parameters were consuming the content budget.** A single Amazon
  product link ran ~360 characters carrying about 85 of meaning, including a
  `/ref=…` trail in the *path* where query stripping cannot reach. Twenty such
  links exceed a 6,000-character budget alone — and on the page where this was
  measured they did, so the budget ran out inside the recommendation carousel and
  never reached the buy box. After `chamber/urls.py`: links average **103
  characters**, and twelve prices are visible where almost none were.
- **Control budget cut 220 → 120.** Measured across four sites, content share went
  42/31/6/55% → **50/42/63/65%**, and every prompt shrank.
- **Icon marking.** On a results page the headline is a 628×26 link and the favicon
  beside it is 32×32 — both links, both labelled. Without the cue a model clicks the
  icon and lands on a filtered search. Marked, not hidden: a close button is 32×32 too.
- Zero-width and bidi characters stripped from labels.

---

## [0.2.0] — 2026-08-06

### The reader

**Fixed**

- **A junk-pattern match removed an entire document.** Wikipedia ships
  `<html class="… vector-feature-language-alert-in-sidebar-enabled">`; matching
  `sidebar` there deleted everything and the extractor returned **zero characters**,
  silently. Two guards now: structural elements are never junk-matched, and no
  single match may remove more than 40% of the page's text. The heuristic is
  allowed to be wrong; it is not allowed to be catastrophically wrong.
- **Main-content detection assumes a main article exists.** On a search results
  page it does not — DuckDuckGo's `<article>` is a single result, and scoping to it
  threw away the other nine. When the winner holds under 30% of the page, the page
  is a list and the whole layout is the content.
- **Layout tables** are rendered as key/value lines rather than a ragged grid. A
  Wikipedia infobox is a caption spanning twelve columns then two-cell rows; forced
  into a grid it spends most of the budget on empty pipes.
- Truncation works line by line. Whole-block truncation abandoned an entire page
  when one infobox exceeded the budget.
- Bare `<a>` inside a plain `<div>` now renders — the exact shape of every search
  result and product card, previously dropped entirely.

---

## [0.1.0] — 2026-08-06

### Foundation

First working system: a headed browser an LLM drives structurally, watchably, and
without the human losing their machine.

**Added**

- **Browser layer.** Discovery of a build that still runs Manifest V2 (Brave first,
  ungoogled-chromium next, Google Chrome flagged as unable), profile seeding that
  makes `--load-extension` actually *enable* uBlock Origin, and launch flags that
  undo the Playwright defaults which silently disable extensions.
- **Extraction.** An in-page script for what needs live layout — geometry,
  occlusion via `elementFromPoint`, shadow DOM, stable refs with re-locatable paths
  — and BeautifulSoup on the Python side for readable content, so the reading half
  is a pure function testable without a browser.
- **The action layer.** One pydantic discriminated union as the entire vocabulary,
  an executor that re-resolves every ref against the live page before acting, and a
  typed outcome taxonomy where **every failure carries a hint naming a different
  thing to try**.
- **Format tolerance.** `{"click": 42}` and its relatives are repaired rather than
  rejected, and each repair is reported so the model converges. The rule is repair
  syntax, never guess intent.
- **The visible layer.** A synthetic cursor that never touches the OS pointer, a
  labelled halo on the element being acted on, and a reasoning read-out.
- **Readiness detection** on DOM quiescence rather than `load` (fires before an SPA
  renders) or `networkidle` (never fires with a websocket open).
- **Challenge handling.** Captcha, login and payment walls detected by iframe
  origin rather than text, then handed to the human. Chamber never solves a
  challenge and never types a credential.
- **Three front-ends** over one core: a library, a CLI agent loop, and an MCP
  server.
- **A trace store** — SQLite, append-only, recording runs, steps, actions, repairs
  and every page opened, with the ones a summary cited marked apart from those
  merely looked at.
- uBlock Origin installer that asserts `manifest_version == 2` and the presence of
  `webRequestBlocking`, so you cannot quietly end up with uBO Lite.

**Verified** — Brave 151.1.93.132 with uBO 1.73.0: manifest accepted, extension
enabled, MV2 background page running, blocking `webRequest` listeners installed.

---

[0.9.0]: https://github.com/mavenrank/chamber/releases/tag/v0.9.0
[0.8.0]: https://github.com/mavenrank/chamber/releases/tag/v0.8.0
[0.7.0]: https://github.com/mavenrank/chamber/releases/tag/v0.7.0
[0.6.0]: https://github.com/mavenrank/chamber/releases/tag/v0.6.0
[0.5.0]: https://github.com/mavenrank/chamber/releases/tag/v0.5.0
[0.4.0]: https://github.com/mavenrank/chamber/releases/tag/v0.4.0
[0.3.0]: https://github.com/mavenrank/chamber/releases/tag/v0.3.0
[0.2.0]: https://github.com/mavenrank/chamber/releases/tag/v0.2.0
[0.1.0]: https://github.com/mavenrank/chamber/releases/tag/v0.1.0
