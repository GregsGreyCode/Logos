"""Voice prints: the spike's enrolment and verification, in Python (Voice VO.2)."""
import math
import random

from gateway import tts
from gateway import voiceprint as vp

DIM = 192


def unit(rng):
    return vp.normalise([rng.gauss(0, 1) for _ in range(DIM)])


def near(base, rng, noise=0.6):
    """Another recording of the same voice: the voice plus a little of everything else."""
    return vp.normalise([b + noise * rng.gauss(0, 1) / math.sqrt(DIM) for b in base])


def voice(seed):
    rng = random.Random(seed)
    return rng, unit(rng)


def cohort(n=60, seed=99):
    rng = random.Random(seed)
    return [unit(rng) for _ in range(n)]


def test_vectors():
    assert abs(vp.cosine([1, 0], [1, 0]) - 1) < 1e-6 and abs(vp.cosine([1, 0], [0, 1])) < 1e-6
    assert abs(sum(x * x for x in vp.normalise([3, 4])) - 1) < 1e-6


def test_heard_key_matches_the_words_not_their_spelling():
    assert vp.heard_key("What's on today?") == vp.heard_key("  whats ON today ")
    assert vp.heard_key("What's on today?") != vp.heard_key("What's on tomorrow?")
    assert vp.heard_key("Add milk.") == tts.key("Add milk.")            # one rule for the words, both ways
    assert "milk" not in vp.heard_key("Add milk.")


def tone(seconds, amp=0.3, rate=vp.RATE):
    return [amp * math.sin(2 * math.pi * 220 * i / rate) for i in range(int(seconds * rate))]


def test_analyse_finds_the_speech_and_trims_the_silence():
    rng = random.Random(1)
    hiss = lambda s: [rng.gauss(0, 0.001) for _ in range(int(s * vp.RATE))]
    take = vp.analyse(hiss(1.0) + tone(2.0) + hiss(1.0))
    assert 1.9 <= take.speech <= 2.4
    assert len(take.audio) < 3.5 * vp.RATE and take.snr > 20 and not take.clipped
    assert max(abs(x) for x in take.audio) > 0.6                         # brought to a steady level
    assert vp.analyse(hiss(2.0)).speech < 0.5
    assert vp.analyse(tone(1.0, amp=1.0)).clipped
    assert vp.analyse([0.0] * 100).speech == 0


def test_enrolment_speech_is_cut_into_command_sized_pieces():
    assert len(vp.pieces([0.0] * (12 * vp.RATE))) == 4
    assert len(vp.pieces([0.0] * (4 * vp.RATE))) == 1
    parts = vp.pieces([0.0] * int(10.2 * vp.RATE))
    assert len(parts) == 3 and len(parts[-1]) == int(10.2 * vp.RATE) - 6 * vp.RATE      # a short tail joins the piece before


def test_a_poor_recording_says_what_is_wrong():
    assert vp.problem({"speech": 10, "snr": 25, "clipped": False}) is None
    assert "need 6" in vp.problem({"speech": 2.1, "snr": 25})
    assert "too loud" in vp.problem({"speech": 10, "snr": 25, "clipped": True})
    assert "background noise" in vp.problem({"speech": 10, "snr": 5})
    assert vp.problem({"speech": 2, "snr": 20}, vp.MIN_TEST_S) is None


def enrolled(seed=1, n=5, others=None):
    rng, base = voice(seed)
    p = vp.enrol([near(base, rng) for _ in range(n)], cohort() if others is None else others)
    return p, rng, base


def test_the_strictness_sits_above_everyone_else_and_never_below_half():
    p, rng, base = enrolled()
    assert len(p.cohort) == 60 and len(p.genuine) == 5
    assert p.threshold >= vp.FLOOR and p.threshold >= p.cohort_top + 0.06
    assert min(p.genuine) > p.threshold                                  # you pass
    assert p.score(near(base, rng)) > p.threshold
    _, other = voice(2)
    assert p.score(other) < p.threshold                                  # someone else does not
    lone = vp.enrol([near(base, rng) for _ in range(5)], [])             # no reference voices: stricter than plain
    assert lone.threshold >= vp.FLOOR and not lone.cohort


