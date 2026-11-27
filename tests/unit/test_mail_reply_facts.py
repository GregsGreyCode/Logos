"""E2: Logos' facts about whether an email needs a reply (gateway/mail_service.py reply_facts), checked
over the eval threads (evals/mail_replies/cases.json) without a model, and the eval's grading."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fx = _load("logos_mail_eval_fixture", REPO / "evals" / "mail_replies" / "fixture.py")
run = _load("logos_mail_eval_run", REPO / "evals" / "mail_replies" / "run.py")
ms = fx.mail_service()
DATA = fx.load()
CASES = {c["name"]: c for c in DATA["cases"]}


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_eval_thread_gets_the_expected_suggestion(name, tmp_path):
    case = CASES[name]
    f = fx.facts(case, DATA, tmp_path)
    assert f["suggestion"] == case["expect"]["suggestion"], f


@pytest.mark.parametrize("name", sorted(CASES))
def test_needs_reply_lists_exactly_what_may_need_an_answer(name, tmp_path):
    case = CASES[name]
    svc, mid = fx.build(case, DATA, tmp_path)
    out = svc.mail_needs_reply(fx.AGENT, 60)
    listed = {e["id"]: e["suggestion"] for e in out["emails"]}
    want = case["expect"]["suggestion"]
    if want == "no":
        assert mid not in listed and sum(out["left_out"].values()) >= 1
    else:
        assert listed.get(mid) == want


def test_the_eval_set_is_balanced_and_anonymous():
    kinds = [c["expect"]["draft"] for c in DATA["cases"]]
    assert kinds.count("yes") >= 4 and kinds.count("no") >= 4 and kinds.count("ask") >= 3
    text = json.dumps(DATA)
    assert all(a.split("@")[1].endswith(".example") for a in __import__("re").findall(r"[\w.+-]+@[\w.-]+", text))
    for c in DATA["cases"]:
        if c["expect"]["draft"] == "yes":
            assert c.get("key_points"), c["name"]


def test_the_facts():
    own = {"alex@mail.example"}
    msg = {"box": "b", "thread": "t", "date": ms.iso(ms.now()), "from_addr": "sam@x.example",
           "to_addrs": "alex@mail.example", "cc_addrs": "", "subject": "Hi", "headers": {},
           "body": "Hi Alex,\nHow are you?\nLet me know if you have any questions.\nCan you send the file by Friday?\n"
                   "Thanks\n\nOn Mon, 1 Dec 2026, Alex <alex@mail.example>\nwrote:\n> Could you call me?"}
    f = ms.reply_facts(msg, own, [])
    assert f["asks"] == ["Can you send the file by Friday?"]          # not small talk, filler or the quote
    assert f["sender"] == "person" and f["you_are"] == "to" and f["suggestion"] == "maybe"   # never written to
    sent = [{"box": "b", "thread": "old", "date": ms.iso(ms.now()), "subject": "x", "to_addrs": "sam@x.example", "cc_addrs": ""}]
    assert ms.reply_facts(msg, own, sent)["suggestion"] == "reply"
    assert ms.reply_facts({**msg, "to_addrs": "a@x.example", "cc_addrs": "Alex@Mail.example"}, own, sent)["you_are"] == "cc"
    assert ms.reply_facts({**msg, "headers": {"unsubscribe": 1}}, own, sent)["sender"] == "newsletter"
    assert ms.reply_facts({**msg, "from_addr": "noreply@shop.example"}, own, sent)["sender"] == "no-reply"
    replied = sent + [{"box": "b", "thread": "t", "date": "2099-01-01T00:00:00Z", "subject": "Re: Hi",
                       "to_addrs": "sam@x.example", "cc_addrs": ""}]
    assert ms.reply_facts(msg, own, replied)["your_person_replied_since"] is True
    assert ms.reply_facts({**msg, "date": "2026-01-01T00:00:00Z"}, own, sent)["age_days"] > 30
    assert ms.new_text("Sure!\n\n________________________________\nFrom: Sam\nSent: Monday\nCan you?") == "Sure!"
    assert ms.new_text("Merci !\n\nLe lun. 1 déc. 2026, Claire a écrit :\n> Tu viens ?") == "Merci !"


def test_headers_are_read_the_same_from_gmail_and_outlook():
    flags = ms.header_flags({"list-unsubscribe": "<mailto:x>", "precedence": "bulk", "x-sg-eid": "1",
                             "auto-submitted": "no", "list-id": "<a.b>"})
    assert flags == {"unsubscribe": 1, "precedence": "bulk", "bulk_sender": 1, "list": 1}
    assert ms.header_flags({"auto-submitted": "auto-replied"}) == {"auto": "auto-replied"}


# ── the model eval's grading (run.py), with made-up answers ──────────────────

GOOD = ("Sam wants to know whether to book the Italian place.\n\n**Draft reply to Sam** (not sent), in your Gmail\n"
        "> Hi Sam,\n> Yes please, book the Italian place on Mill Street for 7:30.\n> Cheers,\n> Alex\n\n"
        "Copy it into your mail app if it works, tell me what to change, or say skip.")


def test_grading_a_good_draft():
    case = CASES["friend_asks_to_book"]
    checks = run.grade(case, GOOD, ["mail_needs_reply", "mail_thread"])
    assert all(c["ok"] for c in checks), checks


def test_grading_catches_the_usual_mistakes():
    case = CASES["friend_asks_to_book"]
    bad = run.grade(case, GOOD.replace("Yes please", "Tuesday or Thursday? Anyway") + "\nI've sent it.", ["mail_needs_reply"])
    failed = {c["check"] for c in bad if not c["ok"]}
    assert {"friend_asks_to_book: read the conversation first", "friend_asks_to_book: never says it sent",
            "friend_asks_to_book: not: asks again which day (Thursday is settled)"} <= failed
    no = run.grade(CASES["newsletter_asks_a_question"], GOOD, ["mail_needs_reply"])
    assert not no[0]["ok"]                                             # a draft where none was wanted
    ask = run.grade(CASES["stranger_asks_about_the_bike"], "Jordan asks if the bike is still for sale. Want a reply?", [])
    assert ask[0]["ok"]


def test_the_tool_loop_runs_against_the_fixture(tmp_path):
    """run.py's conversation with the model, with a scripted model: it calls the real tools."""
    case = CASES["landlord_changes_the_plan"]
    seen = []

    def model(messages, tools):
        seen.append(messages[-1])
        n = sum(1 for m in messages if m["role"] == "tool")
        if n == 0:
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "mail_needs_reply", "arguments": "{}"}}]}
        if n == 1:
            mid = json.loads(messages[-1]["content"])["emails"][0]["id"]
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "2", "type": "function", "function": {"name": "mail_thread", "arguments": json.dumps({"id": mid})}}]}
        thread = json.loads(messages[-1]["content"])
        assert [m["from"] for m in thread["messages"]][:2] == ["Martin Cole <martin@lettings.example>", "your person"]
        return {"role": "assistant", "content": "**Draft reply to Martin** (not sent)\n> Hi Martin,\n> Wednesday at 9 "
                "works. I'll send a photo of the leak today.\n> Thanks,\n> Alex"}

    result = run.run_case(case, DATA, model, tmp_path)
    assert result["tools"] == ["mail_needs_reply", "mail_thread"]
    assert all(c["ok"] for c in result["checks"]), result["checks"]
    assert "Drafting replies" in run.system_prompt() and len(seen) == 3
