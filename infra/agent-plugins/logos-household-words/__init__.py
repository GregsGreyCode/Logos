"""logos-household-words: the household's names, on every turn (Settings › Household words, 28 Nov).

Speech-to-text writes a name it does not know as one it does. Logos puts the words right in the
transcripts it makes, but an older message, or a spelling nobody has added yet, still reaches the
agent. Logos leaves one line in the agent's home (``/opt/data/logos-words.json``, kept up to date
within a minute of a change, gateway/household_words_api.py); this adds it to each turn:
"Names here: Zoé (speech-to-text may write Zoey), Wren."
"""
from __future__ import annotations

import json
import os
from typing import Optional

HOME = os.environ.get("HERMES_HOME", "/opt/data")
NOTE = "logos-words.json"
MAX_CHARS = 800


def note(home: str = HOME, **_) -> Optional[dict]:
    try:
        with open(os.path.join(home, NOTE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    return {"context": text.strip()[:MAX_CHARS]}


def register(ctx):
    ctx.register_hook("pre_llm_call", note)
