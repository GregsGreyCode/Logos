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

    def __call__(self, model_id, question, search, cap=None):
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
    assert svc.advisor_budget("greg") == {"month": adv.month(), "spent_usd": 0.0, "limit_usd": 0.05, "held_usd": 0, "left_usd": 0.05}
    assert svc.advisor_budget("bere")["limit_usd"] == 20.0


def test_no_key_means_nothing_is_sent(svc):
    svc.ask_claude = None
    svc.ask_advisor("greg", QUESTION, "writing")
    with pytest.raises(adv.Refused, match="no Anthropic key"):
        svc.decide("greg", svc.pending("greg")[0]["id"], True, "greg")


def test_rpc_offers_four_tools_and_refusals_say_why(svc):
    names = [t["name"] for t in adv.handle_rpc(svc, "greg", {"id": 1, "method": "tools/list"})["result"]["tools"]]
    assert names == ["ask_advisor", "advisor_answer", "advisor_budget", "request_more_budget"]
    r = adv.handle_rpc(svc, "greg", {"id": 2, "method": "tools/call", "params": {
        "name": "ask_advisor", "arguments": {"question": "Help me write to Sarah", "kind": "writing"}}})["result"]
    assert r["isError"] and "private list" in r["content"][0]["text"]
    assert svc.agent_for_token("tb") == "bere" and svc.agent_for_token("dash") is None


def test_costs():
    assert adv.cost("sonnet", 1_000_000, 100_000) == 3.0
    assert adv.cost("opus", 0, 0, 3) == 0.03
    assert adv.worst_case("haiku", QUESTION) < adv.worst_case("sonnet", QUESTION) < adv.worst_case("opus", QUESTION)


# ── A2: approving from Matrix; A3: budget requests and the admin ───────────────

class FakeMatrix:
    def __init__(self):
        self.rooms = {"greg": {"room": "!g", "mxid": "@greg:home"}, "bere": {"room": "!b", "mxid": "@bere:home"}}
        self.sent = []

    def send(self, room, text):
        self.sent.append((room, text))


@pytest.fixture
def mx(svc):
    svc.matrix, svc.admin = FakeMatrix(), "greg"
    return svc


def test_each_preview_is_posted_once_in_its_persons_room(mx):
    mx.ask_advisor("bere", QUESTION, "writing")
    mx.post_updates()
    mx.post_updates()
    previews = [t for r, t in mx.matrix.sent if "wants to ask Claude" in t]
    assert len(previews) == 1 and mx.matrix.sent[0][0] == "!b"
    assert "> " + QUESTION in previews[0] and "Reply send, no" in previews[0] and "$20.00 left" in previews[0]


def test_only_the_person_can_approve_in_their_room(mx):
    mx.ask_advisor("bere", QUESTION, "writing")
    assert mx.on_message("bere", "@bere-agent:home", "send") is None          # her agent: ignored
    assert mx.on_message("bere", "@greg:home", "send") is None                # Greg in her room: ignored
    assert mx.claude.calls == [] and len(mx.pending("bere")) == 1
    assert mx.on_message("bere", "@bere:home", "Send Opus") == "Sending to Claude..."
    for _ in range(50):
        if mx.claude.calls:
            break
        time.sleep(0.05)
    assert mx.claude.calls == [("claude-opus-5-5", QUESTION, False)]
    assert mx.on_message("bere", "@bere:home", "send") == "Nothing is waiting for you."
    assert mx.on_message("bere", "@bere:home", "thanks!") is None              # ordinary chat: quiet


def test_several_waiting_need_the_id_and_no_declines(mx):
    mx.ask_advisor("greg", QUESTION, "writing")
    mx.ask_advisor("greg", "Explain how a combi boiler differs from a system boiler.", "quick")
    assert "reply with the request id" in mx.on_message("greg", "@greg:home", "no")
    rid = mx.pending("greg")[1]["id"]
    assert mx.on_message("greg", "@greg:home", f"no {rid}") == "Declined. Nothing was sent."
    assert [p["id"] for p in mx.pending("greg")] != [] and rid not in [p["id"] for p in mx.pending("greg")]


def test_a_budget_request_reaches_the_admin_who_alone_can_change_it(mx):
    out = mx.request_more_budget("bere", "planning the honeymoon")
    assert out["status"] == "asked" and mx.request_more_budget("bere", "again")["status"] == "already asked"
    mx.post_updates()
    (ask,) = [t for r, t in mx.matrix.sent if r == "!g" and "asks for more" in t]
    assert "planning the honeymoon" in ask
    assert mx.on_message("bere", "@bere:home", "raise 10") is None             # Béré cannot raise her own
    with pytest.raises(adv.Refused):
        mx.change_budget("bere", "bere", "raise", 10)
    assert mx.on_message("greg", "@greg:home", "raise 10") == "bere's advisor budget: raised by $10 to $30."
    assert mx.budget("bere")["limit_usd"] == 30.0 and mx.budget_requests("bere") == []
    assert mx.on_message("greg", "@greg:home", "raise") == "Say whose budget, e.g. raise 10 for bere."
    assert mx.on_message("greg", "@greg:home", "no for bere").endswith("declined.")
    assert mx.budget("bere")["limit_usd"] == 30.0
    assert [r["change"] for r in mx.budget_log("bere") if r["status"] == "change"][-1] == "raised by $10 to $30"


