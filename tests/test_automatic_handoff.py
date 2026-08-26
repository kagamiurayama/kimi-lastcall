"""Automatic mode reuses the verified switch path without weakening manual mode."""

from __future__ import annotations

import json
import fcntl
import os
from dataclasses import replace
from pathlib import Path

import pytest

from kimi_lastcall import automation, binding, cli, compat, config, controller, gate, secure, state
from kimi_lastcall.tmux_driver import TmuxDriverError


OLD = "session_auto-old-0001"
NEW = "session_auto-new-0002"


def private_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("KIMI_LASTCALL_STATE_DIR", str(root))
    monkeypatch.setenv("KIMI_LASTCALL_SESSIONS_ROOT", str(tmp_path / "sessions"))
    return root


def executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def make_config(tmp_path: Path, *, mode: str = "automatic"):
    cwd = tmp_path / "resident"
    cwd.mkdir(exist_ok=True)
    return config.build_config(
        managed_cwd=str(cwd),
        tmux_socket="resident-socket",
        tmux_session="resident-session",
        tmux_bin=str(executable(tmp_path / "tmux")),
        handoff_files=["LETTER.md", "HANDOVER.md"],
        switch_timeout_seconds=1,
        artifact_timeout_seconds=1,
        switch_mode=mode,
    )


def make_session(tmp_path: Path, cfg, session_id: str, *, used: int = 800_000):
    root = tmp_path / "sessions" / "wd_fixture" / session_id
    wire = root / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(
        json.dumps({"id": session_id, "cwd": str(cfg.managed_cwd), "archived": False}) + "\n",
        encoding="utf-8",
    )
    wire.write_text(
        json.dumps(
            {
                "type": "usage.record",
                "model": "kimi-code/k3",
                "usage": {"inputOther": used, "inputCacheRead": 0, "inputCacheCreation": 0},
            }
        ) + "\n",
        encoding="utf-8",
    )
    return binding.validate_session_artifacts(cfg, session_id, str(cfg.managed_cwd))


class FakeDriver:
    def __init__(self, cfg, *, on_new=None):
        self.cfg = cfg
        self.on_new = on_new
        self.sent = 0

    def verify_managed_session(self):
        return {
            "socket_path": "/tmp/tmux-fixture/%s" % self.cfg.tmux_socket,
            "session_name": self.cfg.tmux_session,
            "pane_id": "%7",
        }

    def pane_identity(self, target):
        if target != "%7":
            raise TmuxDriverError("tmux_identity_mismatch")
        return self.verify_managed_session()

    def send_new(self):
        self.sent += 1
        if self.on_new:
            self.on_new()
        return self.verify_managed_session()


def prepare_bound(monkeypatch, tmp_path: Path, cfg):
    private_root(monkeypatch, tmp_path)
    binding.save_binding(binding.make_binding(make_session(tmp_path, cfg, OLD), source="test"))
    for name in cfg.handoff_files:
        (cfg.managed_cwd / name).write_text("reviewed handoff\n", encoding="utf-8")
    secure.atomic_write_private(state.marker_path(OLD), "done\n")
    secure.atomic_write_private(state.auto_hook_lease_path(OLD), "stop-hook\n")


def signal(cfg, session_id=OLD):
    return {
        "schema": automation.SIGNAL_SCHEMA,
        "event": "Stop",
        "source": "relay_gate",
        "session_id": session_id,
        "cwd": str(cfg.managed_cwd),
        "tmux_socket": cfg.tmux_socket,
        "tmux_session": cfg.tmux_session,
        "tmux_pane": "%7",
    }


