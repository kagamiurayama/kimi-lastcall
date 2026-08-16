"""kimi-lastcall command line: install / uninstall / status / template.

The installer performs a minimal, marker-delimited edit of the Kimi Code
``config.toml``.  It never parses or rewrites existing content: our hooks
are appended as one managed block and removed by deleting exactly that
block, so install is idempotent and uninstall restores the original file
byte-for-byte when nothing else changed it.  A backup copy is written
before the first modification as a second, manual recovery path.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import difflib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, binding, gate, secure, state
from .config import (
    ControllerConfigError,
    build_config,
    controller_path,
    ensure_control_token,
    load_config,
    save_config,
)
from .controller import Controller, ControllerError

MANAGED_BEGIN = "# >>> kimi-lastcall managed block (removed by `kimi-lastcall uninstall`) >>>"
MANAGED_END = "# <<< kimi-lastcall managed block <<<"
BACKUP_SUFFIX = ".kimi-lastcall.bak"

HOOK_TIMEOUT = 10
HOOK_EVENTS = ("Stop", "SessionStart")


def default_config_path() -> Path:
    return Path.home() / ".kimi-code" / "config.toml"


def hook_command() -> str:
    found = shutil.which("kimi-lastcall-hook")
    if found:
        return found
    return sys.executable + " -m kimi_lastcall.gate"


def managed_block() -> str:
    lines = [MANAGED_BEGIN]
    for event in HOOK_EVENTS:
        lines.append("[[hooks]]")
        lines.append('event = "%s"' % event)
        lines.append('command = "%s"' % hook_command())
        lines.append("timeout = %d" % HOOK_TIMEOUT)
        lines.append("")
    lines.append(MANAGED_END)
    return "\n".join(lines) + "\n"


def installed(content: str) -> bool:
    return MANAGED_BEGIN in content


def add_block(content: str) -> str:
    if content and not content.endswith("\n"):
        content += "\n"
    return content + managed_block()


def remove_block(content: str) -> str:
    lines = content.splitlines(keepends=True)
    out: List[str] = []
    depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped == MANAGED_BEGIN:
            depth += 1
            continue
        if stripped == MANAGED_END and depth:
            depth -= 1
            continue
        if depth:
            continue
        out.append(line)
    result = "".join(out)
    return result


def cmd_install(args: argparse.Namespace) -> int:
    config = Path(args.config).expanduser()
    try:
        original = config.read_text(encoding="utf-8")
        existed = True
    except FileNotFoundError:
        original = ""
        existed = False
    except OSError as exc:
        print("kimi-lastcall: cannot read %s: %s" % (config, exc), file=sys.stderr)
        return 1

    if installed(original):
        print("kimi-lastcall: hooks already installed in %s (nothing to do)" % config)
        return 0

    updated = add_block(original)
    backup = config.with_name(config.name + BACKUP_SUFFIX)

    if args.dry_run:
        print("kimi-lastcall install --dry-run: no files were written.")
        print("would create hooks in: %s" % config)
        if existed:
            print("would write backup:  %s" % backup)
        else:
            print("would create a new config file (no backup needed)")
        print("planned change:")
        diff = difflib.unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile=str(config) + " (current)",
            tofile=str(config) + " (planned)",
            lineterm="",
        )
        for line in diff:
            print(line)
        return 0

    try:
        config.parent.mkdir(parents=True, exist_ok=True)
        if existed and not backup.exists():
            backup.write_text(original, encoding="utf-8")
        config.write_text(updated, encoding="utf-8")
    except OSError as exc:
        print("kimi-lastcall: install failed: %s" % exc, file=sys.stderr)
        return 1
    print("kimi-lastcall: hooks installed in %s" % config)
    if existed:
        print("backup of the previous config: %s" % backup)
    print("hook command: %s" % hook_command())
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    config = Path(args.config).expanduser()
    try:
        original = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        print("kimi-lastcall: %s does not exist (nothing to do)" % config)
        return 0
    except OSError as exc:
        print("kimi-lastcall: cannot read %s: %s" % (config, exc), file=sys.stderr)
        return 1

    if not installed(original):
        print("kimi-lastcall: no managed block in %s (nothing to do)" % config)
        return 0

    updated = remove_block(original)
    backup = config.with_name(config.name + BACKUP_SUFFIX)
    try:
        if updated.strip():
            config.write_text(updated, encoding="utf-8")
        else:
            # The config only ever held our block; remove the file so the
            # pre-install state (no config) is restored.
            config.unlink()
    except OSError as exc:
        print("kimi-lastcall: uninstall failed: %s" % exc, file=sys.stderr)
        return 1
    print("kimi-lastcall: hooks removed from %s" % config)
    if backup.exists():
        print("pre-install backup still available at: %s" % backup)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = Path(args.config).expanduser()
    try:
        content = config.read_text(encoding="utf-8")
        hooks = installed(content)
    except OSError:
        hooks = False
    print("kimi-lastcall %s" % __version__)
    print("config: %s" % config)
    print("hooks installed: %s" % ("yes" if hooks else "no"))
    directory = state.state_dir()
    print("state dir: %s" % directory)
    print("sessions root: %s" % state.sessions_root())
    if directory.is_dir():
        done = sorted(p.name[: -len(".done")] for p in directory.glob("*.done"))
        counts = []
        for path in sorted(directory.glob("*.count")):
            try:
                counts.append("%s: %s/%d" % (
                    path.name[: -len(".count")],
                    path.read_text(encoding="utf-8").strip(),
                    gate.MAX_BLOCKS,
                ))
            except OSError:
                counts.append("%s: unreadable" % path.name[: -len(".count")])
        print("sessions with completed handoff: %s" % (", ".join(done) if done else "none"))
        print("block counts: %s" % (", ".join(counts) if counts else "none"))
        missing = state.handoff_missing_path()
        if missing.exists():
            try:
                record = json.loads(missing.read_text(encoding="utf-8"))
                print(
                    "handoff_missing: recorded %s (reason=%s, next window notified=%s)"
                    % (
                        record.get("recorded_at", "?"),
                        record.get("reason", "?"),
                        "yes" if record.get("notified") else "no",
                    )
                )
            except (OSError, ValueError):
                print("handoff_missing: record unreadable")
        else:
            print("handoff_missing: none")
    else:
        print("state dir does not exist yet (gate never triggered)")
    try:
        controller_config = load_config(required=False)
        if controller_config is not None:
            controller_status = Controller(controller_config).status()
            print("controller configured: yes")
            print("managed cwd: %s" % controller_status["managed_cwd"])
            print("tmux online: %s" % ("yes" if controller_status["tmux"]["online"] else "no"))
            print("session bound: %s" % ("yes" if controller_status["current_session"] else "no"))
            print("ready to switch: %s" % ("yes" if controller_status["ready_to_switch"] else "no"))
            print("switch mode: %s" % controller_status["switch_mode"])
            if controller_status["blockers"]:
                print("blockers: %s" % ", ".join(controller_status["blockers"]))
        else:
            print("controller configured: no (core Stop hook only)")
    except (ControllerConfigError, ControllerError) as exc:
        print("controller: invalid (%s)" % exc)
    return 0


def cmd_template(args: argparse.Namespace) -> int:
    path = Path(__file__).resolve().parent / "templates" / "relay.md"
    try:
        sys.stdout.write(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print("kimi-lastcall: cannot read template: %s" % exc, file=sys.stderr)
        return 1
    return 0


def _callback_argv(raw: str) -> List[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ControllerConfigError("controller_on_adopt_json_invalid") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ControllerConfigError("controller_on_adopt_json_invalid")
    return parsed


def cmd_configure(args: argparse.Namespace) -> int:
    try:
        value = build_config(
            managed_cwd=args.cwd,
            tmux_socket=args.tmux_socket,
            tmux_session=args.tmux_session,
            tmux_bin=args.tmux_bin,
            handoff_files=args.handoff_file or ["HANDOFF.md"],
            on_adopt=_callback_argv(args.on_adopt_json),
            port=args.port,
            switch_timeout_seconds=args.switch_timeout,
            artifact_timeout_seconds=args.artifact_timeout,
            switch_mode=args.switch_mode,
        )
    except ControllerConfigError as exc:
        print("kimi-lastcall: configuration rejected: %s" % exc, file=sys.stderr)
        return 1
    if args.dry_run:
        print("kimi-lastcall configure --dry-run: no files were written")
        print(json.dumps(value.to_json(), indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    try:
        save_config(value)
        ensure_control_token()
    except (ControllerConfigError, secure.SecureStateError, OSError) as exc:
        print("kimi-lastcall: configuration write failed: %s" % exc, file=sys.stderr)
        return 1
    print("kimi-lastcall: full controller configured")
    print("config: %s" % controller_path())
    print("managed cwd: %s" % value.managed_cwd)
    print("tmux: %s / %s" % (value.tmux_socket, value.tmux_session))
    print("switch mode: %s" % value.switch_mode)
    print("next: kimi-lastcall serve")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        config = load_config(required=True)
        if config is None:
            raise ControllerConfigError("controller_not_configured")
        token = ensure_control_token()
        from . import web

        print("kimi-lastcall control panel: http://%s:%d/?token=%s" % (config.host, config.port, token))
        print("loopback only; press Ctrl-C to stop")
        web.serve(config)
    except KeyboardInterrupt:
        return 0
    except (ControllerConfigError, ControllerError, OSError) as exc:
        print("kimi-lastcall: controller failed: %s" % exc, file=sys.stderr)
        return 1
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    try:
        current = binding.load_binding()
        if current is None:
            raise ControllerError("session_not_bound")
        session_id = str(current["session_id"])
        path = state.marker_path(session_id)
        secure.atomic_write_private(path, "done\n")
    except (binding.BindingError, ControllerError, secure.SecureStateError, OSError) as exc:
        print("kimi-lastcall: cannot mark handoff complete: %s" % exc, file=sys.stderr)
        return 1
    print("kimi-lastcall: handoff marked complete for session %s" % state.session_digest(session_id))
    return 0


def cmd_set_trigger(args: argparse.Namespace) -> int:
    try:
        result = Controller().update_settings({"trigger_tokens": args.tokens})
    except (ControllerConfigError, ControllerError) as exc:
        print("kimi-lastcall: threshold rejected: %s" % exc, file=sys.stderr)
        return 1
    print("kimi-lastcall: trigger set to %s tokens" % result["usage"]["trigger_tokens"])
    if result["usage"]["writing_headroom"] is not None:
        print("writing room: %s tokens" % result["usage"]["writing_headroom"])
    return 0


def cmd_set_mode(args: argparse.Namespace) -> int:
    try:
        current = load_config(required=True)
        if current is None:
            raise ControllerConfigError("controller_not_configured")
        updated = replace(current, switch_mode=args.mode)
        # Round-trip through the public validator before replacing authority
        # state; a typo must never silently relax the human gate.
        validated = build_config(
            managed_cwd=str(updated.managed_cwd),
            tmux_socket=updated.tmux_socket,
            tmux_session=updated.tmux_session,
            tmux_bin=updated.tmux_bin,
            handoff_files=updated.handoff_files,
            on_adopt=updated.on_adopt,
            host=updated.host,
            port=updated.port,
            switch_timeout_seconds=updated.switch_timeout_seconds,
            artifact_timeout_seconds=updated.artifact_timeout_seconds,
            switch_mode=updated.switch_mode,
        )
        save_config(validated)
    except (ControllerConfigError, secure.SecureStateError, OSError) as exc:
        print("kimi-lastcall: mode change rejected: %s" % exc, file=sys.stderr)
        return 1
    print("kimi-lastcall: switch mode set to %s" % validated.switch_mode)
    print("restart `kimi-lastcall serve` for the running controller to load this mode")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kimi-lastcall",
        description="No silent exits. Leave a verifiable handoff.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text, handler in (
        ("install", "install the Relay hooks into the Kimi Code config", cmd_install),
        ("uninstall", "remove the Relay hooks from the Kimi Code config", cmd_uninstall),
        ("status", "show installation and gate state", cmd_status),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument(
            "--config",
            default=str(default_config_path()),
            help="path to the Kimi Code config.toml (default: %(default)s)",
        )
        if name == "install":
            child.add_argument(
                "--dry-run",
                action="store_true",
                help="print the planned changes without writing anything",
            )
        child.set_defaults(handler=handler)

    child = sub.add_parser("template", help="print the Relay handoff template")
    child.set_defaults(handler=cmd_template)

    child = sub.add_parser("configure", help="configure the optional full local controller")
    child.add_argument("--cwd", required=True, help="absolute cwd owned by the managed Kimi seat")
    child.add_argument("--tmux-socket", required=True, help="tmux -L socket name")
    child.add_argument("--tmux-session", required=True, help="managed tmux session name")
    child.add_argument("--tmux-bin", default="tmux", help="tmux executable (default: %(default)s)")
    child.add_argument("--handoff-file", action="append", help="required relative handoff file (repeatable)")
    child.add_argument(
        "--on-adopt-json",
        default="[]",
        help='optional argv-only callback JSON, e.g. \'["/usr/local/bin/rebind"]\'',
    )
    child.add_argument("--port", type=int, default=8765, help="loopback control-panel port")
    child.add_argument("--switch-timeout", type=int, default=30)
    child.add_argument("--artifact-timeout", type=int, default=5)
    child.add_argument(
        "--switch-mode",
        choices=("manual", "automatic"),
        default="manual",
        help="manual confirmation (default) or automatic switch after verified handoff",
    )
    child.add_argument("--dry-run", action="store_true")
    child.set_defaults(handler=cmd_configure)

    child = sub.add_parser("serve", help="run the loopback-only control panel")
    child.set_defaults(handler=cmd_serve)

    child = sub.add_parser("done", help="mark the bound session's handwritten handoff complete")
    child.set_defaults(handler=cmd_done)

    child = sub.add_parser("set-trigger", help="set the handoff threshold in exact 50k steps")
    child.add_argument("tokens", type=int)
    child.set_defaults(handler=cmd_set_trigger)

    child = sub.add_parser("set-mode", help="choose manual or automatic verified switching")
    child.add_argument("mode", choices=("manual", "automatic"))
    child.set_defaults(handler=cmd_set_mode)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()
