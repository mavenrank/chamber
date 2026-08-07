"""Configuration and profile seeding.

The profile test is the important one: `extensions.ui.developer_mode` is what makes
uBlock Origin actually turn on, and a regression there is silent — the browser
launches fine and the extension sits disabled.
"""

from __future__ import annotations

import json

import pytest

from chamber.browser import profile as profile_mod
from chamber.config import BrowserConfig, ChamberConfig, ModelConfig


class TestConfig:
    def test_defaults_are_headed(self):
        # The whole premise is a window someone watches.
        assert ChamberConfig().browser.headless is False

    def test_profile_shortcut(self):
        assert ChamberConfig().with_profile("research").profile == "research"

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CHAMBER_PROVIDER", "anthropic")
        monkeypatch.setenv("CHAMBER_ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("CHAMBER_MODEL", "claude-sonnet-5")
        cfg = ChamberConfig.from_env()
        assert cfg.model.provider == "anthropic"
        assert cfg.model.model == "claude-sonnet-5"

    def test_autodetects_openai_when_only_that_key_is_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CHAMBER_PROVIDER", raising=False)
        monkeypatch.delenv("CHAMBER_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CHAMBER_OPENAI_API_KEY", "sk-test")
        assert ChamberConfig.from_env().model.provider == "openai"

    def test_nested_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CHAMBER_OPENAI_API_KEY", "sk-test")
        cfg = ChamberConfig.from_env(profile="x", loop__max_steps=7, model__temperature=0.5)
        assert cfg.profile == "x"
        assert cfg.loop.max_steps == 7
        assert cfg.model.temperature == 0.5

    def test_unknown_override_is_an_error(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CHAMBER_OPENAI_API_KEY", "sk-test")
        with pytest.raises(ValueError, match="unknown config key"):
            ChamberConfig.from_env(nonsense=1)

    def test_api_key_is_redacted(self):
        redacted = ModelConfig(api_key="sk-secret-abcd1234").redacted()
        assert "secret" not in str(redacted)
        assert redacted["api_key"].endswith("1234")

    def test_config_is_immutable(self):
        # frozen dataclasses raise FrozenInstanceError, a subclass of AttributeError.
        with pytest.raises(AttributeError):
            ChamberConfig().browser.profile = "other"  # type: ignore[misc]


class TestProfileSeeding:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CHAMBER_HOME", str(tmp_path))

    def _prefs(self, name: str) -> dict:
        from chamber import paths

        return json.loads(
            (paths.profile_dir(name) / "Default" / "Preferences").read_text(encoding="utf-8")
        )

    def test_enables_developer_mode(self):
        # Without this, --load-extension installs uBO and leaves it toggled off.
        profile_mod.prepare_profile("t")
        assert self._prefs("t")["extensions"]["ui"]["developer_mode"] is True

    def test_disables_session_restore(self):
        # Otherwise every run inherits the previous run's scratch tabs.
        profile_mod.prepare_profile("t")
        assert self._prefs("t")["session"]["restore_on_startup"] == 5

    def test_is_idempotent(self):
        first = profile_mod.prepare_profile("t")
        second = profile_mod.prepare_profile("t")
        assert first.seeded is True
        assert second.seeded is False

    def test_preserves_preferences_a_human_changed(self):
        from chamber import paths

        profile_mod.prepare_profile("t")
        prefs_path = paths.profile_dir("t") / "Default" / "Preferences"
        data = json.loads(prefs_path.read_text(encoding="utf-8"))
        data["profile"]["name"] = "chosen by hand"
        data["extensions"]["ui"]["developer_mode"] = False
        prefs_path.write_text(json.dumps(data), encoding="utf-8")

        profile_mod.prepare_profile("t")
        after = self._prefs("t")
        assert after["profile"]["name"] == "chosen by hand"
        # First-run-only prefs stay as the human left them...
        assert after["extensions"]["ui"]["developer_mode"] is False
        # ...but the operational ones are re-applied every launch.
        assert after["session"]["restore_on_startup"] == 5

    def test_force_reseed_restores_developer_mode(self):
        from chamber import paths

        profile_mod.prepare_profile("t")
        prefs_path = paths.profile_dir("t") / "Default" / "Preferences"
        data = json.loads(prefs_path.read_text(encoding="utf-8"))
        data["extensions"]["ui"]["developer_mode"] = False
        prefs_path.write_text(json.dumps(data), encoding="utf-8")

        profile_mod.prepare_profile("t", force_reseed=True)
        assert self._prefs("t")["extensions"]["ui"]["developer_mode"] is True

    def test_survives_a_corrupt_preferences_file(self):
        from chamber import paths

        default = paths.profile_dir("t") / "Default"
        default.mkdir(parents=True, exist_ok=True)
        (default / "Preferences").write_text("{not json at all", encoding="utf-8")
        profile_mod.prepare_profile("t")
        assert self._prefs("t")["extensions"]["ui"]["developer_mode"] is True

    def test_seed_template_is_not_mutated(self):
        # A shared mutable template would leak one profile's state into the next.
        before = json.dumps(profile_mod._SEED_PREFS, sort_keys=True)
        profile_mod.prepare_profile("a")
        profile_mod.prepare_profile("b")
        assert json.dumps(profile_mod._SEED_PREFS, sort_keys=True) == before


class TestBrowserConfig:
    def test_extensions_default_to_ubo_only(self):
        assert BrowserConfig().load_ubo is True
        assert BrowserConfig().extensions == ()
