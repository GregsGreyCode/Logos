"""The logos-voice plugin: the agent turns voice-note transcripts on and off (docs/voice.md)."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lv(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("LOGOS_VOICE_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("logos_voice", ROOT / "infra/agent-plugins/logos-voice/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_choice_is_kept_and_reported(lv, tmp_path):
    assert lv.chosen() is None
    assert "unknown" in json.loads(lv.handle({}))["transcripts"]
    assert json.loads(lv.handle({"on": True}))["transcripts"] == "on"
    assert lv.chosen() is True and json.loads((tmp_path / "voice-echo.json").read_text())["on"] is True
    assert json.loads(lv.handle({"on": False}))["transcripts"] == "off"
    assert lv.chosen() is False and json.loads(lv.handle({}))["transcripts"] == "off"
    (tmp_path / "voice-echo.json").write_text("not json")
    assert lv.chosen() is None                                     # a broken file: the start-up setting


def test_offered_only_to_agents_with_voice_notes(lv, tmp_path, monkeypatch):
    assert lv.has_voice() is False
    (tmp_path / ".env").write_text("LOGOS_VOICE_TOKEN=\n")
    assert lv.has_voice() is False                                 # empty token: voice is off
    (tmp_path / ".env").write_text("OTHER=1\nLOGOS_VOICE_TOKEN=abc\n")
    assert lv.has_voice() is True
    (tmp_path / ".env").unlink()
    monkeypatch.setenv("LOGOS_VOICE_TOKEN", "abc")
    assert lv.has_voice() is True


def test_hermes_asks_the_choice_on_every_voice_note(lv, monkeypatch):
    class GatewayVoiceMixin:
        def __init__(self, configured):
            self.configured = configured

        def _should_echo_stt_transcripts(self):
            return self.configured

    fake = types.ModuleType("gateway.run_voice")
    fake.GatewayVoiceMixin = GatewayVoiceMixin
    monkeypatch.setitem(sys.modules, "gateway.run_voice", fake)
    assert lv.patch_gateway() is True and lv.patch_gateway() is True   # patching twice is harmless
    runner = GatewayVoiceMixin(configured=False)
    assert runner._should_echo_stt_transcripts() is False              # no choice yet: start-up setting
    lv.choose(True)
    assert runner._should_echo_stt_transcripts() is True               # no restart needed
    lv.choose(False)
    assert GatewayVoiceMixin(configured=True)._should_echo_stt_transcripts() is False


def test_register_adds_the_tool(lv, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.run_voice", types.ModuleType("gateway.run_voice"))
    seen = []

    class Ctx:
        def register_tool(self, **kw):
            seen.append(kw)

    lv.register(Ctx())
    assert seen[0]["name"] == "voice_transcripts" and seen[0]["schema"]["name"] == "voice_transcripts"
    assert seen[0]["check_fn"] is lv.has_voice and seen[0]["handler"] is lv.handle
