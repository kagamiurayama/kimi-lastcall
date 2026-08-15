"""SessionStart-side half of the managed Kimi session adoption protocol."""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Dict, Mapping, Optional
from urllib import error, request

from . import secure, state
from .config import ControllerConfigError, load_config, read_control_token
from .tmux_driver import TmuxDriver


PENDING_SCHEMA = "kimi_lastcall.session_start_pending.v1"


class AdoptionError(RuntimeError):
    pass


class AdoptionNotConfigured(AdoptionError):
    pass


def _pending_identity(
    payload: Dict[str, Any],
    seat: Dict[str, str],
) -> Dict[str, Any]:
    session_id = str(payload.get("session_id") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if not state.valid_session_id(session_id) or not session_id.startswith("session_"):
        raise AdoptionError("session_start_id_invalid")
    if not cwd:
        raise AdoptionError("session_start_cwd_missing")
    return {
        "schema": PENDING_SCHEMA,
        "session_id": session_id,
        "cwd": cwd,
        "tmux_socket": os.path.basename(seat["socket_path"]),
        "tmux_session": seat["session_name"],
        "tmux_pane": seat["pane_id"],
    }


def _freeze_pending(identity: Dict[str, Any]) -> Dict[str, Any]:
    path = state.pending_path()
    try:
        secure.atomic_write_json(path, identity, exclusive=True)
        return identity
    except FileExistsError:
        try:
            existing = secure.read_private_json(path)
        except secure.SecureStateError as exc:
            raise AdoptionError("session_start_pending_invalid") from exc
        if existing != identity:
            raise AdoptionError("session_start_pending_conflict")
        return existing


def _post_adoption(
    host: str,
    port: int,
    token: str,
    pending: Dict[str, Any],
    *,
    opener: Any = request.urlopen,
) -> Dict[str, Any]:
    body = json.dumps(pending, sort_keys=True, separators=(",", ":")).encode("utf-8")
    req = request.Request(
        "http://%s:%d/api/v1/adopt" % (host, port),
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        response = opener(req, timeout=8)
        raw = response.read(65537)
    except (OSError, error.URLError, error.HTTPError) as exc:
        raise AdoptionError("session_start_controller_unreachable") from exc
    if len(raw) > 65536:
        raise AdoptionError("session_start_response_too_large")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AdoptionError("session_start_response_invalid") from exc
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"ok", "result"}
        or parsed.get("ok") is not True
        or not isinstance(parsed.get("result"), dict)
    ):
        raise AdoptionError("session_start_response_invalid")
    result = parsed["result"]
    if result.get("status") != "adopted" or not hmac.compare_digest(
        str(result.get("session_id") or ""), str(pending["session_id"])
    ):
        raise AdoptionError("session_start_not_adopted")
    return result


def adopt_from_hook(
    payload: Dict[str, Any],
    *,
    env: Optional[Mapping[str, str]] = None,
    opener: Any = request.urlopen,
) -> Dict[str, Any]:
    try:
        config = load_config(required=False)
    except ControllerConfigError as exc:
        raise AdoptionError("session_start_controller_config_invalid") from exc
    if config is None:
        raise AdoptionNotConfigured("session_start_controller_not_configured")
    observed_env = os.environ if env is None else env
    seat = TmuxDriver(config).prove_hook_seat(observed_env)
    if seat is None:
        raise AdoptionError("session_start_not_managed_seat")
    pending = _freeze_pending(_pending_identity(payload, seat))
    token = read_control_token()
    result = _post_adoption(config.host, config.port, token, pending, opener=opener)
    if state.pending_path().exists() or state.pending_path().is_symlink():
        raise AdoptionError("session_start_pending_not_cleared")
    return result
