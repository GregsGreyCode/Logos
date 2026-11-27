"""Who is speaking at the house speaker (Voice VO.2): the decision, what the agent is told, who may be
answered, and Settings › Your voice."""
import asyncio
import math
import random
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.auth.db as auth_db
from gateway import voice_converse as vc
from gateway import voice_id as vi
from gateway import voiceprint as vp

DIM = 192


def unit(rng):
    return vp.normalise([rng.gauss(0, 1) for _ in range(DIM)])


def near(base, rng, noise=0.6):
    return vp.normalise([b + noise * rng.gauss(0, 1) / math.sqrt(DIM) for b in base])


RNG = random.Random(5)
GREG, BERE, GUEST = unit(RNG), unit(RNG), unit(RNG)
COHORT = [unit(RNG) for _ in range(40)]


def print_of(base):
    return vp.enrol([near(base, RNG) for _ in range(5)], COHORT)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "_DB_PATH", None)
    auth_db.init_db(tmp_path)
    auth_db.create_user("g@example.org", "greg", "x", display_name="Greg")
    auth_db.create_user("b@example.org", "bere", "x", display_name="Béré")
    vi._cohort.clear()
    yield
    vi._cohort.clear()


@pytest.fixture
def heard(monkeypatch):
    """The speech-to-text container: what it heard, and recordings sent from Settings."""
    box = {"rows": [], "recording": None, "down": False, "asked": []}

    async def fake(method, path, *, data=None, timeout=vi.TIMEOUT_S):
        box["asked"].append((method, path.split("?")[0]))
        if box["down"]:
            raise ConnectionError("down")
        if path.startswith("/heard?key="):
            return {"heard": [r for r in box["rows"] if r.get("key") == path.split("=", 1)[1]]}
        if path == "/heard":
            return {"heard": box["rows"]}
        if path == "/cohort":
            return {"cohort": COHORT}
        if path == "/embed":
            if box["recording"] == "unreadable":
                raise ValueError("unreadable")
            return box["recording"]
        raise AssertionError(path)
    monkeypatch.setattr(vi, "_stt", fake)
    return box


def row(text, e, speech=2.5, at=None, id_=1):
    import time
    return {"id": id_, "key": vp.heard_key(text), "at": at or time.time(), "embedding": e, "speech": speech,
            "snr": 20, "clipped": False}


# ── who is it ────────────────────────────────────────────────────────────────────────────────

def test_nobody_enrolled_means_nothing_to_tell(db, heard):
    assert asyncio.run(vi.identify({}, "hello")) is None
    assert heard["asked"] == []                                           # not even asked


def test_the_voice_of_these_words_is_compared_with_every_print(db, heard):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    auth_db.set_voice_print("bere", print_of(BERE).to_json())
    heard["rows"] = [row("What's on today?", near(GREG, RNG))]
    who = asyncio.run(vi.identify({}, "what's on today"))
    assert who.known and who.username == "greg" and who.name == "Greg" and who.enrolled == ("bere", "greg")
    heard["rows"] = [row("Add milk", near(BERE, RNG))]
    assert asyncio.run(vi.identify({}, "Add milk.")).name == "Béré"
    heard["rows"] = [row("Hello", GUEST)]
    who = asyncio.run(vi.identify({}, "Hello"))
    assert who.state == vp.OTHER and who.username is None and who.name == ""


def test_without_a_match_by_words_only_a_single_recent_command_counts(db, heard):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    heard["rows"] = [row("Lights of", near(GREG, RNG))]                  # Home Assistant changed the words
    assert asyncio.run(vi.identify({}, "Lights off")).known
    heard["rows"] = [row("one", near(GREG, RNG), id_=2), row("two", GUEST, id_=1)]
    assert asyncio.run(vi.identify({}, "three")).state == vp.UNSURE      # two to choose from: not sure
    heard["rows"] = [row("old", near(GREG, RNG), at=1.0)]
    assert asyncio.run(vi.identify({}, "new")).state == vp.UNSURE        # too long ago


def test_when_logos_did_not_hear_it_it_is_not_sure(db, heard):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    heard["down"] = True                                                  # Home Assistant's own speech-to-text
    assert asyncio.run(vi.identify({}, "hello")).state == vp.UNSURE
    heard["down"] = False
    heard["rows"] = [row("hi", near(GREG, RNG), speech=0.4)]
    assert asyncio.run(vi.identify({}, "hi")).state == vp.UNSURE         # too short to tell


def test_a_clear_match_is_learned(db, heard):
    p = print_of(GREG)
    auth_db.set_voice_print("greg", p.to_json())
    heard["rows"] = [row("What's on today?", near(GREG, RNG, 0.3), speech=3.0)]
    asyncio.run(vi.identify({}, "What's on today?"))
    assert vp.Profile.from_json(auth_db.get_voice_prints()["greg"]).learned == 1