def test_v1_config_remains_manual_and_v2_mode_is_strict(tmp_path):
    cfg = make_config(tmp_path, mode="automatic")
    legacy = cfg.to_json()
    legacy["schema"] = config.CONTROLLER_SCHEMA_V1
    legacy.pop("switch_mode")
    assert config.parse_config(legacy).switch_mode == "manual"
    assert config.parse_config(cfg.to_json()).switch_mode == "automatic"
    with pytest.raises(config.ControllerConfigError, match="controller_switch_mode_invalid"):
        config.build_config(
            managed_cwd=str(cfg.managed_cwd),
            tmux_socket=cfg.tmux_socket,
            tmux_session=cfg.tmux_session,
            tmux_bin=cfg.tmux_bin,
            switch_mode="surprise",
        )


def test_cache_expiry_hint_preflight_is_conservative(monkeypatch, tmp_path):
    home = tmp_path / "kimi-home"
    home.mkdir()
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    path = home / "tui.toml"

    assert compat.tui_config_path() == path
    assert compat.cache_expiry_hint_disabled() is False
    path.write_text("cache_expiry_hint = true\n", encoding="utf-8")
    assert compat.cache_expiry_hint_disabled() is False
    path.write_text("cache_expiry_hint = FALSE\n", encoding="utf-8")
    assert compat.cache_expiry_hint_disabled() is False
    path.write_text("cache_expiry_hint = false # unattended seat\n", encoding="utf-8")
    assert compat.cache_expiry_hint_disabled() is True
    path.write_text("[notifications]\ncache_expiry_hint = false\n", encoding="utf-8")
    assert compat.cache_expiry_hint_disabled() is False
    path.write_text("cache_expiry_hint = false\ncache_expiry_hint = false\n", encoding="utf-8")
    assert compat.cache_expiry_hint_disabled() is False


def test_automatic_status_warns_when_cache_dialog_can_intercept_input(monkeypatch, tmp_path):
    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))

    assert ctl.status()["compatibility_warnings"] == [compat.CACHE_EXPIRY_WARNING]
    (kimi_home / "tui.toml").write_text("cache_expiry_hint = false\n", encoding="utf-8")
    assert ctl.status()["compatibility_warnings"] == []
    ctl.config = replace(cfg, switch_mode="manual")
    (kimi_home / "tui.toml").write_text("cache_expiry_hint = true\n", encoding="utf-8")
    assert ctl.status()["compatibility_warnings"] == []


