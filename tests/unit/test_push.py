"""F37: the dashboard as the Matrix push gateway for its web app (gateway/push.py)."""
import asyncio
import json

import jwt
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from gateway import push


def phone():
    """A browser's push subscription keys: (private key, p256dh, auth)."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key, push.b64u(push.raw_public(key.public_key())), push.b64u(b"0123456789abcdef")


def decrypt(body: bytes, key, auth: str) -> bytes:
    """The browser's side of RFC 8291, written out from the RFC (not from push.py)."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public = body[21:21 + idlen]
    assert rs == 4096 and idlen == 65
    ua_public = push.raw_public(key.public_key())
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    h = lambda s, i, info, n: HKDF(algorithm=hashes.SHA256(), length=n, salt=s, info=info).derive(i)
    ikm = h(push.unb64u(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek, nonce = h(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16), h(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_a_push_decrypts_on_the_phone():
    key, p256dh, auth = phone()
    body = push.encrypt(b'{"title":"Greg\'s agent"}', p256dh, auth)
    assert decrypt(body, key, auth) == b'{"title":"Greg\'s agent"}'
    assert push.encrypt(b"x", p256dh, auth) != push.encrypt(b"x", p256dh, auth)      # fresh salt and key each time
    with pytest.raises(ValueError):
        push.encrypt(b"x" * 5000, p256dh, auth)


def test_vapid_is_signed_for_the_push_service_and_names_no_one(tmp_path):
    store = push.Store(tmp_path)
    key = store.vapid_key()
    h = push.vapid_headers("https://fcm.googleapis.com/fcm/send/abc", key, now=1_000_000)
    t, k = h["Authorization"].removeprefix("vapid t=").split(", k=")
    assert k == store.public_key()
    claims = jwt.decode(t, key.public_key(), algorithms=["ES256"], audience="https://fcm.googleapis.com",
                        options={"verify_exp": False})
    assert claims["sub"] == push.VAPID_SUBJECT and "@logos.invalid" in claims["sub"] and claims["exp"] == 1_000_000 + 43200


def test_the_key_is_made_once_and_kept_private(tmp_path):
    a = push.Store(tmp_path).public_key()
    assert push.Store(tmp_path).public_key() == a
    assert (tmp_path / "push-vapid.pem").stat().st_mode & 0o777 == 0o600


def test_subscriptions(tmp_path):
    s = push.Store(tmp_path)
    k1 = s.add("greg", "https://push.example/1", "p", "a", "Pixel")
    k2 = s.add("greg", "https://push.example/1", "p", "a", "Pixel")          # the same phone again: one entry
    assert s.get(k1) is None and s.get(k2)["device"] == "Pixel"
    assert not s.remove(k2, "bere") and s.remove(k2, "greg") and s.get(k2) is None


def test_what_a_notification_says():
    dm = push.message_for({"event_id": "$e", "room_id": "!r", "sender": "@greg-agent:x", "sender_display_name": "Greg's agent",
                           "content": {"body": "SECRET"}, "counts": {"unread": 1}})
    assert dm == {"title": "Greg's agent", "body": "New message", "room": "!r", "link": "#/chat/room/!r", "unread": 1}
    group = push.message_for({"event_id": "$e", "room_id": "!g", "room_name": "Household Group", "sender_display_name": "Béré",
                              "counts": {"unread": 3}})
    assert group["title"] == "Household Group" and group["body"] == "Béré: 3 new messages"
    assert "SECRET" not in json.dumps(dm)
    assert push.message_for({"room_id": "!r", "counts": {"unread": 0}}) == {"clear": "!r", "unread": 0}


def test_only_the_docker_bridge_and_this_machine_may_post():
    assert push.trusted("172.17.0.3") and push.trusted("127.0.0.1") and push.trusted("::1")
    assert not push.trusted("192.0.2.50") and not push.trusted("100.64.1.2") and not push.trusted("") and not push.trusted(None)


@pytest.fixture
def app(tmp_path, monkeypatch):
    sent = []

    async def fake_send(session, sub, message, key):
        sent.append((sub["pushkey"], message))
        return 410 if sub["device"] == "gone" else 201
    monkeypatch.setattr(push, "send", fake_send)
    a = web.Application()
    push.register(a, tmp_path)
    a["sent"] = sent
    return a


def post(app, body):
    async def run():
        async with TestClient(TestServer(app)) as client:
            r = await client.post(push.NOTIFY_PATH, json=body)
            return r.status, await r.json()
    return asyncio.run(run())


def test_notify_sends_to_each_known_phone_and_rejects_the_rest(app):
    store = app["push_store"]
    good = store.add("greg", "https://push.example/1", "p", "a", "Pixel")
    gone = store.add("greg", "https://push.example/2", "p", "a", "gone")
    status, out = post(app, {"notification": {
        "event_id": "$e", "room_id": "!r", "sender_display_name": "Greg's agent", "counts": {"unread": 1},
        "devices": [{"app_id": "logos.web", "pushkey": good}, {"app_id": "logos.web", "pushkey": gone},
                    {"app_id": "logos.web", "pushkey": "unknown"}, {"app_id": "im.vector.app", "pushkey": "element"}]}})
    assert status == 200
    assert sorted(out["rejected"]) == sorted([gone, "unknown"])
    assert [k for k, _ in app["sent"]] == [good, gone]
    assert app["sent"][0][1]["title"] == "Greg's agent"
    assert store.get(gone) is None and store.get(good)


def test_notify_from_elsewhere_is_refused(app, monkeypatch):
    monkeypatch.setattr(push, "trusted", lambda remote: False)
    status, _ = post(app, {"notification": {"devices": []}})
    assert status == 403 and app["sent"] == []


def test_a_read_room_sends_nothing(app):
    store = app["push_store"]
    good = store.add("greg", "https://push.example/1", "p", "a", "Pixel")
    status, out = post(app, {"notification": {"room_id": "!r", "counts": {"unread": 0},
                                              "devices": [{"app_id": "logos.web", "pushkey": good}]}})
    assert status == 200 and out == {"rejected": []} and app["sent"] == []
