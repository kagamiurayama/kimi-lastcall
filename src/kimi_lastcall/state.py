"""Filesystem locations and small state helpers for kimi-lastcall.

Everything here is local-only: no network, no telemetry.  All paths can be
overridden with environment variables so tests and multi-user installs never
touch the real session or state directories.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

APP_NAME = "kimi-lastcall"

ENV_STATE_DIR = "KIMI_LASTCALL_STATE_DIR"
ENV_SESSIONS_ROOT = "KIMI_LASTCALL_SESSIONS_ROOT"

AUDIT_FILENAME = "audit.jsonl"
SETTINGS_FILENAME = "settings.json"
HANDOFF_MISSING_FILENAME = "handoff_missing.json"
PENDING_FILENAME = "session-start-pending.json"
BINDING_FILENAME = "current-binding.json"
SWITCH_FILENAME = "switch-in-progress.json"
RECEIPT_FILENAME = "last-switch-receipt.json"

# Session ids become file names, so keep the accepted alphabet tight.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def state_dir() -> Path:
    override = os.environ.get(ENV_STATE_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / APP_NAME


def sessions_root() -> Path:
    override = os.environ.get(ENV_SESSIONS_ROOT)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kimi-code" / "sessions"


def valid_session_id(session_id: str) -> bool:
    return bool(SESSION_ID_RE.match(session_id))


def session_digest(session_id: str) -> str:
    """Irreversible short digest for audit records; the raw id stays local."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def marker_path(session_id: str) -> Path:
    """Done marker: the current window finished its handoff letter."""
    return state_dir() / (session_id + ".done")


def count_path(session_id: str) -> Path:
    """On-disk block counter, bound to the session id by file name."""
    return state_dir() / (session_id + ".count")


def skip_path(session_id: str) -> Path:
    """Skip-once arming file, bound to the session id by file name."""
    return state_dir() / (session_id + ".skip")


def skip_used_path(session_id: str) -> Path:
    """Records that this session already spent its one skip."""
    return state_dir() / (session_id + ".skip-used")


def handoff_missing_path() -> Path:
    return state_dir() / HANDOFF_MISSING_FILENAME


def audit_path() -> Path:
    return state_dir() / AUDIT_FILENAME


def settings_path() -> Path:
    return state_dir() / SETTINGS_FILENAME


def pending_path() -> Path:
    return state_dir() / PENDING_FILENAME


def binding_path() -> Path:
    return state_dir() / BINDING_FILENAME


def switch_path() -> Path:
    return state_dir() / SWITCH_FILENAME


def receipt_path() -> Path:
    return state_dir() / RECEIPT_FILENAME
