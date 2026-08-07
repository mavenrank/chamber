<p align="center">
  <img src="docs/banner.jpg" alt="chamber" width="720">
</p>

<p align="center">
  <b>A headed browser an LLM can drive in the open.</b><br>
  <sub>Visible cursor · visible reasoning · a stop button that actually stops it</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python 3.12">
  <img src="https://img.shields.io/badge/tests-249%20passing-brightgreen" alt="249 tests">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT">
  <img src="https://img.shields.io/badge/version-0.9.0-orange" alt="v0.9.0">
</p>

---

One real browser window, on your screen, with a cursor you can watch and a bar
across the top saying what it is doing and why. Nothing happens off-screen. When it
hits a captcha it asks you. When you press **Take control**, it stops.

```bash
uv sync --extra mcp
uv run python scripts/install_ubo.py
uv run chamber doctor
uv run chamber watch "find the cheapest USB-C hub with ethernet and add it to a basket"
```

For a bare `chamber` on your PATH — `--editable` so it tracks your working copy:

```bash
uv tool install --editable .
chamber --help
```

---

## Why this exists

OpenAI and Anthropic both ship computer-use agents that drive a browser by
screenshotting the screen and predicting pixel coordinates. That is the general
solution, and the right one for an arbitrary desktop application. For the web it is
a bad trade: every step costs a vision call, and most of the model's reasoning goes
on working out what it is looking at rather than what to do.

Chamber reads the page structurally instead. The model sees a compact list of things
it can act on, plus the actual readable content, and acts by naming an element
rather than a coordinate:

```
## Controls on screen (14)
  [e3]  textbox   "Search"  value="wireless mouse"
  [e7]  button    "Go"
  [e12] link      "Logitech M185"  → /dp/B004  ⚠ covered by div#promo

## Page content
| Model | Price | Stock |
|---|---|---|
| Logitech M185 | £12.99 | In stock |
```

Cheaper, faster, and far more accurate — a click on a named element cannot land
twenty pixels off. Vision is still there, as a fallback, for the cases structure
genuinely cannot reach.

The second difference is that it is **watchable**. An agent that works invisibly is
one you cannot supervise, cannot debug, and cannot trust with anything that matters.

---

## What it does

**Reads pages properly.** An injected script handles what needs live layout —
geometry, occlusion, shadow DOM, what is actually on screen. BeautifulSoup handles
the reading: boilerplate stripped, main content found by link density, tables kept
as tables. Controls and content arrive as separate sections, so the model can act on
one and reason over the other.

**Survives real pages.** Every action re-resolves its target before executing. Off
screen gets scrolled into view; a re-render gets re-resolved; a disabled or covered
element comes back as a typed error naming a different thing to try. The model never
sees a raw traceback.

**Tolerates cheap models.** `{"click": 42}` where `{"action":"click","ref":"e42"}`
was specified is a formatting slip, not a wrong decision — so it is repaired, and
the repair is reported so the model converges. Ambiguity is still rejected: the rule
is repair syntax, never guess intent.

**Shows its work.** A bar across the top of every page with the goal, the current
step, and an auto-scrolling feed of what each model said. A synthetic cursor glides
to what it is about to click. Your real mouse is never touched — input is
synthesized inside the browser, so the machine stays yours.

**Hands over on demand.** The **Take control** button stops the agent dead, checked
before every step and enforced again in the executor. Press it again to resume.

**Gives the model DevTools.** Console, network log, computed styles, box model, real
event listeners, raw CDP. Not a screenshot of the Elements panel — the protocol
underneath it.

**Sees when structure is not enough.** An optional small vision model (3–4B local,
or hosted) looks at screenshots and answers in *words*. The planner never pays image
tokens and needs no vision of its own. Fires automatically when the loop is stuck.

**Keeps a trail.** Every run records its steps, actions, repairs and the pages it
opened — with those the answer cited marked apart from those merely looked at.

**Blocks ads for real.** Genuine uBlock Origin, Manifest V2, blocking `webRequest`.

---

## Three models, by frequency

The expensive model thinks rarely; the cheap one acts often.

