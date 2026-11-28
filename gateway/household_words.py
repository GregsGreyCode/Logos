"""Household words: the names speech-to-text gets wrong, and what it writes instead (28 Nov).

Speech-to-text hears a name it does not know as a word it does ("Zoé" written as "Zoey"). Settings ›
Household words keeps a short list, ``[{"word": "Zoé", "heard_as": ["Zoey", "Joey"], "note": ""}]``,
and every speech-to-text path uses it:

- **Put right afterwards** (:func:`correct`), always: each ``heard_as`` is replaced by its word,
  whole words only, whatever the case, punctuation kept. In the house speaker's speech-to-text
  (gateway/stt.py) and the agents' voice notes and dictation (gateway/voice.py).
- **Listened for** (:func:`hotwords`): Whisper (the voice notes) is told the words before it starts,
  so it spells them as written. Parakeet (the speaker) is not: its hotwords made transcripts worse in
  tests (docs/voice.md).
- **Told to the agents** (:func:`agent_note`): one line on each turn, "Names here: Zoé (speech-to-text
  may write Zoey), …".

The list lives on the server only (``~/.config/logos/household-words.json``, owner-only), never in
the repo. People's first names and the agents' own names join it by themselves (:func:`defaults`).

Stdlib only: this file is mounted into the speech-to-text containers beside their server.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

FILE = "household-words.json"
MAX_WORDS = 100
MAX_HEARD = 10
MAX_LEN = 40
MAX_NOTE = 120
NOTE_MAX_CHARS = 600                    # the agents' line: short, it is on every turn
HOTWORDS_MAX_CHARS = 300                # Whisper's prompt is 224 tokens at most; leave it room


# ── the list ─────────────────────────────────────────────────────────────────────────────────

def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    s = unicodedata.normalize("NFC", " ".join(value.split()))
    return s if len(s) <= limit else ""


def _key(s: str) -> str:
    return unicodedata.normalize("NFC", " ".join(s.split())).casefold()


def clean(raw: Any) -> List[Dict[str, Any]]:
    """A list as it may be kept: each word once (1-40 characters), up to ten ``heard_as`` each (not the
    word itself, each once), an optional short note. Anything else is dropped, never an error."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        word = _text(item.get("word"), MAX_LEN)
        if not word or _key(word) in seen:
            continue
        seen.add(_key(word))
        heard: List[str] = []
        got = {_key(word)}
        raw_heard = item.get("heard_as")
        if isinstance(raw_heard, str):
            raw_heard = raw_heard.split(",")
        for h in raw_heard if isinstance(raw_heard, list) else []:
            h = _text(h, MAX_LEN).strip(" ,.;:!?")
            if h and _key(h) not in got and len(heard) < MAX_HEARD:
                got.add(_key(h))
                heard.append(h)
        entry: Dict[str, Any] = {"word": word, "heard_as": heard}
        note = _text(item.get("note"), MAX_NOTE)
        if note:
            entry["note"] = note
        out.append(entry)
        if len(out) >= MAX_WORDS:
            break
    return out


def path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return Path(base) / "logos" / FILE


def load(file: Path) -> List[Dict[str, Any]]:
    """The saved list; [] when there is none or it cannot be read."""
    try:
        data = json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return clean(data.get("words") if isinstance(data, Mapping) else data)


def save(file: Path, entries: Any) -> List[Dict[str, Any]]:
    """Keep the list (cleaned), owner-only, replaced in one go. Returns what was kept."""
    file = Path(file)
    kept = clean(entries)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(file.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"words": kept}, f, indent=1, ensure_ascii=False)
    os.chmod(tmp, 0o600)                 # an old .tmp left by a crash keeps its own mode otherwise
    os.replace(tmp, file)
    return kept


