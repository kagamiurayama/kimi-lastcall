"""Tests for kimi-lastcall install / uninstall / status / template."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimi_lastcall import cli


EXISTING_CONFIG = '[general]\nmodel = "kimi-code/kimi-for-coding"\n'


def make_config(tmp_path: Path, content: str | None = EXISTING_CONFIG) -> Path:
    config = tmp_path / "config.toml"
    if content is not None:
        config.write_text(content, encoding="utf-8")
    return config


def test_dry_run_writes_nothing(tmp_path, capsys):
    config = make_config(tmp_path)
    rc = cli.main(["install", "--dry-run", "--config", str(config)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "no files were written" in out
    assert "[[hooks]]" in out  # planned change is shown
    assert config.read_text(encoding="utf-8") == EXISTING_CONFIG
    assert not (tmp_path / "config.toml.kimi-lastcall.bak").exists()


def test_install_appends_hooks_and_writes_backup(tmp_path):
    config = make_config(tmp_path)
    rc = cli.main(["install", "--config", str(config)])

    assert rc == 0
    content = config.read_text(encoding="utf-8")
    assert EXISTING_CONFIG in content  # existing content untouched
    assert content.count(cli.MANAGED_BEGIN) == 1
    assert content.count("[[hooks]]") == 2
    assert 'event = "Stop"' in content
    assert 'event = "SessionStart"' in content
    backup = tmp_path / "config.toml.kimi-lastcall.bak"
    assert backup.read_text(encoding="utf-8") == EXISTING_CONFIG


def test_install_is_idempotent(tmp_path, capsys):
    config = make_config(tmp_path)
    assert cli.main(["install", "--config", str(config)]) == 0
    after_first = config.read_text(encoding="utf-8")

    assert cli.main(["install", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "already installed" in out
    assert config.read_text(encoding="utf-8") == after_first
    assert after_first.count(cli.MANAGED_BEGIN) == 1


def test_uninstall_restores_original_bytes(tmp_path):
    config = make_config(tmp_path)
    cli.main(["install", "--config", str(config)])
    assert cli.main(["uninstall", "--config", str(config)]) == 0
    assert config.read_text(encoding="utf-8") == EXISTING_CONFIG


def test_uninstall_without_install_is_a_noop(tmp_path, capsys):
    config = make_config(tmp_path)
    assert cli.main(["uninstall", "--config", str(config)]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert config.read_text(encoding="utf-8") == EXISTING_CONFIG


def test_install_into_missing_config_then_uninstall_removes_file(tmp_path):
    config = make_config(tmp_path, content=None)
    assert cli.main(["install", "--config", str(config)]) == 0
    assert config.exists()
    assert cli.MANAGED_BEGIN in config.read_text(encoding="utf-8")

    assert cli.main(["uninstall", "--config", str(config)]) == 0
    assert not config.exists()  # pre-install state (no config) restored


def test_install_does_not_clobber_existing_backup(tmp_path):
    config = make_config(tmp_path)
    backup = tmp_path / "config.toml.kimi-lastcall.bak"
    backup.write_text("sentinel\n", encoding="utf-8")
    assert cli.main(["install", "--config", str(config)]) == 0
    assert backup.read_text(encoding="utf-8") == "sentinel\n"


def test_status_runs_against_empty_state(tmp_path, capsys, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setenv("KIMI_LASTCALL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KIMI_LASTCALL_SESSIONS_ROOT", str(tmp_path / "sessions"))
    rc = cli.main(["status", "--config", str(config)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "hooks installed: no" in out
    assert "state dir" in out


def test_status_reports_installed_and_counts(tmp_path, capsys, monkeypatch):
    config = make_config(tmp_path)
    cli.main(["install", "--config", str(config)])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "sess-test-alpha-0001.count").write_text("2\n", encoding="utf-8")
    (state_dir / "sess-test-alpha-0001.done").write_text("done\n", encoding="utf-8")
    monkeypatch.setenv("KIMI_LASTCALL_STATE_DIR", str(state_dir))

    rc = cli.main(["status", "--config", str(config)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "hooks installed: yes" in out
    assert "2/3" in out
    assert "sess-test-alpha-0001" in out


def test_template_prints_five_sections(tmp_path, capsys):
    rc = cli.main(["template"])
    out = capsys.readouterr().out
    assert rc == 0
    for heading in (
        "## 1. Current state",
        "## 2. Dead ends",
        "## 3. Unresolved items",
        "## 4. First next action",
        "## 5. Optional context",
    ):
        assert heading in out
    assert "None observed in this session." in out


def test_packaged_template_matches_repo_template():
    packaged = (
        Path(cli.__file__).resolve().parent / "templates" / "relay.md"
    ).read_text(encoding="utf-8")
    repo = (Path(__file__).resolve().parents[1] / "templates" / "relay.md").read_text(
        encoding="utf-8"
    )
    assert packaged == repo


def test_sdist_manifest_includes_repo_template():
    manifest = Path(__file__).resolve().parents[1] / "MANIFEST.in"
    entries = {
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "include templates/relay.md" in entries


def test_sdist_manifest_includes_tests_conftest():
    # Without tests/conftest.py the unpacked sdist cannot put src/ on
    # sys.path and every test errors with ModuleNotFoundError — the
    # "run the tests from the sdist" validation silently breaks.
    manifest = Path(__file__).resolve().parents[1] / "MANIFEST.in"
    entries = {
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "include tests/conftest.py" in entries
