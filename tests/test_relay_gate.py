"""Behavior tests for the Relay Stop/SessionStart gate.

Every test runs the gate as a subprocess against temporary directories with
synthetic data only.  Fixture names (user/operator/session ids) are
deliberately neutral.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from kimi_lastcall import gate


REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
SESSION_A = "sess-test-alpha-0001"
SESSION_B = "sess-test-beta-0002"


def write_wire(
    sessions: Path,
    session_id: str = SESSION_A,
    used_tokens: int = 0,
    model: str = "kimi-code/k3",
    with_usage: bool = True,
) -> Path:
    wire = sessions / "wd_fixture" / session_id / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    if with_usage:
        wire.write_text(
            json.dumps(
                {
                    "type": "usage.record",
                    "model": model,
                    "usage": {
                        "inputOther": used_tokens,
                        "inputCacheRead": 0,
                        "inputCacheCreation": 0,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        wire.write_text('{"type": "message", "text": "no usage here"}\n', encoding="utf-8")
    return wire


def run_gate(
    tmp_path: Path,
    used_tokens: int = 0,
    *,
    session_id: str = SESSION_A,
    model: str = "kimi-code/k3",
    event: str = "Stop",
    stdin: str | None = None,
    extra_env: dict | None = None,
    with_wire: bool = True,
    with_usage: bool = True,
):
    sessions = tmp_path / "sessions"
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    if with_wire:
        write_wire(sessions, session_id, used_tokens, model, with_usage)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KIMI_LASTCALL_")}
    env.update(
        {
            "PYTHONPATH": str(SRC),
            "KIMI_LASTCALL_CONFIG": str(tmp_path / "config.toml"),
            "KIMI_LASTCALL_SESSIONS_ROOT": str(sessions),
            "KIMI_LASTCALL_STATE_DIR": str(state_dir),
        }
    )
    if extra_env:
        env.update(extra_env)
    payload = (
        stdin
        if stdin is not None
        else json.dumps({"hook_event_name": event, "session_id": session_id})
    )
    result = subprocess.run(
        [sys.executable, "-m", "kimi_lastcall.gate"],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    return result, state_dir


def save_settings(state_dir: Path, trigger_tokens: int = 450_000) -> Path:
    path = state_dir / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema": "kimi_lastcall.settings.v1",
                "trigger_tokens": trigger_tokens,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def read_audit(state_dir: Path) -> str:
    path = state_dir / "audit.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- thresholds, capacity, corrupt config, fail-open (ported behavior) ------


def test_official_kimi_code_model_capacity_table_is_complete():
    assert gate.MODEL_CONTEXT == {
        "kimi-code/k3": 1_048_576,
        "kimi-code/k3-256k": 262_144,
        "kimi-code/kimi-for-coding": 262_144,
        "kimi-code/kimi-for-coding-highspeed": 262_144,
    }


def test_k3_256k_uses_256k_capacity(tmp_path):
    result, _ = run_gate(tmp_path, 200_000, model="kimi-code/k3-256k")

    assert result.returncode == 2
    assert "262,144" in result.stderr
    assert "200,000" in result.stderr


def test_local_kimi_config_capacity_overrides_plan_dependent_k3_fallback(tmp_path):
    config = tmp_path / "plan-config.toml"
    config.write_text(
        '[models."kimi-code/k3"]\n'
        'model = "k3"\n'
        'max_context_size = 262_144\n',
        encoding="utf-8",
    )

    result, _ = run_gate(
        tmp_path,
        200_000,
        model="kimi-code/k3",
        extra_env={"KIMI_LASTCALL_CONFIG": str(config)},
    )

    assert result.returncode == 2
    assert "262,144" in result.stderr


def test_unrelated_or_invalid_config_capacity_keeps_known_model_fallback(tmp_path):
    config = tmp_path / "invalid-config.toml"
    config.write_text(
        '[models."kimi-code/k3"]\nmax_context_size = "not-an-integer"\n',
        encoding="utf-8",
    )

    result, _ = run_gate(
        tmp_path,
        800_000,
        model="kimi-code/k3",
        extra_env={"KIMI_LASTCALL_CONFIG": str(config)},
    )

    assert result.returncode == 2
    assert "1,048,576" in result.stderr


def test_unknown_model_capacity_fails_open_with_diagnostic(tmp_path):
    result, state_dir = run_gate(tmp_path, 900_000, model="kimi-code/future-model")

    assert result.returncode == 0
    assert result.stderr == ""
    audit = read_audit(state_dir)
    assert '"action": "model_context_unknown"' in audit
    assert '"model": "kimi-code/future-model"' in audit


def test_saved_450k_threshold_passes_at_440k_and_blocks_at_460k(tmp_path):
    first, state_dir = run_gate(tmp_path, 440_000)
    assert first.returncode == 0
    save_settings(state_dir)

    below, _ = run_gate(tmp_path, 440_000)
    above, _ = run_gate(tmp_path, 460_000)

    assert below.returncode == 0
    assert above.returncode == 2
    assert "450,000" in above.stderr
    # remaining writing room is shown: 1,048,576 - 460,000
    assert "588,576" in above.stderr
    assert "hook never writes the letter or sends terminal input" in above.stderr


def test_block_message_covers_why_how_skip_uninstall(tmp_path):
    result, _ = run_gate(tmp_path, 800_000)
    assert result.returncode == 2
    assert "800,000" in result.stderr  # why: usage over trigger
    assert "1. Write the handoff letter" in result.stderr  # what to do now
    assert "KIMI_LASTCALL_SKIP_ONCE" in result.stderr  # how to skip once
    assert "kimi-lastcall uninstall" in result.stderr  # how to uninstall


def test_missing_settings_uses_default_ratio(tmp_path):
    below, _ = run_gate(tmp_path, 700_000)
    above, _ = run_gate(tmp_path, 800_000)

    assert below.returncode == 0
    assert above.returncode == 2
    assert "750,000" in above.stderr


def test_saved_threshold_above_model_capacity_falls_back(tmp_path):
    _, state_dir = run_gate(tmp_path, 0)
    save_settings(state_dir, trigger_tokens=450_000)

    result, _ = run_gate(
        tmp_path, 250_000, model="kimi-code/kimi-for-coding-highspeed"
    )

    assert result.returncode == 2
    assert "200,000" in result.stderr
    assert "settings_trigger_not_below_context_limit" in read_audit(state_dir)


def test_malformed_settings_fall_back_with_audit(tmp_path):
    _, state_dir = run_gate(tmp_path, 0)
    settings = state_dir / "settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    settings.chmod(0o600)

    result, _ = run_gate(tmp_path, 800_000)

    assert result.returncode == 2
    audit = read_audit(state_dir)
    assert '"action": "settings_fallback"' in audit
    assert "settings_shape_invalid" in audit


def test_insecure_settings_permissions_are_not_followed(tmp_path):
    _, state_dir = run_gate(tmp_path, 0)
    settings = save_settings(state_dir, trigger_tokens=900_000)
    settings.chmod(0o644)

    result, _ = run_gate(tmp_path, 800_000)

    assert result.returncode == 2  # secure 70% fallback, not insecure 900k
    assert "750,000" in result.stderr
    assert "private_file_mode_invalid" in read_audit(state_dir)


def test_invalid_ratio_env_falls_back_with_audit(tmp_path):
    result, state_dir = run_gate(
        tmp_path, 800_000, extra_env={"KIMI_LASTCALL_TRIGGER_RATIO": "not-a-number"}
    )
    assert result.returncode == 2
    assert "750,000" in result.stderr
    assert "env_ratio_invalid" in read_audit(state_dir)


def test_custom_ratio_env_is_honored(tmp_path):
    # k3 context limit is 1,048,576; ratio 0.35 -> trigger 350,000 (pass),
    # ratio 0.25 -> trigger 250,000 (block) for the same 300,000 used tokens.
    below, _ = run_gate(
        tmp_path, 300_000, extra_env={"KIMI_LASTCALL_TRIGGER_RATIO": "0.35"}
    )
    above, _ = run_gate(
        tmp_path, 300_000, extra_env={"KIMI_LASTCALL_TRIGGER_RATIO": "0.25"}
    )
    assert below.returncode == 0
    assert above.returncode == 2
    assert "250,000" in above.stderr


@pytest.mark.parametrize("stdin", ["not-json", "{}", '{"hook_event_name":"Other"}'])
def test_bad_or_unrelated_input_fails_open(tmp_path, stdin):
    result, _ = run_gate(tmp_path, 900_000, stdin=stdin)
    assert result.returncode == 0
    assert result.stderr == ""


def test_bad_input_leaves_non_content_diagnostic(tmp_path):
    result, state_dir = run_gate(tmp_path, 900_000, stdin="not-json")
    assert result.returncode == 0
    assert "input_parse_error" in read_audit(state_dir)


def test_missing_usage_record_fails_open(tmp_path):
    result, _ = run_gate(tmp_path, with_usage=False)
    assert result.returncode == 0


def test_missing_wire_fails_open(tmp_path):
    result, _ = run_gate(tmp_path, 900_000, with_wire=False)
    assert result.returncode == 0


# --- the gate cannot trap you ------------------------------------------------


def test_three_blocks_then_fourth_passes_with_handoff_missing(tmp_path):
    for expected in (2, 2, 2):
        result, state_dir = run_gate(tmp_path, 800_000)
        assert result.returncode == expected
        assert result.stderr  # block message each time

    fourth, state_dir = run_gate(tmp_path, 800_000)
    assert fourth.returncode == 0
    assert "handoff_missing" in fourth.stderr

    record = json.loads((state_dir / "handoff_missing.json").read_text(encoding="utf-8"))
    assert record["reason"] == "block_limit_reached"
    assert record["notified"] is False
    assert SESSION_A not in json.dumps(record)  # digest, not raw id

    audit = read_audit(state_dir)
    assert '"action": "block"' in audit
    assert '"action": "handoff_missing"' in audit

    fifth, _ = run_gate(tmp_path, 800_000)
    assert fifth.returncode == 0  # stays open, never traps


def test_block_count_persisted_on_disk(tmp_path):
    run_gate(tmp_path, 800_000)
    run_gate(tmp_path, 800_000)
    _, state_dir = run_gate(tmp_path, 700_000)  # below trigger: no new block
    count_file = state_dir / (SESSION_A + ".count")
    assert count_file.read_text(encoding="utf-8").strip() == "2"


def test_corrupt_count_fails_open_but_is_not_silent(tmp_path):
    _, state_dir = run_gate(tmp_path, 0)
    (state_dir / (SESSION_A + ".count")).write_text("garbage\n", encoding="utf-8")

    result, state_dir = run_gate(tmp_path, 800_000)

    assert result.returncode == 0  # fail-open, not blocked as if fresh
    assert "unreadable" in result.stderr
    audit = read_audit(state_dir)
    assert "count_corrupt" in audit
    assert '"action": "handoff_missing"' in audit


def test_skip_once_env_releases_exactly_once(tmp_path):
    first, _ = run_gate(tmp_path, 800_000)
    assert first.returncode == 2

    skipped, state_dir = run_gate(
        tmp_path, 800_000, extra_env={"KIMI_LASTCALL_SKIP_ONCE": SESSION_A}
    )
    assert skipped.returncode == 0
    assert "skip_once" in read_audit(state_dir)

    again, _ = run_gate(
        tmp_path, 800_000, extra_env={"KIMI_LASTCALL_SKIP_ONCE": SESSION_A}
    )
    assert again.returncode == 2  # one skip per session, then blocks resume


def test_skip_once_env_must_name_exact_session(tmp_path):
    wrong, _ = run_gate(
        tmp_path, 800_000, extra_env={"KIMI_LASTCALL_SKIP_ONCE": SESSION_B}
    )
    assert wrong.returncode == 2  # other session's skip does not apply

    sloppy, _ = run_gate(
        tmp_path, 800_000, extra_env={"KIMI_LASTCALL_SKIP_ONCE": "1"}
    )
    assert sloppy.returncode == 2


def test_skip_once_file_releases_exactly_once(tmp_path):
    _, state_dir = run_gate(tmp_path, 800_000)
    skip_file = state_dir / (SESSION_A + ".skip")
    skip_file.write_text(SESSION_A + "\n", encoding="utf-8")

    skipped, state_dir = run_gate(tmp_path, 800_000)
    assert skipped.returncode == 0
    assert not skip_file.exists()  # consumed

    again, _ = run_gate(tmp_path, 800_000)
    assert again.returncode == 2


def test_done_marker_releases_only_its_own_session(tmp_path):
    _, state_dir = run_gate(tmp_path, 800_000, session_id=SESSION_A)
    marker = state_dir / (SESSION_A + ".done")
    marker.write_text("done\n", encoding="utf-8")
    # A marker older than the gate's demand is stale: it does not release.
    old = time.time() - 8 * 86400  # the incident shape: an 8-day-old letter
    os.utime(marker, (old, old))
    blocked, _ = run_gate(tmp_path, 800_000, session_id=SESSION_A)
    assert blocked.returncode == 2

    # Re-checked and re-touched after the demand, the marker releases.
    marker.touch()
    released, _ = run_gate(tmp_path, 800_000, session_id=SESSION_A)
    assert released.returncode == 0

    other, _ = run_gate(tmp_path, 800_000, session_id=SESSION_B)
    assert other.returncode == 2  # markers never cross sessions


def test_letter_written_before_first_demand_needs_retouch(tmp_path):
    """The incident shape: letter + marker days before the gate ever demanded.

    A long-lived session wrote its letter early (or the window kept running
    after writing it).  When usage finally crosses the trigger, the gate must
    not hand off behind that stale letter — it blocks once, and only a marker
    re-touched after the demand releases.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    marker = state_dir / (SESSION_A + ".done")
    marker.write_text("done\n", encoding="utf-8")
    old = time.time() - 8 * 86400
    os.utime(marker, (old, old))

    blocked, state_dir = run_gate(tmp_path, 800_000, session_id=SESSION_A)
    assert blocked.returncode == 2
    assert (state_dir / (SESSION_A + ".demand")).exists()  # demand anchored

    marker.touch()  # letter re-checked in this window, marker re-touched
    released, _ = run_gate(tmp_path, 800_000, session_id=SESSION_A)
    assert released.returncode == 0


