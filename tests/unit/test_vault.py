"""F47 phase 1: what people send an agent is kept (logos-vault), told to the agent, and written up (vault_digest)."""
import asyncio
import importlib.util
import json
import os
import stat
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(path, name, monkeypatch, home):
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    return tmp_path


@pytest.fixture
def vault(home, monkeypatch):
    return load("infra/agent-plugins/logos-vault/__init__.py", "logos_vault_test", monkeypatch, home)


def cached(home, name, data=b"hello"):
    p = home / "cache" / name
    p.write_bytes(data)
    return str(p)


def event(kind, paths, mimes=None, text="", who="Greg", room="!r:x", mid="$e1"):
    return types.SimpleNamespace(message_type=types.SimpleNamespace(value=kind), media_urls=paths, media_types=mimes or ["application/pdf"] * len(paths),
                                 text=text, message_id=mid, source=types.SimpleNamespace(user_name=who, user_id="@greg:x", chat_id=room))


# ── keeping ──────────────────────────────────────────────────────────────────────────────────

def test_a_file_is_kept_privately_with_who_when_and_where(vault, home):
    p = cached(home, "doc_3fa9c1d2e4b7_Revised Bin Rota - May.pdf", b"%PDF rota")
    m = vault.keep_file(p, mime="application/pdf", who="Greg", room="!r:x", caption="the bin rota", message_id="$e1", now=1_795_400_000, home=str(home))
    assert m["name"] == "Revised Bin Rota - May.pdf" and m["from"] == "Greg" and m["state"] == "new" and m["caption"] == "the bin rota"
    kept = home / "vault" / m["path"]
    assert kept.read_bytes() == b"%PDF rota" and m["path"].startswith("files/2026/11/") and m["id"] == m["sha256"][:12]
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    meta = json.loads((home / "vault" / "meta" / f"{m['id']}.json").read_text())
    assert meta["room"] == "!r:x" and meta["seen"][0]["from"] == "Greg"
    assert stat.S_IMODE((home / "vault" / "meta" / f"{m['id']}.json").stat().st_mode) == 0o600


def test_the_same_file_sent_again_is_kept_once_and_remembered(vault, home):
    a = vault.keep_file(cached(home, "a.pdf", b"same"), mime="application/pdf", who="Greg", room="!r", caption="", message_id="$1", home=str(home))
    b = vault.keep_file(cached(home, "b.pdf", b"same"), mime="application/pdf", who="Béré", room="!r", caption="again", message_id="$2", home=str(home))
    c = vault.keep_file(cached(home, "c.pdf", b"same"), mime="application/pdf", who="Béré", room="!r", caption="again", message_id="$2", home=str(home))   # the same message again
    assert a["id"] == b["id"] and b["again"] and "again" not in a
    assert len(list((home / "vault" / "files").rglob("*.pdf"))) == 1
    seen = json.loads((home / "vault" / "meta" / f"{a['id']}.json").read_text())["seen"]
    assert [s["from"] for s in seen] == ["Greg", "Béré"]


def test_what_is_missing_empty_or_too_big_is_not_kept(vault, home, monkeypatch):
    assert vault.keep_file(str(home / "nope"), mime="", who="", room="", caption="", message_id="", home=str(home)) is None
    assert vault.keep_file(cached(home, "empty", b""), mime="", who="", room="", caption="", message_id="", home=str(home)) is None
    monkeypatch.setattr(vault, "MAX_FILE", 3)
    assert vault.keep_file(cached(home, "big", b"12345"), mime="", who="", room="", caption="", message_id="", home=str(home)) is None


def test_only_photos_and_documents_are_kept_never_voice_or_audio(vault, home):
    p = cached(home, "x.bin", b"data")
    for kind, n in (("photo", 1), ("document", 1), ("voice", 0), ("audio", 0), ("video", 0), ("text", 0)):
        assert len(vault.keep_event(event(kind, [p]), home=str(home))) == n, kind
    assert vault.keep_event(event("photo", [str(home / "gone.jpg")]), home=str(home)) == []
    two = vault.keep_event(event("photo", [cached(home, "1.jpg", b"one"), cached(home, "2.jpg", b"two")], ["image/jpeg"] * 2), home=str(home))
    assert [m["mime"] for m in two] == ["image/jpeg", "image/jpeg"] and len({m["id"] for m in two}) == 2


