"""The house service's container (story H1): the agents' way to the lights.

``logos-house`` (:mod:`gateway.house_service`) is an MCP server on the Docker bridge, one token
per agent, like the calendar service. It holds the Home Assistant login; the agent never does.

Agents with ``house: {enabled: true}`` get it: the household agent, and Greg's and Béré's own
agents (decided 19 Nov, first household-only, then direct light control for both people).

Home Assistant's address and token are in ``~/.config/logos/home-assistant`` (mode 0600)::

    HA_URL=http://192.0.2.10
    HA_TOKEN=<long-lived token of a NON-admin Home Assistant user, e.g. logos-household>

The address must be an IP address: the container has no DNS. The file is copied into the
service's own volume and never enters a sandbox.

A definition chooses what the agent may control::

    house:
      enabled: true
      domains: [light, scene]        # the default; nothing else is supported yet
      exclude: [light.balcony_balcony]   # entity ids the agent must leave alone
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Mapping

CONTAINER = "logos-house"
VOLUME = "logos-house"
NETWORK = "logos-house"                  # its own bridge: it needs to reach Home Assistant on the LAN
IMAGE = "python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"  # 3.12-alpine
SERVICE_FILE = Path(__file__).with_name("house_service.py")
PORT = 8098
TOKEN_ENV = "LOGOS_HOUSE_TOKEN"
SETTINGS_FILE = "home-assistant"          # in ~/.config/logos
DEFAULT_DOMAINS = ["light", "scene"]
SUPPORTED_DOMAINS = ["light", "scene", "todo"]     # todo: the shared lists (shopping, to-do)


class HouseError(Exception):
    pass


NOTES_FILE = "house-notes.md"     # in ~/.config/logos: what the agents should know about this home's lights


def uses_house(agent: Mapping[str, Any]) -> bool:
    """Any agent whose definition asks for it (the household agent, Greg's and Béré's agents)."""
    return bool((agent.get("house") or {}).get("enabled"))


def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".config")
    return Path(base) / "logos"


def parse_env(text: str) -> dict:
    return dict(l.strip().split("=", 1) for l in text.splitlines() if "=" in l and not l.strip().startswith("#"))


def ha_settings(env: Mapping[str, str]) -> Dict[str, str]:
    """``{"url", "token"}`` from ~/.config/logos/home-assistant; raises HouseError when unusable."""
    path = config_dir(env) / SETTINGS_FILE
    try:
        values = parse_env(path.read_text())
    except OSError:
        raise HouseError(f"no {path}: put HA_URL and HA_TOKEN (a non-admin Home Assistant user) in it")
    url, token = values.get("HA_URL", "").strip(), values.get("HA_TOKEN", "").strip()
    if not url or not token:
        raise HouseError(f"{path} needs both HA_URL and HA_TOKEN")
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise HouseError(f"HA_URL must be an IP address (the house service has no DNS), not {host!r}")
    if path.stat().st_mode & 0o077:
        raise HouseError(f"{path} must be readable only by you (chmod 600)")
    return {"url": url.rstrip("/"), "token": token}


def agent_settings(agent: Mapping[str, Any]) -> Dict[str, List[str]]:
    house = agent.get("house") or {}
    domains = [str(d) for d in (house.get("domains") or DEFAULT_DOMAINS)]
    unknown = [d for d in domains if d not in SUPPORTED_DOMAINS]
    if unknown:
        raise HouseError(f"agent {agent.get('name')}: house.domains {unknown} are not supported "
                         f"(only {', '.join(SUPPORTED_DOMAINS)})")
    return {"domains": domains, "exclude": [str(e) for e in house.get("exclude") or []]}


def service_config(agents: Mapping[str, Mapping[str, Any]], tokens: Mapping[str, str],
                   ha: Mapping[str, str]) -> dict:
    return {"ha_url": ha["url"], "ha_token": ha["token"],
            "agents": {n: {"token": tokens[n], **agent_settings(a)} for n, a in sorted(agents.items())}}


class HouseHost:
    """``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`."""

    def __init__(self, runtime: Any):
        self.rt = runtime

    def bind_address(self) -> str:
        out = self.rt.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"], check=False).strip()
        return out or "172.17.0.1"

    def url(self) -> str:
        return f"http://{self.bind_address()}:{PORT}/mcp"

    def agents(self) -> Dict[str, dict]:
        return {n: a for n in self.rt.defined() for a in [self.rt.load(n)] if uses_house(a)}

    def token_for(self, name: str) -> str:
        if hasattr(self.rt, "secret"):           # E7: the host store first (gateway/agent_secrets.py)
            return self.rt.secret(name, TOKEN_ENV)
        for line in self.rt.get(f"logos-home-{name}", ".env").splitlines():
            if line.startswith(TOKEN_ENV + "="):
                return line.split("=", 1)[1].strip()
        return ""

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f", '{{index .Config.Labels "logos.house.stamp"}}'],
                           check=False).strip()

    def running(self) -> bool:
        return CONTAINER in self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                                        check=False).split()

    def up(self) -> bool:
        """Start or update the service. True when it was (re)started."""
        agents = self.agents()
        tokens = {n: self.token_for(n) for n in agents}
        agents = {n: a for n, a in agents.items() if tokens[n]}       # joins on its first `up`
        if not agents:
            return False
        ha = ha_settings(self.rt.env)
        config = json.dumps(service_config(agents, tokens, ha), indent=1, sort_keys=True, ensure_ascii=False)
        stamp = hashlib.sha256((config + SERVICE_FILE.read_text() + IMAGE).encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", VOLUME])
        self.rt.run(["docker", "run", "--rm", "-i", "--network", "none", "-v", f"{VOLUME}:/v", "alpine:3", "sh", "-c",
                     "cat > /v/config.json && chown -R 1000:1000 /v && chmod 0600 /v/config.json"], input=config)
        if NETWORK not in self.rt.run(["docker", "network", "ls", "--format", "{{.Name}}"], check=False).split():
            self.rt.run(["docker", "network", "create", NETWORK])
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.house.stamp={stamp}", "--network", NETWORK, "--user", "1000:1000",
                     "--memory", "64m", "--read-only", "--cap-drop", "ALL",
                     # Home Assistant is reached by address; with no resolver nothing else can be looked up.
                     "--dns", "127.0.0.1",
                     "-p", f"{self.bind_address()}:{PORT}:{PORT}",
                     "-v", f"{VOLUME}:/data:ro", "-v", f"{SERVICE_FILE}:/app/house_service.py:ro",
                     "-e", "HOUSE_CONFIG=/data/config.json", "-e", f"HOUSE_PORT={PORT}",
                     "--entrypoint", "python3", IMAGE, "/app/house_service.py"])
        for _ in range(30):
            try:
                self.rt.runner.http(f"http://{self.bind_address()}:{PORT}/health", timeout=3)
                return True
            except Exception:
                self.rt.runner.sleep(1)
        raise TimeoutError("the house service did not start; see: docker logs " + CONTAINER)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
