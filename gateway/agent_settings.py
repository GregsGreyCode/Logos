"""Agent settings (story E6): what an agent's people may change about it from the dashboard.

An agent is defined in the public repo (``agents/<name>.yaml``, ``souls/<name>/soul.md``). The
people it belongs to may change a few things about it in the dashboard (Agents › Settings), and
those choices are kept on this machine only, outside git, so nothing personal reaches the repo and
a deploy never overwrites them::

    ~/.config/logos/agents/<name>/
        settings.yaml      the chosen values (only the keys in ALLOWED; anything else is ignored)
        soul.md            the agent's own personality and instructions, instead of its soul: file
        history/<id>.md    earlier versions of soul.md (HISTORY_KEEP of them), with history.json
        applied.json       what the running sandbox was made from (to say "not in use yet")

``$LOGOS_AGENT_SETTINGS`` names another folder (tests); else ``$XDG_CONFIG_HOME/logos/agents``.

How it is used: :meth:`gateway.agent_runtime.AgentRuntime.load` lays :func:`overlay` over the
tracked definition and under the machine's own ``agents/*.local.yaml`` files (so an administrator's
hand edit still wins), and ``soul_text`` takes :func:`own_soul` (the overlay, else the repo's
file) before the shared rules, which stay in the repo and cannot be edited here. Nothing changes in
a running agent until it is recreated (``scripts/logos-agent up <name>``, which the dashboard's
"Save and restart" runs).

Like :mod:`gateway.agent_runtime`, this module imports nothing from the rest of ``gateway``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

SOUL_LIMIT = 8_000                 # characters of the agent's own soul (the default ones are ~1,200)
HISTORY_KEEP = 20
DISPLAY_NAME_MAX = 40
#: The only keys settings.yaml may set. Anything else in the file is ignored, so an edit there can
#: never reach owners, policy, providers, budgets or anything secret.
ALLOWED = ("display_name", "voice_echo", "speaker_name", "speaker_wake")
MODEL_KEYS = ("default", "context_length", "vision")
CONTEXT_CHOICES = (16_384, 32_768, 65_536, 96_512, 131_072)
MIN_CONTEXT = 16_384
SHORT_CONTEXT = 32_768             # below this, long chats lose their start (the rules alone are ~7k tokens)
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_WORD = re.compile(r"^[^\W\d_]+(?:['’-][^\W\d_]+)*$")       # letters, maybe O'Neil or Jean-Luc
_HISTORY_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")


class Invalid(ValueError):
    """A choice that cannot be used; the message is shown to the person as it is."""


# ── where ────────────────────────────────────────────────────────────────────────────────────

def base_dir(env: Mapping[str, str]) -> Path:
    if env.get("LOGOS_AGENT_SETTINGS"):
        return Path(env["LOGOS_AGENT_SETTINGS"])
    home = env.get("HOME") or os.path.expanduser("~")
    return Path(env.get("XDG_CONFIG_HOME") or Path(home) / ".config") / "logos" / "agents"


def agent_dir(env: Mapping[str, str], name: str) -> Path:
    if not _NAME.match(str(name or "")):
        raise Invalid("That is not an agent's name.")
    return base_dir(env) / name


def _write(path: Path, text: str) -> None:
    """Atomically, readable by this account only (the folder too)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    os.replace(tmp, path)


# ── settings.yaml ────────────────────────────────────────────────────────────────────────────

def _clean(data: Any) -> dict:
    """Only the allowed keys, whatever the file says."""
    if not isinstance(data, dict):
        return {}
    out = {k: data[k] for k in ALLOWED if k in data}
    model = data.get("model")
    if isinstance(model, dict):
        m = {k: model[k] for k in MODEL_KEYS if k in model}
        if m:
            out["model"] = m
    return out


def overlay(env: Mapping[str, str], name: str) -> dict:
    """What the dashboard set for this agent ({} when nothing, or the file cannot be read)."""
    try:
        return _clean(yaml.safe_load((agent_dir(env, name) / "settings.yaml").read_text()) or {})
    except (OSError, ValueError, yaml.YAMLError, Invalid):
        return {}


