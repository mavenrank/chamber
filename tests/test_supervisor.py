"""Phase 3a supervisor: spec validation, spawn/stop/log with a stub child."""

from __future__ import annotations

import pytest

from chamber import supervisor


class FakeProc:
    def __init__(self, *args, **kwargs):
        self.pid = 1234
        self.args = args
        self.kwargs = kwargs
        self._alive = True
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            raise TimeoutError
        return 0


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMBER_HOME", str(tmp_path / ".chamber"))
    monkeypatch.setenv("CHAMBER_PROJECT_ROOT", str(tmp_path / "proj"))
    (tmp_path / "proj").mkdir()
    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor.shutil, "which", lambda *_a, **_k: "uv")
    supervisor._REGISTRY.clear()
    yield tmp_path
    supervisor._REGISTRY.clear()


def test_spec_validation(home):
    with pytest.raises(ValueError):
        supervisor.RunSpec(task="   ")
    with pytest.raises(ValueError):
        supervisor.RunSpec(task="x", profile="../evil")
    with pytest.raises(ValueError):
        supervisor.RunSpec(task="x", start_url="ftp://x")
    with pytest.raises(ValueError):
        supervisor.RunSpec(task="x", max_steps=500)
    spec = supervisor.RunSpec(task="do it", profile="shop", max_steps=10)
    assert spec.profile == "shop"


def test_build_command_shape(home):
    spec = supervisor.RunSpec(task="do it")
    argv, cwd = supervisor.build_command(spec, "run-1", supervisor.project_root())
    assert argv[:4] == ["uv", "run", "--no-sync", "chamber"]
    assert "--run-id" in argv and "run-1" in argv
    assert str(cwd).endswith("proj")


def test_start_stop_log_round_trip(home):
    handle = supervisor.start_run(supervisor.RunSpec(task="do it"))
    run_id = handle["run_id"]
    assert handle["managed"] is True and handle["pid"] == 1234

    listed = {r["run_id"]: r for r in supervisor.managed_runs()}
    assert listed[run_id]["managed"] is True

    log = supervisor.read_log(run_id)
    assert log["size"] == 0 and log["text"] == ""

    # A beating run the registry doesn't know shows up as an orphan.
    # A stale corpse (open row, silent beat) stays buried.
    from chamber.trace.store import open_store

    with open_store() as store:
        store.start_run("ghost-run", "ghost", profile="default", model="m")
        store.beat()
        store.start_run("corpse-run", "corpse", profile="default", model="m")
    listed = {r["run_id"]: r for r in supervisor.managed_runs()}
    assert listed["ghost-run"]["managed"] is False
    assert "corpse-run" not in listed

    result = supervisor.stop_run(run_id, grace_s=0.1)
    assert result["result"] == "stopped"
    assert run_id not in {r["run_id"] for r in supervisor.managed_runs()}


def test_parent_and_notes_threading(home):
    from chamber import inbox

    handle = supervisor.start_run(
        supervisor.RunSpec(task="do it"),
        parent="20260920-120000-mom",
        notes=["context: earlier", "steer: go on"],
    )
    run_id = handle["run_id"]
    assert handle["parent"] == "20260920-120000-mom"
    claimed = inbox.claim_notes(run_id)
    assert [n["text"] for n in claimed] == ["context: earlier", "steer: go on"]
    child = supervisor._REGISTRY[run_id]
    assert child.kwargs["env"]["CHAMBER_PARENT_RUN"] == "20260920-120000-mom"


def test_stop_unmanaged_run_only_flags(home):
    result = supervisor.stop_run("20260920-120000-abc123")
    assert result == {"run_id": "20260920-120000-abc123", "managed": False, "result": "flagged"}
    from chamber import inbox

    assert inbox.is_stop_requested("20260920-120000-abc123") is True