def test_auto_signal_is_seat_bound_current_ready_and_idempotent(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    first = ctl.queue_auto_handoff(signal(cfg))
    second = ctl.queue_auto_handoff(signal(cfg))
    assert first["status"] == "queued"
    assert second == {**first, "status": "already_queued"}
    assert ctl.status()["auto_handoff"]["session_digest"] == state.session_digest(OLD)
    rendered = json.dumps(ctl.status())
    assert OLD not in rendered
    wrong = signal(cfg, NEW)
    with pytest.raises(controller.ControllerError, match="session_not_current"):
        ctl.queue_auto_handoff(wrong)


def test_manual_mode_rejects_automatic_signal_but_manual_preview_stays_available(monkeypatch, tmp_path):
    cfg = make_config(tmp_path, mode="manual")
    prepare_bound(monkeypatch, tmp_path, cfg)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    with pytest.raises(controller.ControllerError, match="automatic_switch_disabled"):
        ctl.queue_auto_handoff(signal(cfg))
    assert ctl.preview()["confirmation_phrase"].startswith("NEW ")


def test_queued_automatic_request_obeys_mode_disabled_before_execution(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    driver = FakeDriver(cfg, on_new=lambda: pytest.fail("manual mode must block /new"))
    ctl = controller.Controller(cfg, driver=driver)
    queued = ctl.queue_auto_handoff(signal(cfg))

    ctl.config = replace(cfg, switch_mode="manual")
    result = ctl.run_auto_handoff_worker(
        queued["request_id"], OLD, hook_exit_timeout=0, sleeper=lambda ignored: None
    )

    assert result == {"status": "failed_closed", "error_code": "automatic_switch_disabled"}
    assert driver.sent == 0


def test_automatic_worker_uses_one_verified_switch_and_records_source(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    holder = {}

    def on_new():
        make_session(tmp_path, cfg, NEW, used=1)
        pending = {
            "schema": "kimi_lastcall.session_start_pending.v1",
            "session_id": NEW,
            "cwd": str(cfg.managed_cwd),
            "tmux_socket": cfg.tmux_socket,
            "tmux_session": cfg.tmux_session,
            "tmux_pane": "%7",
        }
        secure.atomic_write_json(state.pending_path(), pending, exclusive=True)
        holder["controller"].adopt(pending)

    driver = FakeDriver(cfg, on_new=on_new)
    ctl = controller.Controller(cfg, driver=driver)
    holder["controller"] = ctl
    queued = ctl.queue_auto_handoff(signal(cfg))
    result = ctl.run_auto_handoff_worker(
        queued["request_id"], OLD, hook_exit_timeout=0, sleeper=lambda ignored: None
    )
    assert result["status"] == "completed"
    assert driver.sent == 1
    assert binding.load_binding()["session_id"] == NEW
    receipt = secure.read_private_json(state.receipt_path())
    assert receipt["execution_source"] == "automatic_relay"
    assert receipt["completed"] is True
    assert secure.read_private_json(state.auto_handoff_path())["status"] == "completed"


def test_restart_never_resends_when_switch_file_proves_send_may_have_started(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    first_driver = FakeDriver(cfg)
    ctl = controller.Controller(cfg, driver=first_driver)
    queued = ctl.queue_auto_handoff(signal(cfg))
    secure.atomic_write_json(
        state.switch_path(),
        {
            "schema": controller.SWITCH_SCHEMA,
            "operation_id": "frozen-operation",
            "old_session_id": OLD,
            "old_session_digest": state.session_digest(OLD),
            "started_at": "2026-01-01T00:00:00Z",
            "command": "/new",
            "execution_source": "automatic_relay",
        },
        exclusive=True,
    )
    restarted_driver = FakeDriver(cfg, on_new=lambda: pytest.fail("must not resend /new"))
    restarted = controller.Controller(cfg, driver=restarted_driver)
    assert restarted.resume_auto_handoff()["status"] == "manual_recovery_required"
    assert restarted_driver.sent == 0
    request = secure.read_private_json(state.auto_handoff_path())
    assert request["request_id"] == queued["request_id"]
    assert request["status"] == "manual_recovery_required"


def test_duplicate_hook_is_idempotent_even_after_switch_state_exists(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    queued = ctl.queue_auto_handoff(signal(cfg))
    secure.atomic_write_json(
        state.switch_path(),
        {
            "schema": controller.SWITCH_SCHEMA,
            "operation_id": "frozen-operation",
            "old_session_id": OLD,
            "old_session_digest": state.session_digest(OLD),
            "started_at": "2026-01-01T00:00:00Z",
            "command": "/new",
            "execution_source": "automatic_relay",
        },
        exclusive=True,
    )
    duplicate = ctl.queue_auto_handoff(signal(cfg))
    assert duplicate["status"] == "already_queued"
    assert duplicate["request_id"] == queued["request_id"]


def test_unknown_auto_state_fields_fail_closed(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    ctl.queue_auto_handoff(signal(cfg))
    request = secure.read_private_json(state.auto_handoff_path())
    request["quietly_relax"] = True
    secure.atomic_write_json(state.auto_handoff_path(), request)
    with pytest.raises(controller.ControllerError, match="auto_handoff_state_invalid"):
        ctl.status()


def test_worker_cannot_send_until_stop_hook_kernel_lease_is_released(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    driver = FakeDriver(cfg, on_new=lambda: pytest.fail("locked hook must block /new"))
    ctl = controller.Controller(cfg, driver=driver)
    queued = ctl.queue_auto_handoff(signal(cfg))
    lease = state.auto_hook_lease_path(OLD).open("rb")
    fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = ctl.run_auto_handoff_worker(
            queued["request_id"], OLD, hook_exit_timeout=0, sleeper=lambda ignored: None
        )
    finally:
        fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        lease.close()
    assert result == {"status": "failed_closed", "error_code": "auto_handoff_hook_exit_timeout"}
    assert driver.sent == 0


def test_stop_gate_queues_after_done_marker_and_failure_never_traps(monkeypatch, tmp_path, capsys):
    cfg = make_config(tmp_path)
    prepare_bound(monkeypatch, tmp_path, cfg)
    seen = []
    monkeypatch.setattr(
        automation,
        "request_from_stop_hook",
        lambda session_id: seen.append(session_id) or {
            "status": "queued", "request_id": "request", "session_digest": state.session_digest(session_id)
        },
    )
    # The marker was touched before any gate demand (letter written early or
    # days ago): the first crossing blocks and stamps the demand instead of
    # accepting a possibly-stale letter.
    assert gate.handle_stop(OLD) == 2
    assert seen == []

    # The window re-checks the letter and re-touches the marker after the
    # demand — now the marker is fresh and the switch is queued.
    secure.atomic_write_private(state.marker_path(OLD), "done\n")
    assert gate.handle_stop(OLD) == 0
    assert seen == [OLD]
    assert "auto_handoff_accepted" in state.audit_path().read_text(encoding="utf-8")
    # The release is one-shot: the demand is advanced strictly past the
    # marker (the marker file stays — the controller preflight re-checks it).
    assert state.marker_path(OLD).exists()
    assert state.demand_path(OLD).stat().st_mtime > state.marker_path(OLD).stat().st_mtime

    # A later fresh marker whose switch request fails still fails open and
    # keeps the marker fresh so the next stop can retry.
    marker = state.marker_path(OLD)
    secure.atomic_write_private(marker, "done\n")
    # The consumed demand was advanced past the old marker (possibly into this
    # same second); make the new letter unambiguously newer than the demand.
    fresh = state.demand_path(OLD).stat().st_mtime + 1.0
    os.utime(marker, (fresh, fresh))
    monkeypatch.setattr(
        automation,
        "request_from_stop_hook",
        lambda ignored: (_ for _ in ()).throw(automation.AutoHandoffError("controller_offline")),
    )
    assert gate.handle_stop(OLD) == 0
    assert "controller_offline" in capsys.readouterr().err
    assert "auto_handoff_failed_open" in state.audit_path().read_text(encoding="utf-8")
    # The failed request must not consume the release: the marker is still
    # fresh, so the next stop retries the switch instead of demanding a letter.
    assert gate.marker_fresh(OLD)


def test_hook_post_requires_exact_bound_response():
    sig = {
        "schema": automation.SIGNAL_SCHEMA,
        "event": "Stop",
        "source": "relay_gate",
        "session_id": OLD,
    }

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self, limit):
            assert limit == 65537
            return json.dumps(self.payload).encode("utf-8")

    result = {
        "status": "queued",
        "request_id": "request-1",
        "session_digest": state.session_digest(OLD),
    }
    assert automation._post_signal(
        "127.0.0.1", 8765, "token", sig,
        opener=lambda req, timeout: Response({"ok": True, "result": result}),
    ) == result
    with pytest.raises(automation.AutoHandoffError, match="response_invalid"):
        automation._post_signal(
            "127.0.0.1", 8765, "token", sig,
            opener=lambda req, timeout: Response(result),
        )


def test_cli_set_mode_upgrades_legacy_config_without_touching_other_fields(monkeypatch, tmp_path, capsys):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, mode="manual")
    legacy = cfg.to_json()
    legacy["schema"] = config.CONTROLLER_SCHEMA_V1
    legacy.pop("switch_mode")
    secure.atomic_write_json(config.controller_path(), legacy)
    assert cli.main(["set-mode", "automatic"]) == 0
    loaded = config.load_config(required=True)
    assert loaded.switch_mode == "automatic"
    assert loaded.managed_cwd == cfg.managed_cwd
    assert "automatic" in capsys.readouterr().out
