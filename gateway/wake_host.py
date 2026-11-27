"""The agents' names as the house speaker's wake words (Voice VW.1, "Logos streaming").

Two halves, beside the speaker voices (gateway/tts_host.py):

- **The container.** ``logos-wake`` runs gateway/wake.py (image infra/wake/Dockerfile), a Wyoming
  wake word service Home Assistant streams the speaker's audio to. It listens on the LAN like
  ``logos-tts``, so it is on only when ``~/.config/logos/speaker`` says where::

      TTS_BIND=192.0.2.20      # this machine's LAN address, shared with the speaker voices
      WAKE_PORT=10400          # turns the wake words on (Wyoming's usual port for them)

  It starts on the ``up`` of an agent that speaks on the speaker (``speaker_voice:`` or
  ``speaker_name:``).
- **The names.** Each agent's ``speaker_name:`` gives two wake words, "Hey <name>" and the bare
  name, unless ``speaker_wake:`` lists the phrases (and how strict each is)::

      speaker_name: Finn
      speaker_wake: {Hey Finn: 0.1, Finn: 0.3}       # optional; higher = harder to wake

  :func:`write_names` puts them in ``~/.local/state/logos/wake/names.json``, which the container
  reads again whenever it changes: on every ``logos-agent up`` and when the dashboard starts. A
  renamed agent wakes to its new name without training anything or restarting the container.
- **Who was called.** On a detection the container notes the agent in ``heard.json`` in the same
  folder. :func:`agent_for` is what ``POST /api/v2/voice/converse`` asks: the agent called within
  the last ``HEARD_S`` seconds (each detection used once), else the agent of the conversation the
  request continues, else None (the caller's default, Home).

Wyoming does not say which speaker streamed the audio, so "who was called" is the most recent
detection. With one speaker that is exact; with two speaking within seconds of each other it could
be the other one's.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

CONTAINER = "logos-wake"
IMAGE = "logos-wake:1"                  # built locally from infra/wake on first use
PORT = 10400                            # inside the container; outside it is WAKE_PORT
MEMORY = "512m"                         # it uses about 100 MB; a ceiling, not a reservation
CPUS = "1"                              # it needs about a tenth of one core while listening
SETTINGS_FILE = "speaker"               # in ~/.config/logos, shared with the speaker voices
NAMES_FILE = "names.json"
HEARD_FILE = "heard.json"
SERVER_FILE = Path(__file__).with_name("wake.py")
BUILD_DIR = Path(__file__).resolve().parent.parent / "infra" / "wake"

HEY_THRESHOLD = 0.10                    # "Hey Finn": two words, hard to say by accident (measured 27 Nov)
BARE_THRESHOLD = 0.25                   # "Finn": one word, so stricter
HEARD_S = 30.0                          # wake word to question: speaking plus speech-to-text
CONVERSATION_S = 600.0                  # a follow-up keeps the agent it started with

_lock = threading.Lock()
_used: set = set()                      # detections already given to a request ("at" values)
_conversations: Dict[str, tuple] = {}   # Home Assistant conversation id -> (agent, until)


class WakeError(Exception):
    pass


def uses_wake(agent: Mapping[str, Any]) -> bool:
    return bool(str(agent.get("speaker_name") or "").strip())


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or os.path.expanduser("~"))


def config_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("XDG_CONFIG_HOME") or _home(env) / ".config") / "logos"


def state_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("XDG_STATE_HOME") or _home(env) / ".local" / "state") / "logos" / "wake"


def _parse_env(text: str) -> dict:
    return {k.strip(): v.split("#", 1)[0].strip() for k, v in
            (l.split("=", 1) for l in text.splitlines() if "=" in l and not l.strip().startswith("#"))}


def settings(env: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """``{"bind", "port"}`` from ~/.config/logos/speaker; None when the wake words are off (no
    file, or no WAKE_PORT in it)."""
    env = os.environ if env is None else env
    path = config_dir(env) / SETTINGS_FILE
    try:
        values = _parse_env(path.read_text())
    except OSError:
        return None
    if not values.get("WAKE_PORT"):
        return None
    bind = values.get("WAKE_BIND") or values.get("TTS_BIND", "")
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise WakeError(f"{path}: TTS_BIND (or WAKE_BIND) must be an IP address, not {bind!r}")
    try:
        port = int(values["WAKE_PORT"])
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise WakeError(f"{path}: WAKE_PORT must be a port number")
    return {"bind": bind, "port": port}


# ── The names ──────────────────────────────────────────────────────────────────────────────

def _id(phrase: str) -> str:
    """The same id the container gives the phrase (gateway/wake.py phrase_id)."""
    t = unicodedata.normalize("NFKD", phrase)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"[^a-z' ]+", " ", t)).strip("_")


def phrases_of(name: str, definition: Mapping[str, Any]) -> List[dict]:
    """The wake words of one agent: ``[{"phrase", "agent", "threshold"}]``."""
    spoken = " ".join(str(definition.get("speaker_name") or "").split())
    if not spoken:
        return []
    wake = definition.get("speaker_wake")
    if isinstance(wake, Mapping) and wake:
        items = list(wake.items())
    elif isinstance(wake, (list, tuple)) and wake:
        items = [(p, None) for p in wake]
    else:
        items = [(f"Hey {spoken}", None), (spoken, None)]
    out = []
    for phrase, threshold in items:
        phrase = " ".join(str(phrase or "").split())
        if not _id(phrase):
            continue
        if threshold is None:
            threshold = BARE_THRESHOLD if len(phrase.split()) == 1 else HEY_THRESHOLD
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            raise WakeError(f"agents/{name}.yaml: speaker_wake: {phrase!r} needs a number, like 0.3")
        out.append({"phrase": phrase, "agent": name, "threshold": threshold})
    return out


def all_phrases(definitions: Mapping[str, Mapping[str, Any]]) -> List[dict]:
    """Every agent's wake words. A phrase two agents claim goes to neither (it would be a guess)."""
    rows = [r for name in sorted(definitions) for r in phrases_of(name, definitions[name])]
    owners: Dict[str, set] = {}
    for r in rows:
        owners.setdefault(_id(r["phrase"]), set()).add(r["agent"])
    clash = {k for k, v in owners.items() if len(v) > 1}
    for k in sorted(clash):
        logger.warning("wake words: %r is the name of more than one agent (%s); it wakes none",
                       k, ", ".join(sorted(owners[k])))
    seen, out = set(), []
    for r in rows:
        k = _id(r["phrase"])
        if k not in clash and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def definitions_of(runtime: Any) -> Dict[str, Mapping[str, Any]]:
    out = {}
    for name in runtime.defined():
        try:
            out[name] = runtime.load(name)
        except Exception as e:          # one broken definition must not silence the others
            logger.warning("wake words: skipped %s: %s", name, e)
    return out


