#!/usr/bin/env python3
"""Write a note into the brain for each file in the vault (F47, phase 1). No model reads it live: this runs on a
timer, behind the people's own chats (its model requests say they are scheduled work).

The logos-vault plugin keeps every photo and document sent to the agent in /opt/data/vault (with a small
``meta/<id>.json``). This reads each one that has no note yet and writes ``brain/files/<date>-<name>-<id>.md``:

- a photo or a screenshot: the local vision model transcribes its text exactly (tables kept as tables) and says
  what it shows;
- a PDF with a text layer, a Word document, a text file: its text, with a short summary above it when it is long;
- anything else, or a scan with no text layer: just who sent it, when and what it is (a scan is read in a later
  phase, when it can be rendered to an image).

Two files per run at most (a big image takes minutes at the model's speed). A file that fails three times gets a
note saying so. If the model server is off, nothing is counted and the next run tries again. The original is
never touched. Prints nothing unless something is wrong. Standard library; python-docx and pypdf are used when
the agent's Python has them.
"""
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
VAULT = HOME / "vault"
META = VAULT / "meta"
NOTES = HOME / "brain" / "files"
MAX_PER_RUN = 2
MAX_TRIES = 3
TEXT_CAP = 24000            # characters of a document's text kept in its note
SUMMARY_MIN = 1500          # a document longer than this also gets a short summary
IMAGE_SIDE = 2000           # a photo is sent at most this many pixels on its longest side
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".ics", ".html", ".htm", ".xml"}
# The model guard queues a request behind people's chats when its text says so.
SCHEDULED = "You are running as a scheduled cron job."


class ModelOff(Exception):
    """The model server did not answer: try again next time, count nothing."""


def env_file():
    try:
        return dict(l.strip().split("=", 1) for l in open(HOME / ".env") if "=" in l and not l.startswith("#"))
    except OSError:
        return {}


def model_conf():
    """(base_url, model, key) from the agent's config.yaml and .env; LOGOS_MODEL_* in the environment win."""
    url, name, key = os.environ.get("LOGOS_MODEL_URL", ""), os.environ.get("LOGOS_MODEL_NAME", ""), os.environ.get("LOGOS_MODEL_KEY", "")
    if not (url and name):
        try:
            text = (HOME / "config.yaml").read_text()
            block = re.search(r"^model:\n((?:[ \t]+.*\n)+)", text, re.M)
            lines = block.group(1) if block else ""
            url = url or (re.search(r"base_url:\s*(\S+)", lines) or [None, ""])[1]
            name = name or (re.search(r"default:\s*(\S+)", lines) or [None, ""])[1]
        except OSError:
            pass
    return url.rstrip("/"), name, key or env_file().get("LOGOS_MODEL_KEY", "")


