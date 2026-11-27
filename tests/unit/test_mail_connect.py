"""F40: connecting your own mailbox from Settings › Email (gateway/mail_connect.py)."""
import asyncio
import imaplib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import mail_connect as mc


def test_files_are_private_and_keep_exclusions(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    box = mc.save_box(env, "greg", {"kind": "gmail", "address": "g@x.example", "app_password": "a" * 16,
                                    "exclude": ["bank.example"]})
    p = tmp_path / "logos" / "mail" / "greg" / f"{box['id']}.json"
    assert p.stat().st_mode & 0o777 == 0o600 and p.parent.stat().st_mode & 0o777 == 0o700
    again = mc.save_box(env, "greg", {"kind": "gmail", "address": "G@x.example", "app_password": "b" * 16})
    assert again["id"] == box["id"]                                             # the same mailbox, a new login
    (only,) = mc.boxes(env, "greg")
    assert only["exclude"] == ["bank.example"] and only["app_password"] == "b" * 16   # a new login keeps them
    assert mc.remove_box(env, "greg", box["id"]) and mc.boxes(env, "greg") == []
    assert not mc.remove_box(env, "greg", box["id"]) and not mc.remove_box(env, "greg", "../../x")


def test_outlook_app_id(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert mc.outlook_app(env) == ""
    with pytest.raises(mc.Refused):
        mc.set_outlook_app(env, "not-an-id")
    mc.set_outlook_app(env, "1A2B3C4D-1111-2222-3333-444455556666")
    assert mc.outlook_app(env) == "1a2b3c4d-1111-2222-3333-444455556666"


class FakeImap:
    good = ("g@x.example", "abcdefghijklmnop")

    def __init__(self, *a, **k): pass
    def login(self, user, pw):
        if (user, pw) != self.good:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    def select(self, box, readonly):
        assert readonly is True                                       # never changes anything
        return "OK", [b"42"]
    def logout(self): pass


def test_gmail_check():
    assert mc.gmail_check("g@x.example", "abcd efgh ijkl mnop", FakeImap) == 42
    for addr, pw in [("nope", "abcdefghijklmnop"), ("g@x.example", "short"), ("g@x.example", "abcdefghijklmnoq")]:
        with pytest.raises(mc.Refused):
            mc.gmail_check(addr, pw, FakeImap)


def test_outlook_wait():
    answers = iter([{"error": "authorization_pending"}, {"error": "slow_down"}, {"refresh_token": "r", "access_token": "a"}])
    clock = iter(range(0, 10000, 5))
    tok = mc.outlook_wait("cid", {"device_code": "d", "interval": 5, "expires_in": 900}, post=lambda u, f: next(answers),
                          sleep=lambda s: None, now=lambda: next(clock))
    assert tok["refresh_token"] == "r"
    with pytest.raises(mc.Refused, match="cancelled"):
        mc.outlook_wait("cid", {"device_code": "d"}, post=lambda u, f: {"error": "authorization_declined"},
                        sleep=lambda s: None, now=lambda: 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    who = {"username": "greg", "role": "admin"}
    restarts = []
    monkeypatch.setattr(mc, "_user", lambda r: dict(who))
    monkeypatch.setattr(mc, "_env", lambda r: env)
    monkeypatch.setattr(mc, "_runtime", lambda r: None)
    monkeypatch.setattr(mc, "_agent_for", lambda rt, p: f"{p}-agent")

    async def restart(request, person, fresh):
        restarts.append((person, fresh))
    monkeypatch.setattr(mc, "_restart", restart)

    def check(address, password):
        if password != "abcdefghijklmnop":
            raise mc.Refused("Gmail did not accept it.")
        return 42
    monkeypatch.setattr(mc, "gmail_check", check)

    def start(client_id):
        if client_id.startswith("9188"):
            raise mc.Refused(mc.ms_reason({"error_description": "AADSTS700016: Application with identifier ..."}))
        return {"device_code": "d"}
    monkeypatch.setattr(mc, "outlook_start", start)

    def make():                       # a fresh app for each request (each runs in its own event loop)
        app = web.Application()
        app.router.add_get("/m", mc.handle_get)
        app.router.add_post("/m/gmail", mc.handle_gmail)
        app.router.add_post("/m/exclude", mc.handle_exclude)
        app.router.add_put("/m/app", mc.handle_outlook_app)
        app.router.add_delete("/m", mc.handle_disconnect)
        return app
    return make, env, who, restarts


def call(make, method, path, body=None):
    async def go():
        async with TestClient(TestServer(make())) as c:
            r = await c.request(method, path, json=body)
            return r.status, await r.json()
    return asyncio.run(go())


def test_connect_gmail_then_status_never_returns_the_password(client):
    app, env, who, restarts = client
    assert call(app, "POST", "/m/gmail", {"address": "g@x.example", "app_password": "wrong"})[0] == 400
    assert mc.boxes(env, "greg") == [] and restarts == []
    s, out = call(app, "POST", "/m/gmail", {"address": "g@x.example", "app_password": "abcdefghijklmnop"})
    bid = out["id"]
    assert s == 200 and out == {"connected": True, "inbox": 42, "id": bid} and restarts == [(bid, False)]
    s, out = call(app, "GET", "/m")
    assert out["mailboxes"] == [{"id": bid, "kind": "gmail", "address": "g@x.example", "exclude": [], "health": None}]
    assert "abcdefghijklmnop" not in str(out)


def test_each_person_only_their_own(client):
    app, env, who, _ = client
    call(app, "POST", "/m/gmail", {"address": "g@x.example", "app_password": "abcdefghijklmnop"})
    bid = mc.boxes(env, "greg")[0]["id"]
    who["username"] = "bere"
    assert call(app, "GET", "/m")[1]["mailboxes"] == []
    assert call(app, "DELETE", "/m")[1] == {"disconnected": False}
    assert call(app, "DELETE", f"/m?box={bid}")[1] == {"disconnected": False}        # not hers
    assert call(app, "POST", "/m/exclude", {"box": bid, "sender": "x.example"})[0] == 409
    assert len(mc.boxes(env, "greg")) == 1


def test_exclude_and_disconnect(client):
    app, env, _, restarts = client
    assert call(app, "POST", "/m/exclude", {"sender": "bank.example"})[0] == 409            # nothing connected
    call(app, "POST", "/m/gmail", {"address": "g@x.example", "app_password": "abcdefghijklmnop"})
    bid = mc.boxes(env, "greg")[0]["id"]
    assert call(app, "POST", "/m/exclude", {"sender": "not a domain"})[0] == 400
    assert call(app, "POST", "/m/exclude", {"sender": "Bank.Example"})[1] == {"exclude": ["bank.example"], "id": bid}
    assert call(app, "POST", "/m/exclude", {"box": bid, "sender": "bank.example", "remove": True})[1] == {"exclude": [], "id": bid}
    assert call(app, "DELETE", "/m")[1] == {"disconnected": True}
    assert mc.boxes(env, "greg") == [] and restarts[-1] == (bid, True)


def test_several_mailboxes_each_with_its_own_private_senders(client):
    """F43: a second mailbox is added next to the first, not instead of it."""
    app, env, _, restarts = client
    first = call(app, "POST", "/m/gmail", {"address": "g@x.example", "app_password": "abcdefghijklmnop"})[1]["id"]
    second = call(app, "POST", "/m/gmail", {"address": "work@y.example", "app_password": "abcdefghijklmnop"})[1]["id"]
    mc.save_box(env, "greg", {"kind": "outlook", "address": "g@hotmail.example", "client_id": "c", "refresh_token": "r"})
    out = call(app, "GET", "/m")[1]
    assert [b["address"] for b in out["mailboxes"]] == ["g@x.example", "work@y.example", "g@hotmail.example"]
    assert call(app, "POST", "/m/exclude", {"sender": "bank.example"})[0] == 409        # which one?
    call(app, "POST", "/m/exclude", {"box": second, "sender": "bank.example"})
    by = {b["id"]: b for b in call(app, "GET", "/m")[1]["mailboxes"]}
    assert by[second]["exclude"] == ["bank.example"] and by[first]["exclude"] == []
    assert call(app, "DELETE", "/m")[1] == {"disconnected": False}                     # which one?
    assert call(app, "DELETE", f"/m?box={first}")[1] == {"disconnected": True}
    assert {b["address"] for b in mc.boxes(env, "greg")} == {"work@y.example", "g@hotmail.example"}
    assert restarts[-1] == (first, True)


def test_the_mailbox_from_before_f43_carries_over_without_reconnecting(client, tmp_path):
    app, env, _, _ = client
    old = tmp_path / "logos" / "mail" / "greg.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"kind": "gmail", "address": "g@x.example", "app_password": "abcdefghijklmnop", "exclude": ["bank.example"]}')
    (box,) = call(app, "GET", "/m")[1]["mailboxes"]
    assert box["address"] == "g@x.example" and box["exclude"] == ["bank.example"] and not old.exists()
    (kept,) = mc.boxes(env, "greg")
    assert kept["carried_over"] is True and kept["app_password"] == "abcdefghijklmnop"


def test_only_an_admin_sets_the_outlook_app(client):
    app, env, who, _ = client
    who["role"] = "user"
    assert call(app, "PUT", "/m/app", {"client_id": "1a2b3c4d-1111-2222-3333-444455556666"})[0] == 403
    who["role"] = "admin"
    assert call(app, "PUT", "/m/app", {"client_id": "1a2b3c4d-1111-2222-3333-444455556666"})[1] == {"outlook_ready": True}
    assert call(app, "GET", "/m")[1]["outlook_ready"] is True


def test_a_tenant_id_is_refused_in_plain_words(client):
    app, env, who, _ = client
    s, out = call(app, "PUT", "/m/app", {"client_id": "9188040d-6c67-4c5b-b112-36a304b66dad"})
    assert s == 400 and "Directory (tenant) ID" in out["message"] and mc.outlook_app(env) == ""


def test_microsoft_codes_in_plain_words():
    assert "public client flows" in mc.ms_reason({"error_description": "AADSTS7000218: The request body must contain..."})
    assert mc.ms_reason({"error": "weird", "error_description": "Odd thing.\r\nTrace ID: x"}) == "Microsoft said: Odd thing."
