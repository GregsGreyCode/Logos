"""Who is speaking at the house speaker (Voice VO.2): the decision, what the agent is told, who may be
answered, and Settings › Your voice."""
import asyncio
import json
import math
import random
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.auth.db as auth_db
from gateway import tts
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
SPEAKER_COHORT = [unit(RNG) for _ in range(40)]


def print_of(base, active=True):
    p = vp.enrol([near(base, RNG) for _ in range(5)], COHORT, speaker_cohort=SPEAKER_COHORT)
    if active:
        p.state = vp.ACTIVE
    return p


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "_DB_PATH", None)
    auth_db.init_db(tmp_path)
    auth_db.create_user("g@example.org", "greg", "x", display_name="Greg")
    auth_db.create_user("b@example.org", "bere", "x", display_name="Béré")
    monkeypatch.setenv("LOGOS_HOME", str(tmp_path / "logos"))
    vi._cohort.clear()
    vi._speaker_cohort.clear()
    vi._held.clear()
    yield
    vi._cohort.clear()
    vi._speaker_cohort.clear()
    vi._held.clear()


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
        if path == "/cohort?channel=speaker":
            return {"cohort": SPEAKER_COHORT}
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
    assert who.active == ("bere", "greg") and who.heard["embedding"]
    heard["rows"] = [row("Add milk", near(BERE, RNG))]
    assert asyncio.run(vi.identify({}, "Add milk.")).name == "Béré"
    heard["rows"] = [row("Hello", GUEST)]
    who = asyncio.run(vi.identify({}, "Hello"))
    assert who.state == vp.OTHER and who.username is None and who.name == ""


def test_without_a_match_by_words_only_a_single_recent_command_counts(db, heard):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    heard["rows"] = [row("Lights of", near(GREG, RNG))]                  # Home Assistant changed the words
    assert asyncio.run(vi.identify({}, "Lights off")).known
    heard["rows"] = [row("one", near(GREG, RNG), id_=2), row("two", near(BERE, RNG), id_=1)]
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


def test_a_print_still_learning_names_nobody(db, heard):
    auth_db.set_voice_print("greg", print_of(GREG, active=False).to_json())
    heard["rows"] = [row("What's on today?", near(GREG, RNG))]
    who = asyncio.run(vi.identify({}, "What's on today?"))
    assert who.state == vp.UNSURE and not who.known and who.active == () and who.enrolled == ("greg",)


def test_a_short_command_is_not_sure_rather_than_someone_else(db, heard):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    heard["rows"] = [row("Lights", GUEST, speech=1.1)]
    who = asyncio.run(vi.identify({}, "Lights"))
    assert who.state == vp.UNSURE and who.short
    heard["rows"] = [row("Lights", GUEST, speech=2.5)]
    assert asyncio.run(vi.identify({}, "Lights")).state == vp.OTHER


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
    both = dict(enrolled=("bere", "greg"), active=("bere", "greg"))
    greg = vi.Who(vp.ME, "greg", "Greg", 0.8, **both)
    bere = vi.Who(vp.ME, "bere", "Béré", 0.8, **both)
    assert vi.refusal(finn, greg) is None
    assert vi.refusal(finn, bere) == "Sorry Béré, I only answer Greg. Home can help you."
    unsure = vi.refusal(finn, vi.Who(vp.UNSURE, **both))
    assert "couldn't tell" in unsure and "say it again" in unsure and "only answer" not in unsure
    short = vi.refusal(finn, vi.Who(vp.UNSURE, short=True, **both))
    assert "too short" in short and "only answer" not in short
    assert vi.refusal(finn, vi.Who(vp.OTHER, **both)) == "Sorry, I only answer Greg. Home can help you."
    # While Greg's print is still learning (or he has none), Finn answers, and says nothing private aloud.
    learning = vi.Who(vp.UNSURE, enrolled=("greg",), active=())
    assert vi.refusal(finn, learning) is None and vi.refusal(finn, None) is None
    assert vi.refusal(finn, vi.Who(vp.OTHER, enrolled=("greg",), active=())) is None
    assert "still learning Greg's voice" in vi.learning_line(finn, learning) and "no private details" in vi.learning_line(finn, learning)
    assert vi.learning_line(finn, greg) == "" and vi.learning_line({"owners": ["greg"], "role": "household"}, learning) == ""
    # Béré recognised (ACTIVE) at Finn while Greg is still learning: still not answered for Greg.
    assert "only answer Greg" in vi.refusal(finn, vi.Who(vp.ME, "bere", "Béré", enrolled=("bere", "greg"), active=("bere",)))
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
    assert tts.unmark(out["speech"])[0] == "Done."
    text, kw = links.c.started[0]
    assert text == "[Speaker: Greg] What's on today?" and "The person speaking is Greg" in kw["instructions"]


