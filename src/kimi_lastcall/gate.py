"""Relay gate: the kimi-lastcall Stop/SessionStart hook entry point.

Reads one Kimi Code hook JSON payload from stdin and decides whether the
session may stop.  Design rules, in order:

1. Never trap the user.  Every parse failure, missing file, or unreadable
   state fails open (exit 0) and leaves a non-content diagnostic in the
   audit log.
2. Never write the handoff for the model.  The gate only blocks, warns,
   and checks a session-bound done marker.
3. Never block forever.  A session is blocked at most MAX_BLOCKS times;
   the next stop is allowed with a loud warning and a ``handoff_missing``
   record that the next window is told about.

Audit records contain decisions and error classes only — never handoff
content — and identify sessions by an irreversible digest.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, Optional, Tuple

from . import secure, state

TRIGGER_MIN_TOKENS = 50_000
TRIGGER_MAX_TOKENS = 950_000
TRIGGER_STEP_TOKENS = 50_000
DEFAULT_TRIGGER_RATIO = 0.70
ENV_TRIGGER_RATIO = "KIMI_LASTCALL_TRIGGER_RATIO"
ENV_SKIP_ONCE = "KIMI_LASTCALL_SKIP_ONCE"
ENV_CONFIG_PATH = "KIMI_LASTCALL_CONFIG"

MAX_BLOCKS = 3

MODEL_CONTEXT = {
    "kimi-code/k3": 1_048_576,
    "kimi-code/k3-256k": 262_144,
    "kimi-code/kimi-for-coding": 262_144,
    "kimi-code/kimi-for-coding-highspeed": 262_144,
}

SETTINGS_SCHEMA = "kimi_lastcall.settings.v1"
HANDOFF_MISSING_SCHEMA = "kimi_lastcall.handoff_missing.v1"

MODEL_SECTION_RE = re.compile(r'^\s*\[models\."([^"]+)"\]\s*(?:#.*)?$')
MAX_CONTEXT_RE = re.compile(
    r"^\s*max_context_size\s*=\s*([0-9][0-9_]*)\s*(?:#.*)?$"
)

BLOCK_MESSAGE = """[kimi-lastcall] This session has used {used_tokens:,} of {context_limit:,} tokens, at/over the Relay trigger of {trigger_tokens:,}.
About {remaining_tokens:,} tokens of writing room remain — leave the handoff now, while it still fits.

Relay: no silent exits. Leave a verifiable handoff:
  1. Write the handoff letter yourself, in this window, with five sections:
     current state / dead ends (or "None observed in this session.") /
     unresolved items / first next action / optional context.
     Full template: `kimi-lastcall template` (or templates/relay.md in the repo).
  2. Save it where the next window will read it (e.g. HANDOFF.md in your project root).
  3. Mark this session done:
       touch {marker}
  4. Stop again — manual mode lets you through; automatic mode queues one verified switch.

Already touched the marker before this block? It only counts if it is newer than this demand —
re-check the letter still reflects this window, then touch the marker again.

This is block {block_count}/{max_blocks} for this session. After {max_blocks} blocked stops the gate
steps aside, lets the session end, and records handoff_missing so the next window knows.
The hook never writes the letter or sends terminal input. In automatic mode it only asks the
authenticated loopback controller, which rechecks the managed seat before sending fixed /new.

Skip once (this session only):
  {skip_env}={session_id}        # value must equal this session id
  or: touch {skip_file}
Uninstall:
  kimi-lastcall uninstall
