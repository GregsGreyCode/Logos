"""model_loaded: whether an agent's model is loaded in LM Studio (asleep vs awake on the floating agents)."""
import asyncio
import json
from types import SimpleNamespace

from gateway import dashboard_api


def _request(models, up=True):
    def http(url, timeout=None, **kw):
        if not up:
            raise OSError("LM Studio is off")
        return json.dumps({"data": models}).encode()
    app = {}
    return SimpleNamespace(app=app), SimpleNamespace(runner=SimpleNamespace(http=http))


def _run(monkeypatch, models, up=True, model_id="qwen/qwen3.6-35b-a3b"):
    request, runtime = _request(models, up)
    monkeypatch.setattr(dashboard_api.agent_chat, "runtime_of", lambda backend: runtime)
    definition = {"model": {"provider": "lmstudio", "default": model_id, "base_url": "http://lmstudio:1234/v1"}}
    return asyncio.run(dashboard_api.model_loaded(request, definition))


def test_loaded_model_is_awake(monkeypatch):
    assert _run(monkeypatch, [{"id": "qwen/qwen3.6-35b-a3b", "state": "loaded", "loaded_context_length": 32768}]) is True


def test_server_on_but_model_not_loaded_is_asleep(monkeypatch):
    assert _run(monkeypatch, [{"id": "qwen/qwen3.6-35b-a3b", "state": "not-loaded"},
                              {"id": "other", "state": "loaded"}]) is False


def test_server_off_cannot_tell(monkeypatch):
    assert _run(monkeypatch, [], up=False) is None
