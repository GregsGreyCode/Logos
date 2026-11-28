"""Voice prints: who is speaking at the house speaker (Voice VO.1, VO.2, VO.3).

A voice print is a set of speaker embeddings (192 numbers each, from the CAM++ model in the
``logos-stt`` container, gateway/stt.py) and the strictness it needs. Never audio.

- **Two kinds of samples.** Enrolment (``samples[:fixed]``): about 25 s read into a phone or laptop in
  Settings, cut into command-sized pieces. Speaker samples (``samples[fixed:]``, with ``meta``): commands
  heard at the house speaker, which sounds different (far-field microphone, room, distance).
- **The score** of a voice is the average of its likeness to the centre of all the samples and to the
  three closest.
- **Two strictnesses**, each just above the reference voices (public LibriSpeech recordings in the
  image): ``threshold`` for Settings' recordings, against the clips as recorded; ``speaker_threshold``
  for the speaker, against the same voices made to sound like the speaker and cut to command length,
  and against the other people of the house as the speaker heard them.
  Never looser than 0.5 (measured: docs/voice.md).
- **LEARNING, then ACTIVE (VO.3, Greg 28 Nov).** A print starts LEARNING: nothing is enforced, and
  commands its person's agent was woken for ("Hey Finn" -> Greg) are collected as labelled speaker
  samples, unless the voice is clearly unlike the print (:meth:`Profile.admission`: held back, so a
  guest saying the name does not get in). Each labelled command is first scored against the print as it
  was (a self-test). It switches to ACTIVE only with :data:`ON_SAMPLES` labelled samples over
  :data:`ON_DAYS` days, :data:`ON_RATE` of the last :data:`ON_WINDOW` recognised and nobody else taken
  for them; it falls back below :data:`OFF_RATE`.

Stdlib only: the gateway decides with it (prints stay in the dashboard's database) and the
container cuts recordings with it (it is mounted there read-only beside gateway/stt.py).
"""
from __future__ import annotations

import hashlib
import math
import re
import statistics
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

RATE = 16000
FLOOR = 0.5                 # never looser than this (Settings' recordings)
SPEAKER_FLOOR = 0.5         # nor at the speaker (docs/voice.md: the numbers for keeping it)
SPEAKER_MARGIN = 0.06       # the speaker strictness: this far above the closest speaker-like reference voice
HOUSE_MARGIN = 0.05         # and this far above the closest other person of the house, as heard at the speaker
CEILING = 0.85
LEARN_MARGIN = 0.12         # a match this far above the strictness is clear enough to learn from through Home
NOT_SURE = 0.08             # this close below the strictness: "not sure", not "someone else"
APART = 0.08                # two people both above their strictness: the best must lead by this much
CHUNK_S = 3.0               # enrolment speech is cut into pieces about as long as a spoken command
MIN_ENROL_S = 6.0           # seconds of speech an enrolment recording needs
MIN_TEST_S = 1.5
MIN_HEARD_S = 0.8           # below this, a command at the speaker is too short to tell
SHORT_S = 1.5               # below this, never "someone else" (too short to say it is not you); not a self-test
MAX_ENROL = 30              # enrolment samples kept
SPEAKER_KEEP = 60           # speaker samples kept (spread over days and distances)
MAX_SAMPLES = MAX_ENROL + SPEAKER_KEEP
DUPLICATE = 0.97            # a new speaker sample this like one kept the same day adds nothing
PHONE, SPEAKER = "phone", "speaker"
LEARNING, ACTIVE = "learning", "active"
LABELLED = ("name", "me")   # how a sample is known to be the person's: their agent's name, or "That was me"

# Admission of a labelled command while learning (a guest saying "Hey Finn" must not get in):
ADMIT_CORE = 0.30           # its median likeness to the person's samples must reach this (a minority of bad
                            # samples cannot move a median), else it is held back
CORE_MIN = 5                # until this many labelled speaker samples, the name alone lets one in (the speaker
                            # may sound nothing like the phone at first); then any of them that disagrees with
                            # the rest goes again (:meth:`Profile.prune`)
