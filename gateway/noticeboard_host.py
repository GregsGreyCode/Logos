"""Run the noticeboard container and keep its list of agents current (S5.3).

The server itself is :mod:`gateway.noticeboard` (one stdlib-only file). It runs
in the agent image, which already has Python, with that file mounted read-only,
so there is no extra image to build or pull. It listens on the Docker bridge
address: sandboxes can reach it, the LAN cannot.

Every agent whose definition has ``noticeboard: true`` gets a token on its
first ``up`` (kept in its home volume) and appears in the noticeboard's config.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping

CONTAINER = "logos-noticeboard"
VOLUME = "logos-noticeboard"
PORT = 8090                             # MCP: the two tools
POLL_PORT = 8092                        # plain GET /pending and /urgent for the sandbox scripts
HELPER = "alpine:3"
RUN_AS = "10000:10000"                  # `hermes` in the logos-agent image
TOKEN_ENV = "LOGOS_NOTICEBOARD_TOKEN"   # the agent's own token, kept in its home volume .env
SERVER_FILE = Path(__file__).with_name("noticeboard.py")


def uses_noticeboard(agent: Mapping[str, Any]) -> bool:
    return bool(agent.get("noticeboard"))


def build_config(agents: Mapping[str, Mapping[str, Any]], tokens: Mapping[str, str]) -> dict:
    return {"agents": {
        name: {"token": tokens[name],
               "display_name": str(agent.get("display_name") or name),
               "role": str(agent.get("role") or "person")}
        for name, agent in sorted(agents.items())}}


class NoticeboardHost:
    """``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`."""

    def __init__(self, runtime: Any):
        self.rt = runtime

    def bind_address(self) -> str:
        out = self.rt.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"], check=False).strip()
        return out or "172.17.0.1"

    def url(self) -> str:
        return f"http://{self.bind_address()}:{PORT}/mcp"

    def poll_url(self) -> str:
        return f"http://{self.bind_address()}:{POLL_PORT}"

    def agents(self) -> Dict[str, dict]:
        found = {}
        for name in self.rt.defined():
            agent = self.rt.load(name)
            if uses_noticeboard(agent):
                found[name] = agent
        return found

    def token_for(self, name: str) -> str:
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
                            '{{index .Config.Labels "logos.noticeboard.stamp"}}'], check=False).strip()

    def up(self) -> bool:
        """Write the config and (re)start the container if the agents or the server code changed.
        Returns True when it was started or restarted."""
        agents = self.agents()
        tokens = {n: self.token_for(n) for n in agents}
        # An agent that has never been started has no token yet; it joins on its first `up`.
        agents = {n: a for n, a in agents.items() if tokens[n]}
        if not agents:
            raise ValueError("no agent on the noticeboard has a token yet (run scripts/logos-agent up <name> once)")
        config = json.dumps(build_config(agents, tokens), indent=1, sort_keys=True)
        image = next(iter(agents.values()))["image"]
        stamp = hashlib.sha256((config + SERVER_FILE.read_text() + image).encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", VOLUME])
        self.rt.run(["docker", "run", "--rm", "-i", "-v", f"{VOLUME}:/v", HELPER, "sh", "-c",
                     f"cat > /v/config.json && chown -R {RUN_AS} /v && chmod 0600 /v/config.json"], input=config)
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.noticeboard.stamp={stamp}", "--user", RUN_AS,
                     "-p", f"{self.bind_address()}:{PORT}:{PORT}",
                     "-p", f"{self.bind_address()}:{POLL_PORT}:{POLL_PORT}",
                     "-v", f"{VOLUME}:/data", "-v", f"{SERVER_FILE}:/app/noticeboard.py:ro",
                     "-e", "NOTICEBOARD_CONFIG=/data/config.json", "-e", "NOTICEBOARD_DB=/data/noticeboard.db",
                     "-e", f"NOTICEBOARD_PORT={PORT}", "-e", f"NOTICEBOARD_POLL_PORT={POLL_PORT}", "--entrypoint", "python3", image, "/app/noticeboard.py"])
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
        raise TimeoutError("the noticeboard did not start; see: docker logs " + CONTAINER)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)

    def status(self) -> str:
        if not self.running():
            return "noticeboard: not running"
        names = [n for n in sorted(self.agents()) if self.token_for(n)]
        return f"noticeboard: running at {self.url()}\n  agents: " + ", ".join(names)
