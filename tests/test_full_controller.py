"""Full-product tests: authority files, adoption, HTTP, and real fake-tmux flow."""

from __future__ import annotations

import http.cookiejar
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import threading
from urllib import error, request

import pytest

from kimi_lastcall import adoption, binding, cli, config, controller, gate, secure, state, web
from kimi_lastcall.tmux_driver import TmuxDriver, TmuxDriverError


OLD = "session_old-window-0001"
NEW = "session_new-window-0002"


def private_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("KIMI_LASTCALL_STATE_DIR", str(root))
    monkeypatch.setenv("KIMI_LASTCALL_SESSIONS_ROOT", str(tmp_path / "sessions"))
    return root


def executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path


def make_config(tmp_path: Path, tmux_bin: Path, *, port: int = 8765, callback=None):
    cwd = tmp_path / "resident"
    cwd.mkdir(exist_ok=True)
    return config.build_config(
        managed_cwd=str(cwd),
        tmux_socket="resident-socket",
        tmux_session="resident-session",
        tmux_bin=str(tmux_bin),
        handoff_files=["LETTER.md", "HANDOVER.md"],
        on_adopt=callback or [],
        port=port,
        switch_timeout_seconds=3,
        artifact_timeout_seconds=2,
    )


def make_session(tmp_path: Path, cfg, session_id: str, *, used: int = 100_000, archived=False):
    root = tmp_path / "sessions" / "wd_fixture" / session_id
    wire = root / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(
        json.dumps({"id": session_id, "cwd": str(cfg.managed_cwd), "archived": archived}) + "\n",
        encoding="utf-8",
    )
    wire.write_text(
        json.dumps(
            {
                "type": "usage.record",
                "model": "kimi-code/k3",
                "usage": {"inputOther": used, "inputCacheRead": 0, "inputCacheCreation": 0},
            }
        ) + "\n",
        encoding="utf-8",
    )
    return binding.validate_session_artifacts(cfg, session_id, str(cfg.managed_cwd))


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_private_state_rejects_symlink_and_wrong_mode(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    target = root / "target"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o600)
    link = root / "link"
    link.symlink_to(target)
    with pytest.raises(secure.SecureStateError, match="private_file_invalid"):
        secure.read_private_text(link)
    target.chmod(0o640)
    with pytest.raises(secure.SecureStateError, match="private_file_mode_invalid"):
        secure.read_private_text(target)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"host": "0.0.0.0"}, "controller_host_not_loopback"),
        ({"switch_timeout_seconds": None}, "controller_switch_timeout_invalid"),
        ({"handoff_files": ["../LETTER.md"]}, "controller_handoff_file_invalid"),
    ],
)
def test_controller_config_fails_closed(tmp_path, change, code):
    tmux = executable(tmp_path / "tmux")
    kwargs = {
        "managed_cwd": str(tmp_path),
        "tmux_socket": "socket",
        "tmux_session": "session",
        "tmux_bin": str(tmux),
    }
    kwargs.update(change)
    with pytest.raises(config.ControllerConfigError, match=code):
        config.build_config(**kwargs)


def test_tmux_driver_proves_three_way_identity_and_sends_only_new(tmp_path):
    tmux = executable(tmp_path / "tmux")
    cfg = make_config(tmp_path, tmux)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "display-message" in argv:
            return Completed(stdout="/tmp/tmux-1000/resident-socket\tresident-session\t%7\n")
        return Completed()

    driver = TmuxDriver(cfg, runner=runner)
    seat = driver.prove_hook_seat(
        {"TMUX": "/tmp/tmux-1000/resident-socket,123,0", "TMUX_PANE": "%7"}
    )
    assert seat and seat["pane_id"] == "%7"
    driver.send_new()
    send_calls = [row for row in calls if "send-keys" in row]
    assert [row[-1] for row in send_calls] == ["C-u", "/new", "Enter"]
    assert "-l" in send_calls[1]


