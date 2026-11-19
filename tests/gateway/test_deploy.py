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
    assert p == {"package": False, "image": False, "units": False, "dashboard": True, "agents": []}


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
    assert p == {"package": False, "image": False, "units": False, "dashboard": False, "agents": []}
    assert deploy.describe(p) == ["Update the model gateway, noticeboard, voice, calendar, web reader and house service where they changed"]


def test_unit_changes_update_installed_units(deploy):
    p = deploy.plan(["infra/systemd/logos-backup-export.timer"], RUNNING)
    assert p["units"] and not p["dashboard"]
    p = deploy.plan(["infra/systemd/logos-gateway.service"], RUNNING)
    assert p["units"] and p["dashboard"]                 # the new unit takes effect on restart
    assert deploy.describe(p)[:2] == ["Update the installed systemd units", "Restart the dashboard (a few seconds)"]


def test_install_units_rewrites_only_installed_units_that_differ(deploy, tmp_path):
    repo, units = tmp_path / "repo", tmp_path / "units"
    (repo / "infra" / "systemd").mkdir(parents=True)
    units.mkdir()
    (repo / "infra/systemd/a.service").write_text("ExecStart=@REPO@/run\n")
    (repo / "infra/systemd/b.timer").write_text("OnCalendar=daily\n")
    (repo / "infra/systemd/new.service").write_text("x\n")       # not installed here: left alone
    (repo / "infra/systemd/README.md").write_text("not a unit\n")
    (units / "a.service").write_text("ExecStart=/old/run\n")
    (units / "b.timer").write_text("OnCalendar=daily\n")
    assert deploy.install_units(units, repo) == ["a.service"]
    assert (units / "a.service").read_text() == f"ExecStart={repo}/run\n"
    assert not (units / "new.service").exists()
    assert deploy.install_units(units, repo) == []


def test_the_gateway_unit_has_no_ordering_cycle():
    unit = (SCRIPT.parent.parent / "infra/systemd/logos-gateway.service").read_text()
    after = [l for l in unit.splitlines() if l.startswith("After=")]
    assert not any("openshell-gateway" in l for l in after)


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


def _repo(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q", "-b", "v2")
    run("config", "user.email", "t@example.org"); run("config", "user.name", "t")
    shas = []
    for i in range(3):
        (repo / "f.txt").write_text(str(i))
        run("add", "f.txt"); run("commit", "-q", "-m", f"c{i}")
        shas.append(run("rev-parse", "HEAD"))
    run("update-ref", "refs/remotes/origin/v2", shas[2])
    run("checkout", "-q", "-b", "other", shas[0])
    (repo / "f.txt").write_text("x"); run("commit", "-qam", "off v2")
    stray = run("rev-parse", "HEAD")
    run("checkout", "-q", "v2")
    return repo, shas, stray


def test_resolve_target_accepts_only_commits_of_v2(deploy, tmp_path, monkeypatch):
    repo, shas, stray = _repo(tmp_path)
    monkeypatch.setattr(deploy, "REPO", repo)
    assert deploy.resolve_target(shas[1][:7]) == shas[1]
    with pytest.raises(deploy.Refused, match="not part of origin/v2"):
        deploy.resolve_target(stray)
    with pytest.raises(deploy.Refused, match="not a commit"):
        deploy.resolve_target("nonsense")


def test_previous_is_where_the_last_deploy_started(deploy, tmp_path, monkeypatch):
    repo, shas, _ = _repo(tmp_path)
    monkeypatch.setattr(deploy, "REPO", repo)
    log = tmp_path / "deploy.log"
    monkeypatch.setattr(deploy, "LOG", log)
    with pytest.raises(deploy.Refused, match="No deploy"):
        deploy.resolve_target("previous")
    log.write_text(f"2026-11-18T10:00:00Z {shas[0][:7]} -> {shas[1][:7]} x\n"
                   f"2026-11-19T10:00:00Z {shas[1][:7]} -> {shas[2][:7]} y\n")
    assert deploy.resolve_target("previous") == shas[1]
