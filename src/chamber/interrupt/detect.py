"""Spotting the moment a human has to take over.

Chamber never solves a challenge. That is a deliberate limit, not a missing
feature: the human is sitting in front of this window anyway, so the handoff costs
them four seconds and keeps the whole system on the right side of every site's
terms. Detection exists so the handoff is *prompt* — an agent that grinds through
six confused steps against a Cloudflare interstitial before giving up is worse than
one that asks immediately.

Detection is structural rather than visual. Challenge widgets are third-party
iframes with stable origins (`google.com/recaptcha`, `hcaptcha.com`,
`challenges.cloudflare.com`), and that origin is a far better signal than anything
in the rendered text — it survives translation, restyling and A/B tests.

Login and payment walls are detected too. They are not bot checks, but they need
the same response: stop, surface the window, and let the person decide. An agent
should never be typing credentials or confirming a purchase on someone's behalf.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from playwright.async_api import Error as PWError
from playwright.async_api import Page

log = logging.getLogger(__name__)


class ChallengeKind(StrEnum):
    NONE = "none"
    CAPTCHA = "captcha"
    INTERSTITIAL = "interstitial"  # Cloudflare / Akamai "checking your browser"
    LOGIN = "login"
    PAYMENT = "payment"
    RATE_LIMIT = "rate_limit"
    BLOCKED = "blocked"  # 403 / "access denied"


@dataclass(slots=True)
class Challenge:
    kind: ChallengeKind
    detail: str = ""
    vendor: str = ""
    confidence: float = 0.0

    @property
    def blocking(self) -> bool:
        return self.kind is not ChallengeKind.NONE

    @property
    def needs_human(self) -> bool:
        """Everything except a rate limit, which is a waiting problem, not a
        person problem."""
        return self.blocking and self.kind is not ChallengeKind.RATE_LIMIT

    def prompt(self) -> str:
        """What to say to the human, in the banner and on the console."""
        return {
            ChallengeKind.CAPTCHA: "Please solve the captcha in this tab, then I'll carry on.",
            ChallengeKind.INTERSTITIAL: "The site is running a bot check. Let it finish, or click through it.",
            ChallengeKind.LOGIN: "This page needs you to sign in. I won't touch your credentials.",
            ChallengeKind.PAYMENT: "This is a payment step — that one is yours, not mine.",
            ChallengeKind.RATE_LIMIT: "The site is rate-limiting us. Waiting before I retry.",
            ChallengeKind.BLOCKED: "The site refused the request. You may need to open it yourself.",
        }.get(self.kind, "I need a hand with this page.")


# Iframe origins are the strongest signal — they do not change with locale or theme.
_VENDOR_FRAMES: tuple[tuple[str, str], ...] = (
    ("google.com/recaptcha", "reCAPTCHA"),
    ("recaptcha.net", "reCAPTCHA"),
    ("hcaptcha.com", "hCaptcha"),
    ("challenges.cloudflare.com", "Cloudflare Turnstile"),
    ("arkoselabs.com", "Arkose / FunCaptcha"),
    ("funcaptcha.com", "Arkose / FunCaptcha"),
    ("geetest.com", "GeeTest"),
    ("perimeterx.net", "PerimeterX"),
    ("px-cdn.net", "PerimeterX"),
    ("datadome.co", "DataDome"),
)

_DETECT_JS = """
() => {
  const frames = [...document.querySelectorAll("iframe")]
    .map(f => f.src || "")
    .filter(Boolean);

  const body = (document.body?.innerText || "").slice(0, 4000);
  const low = body.toLowerCase();

  const has = (sel) => !!document.querySelector(sel);

  return {
    frames,
    title: document.title || "",
    text: body.slice(0, 600),
    low,
    // Password fields are the load-bearing login signal — text saying "sign in"
    // appears in the header of half the web.
    passwordFields: document.querySelectorAll('input[type=password]').length,
    cardFields: document.querySelectorAll(
      'input[autocomplete*="cc-"], input[name*="card" i], input[id*="card" i][type!=checkbox]'
    ).length,
    captchaWidgets: has('.g-recaptcha, .h-captcha, #cf-challenge-running, .cf-turnstile, [data-sitekey]'),
    challengeForm: has('form#challenge-form, #challenge-stage, #cf-please-wait'),
    bodyLength: (document.body?.innerText || "").trim().length,
  };
}
"""

# Phrases that only appear on a wall. Kept short and specific — "verify" and
# "robot" on their own match far too much ordinary copy.
_INTERSTITIAL_PHRASES = (
    "checking your browser",
    "verifying you are human",
    "verify you are human",
    "just a moment",
    "please wait while we verify",
    "enable javascript and cookies to continue",
    "ddos protection by",
)
_BLOCKED_PHRASES = (
    "access denied",
    "you have been blocked",
    "sorry, you have been blocked",
    "403 forbidden",
    "unusual traffic from your computer",
    "automated queries",
)
_RATE_PHRASES = (
    "too many requests",
    "rate limit exceeded",
    "429",
    "slow down",
)


async def detect_challenge(page: Page, *, http_status: int | None = None) -> Challenge:
    """Look for anything that means "stop and ask".

    Cheap enough to run after every navigation: one evaluate, no waiting.
    """
    try:
        probe = await page.evaluate(_DETECT_JS)
    except PWError:
        return Challenge(ChallengeKind.NONE)

    frames: list[str] = probe.get("frames", [])
    low: str = probe.get("low", "")
    title_low = (probe.get("title") or "").lower()

    # 1. Vendor iframes — highest confidence available.
    for needle, vendor in _VENDOR_FRAMES:
        if any(needle in f for f in frames):
            return Challenge(
                ChallengeKind.CAPTCHA,
                detail=f"A {vendor} challenge is on the page.",
                vendor=vendor,
                confidence=0.95,
            )

    # 2. Cloudflare's own challenge scaffolding.
    if probe.get("challengeForm"):
        return Challenge(
            ChallengeKind.INTERSTITIAL,
            detail="A Cloudflare challenge page is showing.",
            vendor="Cloudflare",
            confidence=0.9,
        )

    # 3. A captcha widget with no iframe yet — it has not finished loading.
    if probe.get("captchaWidgets"):
        return Challenge(
            ChallengeKind.CAPTCHA,
            detail="A captcha widget is present on the page.",
            confidence=0.75,
        )

    # 4. Interstitial copy on a page with almost no content. Both halves matter:
    #    an article *about* Cloudflare would match the phrase but has plenty of text.
    if probe.get("bodyLength", 0) < 900 and any(p in low or p in title_low for p in _INTERSTITIAL_PHRASES):
        return Challenge(
            ChallengeKind.INTERSTITIAL,
            detail="The page is showing a bot check instead of content.",
            confidence=0.8,
        )

    # The length check matters as much as the phrase: a full page that merely
    # mentions "access denied" is not a wall.
    if (http_status in (403, 401) or any(p in low for p in _BLOCKED_PHRASES)) and probe.get(
        "bodyLength", 0
    ) < 1500:
        return Challenge(
                ChallengeKind.BLOCKED,
                detail=f"The site refused the request{f' (HTTP {http_status})' if http_status else ''}.",
                confidence=0.8,
            )

    if (http_status == 429 or any(p in low for p in _RATE_PHRASES)) and probe.get(
        "bodyLength", 0
    ) < 1500:
        return Challenge(
                ChallengeKind.RATE_LIMIT,
                detail="The site is rate-limiting requests.",
                confidence=0.7,
            )

    # 5. Payment before login: a card field is a harder stop than a password field,
    #    and checkout pages have both.
    if probe.get("cardFields", 0) >= 2:
        return Challenge(
            ChallengeKind.PAYMENT,
            detail="This page is asking for card details.",
            confidence=0.85,
        )

    if probe.get("passwordFields", 0) >= 1:
        return Challenge(
            ChallengeKind.LOGIN,
            detail="This page has a password field.",
            confidence=0.7,
        )

    return Challenge(ChallengeKind.NONE)
