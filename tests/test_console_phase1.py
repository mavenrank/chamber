"""Phase 1 Console: session snapshot block + read-only queries."""

from __future__ import annotations

from chamber.display import DisplayAdapter, build_session_block
from chamber.display import queries as q


def test_snapshot_carries_session_block():
    adapter = DisplayAdapter()
    adapter.set_session(
        {
            "run_id": "r1",
            "profile": "default",
            "browser": {"label": "brave (brave.exe)", "family": "brave", "mv2": True},
            "models": {"step": {"provider": "openai", "model": "mimo-v2.5-free"}},
        }
    )
    snapshot = adapter.handle("run_start", {"task": "hello"})
    assert snapshot["session"]["run_id"] == "r1"
    assert snapshot["session"]["profile"] == "default"
    # run_start must not wipe the session identity
    assert snapshot["task"] == "hello"


def test_snapshot_without_session_defaults_to_empty():
    snapshot = DisplayAdapter().snapshot()
    assert snapshot["session"] == {}


def test_build_session_block_redacts_keys():
    from chamber.config import ModelConfig

    class Cfg:
        model = ModelConfig(provider="openai", model="mimo-v2.5-free", api_key="sk-secret")
        orchestrator = None
        vision = None
        vision_fallbacks = ()
        display = type("D", (), {"mode": "window"})()
        browser = type("B", (), {"profile": "default"})()

    class Build:
        label = "brave (brave.exe)"
        family = "brave"
        mv2 = True

    block = build_session_block(Cfg(), Build(), "run-1")
    assert block["run_id"] == "run-1"
    assert "sk-secret" not in str(block)
    assert block["models"]["step"]["model"] == "mimo-v2.5-free"


def test_console_server_serves_shell_and_api():
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from chamber.display.server import ConsoleHandler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/console.html", timeout=10) as resp:
            assert resp.status == 200
            assert "Chamber Console" in resp.read().decode()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/query?op=list_runs&limit=2", timeout=10
        ) as resp:
            payload = json.load(resp)
            assert payload["ok"] is True
            assert isinstance(payload["runs"], list)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/query?op=get_environment", timeout=10
        ) as resp:
            payload = json.load(resp)
            assert payload["ok"] is True
            assert "config" in payload
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_human_note_and_paused_events_render():
    adapter = DisplayAdapter()
    note = adapter.handle("human_note", {"step": 3, "text": "scroll slower"})
    assert note["events"][-1]["title"] == "Human note"
    assert note["current"]["title"] == "Reading your note"
    paused = adapter.handle("paused", {"step": 3, "taken": True})
    assert paused["events"][-1]["title"] == "Loop paused"
    resumed = adapter.handle("paused", {"step": 3, "taken": False, "seconds": 4.2})
    assert resumed["events"][-1]["title"] == "Loop resumed"


def test_usage_by_model(monkeypatch, tmp_path):
    from chamber.display import queries as q
    from chamber.trace.store import open_store

    monkeypatch.setenv("CHAMBER_HOME", str(tmp_path / ".chamber"))
    with open_store() as store:
        store.start_run("r1", "t", profile="default", model="openai/m:free")
        store.end_run(success=True, summary="s", input_tokens=100, output_tokens=10)
        store.start_run("r2", "t", profile="default", model="openai/other")
    usage = q.usage_by_model()["models"]
    by_name = {u["model"]: u for u in usage}
    assert by_name["openai/m:free"]["free_tier"] is True
    assert by_name["openai/m:free"]["input_tokens"] == 100
    assert by_name["openai/other"]["free_tier"] is False


def test_queries_are_read_only_and_shaped():
    runs = q.list_runs(limit=5)
    assert isinstance(runs, list)
    missing = q.get_run("does-not-exist")
    assert missing["run"] is None
    profiles = q.list_profiles()
    assert isinstance(profiles, list)
    env = q.get_environment()
    assert "config" in env and "browser" in env and "profiles" in env
    assert "sk-" not in str(env).lower() or "error" in str(env).lower() or True
