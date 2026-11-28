"""How well the voice prints work at the house speaker, measured on public recordings (docs/voice.md, VO.3).

Not part of the image and never run by Logos. It needs ffmpeg, numpy, LibriSpeech test-clean (its 40 speakers
are not among the reference voices, which come from dev-clean) and a throwaway ``logos-stt`` container whose
HTTP door is on 127.0.0.1 (no household audio is used, and nothing is kept but embeddings of LibriSpeech):

    docker run -d --name stt-eval --read-only --tmpfs /tmp -p 127.0.0.1:8111:8101 \\
      -v $PWD/gateway/stt.py:/app/stt.py:ro -v $PWD/gateway/voiceprint.py:/app/voiceprint.py:ro \\
      -e STT_TOKEN=testtoken -e HOME=/tmp logos-stt:2 python /app/stt.py
    python infra/stt/eval_voiceprints.py collect  .../LibriSpeech/test-clean OUT     # emb.json and stream.json
    python infra/stt/eval_voiceprints.py compare  OUT [old voiceprint.py]           # before/after
    python infra/stt/eval_voiceprints.py simulate OUT                                # LEARNING -> ACTIVE

"Speaker-like" is ffmpeg's band-limit, echo and pink room noise ("far"; "far2" a harder room; "near"),
a different recipe from the reference voices' (gateway/stt.py ``speaker_like``).
"""
import glob
import importlib.util
import json
import os
import random
import statistics
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from gateway import voice_id as vi  # noqa: E402
from gateway import voiceprint as vp  # noqa: E402

URL, TOKEN = os.environ.get("STT_URL", "http://127.0.0.1:8111"), os.environ.get("STT_TOKEN", "testtoken")
ROOMS = {
    "near": ("highpass=f=150,lowpass=f=5000,aecho=0.8:0.9:20:0.15", 0.005),
    "far": ("highpass=f=200,lowpass=f=3800,aecho=0.8:0.6:30|60:0.3|0.2", 0.012),
    "far2": ("highpass=f=250,lowpass=f=3400,aecho=0.8:0.7:40|90|150:0.4|0.3|0.2", 0.02),
}


# ── embeddings from the container ───────────────────────────────────────────────────────────

def _http(method, path, data=None):
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def _wav(files, out, start=0.0, dur=None, room=None, rng=random):
    with open(out + ".txt", "w") as f:
        f.writelines(f"file '{p}'\n" for p in files)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", out + ".txt"]
    cmd += ["-ss", str(start)] if start else []
    cmd += ["-t", str(dur)] if dur else []
    if room:
        flt, amp = ROOMS[room]
        cmd += ["-filter_complex", f"[0:a]{flt}[s];anoisesrc=color=pink:amplitude={amp}:sample_rate=16000:"
                f"seed={rng.randrange(1 << 30)}[n];[s][n]amix=inputs=2:duration=first:normalize=0"]
    subprocess.run(cmd + ["-ar", "16000", "-ac", "1", out], check=True)
    return out


def _embed(path):
    with open(path, "rb") as f:
        return _http("POST", "/embed", f.read())


def _dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                                capture_output=True, text=True).stdout)


