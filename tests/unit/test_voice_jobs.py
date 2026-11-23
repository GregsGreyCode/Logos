"""Voice notes from the page: long recordings are a job, fetched when ready (gateway/dashboard_api.py)."""
import asyncio
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import dashboard_api as d


@pytest.fixture
def setup(monkeypatch):
    seen = {"calls": 0, "sizes": [], "answer": "Hello, this is a long note.", "delay": 0.0, "status": 200}
    user = {"id": "1", "username": "greg", "role": "user"}

    async def stand_in(request):                       # the voice service
        seen["calls"] += 1
        seen["sizes"].append(len((await request.post())["file"].file.read()))
        await asyncio.sleep(seen["delay"])
        return web.json_response({"text": seen["answer"]}, status=seen["status"])

    async def agent_or_404(request):
        return request.match_info["name"], {"definition": {"voice": True}}, user

    class Host:
        def __init__(self, rt): pass
        def token_for(self, name): return "tok"
        def base_url(self): return f"http://127.0.0.1:{seen['port']}/v1"

    monkeypatch.setattr(d, "_agent_or_404", agent_or_404)
    monkeypatch.setattr(d.agent_chat, "runtime_of", lambda b: object())
    from gateway import voice_host
    monkeypatch.setattr(voice_host, "VoiceHost", Host)
    monkeypatch.setattr(voice_host, "uses_voice", lambda definition: bool(definition.get("voice")))
    monkeypatch.setattr("gateway.auth.middleware.require_csrf", lambda f: f)

    async def run(steps):
        svc = web.Application()
        svc.router.add_post("/v1/audio/transcriptions", stand_in)
        server = TestServer(svc)
        await server.start_server()
        seen["port"] = server.port
        app = web.Application()
        app.router.add_post("/api/v2/agents/{name}/voice", d.handle_voice)
        app.router.add_get("/api/v2/agents/{name}/voice/{job}", d.handle_voice_job)
        try:
            async with TestClient(TestServer(app)) as c:
                return await steps(c, app)
        finally:
            await server.close()
    return run, seen, user


def post(c, path, body=b"x" * 4000, ctype="audio/webm"):
    return c.post(path, data=body, headers={"Content-Type": ctype})


def test_a_recording_is_still_answered_directly_for_the_other_screens(setup):
    run, seen, _ = setup

    async def steps(c, app):
        r = await post(c, "/api/v2/agents/greg/voice")
        return r.status, await r.json()
    assert asyncio.run(run(steps)) == (200, {"text": "Hello, this is a long note."})


def test_a_job_answers_at_once_and_the_words_are_fetched_when_ready(setup):
    run, seen, _ = setup
    seen["delay"] = 0.4

    async def steps(c, app):
        r = await post(c, "/api/v2/agents/greg/voice?job=1")
        job = (await r.json())["job"]
        first = await (await c.get(f"/api/v2/agents/greg/voice/{job}")).json()
        await asyncio.sleep(0.8)
        later = await (await c.get(f"/api/v2/agents/greg/voice/{job}")).json()
        again = await (await c.get(f"/api/v2/agents/greg/voice/{job}")).json()          # still there if the page reloaded
        return r.status, first, later, again
    status, first, later, again = asyncio.run(run(steps))
    assert status == 202 and first == {"state": "working"}
    assert later == again == {"state": "done", "text": "Hello, this is a long note."}


def test_a_failed_job_says_so_in_plain_words(setup):
    run, seen, _ = setup
    seen["status"] = 422

    async def steps(c, app):
        job = (await (await post(c, "/api/v2/agents/greg/voice?job=1")).json())["job"]
        await asyncio.sleep(0.3)
        return await (await c.get(f"/api/v2/agents/greg/voice/{job}")).json()
    assert asyncio.run(run(steps)) == {"state": "failed", "message": "That recording could not be read. Try again."}


def test_a_job_is_the_persons_own_and_unknown_ones_are_404(setup):
    run, seen, user = setup

    async def steps(c, app):
        job = (await (await post(c, "/api/v2/agents/greg/voice?job=1")).json())["job"]
        await asyncio.sleep(0.2)
        user["id"] = "2"                                   # someone else
        other = (await c.get(f"/api/v2/agents/greg/voice/{job}")).status
        user["id"] = "1"
        wrong_agent = (await c.get(f"/api/v2/agents/household/voice/{job}")).status
        nothing = (await c.get("/api/v2/agents/greg/voice/nope")).status
        return other, wrong_agent, nothing
    assert asyncio.run(run(steps)) == (404, 404, 404)


def test_recordings_that_are_not_audio_or_empty_are_refused(setup):
    run, seen, _ = setup

    async def steps(c, app):
        a = await post(c, "/api/v2/agents/greg/voice?job=1", ctype="application/json")
        b = await post(c, "/api/v2/agents/greg/voice?job=1", body=b"")
        return a.status, b.status
    assert asyncio.run(run(steps)) == (415, 400)


def test_old_jobs_are_forgotten(setup):
    run, seen, _ = setup

    async def steps(c, app):
        job = (await (await post(c, "/api/v2/agents/greg/voice?job=1")).json())["job"]
        app["voice_jobs"][job]["at"] -= d.VOICE_JOB_KEEP_S + 1
        await post(c, "/api/v2/agents/greg/voice?job=1")                 # the next one tidies up
        return job in app["voice_jobs"]
    assert asyncio.run(run(steps)) is False


def test_the_limits_allow_half_an_hour():
    assert d.VOICE_MAX_BYTES >= 100 * 1024 * 1024 and d.VOICE_TIMEOUT_S >= 1800
    assert 30 * 60 * 32_000 / 8 < d.VOICE_MAX_BYTES / 10          # 30 min at 32 kbit/s is far under the ceiling
