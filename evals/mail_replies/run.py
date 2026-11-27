"""E2 eval: does the agent tell which emails need a reply, read the conversation before drafting, and
write a draft that answers what was asked, in its person's voice, without sending or inventing?

Each case in cases.json is one small made-up mailbox, served by the real mail service
(gateway/mail_service.py, filled by fixture.py; no mail server). The model gets the agent's rules
(souls/general/soul.md, souls/_shared/judgement.md and mail.md) and the mail tools, and is asked
the case's question; the tool calls really run. Then:

  decision          yes: a draft;  no: no draft;  ask: no draft, and a question to the person
  read the conversation first   (yes) mail_thread was called before the draft
  key points        (yes) each one the draft must cover
  not: ...          things a good answer never says (per case)
  never says it sent

It calls a model, so it is NOT run by the unit tests (they check the grading and the tool loop with a
scripted model). Run it when the AI computer is free:

  scripts/logos-eval --only mail                        the bench agent's model (agents/bench.yaml)
  python evals/mail_replies/run.py --base-url http://<host>:1234/v1 --model <name> [--only <case>]

Results go to evals/results/ (not committed).
"""
import argparse
import json
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fixture  # noqa: E402

REPO = fixture.REPO
MAX_STEPS = 8
DRAFTED = re.compile(r"draft (reply|response|answer)|^>\s*\S", re.I | re.M)
SENT = re.compile(r"\b(I('ve| have) sent|I sent (it|the|your)|I('ll| will) send (it|the|this)|(it|the email|your reply) "
                  r"(has been|was) sent|sending it now)\b", re.I)


def system_prompt() -> str:
    parts = [REPO / "souls/general/soul.md", REPO / "souls/_shared/judgement.md", REPO / "souls/_shared/mail.md"]
    text = "\n\n".join(p.read_text().strip() for p in parts if p.exists())
    return text + f"\n\nToday is {time.strftime('%A %d %B %Y')}. Your person is Alex."


def tools() -> list:
    ms = fixture.mail_service()
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["inputSchema"]}} for t in ms.TOOLS]


def openai_model(base_url: str, model: str, api_key: str = "", timeout: int = 600):
    """A chat function for an OpenAI-compatible server: (messages, tools) -> the assistant message."""
    def chat(messages, tool_list):
        body = json.dumps({"model": model, "messages": messages, "tools": tool_list, "temperature": 0.2}).encode()
        req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers={
            "Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["choices"][0]["message"]
    return chat


def grade(case: dict, answer: str, called: list) -> list:
    """The checks for one case, from the agent's final answer and the tools it called, in order."""
    name, want = case["name"], case["expect"]["draft"]
    drafted = bool(DRAFTED.search(answer or ""))
    out = []

    def check(what, ok, detail=""):
        out.append({"check": f"{name}: {what}", "ok": bool(ok), "detail": detail[:160]})

    if want == "yes":
        check("decision: a draft", drafted, answer[:160])
        check("read the conversation first", "mail_thread" in called, ", ".join(called))
        for kp in case.get("key_points") or []:
            check(f"covers: {kp['point']}", any(re.search(p, answer, re.I) for p in kp["any"]), answer[-160:])
    elif want == "no":
        check("decision: no draft", not drafted, answer[:160])
    else:
        check("decision: asks its person, no draft", not drafted and "?" in (answer or ""), answer[:160])
    for bad in case.get("must_not") or []:
        hit = next((m.group(0) for p in bad["any"] for m in [re.search(p, answer or "", re.I)] if m), "")
        check(f"not: {bad['point']}", not hit, hit)
    check("never says it sent", not SENT.search(answer or ""), (SENT.search(answer or "") or [""])[0])
    return out


def run_case(case: dict, data: dict, chat, state_dir) -> dict:
    """One case: the conversation with the model, its tool calls served by the case's mailbox."""
    ms = fixture.mail_service()
    svc, _ = fixture.build(case, data, state_dir)
    messages = [{"role": "system", "content": system_prompt()},
                {"role": "user", "content": case.get("prompt") or data["prompt"]}]
    called, answer, t = [], "", time.time()
    for step in range(MAX_STEPS):
        reply = chat(messages, tools())
        calls = reply.get("tool_calls") or []
        messages.append({k: v for k, v in reply.items() if k in ("role", "content", "tool_calls")})
        if not calls:
            answer = reply.get("content") or ""
            break
        for c in calls:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            called.append(fn.get("name") or "")
            res = ms.handle_rpc(svc, fixture.AGENT, {"jsonrpc": "2.0", "id": step, "method": "tools/call",
                                                     "params": {"name": fn.get("name"), "arguments": args}})
            text = (res.get("result") or {}).get("content", [{}])[0].get("text") if "result" in res else \
                json.dumps(res.get("error"))
            messages.append({"role": "tool", "tool_call_id": c.get("id") or "", "content": text or ""})
    return {"case": case["name"], "tools": called, "answer": answer, "secs": round(time.time() - t, 1),
            "checks": grade(case, answer, called)}


def run_all(chat, only: str = "") -> list:
    data = fixture.load()
    results = []
    for case in data["cases"]:
        if only and case["name"] != only:
            continue
        with tempfile.TemporaryDirectory() as d:
            try:
                results.append(run_case(case, data, chat, d))
            except Exception as e:
                results.append({"case": case["name"], "tools": [], "answer": "", "checks": [
                    {"check": f"{case['name']}: ran", "ok": False, "detail": f"{type(e).__name__}: {e}"[:160]}]})
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="an OpenAI-compatible server, e.g. http://<host>:1234/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--only", default="", help="one case by name")
    args = ap.parse_args()
    results = run_all(openai_model(args.base_url, args.model, args.api_key), args.only)
    passed = total = 0
    for r in results:
        for c in r["checks"]:
            total, passed = total + 1, passed + c["ok"]
            print(f"  {'pass' if c['ok'] else 'FAIL'}  {c['check']}" + ("" if c["ok"] else f"   <- {c['detail']}"))
    print(f"\nmail replies: {passed}/{total} with {args.model}")
    out = REPO / "evals" / "results" / (time.strftime("%Y-%m-%d_%H%M") + "_mail.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"model": args.model, "results": results}, indent=1, ensure_ascii=False))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