def save_overlay(env: Mapping[str, str], name: str, values: Mapping[str, Any]) -> dict:
    """Replace settings.yaml with ``values`` (cleaned); an empty overlay removes the file."""
    clean = _clean(dict(values))
    path = agent_dir(env, name) / "settings.yaml"
    if not clean:
        path.unlink(missing_ok=True)
        return {}
    _write(path, "# Written by the dashboard (Agents › Settings). See docs/agent-settings.md.\n"
           + yaml.safe_dump(clean, sort_keys=True, allow_unicode=True))
    return clean


# ── the soul ─────────────────────────────────────────────────────────────────────────────────

def soul_file(env: Mapping[str, str], name: str) -> Optional[Path]:
    """The agent's own soul from the dashboard, when there is one."""
    try:
        path = agent_dir(env, name) / "soul.md"
    except Invalid:
        return None
    return path if path.is_file() else None


def default_soul(repo: Path, agent: Mapping[str, Any]) -> str:
    return (Path(repo) / agent["soul"]).read_text()


def own_soul(repo: Path, env: Mapping[str, str], agent: Mapping[str, Any]) -> str:
    """The persona part of SOUL.md: the dashboard's version, else the definition's ``soul:`` file."""
    path = soul_file(env, str(agent.get("name") or ""))
    if path is not None:
        try:
            return path.read_text()
        except OSError:
            pass
    return default_soul(repo, agent)


def check_soul(text: Any, redact=None) -> str:
    """The text as it will be kept, or :class:`Invalid` saying why not."""
    if not isinstance(text, str):
        raise Invalid("The personality must be text.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip("\n") + "\n"
    if not text.strip():
        raise Invalid("The personality cannot be empty. Use Reset to go back to the default.")
    if len(text) > SOUL_LIMIT:
        raise Invalid(f"That is {len(text):,} characters; the most is {SOUL_LIMIT:,}.")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise Invalid("The text has control characters in it.")
    if redact is not None and redact(text) != text:
        raise Invalid("That looks like it has a password or key in it. Secrets never go in an agent's instructions.")
    return text


def _history_dir(env: Mapping[str, str], name: str) -> Path:
    return agent_dir(env, name) / "history"


def history(env: Mapping[str, str], name: str) -> List[dict]:
    """Saved versions, newest first: [{id, ts, by, chars, what}]."""
    try:
        rows = json.loads((_history_dir(env, name) / "history.json").read_text())
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and _HISTORY_ID.match(str(r.get("id") or ""))] if isinstance(rows, list) else []


def history_text(env: Mapping[str, str], name: str, version: str) -> str:
    if not _HISTORY_ID.match(str(version or "")) or not any(r["id"] == version for r in history(env, name)):
        raise Invalid("That version is not kept any more.")
    try:
        return (_history_dir(env, name) / f"{version}.md").read_text()
    except OSError:
        raise Invalid("That version is not kept any more.")


def _remember(env: Mapping[str, str], name: str, text: str, by: str, what: str, now: float) -> dict:
    folder = _history_dir(env, name)
    vid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + secrets.token_hex(2)
    _write(folder / f"{vid}.md", text)
    row = {"id": vid, "ts": int(now), "by": str(by or ""), "chars": len(text), "what": what}
    rows = [row] + history(env, name)
    for old in rows[HISTORY_KEEP:]:
        (folder / f"{old['id']}.md").unlink(missing_ok=True)
    _write(folder / "history.json", json.dumps(rows[:HISTORY_KEEP], indent=1))
    return row


