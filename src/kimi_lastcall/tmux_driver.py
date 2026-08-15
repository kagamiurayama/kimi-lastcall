"""A narrow tmux driver that can send only the fixed Kimi ``/new`` command."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Dict, Mapping, Optional

from .config import ControllerConfig


PANE_RE = re.compile(r"^%[0-9]+$")


class TmuxDriverError(RuntimeError):
    pass


class TmuxDriver:
    def __init__(
        self,
        config: ControllerConfig,
        *,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.config = config
        self.runner = runner

    def _run(self, argv: list[str]) -> Any:
        try:
            result = self.runner(
                argv,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception as exc:
            raise TmuxDriverError("tmux_command_failed") from exc
        if result.returncode != 0:
            raise TmuxDriverError("tmux_command_rejected")
        return result

    def pane_identity(self, target: str) -> Dict[str, str]:
        result = self._run(
            [
                self.config.tmux_bin,
                "-L",
                self.config.tmux_socket,
                "display-message",
                "-p",
                "-t",
                target,
                "#{socket_path}\t#{session_name}\t#{pane_id}",
            ]
        )
        parts = result.stdout.rstrip("\n").split("\t")
        if len(parts) != 3:
            raise TmuxDriverError("tmux_identity_invalid")
        socket_path, session_name, pane_id = parts
        if (
            Path(socket_path).name != self.config.tmux_socket
            or session_name != self.config.tmux_session
            or PANE_RE.fullmatch(pane_id) is None
        ):
            raise TmuxDriverError("tmux_identity_mismatch")
        return {
            "socket_path": socket_path,
            "session_name": session_name,
            "pane_id": pane_id,
        }

    def verify_managed_session(self) -> Dict[str, str]:
        return self.pane_identity(self.config.tmux_session)

    def prove_hook_seat(self, env: Mapping[str, str]) -> Optional[Dict[str, str]]:
        inherited = str(env.get("TMUX") or "").strip()
        pane = str(env.get("TMUX_PANE") or "").strip()
        if not inherited or PANE_RE.fullmatch(pane) is None:
            return None
        inherited_socket = inherited.split(",", 1)[0].strip()
        if not inherited_socket or Path(inherited_socket).name != self.config.tmux_socket:
            return None
        try:
            observed = self.pane_identity(pane)
        except TmuxDriverError:
            return None
        if (
            os.path.realpath(inherited_socket) != os.path.realpath(observed["socket_path"])
            or observed["session_name"] != self.config.tmux_session
            or observed["pane_id"] != pane
        ):
            return None
        return observed

    def send_new(self) -> Dict[str, str]:
        identity = self.verify_managed_session()
        target = identity["pane_id"]
        self._run(
            [
                self.config.tmux_bin,
                "-L",
                self.config.tmux_socket,
                "send-keys",
                "-t",
                target,
                "C-u",
            ]
        )
        self._run(
            [
                self.config.tmux_bin,
                "-L",
                self.config.tmux_socket,
                "send-keys",
                "-l",
                "-t",
                target,
                "/new",
            ]
        )
        self._run(
            [
                self.config.tmux_bin,
                "-L",
                self.config.tmux_socket,
                "send-keys",
                "-t",
                target,
                "Enter",
            ]
        )
        return identity
