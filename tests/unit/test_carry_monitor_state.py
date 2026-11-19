"""infra/agent-jobs/carry_monitor_state.py: monitor jobs keep what they saw when re-created."""
import importlib.util
import json
import sys
import types
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "agent-jobs" / "carry_monitor_state.py"
STATE = {"last_output_hash": "abc", "last_changed_at": "2026-11-19T09:00:00+00:00"}


def load(home, monkeypatch, store):
    monkeypatch.setenv("HERMES_HOME", str(home))
    fake = types.ModuleType("cron.jobs")

    def update_job(job_id, updates):
        for job in store["jobs"]:
            if job["id"] == job_id:
                job.update(updates)
        (home / "cron" / "jobs.json").write_text(json.dumps(store))
    fake.update_job = update_job
    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.jobs", fake)
    spec = importlib.util.spec_from_file_location("carry", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_save_then_restore_gives_new_jobs_the_old_state(tmp_path, monkeypatch, capsys):
    (tmp_path / "cron" / "output" / "old1").mkdir(parents=True)
    (tmp_path / "cron" / "output" / "old1" / "monitor_last_output.txt").write_text("latest message 1")
    store = {"jobs": [{"id": "old1", "name": "noticeboard-check", "monitor_state": STATE},
                      {"id": "old2", "name": "weekly-digest", "monitor_state": None}]}
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps(store))
    mod = load(tmp_path, monkeypatch, store)
    mod.save()
    assert "noticeboard-check" in capsys.readouterr().out

    # `up` removes the jobs and creates new ones, with no state
    store["jobs"] = [{"id": "new1", "name": "noticeboard-check", "monitor_state": None},
                     {"id": "new2", "name": "weekly-digest", "monitor_state": None}]
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps(store))
    mod.restore()
    assert store["jobs"][0]["monitor_state"] == STATE and store["jobs"][1]["monitor_state"] is None
    assert (tmp_path / "cron" / "output" / "new1" / "monitor_last_output.txt").read_text() == "latest message 1"
    assert not (tmp_path / "cron" / ".monitor_carry.json").exists()
    mod.restore()                                   # nothing carried: nothing to do
