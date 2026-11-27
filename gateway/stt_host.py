"""Run the house speaker's speech-to-text container (Voice VO.1): the words, and who said them.

The server is :mod:`gateway.stt`; its image (infra/stt/Dockerfile) carries Whisper small (English),
the CAM++ voice model and the reference voices. It has two doors:

- **Wyoming, on the LAN** (Home Assistant runs on another machine), at the address of the speaker
  voices (``TTS_BIND``) and ``STT_PORT``, in ``~/.config/logos/speaker``::

      TTS_BIND=192.0.2.20      # this machine's LAN address
      STT_PORT=10300           # switches Logos speech-to-text on (Wyoming's usual port)

  No ``STT_PORT``: off, and Home Assistant keeps its own Whisper.
- **HTTP for the dashboard, on 127.0.0.1 only** (port 8101), with a token made here
  (``~/.config/logos/stt.token``, 0600): the agents' sandboxes cannot reach it, and the LAN port
  has no token behind it.

It starts on the ``up`` of an agent whose definition has ``speaker_voice:`` (the Household agent),
with the speaker voices.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from gateway import tts_host

CONTAINER = "logos-stt"
IMAGE = "logos-stt:2"                   # 2: Parakeet v3 (English and French)
WYOMING_PORT = 10300
HTTP_PORT = 8101
RUN_AS = "10000:10000"
MEMORY = "2g"                     # Whisper small and CAM++ use about 700 MB; a ceiling, not a reservation
TOKEN_FILE = "stt.token"
FILES = [Path(__file__).with_name("stt.py"), Path(__file__).with_name("voiceprint.py")]
BUILD_DIR = Path(__file__).resolve().parent.parent / "infra" / "stt"


class SttError(Exception):
    pass


def settings(env: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """``{"bind", "port"}`` when Logos speech-to-text is switched on, else None."""
    env = os.environ if env is None else env
    conf = tts_host.settings(env)              # TTS_BIND, checked there
    if conf is None:
        return None
    try:
        values = tts_host.parse_env((tts_host.config_dir(env) / tts_host.SETTINGS_FILE).read_text())
    except OSError:
        return None
    raw = values.get("STT_PORT", "")
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535 or port == conf["port"]:
        raise SttError(f"{tts_host.config_dir(env) / tts_host.SETTINGS_FILE}: STT_PORT must be a port number, "
                       "not the speaker voices' port")
    return {"bind": conf["bind"], "port": port}


def token(env: Optional[Mapping[str, str]] = None) -> str:
    """The dashboard's key to the HTTP door: made once, owner-only."""
    env = os.environ if env is None else env
    path = tts_host.config_dir(env) / TOKEN_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def base_url() -> str:
    return f"http://127.0.0.1:{HTTP_PORT}"


def can_connect(address: tuple, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(address, timeout=timeout):
            return True
    except OSError:
        return False


class SttHost:
    def __init__(self, runtime: Any, env: Optional[Mapping[str, str]] = None,
                 probe: Callable[[tuple], bool] = can_connect):
        self.rt, self.env, self.probe = runtime, (os.environ if env is None else env), probe

    def running(self) -> bool:
        out = self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"], check=False)
        return CONTAINER in out.split()

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f", '{{index .Config.Labels "logos.stt.stamp"}}'],
                           check=False).strip()

    def ensure_image(self) -> bool:
        if self.rt.runner.run(["docker", "image", "inspect", IMAGE]).returncode == 0:
            return False
        self.rt.run(["docker", "build", "-q", "-t", IMAGE, str(BUILD_DIR)])
        return True

    def up(self) -> Optional[bool]:
        """None when off (no ``STT_PORT``), True when (re)started, False when already as it should be."""
        conf = settings(self.env)
        if conf is None:
            return None
        key = token(self.env)
        stamp = hashlib.sha256((json.dumps(conf, sort_keys=True) + key + (BUILD_DIR / "Dockerfile").read_text()
                                + "".join(f.read_text() for f in FILES)).encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        self.ensure_image()
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        mounts = [a for f in FILES for a in ("-v", f"{f}:/app/{f.name}:ro")]
        self.rt.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.stt.stamp={stamp}", "--user", RUN_AS,
                     "--memory", MEMORY, "--read-only", "--tmpfs", "/tmp",
                     "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                     "--dns", "127.0.0.1",          # it never calls out (and onnxruntime's telemetry cannot)
                     "-p", f"{conf['bind']}:{conf['port']}:{WYOMING_PORT}",
                     "-p", f"127.0.0.1:{HTTP_PORT}:{HTTP_PORT}",
                     *mounts, "-e", f"STT_TOKEN={key}", "-e", "HOME=/tmp",
                     IMAGE, "python", "/app/stt.py"])
        self.wait(conf)
        return True

    def wait(self, conf: Mapping[str, Any], tries: int = 60) -> None:
        for _ in range(tries):
            if self.probe(tts_host.client_address(conf)):
                return
            self.rt.runner.sleep(1)
        raise TimeoutError("speech to text did not start; see: docker logs " + CONTAINER)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