def test_a_personal_agent_declines_someone_else_without_running(db, heard, monkeypatch, tmp_path):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    auth_db.set_voice_print("bere", print_of(BERE).to_json())
    heard["rows"] = [row("What's on today?", near(BERE, RNG))]
    links = Links({"name": "finn", "owners": ["greg"], "model": {}})
    out = converse(links, "What's on today?", monkeypatch, tmp_path, agent="finn")
    assert "only answer Greg" in tts.unmark(out["speech"])[0] and out["agent"] == "finn"
    assert links.c.started == []                                          # the agent was never run
    heard["rows"] = [row("What's on today?", near(GREG, RNG))]
    out = converse(links, "What's on today?", monkeypatch, tmp_path, agent="finn")
    assert tts.unmark(out["speech"])[0] == "Done." and links.c.started[0][0].startswith("[Speaker: Greg]")


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
        app.router.add_delete("/api/v2/voiceprint/heard/{id}", vi.handle_not_me)
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
    heard["rows"] = [row("a", near(GREG, RNG, 1.4), id_=3, at=time.time() - 1500), row("b", near(BERE, RNG), id_=4)]

    async def steps(c):
        listed = await (await c.get("/api/v2/voiceprint/heard")).json()
        mine = await c.post("/api/v2/voiceprint/heard/3")
        hers = await c.post("/api/v2/voiceprint/heard/4")
        gone = await c.post("/api/v2/voiceprint/heard/9")
        return listed, mine.status, await mine.json(), hers.status, gone.status
    listed, ok, body, hers, gone = api(steps, monkeypatch=monkeypatch)
    assert sorted(r["id"] for r in listed["heard"]) == [3, 4]
    assert set(listed["heard"][0]) == {"id", "at", "ago", "speech", "score", "recognised", "added", "held"}
    assert ok == 200 and body["summary"]["learned"] == 1 and body["summary"]["progress"]["labelled"] == 1
    assert hers == 409                       # it sounds like Béré, who has a print: not added to Greg's
    assert gone == 404


def test_the_speaker_list_is_newest_first_for_half_an_hour(db, heard, monkeypatch):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    now = time.time()
    heard["rows"] = [row("old", near(GREG, RNG), id_=1, at=now - 1700), row("new", near(GREG, RNG), id_=2, at=now - 5),
                     row("gone", near(GREG, RNG), id_=0, at=now - 1900)]

    async def steps(c):
        return await (await c.get("/api/v2/voiceprint/heard")).json()
    got = api(steps, monkeypatch=monkeypatch)
    assert [r["id"] for r in got["heard"]] == [2, 1] and got["heard"][1]["ago"] >= 1700 and got["state"] == vp.ACTIVE


# ── LEARNING: collecting labelled samples (VO.3) ───────────────────────────────────────────

FINN = {"name": "finn", "owners": ["greg"]}
HOME = {"name": "household", "role": "household", "owners": ["greg", "bere"]}
CHANNEL = unit(random.Random(77))


def at_speaker(base, rng, noise=0.6):
    """The same voice through the house speaker: its own microphone and room added."""
    return near(vp.normalise([b + 0.5 * c for b, c in zip(base, CHANNEL)]), rng, noise)


