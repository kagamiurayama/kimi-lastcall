"""Small owner-only filesystem primitives used by the controller.

The public controller can send ``/new`` to a managed terminal, so its config,
token and in-flight identity files are authority-bearing.  Keep the rules in
one place: 0700 directories, 0600 regular files, no symlinks, atomic replace.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Dict, Optional


class SecureStateError(RuntimeError):
    pass


def ensure_private_dir(path: Path) -> Path:
    path = path.expanduser()
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
        os.chmod(str(path), 0o700)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SecureStateError("private_dir_invalid")
    if info.st_uid != os.geteuid():
        raise SecureStateError("private_dir_owner_invalid")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise SecureStateError("private_dir_mode_invalid")
    return path


def validate_private_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SecureStateError("private_file_unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SecureStateError("private_file_invalid")
    if info.st_uid != os.geteuid():
        raise SecureStateError("private_file_owner_invalid")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise SecureStateError("private_file_mode_invalid")
    return info


def read_private_text(path: Path) -> str:
    validate_private_file(path)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SecureStateError("private_file_read_failed") from exc


def read_private_json(path: Path) -> Dict[str, Any]:
    try:
        parsed = json.loads(read_private_text(path))
    except (ValueError, json.JSONDecodeError) as exc:
        raise SecureStateError("private_json_invalid") from exc
    if not isinstance(parsed, dict):
        raise SecureStateError("private_json_not_object")
    return parsed


def atomic_write_private(path: Path, text: str, *, exclusive: bool = False) -> None:
    parent = ensure_private_dir(path.parent)
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags, 0o600)
        except FileExistsError:
            raise
        except OSError as exc:
            raise SecureStateError("private_file_create_failed") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_dir(parent)
        except Exception:
            raise
        return

    if path.exists() or path.is_symlink():
        validate_private_file(path)
    temporary = parent / (".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(temporary), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        os.chmod(str(path), 0o600)
        _fsync_dir(parent)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Dict[str, Any], *, exclusive: bool = False) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    atomic_write_private(path, text, exclusive=exclusive)


def remove_private_file(path: Path, expected: Optional[Dict[str, Any]] = None) -> None:
    current = read_private_json(path)
    if expected is not None and current != expected:
        raise SecureStateError("private_file_identity_changed")
    try:
        path.unlink()
        _fsync_dir(path.parent)
    except OSError as exc:
        raise SecureStateError("private_file_remove_failed") from exc


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