def test_tmux_driver_rejects_same_cwd_wild_process(tmp_path):
    tmux = executable(tmp_path / "tmux")
    cfg = make_config(tmp_path, tmux)

    def runner(argv, **kwargs):
        return Completed(stdout="/tmp/tmux-1000/other-socket\tother-session\t%7\n")

    assert TmuxDriver(cfg, runner=runner).prove_hook_seat(
        {"TMUX": "/tmp/tmux-1000/resident-socket,1,0", "TMUX_PANE": "%7"}
    ) is None


def test_binding_rejects_archived_and_wrong_cwd(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    make_session(tmp_path, cfg, OLD)
    state_json = tmp_path / "sessions" / "wd_fixture" / OLD / "state.json"
    payload = json.loads(state_json.read_text(encoding="utf-8"))
    payload["archived"] = True
    state_json.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(binding.BindingError, match="session_archived"):
        binding.validate_session_artifacts(cfg, OLD, str(cfg.managed_cwd))
    with pytest.raises(binding.BindingError, match="session_cwd_mismatch"):
        binding.validate_session_artifacts(cfg, OLD, str(tmp_path))


def test_binding_retries_unterminated_wire_tail_then_accepts_completed_row(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    make_session(tmp_path, cfg, OLD)
    wire = tmp_path / "sessions" / "wd_fixture" / OLD / "agents" / "main" / "wire.jsonl"
    complete = wire.read_bytes()
    wire.write_bytes(complete + b'{"type":"assistant",')

    with pytest.raises(binding.BindingError, match="session_wire_not_ready"):
        binding.validate_session_artifacts(cfg, OLD, str(cfg.managed_cwd))

    wire.write_bytes(complete + b'{"type":"assistant","content":"ready"}\n')
    result = binding.validate_session_artifacts(cfg, OLD, str(cfg.managed_cwd))
    assert result["wire_line_count"] == 2


def test_binding_rejects_newline_terminated_malformed_wire_row(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    make_session(tmp_path, cfg, OLD)
    wire = tmp_path / "sessions" / "wd_fixture" / OLD / "agents" / "main" / "wire.jsonl"
    wire.write_bytes(wire.read_bytes() + b'{"type":"assistant",\n')

    with pytest.raises(binding.BindingError, match="session_wire_invalid"):
        binding.validate_session_artifacts(cfg, OLD, str(cfg.managed_cwd))


def test_pending_marker_is_exclusive_and_cannot_be_retargeted(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    first = {
        "schema": adoption.PENDING_SCHEMA,
        "session_id": OLD,
        "cwd": str(tmp_path),
        "tmux_socket": "socket",
        "tmux_session": "session",
        "tmux_pane": "%1",
    }
    assert adoption._freeze_pending(first) == first
    assert adoption._freeze_pending(dict(first)) == first
    changed = dict(first, session_id=NEW)
    with pytest.raises(adoption.AdoptionError, match="pending_conflict"):
        adoption._freeze_pending(changed)


def test_hook_accepts_exact_http_success_envelope_and_rejects_unwrapped_result():
    pending = {
        "schema": adoption.PENDING_SCHEMA,
        "session_id": NEW,
        "cwd": "/tmp/resident",
        "tmux_socket": "socket",
        "tmux_session": "session",
        "tmux_pane": "%1",
    }

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self, limit):
            assert limit == 65537
            return json.dumps(self.payload).encode("utf-8")

    wrapped = {
        "ok": True,
        "result": {"status": "adopted", "session_id": NEW, "receipt": "bound"},
    }
    assert adoption._post_adoption(
        "127.0.0.1", 9879, "token", pending, opener=lambda req, timeout: Response(wrapped)
    ) == wrapped["result"]

    with pytest.raises(adoption.AdoptionError, match="response_invalid"):
        adoption._post_adoption(
            "127.0.0.1",
            9879,
            "token",
            pending,
            opener=lambda req, timeout: Response(wrapped["result"]),
        )


def test_hook_freezes_pending_before_failed_http_and_never_clears_it(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    config.save_config(cfg)
    config.ensure_control_token()

    class HookDriver:
        def __init__(self, ignored):
            pass

        def prove_hook_seat(self, env):
            return {
                "socket_path": "/tmp/tmux-test/resident-socket",
                "session_name": "resident-session",
                "pane_id": "%7",
            }

    def offline(req, timeout):
        raise OSError("offline")

    monkeypatch.setattr(adoption, "TmuxDriver", HookDriver)
    with pytest.raises(adoption.AdoptionError, match="controller_unreachable"):
        adoption.adopt_from_hook(
            {"hook_event_name": "SessionStart", "session_id": OLD, "cwd": str(cfg.managed_cwd)},
            env={"TMUX": "unused", "TMUX_PANE": "%7"},
            opener=offline,
        )
    frozen = secure.read_private_json(state.pending_path())
    assert frozen["session_id"] == OLD
    assert frozen["cwd"] == str(cfg.managed_cwd)


def test_wild_same_cwd_hook_cannot_create_pending(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    config.save_config(cfg)

    class WildDriver:
        def __init__(self, ignored):
            pass

        def prove_hook_seat(self, env):
            return None

    monkeypatch.setattr(adoption, "TmuxDriver", WildDriver)
    with pytest.raises(adoption.AdoptionError, match="not_managed_seat"):
        adoption.adopt_from_hook(
            {"hook_event_name": "SessionStart", "session_id": OLD, "cwd": str(cfg.managed_cwd)},
            env={"TMUX": "wild", "TMUX_PANE": "%99"},
        )
    assert not state.pending_path().exists()


class FakeDriver:
    def __init__(self, cfg, *, on_new=None, online=True):
        self.cfg = cfg
        self.on_new = on_new
        self.online = online
        self.sent = 0

    def verify_managed_session(self):
        if not self.online:
            raise TmuxDriverError("tmux_command_rejected")
        return {"socket_path": "/tmp/tmux-x/%s" % self.cfg.tmux_socket, "session_name": self.cfg.tmux_session, "pane_id": "%7"}

    def pane_identity(self, target):
        return self.verify_managed_session()

    def send_new(self):
        self.sent += 1
        if self.on_new:
            self.on_new()
        return self.verify_managed_session()


def prepare_bound(monkeypatch, tmp_path, cfg):
    artifacts = make_session(tmp_path, cfg, OLD)
    binding.save_binding(binding.make_binding(artifacts, source="test"))
    for name in cfg.handoff_files:
        (cfg.managed_cwd / name).write_text("reviewed handoff\n", encoding="utf-8")
    state.marker_path(OLD).write_text("done\n", encoding="utf-8")


def test_status_and_settings_use_real_capacity(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    prepare_bound(monkeypatch, tmp_path, cfg)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    status = ctl.status()
    assert status["ready_to_switch"] is True
    assert status["usage"]["slider_max"] == 950_000
    assert status["usage"]["writing_headroom"] == 298_576
    updated = ctl.update_settings({"trigger_tokens": 450_000})
    assert updated["usage"]["trigger_tokens"] == 450_000
    with pytest.raises(controller.ControllerError, match="settings_trigger_invalid"):
        ctl.update_settings({"trigger_tokens": 1_050_000})


def test_confirmation_is_bound_to_current_session(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    prepare_bound(monkeypatch, tmp_path, cfg)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    preview = ctl.preview()
    assert preview["ready"] is True
    assert preview["confirmation_phrase"].startswith("NEW ")
    with pytest.raises(controller.ControllerError, match="switch_confirmation_invalid"):
        ctl.confirm({"confirmation": "NEW wrong"})


def test_terminal_send_failure_is_receipted_and_retryable(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    prepare_bound(monkeypatch, tmp_path, cfg)

    class FailedDriver(FakeDriver):
        def send_new(self):
            raise TmuxDriverError("tmux_command_rejected")

    ctl = controller.Controller(cfg, driver=FailedDriver(cfg))
    phrase = ctl.preview()["confirmation_phrase"]
    with pytest.raises(controller.ControllerError, match="failed_closed_terminal_send"):
        ctl.confirm({"confirmation": phrase})
    assert not state.switch_path().exists()
    receipt = secure.read_private_json(state.receipt_path())
    assert receipt["completed"] is False
    assert receipt["status"] == "failed_closed_terminal_send"
    assert ctl.preview()["ready"] is True


def test_binding_timeout_keeps_state_and_late_sessionstart_self_heals(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    prepare_bound(monkeypatch, tmp_path, cfg)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    phrase = ctl.preview()["confirmation_phrase"]
    ticks = iter([0.0, 3.0])
    result = ctl.confirm(
        {"confirmation": phrase},
        monotonic=lambda: next(ticks),
        sleeper=lambda ignored: None,
    )
    assert result["status"] == "failed_closed_binding_timeout"
    assert state.switch_path().exists()

    make_session(tmp_path, cfg, NEW)
    pending = {
        "schema": adoption.PENDING_SCHEMA,
        "session_id": NEW,
        "cwd": str(cfg.managed_cwd),
        "tmux_socket": cfg.tmux_socket,
        "tmux_session": cfg.tmux_session,
        "tmux_pane": "%7",
    }
    secure.atomic_write_json(state.pending_path(), pending, exclusive=True)
    adopted = ctl.adopt(pending)
    assert adopted["status"] == "adopted"
    assert adopted["switch_receipt"]["completed"] is True
    assert not state.switch_path().exists()
    assert binding.load_binding()["session_id"] == NEW


def test_adoption_rejects_unrequested_session_change(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    prepare_bound(monkeypatch, tmp_path, cfg)
    make_session(tmp_path, cfg, NEW)
    pending = {
        "schema": adoption.PENDING_SCHEMA,
        "session_id": NEW,
        "cwd": str(cfg.managed_cwd),
        "tmux_socket": cfg.tmux_socket,
        "tmux_session": cfg.tmux_session,
        "tmux_pane": "%7",
    }
    secure.atomic_write_json(state.pending_path(), pending, exclusive=True)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    with pytest.raises(controller.ControllerError, match="unrequested_session_change"):
        ctl.adopt(pending)
    assert state.pending_path().exists()
    assert binding.load_binding()["session_id"] == OLD


def test_startup_reconciles_frozen_initial_pending(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    make_session(tmp_path, cfg, OLD)
    pending = {
        "schema": adoption.PENDING_SCHEMA,
        "session_id": OLD,
        "cwd": str(cfg.managed_cwd),
        "tmux_socket": cfg.tmux_socket,
        "tmux_session": cfg.tmux_session,
        "tmux_pane": "%7",
    }
    secure.atomic_write_json(state.pending_path(), pending, exclusive=True)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    result = ctl.reconcile_pending()
    assert result and result["status"] == "adopted"
    assert binding.load_binding()["session_id"] == OLD
    assert not state.pending_path().exists()


def test_callback_failure_keeps_pending_fail_closed(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    callback = executable(tmp_path / "reject-callback", "#!/bin/sh\nexit 7\n")
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"), callback=[str(callback)])
    make_session(tmp_path, cfg, OLD)
    pending = {
        "schema": adoption.PENDING_SCHEMA,
        "session_id": OLD,
        "cwd": str(cfg.managed_cwd),
        "tmux_socket": cfg.tmux_socket,
        "tmux_session": cfg.tmux_session,
        "tmux_pane": "%7",
    }
    secure.atomic_write_json(state.pending_path(), pending, exclusive=True)
    ctl = controller.Controller(cfg, driver=FakeDriver(cfg))
    with pytest.raises(controller.ControllerError, match="callback_rejected"):
        ctl.adopt(pending)
    assert state.pending_path().exists()
    assert ctl.status()["ready_to_switch"] is False
    assert "session_adoption_pending" in ctl.status()["blockers"]


def test_callback_receives_verified_local_bindings_without_shell(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    output = tmp_path / "callback.txt"
    callback = executable(
        tmp_path / "callback",
        "#!/bin/sh\nprintf '%s\\n%s\\n' \"$KIMI_LASTCALL_SESSION_ID\" \"$KIMI_LASTCALL_MANAGED_CWD\" > \"$CALLBACK_OUTPUT\"\n",
    )
    monkeypatch.setenv("CALLBACK_OUTPUT", str(output))
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"), callback=[str(callback)])
    make_session(tmp_path, cfg, OLD)
    pending = {
        "schema": adoption.PENDING_SCHEMA,
        "session_id": OLD,
        "cwd": str(cfg.managed_cwd),
        "tmux_socket": cfg.tmux_socket,
        "tmux_session": cfg.tmux_session,
        "tmux_pane": "%7",
    }
    secure.atomic_write_json(state.pending_path(), pending, exclusive=True)
    result = controller.Controller(cfg, driver=FakeDriver(cfg)).adopt(pending)
    assert result["status"] == "adopted"
    assert output.read_text(encoding="utf-8").splitlines() == [OLD, str(cfg.managed_cwd)]


def test_public_status_never_contains_session_id_or_handoff_body(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"))
    prepare_bound(monkeypatch, tmp_path, cfg)
    secret_text = "PRIVATE-HANDOFF-BODY-SENTINEL"
    (cfg.managed_cwd / "LETTER.md").write_text(secret_text, encoding="utf-8")
    rendered = json.dumps(controller.Controller(cfg, driver=FakeDriver(cfg)).status())
    assert OLD not in rendered
    assert secret_text not in rendered


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http_json(url, *, token=None, body=None, opener=request.urlopen):
    headers = {"Host": urlsplit_host(url)}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    method = "GET"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = request.Request(url, data=data, headers=headers, method=method)
    with opener(req, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def urlsplit_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).netloc


def test_http_requires_authentication_and_sets_cookie(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"), port=free_port())
    config.save_config(cfg)
    token = config.ensure_control_token()
    server = web.make_server(cfg, controller=controller.Controller(cfg, driver=FakeDriver(cfg)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % cfg.port
    try:
        with pytest.raises(error.HTTPError) as exc:
            request.urlopen(base + "/api/v1/status", timeout=3)
        assert exc.value.code == 401
        jar = http.cookiejar.CookieJar()
        opener = request.build_opener(request.HTTPCookieProcessor(jar))
        response = opener.open(base + "/?token=" + token, timeout=3)
        assert response.geturl() == base + "/"
        assert jar
        with opener.open(base + "/api/v1/status", timeout=3) as status_response:
            assert json.loads(status_response.read())["ok"] is True
        hostile = request.Request(
            base + "/api/v1/settings",
            data=json.dumps({"trigger_tokens": 200_000}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
        )
        with pytest.raises(error.HTTPError) as origin_error:
            opener.open(hostile, timeout=3)
        assert origin_error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_http_rejects_dns_rebinding_host_even_with_token(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    cfg = make_config(tmp_path, executable(tmp_path / "tmux"), port=free_port())
    config.save_config(cfg)
    token = config.ensure_control_token()
    server = web.make_server(cfg, controller=controller.Controller(cfg, driver=FakeDriver(cfg)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", cfg.port, timeout=3)
    try:
        connection.putrequest("GET", "/api/v1/status", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.putheader("Authorization", "Bearer " + token)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 401
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_cli_configure_writes_owner_only_files_and_done_marker(monkeypatch, tmp_path, capsys):
    private_root(monkeypatch, tmp_path)
    tmux = executable(tmp_path / "tmux")
    cwd = tmp_path / "resident"
    cwd.mkdir()
    args = [
        "configure", "--cwd", str(cwd), "--tmux-socket", "seat", "--tmux-session", "seat",
        "--tmux-bin", str(tmux), "--handoff-file", "LETTER.md",
    ]
    assert cli.main(args + ["--dry-run"]) == 0
    assert not config.controller_path().exists()
    assert cli.main(args) == 0
    assert stat.S_IMODE(config.controller_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(config.token_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(state.state_dir().stat().st_mode) == 0o700
    cfg = config.load_config(required=True)
    artifacts = make_session(tmp_path, cfg, OLD)
    binding.save_binding(binding.make_binding(artifacts, source="test"))
    assert cli.main(["done"]) == 0
    assert state.marker_path(OLD).read_text(encoding="utf-8") == "done\n"
    assert stat.S_IMODE(state.marker_path(OLD).stat().st_mode) == 0o600


def test_packaged_web_panel_is_bilingual_and_has_no_inline_script():
    root = Path(web.__file__).resolve().parent / "web"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    assert "亲笔交接" in html
    assert "Confirm and switch" in script
    assert '<script src="/app.js" defer></script>' in html
    assert "<script>" not in html


def fake_tmux_program(path: Path) -> Path:
    return executable(
        path,
        """#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
socket_name = os.environ['FAKE_TMUX_SOCKET']
session_name = os.environ['FAKE_TMUX_SESSION']
if 'display-message' in args:
    print('/tmp/tmux-test/%s\\t%s\\t%%7' % (socket_name, session_name))
    raise SystemExit(0)
log = pathlib.Path(os.environ['FAKE_TMUX_LOG'])
with log.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(args) + '\\n')
if args[-1] == 'Enter':
    sid = os.environ['FAKE_NEW_SESSION']
    cwd = os.environ['FAKE_CWD']
    root = pathlib.Path(os.environ['KIMI_LASTCALL_SESSIONS_ROOT']) / 'wd_fake' / sid
    wire = root / 'agents' / 'main' / 'wire.jsonl'
    wire.parent.mkdir(parents=True, exist_ok=True)
    (root / 'state.json').write_text(json.dumps({'id': sid, 'cwd': cwd, 'archived': False}) + '\\n')
    wire.write_text(json.dumps({'type':'usage.record','model':'kimi-code/k3','usage':{'inputOther':1,'inputCacheRead':0,'inputCacheCreation':0}}) + '\\n')
    env = os.environ.copy()
    env['TMUX'] = '/tmp/tmux-test/%s,123,0' % socket_name
    env['TMUX_PANE'] = '%7'
    payload = json.dumps({'hook_event_name':'SessionStart','session_id':sid,'cwd':cwd})
    result = subprocess.run([sys.executable, '-m', 'kimi_lastcall.gate'], input=payload, text=True, env=env)
    pathlib.Path(os.environ['FAKE_HOOK_RC']).write_text(str(result.returncode), encoding='utf-8')
    raise SystemExit(result.returncode)
raise SystemExit(0)
""",
    )


def test_fake_tmux_http_sessionstart_end_to_end(monkeypatch, tmp_path):
    private_root(monkeypatch, tmp_path)
    port = free_port()
    tmux = fake_tmux_program(tmp_path / "fake-tmux")
    cfg = make_config(tmp_path, tmux, port=port)
    config.save_config(cfg)
    token = config.ensure_control_token()
    prepare_bound(monkeypatch, tmp_path, cfg)
    log = tmp_path / "tmux.log"
    hook_rc = tmp_path / "hook-rc.txt"
    monkeypatch.setenv("FAKE_TMUX_SOCKET", cfg.tmux_socket)
    monkeypatch.setenv("FAKE_TMUX_SESSION", cfg.tmux_session)
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    monkeypatch.setenv("FAKE_HOOK_RC", str(hook_rc))
    monkeypatch.setenv("FAKE_NEW_SESSION", NEW)
    monkeypatch.setenv("FAKE_CWD", str(cfg.managed_cwd))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    server = web.make_server(cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port
    try:
        _, preview_body = http_json(base + "/api/v1/switch/preview", token=token, body={})
        phrase = preview_body["result"]["confirmation_phrase"]
        _, result_body = http_json(
            base + "/api/v1/switch/confirm",
            token=token,
            body={"confirmation": phrase},
        )
        assert result_body["result"]["status"] == "completed"
        assert binding.load_binding()["session_id"] == NEW
        assert not state.pending_path().exists()
        assert not state.switch_path().exists()
        receipt = secure.read_private_json(state.receipt_path())
        assert receipt["completed"] is True
        assert hook_rc.read_text(encoding="utf-8") == "0"
        assert OLD not in json.dumps(receipt)
        sends = [json.loads(row) for row in log.read_text(encoding="utf-8").splitlines()]
        assert [row[-1] for row in sends] == ["C-u", "/new", "Enter"]
    finally:
        server.shutdown()
        server.server_close()
