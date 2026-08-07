"""Launching the window.

Two things here are load-bearing and easy to get wrong:

1. **Playwright disables extensions by default.** Its default argument list carries
   `--disable-extensions` and `--disable-component-extensions-with-background-pages`.
   Passing `--load-extension` without removing those gets you a browser that starts
   fine and loads nothing, with no error anywhere. Hence `ignore_default_args`.

2. **`--enable-automation` is worth dropping.** It is the flag behind the "controlled
   by automated software" infobar, it sets `navigator.webdriver`, and it is the
   cheapest signal a bot-detection script reads. Chamber is not pretending to be a
   human — the visible cursor and HUD announce exactly what it is — but tripping a
   challenge wall on every page turns every task into a captcha handoff, which helps
   nobody. Paired with `--disable-blink-features=AutomationControlled`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import BrowserContext, Playwright

from chamber.browser.discovery import BrowserBuild, discover_browser, find_ubo
from chamber.browser.profile import ProfileInfo, prepare_profile
from chamber.config import BrowserConfig

log = logging.getLogger(__name__)

# Playwright sets these; they defeat extension loading, advertise automation, or
# turn off a protection we want on.
_DROP_DEFAULT_ARGS = (
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--enable-automation",
    "--disable-component-update",
    # Playwright disables the renderer sandbox by default — sensible for throwaway
    # CI containers, wrong here. Chamber drives a *persistent profile* that holds
    # the user's real cookies and logins, against arbitrary websites, on their own
    # machine. The sandbox is the boundary that stops a compromised renderer
    # reaching any of that, and Brave shows a standing warning while it is off.
    # Windows and macOS sandbox fine as a normal user; the only environment that
    # genuinely needs it disabled is running as root in a container, which is not
    # what this is for.
    "--no-sandbox",
)


def _base_args(cfg: BrowserConfig) -> list[str]:
    width, height = cfg.viewport
    args = [
        # A maximised window is not cosmetic: more of the page is in-viewport, so
        # more controls are immediately actionable and a screenshot carries more
        # of the page to the vision model. Both layers are viewport-limited.
        "--start-maximized" if cfg.maximized else f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--disable-blink-features=AutomationControlled",
        # Popups and modals that would cover the page the agent is reading.
        "--disable-notifications",
        "--disable-search-engine-choice-screen",
        "--disable-features=Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
        # Crash-report plumbing has no consumer here.
        "--disable-breakpad",
        "--disable-backgrounding-occluded-windows",
        # A background tab that gets its timers throttled looks "stuck" to the
        # readiness detector for reasons that have nothing to do with the page.
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        # Brave-only; ignored elsewhere.
        "--disable-brave-update",
    ]
    if cfg.devtools_panel:
        args.append("--auto-open-devtools-for-tabs")
    args.extend(cfg.extra_args)
    return args


@dataclass(slots=True)
class LaunchResult:
    context: BrowserContext
    build: BrowserBuild
    profile: ProfileInfo
    extensions: tuple[Path, ...]


def resolve_extensions(cfg: BrowserConfig) -> tuple[Path, ...]:
    """Explicit extensions first, then uBO if it has been installed and is wanted."""
    found = list(cfg.extensions)
    if cfg.load_ubo:
        ubo = find_ubo()
        if ubo and ubo not in found:
            found.append(ubo)
        elif not ubo:
            log.info(
                "uBlock Origin not installed — run `uv run python scripts/install_ubo.py`"
            )
    return tuple(found)


async def launch(pw: Playwright, cfg: BrowserConfig) -> LaunchResult:
    """Bring up a persistent, headed context on a chamber-owned profile."""
    build = discover_browser(cfg.executable_path)
    extensions = resolve_extensions(cfg)

    if extensions and not build.mv2:
        log.warning(
            "%s cannot run Manifest V2 extensions. %s",
            build.label,
            build.note or "uBlock Origin will silently fail to load.",
        )
    if extensions and cfg.headless:
        # Chromium's old headless mode drops extensions entirely. The newer headless
        # keeps them, but the whole point of chamber is a window a human watches, so
        # this is a warning rather than a supported configuration.
        log.warning("headless mode: extension support is unreliable, uBO may not load")

    profile = prepare_profile(cfg.profile, extensions)

    args = _base_args(cfg)
    if extensions:
        joined = ",".join(str(p) for p in extensions)
        args += [f"--disable-extensions-except={joined}", f"--load-extension={joined}"]

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile.user_data_dir),
        executable_path=str(build.path),
        headless=cfg.headless,
        args=args,
        ignore_default_args=list(_DROP_DEFAULT_ARGS),
        # The OS window is the viewport. Forcing an emulated viewport on a headed
        # window leaves a mismatched letterbox that makes coordinates confusing to
        # anyone watching.
        no_viewport=True,
        locale=cfg.locale,
        timezone_id=cfg.timezone,
        slow_mo=cfg.slow_mo_ms or None,
        # Downloads land in the run directory rather than the user's Downloads.
        accept_downloads=True,
    )

    log.info(
        "launched %s | profile=%s | extensions=%d",
        build.label,
        profile.name,
        len(extensions),
    )
    return LaunchResult(context, build, profile, extensions)


async def extension_report(context: BrowserContext) -> list[dict[str, object]]:
    """What actually loaded, read from the browser's own extensions page.

    Worth calling once at startup: "I passed --load-extension" and "the extension is
    running" are different claims, and only the second one matters. Reads through
    the page's shadow DOM because chrome://extensions is entirely custom elements.
    """
    page = await context.new_page()
    try:
        scheme = "brave" if "brave" in (context.browser.browser_type.name or "") else "chrome"
        for candidate in (f"{scheme}://extensions/", "chrome://extensions/"):
            try:
                await page.goto(candidate, wait_until="domcontentloaded", timeout=8000)
                break
            except Exception:
                continue
        await page.wait_for_timeout(600)
        return await page.evaluate(
            """() => {
              const mgr = document.querySelector('extensions-manager');
              if (!mgr || !mgr.shadowRoot) return [];
              const list = mgr.shadowRoot.querySelector('extensions-item-list');
              if (!list || !list.shadowRoot) return [];
              return [...list.shadowRoot.querySelectorAll('extensions-item')].map(it => {
                const s = it.shadowRoot;
                return {
                  id: it.id,
                  name: s.querySelector('#name')?.textContent?.trim() ?? null,
                  version: s.querySelector('#version')?.textContent?.trim() ?? null,
                  enabled: !!s.querySelector('#enableToggle')?.checked,
                  warning: s.querySelector('#warnings')?.textContent?.trim() || null,
                };
              });
            }"""
        )
    finally:
        await page.close()
