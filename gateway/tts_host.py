"""Run the speaker voices container (Voice V1.2): Piper for Home Assistant, a voice for each agent.

The server is :mod:`gateway.tts`; its image (infra/tts/Dockerfile) carries Piper and twelve
English voices. Unlike the other services it listens on the LAN, because Home Assistant runs on
another machine. Wyoming has no login, so the address is chosen here and nowhere in the repo, in
``~/.config/logos/speaker``::

    TTS_BIND=192.0.2.20      # this machine's LAN address (0.0.0.0 for every address)
    TTS_PORT=10200           # optional; Wyoming's usual port

Without the file the speaker voices are off and Home Assistant keeps its own Piper.

The dashboard tells the container which agent said which words through one small file,
``~/.local/state/logos/speaker/said.json`` (gateway/speaker_voice.py writes it, the container
reads it, mounted read-only). It holds hashes of the words, never the words.

It starts on the ``up`` of an agent whose definition has ``speaker_voice:`` (the Household agent).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

CONTAINER = "logos-tts"
IMAGE = "logos-tts:1"                   # built locally from infra/tts on first use
PORT = 10200                            # inside the container, and the default outside
RUN_AS = "10000:10000"
MEMORY = "1g"                           # two voices loaded use about 300 MB; a ceiling, not a reservation
SETTINGS_FILE = "speaker"               # in ~/.config/logos
STATE_FILE = "said.json"
SERVER_FILE = Path(__file__).with_name("tts.py")
BUILD_DIR = Path(__file__).resolve().parent.parent / "infra" / "tts"


class TtsError(Exception):
    pass


def uses_speaker(agent: Mapping[str, Any]) -> bool:
    """An agent that answers through the house speaker has a voice of its own."""
    return bool(agent.get("speaker_voice"))


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or os.path.expanduser("~"))


def config_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("XDG_CONFIG_HOME") or _home(env) / ".config") / "logos"


def state_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("XDG_STATE_HOME") or _home(env) / ".local" / "state") / "logos" / "speaker"


def parse_env(text: str) -> dict:
    return {k.strip(): v.split("#", 1)[0].strip() for k, v in
            (l.split("=", 1) for l in text.splitlines() if "=" in l and not l.strip().startswith("#"))}


def settings(env: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """``{"bind", "port"}`` from ~/.config/logos/speaker; None when there is no such file (off)."""
    env = os.environ if env is None else env
    path = config_dir(env) / SETTINGS_FILE
    try:
        values = parse_env(path.read_text())
    except OSError:
        return None
    bind = values.get("TTS_BIND", "")
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise TtsError(f"{path}: TTS_BIND must be an IP address (this machine's LAN address), not {bind!r}")
    try:
        port = int(values.get("TTS_PORT") or PORT)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise TtsError(f"{path}: TTS_PORT must be a port number")
    return {"bind": bind, "port": port}


def client_address(conf: Mapping[str, Any]) -> tuple:
    """Where this machine reaches the service itself (for the samples in Settings)."""
    bind = str(conf["bind"])
    if bind in ("0.0.0.0", "::"):
        bind = "127.0.0.1"
    return bind, int(conf["port"])


def can_connect(address: tuple, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(address, timeout=timeout):
            return True
    except OSError:
        return False


class TtsHost:
    """``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`."""

    def __init__(self, runtime: Any, env: Optional[Mapping[str, str]] = None,
                 probe: Callable[[tuple], bool] = can_connect):
        self.rt, self.env, self.probe = runtime, (os.environ if env is None else env), probe

    def running(self) -> bool:
        out = self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                          check=False)
        return CONTAINER in out.split()

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f",
                            '{{index .Config.Labels "logos.tts.stamp"}}'], check=False).strip()

    def ensure_image(self) -> bool:
        if self.rt.runner.run(["docker", "image", "inspect", IMAGE]).returncode == 0:
            return False
        self.rt.run(["docker", "build", "-q", "-t", IMAGE, str(BUILD_DIR)])
        return True

    def ensure_state(self) -> Path:
        """The folder the container reads (0755, its file 0644: only hashes and voice names)."""
        folder = state_dir(self.env)
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o755)
        path = folder / STATE_FILE
        if not path.exists():
            path.write_text(json.dumps({"said": []}))
        path.chmod(0o644)
        return folder

    def up(self) -> Optional[bool]:
        """Start or update the container. None when the speaker voices are off (no settings file),
        True when it was (re)started, False when it was already running as it should."""
        conf = settings(self.env)
        if conf is None:
            return None
        folder = self.ensure_state()
        dockerfile = (BUILD_DIR / "Dockerfile").read_text()
        stamp = hashlib.sha256((json.dumps(conf, sort_keys=True) + SERVER_FILE.read_text() + dockerfile)
                               .encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        self.ensure_image()
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.tts.stamp={stamp}", "--user", RUN_AS,
                     "--memory", MEMORY, "--read-only", "--tmpfs", "/tmp",
                     "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                     # It never calls out: no resolver (which also stops onnxruntime's telemetry).
                     "--dns", "127.0.0.1",
                     "-p", f"{conf['bind']}:{conf['port']}:{PORT}",
                     "-v", f"{folder}:/state:ro", "-v", f"{SERVER_FILE}:/app/tts.py:ro",
                     "-e", f"TTS_STATE=/state/{STATE_FILE}", "-e", f"TTS_PORT={PORT}", "-e", "HOME=/tmp",
                     IMAGE, "python", "/app/tts.py"])
        self.wait(conf)
        return True

    def wait(self, conf: Mapping[str, Any], tries: int = 30) -> None:
        for _ in range(tries):
            if self.probe(client_address(conf)):
                return
            self.rt.runner.sleep(1)
        raise TimeoutError("the speaker voices did not start; see: docker logs " + CONTAINER)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
