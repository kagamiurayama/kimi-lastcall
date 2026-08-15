"""Local controller for handoff status, confirmed ``/new``, and adoption."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from . import binding, gate, secure, state
from .adoption import PENDING_SCHEMA
from .config import ControllerConfig, load_config
from .tmux_driver import TmuxDriver, TmuxDriverError


SWITCH_SCHEMA = "kimi_lastcall.switch.v1"
RECEIPT_SCHEMA = "kimi_lastcall.switch_receipt.v1"


class ControllerError(RuntimeError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_optional_private(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return secure.read_private_json(path)
    except secure.SecureStateError as exc:
        raise ControllerError("controller_state_invalid:%s" % path.name) from exc


class Controller:
    def __init__(self, config: Optional[ControllerConfig] = None, *, driver: Optional[TmuxDriver] = None) -> None:
        loaded = config if config is not None else load_config(required=True)
        if loaded is None:
            raise ControllerError("controller_not_configured")
        self.config = loaded
        self.driver = driver or TmuxDriver(loaded)
        self._lock = threading.Lock()

    def _binding(self) -> Optional[Dict[str, Any]]:
        try:
            return binding.load_binding()
        except binding.BindingError as exc:
            raise ControllerError(str(exc)) from exc

    def _tmux_status(self) -> Dict[str, Any]:
        try:
            identity = self.driver.verify_managed_session()
            return {"online": True, "pane_id": identity["pane_id"]}
        except TmuxDriverError as exc:
            return {"online": False, "reason": str(exc)}

    def _usage(self, current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        used = 0
        limit = 0
        model = ""
        warning: Optional[str] = None
        if current:
            try:
                used, limit, model = gate.last_usage(Path(str(current["wire_path"])))
            except (OSError, ValueError, KeyError) as exc:
                warning = "usage_unreadable:%s" % type(exc).__name__
        if limit > 0:
            trigger, source, setting_warning = gate.read_trigger_tokens(limit)
        else:
            trigger, source, setting_warning = 200_000, "unknown_capacity_default", None
            path = state.settings_path()
            if path.exists() or path.is_symlink():
                try:
                    raw = secure.read_private_json(path)
                    value = raw.get("trigger_tokens")
                    if (
                        set(raw) == {"schema", "trigger_tokens", "updated_at"}
                        and raw.get("schema") == gate.SETTINGS_SCHEMA
                        and isinstance(value, int)
                        and not isinstance(value, bool)
                        and gate.TRIGGER_MIN_TOKENS <= value <= 250_000
                        and value % gate.TRIGGER_STEP_TOKENS == 0
                    ):
                        trigger, source = value, "settings_capacity_unknown"
                    else:
                        setting_warning = "settings_invalid_for_unknown_capacity"
                except secure.SecureStateError:
                    setting_warning = "settings_unreadable"
        return {
            "used_tokens": used,
            "context_limit": limit,
            "model": model,
            "trigger_tokens": trigger,
            "trigger_source": source,
            "trigger_warning": setting_warning or warning,
            "trigger_reached": bool(limit > 0 and used >= trigger),
            "slider_min": gate.TRIGGER_MIN_TOKENS,
            "slider_max": gate.trigger_slider_max(limit),
            "slider_step": gate.TRIGGER_STEP_TOKENS,
            "writing_headroom": max(0, limit - trigger) if limit > 0 else None,
        }

    def _handoff_status(self, current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        files = []
        for item in self.config.handoff_files:
            status = binding.public_file_status(self.config.managed_cwd / item)
            status.pop("path", None)
            status["name"] = item
            files.append(status)
        marker = binding.public_file_status(
            state.marker_path(str(current.get("session_id") or "")) if current else state.state_dir() / "unbound.done"
        )
        marker.pop("path", None)
        marker["name"] = "done_marker"
        return {
            "files": files,
            "all_files_ready": bool(files) and all(item["valid"] and item["size"] > 0 for item in files),
            "done_marker": marker,
        }

    def status(self) -> Dict[str, Any]:
        current = self._binding()
        tmux = self._tmux_status()
        handoff = self._handoff_status(current)
        pending = _read_optional_private(state.pending_path())
        switch = _read_optional_private(state.switch_path())
        receipt = _read_optional_private(state.receipt_path())
        blockers = []
        if current is None:
            blockers.append("session_not_bound")
        if not tmux["online"]:
            blockers.append("managed_tmux_offline")
        if not handoff["all_files_ready"]:
            blockers.append("handoff_files_not_ready")
        if not handoff["done_marker"]["valid"]:
            blockers.append("handoff_not_marked_done")
        if pending is not None:
            blockers.append("session_adoption_pending")
        if switch is not None:
            blockers.append("switch_already_in_progress")
        return {
            "schema": "kimi_lastcall.status.v1",
            "checked_at": _now(),
            "configured": True,
            "managed_cwd": str(self.config.managed_cwd),
            "current_session": (
                {"digest": state.session_digest(str(current["session_id"])), "bound_at": current["bound_at"]}
                if current else None
            ),
            "tmux": tmux,
            "usage": self._usage(current),
            "handoff": handoff,
            "pending": pending is not None,
            "switch_in_progress": switch is not None,
            "last_receipt": receipt,
            "ready_to_switch": not blockers,
            "blockers": blockers,
        }

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"trigger_tokens"}:
            raise ControllerError("settings_body_invalid")
        current = self._binding()
        usage = self._usage(current)
        try:
            gate.write_trigger_tokens(payload["trigger_tokens"], int(usage["context_limit"] or 0))
        except (ValueError, secure.SecureStateError) as exc:
            raise ControllerError(str(exc)) from exc
        return self.status()

    def _confirmation_phrase(self, current: Dict[str, Any]) -> str:
        digest = hashlib.sha256(
            (str(current["session_id"]) + "\0" + str(current["wire_path"])).encode("utf-8")
        ).hexdigest()[:12]
        return "NEW " + digest

    def preview(self) -> Dict[str, Any]:
        status = self.status()
        current = self._binding()
        phrase = self._confirmation_phrase(current) if current else None
        return {
            "schema": "kimi_lastcall.switch_preview.v1",
            "ready": status["ready_to_switch"],
            "confirmation_phrase": phrase,
            "blockers": status["blockers"],
            "will_do": [
                "clear the managed Kimi input line",
                "send the fixed /new command once",
                "wait for SessionStart to verify and bind the new local session",
            ],
            "will_not_do": [
                "write or edit the handoff letter",
                "switch without this exact human confirmation",
                "send transcript or handoff content over the network",
            ],
        }

    def _save_failed_receipt(self, switch: Dict[str, Any], code: str) -> Dict[str, Any]:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "operation_id": switch["operation_id"],
            "status": code,
            "old_session_digest": switch["old_session_digest"],
            "new_session_digest": None,
            "started_at": switch["started_at"],
            "finished_at": _now(),
            "completed": False,
        }
        secure.atomic_write_json(state.receipt_path(), receipt)
        secure.remove_private_file(state.switch_path(), expected=switch)
        return receipt

    def confirm(self, payload: Dict[str, Any], *, monotonic=time.monotonic, sleeper=time.sleep) -> Dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"confirmation"}:
            raise ControllerError("switch_confirmation_body_invalid")
        confirmation = str(payload.get("confirmation") or "").strip()
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            raise ControllerError("switch_locked")
        current: Optional[Dict[str, Any]] = None
        switch: Optional[Dict[str, Any]] = None
        try:
            status_before = self.status()
            current = self._binding()
            if current is None or not status_before["ready_to_switch"]:
                raise ControllerError("switch_not_ready")
            phrase = self._confirmation_phrase(current)
            if not hmac.compare_digest(confirmation, phrase):
                raise ControllerError("switch_confirmation_invalid")
            switch = {
                "schema": SWITCH_SCHEMA,
                "operation_id": secrets.token_hex(16),
                "old_session_id": current["session_id"],
                "old_session_digest": state.session_digest(str(current["session_id"])),
                "started_at": _now(),
                "command": "/new",
            }
            try:
                secure.atomic_write_json(state.switch_path(), switch, exclusive=True)
            except FileExistsError as exc:
                raise ControllerError("switch_already_in_progress") from exc

        finally:
            if acquired:
                self._lock.release()

        assert current is not None and switch is not None
        # The terminal command can synchronously provoke SessionStart in test
        # harnesses and can race it on a fast real seat. Never hold the
        # operation lock across this external action.
        try:
            self.driver.send_new()
        except TmuxDriverError as exc:
            with self._lock:
                if state.switch_path().exists() or state.switch_path().is_symlink():
                    receipt = self._save_failed_receipt(switch, "failed_closed_terminal_send")
                    raise ControllerError(receipt["status"]) from exc
                completed_receipt = _read_optional_private(state.receipt_path())
                if completed_receipt and completed_receipt.get("status") == "completed":
                    return {"status": "completed", "receipt": completed_receipt, "handoff": self.status()}
            raise ControllerError("failed_closed_terminal_send") from exc

        # SessionStart arrives through a second HTTP request. Never hold the
        # operation lock while waiting, or the callback could not adopt the
        # very session this request is waiting for.
        deadline = monotonic() + self.config.switch_timeout_seconds
        while monotonic() < deadline:
            observed = self._binding()
            in_progress = state.switch_path().exists() or state.switch_path().is_symlink()
            if observed and observed.get("session_id") != current["session_id"] and not in_progress:
                receipt = _read_optional_private(state.receipt_path())
                if receipt and receipt.get("status") == "completed":
                    return {"status": "completed", "receipt": receipt, "handoff": self.status()}
            sleeper(min(0.05, max(0.0, deadline - monotonic())))
        return {
            "status": "failed_closed_binding_timeout",
            "completed": False,
            "switch_in_progress": True,
        }

    def _validate_pending(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        pending = _read_optional_private(state.pending_path())
        if pending is None or pending != payload:
            raise ControllerError("session_start_pending_mismatch")
        expected = {"schema", "session_id", "cwd", "tmux_socket", "tmux_session", "tmux_pane"}
        if set(pending) != expected or pending.get("schema") != PENDING_SCHEMA:
            raise ControllerError("session_start_pending_shape_invalid")
        if pending["tmux_socket"] != self.config.tmux_socket or pending["tmux_session"] != self.config.tmux_session:
            raise ControllerError("session_start_seat_mismatch")
        try:
            seat = self.driver.pane_identity(str(pending["tmux_pane"]))
        except TmuxDriverError as exc:
            raise ControllerError("session_start_seat_unverified") from exc
        if seat["session_name"] != self.config.tmux_session:
            raise ControllerError("session_start_seat_mismatch")
        return pending

    def _run_callback(self, new_binding: Dict[str, Any]) -> None:
        if not self.config.on_adopt:
            return
        env = os.environ.copy()
        env.update(
            {
                "KIMI_LASTCALL_SESSION_ID": str(new_binding["session_id"]),
                "KIMI_LASTCALL_SESSION_DIR": str(new_binding["session_dir"]),
                "KIMI_LASTCALL_WIRE_PATH": str(new_binding["wire_path"]),
                "KIMI_LASTCALL_MANAGED_CWD": str(new_binding["cwd"]),
            }
        )
        try:
            result = subprocess.run(
                list(self.config.on_adopt),
                env=env,
                cwd=str(self.config.managed_cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            raise ControllerError("session_start_callback_failed") from exc
        if result.returncode != 0:
            raise ControllerError("session_start_callback_rejected")

    def adopt(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise ControllerError("session_adoption_locked")
        try:
            pending = self._validate_pending(payload)
            try:
                artifacts = binding.wait_for_session_artifacts(
                    self.config, str(pending["session_id"]), str(pending["cwd"])
                )
            except binding.BindingError as exc:
                raise ControllerError(str(exc)) from exc
            new_binding = binding.make_binding(artifacts, source="session_start_hook")
            current = self._binding()
            switch = _read_optional_private(state.switch_path())
            if switch is not None:
                if switch.get("schema") != SWITCH_SCHEMA:
                    raise ControllerError("switch_state_invalid")
                if str(switch.get("old_session_id") or "") == str(new_binding["session_id"]):
                    raise ControllerError("session_start_did_not_change_session")
            elif current is not None and current.get("session_id") != new_binding["session_id"]:
                raise ControllerError("unrequested_session_change")

            binding.save_binding(new_binding)
            self._run_callback(new_binding)
            receipt = None
            if switch is not None:
                receipt = {
                    "schema": RECEIPT_SCHEMA,
                    "operation_id": switch["operation_id"],
                    "status": "completed",
                    "old_session_digest": switch["old_session_digest"],
                    "new_session_digest": state.session_digest(str(new_binding["session_id"])),
                    "started_at": switch["started_at"],
                    "finished_at": _now(),
                    "completed": True,
                    "artifact_wait_ms": new_binding["artifact_wait_ms"],
                    "artifact_poll_count": new_binding["artifact_poll_count"],
                }
                secure.atomic_write_json(state.receipt_path(), receipt)
                secure.remove_private_file(state.switch_path(), expected=switch)
            secure.remove_private_file(state.pending_path(), expected=pending)
            return {
                "status": "adopted",
                "session_id": new_binding["session_id"],
                "session_digest": state.session_digest(str(new_binding["session_id"])),
                "artifact_wait_ms": new_binding["artifact_wait_ms"],
                "artifact_poll_count": new_binding["artifact_poll_count"],
                "switch_receipt": receipt,
            }
        finally:
            self._lock.release()

    def reconcile_pending(self) -> Optional[Dict[str, Any]]:
        """Finish a verified pending SessionStart after controller restart.

        The hook freezes identity before making HTTP contact. If the server was
        offline or restarted, that marker is sufficient to retry locally; it
        is never cleared merely because the daemon came back.
        """
        pending = _read_optional_private(state.pending_path())
        if pending is None:
            return None
        return self.adopt(pending)
