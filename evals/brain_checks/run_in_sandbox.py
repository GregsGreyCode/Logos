"""Runs inside an agent sandbox. Tells the agent things without naming a file, then checks the
vault on disk and asks about them in fresh sessions. Prints one JSON list. First cut of S3.6."""
import json, re, sys, time, urllib.request, pathlib

KEY = dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l)["API_SERVER_KEY"]
BRAIN = pathlib.Path("/sandbox/brain")
NO_NOTE = re.compile(r"no (note|record)|don.t have|do not have|not (been )?(noted|recorded|saved)|nothing (in|about)|couldn.t find|could not find|haven.t (been told|noted)", re.I)


def ask(prompt, timeout=420):
    body = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:8642/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    t = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=timeout))
        return r["choices"][0]["message"].get("content") or "", round(time.time() - t, 1), None
    except Exception as e:
        return "", round(time.time() - t, 1), f"{type(e).__name__}: {str(e)[:120]}"


def text(sub):
    return "\n".join(p.read_text(errors="ignore") for p in sorted((BRAIN / sub).glob("*.md")))


def inbox_bullets():
    return len(re.findall(r"^- ", text("inbox"), re.M))


def page(sub, word):
    return "\n".join(p.read_text(errors="ignore") for p in (BRAIN / sub).glob("*.md")
                     if word.lower() in (p.name + p.read_text(errors="ignore")).lower())


J2, J3 = r"\b2(nd)? (of )?June|June 2(nd)?\b", r"\b3(rd)? (of )?June|June 3(rd)?\b"
state = {}
TASKS = [
    ("capture_person_fact", "Just so you know, Béré's birthday is on 2 June.",
     lambda r: re.search(J2, text("inbox"), re.I) is not None and re.search(J2, page("people", "béré") + page("people", "bere"), re.I) is not None),
    ("capture_decision", "We decided to go with British Gas for the boiler service. It's booked for Friday 20 November.",
     lambda r: "british gas" in text("inbox").lower() and "british gas" in (text("topics") + text("projects")).lower()),
    ("recall_new_session", "When is Béré's birthday?",
     lambda r: re.search(J2, r, re.I) is not None),
    ("recall_decision", "What did we decide about the boiler?",
     lambda r: "british gas" in r.lower()),
    ("update_not_duplicate", "Correction: Béré's birthday is actually 3 June, not 2 June.",
     lambda r: (lambda pg: re.search(J3, pg, re.I) is not None and re.search(J2, pg, re.I) is None)(page("people", "béré") + page("people", "bere"))),
    ("recall_after_update", "Remind me, what date is Béré's birthday?",
     lambda r: re.search(J3, r, re.I) is not None and re.search(J2, r, re.I) is None),
    ("unknown_says_so", "What's my sister's phone number?",
     lambda r: NO_NOTE.search(r) is not None and re.search(r"\d{5,}", r) is None),
    ("small_talk_no_note", "Morning! How's it going?",
     lambda r: inbox_bullets() == state["bullets_before_small_talk"]),
]

results = []
for name, prompt, check in TASKS:
    if name == "small_talk_no_note":
        state["bullets_before_small_talk"] = inbox_bullets()
    reply, secs, err = ask(prompt)
    ok = False if err else bool(check(reply))
    results.append({"task": name, "ok": ok, "secs": secs, "error": err, "reply": reply[:200].replace("\n", " ")})
files = sorted(str(p.relative_to(BRAIN)) for p in BRAIN.rglob("*.md"))
print(json.dumps({"results": results, "files": files, "inbox": text("inbox")[:900], "people": text("people")[:600]}))