def save_soul(env: Mapping[str, str], repo: Path, agent: Mapping[str, Any], text: str, by: str,
              what: str = "edited", now: Optional[float] = None) -> Optional[dict]:
    """Keep ``text`` (already checked) as the agent's own soul, and in its history. The default text
    itself removes the overlay. Returns the history row, or None when nothing changed."""
    name = str(agent["name"])
    now = time.time() if now is None else now
    current = own_soul(repo, env, agent)
    if text == current:
        return None
    if text == default_soul(repo, agent):
        return reset_soul(env, repo, agent, by, now=now)
    if soul_file(env, name) is None and not history(env, name):
        _remember(env, name, current, "", "default", now - 1)    # the starting point can be restored too
    row = _remember(env, name, text, by, what, now)
    _write(agent_dir(env, name) / "soul.md", text)
    return row


def reset_soul(env: Mapping[str, str], repo: Path, agent: Mapping[str, Any], by: str,
               now: Optional[float] = None) -> Optional[dict]:
    """Back to the definition's soul file. The version in use is kept in the history first."""
    name = str(agent["name"])
    path = soul_file(env, name)
    if path is None:
        return None
    now = time.time() if now is None else now
    row = _remember(env, name, default_soul(repo, agent), by, "default", now)
    path.unlink(missing_ok=True)
    return row


# ── checks for the other settings ────────────────────────────────────────────────────────────

def check_display_name(value: Any) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        raise Invalid("The name cannot be empty.")
    if len(text) > DISPLAY_NAME_MAX:
        raise Invalid(f"The name can be at most {DISPLAY_NAME_MAX} characters.")
    if any(unicodedata.category(c).startswith("C") for c in text) or any(c in text for c in "<>{}`\\"):
        raise Invalid("Use letters, numbers and ordinary punctuation in the name.")
    return text


def check_speaker_name(value: Any, taken: Mapping[str, str]) -> str:
    """One or two words of letters (the house speaker listens for "Hey <name>"). ``taken`` maps the
    wake words other agents use (normalised, see :func:`wake_key`) to the agent that has them."""
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        raise Invalid("The speaker name cannot be empty.")
    words = text.split()
    if len(text) < 2 or len(text) > 20 or len(words) > 2 or not all(_WORD.match(w) for w in words):
        raise Invalid("Use one or two words of letters for the speaker name, like Finn or Aunt May.")
    for phrase in (f"Hey {text}", text):
        owner = taken.get(wake_key(phrase))
        if owner:
            raise Invalid(f'"{phrase}" already wakes {owner}. Pick another name.')
    return text


def name_key(text: str) -> str:
    """Two spellings of one name are the same name: "Zoé" and "zoe", "Aunt-May" and "aunt may"."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", t).replace("_", " ").split())


def check_not_taken(value: str, taken: Mapping[str, str]) -> str:
    """Refuse a name another agent or a person already goes by. ``taken`` maps :func:`name_key` of
    each such name to who has it ("Household agent", "Béré")."""
    owner = taken.get(name_key(value))
    if owner:
        raise Invalid(f'"{value}" is taken: {owner} goes by it. Pick another name.')
    return value


def wake_key(phrase: str) -> str:
    """As gateway/wake_host.py ``_id``: two spellings of one phrase are the same wake word."""
    t = unicodedata.normalize("NFKD", phrase)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"[^a-z' ]+", " ", t)).strip("_")


# ── the model ────────────────────────────────────────────────────────────────────────────────

def model_choices(listing: Any) -> List[dict]:
    """LM Studio's ``GET /api/v0/models`` as choices: chat models only, with what matters here."""
    rows = (listing or {}).get("data") if isinstance(listing, dict) else None
    out = []
    for m in rows or []:
        if not isinstance(m, dict) or m.get("type") not in ("llm", "vlm") or not m.get("id"):
            continue
        out.append({"id": str(m["id"]), "vision": m.get("type") == "vlm",
                    "max_context": int(m.get("max_context_length") or 0),
                    "loaded": m.get("state") == "loaded",
                    "loaded_context": int(m.get("loaded_context_length") or 0) or None})
    return sorted(out, key=lambda c: (not c["loaded"], c["id"]))


