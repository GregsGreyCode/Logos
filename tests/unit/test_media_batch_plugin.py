"""logos-media-batch: photos sent together, with their message, reach the agent as one turn."""
import asyncio
import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "infra" / "agent-plugins" / "logos-media-batch" / "__init__.py"


def load():
    spec = importlib.util.spec_from_file_location("logos_media_batch_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MT(Enum):
    TEXT = "text"; PHOTO = "photo"; VOICE = "voice"; DOCUMENT = "document"


class Ev:
    def __init__(self, kind, text="", media=None):
        self.message_type, self.text, self.media_urls = kind, text, list(media or [])


class Adapter:
    """Enough of hermes' adapter: a merge with a quiet period, as in gateway/platforms/base.py."""
    def __init__(self):
        self._text_batch_delay_seconds = 0.6
        self.turns, self.pending, self.timer = [], None, None

    async def handle_message(self, event):
        self.turns.append(event)

    def _text_batch_delay_for(self, pending):
        return self._text_batch_delay_seconds

    def _enqueue_text_event(self, event):
        if self.pending is None:
            self.pending = event
        else:
            if event.text:
                self.pending.text = f"{self.pending.text}\n{event.text}" if self.pending.text else event.text
            self.pending.media_urls.extend(event.media_urls)


def setup(monkeypatch):
    fake = types.ModuleType("hermes_plugins.platforms__matrix.adapter")
    class MatrixAdapter(Adapter):
        pass
    fake.MatrixAdapter = MatrixAdapter
    monkeypatch.setitem(sys.modules, "hermes_plugins.platforms__matrix.adapter", fake)
    mod = load()
    mod.register(None)
    return MatrixAdapter()


def test_photos_and_their_message_are_one_turn(monkeypatch):
    a = setup(monkeypatch)
    async def send():
        await a.handle_message(Ev(MT.PHOTO, "IMG_1.jpg", ["/c/1.jpg"]))
        await a.handle_message(Ev(MT.PHOTO, "IMG_2.png", ["/c/2.png"]))
        a._enqueue_text_event(Ev(MT.TEXT, "This is the bin rota, remember it"))     # the message, as hermes enqueues text
    asyncio.run(send())
    assert a.turns == []                                        # nothing dispatched yet: one batch waits
    batch = a.pending
    assert batch.media_urls == ["/c/1.jpg", "/c/2.png"]
    assert batch.text == "This is the bin rota, remember it"   # file names are not passed on as words
    asyncio.run(a.handle_message(batch))                         # the flush dispatches it: straight through
    assert a.turns == [batch]


def test_a_photo_with_a_real_caption_keeps_it(monkeypatch):
    a = setup(monkeypatch)
    asyncio.run(a.handle_message(Ev(MT.PHOTO, "our new sofa", ["/c/1.jpg"])))
    assert a.pending.text == "our new sofa"


def test_voice_notes_and_text_are_left_alone(monkeypatch):
    a = setup(monkeypatch)
    asyncio.run(a.handle_message(Ev(MT.VOICE, "", ["/c/v.ogg"])))
    asyncio.run(a.handle_message(Ev(MT.TEXT, "hi")))
    assert [e.message_type for e in a.turns] == [MT.VOICE, MT.TEXT] and a.pending is None


def test_the_wait_is_longer_with_photos_in_it(monkeypatch):
    a = setup(monkeypatch)
    assert a._text_batch_delay_for(Ev(MT.TEXT, "x")) == 0.6
    assert a._text_batch_delay_for(Ev(MT.PHOTO, "", ["/c/1.jpg"])) == 2.5


def test_patched_once(monkeypatch):
    a = setup(monkeypatch)
    mod = load()
    mod.patch(type(a))
    asyncio.run(a.handle_message(Ev(MT.PHOTO, "", ["/c/1.jpg"])))
    assert a.turns == [] and a.pending is not None