"""

LIMIT_REACHED_MESSAGE = """[kimi-lastcall] WARNING: session reached the {max_blocks}-block limit with no completed handoff.
The gate is stepping aside (fail-open) and recording handoff_missing.
The next window will be told it starts without a verified handoff.
"""

STATE_CORRUPT_MESSAGE = """[kimi-lastcall] WARNING: local block-count state for this session is unreadable ({code}).
The gate is failing open rather than pretending this never happened; see the audit log.
"""

HANDOFF_MISSING_NOTICE = (
    "[kimi-lastcall] The previous window ended over the context threshold without leaving a "
    "handoff letter (handoff_missing). You are starting without a verified handoff — do not "
    "trust prior session state until you have checked for a handoff file yourself."
)

AUTO_HANDOFF_WARNING = """[kimi-lastcall] The handoff is complete, but the automatic switch request failed ({code}).
The Stop hook is failing open so it cannot trap the session. Use the local panel's human-confirmed
fallback after checking the controller. The hook itself sent no terminal input; if the response was
lost after acceptance, the controller's persisted status remains authoritative and idempotent.
"""


def marker_fresh(session_id: str) -> bool:
    """The done marker only counts if it is newer than the latest demand.

    Sessions are long-lived: a letter written days ago (marker touched, no
    switch taken, work continued) must not release a handoff today.  The
    anchor is the gate's own latest demand, not the session start — a letter
    can be newer than the session start and still be stale.
    """
    try:
        marker_mtime = state.marker_path(session_id).stat().st_mtime
        demand_mtime = state.demand_path(session_id).stat().st_mtime
    except OSError:
        return False
    # ``>=`` so a same-second block→letter→touch on coarse-mtime filesystems
    # still counts; a stale marker is days old and never collides.
    return marker_mtime >= demand_mtime


def stamp_demand(session_id: str) -> None:
    """Anchor "the gate demanded a letter now".  Best-effort, never raises."""
    try:
        state.state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        state.demand_path(session_id).touch()
    except OSError as exc:
        audit({"action": "demand_stamp_failed", "code": type(exc).__name__}, session_id)


def consume_marker(session_id: str) -> None:
    """One-shot: advance the demand strictly past the current marker.

    The marker file stays — the controller's switch preflight re-checks it —
    but it is now stale relative to the demand: if the window keeps running
    after this release, the next crossing demands a fresh letter instead of
    re-releasing behind the old one.
    """
    try:
        marker_mtime = state.marker_path(session_id).stat().st_mtime
    except OSError:
        marker_mtime = 0.0
    try:
        demand = state.demand_path(session_id)
        demand.touch()
        now = time.time()
        # Strictly newer than the marker even on coarse-mtime filesystems.
        os.utime(demand, (now, max(now, marker_mtime + 1.0)))
    except OSError as exc:
        audit({"action": "marker_consume_failed", "code": type(exc).__name__}, session_id)


def audit(record: Dict[str, Any], session_id: Optional[str] = None) -> None:
    """Append one non-content diagnostic record.  Never raises."""
    try:
        entry = dict(record)
        entry["ts"] = time.time()
        if session_id:
            entry["session"] = state.session_digest(session_id)
        directory = state.state_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with state.audit_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def find_wire(session_id: str) -> Optional[Path]:
    matches = sorted(
        state.sessions_root().glob("wd_*/" + session_id + "/agents/main/wire.jsonl")
    )
    return matches[0] if len(matches) == 1 else None


def configured_model_context(model: str) -> Optional[int]:
    """Read one official Kimi model field without parsing unrelated TOML.

    Kimi's own config binds the wire model alias to ``max_context_size``.
    That value is more precise than the fallback table for plan-dependent
    aliases such as ``kimi-code/k3``.  This deliberately tiny reader accepts
    only the documented quoted model-section shape and a positive integer;
    anything else leaves the fallback behavior unchanged.
    """
    override = os.environ.get(ENV_CONFIG_PATH)
    path = Path(override).expanduser() if override else Path.home() / ".kimi-code" / "config.toml"
    in_target_section = False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                section = MODEL_SECTION_RE.match(line)
                if section:
                    in_target_section = section.group(1) == model
                    continue
                if line.lstrip().startswith("["):
                    in_target_section = False
                    continue
                if not in_target_section:
                    continue
                value = MAX_CONTEXT_RE.match(line)
                if value:
                    parsed = int(value.group(1).replace("_", ""))
                    return parsed if parsed > 0 else None
    except (OSError, UnicodeError, ValueError):
        return None
    return None


def last_usage(wire_path: Path) -> Tuple[int, int, str]:
    """Read-only scan of the last usage.record; unknown capacity fails open."""
    last: Optional[Dict[str, Any]] = None
    with wire_path.open("rb") as handle:
        for raw in handle:
            if b'"usage.record"' not in raw:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if item.get("type") == "usage.record" and isinstance(item.get("usage"), dict):
                last = item
    if last is None:
        return 0, 0, ""
    model = str(last.get("model") or "")
    usage = last["usage"]
    used = sum(
        max(0, int(usage.get(key) or 0))
        for key in ("inputOther", "inputCacheRead", "inputCacheCreation")
    )
    context_limit = configured_model_context(model)
    if context_limit is None:
        context_limit = MODEL_CONTEXT.get(model, 0)
    return used, context_limit, model


def _clamp_trigger(tokens: int, context_limit: int) -> int:
    tokens = max(TRIGGER_MIN_TOKENS, min(tokens, TRIGGER_MAX_TOKENS))
    if tokens >= context_limit:
        tokens = max(1, context_limit - TRIGGER_STEP_TOKENS)
    return tokens


def ratio_trigger_tokens(context_limit: int) -> Tuple[int, Optional[str]]:
    raw = os.environ.get(ENV_TRIGGER_RATIO)
    warning = None
    if raw is None:
        ratio = DEFAULT_TRIGGER_RATIO
    else:
        try:
            ratio = float(raw)
        except (TypeError, ValueError):
            ratio = DEFAULT_TRIGGER_RATIO
            warning = "env_ratio_invalid"
    ratio = max(0.05, min(ratio, 0.99))
    rounded = int(round(context_limit * ratio / TRIGGER_STEP_TOKENS)) * TRIGGER_STEP_TOKENS
    return _clamp_trigger(rounded, context_limit), warning


def read_trigger_tokens(context_limit: int) -> Tuple[int, str, Optional[str]]:
    """Explicit settings.json wins; anything invalid falls back to the ratio."""
    fallback, ratio_warning = ratio_trigger_tokens(context_limit)
    path = state.settings_path()
    if not path.exists() and not path.is_symlink():
        source = "env_ratio" if os.environ.get(ENV_TRIGGER_RATIO) else "default_ratio"
        return fallback, source, ratio_warning
    try:
        parsed = secure.read_private_json(path)
    except secure.SecureStateError as exc:
        return fallback, "invalid_fallback", str(exc) or type(exc).__name__
    try:
        if not isinstance(parsed, dict) or set(parsed) != {"schema", "trigger_tokens", "updated_at"}:
            raise ValueError("settings_shape_invalid")
        if parsed.get("schema") != SETTINGS_SCHEMA:
            raise ValueError("settings_schema_invalid")
        value = parsed.get("trigger_tokens")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("settings_trigger_invalid")
        if (
            not TRIGGER_MIN_TOKENS <= value <= TRIGGER_MAX_TOKENS
            or value % TRIGGER_STEP_TOKENS
        ):
            raise ValueError("settings_trigger_invalid")
        if value >= context_limit:
            raise ValueError("settings_trigger_not_below_context_limit")
        return value, "settings", None
    except ValueError as exc:
        return fallback, "invalid_fallback", str(exc)


def trigger_slider_max(context_limit: int) -> int:
    """Largest selectable 50k step; unknown models use a conservative cap."""
    if context_limit <= 0:
        return 250_000
    below_limit = ((context_limit - 1) // TRIGGER_STEP_TOKENS) * TRIGGER_STEP_TOKENS
    return max(TRIGGER_MIN_TOKENS, min(TRIGGER_MAX_TOKENS, below_limit))


def write_trigger_tokens(trigger_tokens: int, context_limit: int = 0) -> None:
    """Persist one explicit threshold without allowing a dead gate."""
    if (
        isinstance(trigger_tokens, bool)
        or not isinstance(trigger_tokens, int)
        or trigger_tokens < TRIGGER_MIN_TOKENS
        or trigger_tokens > trigger_slider_max(context_limit)
        or trigger_tokens % TRIGGER_STEP_TOKENS
    ):
        raise ValueError("settings_trigger_invalid")
    if context_limit > 0 and trigger_tokens >= context_limit:
        raise ValueError("settings_trigger_not_below_context_limit")
    secure.atomic_write_json(
        state.settings_path(),
        {
            "schema": SETTINGS_SCHEMA,
            "trigger_tokens": trigger_tokens,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


def read_block_count(session_id: str) -> Tuple[int, Optional[str]]:
    """On-disk counter bound to the session id.  Read failure fails open.

    A corrupt counter is never silently treated as "never triggered": the
    caller gets a warning code and behaves as if the limit were reached.
    """
    try:
        raw = state.count_path(session_id).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0, None
    except OSError as exc:
        return MAX_BLOCKS, "count_read_error:" + type(exc).__name__
    try:
        count = int(raw)
    except ValueError:
        return MAX_BLOCKS, "count_corrupt"
    if count < 0:
        return MAX_BLOCKS, "count_corrupt"
    return count, None


def write_block_count(session_id: str, count: int) -> bool:
    try:
        directory = state.state_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.count_path(session_id).write_text(str(count) + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        audit({"action": "count_write_error", "code": type(exc).__name__}, session_id)
        return False


def skip_armed(session_id: str) -> bool:
    """Skip-once must name the exact session id; anything else is ignored."""
    raw = os.environ.get(ENV_SKIP_ONCE)
    if raw and session_id in re.split(r"[,\s]+", raw.strip()):
        return True
    return state.skip_path(session_id).exists()


def consume_skip(session_id: str) -> None:
    try:
        state.skip_path(session_id).unlink()
    except OSError:
        pass
    try:
        state.skip_used_path(session_id).write_text("1\n", encoding="utf-8")
    except OSError:
        pass


def record_handoff_missing(session_id: str, reason: str) -> None:
    try:
        directory = state.state_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "schema": HANDOFF_MISSING_SCHEMA,
            "session": state.session_digest(session_id),
            "reason": reason,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "notified": False,
        }
        state.handoff_missing_path().write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def allow_with_handoff_missing(session_id: str, reason: str, message: str) -> int:
    audit({"action": "handoff_missing", "reason": reason}, session_id)
    record_handoff_missing(session_id, reason)
    print(message, file=sys.stderr)
    return 0


def handle_session_start_notice() -> int:
    """Tell the new window if the previous one left without a handoff."""
    try:
        raw = state.handoff_missing_path().read_text(encoding="utf-8")
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("schema") != HANDOFF_MISSING_SCHEMA:
            raise ValueError("handoff_missing_shape_invalid")
        if record.get("notified"):
            return 0
        print(HANDOFF_MISSING_NOTICE)
        record["notified"] = True
        state.handoff_missing_path().write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError) as exc:
        audit({"action": "handoff_missing_read_error", "code": str(exc) or type(exc).__name__})
    return 0


def handle_session_start(payload: Dict[str, Any]) -> int:
    """Show the continuity notice, then adopt only a configured managed seat."""
    notice_result = handle_session_start_notice()
    from . import adoption

    try:
        adoption.adopt_from_hook(payload)
    except adoption.AdoptionNotConfigured:
        pass
    except Exception as exc:
        # SessionStart is observational in Kimi: a non-zero exit cannot undo
        # the new window.  Keep the pending marker fail-closed and make the
        # failure visible instead of pretending that external surfaces moved.
        audit({"action": "session_adoption_failed", "code": str(exc) or type(exc).__name__})
        print("[kimi-lastcall] SessionStart adoption failed: %s" % (str(exc) or type(exc).__name__), file=sys.stderr)
        return 2
    return notice_result


def handle_stop(session_id: str) -> int:
    marker_ready = marker_fresh(session_id)
    wire = find_wire(session_id)
    if wire is None:
        return 0
    used_tokens, context_limit, model = last_usage(wire)
    if context_limit <= 0:
        audit({"action": "model_context_unknown", "model": model}, session_id)
        return 0
    trigger_tokens, settings_source, settings_warning = read_trigger_tokens(context_limit)
    if settings_warning:
        audit(
            {"action": "settings_fallback", "code": settings_warning},
            session_id,
        )
    if used_tokens < trigger_tokens:
        return 0

    if marker_ready:
        from . import automation

        try:
            result = automation.request_from_stop_hook(session_id)
        except automation.AutoHandoffNotConfigured:
            # Manual mode: the release is one-shot too, or a session that
            # keeps running after its letter would re-release on a stale
            # marker at the next crossing.
            consume_marker(session_id)
            audit({"action": "handoff_released_manual"}, session_id)
            return 0
        except Exception as exc:
            code = str(exc) or type(exc).__name__
            audit({"action": "auto_handoff_failed_open", "code": code}, session_id)
            print(AUTO_HANDOFF_WARNING.format(code=code), file=sys.stderr)
            return 0
        audit(
            {
                "action": "auto_handoff_accepted",
                "request_status": result.get("status"),
            },
            session_id,
        )
        consume_marker(session_id)
        return 0

    if skip_armed(session_id):
        if state.skip_used_path(session_id).exists():
            audit({"action": "skip_ignored_already_used"}, session_id)
        else:
            consume_skip(session_id)
            audit({"action": "skip_once"}, session_id)
            return 0

    count, count_warning = read_block_count(session_id)
    if count_warning:
        audit({"action": "state_failopen", "code": count_warning}, session_id)
        return allow_with_handoff_missing(
            session_id,
            count_warning,
            STATE_CORRUPT_MESSAGE.format(code=count_warning),
        )
    if count >= MAX_BLOCKS:
        return allow_with_handoff_missing(
            session_id,
            "block_limit_reached",
            LIMIT_REACHED_MESSAGE.format(max_blocks=MAX_BLOCKS),
        )

    if not write_block_count(session_id, count + 1):
        # Without a persisted counter a block could repeat forever; fail open.
        return 0
    stamp_demand(session_id)

    audit(
        {
            "action": "block",
            "block_count": count + 1,
            "used_tokens": used_tokens,
            "context_limit": context_limit,
            "trigger_tokens": trigger_tokens,
            "settings_source": settings_source,
            "model": model,
        },
        session_id,
    )
    print(
        BLOCK_MESSAGE.format(
            used_tokens=used_tokens,
            context_limit=context_limit,
            trigger_tokens=trigger_tokens,
            remaining_tokens=max(0, context_limit - used_tokens),
            block_count=count + 1,
            max_blocks=MAX_BLOCKS,
            marker=state.marker_path(session_id),
            skip_env=ENV_SKIP_ONCE,
            skip_file=state.skip_path(session_id),
            session_id=session_id,
        ),
        file=sys.stderr,
    )
    return 2


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        audit({"action": "input_parse_error"})
        return 0
    if not isinstance(payload, dict):
        audit({"action": "input_parse_error", "code": "payload_not_object"})
        return 0
    event = str(payload.get("hook_event_name") or "")
    if event == "SessionStart":
        return handle_session_start(payload)
    if event != "Stop":
        return 0
    session_id = str(payload.get("session_id") or "")
    if not session_id or not state.valid_session_id(session_id):
        if session_id:
            audit({"action": "invalid_session_id"})
        return 0
    try:
        return handle_stop(session_id)
    except Exception as exc:  # last-resort fail-open
        audit({"action": "internal_error", "code": type(exc).__name__}, session_id)
        return 0


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()
