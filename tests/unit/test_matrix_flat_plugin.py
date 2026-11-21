"""logos-matrix-flat marks hermes' Matrix adapter as able to post scheduled messages in the chat."""
import importlib.util
import sys
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "infra" / "agent-plugins" / "logos-matrix-flat" / "__init__.py"


def load():
    spec = importlib.util.spec_from_file_location("logos_matrix_flat_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_marks_a_loaded_matrix_adapter(monkeypatch):
    class MatrixAdapter:
        supports_inchannel_continuable = False
    fake = types.ModuleType("hermes_plugins.platforms__matrix.adapter")
    fake.MatrixAdapter = MatrixAdapter
    monkeypatch.setitem(sys.modules, "hermes_plugins.platforms__matrix.adapter", fake)
    load().register(None)
    assert MatrixAdapter.supports_inchannel_continuable is True


def test_waits_for_the_adapter_when_it_loads_later(monkeypatch):
    mod = load()
    started = []
    monkeypatch.setattr(mod.threading, "Thread", lambda **k: types.SimpleNamespace(start=lambda: started.append(k["name"])))
    for name in [n for n in sys.modules if n.endswith(mod.ADAPTER_MODULE_SUFFIX)]:
        monkeypatch.delitem(sys.modules, name)
    mod.register(None)
    assert started == ["logos-matrix-flat"]
