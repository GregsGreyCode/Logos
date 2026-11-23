"""logos-vault: what people send the agent is kept (F47, phase 1).

hermes downloads every photo and document into a cache and empties that cache after about a day, so a
rota sent on Monday was gone by Tuesday and the person had to send it again (Greg, 23 Nov). This copies each
photo and document into the agent's own vault the moment it arrives:

    /opt/data/vault/files/<yyyy>/<mm>/<id><ext>      the original, owner-only
    /opt/data/vault/meta/<id>.json                   who, when, where, caption, what has been done with it

The id is the start of the file's SHA-256, so the same file sent twice is kept once (the second sending is
added to ``seen``). The vault is in the agent's own home volume: private to that agent, never in its brain
(which Syncthing carries to phones), never shared with another agent. A background job
(infra/agent-jobs/vault_digest.py) reads each file and writes a note about it into the brain's files/ folder.

The agent is told, in that turn, that the file was kept, so it knows to look in brain/files/ before ever
asking for it again. Voice notes and audio are not kept (voice notes are never stored); videos are not kept
yet (large). Nothing is ever deleted here: retention comes with the Files screen (phase 3).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time

logger = logging.getLogger("logos.vault")
ADAPTER_MODULE_SUFFIX = "platforms__matrix.adapter"
HOME = os.environ.get("HERMES_HOME", "/opt/data")
KEEP_KINDS = {"photo", "document"}
MAX_FILE = 50 * 1024 * 1024
SEEN_MAX = 20
NOTE_MARK = "[Kept in your vault:"


def vault_dir(home: str = "") -> str:
    return os.path.join(home or HOME, "vault")


def _kind(event) -> str:
    t = getattr(event, "message_type", None)
    return str(getattr(t, "value", t) or "")


def clean_name(path: str) -> str:
    """The name the person sent: hermes prefixes a cached document with doc_<hash>_ and an image with img_<hash>."""
    name = os.path.basename(path)
    name = re.sub(r"^(?:doc|img|image|file)_[0-9a-f]{6,}_?", "", name) or os.path.basename(path)
    return re.sub(r"[^\w .,()+\-]", "_", name)[:120]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def keep_file(path: str, *, mime: str, who: str, room: str, caption: str, message_id: str,
              now: float = 0.0, home: str = "") -> dict | None:
    """Copy one file into the vault; returns its meta, or None when it is not kept (missing, too big)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= 0 or size > MAX_FILE:
        return None
    now = now or time.time()
    sha = _sha256(path)
    fid = sha[:12]
    base = vault_dir(home)
    meta_dir = os.path.join(base, "meta")
    os.makedirs(meta_dir, mode=0o700, exist_ok=True)
    meta_path = os.path.join(meta_dir, f"{fid}.json")
    seen = {"at": int(now), "from": who, "room": room, "caption": caption[:300], "message_id": message_id}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = None
        if meta:
            if not any(s.get("message_id") == message_id for s in meta.get("seen", [])):
                meta["seen"] = (meta.get("seen", []) + [seen])[-SEEN_MAX:]
                _write_json(meta_path, meta)
            meta["again"] = True
            return meta
    name = clean_name(path)
    ext = os.path.splitext(name)[1].lower()[:10] or os.path.splitext(path)[1].lower()[:10]
    day = time.strftime("%Y/%m", time.localtime(now))
    folder = os.path.join(base, "files", day)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    dest = os.path.join(folder, f"{fid}{ext}")
    if not os.path.exists(dest):
        tmp = dest + ".tmp"
        shutil.copyfile(path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    meta = {"id": fid, "sha256": sha, "name": name, "mime": mime or "", "size": size, "kept_at": int(now),
            "path": os.path.relpath(dest, base), "from": who, "room": room, "caption": caption[:300],
            "message_id": message_id, "seen": [seen], "state": "new", "tries": 0}
    _write_json(meta_path, meta)
    logger.info("vault: kept %s (%d bytes) as %s", name, size, fid)
    return meta


def keep_event(event, home: str = "") -> list:
    """Keep the photos and documents of one inbound event; the metas of those kept."""
    if _kind(event) not in KEEP_KINDS:
        return []
    src = getattr(event, "source", None)
    who = str(getattr(src, "user_name", "") or getattr(src, "user_id", "") or "")
    room = str(getattr(src, "chat_id", "") or "")
    caption = str(getattr(event, "text", "") or "")
    mids = list(getattr(event, "media_types", None) or [])
    out = []
    for i, path in enumerate(getattr(event, "media_urls", None) or []):
        if not isinstance(path, str) or not os.path.isfile(path):
            continue
        meta = keep_file(path, mime=str(mids[i] if i < len(mids) else ""), who=who, room=room, caption=caption,
                         message_id=str(getattr(event, "message_id", "") or ""), home=home)
        if meta:
            out.append(meta)
    return out


def note_for(metas: list) -> str:
    lines = [f'{NOTE_MARK} "{m["name"]}" (id {m["id"]}{", sent before" if m.get("again") else ""}). '
             "Kept for at least 90 days. A written note of it appears in brain/files/ within about ten minutes: "
             "look there before ever asking for it again.]" for m in metas]
    return "\n".join(lines)


def _find_adapter_class():
    for name, mod in list(sys.modules.items()):
        if name.endswith(ADAPTER_MODULE_SUFFIX) and hasattr(mod, "MatrixAdapter"):
            return mod.MatrixAdapter
    return None


def patch(cls) -> None:
    if getattr(cls, "_logos_vault", False) or not hasattr(cls, "handle_message"):
        return
    original = cls.handle_message

    async def handle_message(self, event, *args, **kwargs):
        try:
            metas = await asyncio.to_thread(keep_event, event)
            if metas and NOTE_MARK not in str(getattr(event, "text", "") or ""):
                event.text = (str(getattr(event, "text", "") or "").rstrip() + "\n\n" + note_for(metas)).strip()
        except Exception as e:                       # never in the way of a message
            logger.warning("vault: could not keep a file (%s)", type(e).__name__)
        return await original(self, event, *args, **kwargs)

    cls.handle_message = handle_message
    cls._logos_vault = True
    logger.info("Matrix: photos and documents sent to this agent are kept in its vault")


def _patch_when_loaded(timeout: float = 600.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        cls = _find_adapter_class()
        if cls is not None:
            patch(cls)
            return
        time.sleep(0.2)
    logger.error("Matrix: the adapter never loaded; files are not kept")


def register(ctx):
    cls = _find_adapter_class()
    if cls is not None:
        patch(cls)
    else:
        threading.Thread(target=_patch_when_loaded, daemon=True, name="logos-vault").start()
