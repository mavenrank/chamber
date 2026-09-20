# Setup

Three commands, then a check. The only part that needs explaining is the browser.

```bash
uv sync --extra mcp
uv run python scripts/install_ubo.py
cp .env.example .env        # then put your API key in it
uv run chamber doctor
```

`doctor` launches the browser and reports what actually loaded, which is the only
check that matters — "I passed `--load-extension`" and "the extension is running"
are different claims.

The default run display is Chamber Desk, a bundled React/Vite window. Its source is
under `src/chamber/display/web`; the checked-in production bundle is loaded by the
Python host, so normal users do not need Node after installation. During display
development, rebuild it with:

```bash
cd src/chamber/display/web
npm install
npm run build
```

---

## 1. Python

Everything goes through `uv`. It creates and manages `.venv` itself: no
`python -m venv`, no `activate`, no `pip`.

```bash
uv python install 3.12   # only if you don't have it; uv reads .python-version
uv sync --extra mcp      # drop --extra mcp if you only want the CLI and library
```

Pinned to 3.12 by `.python-version` and enforced by `requires-python`.

## 2. The browser

**Chamber needs a Chromium build that still runs Manifest V2 extensions.** It
autodetects Brave, then ungoogled-chromium, then plain Chromium.

If you have Brave installed, you are done — skip to step 3.

### Why this constraint exists

Real uBlock Origin is built on blocking `webRequest`. Manifest V3 removed that API,
which is why uBO Lite exists as a separate, much weaker product rather than as an
update — it is not a port, it is a different extension.

Google Chrome stopped running MV2 on stable in 138, the enterprise policy expired
with 139, and 151 deleted the last flags. The rejection happens at manifest-parse
time, so **where you get the extension from makes no difference** — Web Store,
GitHub zip, unpacked, `--load-extension`, developer mode all hit the same check. You
also cannot rewrite the manifest, because the thing being removed is the API the
extension is built on.

### What actually works

Measured on **Brave 151.1.93.132** with **uBO 1.73.0**, 2026-08-06:

| | |
|---|---|
| Manifest accepted | ✅ no version rejection |
| Extension enabled | ✅ |
| MV2 background page running | ✅ |
| `chrome.webRequest.onBeforeRequest` | ✅ available |
| Blocking listeners installed | ✅ `vAPI.net` live |

The one catch: Brave loads an unpacked extension but leaves it toggled off behind
*"Turn on developer mode to use this extension."* That is a **profile preference**,
not a browser capability — and chamber owns the profile, so it seeds
`extensions.ui.developer_mode` into `Default/Preferences` before the first launch
and the extension comes up enabled. Nothing manual, nothing clicked.

ungoogled-chromium works too — it carries its own `extensions-manifestv2.patch`.
Download from
<https://ungoogled-software.github.io/ungoogled-chromium-binaries/>, unpack
somewhere stable, and either let autodetection find it or set the path:

```bash
CHAMBER_BROWSER="C:\Users\you\AppData\Local\ungoogled-chromium\chrome.exe"
```

Verify you have the right build at `chrome://version` — it should say Chromium, not
Google Chrome.

## 3. uBlock Origin

```bash
uv run python scripts/install_ubo.py
```

Pulls `uBlock0_<version>.chromium.zip` from gorhill/uBlock's GitHub releases into
`~/.chamber/extensions/`, and **asserts `manifest_version == 2` and the presence of
`webRequestBlocking`** before installing. That check is there so you cannot quietly
end up with Lite.

Not the Chrome Web Store: it serves Lite, and it delists MV2 extensions entirely on
**2026-08-31**.

## 4. The model

```bash
cp .env.example .env
```

Chamber speaks two protocols and autodetects from whichever key is present.

**Official OpenAI** — Responses API, GPT-5.6 Luna at medium reasoning:

```ini
CHAMBER_BACKEND=openai
CHAMBER_OFFICIAL_OPENAI_API_KEY=sk-...
CHAMBER_MODEL=gpt-5.6-luna
CHAMBER_REASONING_EFFORT=medium
CHAMBER_OPENAI_API_STYLE=responses
```

`CHAMBER_OPENAI_API_KEY` is an API Platform credential. ChatGPT Plus includes
access to OpenAI products such as Codex, but it is not a general API credential
for a custom HTTP client and API usage is billed separately.

**OpenCode Zen** — limited-time free MiMo option, plus OpenRouter, Together, vLLM,
LM Studio, and Ollama:

```ini
CHAMBER_OPENAI_BASE_URL=https://opencode.ai/zen/v1
OPENCODE_API_KEY=sk-...
CHAMBER_MODEL=mimo-v2.5-free
```

OpenCode lists `mimo-v2.5-free` as free for a limited time; review its current
privacy terms before sending sensitive data.

The two paths are independent and both keys can remain in `.env`.
`CHAMBER_BACKEND=openai` selects `CHAMBER_OFFICIAL_OPENAI_API_KEY`;
`CHAMBER_BACKEND=opencode` selects `OPENCODE_API_KEY` (or the legacy
`CHAMBER_OPENAI_API_KEY`). Set `CHAMBER_OPENAI_BASE_URL` for another compatible
service.

**Anthropic native:**

```ini
CHAMBER_ANTHROPIC_API_KEY=sk-ant-...
CHAMBER_MODEL=claude-sonnet-5
```

Tool calling is off by default for OpenAI-compatible endpoints and on for Anthropic.
Cheap models frequently advertise tool support and then emit malformed arguments;
the JSON-in-text path plus the format-teaching prompt is measurably more reliable
for them. Override with `CHAMBER_TOOL_CALLING=true`.

