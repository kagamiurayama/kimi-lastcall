"""Stop-hook half of the optional automatic handoff protocol.

The hook never sends terminal input.  It proves that it runs inside the
configured tmux seat, then asks the loopback controller to queue one switch.
The controller responds before starting the background worker, so ``/new``
cannot re-enter the Stop hook that requested it.
"""

from __future__ import annotations

import hmac
import fcntl
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Dict, Mapping, Optional
from urllib import error, request

from . import secure, state
from .config import ControllerConfigError, load_config, read_control_token
from .tmux_driver import TmuxDriver


SIGNAL_SCHEMA = "kimi_lastcall.auto_handoff_signal.v1"


class AutoHandoffError(RuntimeError):
    pass


class AutoHandoffNotConfigured(AutoHandoffError):
    pass


# Deliberately retained until process exit. The kernel releases flock only
# after the Stop-hook process is gone, giving the controller a mechanical
# boundary instead of a timing guess.
_HOOK_LEASES = []


def _acquire_hook_lease(session_id: str) -> Path:
    path = state.auto_hook_lease_path(session_id)
    if not path.exists() and not path.is_symlink():
        try:
            secure.atomic_write_private(path, "stop-hook\n", exclusive=True)
        except FileExistsError:
            pass
    secure.validate_private_file(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(str(path), flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise AutoHandoffError("auto_handoff_lease_invalid")
        handle = os.fdopen(fd, "rb")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception as exc:
        try:
            if fd >= 0:
                os.close(fd)
        except Exception:
            pass
        if isinstance(exc, AutoHandoffError):
            raise
        raise AutoHandoffError("auto_handoff_lease_unavailable") from exc
    _HOOK_LEASES.append(handle)
    return path


def wait_for_hook_exit(
    session_id: str,
    *,
    timeout_seconds: float,
    monotonic=time.monotonic,
    sleeper=time.sleep,
) -> bool:
    path = state.auto_hook_lease_path(session_id)
    try:
        secure.validate_private_file(path)
        flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
        fd = os.open(str(path), flags)
        handle = os.fdopen(fd, "rb")
    except Exception as exc:
        raise AutoHandoffError("auto_handoff_lease_invalid") from exc
    deadline = monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return True
            except BlockingIOError:
                if monotonic() >= deadline:
                    return False
                sleeper(min(0.05, max(0.0, deadline - monotonic())))
    finally:
        handle.close()


def _signal(session_id: str, seat: Dict[str, str], managed_cwd: str) -> Dict[str, Any]:
    if not state.valid_session_id(session_id) or not session_id.startswith("session_"):
        raise AutoHandoffError("auto_handoff_session_invalid")
    return {
        "schema": SIGNAL_SCHEMA,
        "event": "Stop",
        "source": "relay_gate",
        "session_id": session_id,
        "cwd": managed_cwd,
        "tmux_socket": os.path.basename(seat["socket_path"]),
        "tmux_session": seat["session_name"],
        "tmux_pane": seat["pane_id"],
    }


def _post_signal(
    host: str,
    port: int,
    token: str,
    signal: Dict[str, Any],
    *,
    opener: Any = request.urlopen,
) -> Dict[str, Any]:
    body = json.dumps(signal, sort_keys=True, separators=(",", ":")).encode("utf-8")
    req = request.Request(
        "http://%s:%d/api/v1/auto-handoff" % (host, port),
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
        raise AutoHandoffError("auto_handoff_controller_unreachable") from exc
    if len(raw) > 65536:
        raise AutoHandoffError("auto_handoff_response_too_large")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AutoHandoffError("auto_handoff_response_invalid") from exc
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"ok", "result"}
        or parsed.get("ok") is not True
        or not isinstance(parsed.get("result"), dict)
    ):
        raise AutoHandoffError("auto_handoff_response_invalid")
    result = parsed["result"]
    expected = {"status", "request_id", "session_digest"}
    if (
        set(result) != expected
        or result.get("status") not in {"queued", "already_queued"}
        or not hmac.compare_digest(
            str(result.get("session_digest") or ""), state.session_digest(str(signal["session_id"]))
        )
        or not str(result.get("request_id") or "")
    ):
        raise AutoHandoffError("auto_handoff_not_queued")
    return result


def request_from_stop_hook(
    session_id: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    opener: Any = request.urlopen,
) -> Dict[str, Any]:
    try:
        config = load_config(required=False)
    except ControllerConfigError as exc:
        raise AutoHandoffError("auto_handoff_controller_config_invalid") from exc
    if config is None or config.switch_mode != "automatic":
        raise AutoHandoffNotConfigured("auto_handoff_not_configured")
    observed_env = os.environ if env is None else env
    seat = TmuxDriver(config).prove_hook_seat(observed_env)
    if seat is None:
        raise AutoHandoffError("auto_handoff_not_managed_seat")
    _acquire_hook_lease(session_id)
    token = read_control_token()
    return _post_signal(
        config.host,
        config.port,
        token,
        _signal(session_id, seat, str(config.managed_cwd)),
        opener=opener,
    )