def write_names(runtime: Any, env: Optional[Mapping[str, str]] = None, create: bool = False) -> Optional[List[dict]]:
    """Write names.json from the agents' definitions. Nothing to do (None) where the wake words are
    not set up, unless ``create`` (the container's ``up``)."""
    folder = state_dir(env)
    if not folder.is_dir():
        if not create:
            return None
        folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    phrases = all_phrases(definitions_of(runtime))
    path = folder / NAMES_FILE
    text = json.dumps({"phrases": phrases}, indent=1, ensure_ascii=False)
    if not path.exists() or path.read_text() != text:          # the container reloads on a change
        tmp = folder / (NAMES_FILE + ".tmp")
        tmp.write_text(text)
        tmp.chmod(0o644)
        os.replace(tmp, path)
    return phrases


# ── Who was called ─────────────────────────────────────────────────────────────────────────

def _heard(env: Optional[Mapping[str, str]]) -> List[dict]:
    try:
        data = json.loads((state_dir(env) / HEARD_FILE).read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("heard") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and r.get("agent")]


def agent_for(conversation_id: str = "", env: Optional[Mapping[str, str]] = None,
              now: Optional[float] = None) -> Optional[str]:
    """The agent a speaker request is for: the one whose name was just heard, else the one this
    conversation started with, else None. Never raises."""
    now = time.time() if now is None else now
    try:
        with _lock:
            for k in [k for k, (_, until) in _conversations.items() if until < now]:
                del _conversations[k]
            fresh = [r for r in _heard(env) if 0 <= now - float(r.get("at") or 0) <= HEARD_S
                     and float(r["at"]) not in _used]
            if fresh:
                row = max(fresh, key=lambda r: float(r["at"]))
                _used.update(float(r["at"]) for r in fresh)    # older ones were superseded
                if len(_used) > 200:
                    _used.difference_update(sorted(_used)[:100])
                agent = str(row["agent"])
                logger.info("voice: woken by %r -> %s", str(row.get("phrase") or "")[:40], agent)
                if conversation_id:
                    _conversations[conversation_id] = (agent, now + CONVERSATION_S)
                return agent
            hit = _conversations.get(conversation_id) if conversation_id else None
            if hit:
                _conversations[conversation_id] = (hit[0], now + CONVERSATION_S)
                return hit[0]
    except Exception as e:
        logger.warning("voice: could not tell which agent was called: %s", e)
    return None