# ── what the agent is told ───────────────────────────────────────────────────────────────────

def test_the_instructions_say_who_is_speaking():
    greg = vi.Who(vp.ME, "greg", "Greg", 0.8)
    told = vi.instructions(vc.INSTRUCTIONS, greg)
    assert "has not been identified" not in told and "The person speaking is Greg" in told
    assert "their day" in told and told.startswith(vc.INSTRUCTIONS.split("Whoever")[0])
    assert "guest" in vi.instructions(vc.INSTRUCTIONS, vi.Who(vp.OTHER))
    assert "could not be told for sure" in vi.instructions(vc.INSTRUCTIONS, vi.Who(vp.UNSURE))
    assert vi.instructions(vc.INSTRUCTIONS, None) == vc.INSTRUCTIONS      # nobody enrolled: as before
    assert vi.instructions("Speak briefly.", greg).endswith("from Greg.")   # without the sentence: added at the end
    assert vi.tagged("Add milk", greg) == "[Speaker: Greg] Add milk"
    assert vi.tagged("Add milk", vi.Who(vp.UNSURE)) == "Add milk" and vi.tagged("Add milk", None) == "Add milk"


def test_a_personal_agent_answers_only_its_people(db):
    finn = {"owners": ["greg"]}
    greg = vi.Who(vp.ME, "greg", "Greg", 0.8, enrolled=("bere", "greg"))
    bere = vi.Who(vp.ME, "bere", "Béré", 0.8, enrolled=("bere", "greg"))
    assert vi.refusal(finn, greg) is None
    assert "only answer Greg" in vi.refusal(finn, bere) and "Home can help" in vi.refusal(finn, bere)
    assert "say it again" in vi.refusal(finn, vi.Who(vp.UNSURE, enrolled=("greg",)))
    assert "only answer Greg" in vi.refusal(finn, vi.Who(vp.OTHER, enrolled=("greg",)))
    assert "don't know their voice yet" in vi.refusal(finn, None)
    assert "don't know their voice yet" in vi.refusal(finn, vi.Who(vp.ME, "bere", "Béré", enrolled=("bere",)))
    # Home answers anyone, and so does an agent for everyone.
    home = {"owners": ["greg", "bere"], "role": "household"}
    assert vi.refusal(home, vi.Who(vp.OTHER, enrolled=("greg",))) is None and vi.refusal(home, None) is None
    assert vi.refusal({}, vi.Who(vp.OTHER)) is None


# ── through voice/converse ───────────────────────────────────────────────────────────────────

class Client:
    def __init__(self):
        self.started = []

    async def start_run(self, text, **kw):
        self.started.append((text, kw))
        return "run-1"

    async def stream_events(self, run_id):
        yield SimpleNamespace(type="run_completed", text="Done.", terminal=True)


class Links:
    def __init__(self, definition):
        self.c, self.definition = Client(), definition
        self.runtime = SimpleNamespace(load=lambda name: definition, runner=SimpleNamespace(http=None))

    async def health(self, name):
        pass

    async def client(self, name):
        return self.c


def converse(links, text, monkeypatch, tmp_path, agent="household"):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LOGOS_VOICE_AGENT", agent)

    async def run():
        app = web.Application()
        app["agent_links"] = links
        vc.register(app)
        async with TestClient(TestServer(app)) as c:
            r = await c.post(vc.PATH, json={"text": text, "device_id": "pe1"}, headers={"Authorization": f"Bearer {vc.token()}"})
            return await r.json()
    return asyncio.run(run())


def test_home_is_told_who_asked(db, heard, monkeypatch, tmp_path):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    heard["rows"] = [row("What's on today?", near(GREG, RNG))]
    links = Links({"name": "household", "role": "household", "owners": ["greg", "bere"], "model": {}})
    out = converse(links, "What's on today?", monkeypatch, tmp_path)
    assert out["speech"] == "Done."
    text, kw = links.c.started[0]
    assert text == "[Speaker: Greg] What's on today?" and "The person speaking is Greg" in kw["instructions"]


def test_a_personal_agent_declines_someone_else_without_running(db, heard, monkeypatch, tmp_path):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    auth_db.set_voice_print("bere", print_of(BERE).to_json())
    heard["rows"] = [row("What's on today?", near(BERE, RNG))]
    links = Links({"name": "finn", "owners": ["greg"], "model": {}})
    out = converse(links, "What's on today?", monkeypatch, tmp_path, agent="finn")
    assert "only answer Greg" in out["speech"] and out["agent"] == "finn"
    assert links.c.started == []                                          # the agent was never run
    heard["rows"] = [row("What's on today?", near(GREG, RNG))]
    out = converse(links, "What's on today?", monkeypatch, tmp_path, agent="finn")
    assert out["speech"] == "Done." and links.c.started[0][0].startswith("[Speaker: Greg]")