def check_model(choice: Any, choices: List[dict], current: Mapping[str, Any],
                others: Mapping[str, Mapping[str, Any]]) -> Tuple[dict, List[dict]]:
    """``choice`` {default, context_length} -> (the model block to keep, warnings).

    ``current`` is the agent's model block now; ``others`` the other agents' model blocks (what
    they share on the model server). A warning with ``confirm: True`` must be agreed to first."""
    if not isinstance(choice, dict):
        raise Invalid("Pick a model.")
    model_id = str(choice.get("default") or "")
    found = next((c for c in choices if c["id"] == model_id), None)
    if found is None:
        raise Invalid("That model is not on the model server. Pick one from the list.")
    try:
        window = int(choice.get("context_length") or current.get("context_length") or 0)
    except (TypeError, ValueError):
        raise Invalid("The context window must be a number of tokens.")
    if window not in CONTEXT_CHOICES and window != int(current.get("context_length") or 0):
        raise Invalid("Pick one of the context window sizes listed.")
    if window < MIN_CONTEXT:
        raise Invalid(f"The context window must be at least {MIN_CONTEXT:,} tokens.")
    if found["max_context"] and window > found["max_context"]:
        raise Invalid(f"{model_id} takes at most {found['max_context']:,} tokens of context.")
    warnings: List[dict] = []
    same = model_id == str(current.get("default") or "") and window == int(current.get("context_length") or 0)
    if not same:
        shared = sorted(n for n, m in others.items() if str(m.get("default") or "") == model_id)
        if not found["loaded"]:
            warnings.append({"confirm": True, "text": (
                f"{model_id} is not loaded right now. It loads when the agent next answers (a minute or more), "
                "and takes graphics memory beside any model the other agents use, which can slow them all.")})
        biggest = max([found["loaded_context"] or 0] + [int(m.get("load_context_length") or m.get("context_length") or 0)
                                                        for n, m in others.items() if str(m.get("default") or "") == model_id])
        if found["loaded"] and window > biggest:
            warnings.append({"confirm": True, "text": (
                f"{window:,} tokens is more than {model_id} is loaded with ({biggest:,}). It is loaded again with the "
                "larger window" + (f", which pauses {', '.join(shared)} for a minute" if shared else "") + ".")})
        if window < SHORT_CONTEXT:
            warnings.append({"confirm": False, "text": (
                f"{window:,} tokens is small: the agent's rules and memory take about 8,000, so long chats lose their start.")})
        if current.get("vision") and not found["vision"]:
            warnings.append({"confirm": False, "text": f"{model_id} cannot see photos; photos sent in a chat will not be understood."})
    block = {"default": model_id, "context_length": window, "vision": bool(found["vision"])}
    return block, warnings


# ── what the running agent was made from ─────────────────────────────────────────────────────

def fingerprint(env: Mapping[str, str], name: str) -> str:
    path = soul_file(env, name)
    soul = path.read_text() if path else ""
    return hashlib.sha256(json.dumps([overlay(env, name), soul], sort_keys=True).encode()).hexdigest()[:16]


def mark_applied(env: Mapping[str, str], name: str, now: Optional[float] = None) -> None:
    _write(agent_dir(env, name) / "applied.json",
           json.dumps({"fingerprint": fingerprint(env, name), "ts": int(time.time() if now is None else now)}))


def note_applied(env: Mapping[str, str], name: str) -> None:
    """Called by ``up`` once the sandbox is Ready: only for an agent with settings here (an agent
    nobody changed gets no folder)."""
    try:
        if agent_dir(env, name).is_dir():
            mark_applied(env, name)
    except (OSError, Invalid):
        pass


def pending(env: Mapping[str, str], name: str) -> bool:
    """Saved here but not yet in the running agent (it has not been restarted since)."""
    try:
        applied = json.loads((agent_dir(env, name) / "applied.json").read_text()).get("fingerprint")
    except (OSError, ValueError, AttributeError):
        applied = None
    now = fingerprint(env, name)
    if applied is None:                          # never restarted from here: anything set at all
        return now != hashlib.sha256(json.dumps([{}, ""]).encode()).hexdigest()[:16]
    return applied != now