def test_someone_else_heard_raises_the_strictness():
    p, rng, base = enrolled()
    before = p.threshold
    p.impostors.append(before + 0.05)          # a person who scored above it, in "Test: someone else"
    p.calibrate()
    assert p.threshold > before + 0.05


def test_it_learns_clear_matches_and_keeps_its_enrolment():
    p, rng, base = enrolled()
    clean = {"speech": 2.5, "snr": 20, "clipped": False, "level": -30}
    assert not p.learn(near(base, rng), p.threshold + 0.01, clean)                      # not clear enough
    assert not p.learn(near(base, rng), 0.99, {"speech": 1.0, "snr": 20})               # too short
    assert not p.learn(near(base, rng), 0.99, {"speech": 3, "snr": 5})                  # too noisy
    first = [list(s) for s in p.samples]
    for i in range(vp.SPEAKER_KEEP + 15):
        assert p.learn(near(base, rng), 0.99, clean, now=1_700_000_000 + i * 3600)
    assert len(p.samples) == 5 + vp.SPEAKER_KEEP and p.learned == vp.SPEAKER_KEEP
    assert p.samples[:5] == first                                        # enrolment never falls away
    assert p.meta[-1]["src"] == "home" and p.meta[-1]["level"] == -30 and p.meta[-1]["speech"] == 2.5
    assert p.labelled == []                                              # a match through Home is not a label
    e = near(base, rng)
    assert p.add_speaker(e, clean, "name", heard="9:1", now=1_800_000_000)
    assert not p.add_speaker(e, clean, "name", heard="9:2", now=1_800_000_100)   # a near copy the same day adds nothing


def test_keeping_the_speaker_samples_spread_over_days():
    p, rng, base = enrolled()
    for i in range(vp.SPEAKER_KEEP):                     # one long sitting...
        p.add_speaker(near(base, rng), {"speech": 2}, "name", now=1_700_000_000 + i)
    for d in range(1, 4):                                # ...then a few on other days
        p.add_speaker(near(base, rng), {"speech": 2}, "name", now=1_700_000_000 + d * 86400)
    days = {m["day"] for m in p.meta}
    assert len(p.speaker_samples) == vp.SPEAKER_KEEP and len(days) == 4    # the long sitting gave way, not the others


def test_the_speaker_strictness_is_set_against_speaker_like_voices_and_the_house():
    p, rng, base = enrolled()
    assert p.threshold_for(vp.SPEAKER) == p.threshold                    # none yet: the recording's
    p.calibrate_speaker(cohort(seed=7))
    assert p.speaker_threshold >= vp.SPEAKER_FLOOR and p.speaker_threshold >= p.speaker_cohort["top"] + vp.SPEAKER_MARGIN
    assert p.speaker_cohort["over"] == 0 and p.speaker_cohort["n"] == 60
    housemate = near(base, rng, 1.5)                                     # someone of the house who sounds alike
    p.calibrate_speaker(cohort(seed=7), [housemate])
    assert p.speaker_threshold >= p.score(housemate, vp.SPEAKER) + vp.HOUSE_MARGIN - 1e-3 or p.speaker_threshold == vp.CEILING
    assert p.threshold_for(vp.SPEAKER) == p.speaker_threshold


def test_a_print_is_stored_as_numbers_and_shown_without_them():
    p, rng, base = enrolled()
    q = vp.Profile.from_json(p.to_json())
    assert q.threshold == p.threshold and q.fixed == p.fixed and len(q.samples) == len(p.samples)
    e = near(base, rng)
    assert abs(q.score(e) - p.score(e)) < 1e-3
    s = p.summary()
    assert s["samples"] == 5 and s["others"] == 60 and "samples" not in str(s.get("own"))
    assert all(not isinstance(v, list) or len(v) <= 2 for v in s.values())    # no embeddings in what is shown
    try:
        vp.Profile.from_json({"samples": []})
        assert False
    except ValueError:
        pass


def test_recording_again_adds_to_the_print():
    p, rng, base = enrolled(n=4)
    p.impostors.append(0.3)
    again = vp.enrol([near(base, rng) for _ in range(4)], cohort(), before=p)
    assert again.fixed == 8 and again.impostors == [0.3]


