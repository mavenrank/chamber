"""Profile preparation — the part that makes uBlock Origin actually turn on.

The thing that trips people up: Brave 151 *accepts* uBO's Manifest V2 without
complaint, but any unpacked extension loaded via `--load-extension` stays toggled
off behind "Turn on developer mode to use this extension." That is a profile
preference, not a browser capability, and chamber owns the profile — so it seeds
`extensions.ui.developer_mode = true` into `Default/Preferences` before the first
launch and the extension comes up enabled.

Preferences must be written while the browser is *not* running: Chromium holds the
file in memory and rewrites it on exit, so a live edit is silently discarded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from chamber import paths

log = logging.getLogger(__name__)

# Rewritten on *every* launch. These are operational, not preferences a human would
# have an opinion about — and one of them is load-bearing.
#
# Session restore is the load-bearing one. A persistent profile is the whole point
# (log in once, the agent inherits it), but Chromium also restores the *tabs* from
# last time, so every run inherits the previous run's scratch tabs and the window
# fills up with junk. Cookies and logins live in separate files and are untouched
# by this: 5 = "start on the new tab page".
_ALWAYS_PREFS: dict[str, object] = {
    "session": {"restore_on_startup": 5, "startup_urls": []},
    "profile": {"exit_type": "Normal", "exited_cleanly": True},
}

# Seeded on first launch only, then left alone so a preference you change by hand
# in the browser survives. Nothing here bypasses a security decision.
_SEED_PREFS: dict[str, object] = {
    # The one that matters — without it --load-extension installs but never enables.
    "extensions": {"ui": {"developer_mode": True}},
    # Keep the window free of modals that would sit on top of the agent's work.
    "browser": {
        "has_seen_welcome_page": True,
        "check_default_browser": False,
        "window_placement": {"maximized": False},
    },
    "profile": {
        "exit_type": "Normal",
        "exited_cleanly": True,
        # Chamber's own network posture is explicit; leave prediction off so the
        # request log reflects what the page actually asked for.
        "network_prediction_options": 2,
        "default_content_setting_values": {
            # Notification prompts are pure interruption for an automated session.
            "notifications": 2,
            "geolocation": 2,
        },
    },
    # Autofill and password prompts are modal interruptions that land on top of
    # whatever the agent is reading. Saved logins already in the profile still
    # work; this only stops the "save this password?" bubble.
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
    "payments": {"can_make_payment_enabled": False},
    "translate": {"enabled": False},
    "bookmark_bar": {"show_on_all_tabs": False},
    # Brave-specific. Harmless on non-Brave builds — Chromium ignores unknown
    # preference keys rather than rejecting the file. All of this is surface the
    # agent never uses and that costs memory, network and screen space on every
    # new tab.
    "brave": {
        "rewards": {"enabled": False, "show_brave_rewards_button_in_location_bar": False},
        "onboarding": {"last_shielded_time": 1, "last_shown_time": 1},
        "stats": {"reporting_enabled": False},
        "p3a": {"enabled": False, "notice_acknowledged": True},
        "brave_vpn": {"show_button": False},
        "wallet": {"default_wallet2": 0, "show_wallet_icon_on_toolbar": False},
        "brave_ai_chat": {"show_leo_icon_in_location_bar": False},
        "sidebar": {"sidebar_show_option": 3},  # 3 = never
        "today": {"opted_in": False, "should_show_toolbar_button": False},
        "new_tab_page": {
            "show_background_image": False,
            "show_branded_background_image": False,
            "show_brave_news": False,
            "show_stats": False,
            "show_rewards": False,
            "show_together": False,
            "hide_all_widgets": True,
        },
        "speedreader": {"enabled": False},
        "talk": {"show_button": False},
    },
}


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    name: str
    user_data_dir: Path
    seeded: bool
    extensions: tuple[Path, ...]


def _copy(d: dict) -> dict:
    """Deep copy, so merging never mutates the module-level templates."""
    return json.loads(json.dumps(d))


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Overlay wins on scalars, recurses on dicts. Does not touch keys it has no
    opinion about — the point is to leave a real profile's history intact."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def prepare_profile(
    name: str = "default",
    extensions: tuple[Path, ...] = (),
    *,
    force_reseed: bool = False,
) -> ProfileInfo:
    """Create (or adopt) a profile directory and make sure extensions can enable.

    Idempotent. On an existing profile it only re-seeds when `force_reseed` is set,
    so a preference you deliberately changed in the browser UI survives restarts.
    """
    user_data_dir = paths.profile_dir(name)
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"

    marker = user_data_dir / ".chamber-seeded"
    first_run = not marker.exists() or force_reseed

    existing: dict = {}
    if prefs_path.is_file():
        try:
            existing = json.loads(prefs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt Preferences file is Chromium's own recoverable case — it
            # rebuilds from defaults. Overwriting is strictly better than crashing.
            log.warning("unreadable Preferences at %s (%s); rewriting", prefs_path, exc)
            existing = {}

    merged = _deep_merge(existing, _copy(_ALWAYS_PREFS))
    if first_run:
        merged = _deep_merge(merged, _copy(_SEED_PREFS))
    prefs_path.write_text(json.dumps(merged), encoding="utf-8")

    if not first_run:
        return ProfileInfo(name, user_data_dir, seeded=False, extensions=extensions)

    # "Local State" lives at the user-data-dir root and holds browser-wide settings.
    local_state = user_data_dir / "Local State"
    state: dict = {}
    if local_state.is_file():
        try:
            state = json.loads(local_state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    _deep_merge(
        state,
        {
            "browser": {"enabled_labs_experiments": []},
            "user_experience_metrics": {"reporting_enabled": False},
        },
    )
    local_state.write_text(json.dumps(state), encoding="utf-8")

    marker.write_text(
        json.dumps({"seeded_prefs": sorted(_SEED_PREFS)}, indent=2), encoding="utf-8"
    )
    log.info("seeded profile %r at %s", name, user_data_dir)
    return ProfileInfo(name, user_data_dir, seeded=True, extensions=extensions)


def profile_is_running(name: str = "default") -> bool:
    """Best-effort check for a live browser on this profile.

    Chromium keeps a `SingletonLock` while running. On Windows it is a plain file
    that a hard kill can leave behind, so this is a hint for a clearer error
    message, never a hard gate.
    """
    return (paths.profile_dir(name) / "SingletonLock").exists()
