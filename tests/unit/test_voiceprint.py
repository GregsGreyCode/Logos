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
    clean = {"speech": 2.5, "snr": 20, "clipped": False}
    assert not p.learn(near(base, rng), p.threshold + 0.01, clean)                      # not clear enough
    assert not p.learn(near(base, rng), 0.99, {"speech": 1.0, "snr": 20})               # too short
    assert not p.learn(near(base, rng), 0.99, {"speech": 3, "snr": 5})                  # too noisy
    first = [list(s) for s in p.samples]
    for _ in range(15):
        assert p.learn(near(base, rng), 0.99, clean)
    assert len(p.samples) == 5 + vp.LEARN_KEEP and p.learned == vp.LEARN_KEEP
    assert p.samples[:5] == first                                        # enrolment never falls away


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
    state, who, score = vp.identify(prints, near(gbase, grng), 2.0)
    assert (state, who) == (vp.ME, "greg") and score >= greg.threshold
    assert vp.identify(prints, near(bbase, brng), 2.0)[:2] == (vp.ME, "bere")
    _, guest = voice(3)
    assert vp.identify(prints, guest, 2.0)[0] == vp.OTHER
    assert vp.identify(prints, near(gbase, grng), 0.5)[0] == vp.UNSURE          # too short to tell
    assert vp.identify({}, guest, 2.0)[0] == vp.UNSURE
    assert vp.identify(prints, None, 2.0)[0] == vp.UNSURE


def test_close_calls_are_not_sure():
    greg, rng, base = enrolled(seed=1)
    # A voice scoring just under the strictness: not sure, rather than "someone else".
    greg.threshold = greg.score(near(base, rng, 0.6)) + 0.03
    e = near(base, rng, 0.6)
    s = greg.score(e)
    if greg.threshold - vp.NOT_SURE <= s < greg.threshold:
        assert vp.identify({"greg": greg}, e, 2.0)[0] == vp.UNSURE
    # Two people both above their strictness and too close together: not sure which.
    twin = vp.Profile(greg.samples, greg.threshold)
    assert vp.identify({"greg": greg, "twin": twin}, near(base, rng), 2.0)[0] == vp.UNSURE