No key is needed for `chamber open`, `chamber doctor`, or the MCP server — in that
last case the model lives in whichever harness is connecting.

## 5. Vision (optional)

A small local model that looks at screenshots and answers in words. The planner
stays text-only and reads the description — so it works even with a planner that
has no vision at all, and never pays image tokens.

```bash
ollama pull qwen2.5vl:3b
```

```ini
CHAMBER_VISION_MODEL=qwen2.5vl:3b
CHAMBER_VISION_BASE_URL=http://localhost:11434/v1
```

Check it, including *which* GPU it landed on:

```bash
uv run chamber doctor
```

```
✓  vision       qwen2.5vl:3b @ http://localhost:11434/v1
✓  vision GPU   2.16 GB, 100% resident in VRAM on NVIDIA GeForce RTX 4060 Laptop GPU
```

That last line matters on a laptop with both a discrete GPU and an integrated one.
Ollama's `/api/ps` reports `size_vram`, which only distinguishes GPU memory from
system RAM — it does not say which GPU. `doctor` asks `nvidia-smi` whether the
inference process is actually on the NVIDIA device, rather than inferring it.

Benchmark a model against real pages before trusting it:

```bash
uv run chamber see https://example.com --ask "what is the main heading?"
uv run chamber see <url> --model gemma3:4b --scale 1.0
```

`see` reports latency and image size, which is what decides whether a model is
usable in a loop.

### Three things that were measured, not assumed

**Cold loads cost ~90 seconds.** On an RTX 4060 Laptop, qwen2.5vl:3b took 107s cold
against 19s warm. Ollama also evicts after 5 minutes idle, and the gaps between
vision calls in an agent loop routinely exceed that. Chamber preloads the model at
run start and sends `keep_alive=30m` on every request, so it stays resident.

**Scaling screenshots is a free win.** Vision cost scales with image *area*, and
Qwen2.5-VL tiles its input dynamically. On the same page and question:

| scale | time | size | answer |
|---|---|---|---|
| 1.0 | 59.9s | 667 KB | "Yes, there is an Add to Cart button… Currently unavailable" — self-contradictory |
| 0.6 | 18.7s | 319 KB | "There is **no** Add to Cart button… marked **Currently unavailable**" — correct |

3.2× faster *and* more accurate. `vision_scale` defaults to 0.6; the resize happens
in the browser compositor via CDP, so it costs no dependency and no extra encode.

**Text gets two chances first.** At `stuck_after` (3) steps with no page change the
planner is nudged to read more of the page; only at `vision_after` (5) is the vision
model consulted. The structural view usually does hold the answer, and a local look
costs ~19s.

## 6. Profiles

A profile is a whole browser profile: cookies, logins, extension settings, history,
and **any preference you change by hand**. They live in
`~/.chamber/profiles/<name>/` and are never committed.

### They persist, fully

Same `--profile` name means the same profile, every run. Verified end to end:
cookies, `localStorage` and hand-edited `Preferences` all survive across sessions.

```bash
chamber open https://github.com --profile dev   # log in by hand, once
chamber run "check my open PRs" --profile dev   # the agent inherits the session
```

**To customise the browser**, open the profile with no model attached, change
whatever you like in Brave's own settings, and close the window:

```bash
chamber open --profile default
```

Everything sticks. Chamber only rewrites two things on each launch —
`session.restore_on_startup` and the clean-exit markers — because the agent's
scratch tabs coming back from the previous run is never wanted. Everything else it
seeds is **first-run only**, so a preference you set by hand always wins over a
default chamber picked.

New profiles start with the obvious bloat already off: Brave Rewards, Wallet, VPN
button, Brave News, Leo, the sidebar, new-tab background images and widgets, plus
Chromium's autofill and password-save prompts. Those are first-run defaults — turn
any of them back on by hand and it stays on.

### They cost disk

A working profile runs 100–300 MB, so one-off `--profile` names add up:

```bash
chamber profiles              # list with sizes, and which hold logins
chamber profiles --prune      # delete all but 'default'
chamber profiles --remove foo # delete one
```

Keep them per-purpose rather than per-run. A `research` profile with no logins and
a `dev` profile carrying your app's session should not be the same profile — you do
not want open-ended browsing happening inside a logged-in session.

## 7. Checking it worked

```bash
uv run chamber doctor
```

```
  ✓  browser        brave — C:\Program Files\BraveSoftware\...\brave.exe
  ✓  manifest v2    this build can run real uBlock Origin
  ✓  uBlock Origin  1.73.0 (manifest v2) — C:\Users\you\.chamber\extensions\...
  ✓  model          openai/mimo-v2.5 @ https://opencode.ai/zen/go/v1

  ✓ uBlock Origin 1.73.0 — enabled
```

If the extension shows **disabled**, developer mode did not stick. Force a reseed:

```python
from chamber.browser.profile import prepare_profile
prepare_profile("default", force_reseed=True)
```

If nothing loads at all, the browser is the problem, not the extension — go back to
step 2.

For a full end-to-end check including extraction, the overlay and a real click:

```bash
uv run python scripts/smoke.py
```

## Common commands

```bash
uv run pytest                    # tests, no browser needed
uv run ruff check src tests
uv add <package>                 # updates pyproject + uv.lock together
uv sync --upgrade                # refresh the lockfile
```

Never `pip install` into `.venv` directly — it desyncs `uv.lock`, and the next
`uv sync` silently undoes it.