| tier | called | job |
|---|---|---|
| **planner** | a handful of times per run | holds the stage list and the gathered notes |
| **step model** | every step | sees the page and one stage, emits one action |
| **vision** | only when stuck | looks at a screenshot, answers in words |

All three are optional and independently configurable. Drop the planner and the step
model plans for itself; drop vision and screenshots just save to disk.

```ini
CHAMBER_ORCHESTRATOR_MODEL=gpt-5.6-luna
CHAMBER_MODEL=mimo-v2.5
CHAMBER_VISION_MODEL=qwen2.5vl:3b
CHAMBER_VISION_FALLBACK_MODEL=gpt-5.6-luna
```

---

## Three ways to drive it

**A library**, if you are embedding it:

```python
from chamber import Chamber, ChamberConfig

async with Chamber.open(ChamberConfig.from_env(profile="research")) as ch:
    await ch.goto("https://news.ycombinator.com")
    print(ch.render())                                  # what a model would see
    await ch.act({"action": "click", "ref": "e12"})
```

**A CLI agent**, if you want it to drive itself:

```bash
chamber run "what changed in Chrome 151?" --url https://duckduckgo.com
chamber watch "..."     # same, plus every model call on screen
```

**An MCP server**, if your harness is the brain:

```json
{ "mcpServers": { "chamber": { "command": "uv",
  "args": ["run", "--directory", "/path/to/chamber", "chamber", "mcp"] } } }
```

Claude Code, opencode or anything else then drives the same visible window through
the same action layer. The browser opens on the first tool call, not at startup.

---

## Commands

| | |
|---|---|
| `chamber doctor` | Check the setup, launch the browser, confirm uBO actually loaded |
| `chamber setup` | Configure the browser yourself, with the agent switched off |
| `chamber run "<task>"` | Give the agent a task and watch |
| `chamber watch "<task>"` | Same, with a live view of every model call and action |
| `chamber see <url>` | Screenshot a page and have the vision model describe it |
| `chamber trace [run-id]` | List past runs, or print one as a tree with its sources |
| `chamber open <url>` | A plain window — log in by hand; the profile keeps it |
| `chamber profiles` | List profiles with sizes; `--prune` to clean up |
| `chamber mcp` | Serve over MCP |

---

## Profiles

A profile is a whole browser profile — cookies, logins, extension settings, and any
preference you change by hand. Same `--profile` name means the same profile, every
run.

```bash
chamber setup --profile dev          # configure it, agent off
chamber open https://github.com -p dev   # log in by hand, once
chamber run "check my open PRs" -p dev   # the agent inherits the session
```

Chamber rewrites only the startup page on each launch; everything else you set by
hand wins. Keep them per-purpose — a `research` profile with no logins and a `dev`
profile carrying your app's session should not be the same profile.

---

## Documentation

- **[HOW-IT-WORKS.md](HOW-IT-WORKS.md)** — the five phases of a step, measured
  failure rates, the four layers of recovery, and a real run walked through step by
  step. Written from the trace database, not from memory.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the design decisions and the
  measurements behind them, including what is deliberately *not* built.
- **[SETUP.md](SETUP.md)** — installation, why the browser choice is constrained,
  and configuring vision.
- **[CHANGELOG.md](CHANGELOG.md)** — every version, with the bug or measurement
  that motivated it.

---

## Status

Working and verified end to end on Windows with Brave 151 and uBlock Origin 1.73.
**98.6% of 144 recorded actions succeeded.** A two-region, eight-product comparison
task completed in 24 steps with zero failed actions — including working out on its
own that amazon.com hides USD prices when the delivery region is India, and fixing
it.

Pre-1.0, and honest about it. The open items are a bounded tab pool, importance-
ordered content rendering, cross-origin iframe traversal, and a format-repair rate
that is 24% on a cheap step model against 0% on a strong one. All four are written
up in [ARCHITECTURE.md](ARCHITECTURE.md#10-what-is-not-here).

## Development

```bash
uv sync --extra mcp
uv run pytest                    # 249 tests, no browser needed
uv run ruff check src tests
uv run python scripts/smoke.py   # end-to-end browser check, opens a window
```

Never `pip install` into `.venv` — it desyncs `uv.lock`. Use `uv add`.

## Licence

MIT © mav
