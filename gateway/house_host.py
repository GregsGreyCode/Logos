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

Optionally, ``~/.config/logos/home-assistant-admin`` (same format, mode 0600) holds a token of an
ADMIN Home Assistant user. It is used for one thing only: creating a new shared list (a "Local
To-do" config entry, which Home Assistant lets only admins add) when an agent calls
``create_list``. It goes into the same service-only volume, never into a sandbox, a log or a tool
answer. Without it, ``create_list`` says plainly that it needs turning on (docs/house.md).

A definition chooses what the agent may control::

    house:
      enabled: true
      domains: [light, scene]        # the default; nothing else is supported yet
      exclude: [light.balcony_balcony]   # entity ids the agent must leave alone

Personal lists (Greg, 28 Nov): an agent with exactly one owner (``owners: [bere]``) and the
``todo`` domain also works on that person's own lists, which only they see. The service keeps
them in its own ``logos-lists`` volume (``/lists``, one ``<person>.json`` each, mode 0600), the
one thing it may write; the household agent (several owners) gets none.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Mapping

CONTAINER = "logos-house"
VOLUME = "logos-house"
LISTS_VOLUME = "logos-lists"             # personal lists (Greg, 28 Nov): the service's only writable place
NETWORK = "logos-house"                  # its own bridge: it needs to reach Home Assistant on the LAN
IMAGE = "python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"  # 3.12-alpine
SERVICE_FILE = Path(__file__).with_name("house_service.py")
PORT = 8098
TOKEN_ENV = "LOGOS_HOUSE_TOKEN"
SETTINGS_FILE = "home-assistant"          # in ~/.config/logos
ADMIN_SETTINGS_FILE = "home-assistant-admin"   # optional: an admin token, only for creating lists
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


def _read_settings(path: Path, missing: str) -> Dict[str, str]:
    try:
        values = parse_env(path.read_text())
    except OSError:
        raise HouseError(missing)
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


def ha_settings(env: Mapping[str, str]) -> Dict[str, str]:
    """``{"url", "token"}`` from ~/.config/logos/home-assistant; raises HouseError when unusable."""
    path = config_dir(env) / SETTINGS_FILE
    return _read_settings(path, f"no {path}: put HA_URL and HA_TOKEN (a non-admin Home Assistant user) in it")


def ha_admin_settings(env: Mapping[str, str]) -> Dict[str, str]:
    """``{"url", "token"}`` from ~/.config/logos/home-assistant-admin, or ``{}`` when there is none
    (creating lists is then off). Raises HouseError when the file is there but unusable."""
    path = config_dir(env) / ADMIN_SETTINGS_FILE
    if not path.exists():
        return {}
    return _read_settings(path, f"cannot read {path}")


def agent_settings(agent: Mapping[str, Any]) -> Dict[str, List[str]]:
    house = agent.get("house") or {}
    domains = [str(d) for d in (house.get("domains") or DEFAULT_DOMAINS)]
    unknown = [d for d in domains if d not in SUPPORTED_DOMAINS]
    if unknown:
        raise HouseError(f"agent {agent.get('name')}: house.domains {unknown} are not supported "
                         f"(only {', '.join(SUPPORTED_DOMAINS)})")
    return {"domains": domains, "exclude": [str(e) for e in house.get("exclude") or []]}


def list_owner(agent: Mapping[str, Any]) -> str:
    """The person whose personal lists this agent works on: its one owner, or '' (the household agent,
    an agent with no owner or with several)."""
    owners = agent.get("owners")
    if agent.get("role") == "household" or not isinstance(owners, list) or len(owners) != 1:
        return ""
    person = str(owners[0] or "").strip()
    return person if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", person) else ""


def service_config(agents: Mapping[str, Mapping[str, Any]], tokens: Mapping[str, str],
                   ha: Mapping[str, str], admin: Mapping[str, str] = None) -> dict:
    out = {"ha_url": ha["url"], "ha_token": ha["token"],
           "agents": {n: {"token": tokens[n], **agent_settings(a), **({"person": list_owner(a)} if list_owner(a) else {})}
                      for n, a in sorted(agents.items())}}
    if admin:                       # only for create_list; see ADMIN_SETTINGS_FILE
        out.update(ha_admin_url=admin["url"], ha_admin_token=admin["token"])
    return out


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
        try:
            admin = ha_admin_settings(self.rt.env)
        except HouseError as e:     # the lights and lists still work; only creating lists stays off
            logging.getLogger(__name__).warning("house: creating lists is off: %s", e)
            admin = {}
        config = json.dumps(service_config(agents, tokens, ha, admin), indent=1, sort_keys=True, ensure_ascii=False)
        stamp = hashlib.sha256((config + SERVICE_FILE.read_text() + IMAGE).encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", VOLUME])
        self.rt.run(["docker", "run", "--rm", "-i", "--network", "none", "-v", f"{VOLUME}:/v", "alpine:3", "sh", "-c",
                     "cat > /v/config.json && chown -R 1000:1000 /v && chmod 0600 /v/config.json"], input=config)
        if self.rt.runner.run(["docker", "volume", "inspect", LISTS_VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", LISTS_VOLUME])
        # Owned by the service's user and closed to everyone else; the files in it are 0600.
        self.rt.run(["docker", "run", "--rm", "--network", "none", "-v", f"{LISTS_VOLUME}:/l", "alpine:3", "sh", "-c",
                     "chown 1000:1000 /l && chmod 0700 /l"])
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
                     "-v", f"{LISTS_VOLUME}:/lists",
                     "-e", "HOUSE_CONFIG=/data/config.json", "-e", f"HOUSE_PORT={PORT}", "-e", "HOUSE_LISTS=/lists",
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