def test_who_is_it():
    greg, grng, gbase = enrolled(seed=1)
    bere, brng, bbase = enrolled(seed=2)
    prints = {"greg": greg, "bere": bere}
    assert vp.identify(prints, near(gbase, grng), 2.0)[:2] == (vp.UNSURE, "greg")     # still learning: nobody named
    assert vp.identify(prints, near(gbase, grng), 2.0, everyone=True)[:2] == (vp.ME, "greg")
    greg.state = bere.state = vp.ACTIVE
    state, who, score = vp.identify(prints, near(gbase, grng), 2.0)
    assert (state, who) == (vp.ME, "greg") and score >= greg.threshold
    assert vp.identify(prints, near(bbase, brng), 2.0)[:2] == (vp.ME, "bere")
    _, guest = voice(3)
    assert vp.identify(prints, guest, 2.0)[0] == vp.OTHER
    assert vp.identify(prints, near(gbase, grng), 0.5)[0] == vp.UNSURE          # too short to tell
    assert vp.identify(prints, guest, 1.2)[0] == vp.UNSURE                     # short: never "someone else"
    assert vp.identify({}, guest, 2.0)[0] == vp.UNSURE
    assert vp.identify(prints, None, 2.0)[0] == vp.UNSURE


def test_close_calls_are_not_sure():
    greg, rng, base = enrolled(seed=1)
    # A voice scoring just under the strictness: not sure, rather than "someone else".
    greg.threshold = greg.score(near(base, rng, 0.6)) + 0.03
    e = near(base, rng, 0.6)
    s = greg.score(e)
    if greg.threshold - vp.NOT_SURE <= s < greg.threshold:
        assert vp.identify({"greg": greg}, e, 2.0, vp.PHONE)[0] == vp.UNSURE
    # Two people both above their strictness and too close together: not sure which.
    twin = vp.Profile(greg.samples, greg.threshold)
    greg.state = twin.state = vp.ACTIVE
    assert vp.identify({"greg": greg, "twin": twin}, near(base, rng), 2.0)[0] == vp.UNSURE


def test_an_old_print_is_read_as_learning_with_its_learned_samples_from_the_speaker():
    p, rng, base = enrolled()
    old = {"v": 1, "samples": p.samples[:5] + [near(base, rng)], "fixed": 5, "threshold": 0.6, "genuine": [], "impostors": [], "cohort": []}
    q = vp.Profile.from_json(old)
    assert q.state == vp.LEARNING and q.learned == 1 and q.meta == [{"src": "learned"}] and q.speaker_threshold is None
    r = vp.Profile.from_json(q.to_json())
    assert r.to_json() == q.to_json()


# ── LEARNING -> ACTIVE (VO.3) ─────────────────────────────────────────────────────────────────

DAY = 86400


def ready_print(days=3, n=vp.ON_SAMPLES, misses=0, held=0, seed=1):
    p, rng, base = enrolled(seed=seed)
    p.calibrate_speaker(cohort(seed=7))
    t0 = 1_700_000_000
    for i in range(n):
        at = t0 + (i % days) * DAY + i * 60
        p.add_speaker(near(base, rng), {"speech": 2.5}, "name", heard=f"{i}:{at}", now=at)
        p.record_trial(i >= misses, 0.7, f"{i}:{at}", now=at)
    for k in range(held):
        p.record_trial(None, 0.1, f"h{k}", now=t0 + 3 * DAY + k)
    return p, rng, base


def test_it_switches_on_only_when_the_self_test_says_it_would_get_it_right():
    p, _, _ = ready_print()
    pr = p.progress()
    assert pr["ready"] and pr["labelled"] == 50 and pr["days"] == 3 and pr["hits"] == 50
    assert p.review(now=1_800_000_000) == vp.ACTIVE and p.state == vp.ACTIVE and p.since == 1_800_000_000
    assert p.review() is None                                            # nothing changes after


