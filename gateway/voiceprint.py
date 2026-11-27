"""Voice prints: who is speaking at the house speaker (Voice VO.1, VO.2).

A voice print is a handful of speaker embeddings (192 numbers each, from the CAM++ model in the
``logos-stt`` container, gateway/stt.py) made from about ten seconds of a person's voice, and the
strictness it needs. Never audio. Ported from the wake-word spike (F48), which tried it on a phone:

- **The score** of a new voice is the average of its likeness to the centre of the person's samples
  and to their three closest samples.
- **The strictness** (``threshold``) sits just above the highest score of anyone else (and below the
  person's own lowest scores, each enrolment sample against the others): a cohort of reference voices
  (public LibriSpeech recordings, built into the image), with a margin because studio recordings
  score lower than a stranger in the same room would, and anyone the person let it hear ("someone
  else" test). Never looser than 0.5.
- **It learns:** a clear match from a clean, long-enough sentence at the speaker is added (the
  newest ten beyond enrolment are kept), so it gets used to the speaker's microphone.

Stdlib only: the gateway decides with it (prints stay in the dashboard's database) and the
container cuts recordings with it (it is mounted there read-only beside gateway/stt.py).
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

RATE = 16000
FLOOR = 0.5                 # never looser than this
CEILING = 0.85
LEARN_MARGIN = 0.12         # a match this far above the strictness is clear enough to learn from
LEARN_KEEP = 10             # learned samples kept beyond enrolment
NOT_SURE = 0.08             # this close below the strictness: "not sure", not "someone else"
APART = 0.08                # two people both above their strictness: the best must lead by this much
CHUNK_S = 3.0               # enrolment speech is cut into pieces about as long as a spoken command
MIN_ENROL_S = 6.0           # seconds of speech an enrolment recording needs
MIN_TEST_S = 1.5
MIN_HEARD_S = 0.8           # below this, a command at the speaker is too short to tell
MAX_SAMPLES = 40


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

    def __init__(self, speech: float, snr: float, clipped: bool, audio: Sequence[float]):
        self.speech, self.snr, self.clipped, self.audio = speech, snr, clipped, audio

    def quality(self) -> dict:
        return {"speech": round(self.speech, 1), "snr": round(self.snr, 1), "clipped": self.clipped}


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
    return Take(speaking * frame / rate, snr, peak > 0.98, out)


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


# ── A person's voice print ───────────────────────────────────────────────────────────────────

class Profile:
    def __init__(self, samples: Iterable[Sequence[float]], threshold: float = FLOOR, *, fixed: Optional[int] = None,
                 genuine: Optional[List[float]] = None, impostors: Optional[List[float]] = None,
                 cohort: Optional[List[float]] = None):
        self.samples: List[List[float]] = [list(map(float, s)) for s in samples]
        self.threshold = float(threshold)
        self.fixed = len(self.samples) if fixed is None else int(fixed)
        self.genuine = list(genuine or [])
        self.impostors = list(impostors or [])
        self.cohort = list(cohort or [])

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

    def score(self, e: Sequence[float]) -> float:
        sims = sorted((cosine(e, s) for s in self.samples), reverse=True)
        top = sims[:3]
        return (cosine(e, self.centre) + sum(top) / len(top)) / 2

    def calibrate(self, others: Optional[Sequence[Sequence[float]]] = None) -> None:
        """Set the strictness: between your own lowest scores and the highest of anyone else, nearer
        the others."""
        if others:
            self.cohort = [round(self.score(o), 4) for o in others]
        enrol = self.samples[: self.fixed]
        if len(enrol) >= 2:
            self.genuine = [round(Profile(enrol[:i] + enrol[i + 1:]).score(enrol[i]), 4) for i in range(len(enrol))]
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

    def learn(self, e: Sequence[float], score: float, q: Mapping[str, Any]) -> bool:
        """Add a clear match from a clean sentence (the spike's rule). True when it was added."""
        if score < self.threshold + LEARN_MARGIN or float(q.get("speech") or 0) < 2.0 \
                or float(q.get("snr") or 0) < 12 or q.get("clipped"):
            return False
        self.add_learned(e)
        return True

    def add_learned(self, e: Sequence[float]) -> None:
        self.samples.append(list(map(float, e)))
        while len(self.samples) > self.fixed + LEARN_KEEP:
            self.samples.pop(self.fixed)

    @property
    def learned(self) -> int:
        return max(0, len(self.samples) - self.fixed)

    def summary(self) -> dict:
        """What Settings shows: never the embeddings."""
        g = self.genuine
        return {"samples": len(self.samples), "learned": self.learned, "threshold": round(self.threshold, 2),
                "own": [round(min(g), 2), round(max(g), 2)] if g else None,
                "others": len(self.cohort), "others_top": round(self.cohort_top, 2) if self.cohort else None,
                "tested_others": len(self.impostors)}

    def to_json(self) -> dict:
        return {"v": 1, "samples": [[round(x, 5) for x in s] for s in self.samples], "fixed": self.fixed,
                "threshold": self.threshold, "genuine": self.genuine, "impostors": self.impostors,
                "cohort": self.cohort}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Profile":
        samples = [s for s in (d.get("samples") or []) if isinstance(s, list) and s][:MAX_SAMPLES]
        if not samples:
            raise ValueError("a voice print needs samples")
        return cls(samples, float(d.get("threshold") or FLOOR), fixed=min(int(d.get("fixed") or len(samples)), len(samples)),
                   genuine=d.get("genuine"), impostors=d.get("impostors"), cohort=d.get("cohort"))


def enrol(embeddings: Sequence[Sequence[float]], cohort: Sequence[Sequence[float]],
          before: Optional[Profile] = None) -> Profile:
    """A voice print from new enrolment samples; ``before`` (an earlier enrolment of the same person)
    keeps its enrolment samples and "someone else" scores, so recording again adds to it."""
    old = before.samples[: before.fixed] if before else []
    samples = (list(old) + [list(e) for e in embeddings])[-(MAX_SAMPLES - LEARN_KEEP):]
    p = Profile(samples, impostors=before.impostors if before else None)
    p.calibrate(cohort)
    return p


# ── Who is it ────────────────────────────────────────────────────────────────────────────────

ME, OTHER, UNSURE = "known", "someone_else", "not_sure"


def identify(prints: Mapping[str, Profile], e: Optional[Sequence[float]], speech: float) -> Tuple[str, Optional[str], float]:
    """``(state, username, score)``: ``known`` (that person), ``not_sure`` or ``someone_else``.
    ``username`` is the closest person even when not sure (for the log and Settings, never acted on)."""
    if not prints or e is None:
        return UNSURE, None, 0.0
    scored = sorted(((p.score(e), p.threshold, u) for u, p in prints.items()), reverse=True)
    best, thr, who = scored[0]
    if speech < MIN_HEARD_S:
        return UNSURE, who, best
    if best >= thr:
        rival = next((s for s in scored[1:] if s[0] >= s[1]), None)
        if rival is None or best - rival[0] >= APART:
            return ME, who, best
        return UNSURE, who, best
    if best >= thr - NOT_SURE:
        return UNSURE, who, best
    return OTHER, who, best