def test_release_is_one_shot_and_redemands_fresh_letter(tmp_path):
    """After a release the marker goes stale: if the window keeps running,
    the next crossing demands a fresh letter instead of re-releasing."""
    _, state_dir = run_gate(tmp_path, 800_000, session_id=SESSION_A)
    marker = state_dir / (SESSION_A + ".done")
    marker.touch()

    released, state_dir = run_gate(tmp_path, 800_000, session_id=SESSION_A)
    assert released.returncode == 0
    # The marker file stays (controller preflight re-checks it) but the
    # demand has been advanced strictly past it.
    demand = state_dir / (SESSION_A + ".demand")
    assert demand.stat().st_mtime > marker.stat().st_mtime

    blocked_again, _ = run_gate(tmp_path, 800_000, session_id=SESSION_A)
    assert blocked_again.returncode == 2  # fresh letter demanded


def test_next_window_is_told_about_handoff_missing(tmp_path):
    for _ in range(4):
        run_gate(tmp_path, 800_000)  # ends with handoff_missing recorded

    start, state_dir = run_gate(tmp_path, 0, session_id=SESSION_B, event="SessionStart")
    assert start.returncode == 0
    assert "handoff_missing" in start.stdout
    assert "without a verified handoff" in start.stdout

    record = json.loads((state_dir / "handoff_missing.json").read_text(encoding="utf-8"))
    assert record["notified"] is True

    again, _ = run_gate(tmp_path, 0, session_id=SESSION_B, event="SessionStart")
    assert again.returncode == 0
    assert again.stdout == ""  # told exactly once


def test_session_start_is_quiet_without_handoff_missing(tmp_path):
    result, _ = run_gate(tmp_path, 0, session_id=SESSION_B, event="SessionStart")
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


# --- audit privacy ------------------------------------------------------------


def test_audit_uses_digest_and_never_raw_session_id(tmp_path):
    run_gate(tmp_path, 800_000)
    run_gate(tmp_path, 800_000)
    _, state_dir = run_gate(tmp_path, 800_000)
    audit = read_audit(state_dir)
    assert '"action": "block"' in audit
    assert SESSION_A not in audit
    assert '"session":' in audit  # digest present