# ── Settings › Your voice ────────────────────────────────────────────────────────────────────

GOOD = {"speech": 10.2, "snr": 24.0, "clipped": False}


def api(steps, user="greg", monkeypatch=None):
    from gateway import dashboard_api as d
    monkeypatch.setattr(d, "_user", lambda r: {"id": user, "username": user, "display_name": user, "role": "user"})

    async def run():
        app = web.Application()
        app.router.add_get("/api/v2/voiceprint", vi.handle_get)
        app.router.add_post("/api/v2/voiceprint", vi.handle_post)
        app.router.add_delete("/api/v2/voiceprint", vi.handle_delete)
        app.router.add_get("/api/v2/voiceprint/heard", vi.handle_heard)
        app.router.add_post("/api/v2/voiceprint/heard/{id}", vi.handle_claim)
        async with TestClient(TestServer(app)) as c:
            return await steps(c)
    return asyncio.run(run())


def post(c, step, extra=""):
    return c.post(f"/api/v2/voiceprint?step={step}{extra}", data=b"OggS....", headers={"Content-Type": "audio/ogg"})


def test_enrol_test_and_delete(db, heard, monkeypatch):
    async def steps(c):
        out = {"before": await (await c.get("/api/v2/voiceprint")).json()}
        heard["recording"] = {"quality": {"speech": 2.0, "snr": 20, "clipped": False}, "embedding": GREG, "pieces": []}
        out["short"] = await (await post(c, "enrol")).json()
        heard["recording"] = {"quality": GOOD, "embedding": near(GREG, RNG), "pieces": [near(GREG, RNG) for _ in range(4)]}
        out["enrol"] = await (await post(c, "enrol")).json()
        out["after"] = await (await c.get("/api/v2/voiceprint")).json()
        heard["recording"] = {"quality": {"speech": 4, "snr": 20}, "embedding": near(GREG, RNG), "pieces": []}
        out["me"] = await (await post(c, "me")).json()
        heard["recording"] = {"quality": {"speech": 4, "snr": 20}, "embedding": GUEST, "pieces": []}
        out["other"] = await (await post(c, "other")).json()
        heard["recording"] = "unreadable"
        out["bad"] = (await post(c, "me")).status
        out["deleted"] = (await c.delete("/api/v2/voiceprint")).status
        out["gone"] = await (await c.get("/api/v2/voiceprint")).json()
        return out
    out = api(steps, monkeypatch=monkeypatch)
    assert out["before"]["enrolled"] is False and "count slowly" in out["before"]["prompt"]
    assert out["short"]["ok"] is False and "need 6" in out["short"]["problem"] and not auth_db.get_voice_prints() == {"x": 1}
    assert out["enrol"]["ok"] and out["enrol"]["summary"]["samples"] == 5 and out["enrol"]["summary"]["others"] == 40
    assert out["after"]["enrolled"] and "samples" not in str(out["after"]["summary"].get("own"))
    assert out["me"]["recognised"] and out["me"]["as"] == "Greg"
    assert out["other"]["recognised"] is False and out["other"]["summary"]["tested_others"] == 1
    assert out["bad"] == 422 and out["deleted"] == 200 and out["gone"]["enrolled"] is False
    assert auth_db.get_voice_prints() == {}


def test_each_person_has_only_their_own(db, heard, monkeypatch):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())

    async def steps(c):
        mine = await (await c.get("/api/v2/voiceprint")).json()
        test = (await post(c, "me")).status
        await c.delete("/api/v2/voiceprint")
        return mine, test
    mine, test = api(steps, user="bere", monkeypatch=monkeypatch)
    assert mine["enrolled"] is False and test == 409
    assert "greg" in auth_db.get_voice_prints()                           # Béré's delete deletes only Béré's


def test_that_was_me_learns_the_speakers_microphone(db, heard, monkeypatch):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    auth_db.set_voice_print("bere", print_of(BERE).to_json())
    heard["rows"] = [row("a", near(GREG, RNG, 1.4), id_=3), row("b", near(BERE, RNG), id_=4)]

    async def steps(c):
        listed = await (await c.get("/api/v2/voiceprint/heard")).json()
        mine = await c.post("/api/v2/voiceprint/heard/3")
        hers = await c.post("/api/v2/voiceprint/heard/4")
        gone = await c.post("/api/v2/voiceprint/heard/9")
        return listed, mine.status, await mine.json(), hers.status, gone.status
    listed, ok, body, hers, gone = api(steps, monkeypatch=monkeypatch)
    assert [r["id"] for r in listed["heard"]] == [3, 4] and set(listed["heard"][0]) == {"id", "ago", "speech", "score", "recognised"}
    assert ok == 200 and body["summary"]["learned"] == 1
    assert hers == 409                       # it sounds like Béré, who has a print: not added to Greg's
    assert gone == 404