# Switching on (ACTIVE) and off again:
ON_SAMPLES = 50             # labelled speaker samples
ON_DAYS = 3                 # on this many different days
ON_DAY_SHARE = 0.6          # and no day more than this share of them (not all from one sitting)
ON_WINDOW = 50              # the self-test: the last this many labelled commands
ON_RATE = 0.99              # recognised at least this share of them (with 50: every one)
ON_HELD = 10                # at most this many held back while those were heard (one in six)
OFF_RATE = 0.95             # ACTIVE falls back to learning below this
TRIALS_KEEP = 100


# ── Vectors ──────────────────────────────────────────────────────────────────────────────────

def normalise(v: Sequence[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in v)) + 1e-9
    return [float(x) / n for x in v]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    d = x = y = 0.0
    for p, q in zip(a, b):
        d += p * q
        x += p * p
        y += q * q
    return d / (math.sqrt(x) * math.sqrt(y) + 1e-9)


def heard_key(text: str) -> str:
    """The same for the same words, whatever Home Assistant does to spaces, case or punctuation
    (as gateway/tts.py does for the answers). A hash: the words themselves are not kept."""
    words = re.findall(r"\w+", re.sub(r"['’]", "", str(text or "").casefold()))
    return hashlib.sha256(" ".join(words).encode()).hexdigest()


# ── Looking at a recording ───────────────────────────────────────────────────────────────────

class Take:
    """How much of a recording is speech, how clean, whether it was clipped; and the speech alone,
    trimmed and brought to a steady level (what the embedding is made from)."""

    def __init__(self, speech: float, snr: float, clipped: bool, audio: Sequence[float], level: float = -90.0):
        self.speech, self.snr, self.clipped, self.audio, self.level = speech, snr, clipped, audio, level

    def quality(self) -> dict:
        """``level``: how loud the speech arrived (dBFS of its loud part), a hint of how far away it was said."""
        return {"speech": round(self.speech, 1), "snr": round(self.snr, 1), "clipped": self.clipped,
                "level": round(self.level, 1)}


def analyse(f: Sequence[float], rate: int = RATE) -> Take:
    """The spike's ``analyse``: 20 ms frames, a gate above the room's own level, every loud frame
    also counting the 120 ms either side of it (the gaps between words are part of speaking)."""
    frame = rate // 50
    n = len(f) // frame
    if n < 10:
        return Take(0.0, 0.0, False, f)
    rms = []
    for i in range(n):
        seg = f[i * frame:(i + 1) * frame]
        rms.append(math.sqrt(sum(v * v for v in seg) / frame))
    srt = sorted(rms)
    floor = srt[int(n * 0.2)] + 1e-5
    loud = srt[int(n * 0.9)]
    gate = max(0.004, floor * 2.5)
    voiced = [False] * n
    first = last = -1
    for i, r in enumerate(rms):
        if r > gate:
            if first < 0:
                first = i
            last = i
            for k in range(max(0, i - 6), min(n - 1, i + 6) + 1):
                voiced[k] = True
    speaking = sum(voiced)
    peak = max((abs(v) for v in f), default=0.0)
    if first < 0:
        return Take(0.0, 0.0, peak > 0.98, f)
    cut = f[max(0, (first - 10) * frame):min(len(f), (last + 15) * frame)]
    gain = min(8.0, 0.7 / peak) if peak > 0.001 else 1.0
    out = [max(-1.0, min(1.0, v * gain)) for v in cut]
    snr = 20 * math.log10(max(loud, 1e-9) / floor)
    return Take(speaking * frame / rate, snr, peak > 0.98, out, 20 * math.log10(max(loud, 1e-9)))


def pieces(audio: Sequence[float], rate: int = RATE, seconds: float = CHUNK_S) -> List[Sequence[float]]:
    """Enrolment speech cut into pieces of about ``seconds`` (the last one joins the one before when
    it is short), so the print holds several samples as long as a spoken command."""
    size = int(rate * seconds)
    if len(audio) < size * 1.5:
        return [audio]
    out = [audio[i:i + size] for i in range(0, len(audio), size)]
    if len(out) > 1 and len(out[-1]) < size // 2:
        tail = out.pop()
        out[-1] = list(out[-1]) + list(tail)
    return out


