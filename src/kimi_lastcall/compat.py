"""Read-only compatibility checks for unattended Kimi Code seats."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import List, Optional


CACHE_EXPIRY_WARNING = "kimi_cache_expiry_hint_may_block_input"
_CACHE_EXPIRY_LINE = re.compile(
    r"cache_expiry_hint\s*=\s*(true|false)(?:\s*#.*)?$",
)


def tui_config_path() -> Path:
    root = os.environ.get("KIMI_CODE_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".kimi-code") / "tui.toml"


def cache_expiry_hint_disabled(path: Optional[Path] = None) -> bool:
    """Return true only when a top-level, unambiguous false value is present."""
    try:
        lines = (path or tui_config_path()).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    values = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        if not line.startswith("cache_expiry_hint"):
            continue
        match = _CACHE_EXPIRY_LINE.fullmatch(line)
        if match is None:
            return False
        values.append(match.group(1) == "false")
    return values == [True]


def automatic_mode_warnings(switch_mode: str, path: Optional[Path] = None) -> List[str]:
    if switch_mode != "automatic" or cache_expiry_hint_disabled(path):
        return []
    return [CACHE_EXPIRY_WARNING]
