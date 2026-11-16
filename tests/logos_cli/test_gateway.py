"""Tests for logos_cli.gateway (PID-file based gateway control)."""

import signal
from types import SimpleNamespace

import pytest

import logos_cli.gateway as gw


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGOS_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOGOS_PORT", raising=False)
    monkeypatch.delenv("HERMES_PORT", raising=False)
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    return tmp_path


def test_gateway_port_resolution(monkeypatch):
    assert gw.gateway_port() == 8091
    monkeypatch.setenv("HERMES_PORT", "18100")  # removed alias: ignored
    assert gw.gateway_port() == 8091
    monkeypatch.setenv("LOGOS_PORT", "18200")
    assert gw.gateway_port() == 18200


def test_status_not_running(monkeypatch, capsys):
    monkeypatch.setattr(gw, "get_running_pid", lambda: None)
    monkeypatch.setattr(gw, "_port_listening", lambda port: False)
    with pytest.raises(SystemExit) as exc:
        gw.gateway_command(SimpleNamespace(gateway_command="status"))
    assert exc.value.code == 1
    assert "not running" in capsys.readouterr().out


def test_status_running(monkeypatch, capsys, _home):
    monkeypatch.setattr(gw, "get_running_pid", lambda: 4242)
    monkeypatch.setattr(gw, "_port_listening", lambda port: True)
    gw.gateway_command(SimpleNamespace(gateway_command="status"))
    out = capsys.readouterr().out
    assert "running (PID 4242)" in out
    assert "Listening on port 8091" in out
    assert str(_home) in out


def test_stop_when_not_running_sends_no_signal(monkeypatch, capsys):
    monkeypatch.setattr(gw, "get_running_pid", lambda: None)
    monkeypatch.setattr(gw.os, "kill", lambda *a: pytest.fail("must not signal anything"))
    gw.gateway_command(SimpleNamespace(gateway_command="stop"))
    assert "not running" in capsys.readouterr().out


def test_stop_signals_only_the_pid_file_process(monkeypatch, capsys):
    sent = []
    alive = {"v": True}

    def fake_kill(pid, sig):
        if sig == 0:
            if not alive["v"]:
                raise ProcessLookupError
            return
        sent.append((pid, sig))
        alive["v"] = False

    monkeypatch.setattr(gw, "get_running_pid", lambda: 4242)
    monkeypatch.setattr(gw.os, "kill", fake_kill)
    monkeypatch.setattr(gw, "_port_listening", lambda port: False)
    gw.gateway_command(SimpleNamespace(gateway_command="stop"))
    assert sent == [(4242, signal.SIGTERM)]
    assert "Stopped gateway (PID 4242)" in capsys.readouterr().out


def test_stop_escalates_to_sigkill(monkeypatch):
    sent = []

    def fake_kill(pid, sig):
        if sig != 0:
            sent.append(sig)

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(gw, "get_running_pid", lambda: 4242)
    monkeypatch.setattr(gw.os, "kill", fake_kill)
    monkeypatch.setattr(gw.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(gw, "_port_listening", lambda port: False)
    assert gw.stop_gateway(grace_seconds=3) == 4242
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_start_when_already_running_does_not_spawn(monkeypatch, capsys):
    monkeypatch.setattr(gw, "get_running_pid", lambda: 4242)
    monkeypatch.setattr(gw, "run_gateway_detached", lambda: pytest.fail("must not spawn"))
    gw.gateway_command(SimpleNamespace(gateway_command="start"))
    assert "already running" in capsys.readouterr().out


def test_start_refuses_when_port_is_taken(monkeypatch, capsys):
    monkeypatch.setattr(gw, "get_running_pid", lambda: None)
    monkeypatch.setattr(gw, "_port_listening", lambda port: True)
    monkeypatch.setattr(gw, "run_gateway_detached", lambda: pytest.fail("must not spawn"))
    with pytest.raises(SystemExit) as exc:
        gw.gateway_command(SimpleNamespace(gateway_command="start"))
    assert exc.value.code == 1
    assert "already in use" in capsys.readouterr().out


def test_start_spawns_and_waits_for_port(monkeypatch, capsys):
    probes = iter([False, False, True])
    monkeypatch.setattr(gw, "get_running_pid", lambda: None)
    monkeypatch.setattr(gw, "_port_listening", lambda port: next(probes))
    monkeypatch.setattr(gw, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(gw, "run_gateway_detached", lambda: 777)
    gw.gateway_command(SimpleNamespace(gateway_command="start"))
    assert "listening on port 8091" in capsys.readouterr().out


def test_start_fails_when_child_exits_early(monkeypatch, capsys):
    monkeypatch.setattr(gw, "get_running_pid", lambda: None)
    monkeypatch.setattr(gw, "_port_listening", lambda port: False)
    monkeypatch.setattr(gw, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(gw, "run_gateway_detached", lambda: 777)
    with pytest.raises(SystemExit) as exc:
        gw.gateway_command(SimpleNamespace(gateway_command="start"))
    assert exc.value.code == 1
    assert "exited before binding" in capsys.readouterr().out


def test_restart_stops_then_starts(monkeypatch):
    calls = []
    monkeypatch.setattr(gw, "stop_gateway", lambda: calls.append("stop") or 4242)
    monkeypatch.setattr(gw, "_start_and_wait", lambda: calls.append("start"))
    gw.gateway_command(SimpleNamespace(gateway_command="restart"))
    assert calls == ["stop", "start"]


def test_detached_launch_command_and_env(monkeypatch, _home):
    (_home / ".jwt_secret").write_text("s3cret\n")
    (_home / "env").write_text("# comment\nexport FOO_FROM_FILE=bar\nALREADY_SET=file\n")
    monkeypatch.delenv("LOGOS_JWT_SECRET", raising=False)
    monkeypatch.delenv("FOO_FROM_FILE", raising=False)
    monkeypatch.setenv("ALREADY_SET", "shell")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return SimpleNamespace(pid=999)

    monkeypatch.setattr(gw.subprocess, "Popen", fake_popen)
    assert gw.run_gateway_detached() == 999
    assert captured["cmd"][1:] == ["-m", "gateway.run"]
    assert captured["cwd"] == str(gw.PROJECT_ROOT)
    assert captured["start_new_session"] is True
    assert captured["env"]["LOGOS_JWT_SECRET"] == "s3cret"
    assert captured["env"]["FOO_FROM_FILE"] == "bar"
    assert captured["env"]["ALREADY_SET"] == "shell"
    assert (_home / "logs" / "gateway.bg.log").exists()


def test_run_passes_replace_and_exits_1_on_failure(monkeypatch):
    import gateway.run as gateway_run
    seen = {}

    async def fake_start_gateway(config=None, replace=False):
        seen["replace"] = replace
        return False

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    with pytest.raises(SystemExit) as exc:
        gw.gateway_command(SimpleNamespace(gateway_command=None, replace=True))
    assert exc.value.code == 1
    assert seen == {"replace": True}