# ── telling the agent ────────────────────────────────────────────────────────────────────────

def test_the_agent_is_told_once_that_the_file_was_kept_and_where_to_look(vault, home, monkeypatch):
    monkeypatch.setattr(vault, "HOME", str(home))
    seen = []

    class Adapter:
        async def handle_message(self, event, *a, **k):
            seen.append(event.text)
    vault.patch(Adapter)
    vault.patch(Adapter)                                           # patching twice does nothing more
    ev = event("document", [cached(home, "doc_ab12cd34ef56_Rota.pdf", b"rota")], text="the rota")
    asyncio.run(Adapter().handle_message(ev))
    asyncio.run(Adapter().handle_message(ev))                      # the batch re-dispatches the merged event: no second note
    assert len(seen) == 2 and seen[0].startswith("the rota\n\n[Kept in your vault:") and '"Rota.pdf"' in seen[0] and "brain/files/" in seen[0]
    assert seen[1].count(vault.NOTE_MARK) == 1
    assert (home / "vault" / "meta").is_dir()


def test_a_failure_to_keep_never_blocks_the_message(vault, home, monkeypatch):
    monkeypatch.setattr(vault, "keep_event", lambda e, home="": (_ for _ in ()).throw(OSError("disk full")))
    got = []

    class Adapter:
        async def handle_message(self, event, *a, **k):
            got.append(event.text)
    vault.patch(Adapter)
    asyncio.run(Adapter().handle_message(event("photo", ["/x"], text="hi")))
    assert got == ["hi"]


# ── the voice tag ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tag(home, monkeypatch):
    return load("infra/agent-plugins/logos-voice-tag/__init__.py", "logos_voice_tag_test", monkeypatch, home)


def test_a_voice_message_reaches_the_agent_as_a_quoted_voice_note(tag):
    v = {"seconds": 4.2, "wave": [10, 20]}
    assert tag.tagged({"body": "Is the dentist on Thursday?", "org.logos.voice": v})["body"] == '"Is the dentist on Thursday?"'
    group = {"body": "Household agent: when are the bins out?", "org.logos.voice": v, "m.mentions": {"user_ids": ["@household-agent:x"]}}
    assert tag.tagged(group)["body"] == 'Household agent: "when are the bins out?"'
    typed = {"body": "Is the dentist on Thursday?"}
    assert tag.tagged(typed) is typed                                         # a typed message is left alone
    quoted = {"body": '"already"', "org.logos.voice": v}
    assert tag.tagged(quoted) is quoted
    for odd in ({"body": "", "org.logos.voice": v}, {"body": "x", "org.logos.voice": {"seconds": 0}}, {"body": "x", "org.logos.voice": "yes"}, None, "text"):
        assert tag.tagged(odd) is odd
    assert tag.tagged({"body": "hi", "org.logos.voice": v, "msgtype": "m.text"})["msgtype"] == "m.text"       # nothing else changes


def test_the_voice_tag_is_applied_in_front_of_hermes(tag):
    got = []

    class Adapter:
        async def _handle_text_message(self, room_id, sender, event_id, event_ts, source_content, relates_to):
            got.append(source_content["body"])
    tag.patch(Adapter)
    tag.patch(Adapter)
    asyncio.run(Adapter()._handle_text_message("!r", "@g", "$e", 1.0, {"body": "hello", "org.logos.voice": {"seconds": 2}}, {}))
    asyncio.run(Adapter()._handle_text_message("!r", "@g", "$e", 1.0, {"body": "hello"}, {}))
    assert got == ['"hello"', "hello"]


# ── the notes ────────────────────────────────────────────────────────────────────────────────

