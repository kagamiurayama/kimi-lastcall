"""Local controller for handoff status, confirmed ``/new``, and adoption."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from . import binding, compat, gate, secure, state
from .adoption import PENDING_SCHEMA
from .config import ControllerConfig, load_config
from .tmux_driver import TmuxDriver, TmuxDriverError


SWITCH_SCHEMA = "kimi_lastcall.switch.v1"
RECEIPT_SCHEMA = "kimi_lastcall.switch_receipt.v1"
AUTO_REQUEST_SCHEMA = "kimi_lastcall.auto_handoff_request.v1"
AUTO_ACTIVE_STATUSES = {"queued", "waiting_for_stop", "running"}
AUTO_TERMINAL_STATUSES = {"completed", "failed_closed", "manual_recovery_required", "superseded"}
AUTO_REQUEST_FIELDS = {
    "schema", "request_id", "session_id", "session_digest", "status",
    "accepted_at", "started_at", "finished_at", "error_code",
}


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


def _read_auto_request() -> Optional[Dict[str, Any]]:
    request = _read_optional_private(state.auto_handoff_path())
    if request is None:
        return None
    session_id = str(request.get("session_id") or "")
    if (
        set(request) != AUTO_REQUEST_FIELDS
        or request.get("schema") != AUTO_REQUEST_SCHEMA
        or re.fullmatch(r"[0-9a-f]{32}", str(request.get("request_id") or "")) is None
        or not state.valid_session_id(session_id)
        or request.get("session_digest") != state.session_digest(session_id)
        or request.get("status") not in AUTO_ACTIVE_STATUSES | AUTO_TERMINAL_STATUSES
        or not isinstance(request.get("accepted_at"), str)
        or request.get("started_at") is not None and not isinstance(request.get("started_at"), str)
        or request.get("finished_at") is not None and not isinstance(request.get("finished_at"), str)
        or request.get("error_code") is not None and not isinstance(request.get("error_code"), str)
    ):
        raise ControllerError("auto_handoff_state_invalid")
    return request


class Controller:
    def __init__(self, config: Optional[ControllerConfig] = None, *, driver: Optional[TmuxDriver] = None) -> None:
        loaded = config if config is not None else load_config(required=True)
        if loaded is None:
            raise ControllerError("controller_not_configured")
        self.config = loaded
        self.driver = driver or TmuxDriver(loaded)
        self._lock = threading.Lock()
        self._auto_thread: Optional[threading.Thread] = None

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
        auto_request = _read_auto_request()
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
            "switch_mode": self.config.switch_mode,
            "automatic_switch_enabled": self.config.switch_mode == "automatic",
            "compatibility_warnings": compat.automatic_mode_warnings(self.config.switch_mode),
            "auto_handoff": self._public_auto_request(auto_request),
            "ready_to_switch": not blockers,
            "blockers": blockers,
        }

    def _public_auto_request(self, request: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if request is None:
            return None
        return {
            "status": str(request.get("status") or "invalid"),
            "request_id": str(request.get("request_id") or ""),
            "session_digest": str(request.get("session_digest") or ""),
            "accepted_at": request.get("accepted_at"),
            "started_at": request.get("started_at"),
            "finished_at": request.get("finished_at"),
            "error_code": request.get("error_code"),
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
                "use this manual fallback without this exact human confirmation",
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
            "execution_source": switch.get("execution_source", "human_confirmed"),
        }
        secure.atomic_write_json(state.receipt_path(), receipt)
        secure.remove_private_file(state.switch_path(), expected=switch)
        return receipt

    def confirm(self, payload: Dict[str, Any], *, monotonic=time.monotonic, sleeper=time.sleep) -> Dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"confirmation"}:
            raise ControllerError("switch_confirmation_body_invalid")
        confirmation = str(payload.get("confirmation") or "").strip()
        return self._execute_switch(
            execution_source="human_confirmed",
            confirmation=confirmation,
            monotonic=monotonic,
            sleeper=sleeper,
        )

    def _execute_switch(
        self,
        *,
        execution_source: str,
        confirmation: Optional[str] = None,
        expected_session_id: Optional[str] = None,
        monotonic=time.monotonic,
        sleeper=time.sleep,
    ) -> Dict[str, Any]:
        if execution_source not in {"human_confirmed", "automatic_relay"}:
            raise ControllerError("switch_execution_source_invalid")
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
            if expected_session_id is not None and not hmac.compare_digest(
                str(current["session_id"]), expected_session_id
            ):
                raise ControllerError("switch_session_changed")
            if execution_source == "human_confirmed":
                phrase = self._confirmation_phrase(current)
                if not hmac.compare_digest(str(confirmation or ""), phrase):
                    raise ControllerError("switch_confirmation_invalid")
            elif self.config.switch_mode != "automatic":
                raise ControllerError("automatic_switch_disabled")
            switch = {
                "schema": SWITCH_SCHEMA,
                "operation_id": secrets.token_hex(16),
                "old_session_id": current["session_id"],
                "old_session_digest": state.session_digest(str(current["session_id"])),
                "started_at": _now(),
                "command": "/new",
                "execution_source": execution_source,
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

    def queue_auto_handoff(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Freeze one seat-bound automatic request without sending terminal input."""
        from .automation import SIGNAL_SCHEMA

        expected = {
            "schema", "event", "source", "session_id", "cwd",
            "tmux_socket", "tmux_session", "tmux_pane",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ControllerError("auto_handoff_payload_invalid")
        if payload.get("schema") != SIGNAL_SCHEMA or payload.get("event") != "Stop":
            raise ControllerError("auto_handoff_payload_invalid")
        if payload.get("source") != "relay_gate":
            raise ControllerError("auto_handoff_source_invalid")
        if self.config.switch_mode != "automatic":
            raise ControllerError("automatic_switch_disabled")
        session_id = str(payload.get("session_id") or "")
        if not state.valid_session_id(session_id) or not session_id.startswith("session_"):
            raise ControllerError("auto_handoff_session_invalid")
        if str(payload.get("cwd") or "") != str(self.config.managed_cwd):
            raise ControllerError("auto_handoff_cwd_mismatch")
        if (
            payload.get("tmux_socket") != self.config.tmux_socket
            or payload.get("tmux_session") != self.config.tmux_session
        ):
            raise ControllerError("auto_handoff_seat_mismatch")
        try:
            seat = self.driver.pane_identity(str(payload.get("tmux_pane") or ""))
        except TmuxDriverError as exc:
            raise ControllerError("auto_handoff_seat_unverified") from exc
        if (
            os.path.basename(str(seat.get("socket_path") or "")) != payload["tmux_socket"]
            or seat.get("session_name") != payload["tmux_session"]
            or seat.get("pane_id") != payload["tmux_pane"]
        ):
            raise ControllerError("auto_handoff_seat_mismatch")

        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            raise ControllerError("auto_handoff_locked")
        try:
            current = self._binding()
            if current is None or not hmac.compare_digest(str(current["session_id"]), session_id):
                raise ControllerError("auto_handoff_session_not_current")
            existing = _read_auto_request()
            if existing and existing.get("status") in AUTO_ACTIVE_STATUSES:
                if hmac.compare_digest(str(existing.get("session_id") or ""), session_id):
                    return {
                        "status": "already_queued",
                        "request_id": str(existing.get("request_id") or ""),
                        "session_digest": state.session_digest(session_id),
                    }
                raise ControllerError("auto_handoff_already_active")
            status = self.status()
            if not status["usage"]["trigger_reached"]:
                raise ControllerError("auto_handoff_threshold_not_reached")
            if not status["ready_to_switch"]:
                raise ControllerError("auto_handoff_not_ready")
            request = {
                "schema": AUTO_REQUEST_SCHEMA,
                "request_id": secrets.token_hex(16),
                "session_id": session_id,
                "session_digest": state.session_digest(session_id),
                "status": "queued",
                "accepted_at": _now(),
                "started_at": None,
                "finished_at": None,
                "error_code": None,
            }
            secure.atomic_write_json(state.auto_handoff_path(), request)
            return {
                "status": "queued",
                "request_id": request["request_id"],
                "session_digest": request["session_digest"],
            }
        finally:
            self._lock.release()

    def _finish_auto_handoff(
        self,
        request_id: str,
        *,
        status_value: str,
        error_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            request = _read_auto_request()
            if request is None or request.get("request_id") != request_id:
                raise ControllerError("auto_handoff_request_changed")
            request["status"] = status_value
            request["finished_at"] = _now()
            request["error_code"] = error_code
            secure.atomic_write_json(state.auto_handoff_path(), request)
            return request

    def run_auto_handoff_worker(
        self,
        request_id: str,
        session_id: str,
        *,
        hook_exit_timeout: Optional[float] = None,
        monotonic=time.monotonic,
        sleeper=time.sleep,
    ) -> Dict[str, Any]:
        """Wait for the Stop HTTP response, then reuse the verified switch path."""
        try:
            from . import automation

            released = automation.wait_for_hook_exit(
                session_id,
                timeout_seconds=(
                    float(self.config.switch_timeout_seconds)
                    if hook_exit_timeout is None
                    else max(0.0, hook_exit_timeout)
                ),
                monotonic=monotonic,
                sleeper=sleeper,
            )
            if not released:
                self._finish_auto_handoff(
                    request_id,
                    status_value="failed_closed",
                    error_code="auto_handoff_hook_exit_timeout",
                )
                return {"status": "failed_closed", "error_code": "auto_handoff_hook_exit_timeout"}
            with self._lock:
                request = _read_auto_request()
                if (
                    request is None
                    or request.get("request_id") != request_id
                    or request.get("session_id") != session_id
                    or request.get("status") not in AUTO_ACTIVE_STATUSES
                ):
                    return {"status": "request_not_active"}
                request["status"] = "waiting_for_stop"
                secure.atomic_write_json(state.auto_handoff_path(), request)

            current = self._binding()
            receipt = _read_optional_private(state.receipt_path())
            switch = _read_optional_private(state.switch_path())
            if current is None or current.get("session_id") != session_id:
                completed = bool(
                    receipt
                    and receipt.get("completed") is True
                    and receipt.get("old_session_digest") == state.session_digest(session_id)
                    and receipt.get("execution_source") == "automatic_relay"
                )
                self._finish_auto_handoff(
                    request_id,
                    status_value="completed" if completed else "superseded",
                )
                return {"status": "completed" if completed else "superseded"}
            if switch is not None:
                self._finish_auto_handoff(
                    request_id,
                    status_value="manual_recovery_required",
                    error_code="switch_may_already_have_been_sent",
                )
                return {"status": "manual_recovery_required"}

            status = self.status()
            if not status["usage"]["trigger_reached"] or not status["ready_to_switch"]:
                self._finish_auto_handoff(
                    request_id,
                    status_value="failed_closed",
                    error_code=(
                        "auto_handoff_threshold_not_reached"
                        if not status["usage"]["trigger_reached"]
                        else "auto_handoff_not_ready"
                    ),
                )
                return {"status": "failed_closed"}
            with self._lock:
                request = _read_auto_request()
                if request is None or request.get("request_id") != request_id:
                    return {"status": "request_not_active"}
                request["status"] = "running"
                request["started_at"] = _now()
                secure.atomic_write_json(state.auto_handoff_path(), request)
            try:
                result = self._execute_switch(
                    execution_source="automatic_relay",
                    expected_session_id=session_id,
                )
            except ControllerError as exc:
                self._finish_auto_handoff(
                    request_id,
                    status_value="failed_closed",
                    error_code=str(exc),
                )
                return {"status": "failed_closed", "error_code": str(exc)}
            if result.get("status") == "completed":
                self._finish_auto_handoff(request_id, status_value="completed")
            else:
                self._finish_auto_handoff(
                    request_id,
                    status_value="manual_recovery_required",
                    error_code=str(result.get("status") or "automatic_switch_incomplete"),
                )
            return result
        except Exception as exc:
            try:
                self._finish_auto_handoff(
                    request_id,
                    status_value="failed_closed",
                    error_code=type(exc).__name__,
                )
            except Exception:
                pass
            return {"status": "failed_closed", "error_code": type(exc).__name__}
        finally:
            with self._lock:
                if self._auto_thread is threading.current_thread():
                    self._auto_thread = None

    def start_auto_handoff_worker(self, request_id: str, session_id: str) -> bool:
        with self._lock:
            if self._auto_thread is not None and self._auto_thread.is_alive():
                return False
            thread = threading.Thread(
                target=self.run_auto_handoff_worker,
                args=(request_id, session_id),
                name="kimi-lastcall-auto-handoff",
                daemon=True,
            )
            self._auto_thread = thread
            thread.start()
            return True

    def resume_auto_handoff(self) -> Optional[Dict[str, Any]]:
        request = _read_auto_request()
        if request is None or request.get("status") not in AUTO_ACTIVE_STATUSES:
            return None
        request_id = str(request.get("request_id") or "")
        session_id = str(request.get("session_id") or "")
        if (
            request.get("schema") != AUTO_REQUEST_SCHEMA
            or not request_id
            or not state.valid_session_id(session_id)
        ):
            raise ControllerError("auto_handoff_state_invalid")
        if state.switch_path().exists() or state.switch_path().is_symlink():
            self._finish_auto_handoff(
                request_id,
                status_value="manual_recovery_required",
                error_code="switch_may_already_have_been_sent",
            )
            return {"status": "manual_recovery_required"}
        started = self.start_auto_handoff_worker(request_id, session_id)
        return {"status": "worker_started" if started else "worker_already_running"}

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
                    "execution_source": switch.get("execution_source", "human_confirmed"),
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
