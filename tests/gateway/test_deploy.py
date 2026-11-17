"""scripts/logos-deploy planning and the Updates screen's API."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

from gateway import updates

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "logos-deploy"


@pytest.fixture(scope="module")
def deploy():
    loader = importlib.machinery.SourceFileLoader("logos_deploy", str(SCRIPT))
    spec = importlib.util.spec_from_loader("logos_deploy", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


RUNNING = ["bere", "greg", "household"]


def test_dashboard_only_change_restarts_only_the_dashboard(deploy):
    p = deploy.plan(["gateway/ui/chat.js", "gateway/agent_chat.py"], RUNNING)
    assert p == {"package": False, "image": False, "dashboard": True, "agents": []}


def test_one_agent_definition_recreates_only_that_agent(deploy):
    assert deploy.plan(["agents/greg.yaml"], RUNNING)["agents"] == ["greg"]


def test_agents_that_are_not_running_here_are_never_started(deploy):
    p = deploy.plan(["agents/dev.yaml", "agents/bench.yaml", "souls/general/soul.md"], RUNNING)
    assert p["agents"] == RUNNING                 # souls/ hits every RUNNING agent, not dev or bench


def test_image_and_runtime_changes_recreate_every_running_agent(deploy):
    p = deploy.plan(["infra/agent-image/Dockerfile"], RUNNING)
    assert p["image"] and p["agents"] == RUNNING and not p["dashboard"]
    assert deploy.plan(["gateway/agent_runtime.py"], RUNNING)["agents"] == RUNNING


def test_package_change_reinstalls_and_restarts_the_dashboard(deploy):
    p = deploy.plan(["pyproject.toml"], RUNNING)
    assert p["package"] and p["dashboard"]


def test_docs_change_restarts_nothing(deploy):
    p = deploy.plan(["docs/model-gateway.md", "README.md"], RUNNING)
    assert p == {"package": False, "image": False, "dashboard": False, "agents": []}
    assert deploy.describe(p) == ["Update the model gateway, noticeboard and voice where they changed"]


def test_the_button_runs_outside_the_dashboard_service():
    argv = updates.start_argv()
    assert argv[:2] == ["systemd-run", "--user"] and f"--unit={updates.UNIT}" in argv
    assert argv[-2:] == [str(updates.SCRIPT), "--yes"]
    assert any(a.startswith("--property=StandardOutput=truncate:") for a in argv)


def test_status_and_output_are_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "STATUS", tmp_path / "s.json")
    monkeypatch.setattr(updates, "OUTPUT", tmp_path / "o.txt")
    assert updates.read_status() is None and updates.read_output() == []
    (tmp_path / "s.json").write_text(json.dumps({"state": "done"}))
    (tmp_path / "o.txt").write_text("\n".join(str(i) for i in range(500)))
    assert updates.read_status() == {"state": "done"}
    out = updates.read_output()
    assert len(out) == updates.OUTPUT_LINES and out[-1] == "499"


def test_start_refuses_while_an_update_runs(monkeypatch):
    monkeypatch.setattr(updates, "unit_active", lambda: True)
    monkeypatch.setattr(updates, "_run", lambda *a, **k: pytest.fail("must not start a second update"))
    assert "already running" in updates.start()