def test_reset_makes_the_default_available_again(mx):
    mx.ledger.run("INSERT INTO requests (id, agent, person, kind, model, question, hash, status, created, month, cost) "
                  "VALUES ('x1', 'greg', 'greg', 'quick', 'haiku', 'q', 'h', 'answered', ?, ?, 19.5)", (time.time(), adv.month()))
    assert mx.budget("greg")["left_usd"] == 0.5
    mx.change_budget("greg", "greg", "reset")
    assert mx.budget("greg")["left_usd"] == 20.0


def test_research_is_priced_for_every_round_the_search_loop_can_run():
    one = adv.round_worst("opus", QUESTION, True)
    assert adv.worst_case("opus", QUESTION, "research") == pytest.approx(one * adv.ROUNDS)
    assert adv.worst_case("opus", QUESTION, "writing") == adv.round_worst("opus", QUESTION, False)
    assert adv.worst_case("haiku", QUESTION, "research") == adv.worst_case("haiku", QUESTION, "writing")


def test_a_request_being_sent_holds_its_worst_case_so_a_second_approval_cannot_overspend(svc):
    release, started = threading.Event(), threading.Event()

    def slow(model_id, question, search, cap=None):
        started.set()
        release.wait(5)
        return {"text": "ok", "input_tokens": 10, "output_tokens": 10, "searches": 0}
    svc.ask_claude = slow
    svc.ask_advisor("greg", QUESTION, "coding")
    svc.ask_advisor("greg", QUESTION + " Keep it short.", "coding")
    first, second = svc.pending("greg")
    left = first["estimate_max_usd"] + second["estimate_max_usd"] / 2      # room for one, not two
    svc.ledger.run("INSERT OR REPLACE INTO limits VALUES (?, ?, ?)", ("greg", adv.month(), left))
    assert svc.decide("greg", first["id"], True, "greg", background=True)["status"] == "sending"
    assert started.wait(5)
    assert svc.budget("greg")["held_usd"] == pytest.approx(first["estimate_max_usd"], abs=0.01)
    with pytest.raises(adv.Refused):
        svc.decide("greg", second["id"], True, "greg")
    release.set()


def test_a_send_that_fails_part_way_keeps_what_was_billed(svc):
    def flaky(model_id, question, search, cap=None):
        raise adv.AskFailed("OverloadedError: busy", 4000, 16000, 5)
    svc.ask_claude = flaky
    svc.ask_advisor("greg", QUESTION, "research", "opus")
    (p,) = svc.pending("greg")
    assert svc.decide("greg", p["id"], True, "greg")["status"] == "failed"
    (h,) = svc.history("greg")
    assert h["cost"] == adv.cost("opus", 4000, 16000, 5) and svc.budget("greg")["spent_usd"] > 0


def test_a_request_left_sending_by_a_restart_is_failed_and_counted_at_its_most(svc, tmp_path):
    svc.ask_advisor("greg", QUESTION, "writing")
    (p,) = svc.pending("greg")
    svc.ledger.run("UPDATE requests SET status = 'sending' WHERE id = ?", (p["id"],))
    again = adv.Service(svc.ledger, svc.agents, svc.filter, "dash", ask=svc.claude, log=lambda m: None)
    r = again.advisor_answer("greg", p["id"])
    assert r["status"] == "failed" and "restarted" in r["note"]
    assert svc.ledger.get(p["id"])["cost"] == pytest.approx(adv.worst_case("sonnet", QUESTION, "writing"))


def test_a_preview_whose_send_failed_is_posted_on_the_next_round(mx):
    real = mx.matrix.send
    mx.matrix.send = lambda room, text: (_ for _ in ()).throw(OSError("homeserver restarting"))
    mx.ask_advisor("bere", QUESTION, "writing")
    mx.post_updates()
    assert mx.matrix.sent == []
    mx.matrix.send = real
    mx.post_updates()
    mx.post_updates()
    assert len([t for r, t in mx.matrix.sent if "wants to ask Claude" in t]) == 1


def test_a_bare_no_from_the_admin_answers_the_budget_request(mx):
    mx.request_more_budget("bere", "planning the honeymoon")
    assert mx.on_message("greg", "@greg:home", "no") == "bere's advisor budget: declined."
    mx.request_more_budget("bere", "once more")
    mx.ask_advisor("greg", QUESTION, "writing")
    assert "Both a question and a budget request" in mx.on_message("greg", "@greg:home", "no")
    assert len(mx.pending("greg")) == 1 and mx.budget_requests("bere")              # neither was touched