def forget() -> None:
    with _lock:
        _used.clear()
        _conversations.clear()


# ── The container ──────────────────────────────────────────────────────────────────────────

def can_connect(address: tuple, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(address, timeout=timeout):
            return True
    except OSError:
        return False


class WakeHost:
    """``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`."""

    def __init__(self, runtime: Any, env: Optional[Mapping[str, str]] = None,
                 probe: Callable[[tuple], bool] = can_connect, user: Optional[str] = None):
        self.rt, self.env, self.probe = runtime, (os.environ if env is None else env), probe
        # It writes heard.json in a folder only this user may write, so it runs as this user.
        self.user = user or f"{os.getuid()}:{os.getgid()}"

    def running(self) -> bool:
        out = self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                          check=False)
        return CONTAINER in out.split()

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f",
                            '{{index .Config.Labels "logos.wake.stamp"}}'], check=False).strip()

    def ensure_image(self) -> bool:
        if self.rt.runner.run(["docker", "image", "inspect", IMAGE]).returncode == 0:
            return False
        self.rt.run(["docker", "build", "-q", "-t", IMAGE, str(BUILD_DIR)])
        return True

    def up(self) -> Optional[bool]:
        """Write the names, then start or update the container. None when the wake words are off,
        True when it was (re)started, False when it was already running as it should."""
        conf = settings(self.env)
        if conf is None:
            write_names(self.rt, self.env)        # if it was on before, keep its names right
            return None
        write_names(self.rt, self.env, create=True)
        folder = state_dir(self.env)
        dockerfile = (BUILD_DIR / "Dockerfile").read_text()
        stamp = hashlib.sha256((json.dumps(conf, sort_keys=True) + self.user + SERVER_FILE.read_text()
                                + dockerfile).encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        self.ensure_image()
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.wake.stamp={stamp}", "--user", self.user,
                     "--memory", MEMORY, "--cpus", CPUS, "--read-only", "--tmpfs", "/tmp",
                     "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                     # It never calls out: no resolver (which also stops onnxruntime's telemetry).
                     "--dns", "127.0.0.1",
                     "-p", f"{conf['bind']}:{conf['port']}:{PORT}",
                     "-v", f"{folder}:/state", "-v", f"{SERVER_FILE}:/app/wake.py:ro",
                     "-e", "WAKE_STATE=/state", "-e", f"WAKE_PORT={PORT}", "-e", "HOME=/tmp",
                     IMAGE, "python", "/app/wake.py"])
        self.wait(conf)
        return True

    def wait(self, conf: Mapping[str, Any], tries: int = 30) -> None:
        bind = str(conf["bind"])
        address = ("127.0.0.1" if bind in ("0.0.0.0", "::") else bind, int(conf["port"]))
        for _ in range(tries):
            if self.probe(address):
                return
            self.rt.runner.sleep(1)
        raise TimeoutError("the wake words did not start; see: docker logs " + CONTAINER)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)


# ── The dashboard: keep names.json in step when it starts ──────────────────────────────────

async def _refresh(app: Any) -> None:
    import asyncio
    runtime = getattr(app.get("sandbox_backend"), "runtime", None)
    if runtime is None:
        return
    try:
        await asyncio.to_thread(write_names, runtime)
    except Exception as e:
        logger.warning("wake words: could not write %s: %s", NAMES_FILE, e)


def register(app: Any) -> None:
    forget()
    app.on_startup.append(_refresh)
