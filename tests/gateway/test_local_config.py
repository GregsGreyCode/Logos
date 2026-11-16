"""Tests for gateway.local_config (paths, config.yaml, .env helpers)."""

import os
import stat

import pytest

from gateway import local_config as lc


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGOS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    return tmp_path / "home"


def test_home_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "legacy"))
    assert lc.get_logos_home() == tmp_path / "home"
    monkeypatch.delenv("LOGOS_HOME")
    assert lc.get_hermes_home() == tmp_path / "legacy"
    monkeypatch.delenv("HERMES_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert lc.get_logos_home() == tmp_path / "user" / ".logos"


def test_get_home_has_no_side_effects(tmp_path, monkeypatch):
    """Resolving the path must not create or rename anything (no ~/.hermes migration)."""
    user = tmp_path / "user"
    (user / ".hermes").mkdir(parents=True)
    monkeypatch.delenv("LOGOS_HOME")
    monkeypatch.setenv("HOME", str(user))
    assert lc.get_logos_home() == user / ".logos"
    assert (user / ".hermes").is_dir()
    assert not (user / ".logos").exists()


def test_ensure_home_creates_private_dirs(_home):
    lc.ensure_logos_home()
    for sub in ("cron", "sessions", "logs", "memories"):
        assert (_home / sub).is_dir()
    assert stat.S_IMODE(_home.stat().st_mode) == 0o700


def test_load_config_defaults_and_overlay(_home):
    assert lc.load_config()["stt"]["provider"] == "local"
    (_home / "config-base.yaml").write_text(
        "mcp_servers:\n  a: {command: x}\nstt:\n  provider: openai\n")
    (_home / "config.yaml").write_text("stt:\n  local:\n    model: small\nmodel: m1\n")
    cfg = lc.load_config()
    assert cfg["mcp_servers"] == {"a": {"command": "x"}}
    assert cfg["stt"] == {"provider": "openai", "local": {"model": "small"},
                          "openai": {"model": "whisper-1"}}
    assert cfg["model"] == "m1"
    # defaults are not mutated by loads
    assert lc.DEFAULT_CONFIG["stt"]["provider"] == "local"


def test_load_config_survives_bad_yaml(_home, capsys):
    _home.mkdir(parents=True)
    (_home / "config.yaml").write_text("stt: [unclosed\n")
    assert lc.load_config()["stt"]["provider"] == "local"
    assert "Failed to load config.yaml" in capsys.readouterr().out


def test_env_roundtrip_and_permissions(_home, monkeypatch):
    monkeypatch.delenv("S18_TEST_KEY", raising=False)
    monkeypatch.delenv("S18_OTHER", raising=False)
    lc.save_env_value("S18_TEST_KEY", "one")
    lc.save_env_value("S18_OTHER", "x")
    lc.save_env_value("S18_TEST_KEY", "two\n")
    env_path = _home / ".env"
    assert env_path.read_text() == "S18_TEST_KEY=two\nS18_OTHER=x\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert os.environ["S18_TEST_KEY"] == "two"
    monkeypatch.delenv("S18_TEST_KEY")
    assert lc.get_env_value("S18_TEST_KEY") == "two"
    assert lc.get_env_value("S18_MISSING") is None
    monkeypatch.delenv("S18_OTHER", raising=False)


def test_save_env_value_rejects_bad_names():
    with pytest.raises(ValueError):
        lc.save_env_value("BAD NAME", "x")
