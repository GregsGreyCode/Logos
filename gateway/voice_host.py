"""Run the voice container and keep its list of agents current (F31).

The server is :mod:`gateway.voice`; its image (infra/voice/Dockerfile) carries
faster-whisper and one Whisper model. It listens on the Docker bridge address:
sandboxes can reach it, the LAN cannot. One container serves every agent, so the
model is in memory once, and only while someone is using it.

Every agent whose definition has ``voice: true`` gets a token on its next ``up``
(kept in its home volume) and appears in the service's config.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping

CONTAINER = "logos-voice"
VOLUME = "logos-voice"
IMAGE = "logos-voice:1"                 # built locally from infra/voice on first use
PORT = 8093
HELPER = "alpine:3"
RUN_AS = "10000:10000"
TOKEN_ENV = "LOGOS_VOICE_TOKEN"         # the agent's own token, kept in its home volume .env
MEMORY = "3g"                           # Whisper small needs well under 1 GB; a ceiling, not a reservation
SERVER_FILE = Path(__file__).with_name("voice.py")
BUILD_DIR = Path(__file__).resolve().parent.parent / "infra" / "voice"


def uses_voice(agent: Mapping[str, Any]) -> bool:
    return bool(agent.get("voice"))


def build_config(tokens: Mapping[str, str]) -> dict:
    return {"agents": dict(sorted(tokens.items()))}


class VoiceHost:
    """``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`."""

    def __init__(self, runtime: Any):
        self.rt = runtime

    def bind_address(self) -> str:
        out = self.rt.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"], check=False).strip()
        return out or "172.17.0.1"

    def base_url(self) -> str:
        """What hermes is given as ``stt.openai.base_url``."""
        return f"http://{self.bind_address()}:{PORT}/v1"

    def agents(self) -> Dict[str, dict]:
        found = {}
        for name in self.rt.defined():
            agent = self.rt.load(name)
            if uses_voice(agent):
                found[name] = agent
        return found

    def token_for(self, name: str) -> str:
        if hasattr(self.rt, "secret"):           # E7: the host store first (gateway/agent_secrets.py)
            return self.rt.secret(name, TOKEN_ENV)
        for line in self.rt.get(f"logos-home-{name}", ".env").splitlines():
            if line.startswith(TOKEN_ENV + "="):
                return line.split("=", 1)[1].strip()
        return ""

    def running(self) -> bool:
        out = self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                          check=False)
        return CONTAINER in out.split()

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f",
                            '{{index .Config.Labels "logos.voice.stamp"}}'], check=False).strip()

    def ensure_image(self) -> bool:
        """Build the image when it is missing. True when it was built (a few minutes, once)."""
        if self.rt.runner.run(["docker", "image", "inspect", IMAGE]).returncode == 0:
            return False
        self.rt.run(["docker", "build", "-q", "-t", IMAGE, str(BUILD_DIR)])
        return True

    def up(self) -> bool:
        """Write the config and (re)start the container if the agents or the server code changed.
        Returns True when it was started or restarted."""
        tokens = {n: self.token_for(n) for n in self.agents()}
        # An agent that has never been started with voice on has no token yet; it joins on its next `up`.
        tokens = {n: t for n, t in tokens.items() if t}
        if not tokens:
            raise ValueError("no agent with voice has a token yet (run scripts/logos-agent up <name> once)")
        config = json.dumps(build_config(tokens), indent=1, sort_keys=True)
        dockerfile = (BUILD_DIR / "Dockerfile").read_text()
        stamp = hashlib.sha256((config + SERVER_FILE.read_text() + dockerfile).encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        self.ensure_image()
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", VOLUME])
        self.rt.run(["docker", "run", "--rm", "-i", "-v", f"{VOLUME}:/v", HELPER, "sh", "-c",
                     f"cat > /v/config.json && chown -R {RUN_AS} /v && chmod 0600 /v/config.json"], input=config)
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.voice.stamp={stamp}", "--user", RUN_AS,
                     "--memory", MEMORY, "--read-only", "--tmpfs", "/tmp",
                     # Nothing in here needs to call out. With no resolver it cannot look a name up,
                     # which also stops library telemetry (see voice.quiet_onnxruntime).
                     "--dns", "127.0.0.1",
                     "-p", f"{self.bind_address()}:{PORT}:{PORT}",
                     "-v", f"{VOLUME}:/data:ro", "-v", f"{SERVER_FILE}:/app/voice.py:ro",
                     "-e", "VOICE_CONFIG=/data/config.json", "-e", f"VOICE_PORT={PORT}",
                     IMAGE, "python", "/app/voice.py"])
        self.wait()
        return True

    def wait(self, tries: int = 30) -> None:
        url = f"http://{self.bind_address()}:{PORT}/health"
        for _ in range(tries):
            try:
                self.rt.runner.http(url, timeout=3)
                return
            except Exception:
                self.rt.runner.sleep(1)
        raise TimeoutError("the voice service did not start; see: docker logs " + CONTAINER)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)

    def status(self) -> str:
        if not self.running():
            return "voice: not running"
        names = [n for n in sorted(self.agents()) if self.token_for(n)]
        return f"voice: running at {self.base_url()}\n  agents: " + ", ".join(names)
