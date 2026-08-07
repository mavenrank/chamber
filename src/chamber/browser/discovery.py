"""Find a Chromium-family browser that can still run Manifest V2 extensions.

Why this module exists at all: the browser choice is not interchangeable here. Real
uBlock Origin is built on blocking `webRequest`, which Manifest V3 removed — that is
why uBO Lite exists as a separate, weaker product instead of as an update. Google
Chrome stopped running MV2 on stable in 138 and deleted the last flags by 151, and
the rejection happens at manifest-parse time, so where you get the extension from
makes no difference.

Measured on Brave 151.1.93.132 (Chromium 151), 2026-08-06: uBO 1.73.0 MV2 loads,
enables, its background page runs, and `chrome.webRequest.onBeforeRequest` with
blocking listeners is live. That is the whole reason Brave is first in the search
order. ungoogled-chromium carries its own `extensions-manifestv2.patch` and is the
fallback. Google Chrome is listed last and flagged, because it will load the browser
fine and silently refuse the extension.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from chamber import paths


@dataclass(frozen=True, slots=True)
class BrowserBuild:
    path: Path
    family: str  # brave | ungoogled-chromium | chromium | chrome
    mv2: bool  # can it run Manifest V2 extensions?
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.family} ({self.path.name})"


# Ordered by preference. First hit wins.
_CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LOCALAPPDATA%\ungoogled-chromium\chrome.exe",
        r"%ProgramFiles%\ungoogled-chromium\chrome.exe",
        r"%ProgramFiles%\Chromium\Application\chrome.exe",
        r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    ],
    "darwin": [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ],
    "linux": [
        "/usr/bin/brave-browser",
        "/usr/bin/brave",
        "/opt/brave.com/brave/brave-browser",
        "/usr/bin/ungoogled-chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ],
}


def _classify(path: Path) -> BrowserBuild:
    name = str(path).lower()
    if "brave" in name:
        return BrowserBuild(path, "brave", mv2=True)
    if "ungoogled" in name:
        return BrowserBuild(path, "ungoogled-chromium", mv2=True)
    if "google" in name and "chrome" in name:
        return BrowserBuild(
            path,
            "chrome",
            mv2=False,
            note=(
                "Google Chrome refuses Manifest V2 at parse time since 138 and has no "
                "flag left to re-enable it. uBlock Origin will not load. Install Brave "
                "or ungoogled-chromium, or set CHAMBER_BROWSER explicitly to accept this."
            ),
        )
    # Bare "Chromium" builds vary; assume no MV2 unless it is the ungoogled fork.
    return BrowserBuild(
        path,
        "chromium",
        mv2=False,
        note="Plain Chromium build — MV2 support depends on how it was compiled. Untested.",
    )


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def discover_browser(explicit: Path | None = None) -> BrowserBuild:
    """Locate a usable browser, or raise with actionable guidance.

    An explicit path is trusted and never rejected — if you point chamber at Chrome
    on purpose, it launches Chrome and warns about the extension instead of refusing.
    """
    if explicit:
        p = Path(os.path.expandvars(str(explicit))).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"CHAMBER_BROWSER points at a missing file: {p}")
        return _classify(p)

    for raw in _CANDIDATES[_platform_key()]:
        p = Path(os.path.expandvars(raw)).expanduser()
        if p.exists():
            return _classify(p)

    for exe in ("brave", "brave-browser", "chromium", "chromium-browser"):
        found = shutil.which(exe)
        if found:
            return _classify(Path(found))

    raise FileNotFoundError(
        "No Chromium-family browser found.\n"
        "Chamber needs one that still runs Manifest V2 extensions, because real\n"
        "uBlock Origin depends on blocking webRequest.\n\n"
        "  Recommended: Brave      https://brave.com/download/\n"
        "  Alternative: ungoogled-chromium\n"
        "               https://ungoogled-software.github.io/ungoogled-chromium-binaries/\n\n"
        "Then either let chamber autodetect it, or set CHAMBER_BROWSER to the executable."
    )


def find_ubo() -> Path | None:
    """The unpacked uBlock Origin directory, if `scripts/install_ubo.py` has run.

    Returns the directory containing `manifest.json` — that is what
    `--load-extension` wants, not the parent.
    """
    root = paths.extensions_dir()
    for candidate in sorted(root.glob("*")):
        name = candidate.name.lower()
        if (candidate / "manifest.json").is_file() and ("ublock" in name or "ubo" in name):
            return candidate
    return None