def problem(q: Mapping[str, Any], need: float = MIN_ENROL_S) -> Optional[str]:
    """What is wrong with a recording, in words for the person; None when it is good enough."""
    speech = float(q.get("speech") or 0)
    if speech < need:
        return (f"I heard {speech:.1f} seconds of speech and need {need:.0f}. Record again, and keep "
                "reading until the time runs out.")
    if q.get("clipped"):
        return "That was too loud and distorted. Record again a little further from the microphone."
    if float(q.get("snr") or 0) < 8:
        return (f"Too much background noise ({float(q.get('snr') or 0):.0f} dB). Find a quieter spot "
                "and record again.")
    return None


def scorer(samples: Sequence[Sequence[float]]):
    """``Profile._score`` against the same samples many times (the reference voices): the centre and the
    lengths worked out once."""
    d = len(samples[0])
    m = [0.0] * d
    for s in samples:
        for i in range(d):
            m[i] += s[i]
    lens = [math.sqrt(sum(x * x for x in s)) for s in samples]
    mlen = math.sqrt(sum(x * x for x in m))

    def score(e: Sequence[float]) -> float:
        ne = math.sqrt(sum(x * x for x in e))
        sims = sorted((sum(a * b for a, b in zip(e, s)) / (ne * n + 1e-9) for s, n in zip(samples, lens)), reverse=True)
        top = sims[:3]
        return (sum(a * b for a, b in zip(e, m)) / (ne * mlen + 1e-9) + sum(top) / len(top)) / 2
    return score


# ── A person's voice print ───────────────────────────────────────────────────────────────────

