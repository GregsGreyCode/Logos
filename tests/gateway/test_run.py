"""gateway/run.py: the gateway process entry point."""
import asyncio

import pytest

import gateway.run as run
from gateway import status


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "LOGOS_HOME", tmp_path)
    monkeypatch.setenv("LOGOS_HOME", str(tmp_path))
    monkeypatch.setattr(run, "_setup_logs", lambda home=None: None)
    monkeypatch.setattr(run, "load_env", lambda home=None: None)
    return tmp_path


def test_refuses_a_second_gateway(home, monkeypatch, capsys):
    monkeypatch.setattr(status, "get_running_pid", lambda: 999999)
    assert asyncio.run(run.start_gateway()) is False
    assert "already running" in capsys.readouterr().out


def test_runs_until_stopped(home, monkeypatch):
    monkeypatch.setattr(status, "get_running_pid", lambda: None)
    seen = {}

    async def fake_http(gw, port):
        seen["runner"], seen["port"] = gw, port
        assert gw.sandbox_backend is not None
        asyncio.get_running_loop().call_later(0.05, gw.stop)

    import gateway.http_api as http_api
    monkeypatch.setattr(http_api, "start_http_api", fake_http)
    monkeypatch.setenv("LOGOS_PORT", "18091")
    assert asyncio.run(run.start_gateway()) is True
    assert seen["port"] == 18091
    assert (home / "auth.db").exists()


def test_fails_when_the_http_server_cannot_start(home, monkeypatch):
    monkeypatch.setattr(status, "get_running_pid", lambda: None)

    async def broken(gw, port):
        raise OSError("address in use")

    import gateway.http_api as http_api
    monkeypatch.setattr(http_api, "start_http_api", broken)
    assert asyncio.run(run.start_gateway()) is False


def test_env_files_load_in_order(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("LOGOS_T_A=dotenv\nLOGOS_T_B=dotenv\n")
    (tmp_path / "env").write_text("LOGOS_T_B=env\nLOGOS_T_C=env\n")
    for k in ("LOGOS_T_A", "LOGOS_T_B", "LOGOS_T_C"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOGOS_T_C", "shell")
    run.load_env(tmp_path)
    import os
    assert (os.environ["LOGOS_T_A"], os.environ["LOGOS_T_B"], os.environ["LOGOS_T_C"]) == ("dotenv", "dotenv", "shell")