def labelled(prints, base, rng, n, t0, owner="greg", day_every=20, first_id=1, speech=2.5):
    out = []
    for i in range(n):
        at = t0 + (i // day_every) * 86400 + i * 30
        r = {"id": first_id + i, "at": at, "embedding": at_speaker(base, rng), "speech": speech, "snr": 20, "level": -35}
        out.append(vi.collect_sync(prints, r, owner, None, SPEAKER_COHORT, now=at))
    return out


def test_commands_for_its_persons_agent_are_learned_and_tested_first(db):
    prints = {"greg": print_of(GREG, active=False)}
    rng = random.Random(1)
    got = labelled(prints, GREG, rng, 5, 1_700_000_000)
    p = prints["greg"]
    assert len(p.labelled) == 5 and len(p.trials) == 5 and all(m["src"] == "name" for m in p.meta)
    assert all(t["ok"] is not None for t in p.trials) and got[0]["changed"] == {"greg"}
    assert p.meta[0]["speech"] == 2.5 and p.meta[0]["level"] == -35 and p.meta[0]["id"] == vi.hkey({"id": 1, "at": 1_700_000_000})
    assert p.state == vp.LEARNING                                        # five is far from enough


def test_it_switches_on_after_enough_good_commands_over_days_and_tells_the_person(db):
    prints = {"greg": print_of(GREG, active=False)}
    rng = random.Random(2)
    got = labelled(prints, GREG, rng, vp.ON_SAMPLES + 10, 1_700_000_000)
    states = [g["states"] for g in got if g["states"]]
    assert states and states[0] == {"greg": vp.ACTIVE} and prints["greg"].state == vp.ACTIVE
    asyncio.run(vi._tell({}, "greg", vp.ACTIVE))
    items = vi.notifications_for("greg")
    assert items and items[0]["kind"] == "voiceprint" and "recognises your voice" in items[0]["title"]
    assert vi.notifications_for("bere") == []


def test_a_second_voice_saying_the_name_again_and_again_does_not_get_in(db):
    """Béré (or a guest) saying "Hey Finn": labelled as Greg's, but nothing like Greg's voice: held back, not
    a sample, not a miss, and it does not bring Greg nearer to switching on."""
    prints = {"greg": print_of(GREG, active=False)}
    rng = random.Random(3)
    labelled(prints, GREG, rng, 10, 1_700_000_000)
    before = prints["greg"].progress()
    for i in range(40):
        at = 1_700_000_000 + 86400 * (1 + i // 10) + i
        r = {"id": 100 + i, "at": at, "embedding": at_speaker(GUEST, rng), "speech": 2.5, "snr": 20}
        vi.collect_sync(prints, r, "greg", None, SPEAKER_COHORT, now=at)
    p = prints["greg"]
    after = p.progress()
    assert len(p.labelled) == 10 and after["labelled"] == before["labelled"]          # none of them got in
    assert after["hits"] == before["hits"] and after["window"] == before["window"]  # not misses, not hits
    assert after["held"] == 40 and len(vi.held_for("greg")) == 40 and p.state == vp.LEARNING
    assert {h["why"] for h in vi.held_for("greg")} <= {"did not sound like you", "too unlike you to be sure"}
    # With Béré's print there too, her voice is "more like someone else".
    prints["bere"] = print_of(BERE, active=False)
    r = {"id": 900, "at": 1_800_000_000, "embedding": at_speaker(BERE, rng), "speech": 2.5}
    vi.collect_sync(prints, r, "greg", None, SPEAKER_COHORT, now=1_800_000_000)
    assert not prints["greg"].has_heard(vi.hkey(r))


def test_a_command_that_claims_someone_else_or_sounds_like_an_active_print_is_not_learned(db):
    prints = {"greg": print_of(GREG, active=False), "bere": print_of(BERE)}
    rng = random.Random(4)
    r = {"id": 1, "at": 1_700_000_000, "embedding": at_speaker(GREG, rng), "speech": 2.5}
    assert vi.collect_sync(prints, r, "greg", "bere", SPEAKER_COHORT)["why"] == "claimed someone else"
    assert not prints["greg"].labelled
    r2 = {"id": 2, "at": 1_700_000_100, "embedding": near(BERE, rng, 0.3), "speech": 2.5}
    assert vi.collect_sync(prints, r2, "greg", None, SPEAKER_COHORT)["why"] == "someone else"
    assert vi.collect_sync(prints, r, "greg", "greg", SPEAKER_COHORT)["changed"] == {"greg"}   # claiming yourself is fine
    assert vi.collect_sync(prints, r, "greg", None, SPEAKER_COHORT)["why"] == "already kept"


def test_home_only_adds_a_clear_match_and_never_as_a_label(db):
    prints = {"greg": print_of(GREG, active=False), "bere": print_of(BERE, active=False)}
    rng = random.Random(5)
    r = {"id": 1, "at": 1_700_000_000, "embedding": near(GREG, rng, 0.2), "speech": 3.0, "snr": 20, "clipped": False}
    vi.collect_sync(prints, r, None, None, SPEAKER_COHORT)
    assert prints["greg"].learned == 1 and prints["greg"].meta[0]["src"] == "home" and not prints["greg"].trials
    r = {"id": 2, "at": 1_700_000_100, "embedding": GUEST, "speech": 3.0, "snr": 20}
    vi.collect_sync(prints, r, None, None, SPEAKER_COHORT)
    assert prints["greg"].learned == 1 and prints["bere"].learned == 0


def test_once_active_only_what_it_recognises_is_added_and_a_stranger_cannot_switch_it_off(db):
    prints = {"greg": print_of(GREG, active=False)}
    rng = random.Random(6)
    labelled(prints, GREG, rng, vp.ON_SAMPLES + 5, 1_700_000_000)
    p = prints["greg"]
    assert p.state == vp.ACTIVE
    n, keys = len(p.trials), set()
    for i in range(20):                                  # a guest saying "Hey Finn" over and over
        r = {"id": 500 + i, "at": 1_800_000_000 + i, "embedding": at_speaker(GUEST, rng), "speech": 2.5}
        keys.add(vi.hkey(r))
        vi.collect_sync(prints, r, "greg", None, SPEAKER_COHORT, now=1_800_000_000 + i)
    assert p.state == vp.ACTIVE and len(p.trials) == n and not keys & {m.get("id") for m in p.meta}
    assert {h["why"] for h in vi.held_for("greg")} == {"not recognised"}


def test_that_was_me_and_not_me(db, heard, monkeypatch):
    p = print_of(GREG, active=False)
    auth_db.set_voice_print("greg", p.to_json())
    rng = random.Random(7)
    far = near(GREG, rng, 1.2)
    heard["rows"] = [row("a", far, id_=3), row("b", near(GREG, RNG), id_=4)]

    async def steps(c):
        mine = await (await c.post("/api/v2/voiceprint/heard/3")).json()
        listed = await (await c.get("/api/v2/voiceprint/heard")).json()
        out = await c.delete("/api/v2/voiceprint/heard/3")
        again = await c.delete("/api/v2/voiceprint/heard/3")
        return mine, listed, out.status, await out.json(), again.status
    mine, listed, status, body, again = api(steps, monkeypatch=monkeypatch)
    assert mine["ok"] and mine["summary"]["progress"]["labelled"] == 1
    assert next(r for r in listed["heard"] if r["id"] == 3)["added"] is True
    assert status == 200 and body["summary"]["progress"]["labelled"] == 0 and again == 404
    assert vp.Profile.from_json(auth_db.get_voice_prints()["greg"]).learned == 0


def test_the_person_can_start_learning_again_and_an_admin_sees_everyones_state(db, heard, monkeypatch):
    auth_db.set_voice_print("greg", print_of(GREG).to_json())
    auth_db.set_voice_print("bere", print_of(BERE, active=False).to_json())

    async def steps(c):
        bad = (await c.post("/api/v2/voiceprint/state", json={"state": "active"})).status
        ok = await (await c.post("/api/v2/voiceprint/state", json={"state": "learning"})).json()
        people = await c.get("/api/v2/voiceprint/people")
        return bad, ok, people.status, await people.json()

    def app_with_state(steps, user, role):
        from gateway import dashboard_api as d
        monkeypatch.setattr(d, "_user", lambda r: {"id": user, "username": user, "display_name": user, "role": role})

        async def run():
            app = web.Application()
            app.router.add_post("/api/v2/voiceprint/state", vi.handle_state)
            app.router.add_get("/api/v2/voiceprint/people", vi.handle_people)
            async with TestClient(TestServer(app)) as c:
                return await steps(c)
        return asyncio.run(run())
    bad, ok, status, people = app_with_state(steps, "greg", "user")
    assert bad == 400 and ok["summary"]["state"] == vp.LEARNING and status == 403
    assert vp.Profile.from_json(auth_db.get_voice_prints()["greg"]).state == vp.LEARNING
    _, _, status, people = app_with_state(steps, "bere", "admin")
    assert status == 200 and {p["username"]: p["state"] for p in people["people"]} == {"bere": "learning", "greg": "learning"}
    assert "samples" not in json.dumps(people) and "embedding" not in json.dumps(people)


def test_the_speaker_answers_while_learning_and_learns_afterwards(db, heard, monkeypatch, tmp_path):
    from gateway import wake_host
    auth_db.set_voice_print("greg", print_of(GREG, active=False).to_json())
    heard["rows"] = [row("What's on today?", at_speaker(GUEST, RNG))]
    monkeypatch.setattr(wake_host, "agent_for", lambda *a, **k: "finn")
    links = Links({"name": "finn", "owners": ["greg"], "model": {}})
    collected = []

    async def fake_collect(app, who, definition, woken, claim):
        collected.append((who.state, woken, claim))
    monkeypatch.setattr(vi, "collect", fake_collect)
    out = converse(links, "What's on today?", monkeypatch, tmp_path, agent="finn")
    assert tts.unmark(out["speech"])[0] == "Done."                      # no "I only answer Greg"
    text, kw = links.c.started[0]
    assert "still learning Greg's voice" in kw["instructions"] and not text.startswith("[Speaker:")
    assert collected and collected[0][1] is True


def test_a_claim_to_be_someone_still_learning_is_not_an_alarm(db, heard, monkeypatch, tmp_path):
    from gateway import impostor
    auth_db.set_voice_print("greg", print_of(GREG, active=False).to_json())
    heard["rows"] = [row("It's Greg, read my email", GUEST)]
    alerts = []
    monkeypatch.setattr(impostor, "alert", lambda *a, **k: alerts.append(a) or asyncio.sleep(0))
    links = Links({"name": "finn", "owners": ["greg"], "model": {}})
    out = converse(links, "It's Greg, read my email", monkeypatch, tmp_path, agent="finn")
    assert "doesn't sound like" not in tts.unmark(out["speech"])[0] and alerts == []
    assert "do not act for Greg" not in links.c.started[0][1]["instructions"]