def test_not_yet_too_few_or_one_sitting_or_one_miss_or_a_false_accept_or_held_back():
    assert not ready_print(n=40)[0].progress()["ready"]                 # too few
    one_day = ready_print(days=1)[0].progress()
    assert not one_day["ready"] and any("day" in m for m in one_day["missing"])
    missed = ready_print(misses=1)[0]                                    # 49 of 50 is under 99%
    assert not missed.progress()["ready"] and missed.review() is None
    assert not ready_print()[0].progress(false_accepts=1)["ready"]      # it took someone of the house for them
    many_held = ready_print(held=vp.ON_HELD + 1)[0].progress()
    assert not many_held["ready"] and many_held["held"] == vp.ON_HELD + 1
    assert ready_print(held=vp.ON_HELD)[0].progress()["ready"]          # a few held back: not misses


def test_it_falls_back_to_learning_when_it_gets_worse():
    p, rng, base = ready_print()
    p.review()
    for i in range(3):                                                   # three "That was me" corrections
        p.record_trial(False, 0.4, f"m{i}")
    assert p.progress()["rate"] < vp.OFF_RATE and p.review() == vp.LEARNING and p.state == vp.LEARNING
    q, _, _ = ready_print()
    q.review()
    assert q.review(false_accepts=1) == vp.LEARNING                      # or takes someone else for them
    q.reset()
    assert q.state == vp.LEARNING and q.trials == []


def test_not_me_takes_the_sample_and_its_test_out():
    p, rng, base = ready_print(n=5)
    key = p.meta[2]["id"]
    assert p.has_heard(key) and p.remove_heard(key)
    assert not p.has_heard(key) and len(p.speaker_samples) == 4 and all(t["id"] != key for t in p.trials)
    assert not p.remove_heard("nope")


def started(seed=1, n=vp.CORE_MIN):
    """A print with its first few labelled samples from the speaker."""
    p, rng, base = enrolled(seed=seed)
    p.calibrate_speaker(cohort(seed=7))
    for i in range(n):
        p.add_speaker(near(base, rng), {"speech": 2}, "name", heard=f"own{i}", now=1_700_000_000 + i * 600)
    return p, rng, base


def test_a_voice_unlike_the_print_is_held_back():
    fresh, rng, base = enrolled(seed=1)
    _, guest = voice(9)
    assert fresh.admission(guest, {}, cohort(seed=7))[:2] == (True, "starting")   # the first few: the name decides
    p, rng, base = started()
    ok, why, _ = p.admission(near(base, rng), {}, cohort(seed=7))
    assert ok and why == "recognised"
    assert p.admission(guest, {}, cohort(seed=7))[:2] == (False, "unlike")
    bere, brng, bbase = enrolled(seed=2)
    assert p.admission(near(bbase, brng), {"bere": bere}, cohort(seed=7))[:2] == (False, "someone_else")
    assert fresh.admission(near(bbase, brng), {"bere": bere}, cohort(seed=7))[:2] == (False, "someone_else")
    # Borderline (above the core, under the strictness): in only when nearer this person than any reference voice.
    p.speaker_threshold = 0.99
    e = near(base, rng)
    assert p.admission(e, {}, cohort(seed=7))[:2] == (True, "nearest")
    assert p.admission(e, {}, [e])[:2] == (False, "stranger")


def test_a_guest_who_got_in_first_is_taken_out_again():
    """A guest said "Hey Finn" before its person did: let in while starting, then out once the person's own
    samples are the majority; their self-tests become "held back", and the guest stays out."""
    p, rng, base = enrolled(seed=1, n=8)
    p.calibrate_speaker(cohort(seed=7))
    grng, guest = voice(9)
    for i in range(3):
        p.add_speaker(near(guest, grng), {"speech": 2}, "name", heard=f"g{i}", now=1_700_000_000 + i * DAY)
        p.record_trial(False, 0.1, f"g{i}")
    for i in range(4):
        p.add_speaker(near(base, rng), {"speech": 2}, "name", heard=f"own{i}", now=1_700_100_000 + i * DAY)
    gone = p.prune()
    assert sorted(gone) == ["g0", "g1", "g2"] and all(not p.has_heard(f"g{i}") for i in range(3))
    assert all(p.has_heard(f"own{i}") for i in range(4))
    assert [t["ok"] for t in p.trials] == [None, None, None] and p.progress()["held"] == 3
    p.add_speaker(near(base, rng), {"speech": 2}, "name", heard="own9", now=1_700_900_000)
    assert p.admission(near(guest, grng), {}, cohort(seed=7))[:2] == (False, "unlike")