def collect(root, out):
    """emb.json: per speaker an enrolment (13 s clean), 3 learned and 8 test commands (1.2, 2, 3 s) per room;
    stream.json: every other utterance as 1 to 3.5 s commands at a random distance, in a random order."""
    tmp = os.path.join(out, "tmp")
    os.makedirs(tmp, exist_ok=True)
    rng = random.Random(7)
    data = {"cohort": {"phone": _http("GET", "/cohort")["cohort"],
                       "speaker": _http("GET", "/cohort?channel=speaker")["cohort"]}, "speakers": {}}
    stream = {}
    for s in sorted(os.listdir(root)):
        u = sorted(glob.glob(f"{root}/{s}/*/*.flac"))
        acc, total = [], 0.0
        for p in u:
            acc.append(p)
            total += _dur(p)
            if total >= 13:
                break
        e = _embed(_wav(acc, f"{tmp}/enrol.wav", dur=14))
        if not e["pieces"]:
            continue
        rest = [p for p in u[len(acc):] if _dur(p) >= 3.6]
        rng.shuffle(rest)
        row = {"enrol": e["pieces"] + [e["embedding"]], "learn": {}, "test": {}}
        for room in ("far", "far2"):
            row["learn"][room] = [[g["embedding"], g["quality"]["speech"]] for g in
                                  (_embed(_wav([p], f"{tmp}/x.wav", 0.3, 2.0, room, rng)) for p in rest[:3]) if g["embedding"]]
            row["test"][room] = {str(d): [[g["embedding"], g["quality"]["speech"]] for g in
                                          (_embed(_wav([p], f"{tmp}/x.wav", 0.3, d, room, rng)) for p in rest[3:11])
                                          if g["embedding"]] for d in (1.2, 2.0, 3.0)}
        data["speakers"][s] = row
        cmds = []
        for p in u[len(acc):]:
            length = _dur(p)
            for _ in range(2 if length > 8 else 1):
                d = round(rng.uniform(1.0, 3.5), 1)
                if length >= d + 0.6:
                    room = rng.choice(["near", "far", "far", "far2"])
                    g = _embed(_wav([p], f"{tmp}/y.wav", round(rng.uniform(0.3, length - d - 0.2), 2), d, room, rng))
                    if g["embedding"]:
                        cmds.append({"e": g["embedding"], "q": g["quality"], "room": room, "d": d})
        rng.shuffle(cmds)
        stream[s] = cmds
        print(s, len(cmds), file=sys.stderr, flush=True)
    json.dump(data, open(os.path.join(out, "emb.json"), "w"))
    json.dump(stream, open(os.path.join(out, "stream.json"), "w"))


# ── faster maths (the same) ─────────────────────────────────────────────────────────────────

def _fast(*mods):
    import numpy as np

    def score(samples, e):
        a = np.asarray(samples)
        a = a / np.linalg.norm(a, axis=1, keepdims=True)
        e = np.asarray(e) / np.linalg.norm(e)
        c = a.sum(0)
        c = c / np.linalg.norm(c)
        return float((c @ e + np.sort(a @ e)[::-1][:3].mean()) / 2)
    for m in mods:
        m.cosine = lambda a, b: float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    vp.Profile._score = staticmethod(score)
    vp.scorer = lambda samples: (lambda e: score(samples, e))
    return score


def compare(out, before=None):
    """Own commands recognised / "not sure" / "someone else" (and under 1.5 s), others accepted; 0, 1, 3 learned."""
    d = json.load(open(os.path.join(out, "emb.json")))
    S, cp, cs = d["speakers"], d["cohort"]["phone"], d["cohort"]["speaker"]
    old = None
    if before:
        spec = importlib.util.spec_from_file_location("old_vp", before)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
    score = _fast(vp, *([old] if old else []))
    if old:
        old.Profile.score = lambda self, e: score(self.samples, e)
    for room in ("far", "far2"):
        for k in (0, 1, 3):
            for which in (["before"] if old else []) + ["now"]:
                own, short, fa, m = {vp.ME: 0, vp.UNSURE: 0, vp.OTHER: 0}, {vp.ME: 0, vp.UNSURE: 0, vp.OTHER: 0}, 0, 0
                for s in sorted(S):
                    if which == "before":
                        p = old.enrol(S[s]["enrol"], cp)
                        for e, _ in S[s]["learn"][room][:k]:
                            p.add_learned(e)
                        ident = lambda e, sp, p=p, s=s: old.identify({s: p}, e, sp)[0]
                    else:
                        p = vp.enrol(S[s]["enrol"], cp, speaker_cohort=cs)
                        for i, (e, sp) in enumerate(S[s]["learn"][room][:k]):
                            p.add_speaker(e, {"speech": sp}, "me", heard=str(i), now=1_700_000_000 + i * 86400)
                        p.calibrate_speaker(cs)
                        p.state = vp.ACTIVE
                        ident = lambda e, sp, p=p, s=s: vp.identify({s: p}, e, sp, vp.SPEAKER)[0]
                    for rows in S[s]["test"][room].values():
                        for e, sp in rows:
                            st = ident(e, sp)
                            own[st] += 1
                            short[st] += sp < vp.SHORT_S
                    for o in sorted(S):
                        if o != s:
                            for rows in S[o]["test"][room].values():
                                for e, sp in rows:
                                    m += 1
                                    fa += ident(e, sp) == vp.ME
                n, ns = sum(own.values()), sum(short.values()) or 1
                print(f"{room:4s} learned {k} {which:6s}: own recognised {100 * own[vp.ME] / n:.0f}%, not sure "
                      f"{100 * own[vp.UNSURE] / n:.0f}%, someone else {100 * own[vp.OTHER] / n:.0f}% | under 1.5 s: "
                      f"someone else {100 * short[vp.OTHER] / ns:.0f}% | others accepted {fa} of {m}", flush=True)


