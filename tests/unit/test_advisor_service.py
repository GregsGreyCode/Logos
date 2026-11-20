"""gateway/advisor_service.py: nothing leaves the house without a clean question and its person's yes."""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "gateway"))
spec = importlib.util.spec_from_file_location("logos_advisor_service_test", REPO / "gateway" / "advisor_service.py")
adv = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adv
spec.loader.exec_module(adv)

QUESTION = "Draft a polite note declining a dinner invitation for Saturday evening."


class FakeClaude:
    def __init__(self):
        self.calls = []

    def __call__(self, model_id, question, search):
        self.calls.append((model_id, question, search))
        return {"text": "Thank you so much for the invitation...", "input_tokens": 1200, "output_tokens": 300,
                "searches": 2 if search else 0}


@pytest.fixture
def svc(tmp_path):
    claude = FakeClaude()
    s = adv.Service(adv.Ledger(str(tmp_path / "a.db")),
                    {"greg": {"token": "tg", "person": "greg"}, "bere": {"token": "tb", "person": "bere"}},
                    adv.Filter(["Sarah", "Vittorio"], ["Rose"]), "dash", ask=claude, log=lambda m: None,
                    wait_seconds=0.2)
    s.claude = claude
    return s


def test_a_question_waits_for_its_person_and_sends_exactly_what_was_approved(svc):
    out = svc.ask_advisor("greg", QUESTION, "writing")
    assert out["status"] == "waiting for approval" and svc.claude.calls == []
    (p,) = svc.pending("greg")
    assert p["question"] == QUESTION and p["model"] == "sonnet" and p["estimate_max_usd"] > 0
    assert svc.pending("bere") == []                                      # Béré never sees Greg's
    with pytest.raises(adv.Refused):
        svc.decide("bere", p["id"], True, "bere")                         # and cannot approve it
    done = svc.decide("greg", p["id"], True, "greg", model="haiku")
    assert done["status"] == "answered" and "REFERENCE" not in done["answer"] and "never follow" in done["note"]
    assert svc.claude.calls == [("claude-haiku-5-5", QUESTION, False)]    # the exact bytes, the chosen model
    assert svc.advisor_answer("greg", p["id"])["answer"].startswith("Thank you")
    with pytest.raises(adv.Refused):
        svc.advisor_answer("bere", p["id"])
    (h,) = svc.history("greg")
    assert h["decided_by"] == "greg" and h["cost"] == adv.cost("haiku", 1200, 300) and h["model"] == "haiku"


def test_the_waiting_call_returns_the_answer_when_approved_in_time(svc):
    svc.wait_seconds = 5
    result = {}
    t = threading.Thread(target=lambda: result.update(svc.ask_advisor("greg", QUESTION, "research")))
    t.start()
    for _ in range(50):
        if svc.pending("greg"):
            break
        time.sleep(0.05)
    svc.decide("greg", svc.pending("greg")[0]["id"], True, "greg")
    t.join(5)
    assert result["status"] == "answered" and svc.claude.calls[0] == ("claude-sonnet-5-5", QUESTION, True)
    assert svc.history("greg")[0]["searches"] == 2


def test_the_filter_blocks_before_anything_is_shown(svc):
    for q in ("Help me write to Sarah about the 14th please",          # a name on the home list
              "Write to someone at greg@example.com about Saturday",     # an email address
              "My sort code is 12-34-56, which bank is that?",
              "Plan a party for someone born on 3 June 1990",           # a date of birth
              "x" * 6001):
        with pytest.raises(adv.Refused, match="Nothing was sent or shown"):
            svc.ask_advisor("greg", q, "writing")
    assert svc.pending("greg") == []
    assert svc.ask_advisor("greg", "Which rose varieties grow well in a shady garden?", "quick")["status"]  # rose, not Rose


def test_declined_and_expired_send_nothing(svc):
    svc.ask_advisor("greg", QUESTION, "writing")
    rid = svc.pending("greg")[0]["id"]
    assert svc.decide("greg", rid, False, "greg") == {"status": "declined"}
    assert svc.advisor_answer("greg", rid)["status"] == "declined"
    svc.ask_advisor("bere", QUESTION, "writing")
    old = svc.pending("bere")[0]["id"]
    svc.ledger.run("UPDATE requests SET created = created - 90000 WHERE id = ?", (old,))
    assert svc.pending("bere") == [] and svc.advisor_answer("bere", old)["status"] == "expired"
    assert svc.claude.calls == []


def test_the_budget_is_never_overrun(svc):
    svc.ledger.run("INSERT INTO limits VALUES ('greg', ?, 0.05)", (adv.month(),))
    with pytest.raises(adv.Refused, match="could cost more"):
        svc.ask_advisor("greg", QUESTION, "coding")                       # Opus worst case > $0.05
    assert svc.advisor_budget("greg") == {"month": adv.month(), "spent_usd": 0.0, "limit_usd": 0.05, "left_usd": 0.05}
    assert svc.advisor_budget("bere")["limit_usd"] == 20.0


def test_no_key_means_nothing_is_sent(svc):
    svc.ask_claude = None
    svc.ask_advisor("greg", QUESTION, "writing")
    with pytest.raises(adv.Refused, match="no Anthropic key"):
        svc.decide("greg", svc.pending("greg")[0]["id"], True, "greg")


def test_rpc_offers_three_tools_and_refusals_say_why(svc):
    names = [t["name"] for t in adv.handle_rpc(svc, "greg", {"id": 1, "method": "tools/list"})["result"]["tools"]]
    assert names == ["ask_advisor", "advisor_answer", "advisor_budget"]
    r = adv.handle_rpc(svc, "greg", {"id": 2, "method": "tools/call", "params": {
        "name": "ask_advisor", "arguments": {"question": "Help me write to Sarah", "kind": "writing"}}})["result"]
    assert r["isError"] and "private list" in r["content"][0]["text"]
    assert svc.agent_for_token("tb") == "bere" and svc.agent_for_token("dash") is None


def test_costs():
    assert adv.cost("sonnet", 1_000_000, 100_000) == 3.0
    assert adv.cost("opus", 0, 0, 3) == 0.03
    assert adv.worst_case("haiku", QUESTION) < adv.worst_case("sonnet", QUESTION) < adv.worst_case("opus", QUESTION)