class Profile:
    """Enrolment samples (``samples[:fixed]``, recorded on a phone or laptop in Settings) and speaker samples
    (``samples[fixed:]``, heard at the house speaker; ``meta`` says when, how long, how loud and how each was
    labelled). Two strictnesses: ``threshold`` for a recording from Settings, ``speaker_threshold`` for a
    command at the speaker. ``state``: LEARNING (collecting, nothing enforced) or ACTIVE (recognising)."""

    def __init__(self, samples: Iterable[Sequence[float]], threshold: float = FLOOR, *, fixed: Optional[int] = None,
                 genuine: Optional[List[float]] = None, impostors: Optional[List[float]] = None,
                 cohort: Optional[List[float]] = None, meta: Optional[List[dict]] = None,
                 speaker_threshold: Optional[float] = None, speaker_cohort: Optional[Mapping[str, Any]] = None,
                 state: str = LEARNING, since: float = 0.0, trials: Optional[List[dict]] = None):
        self.samples: List[List[float]] = [list(map(float, s)) for s in samples]
        self.threshold = float(threshold)
        self.fixed = len(self.samples) if fixed is None else int(fixed)
        self.genuine = list(genuine or [])
        self.impostors = list(impostors or [])
        self.cohort = list(cohort or [])
        extra = len(self.samples) - self.fixed
        meta = [dict(m) if isinstance(m, Mapping) else {} for m in (meta or [])][:extra]
        self.meta: List[dict] = meta + [{"src": "learned"} for _ in range(extra - len(meta))]   # v1: auto-learned
        self.speaker_threshold = None if speaker_threshold is None else float(speaker_threshold)
        self.speaker_cohort = dict(speaker_cohort or {})            # {"n", "top", "over"}: never the voices
        self.state = state if state in (LEARNING, ACTIVE) else LEARNING
        self.since = float(since or 0)
        self.trials: List[dict] = [dict(t) for t in (trials or []) if isinstance(t, Mapping)][-TRIALS_KEEP:]

    # ── scores ──

    @property
    def speaker_samples(self) -> List[List[float]]:
        return self.samples[self.fixed:]

    @staticmethod
    def _score(samples: Sequence[Sequence[float]], e: Sequence[float]) -> float:
        sims = sorted((cosine(e, s) for s in samples), reverse=True)
        top = sims[:3]
        d = len(samples[0])
        m = [0.0] * d
        for s in samples:
            for i in range(d):
                m[i] += s[i]
        return (cosine(e, m) + sum(top) / len(top)) / 2

    @property
    def centre(self) -> List[float]:
        d = len(self.samples[0])
        m = [0.0] * d
        for s in self.samples:
            for i in range(d):
                m[i] += s[i]
        return normalise(m)

    @property
    def cohort_top(self) -> float:
        return max(self.cohort) if self.cohort else 0.0

    def score(self, e: Sequence[float], channel: str = PHONE) -> float:
        """The average of the likeness to the centre of the samples and to the three closest, over every sample
        (enrolment and speaker). Measured 28 Nov: scoring a speaker command against the speaker samples alone
        recognised you a little more often but let far more other voices through, because the room and the
        microphone are in those samples too (docs/voice.md). ``channel`` only picks the strictness."""
        return self._score(self.samples, e)

    def threshold_for(self, channel: str = PHONE) -> float:
        if channel == SPEAKER and self.speaker_threshold is not None:
            return self.speaker_threshold
        return self.threshold

    # ── strictness ──

    def calibrate(self, others: Optional[Sequence[Sequence[float]]] = None) -> None:
        """Set the strictness for recordings from Settings: just above the highest of anyone else."""
        if others:
            f = scorer(self.samples)
            self.cohort = [round(f(o), 4) for o in others]
        enrol = self.samples[: self.fixed]
        if len(enrol) >= 2:
            self.genuine = [round(self._score(enrol[:i] + enrol[i + 1:], enrol[i]), 4) for i in range(len(enrol))]
        else:
            self.genuine = []
        if self.genuine:
            mu = sum(self.genuine) / len(self.genuine)
            sd = math.sqrt(sum((g - mu) ** 2 for g in self.genuine) / len(self.genuine))
            low = mu - 2 * sd                                   # nearly every time you speak is above this
        else:
            low = FLOOR
        tops = ([max(self.impostors)] if self.impostors else []) + ([self.cohort_top + 0.06] if self.cohort else [])
        if tops:
            top = max(tops)
            # Just above the closest other voice, not halfway to your own scores as in the spike: you enrol on a
            # phone or laptop and are heard through the speaker's microphone, which scores you lower (measured
            # 27 Nov: halfway rejected 63% of far-field commands, just above the others 29%, 9% once it learned two).
            t = top + 0.04 if top + 0.04 < low else max(top + 0.03, low - 0.02)
        else:
            t = max(FLOOR, low)                                 # nobody else heard yet: stricter than plain
        self.threshold = round(min(CEILING, max(FLOOR, t)), 4)

    def calibrate_speaker(self, others: Sequence[Sequence[float]], household: Sequence[Sequence[float]] = ()) -> None:
        """Set the strictness at the speaker: above the reference voices made to sound like the speaker (the
        container's ``/cohort?channel=speaker``) and above the other people of the house as the speaker itself
        heard them (``household``: their labelled speaker samples, the same room and microphone)."""
        if not others:
            return
        f = scorer(self.samples)
        scores = [f(o) for o in others]
        top = max(scores)
        near = max((f(h) for h in household), default=None)
        tops = [top + SPEAKER_MARGIN] + ([max(self.impostors) + 0.03] if self.impostors else []) \
            + ([near + HOUSE_MARGIN] if near is not None else [])
        self.speaker_threshold = round(min(CEILING, max(SPEAKER_FLOOR, *tops)), 4)
        self.speaker_cohort = {"n": len(scores), "top": round(top, 4), "house": round(near, 4) if near is not None else None,
                               "over": sum(1 for x in scores if x >= self.speaker_threshold)}

    # ── speaker samples ──

    def learn(self, e: Sequence[float], score: float, q: Mapping[str, Any], now: Optional[float] = None) -> bool:
        """A clear match from a clean sentence (the spike's rule): added, unlabelled. True when it was added."""
        if score < self.threshold_for(SPEAKER) + LEARN_MARGIN or float(q.get("speech") or 0) < 2.0 \
                or float(q.get("snr") or 0) < 12 or q.get("clipped"):
            return False
        return self.add_speaker(e, q, "home", now=now)

    def add_learned(self, e: Sequence[float]) -> None:
        self.add_speaker(e, {}, "learned")

    def add_speaker(self, e: Sequence[float], q: Mapping[str, Any], src: str, heard: Any = None,
                    now: Optional[float] = None) -> bool:
        """Keep a speaker sample (``src``: ``name`` woken by its person's agent's name, ``me`` "That was me",
        ``home`` a clear match through Home). A near copy of one kept the same day is not added. Over
        :data:`SPEAKER_KEEP`, the most alike sample of the best-covered day goes (keeping days and distances)."""
        now = time.time() if now is None else now
        e = list(map(float, e))
        day = _day(now)
        if any(cosine(e, s) >= DUPLICATE and m.get("day") == day for s, m in zip(self.speaker_samples, self.meta)):
            return False
        self.samples.append(e)
        self.meta.append({"at": round(now, 1), "day": day, "src": src, "id": heard,
                          "speech": q.get("speech"), "snr": q.get("snr"), "level": q.get("level")})
        while len(self.speaker_samples) > SPEAKER_KEEP or len(self.samples) > MAX_SAMPLES:
            self._drop_one()
        return True

    def _drop_one(self) -> None:
        sp, meta = self.speaker_samples, self.meta
        days: Dict[Any, int] = {}
        for m in meta[:-1]:
            days[m.get("day")] = days.get(m.get("day"), 0) + 1
        busiest = max(days, key=lambda d: days[d])
        idx = [i for i, m in enumerate(meta[:-1]) if m.get("day") == busiest]
        worst = max(idx, key=lambda i: max((cosine(sp[i], sp[j]) for j in range(len(sp)) if j != i), default=0.0))
        self.samples.pop(self.fixed + worst)
        self.meta.pop(worst)

    def remove_heard(self, heard: Any, at: Optional[float] = None) -> bool:
        """"Not me": the sample added from this command, and its trial, are taken out."""
        hit = [i for i, m in enumerate(self.meta) if m.get("id") == heard and m.get("id") is not None
               and (at is None or abs(float(m.get("heard_at") or m.get("at") or 0) - float(at)) < 120)]
        for i in reversed(hit):
            self.samples.pop(self.fixed + i)
            self.meta.pop(i)
        before = len(self.trials)
        self.trials = [t for t in self.trials if t.get("id") != heard or t.get("id") is None]
        return bool(hit) or len(self.trials) != before

    def has_heard(self, heard: Any) -> bool:
        return heard is not None and any(m.get("id") == heard for m in self.meta)

    @property
    def learned(self) -> int:
        return max(0, len(self.samples) - self.fixed)

    @property
    def labelled(self) -> List[dict]:
        return [m for m in self.meta if m.get("src") in LABELLED]

    def core(self, e: Sequence[float]) -> float:
        """The median likeness to every sample: what most of the print says, so a few bad samples (a
        guest who got in early) cannot pull a new voice in."""
        return statistics.median(cosine(e, s) for s in self.samples)

    def admission(self, e: Sequence[float], others: Mapping[str, "Profile"],
                  cohort: Sequence[Sequence[float]] = ()) -> Tuple[bool, str, float]:
        """Whether a labelled command (its agent's name was said) is let into this print while learning:
        ``(admitted, why, score)``. Clearly unlike the print, or more like someone else who has a print, or
        borderline and no nearer this person than to a reference voice: held back."""
        s = self.score(e, SPEAKER)
        if any(p.score(e, SPEAKER) >= s for p in others.values()):
            return False, "someone_else", s
        if len(self.labelled) < CORE_MIN:
            return True, "starting", s
        if self.core(e) < ADMIT_CORE:
            return False, "unlike", s
        if s >= self.threshold_for(SPEAKER):
            return True, "recognised", s
        nearest = max((cosine(e, c) for c in cohort), default=-1.0)
        if s > nearest:
            return True, "nearest", s
        return False, "stranger", s

    def prune(self) -> List[Any]:
        """Labelled speaker samples that disagree with the others (their median likeness to every other
        sample, the person's own recording included, is under :data:`ADMIT_CORE`) are taken out again, and
        their self-tests become "held back": an early guest cannot stay in the core. The ids taken out."""
        sp = self.speaker_samples
        if len(self.labelled) < CORE_MIN or len(sp) < 3:
            return []
        out = []
        for i in reversed(range(len(sp))):
            if self.meta[i].get("src") not in LABELLED:
                continue
            rest = [cosine(sp[i], x) for j, x in enumerate(self.samples) if j != self.fixed + i]
            if statistics.median(rest) < ADMIT_CORE:
                out.append(self.meta[i].get("id"))
        for key in out:
            i = next(k for k, m in enumerate(self.meta) if m.get("id") == key)
            self.samples.pop(self.fixed + i)
            self.meta.pop(i)
            for t in self.trials:
                if t.get("id") == key and key is not None:
                    t["ok"] = None
        return out

    # ── would it have known you (the self-test) ──

    def record_trial(self, ok: Optional[bool], score: float, heard: Any = None, now: Optional[float] = None,
                     src: str = "name") -> None:
        """One labelled command: recognised (``ok``) or not by the print as it was; ``None``: held back
        (logged, never a miss)."""
        self.trials.append({"at": round(time.time() if now is None else now, 1),
                            "ok": None if ok is None else bool(ok), "score": round(float(score), 3), "id": heard, "src": src})
        self.trials = self.trials[-TRIALS_KEEP:]

    def progress(self, false_accepts: int = 0) -> dict:
        """How far it is from switching on, in numbers (Settings shows them; :meth:`review` acts on them)."""
        tested = [t for t in self.trials if t.get("ok") is not None]
        window = tested[-ON_WINDOW:]
        since = window[0]["at"] if len(window) >= ON_WINDOW else 0.0
        held = sum(1 for t in self.trials if t.get("ok") is None and float(t.get("at") or 0) >= since)
        hits = sum(1 for t in window if t.get("ok"))
        days: Dict[Any, int] = {}
        for m in self.labelled:
            days[m.get("day")] = days.get(m.get("day"), 0) + 1
        n = len(self.labelled)
        share = max(days.values()) / n if n else 1.0
        over = int(self.speaker_cohort.get("over") or 0)
        rate = hits / len(window) if window else 0.0
        missing = []
        if n < ON_SAMPLES:
            missing.append(f"{ON_SAMPLES - n} more commands at the speaker")
        if len(days) < ON_DAYS:
            missing.append(f"commands on {ON_DAYS - len(days)} more day{'s' if ON_DAYS - len(days) > 1 else ''}")
        elif share > ON_DAY_SHARE:
            missing.append("more commands on other days")
        if len(window) < ON_WINDOW or rate < ON_RATE:
            missing.append(f"recognising you {ON_RATE:.0%} of the last {ON_WINDOW} times")
        if held > ON_HELD:
            missing.append("fewer commands that did not sound like you")
        if false_accepts or over:
            missing.append("not taking anyone else for you")
        if self.speaker_threshold is None:
            missing.append("the speaker's reference voices")
        return {"labelled": n, "days": len(days), "day_share": round(share, 2), "window": len(window), "hits": hits,
                "rate": round(rate, 3), "held": held, "false_accepts": int(false_accepts), "cohort_over": over,
                "ready": not missing, "missing": missing}

    def review(self, false_accepts: int = 0, now: Optional[float] = None) -> Optional[str]:
        """Switch on when ready; back to learning when it gets worse. The new state when it changed, else None."""
        now = time.time() if now is None else now
        pr = self.progress(false_accepts)
        if self.state == LEARNING and pr["ready"]:
            self.state, self.since = ACTIVE, now
            return ACTIVE
        if self.state == ACTIVE and (false_accepts or (pr["window"] >= ON_WINDOW and pr["rate"] < OFF_RATE)):
            self.state, self.since = LEARNING, now
            return LEARNING
        return None

    def reset(self, now: Optional[float] = None) -> None:
        """Back to learning (the person asked): the self-test starts again; the samples are kept."""
        self.state, self.since, self.trials = LEARNING, time.time() if now is None else now, []

    # ── stored and shown ──

    def summary(self, false_accepts: int = 0) -> dict:
        """What Settings shows: never the embeddings."""
        g = self.genuine
        return {"samples": len(self.samples), "learned": self.learned, "threshold": round(self.threshold, 2),
                "speaker_threshold": round(self.speaker_threshold, 2) if self.speaker_threshold is not None else None,
                "own": [round(min(g), 2), round(max(g), 2)] if g else None,
                "others": len(self.cohort), "others_top": round(self.cohort_top, 2) if self.cohort else None,
                "speaker_others": self.speaker_cohort.get("n"), "speaker_others_top": self.speaker_cohort.get("top"),
                "tested_others": len(self.impostors), "state": self.state, "since": self.since,
                "speaker": len(self.speaker_samples), "progress": self.progress(false_accepts)}

    def to_json(self) -> dict:
        return {"v": 2, "samples": [[round(x, 5) for x in s] for s in self.samples], "fixed": self.fixed,
                "threshold": self.threshold, "genuine": self.genuine, "impostors": self.impostors,
                "cohort": self.cohort, "meta": self.meta, "speaker_threshold": self.speaker_threshold,
                "speaker_cohort": self.speaker_cohort, "state": self.state, "since": self.since, "trials": self.trials}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Profile":
        samples = [s for s in (d.get("samples") or []) if isinstance(s, list) and s][:MAX_SAMPLES]
        if not samples:
            raise ValueError("a voice print needs samples")
        return cls(samples, float(d.get("threshold") or FLOOR), fixed=min(int(d.get("fixed") or len(samples)), len(samples)),
                   genuine=d.get("genuine"), impostors=d.get("impostors"), cohort=d.get("cohort"), meta=d.get("meta"),
                   speaker_threshold=d.get("speaker_threshold"), speaker_cohort=d.get("speaker_cohort"),
                   state=str(d.get("state") or LEARNING), since=float(d.get("since") or 0), trials=d.get("trials"))