def defaults(people: Iterable[str] = (), agents: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Words that join by themselves: people's first names and the agents' own names (``source``
    says which), as entries with nothing heard yet."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for source, names in (("person", people), ("agent", agents)):
        for n in names:
            n = _text(n, MAX_LEN)
            if n and _key(n) not in seen:
                seen.add(_key(n))
                out.append({"word": n, "heard_as": [], "source": source})
    return out


def merged(saved: Sequence[Mapping[str, Any]], auto: Sequence[Mapping[str, Any]] = ()) -> List[Dict[str, Any]]:
    """The saved words (a saved one that would also join by itself says where from), then the
    automatic ones not already there."""
    sources = {_key(e["word"]): e.get("source") for e in auto}
    out = []
    for e in saved:
        e = dict(e)
        if sources.get(_key(e["word"])):
            e["source"] = sources[_key(e["word"])]
        out.append(e)
    have = {_key(e["word"]) for e in out}
    out += [dict(e) for e in auto if _key(e["word"]) not in have]
    return out


# ── putting the words right ──────────────────────────────────────────────────────────────────

def plain(s: str) -> str:
    """Without accents: "Zoé" -> "Zoe"."""
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def variants(entries: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """``{what speech-to-text may write (casefolded): the word}``. Each ``heard_as``, and the word
    without its accents ("Zoe" for "Zoé"). Never the word itself in another case: "home" stays
    "home" even when an agent is called Home. The first word to claim a spelling keeps it."""
    out: Dict[str, str] = {}
    for e in entries:
        word = e.get("word") or ""
        if not word:
            continue
        for v in [*(e.get("heard_as") or []), plain(word)]:
            k = _key(v)
            if k and k != _key(word) and k not in out:
                out[k] = word
    return out


def _pattern(found: Mapping[str, str]) -> Optional["re.Pattern[str]"]:
    if not found:
        return None
    # Longest first, so "be ray" wins over "ray"; spaces in a variant match any run of spaces.
    alts = [r"\s+".join(re.escape(part) for part in v.split()) for v in sorted(found, key=len, reverse=True)]
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)


def correct(text: str, entries: Sequence[Mapping[str, Any]]) -> str:
    """``text`` with each ``heard_as`` (and each word written without its accents) replaced by the
    word: whole words only, whatever the case, punctuation and spacing around it kept. Once, left
    to right, so a replacement is never replaced again. "ZOEY" becomes the word in capitals."""
    if not text:
        return text or ""
    found = variants(entries)
    pat = _pattern(found)
    if pat is None:
        return text
    text = unicodedata.normalize("NFC", text)

    def swap(m: "re.Match[str]") -> str:
        got = m.group(0)
        word = found.get(_key(got))
        if word is None:
            return got
        return word.upper() if len(got) > 1 and got.isupper() else word

    return pat.sub(swap, text)


def hotwords(entries: Sequence[Mapping[str, Any]], limit: int = HOTWORDS_MAX_CHARS) -> str:
    """The words, for Whisper's ``hotwords``: "Zoé, Wren, Quill"."""
    out = ""
    for e in entries:
        w = e.get("word") or ""
        nxt = f"{out}, {w}" if out else w
        if w and len(nxt) <= limit:
            out = nxt
    return out


def agent_note(entries: Sequence[Mapping[str, Any]], limit: int = NOTE_MAX_CHARS) -> str:
    """One line for the agents: "Names here: Zoé (speech-to-text may write Zoey or Joey), Wren." """
    parts: List[str] = []
    total = len("Names here: ") + 1
    for e in entries:
        w = e.get("word") or ""
        if not w:
            continue
        extra = []
        if e.get("note"):
            extra.append(str(e["note"]))
        heard = list(e.get("heard_as") or [])
        if heard:
            extra.append("speech-to-text may write " + (", ".join(heard[:-1]) + " or " + heard[-1] if len(heard) > 1 else heard[0]))
        part = f"{w} ({'; '.join(extra)})" if extra else w
        if total + len(part) + 2 > limit:
            break
        parts.append(part)
        total += len(part) + 2
    return ("Names here: " + ", ".join(parts) + ".") if parts else ""


# ── inside the speech-to-text containers ─────────────────────────────────────────────────────

class Watched:
    """A list kept in a file that something else replaces (the voice service's volume): read again
    when the file changes, checked at most every ``every_s`` seconds. [] while there is none."""

    def __init__(self, file: str, every_s: float = 5.0):
        self.file, self.every_s = file, every_s
        self._stamp: Any = None
        self._checked = -1e9
        self._entries: List[Dict[str, Any]] = []

    def entries(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        import time
        now = time.monotonic() if now is None else now
        if now - self._checked >= self.every_s:
            self._checked = now
            try:
                st = os.stat(self.file)
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = None
            if stamp != self._stamp:
                self._stamp = stamp
                self._entries = load(Path(self.file)) if stamp else []
        return self._entries