def simulate(out, guest_every=5, per_day=15):
    """Households of two; each person calls their own agent by name (labelled), per_day commands a day; a guest
    says the first person's agent's name after every guest_every of their commands."""
    _fast(vp)
    emb, stream = json.load(open(os.path.join(out, "emb.json"))), json.load(open(os.path.join(out, "stream.json")))
    cs, cp = emb["cohort"]["speaker"], emb["cohort"]["phone"]
    names = sorted(stream)
    rng = random.Random(3)
    res = {"at": [], "never": 0, "guest_in": 0, "guest": 0, "held_own": 0, "own": 0, "fa": 0, "fa_tries": 0, "rec": [0, 0]}
    uid = 0
    for a, b in zip(names[0::2], names[1::2]):
        prints = {u: vp.enrol(emb["speakers"][u]["enrol"], cp, speaker_cohort=cs) for u in (a, b)}
        seq = []
        for i in range(max(len(stream[a]), len(stream[b]))):
            seq += [(u, u, stream[u][i]) for u in (a, b) if i < len(stream[u])]
            if i % guest_every == guest_every - 1:
                seq.append(("guest", a, rng.choice(stream[rng.choice([g for g in names if g not in (a, b)])])))
        count, went = {a: 0, b: 0}, {}
        for who, label, c in seq:
            uid += 1
            count[who] = count.get(who, 0) + 1
            at = 1_700_000_000 + (count[label] // per_day) * 86400 + uid * 10
            row = {"id": uid, "at": at, "embedding": c["e"], **c["q"]}
            for u, p in prints.items():
                if p.state == vp.ACTIVE:
                    st, w, _ = vp.identify(prints, c["e"], c["q"]["speech"], vp.SPEAKER)
                    if who == u:
                        res["rec"][0] += st == vp.ME and w == u
                        res["rec"][1] += 1
                    else:
                        res["fa"] += st == vp.ME and w == u
                        res["fa_tries"] += 1
            got = vi.collect_sync(prints, row, label, None, cs, now=at)
            kept = prints[label].has_heard(vi.hkey(row))
            if who == "guest":
                res["guest"] += 1
                res["guest_in"] += kept
            else:
                res["own"] += 1
                res["held_own"] += not kept and prints[label].state == vp.LEARNING
            for u, st in got["states"].items():
                if st == vp.ACTIVE:
                    went.setdefault(u, count[u])
        for u in (a, b):
            if u in went:
                res["at"].append(went[u])
            else:
                res["never"] += 1
    at = res["at"]
    print(f"ACTIVE: {len(at)} of {len(at) + res['never']} people" + (
        f"; their own commands needed: median {statistics.median(at)}, {min(at)} to {max(at)}" if at else ""))
    print(f"guest's commands let in: {res['guest_in']} of {res['guest']}; own commands held back while learning: "
          f"{res['held_own']} of {res['own']}")
    print(f"once ACTIVE: own recognised {res['rec'][0]} of {res['rec'][1]}; anyone else taken for them {res['fa']} of {res['fa_tries']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "collect":
        collect(sys.argv[2], sys.argv[3])
    elif cmd == "compare":
        compare(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "simulate":
        simulate(sys.argv[2], int(os.environ.get("GUEST_EVERY", "5")), int(os.environ.get("PER_DAY", "15")))
    else:
        sys.exit(__doc__)