def chat(conf, content, max_tokens, timeout=900):
    url, name, key = conf
    if not (url and name):
        raise ModelOff("no model configured")
    body = {"model": name, "max_tokens": max_tokens, "temperature": 0.2, "messages": [
        {"role": "system", "content": SCHEDULED + " You make accurate notes for a household assistant's memory."},
        {"role": "user", "content": content}]}
    req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (502, 503, 504):
            raise ModelOff(f"model server answered {e.code}")
        raise
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise ModelOff(str(e)[:120])
    text = ((out.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not text.strip():
        raise ValueError("the model gave no words")
    return text.strip()


# ── reading each kind of file ────────────────────────────────────────────────────────────────
# The Python the sandbox lets reach the model has no PDF, Word or image libraries; the agent's other Python
# (hermes's own) has them but may not touch the network. Reading a file needs no network, so that one does it,
# as a small subprocess, and only this process talks to the model.
VENV_PYTHON = os.environ.get("LOGOS_VAULT_HELPER_PYTHON", "/opt/hermes/.venv/bin/python")
HELPER = r'''
import base64, io, json, sys
kind, path, side = sys.argv[1], sys.argv[2], int(sys.argv[3])
if kind == "pdf":
    from pypdf import PdfReader
    pages = []
    for p in PdfReader(path).pages[:60]:
        try:
            pages.append((p.extract_text() or "").strip())
        except Exception:
            pages.append("")
    print(json.dumps({"text": "\n\n".join(t for t in pages if t)}))
elif kind == "docx":
    import docx
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    print(json.dumps({"text": "\n".join(parts)}))
elif kind == "image":
    from PIL import Image
    im = Image.open(path)
    im.load()
    if max(im.size) > side:
        im.thumbnail((side, side))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    print(json.dumps({"jpeg": base64.b64encode(buf.getvalue()).decode()}))
'''


def helper(kind, path):
    """What the reading helper makes of a file; ImportError when it is not there (that kind is then not read)."""
    import subprocess
    if not os.path.exists(VENV_PYTHON):
        raise ImportError("no helper python")
    r = subprocess.run([VENV_PYTHON, "-c", HELPER, kind, str(path), str(IMAGE_SIDE)], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise ImportError((r.stderr or "helper failed")[-200:])
    return json.loads(r.stdout)


def image_url(path, mime):
    try:
        data, mime = base64.b64decode(helper("image", path)["jpeg"]), "image/jpeg"      # smaller: a table stays legible
    except ImportError:
        data = Path(path).read_bytes()
        if len(data) > 12 * 1024 * 1024:
            raise ValueError("an image too big to send without resizing")
    return f"data:{mime or 'image/png'};base64,{base64.b64encode(data).decode()}"


TITLE = re.compile(r"^\s*TITLE:\s*(.+)$", re.M | re.I)


def split_title(text, fallback):
    m = TITLE.search(text)
    title = (m.group(1).strip().strip("*#") if m else "") or fallback
    return title[:120], TITLE.sub("", text, count=1).strip()


def read_image(meta, conf):
    prompt = ("Describe this file for the notes. Start with one line: TITLE: and a short title. Then transcribe every piece of "
              "text in it exactly, keeping tables as markdown tables with all their rows and columns, and list the dates, "
              "names, amounts and codes. If it is a photo of something else, say what it shows and anything useful about it. "
              "Do not invent anything that is not visible.")
    if meta.get("caption"):
        prompt += f"\nThe person sent it with the words: {meta['caption'][:300]}"
    text = chat(conf, [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": image_url(VAULT / meta["path"], meta.get("mime"))}}], 3500)
    title, words = split_title(text, meta["name"])
    return title, "## What it says\n\n" + words


def read_pdf(path):
    return helper("pdf", path)["text"]


def read_docx(path):
    return helper("docx", path)["text"]


def strip_markup(text):
    return re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", " ", text))


def extract_text(meta):
    """The file's own text, or None when this kind cannot be read here."""
    path = VAULT / meta["path"]
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return read_pdf(path) or ""
        if ext == ".docx":
            return read_docx(path)
        if ext in TEXT_EXT:
            raw = path.read_bytes()[:400000].decode("utf-8", "replace")
            return strip_markup(raw) if ext in (".html", ".htm", ".xml") else raw
    except ImportError:
        return None
    return None


def read_document(meta, conf):
    text = extract_text(meta)
    if text is None:
        return meta["name"], "This kind of file is kept but not read yet (only its details are in this note)."
    text = text.strip()
    if len(text) < 40:
        return meta["name"], ("A file with no readable text in it: perhaps a scan. It is kept, and a later version "
                              "will read scans too. Ask Greg's or Béré's agent to look at the original if needed.")
    title, summary = meta["name"], ""
    if len(text) > SUMMARY_MIN:
        try:
            got = chat(conf, "Write TITLE: and a short title on the first line, then 3 to 6 bullet points on what this document "
                             "is and its key facts (dates, names, amounts). Use only what is in it.\n\n" + text[:12000], 700)
            title, summary = split_title(got, meta["name"])
        except ModelOff:
            raise
        except Exception:
            pass                                    # the text still goes in
    body = (f"## Summary\n\n{summary}\n\n" if summary else "") + "## Contents\n\n" + text[:TEXT_CAP]
    if len(text) > TEXT_CAP:
        body += f"\n\n(…{len(text) - TEXT_CAP} more characters in the original.)"
    return title, body


def read(meta, conf):
    path = VAULT / meta["path"]
    ext = path.suffix.lower()
    if ext in IMAGE_EXT or (ext == "" and meta.get("mime", "").startswith("image/")):
        return read_image(meta, conf)
    return read_document(meta, conf)


# ── the note ─────────────────────────────────────────────────────────────────────────────────

def slug(name):
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:40] or "file"


def human(size):
    if size < 1024:
        return f"{size} bytes"
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1048576:.1f} MB"


def render(meta, title, body):
    day = time.strftime("%Y-%m-%d", time.localtime(meta["kept_at"]))
    sent = [s for s in meta.get("seen", [])] or [{"from": meta.get("from", ""), "at": meta["kept_at"]}]
    who = ", ".join(sorted({s.get("from") or "someone" for s in sent}))
    more = f" (sent {len(sent)} times)" if len(sent) > 1 else ""
    cap = f"\nSent with the words: {meta['caption']}\n" if meta.get("caption") else ""
    return (f"# {title}\n\n"
            f"*Sent by {who} on {day}{more}. The original, \"{meta['name']}\" ({human(meta.get('size', 0))}), is kept in "
            f"the vault as `{meta['id']}`.*{cap}\n{body.rstrip()}\n")


def write_note(meta, title, body):
    NOTES.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.localtime(meta["kept_at"]))
    path = NOTES / f"{day}-{slug(meta['name'])}-{meta['id'][:8]}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render(meta, title, body), encoding="utf-8")
    os.replace(tmp, path)
    return path


def save_meta(meta):
    path = META / f"{meta['id']}.json"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, path)


def pending():
    out = []
    for p in sorted(META.glob("*.json")) if META.is_dir() else []:
        try:
            meta = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if meta.get("state") in ("new", "retry") and meta.get("tries", 0) < MAX_TRIES:
            out.append(meta)
    return sorted(out, key=lambda m: m.get("kept_at", 0))


def run(conf=None, limit=MAX_PER_RUN):
    """Process up to ``limit`` files; returns the ids done."""
    conf = conf or model_conf()
    done = []
    for meta in pending()[:limit]:
        try:
            title, body = read(meta, conf)
            note = write_note(meta, title, body)
            meta.update(state="done", note=os.path.relpath(note, HOME), title=title, digested_at=int(time.time()))
        except ModelOff as e:
            print(f"vault: the model did not answer ({e}); will try again", file=sys.stderr)
            return done
        except Exception as e:
            meta["tries"] = meta.get("tries", 0) + 1
            meta["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
            if meta["tries"] >= MAX_TRIES:
                note = write_note(meta, meta["name"], "This file could not be read after several tries "
                                  f"({meta['last_error']}). It is kept; ask an agent to look at the original.")
                meta.update(state="failed", note=os.path.relpath(note, HOME))
            print(f"vault: {meta['name']}: {meta['last_error']}", file=sys.stderr)
        save_meta(meta)
        if meta.get("state") == "done":
            done.append(meta["id"])
    return done


if __name__ == "__main__":
    try:
        run()
    except Exception as e:                           # a job that never breaks the agent
        print(f"vault: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(0)
