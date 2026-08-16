"""Owner-only configuration for the optional full-product controller."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import secure, state


CONTROLLER_SCHEMA_V1 = "kimi_lastcall.controller.v1"
CONTROLLER_SCHEMA = "kimi_lastcall.controller.v2"
TOKEN_FILENAME = "control.token"
CONTROLLER_FILENAME = "controller.json"
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ControllerConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControllerConfig:
    managed_cwd: Path
    tmux_socket: str
    tmux_session: str
    tmux_bin: str
    handoff_files: Tuple[str, ...]
    on_adopt: Tuple[str, ...]
    host: str = "127.0.0.1"
    port: int = 8765
    switch_timeout_seconds: int = 30
    artifact_timeout_seconds: int = 5
    switch_mode: str = "manual"

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema": CONTROLLER_SCHEMA,
            "managed_cwd": str(self.managed_cwd),
            "tmux_socket": self.tmux_socket,
            "tmux_session": self.tmux_session,
            "tmux_bin": self.tmux_bin,
            "handoff_files": list(self.handoff_files),
            "on_adopt": list(self.on_adopt),
            "host": self.host,
            "port": self.port,
            "switch_timeout_seconds": self.switch_timeout_seconds,
            "artifact_timeout_seconds": self.artifact_timeout_seconds,
            "switch_mode": self.switch_mode,
        }


def controller_path() -> Path:
    return state.state_dir() / CONTROLLER_FILENAME


def token_path() -> Path:
    return state.state_dir() / TOKEN_FILENAME


def _relative_handoff(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControllerConfigError("controller_handoff_file_invalid")
    candidate = Path(value.strip())
    if candidate.is_absolute() or ".." in candidate.parts or candidate.name in ("", "."):
        raise ControllerConfigError("controller_handoff_file_invalid")
    return candidate.as_posix()


def _safe_name(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if SAFE_NAME_RE.fullmatch(text) is None:
        raise ControllerConfigError(code)
    return text


def _absolute_executable(value: Any) -> str:
    text = str(value or "").strip()
    resolved = shutil.which(text) if text and not os.path.isabs(text) else text
    if not resolved:
        raise ControllerConfigError("controller_tmux_bin_missing")
    path = Path(resolved).expanduser()
    try:
        info = path.stat()
    except OSError as exc:
        raise ControllerConfigError("controller_tmux_bin_missing") from exc
    if not path.is_absolute() or not stat.S_ISREG(info.st_mode) or not os.access(str(path), os.X_OK):
        raise ControllerConfigError("controller_tmux_bin_invalid")
    return str(path.resolve())


def build_config(
    *,
    managed_cwd: str,
    tmux_socket: str,
    tmux_session: str,
    tmux_bin: str = "tmux",
    handoff_files: Optional[Sequence[str]] = None,
    on_adopt: Optional[Sequence[str]] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    switch_timeout_seconds: int = 30,
    artifact_timeout_seconds: int = 5,
    switch_mode: str = "manual",
) -> ControllerConfig:
    cwd = Path(managed_cwd).expanduser()
    if not cwd.is_absolute():
        raise ControllerConfigError("controller_cwd_not_absolute")
    try:
        info = cwd.lstat()
    except OSError as exc:
        raise ControllerConfigError("controller_cwd_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ControllerConfigError("controller_cwd_invalid")
    if info.st_uid != os.geteuid():
        raise ControllerConfigError("controller_cwd_owner_invalid")
    if host != "127.0.0.1":
        raise ControllerConfigError("controller_host_not_loopback")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ControllerConfigError("controller_port_invalid")
    if isinstance(switch_timeout_seconds, bool) or not isinstance(switch_timeout_seconds, int):
        raise ControllerConfigError("controller_switch_timeout_invalid")
    if not 1 <= switch_timeout_seconds <= 120:
        raise ControllerConfigError("controller_switch_timeout_invalid")
    if isinstance(artifact_timeout_seconds, bool) or not isinstance(artifact_timeout_seconds, int):
        raise ControllerConfigError("controller_artifact_timeout_invalid")
    if not 1 <= artifact_timeout_seconds <= 30:
        raise ControllerConfigError("controller_artifact_timeout_invalid")
    if switch_mode not in {"manual", "automatic"}:
        raise ControllerConfigError("controller_switch_mode_invalid")
    files = tuple(_relative_handoff(item) for item in (handoff_files or ("HANDOFF.md",)))
    if not files or len(files) > 8 or len(set(files)) != len(files):
        raise ControllerConfigError("controller_handoff_files_invalid")
    callback = tuple(str(item) for item in (on_adopt or ()))
    if len(callback) > 32 or any(not item or len(item) > 4096 for item in callback):
        raise ControllerConfigError("controller_on_adopt_invalid")
    return ControllerConfig(
        managed_cwd=cwd.resolve(),
        tmux_socket=_safe_name(tmux_socket, "controller_tmux_socket_invalid"),
        tmux_session=_safe_name(tmux_session, "controller_tmux_session_invalid"),
        tmux_bin=_absolute_executable(tmux_bin),
        handoff_files=files,
        on_adopt=callback,
        host=host,
        port=port,
        switch_timeout_seconds=switch_timeout_seconds,
        artifact_timeout_seconds=artifact_timeout_seconds,
        switch_mode=switch_mode,
    )


def parse_config(payload: Dict[str, Any]) -> ControllerConfig:
    common = {
        "schema",
        "managed_cwd",
        "tmux_socket",
        "tmux_session",
        "tmux_bin",
        "handoff_files",
        "on_adopt",
        "host",
        "port",
        "switch_timeout_seconds",
        "artifact_timeout_seconds",
    }
    schema = payload.get("schema")
    expected = common if schema == CONTROLLER_SCHEMA_V1 else common | {"switch_mode"}
    if schema not in {CONTROLLER_SCHEMA_V1, CONTROLLER_SCHEMA} or set(payload) != expected:
        raise ControllerConfigError("controller_config_shape_invalid")
    files = payload.get("handoff_files")
    callback = payload.get("on_adopt")
    if not isinstance(files, list) or not isinstance(callback, list):
        raise ControllerConfigError("controller_config_shape_invalid")
    return build_config(
        managed_cwd=str(payload.get("managed_cwd") or ""),
        tmux_socket=str(payload.get("tmux_socket") or ""),
        tmux_session=str(payload.get("tmux_session") or ""),
        tmux_bin=str(payload.get("tmux_bin") or ""),
        handoff_files=files,
        on_adopt=callback,
        host=str(payload.get("host") or ""),
        port=payload.get("port"),
        switch_timeout_seconds=payload.get("switch_timeout_seconds"),
        artifact_timeout_seconds=payload.get("artifact_timeout_seconds"),
        switch_mode=("manual" if schema == CONTROLLER_SCHEMA_V1 else payload.get("switch_mode")),
    )


def load_config(*, required: bool = True) -> Optional[ControllerConfig]:
    path = controller_path()
    try:
        payload = secure.read_private_json(path)
    except secure.SecureStateError as exc:
        if not required and not path.exists() and not path.is_symlink():
            return None
        raise ControllerConfigError(str(exc)) from exc
    return parse_config(payload)


def save_config(value: ControllerConfig) -> Path:
    secure.atomic_write_json(controller_path(), value.to_json())
    return controller_path()


def ensure_control_token() -> str:
    path = token_path()
    try:
        token = secure.read_private_text(path).strip()
    except secure.SecureStateError:
        if path.exists() or path.is_symlink():
            raise ControllerConfigError("controller_token_invalid")
        import secrets

        token = secrets.token_urlsafe(32)
        secure.atomic_write_private(path, token + "\n", exclusive=True)
    if len(token) < 32 or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
        raise ControllerConfigError("controller_token_invalid")
    return token


def read_control_token() -> str:
    try:
        token = secure.read_private_text(token_path()).strip()
    except secure.SecureStateError as exc:
        raise ControllerConfigError("controller_token_invalid") from exc
    if len(token) < 32 or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
        raise ControllerConfigError("controller_token_invalid")
    return token