class Model(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        answer = self.server.answer(body)
        if isinstance(answer, int):
            self.send_response(answer); self.send_header("Content-Length", "0"); self.end_headers(); return
        data = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def model():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    srv.requests = []
    srv.answer = lambda body: "TITLE: Bin rota\n| Flat | Week |\n|---|---|\n| 5-23 | 22 Nov |"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def digest(home, monkeypatch, vault, model):
    mod = load("infra/agent-jobs/vault_digest.py", "vault_digest_test", monkeypatch, home)
    conf = (f"http://127.0.0.1:{model.server_address[1]}/v1", "lm-studio/qwen/qwen3.6-35b-a3b", "sk-test")
    return mod, conf


def kept(vault, home, name, data, mime, **kw):
    return vault.keep_file(cached(home, name, data), mime=mime, who=kw.get("who", "Greg"), room="!r", caption=kw.get("caption", ""),
                           message_id=kw.get("mid", "$" + name), now=1_795_400_000, home=str(home))


def test_a_photo_becomes_a_note_with_its_table_kept_and_is_asked_for_behind_the_chats(digest, vault, home, model):
    mod, conf = digest
    m = kept(vault, home, "rota.png", b"\x89PNG fake", "image/png", caption="the bin rota")
    assert mod.run(conf) == [m["id"]]
    note = (home / json.loads((home / "vault" / "meta" / f"{m['id']}.json").read_text())["note"]).read_text()
    assert note.startswith("# Bin rota") and "| 5-23 | 22 Nov |" in note and "Sent by Greg on 2026-11-23" in note
    assert f"`{m['id']}`" in note and "Sent with the words: the bin rota" in note
    req = model.requests[0]
    assert req["model"] == "lm-studio/qwen/qwen3.6-35b-a3b" and "scheduled cron job" in req["messages"][0]["content"]
    parts = req["messages"][1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/") and "the bin rota" in parts[0]["text"]
    meta = json.loads((home / "vault" / "meta" / f"{m['id']}.json").read_text())
    assert meta["state"] == "done" and meta["title"] == "Bin rota"
    assert mod.run(conf) == []                                                # done once; not again


def test_a_text_file_needs_no_model_and_a_long_one_is_summarised(digest, vault, home, model):
    mod, conf = digest
    short = kept(vault, home, "list.txt", b"milk\neggs\nbread " * 10, "text/plain")
    assert mod.run(conf) == [short["id"]] and model.requests == []
    note = (home / "brain" / "files").glob("*list*.md").__next__().read_text()
    assert "## Contents" in note and "milk" in note and "## Summary" not in note
    model.answer = lambda body: "TITLE: Tenancy letter\n- rent rises in January"
    long = kept(vault, home, "letter.md", ("Dear tenant, the rent will rise. " * 80).encode(), "text/markdown")
    assert mod.run(conf) == [long["id"]]
    note = (home / "brain" / "files").glob("*letter*.md").__next__().read_text()
    assert note.startswith("# Tenancy letter") and "## Summary" in note and "rent rises in January" in note and "## Contents" in note


def test_a_model_that_is_off_counts_nothing_and_a_file_that_keeps_failing_gets_an_honest_note(digest, vault, home, model, capsys):
    mod, conf = digest
    m = kept(vault, home, "rota.png", b"\x89PNG fake", "image/png")
    assert mod.run(("http://127.0.0.1:9/v1", conf[1], "k")) == []
    meta = json.loads((home / "vault" / "meta" / f"{m['id']}.json").read_text())
    assert meta["state"] == "new" and meta["tries"] == 0 and "did not answer" in capsys.readouterr().err
    model.answer = lambda body: 503
    assert mod.run(conf) == [] and json.loads((home / "vault" / "meta" / f"{m['id']}.json").read_text())["tries"] == 0
    model.answer = lambda body: ""                                            # it answers, with nothing
    for _ in range(3):
        mod.run(conf)
    meta = json.loads((home / "vault" / "meta" / f"{m['id']}.json").read_text())
    assert meta["state"] == "failed" and meta["tries"] == 3
    assert "could not be read after several tries" in (home / meta["note"]).read_text()
    assert mod.run(conf) == []


def test_two_files_a_run_at_most_and_the_oldest_first(digest, vault, home, model):
    mod, conf = digest
    ids = []
    for i in range(3):
        ids.append(vault.keep_file(cached(home, f"f{i}.txt", f"file {i} ".encode() * 10), mime="text/plain", who="Greg", room="!r", caption="",
                                   message_id=f"$m{i}", now=1_795_400_000 + i, home=str(home))["id"])
    assert mod.run(conf) == ids[:2] and mod.run(conf) == ids[2:]


def test_what_cannot_be_read_is_still_noted_with_its_details(digest, vault, home):
    mod, conf = digest
    m = kept(vault, home, "thing.xyz", b"\x00\x01binary", "application/octet-stream", who="Béré")
    assert mod.run(conf) == [m["id"]]
    note = next((home / "brain" / "files").glob("*thing*.md")).read_text()
    assert "kept but not read yet" in note and "Sent by Béré" in note
    assert mod.slug("Revised Bin Rota - May.pdf") == "revised-bin-rota-may" and mod.human(69 * 1024) == "69 KB" and mod.human(500) == "500 bytes" and mod.human(3 * 1048576) == "3.0 MB"


def test_the_brain_index_lists_the_notes_of_files():
    spec = importlib.util.spec_from_file_location("brain_index_t", ROOT / "infra/agent-jobs/brain_index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "files" in mod.FOLDERS


# ── the helper Python (PDF, Word, resizing) ─────────────────────────────────────────────────

def fake_helper(tmp_path, fail=False):
    """Stands in for hermes's own Python: called as <python> -c <code> <kind> <path> <side>."""
    script = tmp_path / "helper.sh"
    script.write_text("#!/bin/sh\n" + ("echo oops >&2; exit 1\n" if fail else
        'case "$3" in\n'
        '  pdf) echo \'{"text": "Flat 5-23: week of 22 Nov. Flat 7-18: week of 29 Nov. This rota is long enough to be a real document."}\' ;;\n'
        '  docx) echo \'{"text": "A Word letter about the lease, long enough to be read as a real document."}\' ;;\n'
        '  image) echo \'{"jpeg": "SlBFR0RBVEE="}\' ;;\n'
        'esac\n'))
    script.chmod(0o755)
    return str(script)


def test_pdfs_and_word_files_are_read_by_the_helper_and_images_are_resized_by_it(digest, vault, home, model, tmp_path):
    mod, conf = digest
    mod.VENV_PYTHON = fake_helper(tmp_path)
    pdf = kept(vault, home, "rota.pdf", b"%PDF-1.4 fake", "application/pdf")
    doc = kept(vault, home, "lease.docx", b"PK fake", "application/vnd.openxmlformats")
    img = kept(vault, home, "photo.jpg", b"\xff\xd8 big original", "image/jpeg")
    assert sorted(mod.run(conf, limit=3)) == sorted([pdf["id"], doc["id"], img["id"]])
    notes = {p.name.split("-", 3)[3][:4]: p.read_text() for p in (home / "brain" / "files").glob("*.md")}
    assert "Flat 5-23: week of 22 Nov" in next(t for t in notes.values() if '"rota.pdf"' in t)
    assert "A Word letter about the lease" in next(t for t in notes.values() if '"lease.docx"' in t)
    url = model.requests[-1]["messages"][1]["content"][1]["image_url"]["url"]
    assert url == "data:image/jpeg;base64,SlBFR0RBVEE="                      # what the helper made, not the original


def test_without_the_helper_a_pdf_is_noted_as_not_read_yet_and_an_image_goes_as_it_is(digest, vault, home, model, tmp_path):
    mod, conf = digest
    mod.VENV_PYTHON = str(tmp_path / "absent")
    pdf = kept(vault, home, "rota.pdf", b"%PDF-1.4 fake", "application/pdf")
    img = kept(vault, home, "photo.png", b"\x89PNG original bytes", "image/png")
    assert sorted(mod.run(conf)) == sorted([pdf["id"], img["id"]])
    assert "kept but not read yet" in next((home / "brain" / "files").glob("*rota*.md")).read_text()
    url = model.requests[-1]["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,") and url.endswith("UE5HIG9yaWdpbmFsIGJ5dGVz".replace("UE5HIG9yaWdpbmFsIGJ5dGVz", __import__("base64").b64encode(b"\x89PNG original bytes").decode()))
    mod.VENV_PYTHON = fake_helper(tmp_path, fail=True)                        # the helper breaks: same, no crash
    img2 = kept(vault, home, "photo2.png", b"\x89PNG two", "image/png")
    assert mod.run(conf) == [img2["id"]]
