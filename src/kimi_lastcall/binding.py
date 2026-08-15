"""Mechanical Kimi session artifact validation and local binding state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Dict, Optional

from . import secure, state
from .config import ControllerConfig


BINDING_SCHEMA = "kimi_lastcall.binding.v1"


class BindingError(RuntimeError):
    pass


def _regular_owner_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BindingError("session_artifact_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BindingError("session_artifact_invalid")
    if info.st_uid != os.geteuid():
        raise BindingError("session_artifact_owner_invalid")
    return info


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path.resolve()), str(root.resolve())]) == str(root.resolve())
    except (OSError, ValueError):
        return False


def locate_session(session_id: str) -> Path:
    root = state.sessions_root().expanduser()
    matches = sorted(root.glob("wd_*/%s" % session_id))
    safe = [item for item in matches if item.is_dir() and not item.is_symlink() and _within(item, root)]
    if len(safe) != 1:
        raise BindingError("session_directory_not_unique")
    return safe[0]


def validate_session_artifacts(
    config: ControllerConfig,
    session_id: str,
    cwd: str,
) -> Dict[str, Any]:
    if not state.valid_session_id(session_id) or not session_id.startswith("session_"):
        raise BindingError("session_id_invalid")
    try:
        observed_cwd = Path(cwd).expanduser().resolve()
    except OSError as exc:
        raise BindingError("session_cwd_invalid") from exc
    if observed_cwd != config.managed_cwd:
        raise BindingError("session_cwd_mismatch")
    session_dir = locate_session(session_id)
    state_path = session_dir / "state.json"
    wire_path = session_dir / "agents" / "main" / "wire.jsonl"
    _regular_owner_file(state_path)
    _regular_owner_file(wire_path)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise BindingError("session_state_invalid") from exc
    if not isinstance(payload, dict):
        raise BindingError("session_state_invalid")
    if str(payload.get("id") or "") != session_id:
        raise BindingError("session_state_id_mismatch")
    if bool(payload.get("archived")):
        raise BindingError("session_archived")
    try:
        state_cwd = Path(str(payload.get("cwd") or "")).expanduser().resolve()
    except OSError as exc:
        raise BindingError("session_state_cwd_invalid") from exc
    if state_cwd != config.managed_cwd or state_cwd != observed_cwd:
        raise BindingError("session_state_cwd_mismatch")
    try:
        wire_bytes = wire_path.read_bytes()
    except OSError as exc:
        raise BindingError("session_wire_invalid") from exc
    line_count = 0
    wire_rows = wire_bytes.splitlines(keepends=True)
    for index, raw in enumerate(wire_rows):
        if not raw.strip():
            continue
        try:
            json.loads(raw)
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            is_unterminated_tail = (
                index == len(wire_rows) - 1
                and not raw.endswith((b"\n", b"\r"))
            )
            if is_unterminated_tail:
                # Kimi may be observed between two writes to the final JSONL row.
                # That is transient; a newline-terminated malformed row is not.
                raise BindingError("session_wire_not_ready") from exc
            raise BindingError("session_wire_invalid") from exc
        else:
            line_count += 1
    if line_count < 1:
        raise BindingError("session_wire_not_ready")
    return {
        "session_id": session_id,
        "cwd": str(config.managed_cwd),
        "session_dir": str(session_dir.resolve()),
        "state_path": str(state_path.resolve()),
        "wire_path": str(wire_path.resolve()),
        "wire_line_count": line_count,
    }


def wait_for_session_artifacts(
    config: ControllerConfig,
    session_id: str,
    cwd: str,
    *,
    monotonic=time.monotonic,
    sleeper=time.sleep,
) -> Dict[str, Any]:
    started = monotonic()
    deadline = started + config.artifact_timeout_seconds
    polls = 0
    last_code = "session_artifacts_not_ready"
    while True:
        polls += 1
        try:
            result = validate_session_artifacts(config, session_id, cwd)
            result["artifact_wait_ms"] = max(0, int((monotonic() - started) * 1000))
            result["artifact_poll_count"] = polls
            return result
        except BindingError as exc:
            last_code = str(exc)
            if last_code not in {
                "session_directory_not_unique",
                "session_artifact_missing",
                "session_wire_not_ready",
            }:
                raise
        now = monotonic()
        if now >= deadline:
            raise BindingError("session_artifact_timeout:" + last_code)
        sleeper(min(0.05, max(0.0, deadline - now)))


def make_binding(artifacts: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    return {
        "schema": BINDING_SCHEMA,
        "session_id": artifacts["session_id"],
        "cwd": artifacts["cwd"],
        "session_dir": artifacts["session_dir"],
        "state_path": artifacts["state_path"],
        "wire_path": artifacts["wire_path"],
        "wire_line_count_at_adoption": artifacts["wire_line_count"],
        "artifact_wait_ms": artifacts.get("artifact_wait_ms", 0),
        "artifact_poll_count": artifacts.get("artifact_poll_count", 1),
        "source": source,
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def save_binding(payload: Dict[str, Any]) -> None:
    secure.atomic_write_json(state.binding_path(), payload)


def load_binding() -> Optional[Dict[str, Any]]:
    path = state.binding_path()
    if not path.exists() and not path.is_symlink():
        return None
    try:
        payload = secure.read_private_json(path)
    except secure.SecureStateError as exc:
        raise BindingError("binding_state_invalid") from exc
    expected = {
        "schema",
        "session_id",
        "cwd",
        "session_dir",
        "state_path",
        "wire_path",
        "wire_line_count_at_adoption",
        "artifact_wait_ms",
        "artifact_poll_count",
        "source",
        "bound_at",
    }
    if set(payload) != expected or payload.get("schema") != BINDING_SCHEMA:
        raise BindingError("binding_state_invalid")
    return payload


def public_file_status(path: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {"path": str(path), "exists": False, "valid": False, "size": 0}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return result
    except OSError:
        result["reason"] = "unreadable"
        return result
    result["exists"] = True
    result["size"] = int(info.st_size)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        result["reason"] = "not_regular"
    elif info.st_uid != os.geteuid():
        result["reason"] = "owner_mismatch"
    else:
        result["valid"] = True
    return result
