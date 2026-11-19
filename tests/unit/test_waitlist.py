"""Story D2: the logos-waitlist plugin, the rerun-after-outage job and the household's model watch."""
import asyncio
import datetime
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(path, name, monkeypatch=None, home=None):
    if monkeypatch is not None and home is not None:
        monkeypatch.setenv("HERMES_HOME", str(home))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wl(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    return load(ROOT / "infra/agent-plugins/logos-waitlist/__init__.py", "logos_waitlist", monkeypatch, tmp_path)


OFF = "My model is offline right now (LM Studio isn't running) ... model_server_off"


def test_model_off_is_final_for_the_turn_and_other_errors_are_left_alone(wl):
    got = wl._classify(status_code=503, error_code="model_server_off", error_message="x")
    assert got["retryable"] is False and got["reason"] == "overloaded"
    assert wl._classify(status_code=503, error_body={"error": {"code": "model_server_off"}})["retryable"] is False
    assert wl._classify(status_code=503, error_message="Service Unavailable") is None
    assert wl._classify(status_code=500, error_message="model_server_off") is None


def test_only_chat_conversations_wait_and_only_once(wl):
    wl._on_error(session_id="s1", platform="matrix", status_code=503, error={"message": OFF})
    wl._on_error(session_id="s1", platform="matrix", status_code=503, error={"message": OFF})
    wl._on_error(session_id="s2", platform="cron", status_code=503, error={"message": OFF})
    wl._on_error(session_id="s3", platform="api_server", status_code=503, error={"message": OFF})
    wl._on_error(session_id="s4", platform="matrix", status_code=500, error={"message": "boom"})
    assert list(wl.load()) == ["s1"]


class Gateway:
    def __init__(self, origins):
        self.origins, self._running_agents = origins, {}

    def _gateway_session_origin_for_id(self, sid):
        return self.origins.get(sid)


def run(coro):
    return asyncio.run(coro)


def test_back_answers_oldest_first_one_at_a_time(wl):
    now = 1_000_000.0
    wl.save({"new": {"platform": "matrix", "since": now - 60},
             "old": {"platform": "matrix", "since": now - 600},
             "stale": {"platform": "matrix", "since": now - 13 * 3600},
             "gone": {"platform": "matrix", "since": now - 30}})
    gw = Gateway({"new": "room-new", "old": "room-old", "stale": "room-stale"})
    sent, notes, busy_checks = [], [], []

    async def retry(g, origin):
        sent.append(origin)
        g._running_agents = {"k": 1}          # the re-sent turn runs for a while

    async def note(g, origin):
        notes.append(origin)

    async def busy(g):
        busy_checks.append(len(sent))
        was, g._running_agents = bool(g._running_agents), {}
        return was

    async def no_sleep(_):
        return None

    done = run(wl.answer_waiting(gw, now, retry, note, busy, sleep=no_sleep))
    assert [sid for sid, _ in done] == ["stale", "old", "new", "gone"]
    assert dict(done) == {"stale": "too old", "old": "retried", "new": "retried", "gone": "gone"}
    assert sent == ["room-old", "room-new"] and notes == ["room-stale"]
    assert busy_checks[:2] == [1, 1]           # waited for the first answer before the second
    assert wl.load() == {}


def test_a_conversation_that_fails_again_during_its_answer_waits_again(wl):
    now = 2_000_000.0
    wl.save({"s1": {"platform": "matrix", "since": now - 60}})

    async def retry(g, origin):                # LM Studio closes again mid-answer
        wl._on_error(session_id="s1", platform="matrix", status_code=503, error={"message": OFF})

    async def nothing(*a):
        return False

    run(wl.answer_waiting(Gateway({"s1": "room"}), now, retry, nothing, nothing, sleep=nothing))
    assert list(wl.load()) == ["s1"]


def test_model_back_reads_the_guard(wl, monkeypatch):
    class R:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return self.body

    monkeypatch.setattr(wl.urllib.request, "urlopen", lambda url, timeout: R(b'{"model_server_off": false}'))
    assert wl.model_back("http://guard/guard/health") is True
    monkeypatch.setattr(wl.urllib.request, "urlopen", lambda url, timeout: R(b'{"model_server_off": true}'))
    assert wl.model_back("http://guard/guard/health") is False

    def down(url, timeout):
        raise OSError("no route")
    monkeypatch.setattr(wl.urllib.request, "urlopen", down)
    assert wl.model_back("http://guard/guard/health") is False


# ── rerun_after_outage.py ─────────────────────────────────────────────────────

def test_jobs_that_failed_for_the_outage_run_again_in_time():
    ra = load(ROOT / "infra/agent-jobs/rerun_after_outage.py", "rerun_after_outage")
    tz = datetime.timezone.utc
    at = "2026-11-22T03:00:05+00:00"
    failed = {"last_status": "error", "last_failure": {"at": at, "detail": "RuntimeError: ... model_server_off"}}
    jobs = [{"id": "c1", "name": "consolidate", **failed}, {"id": "d1", "name": "weekly-digest", **failed},
            {"id": "b1", "name": "morning-briefing", **failed},
            {"id": "c2", "name": "consolidate", "last_status": "error",
             "last_failure": {"at": at, "detail": "Context size has been exceeded"}},
            {"id": "c3", "name": "consolidate", "last_status": "ok", "last_failure": failed["last_failure"]}]
    morning = datetime.datetime(2026, 11, 22, 7, 30, tzinfo=tz)
    assert [i for i, _ in ra.due(jobs, morning, set())] == ["c1", "d1"]
    assert [i for i, _ in ra.due(jobs, morning.replace(hour=10), set())] == ["d1"]     # too late to tidy
    assert ra.due(jobs, morning + datetime.timedelta(days=1), set()) == []              # another day
    done = {k for _, k in ra.due(jobs, morning, set())}
    assert ra.due(jobs, morning, done) == []                                            # only once


# ── model_watch.py (household) ────────────────────────────────────────────────

def test_the_group_chat_hears_about_long_outages_only():
    mw = load(ROOT / "infra/agent-jobs/model_watch.py", "model_watch")
    state, said = {}, []
    for t, off in [(0, True), (60, True), (100, False), (200, True), (300, True), (330, True), (400, True),
                   (500, False), (560, False)]:
        message, state = mw.step(off, t, state)
        if message:
            said.append((t, message))
    assert [t for t, _ in said] == [330, 500]          # a 100-second blip said nothing
    assert said[0][1] == mw.OFFLINE and said[1][1] == mw.BACK