def _day(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(at))


def enrol(embeddings: Sequence[Sequence[float]], cohort: Sequence[Sequence[float]],
          before: Optional[Profile] = None, speaker_cohort: Optional[Sequence[Sequence[float]]] = None) -> Profile:
    """A voice print from new enrolment samples; ``before`` (an earlier print of the same person) keeps its
    enrolment samples, "someone else" scores, speaker samples, state and self-test, so recording again adds to it."""
    old = before.samples[: before.fixed] if before else []
    enrolment = (list(old) + [list(e) for e in embeddings])[-MAX_ENROL:]
    speaker = before.speaker_samples if before else []
    p = Profile(enrolment + speaker, fixed=len(enrolment), impostors=before.impostors if before else None,
                meta=before.meta if before else None, state=before.state if before else LEARNING,
                since=before.since if before else time.time(), trials=before.trials if before else None)
    p.calibrate(cohort)
    if speaker_cohort:
        p.calibrate_speaker(speaker_cohort)
    elif before is not None:
        p.speaker_threshold, p.speaker_cohort = before.speaker_threshold, before.speaker_cohort
    return p


# ── Who is it ────────────────────────────────────────────────────────────────────────────────

ME, OTHER, UNSURE = "known", "someone_else", "not_sure"


def identify(prints: Mapping[str, Profile], e: Optional[Sequence[float]], speech: float, channel: str = SPEAKER,
             everyone: bool = False) -> Tuple[str, Optional[str], float]:
    """``(state, username, score)``: ``known`` (that person), ``not_sure`` or ``someone_else``.
    ``username`` is the closest person even when not sure (for the log and Settings, never acted on).
    Only an ACTIVE print can be ``known`` (``everyone``: any print, for the self-test and Settings' tests);
    the closest being a print still learning is ``not_sure``. Under :data:`SHORT_S` of speech it is never
    ``someone_else``: too short to say it is not you."""
    if not prints or e is None:
        return UNSURE, None, 0.0
    scored = sorted(((p.score(e, channel), p.threshold_for(channel), u) for u, p in prints.items()), reverse=True)
    best, thr, who = scored[0]
    if speech < MIN_HEARD_S:
        return UNSURE, who, best
    if best >= thr:
        rival = next((s for s in scored[1:] if s[0] >= s[1]), None)
        if (rival is None or best - rival[0] >= APART) and (everyone or prints[who].state == ACTIVE):
            return ME, who, best
        return UNSURE, who, best
    if best >= thr - NOT_SURE or speech < SHORT_S:
        return UNSURE, who, best
    return OTHER, who, best
