"""The web reader's two containers (story W1): a private SearXNG and the reader in front of it.

``logos-searxng`` asks the search engines (Google, Bing, Brave and Wikipedia; GitHub and Stack
Overflow for code; Bing News for news); nothing reaches it but the reader, over the
private Docker network ``logos-web``. ``logos-reader`` (:mod:`gateway.web_service`) is the
only thing agents talk to: an MCP server on the Docker bridge, one token per agent, with the
privacy filter in front of every query.

Only person agents get it (``web: {enabled: true}`` in their definition). The household agent
never does, whatever its definition says (decided 19 Nov).

The private names the filter refuses come from three places, gathered on every ``up``:
the people's and agents' own names, the titles of the pages in each brain's ``people/``
folder, the names the agents added (``add_private_name``, kept in the ``logos-reader-names``
volume), and ``~/.config/logos/home-names.txt`` (one name per line; a line starting with
``=`` is matched only as written, for names that are also words, like ``=Rose``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

NETWORK = "logos-web"
SEARXNG = "logos-searxng"
SEARXNG_VOLUME = "logos-searxng"
SEARXNG_IMAGE = "searxng/searxng@sha256:ffad724acd87b8dd312328d18da88ddd42e5c4b1d23231594662620053c13cae"  # 2026.11.5
SEARXNG_UID = "977:977"
READER = "logos-reader"
READER_VOLUME = "logos-reader"
NAMES_VOLUME = "logos-reader-names"      # names the agents added (add_private_name); the only writable place
READER_IMAGE = "python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"  # 3.12-alpine
SERVICE_FILE = Path(__file__).with_name("web_service.py")
PORT = 8096
TOKEN_ENV = "LOGOS_WEB_TOKEN"

NAMES_TEMPLATE = """# Names that must never leave the house in a web search (or, later, a question to the advisor).
# One per line. People's and agents' names and the pages in each brain's people/ folder are
# added by themselves; put here anyone else: family, friends, children, pets, employers,
# schools, your street and town. A line starting with = is matched only exactly as written,
# for names that are also everyday words (=Rose, =Bill).
"""

SETTINGS = """# Written by gateway/web_host.py. SearXNG for the Logos web reader: JSON only, no limiter
# (nothing but the reader reaches it), and only the engines below.
use_default_settings:
  engines:
    keep_only: [google, bing, brave, wikipedia, github, stackoverflow, bing news]
engines:                     # Google and Bing are off in SearXNG's defaults; this release has no Startpage
  - name: google
    disabled: false
  - name: bing
    disabled: false
general:
  instance_name: logos
  enable_metrics: false
server:
  secret_key: "{secret}"
  limiter: false
  public_instance: false
  image_proxy: false
search:
  safe_search: 1
  formats: [json]
outgoing:
  request_timeout: 4.0
  max_request_timeout: 8.0
"""


class WebError(Exception):
    pass


def uses_web(agent: Mapping[str, Any]) -> bool:
    """Person agents that ask for it. Never the household agent."""
    return bool((agent.get("web") or {}).get("enabled")) and agent.get("role") != "household"


def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".config")
    return Path(base) / "logos"


def parse_names(text: str) -> Tuple[List[str], List[str]]:
    """``home-names.txt`` -> (names matched however written, names matched only as written)."""
    names, exact = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        (exact if line.startswith("=") else names).append(line.lstrip("=").strip())
    return [n for n in names if n], [n for n in exact if n]


def own_names(agents: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """The agents' names and the people they belong to: "greg", "Greg", "Béré", "household" is not one."""
    out = []
    for name, a in agents.items():
        if a.get("role") == "household":
            continue
        out.append(name)
        shown = re.sub(r"(?i)['’]s agent$", "", str(a.get("display_name") or "")).strip()
        if shown and shown.lower() != "household":
            out.append(shown)
        person = (a.get("calendar") or {}).get("person")
        if person:
            out.append(person)
    return out


def reader_config(agents: Mapping[str, Mapping[str, Any]], tokens: Mapping[str, str],
                  names: List[str], exact: List[str]) -> dict:
    return {"searxng": f"http://{SEARXNG}:8080",
            "agents": {n: {"token": tokens[n]} for n in sorted(agents) if uses_web(agents[n]) and tokens.get(n)},
            "names": sorted(set(names)), "capital_only": sorted(set(exact))}


class WebHost:
    """``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`."""

    def __init__(self, runtime: Any):
        self.rt = runtime

    def bind_address(self) -> str:
        out = self.rt.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"], check=False).strip()
        return out or "172.17.0.1"

    def url(self) -> str:
        return f"http://{self.bind_address()}:{PORT}/mcp"

    def token_for(self, name: str) -> str:
        for line in self.rt.get(f"logos-home-{name}", ".env").splitlines():
            if line.startswith(TOKEN_ENV + "="):
                return line.split("=", 1)[1].strip()
        return ""

    def _label(self, container: str) -> str:
        return self.rt.run(["docker", "inspect", container, "-f", '{{index .Config.Labels "logos.web.stamp"}}'],
                           check=False).strip()

    def _running(self, container: str) -> bool:
        return container in self.rt.run(["docker", "ps", "--filter", f"name=^{container}$", "--format", "{{.Names}}"],
                                        check=False).split()

    def _write_volume(self, volume: str, name: str, text: str, owner: str) -> None:
        if self.rt.runner.run(["docker", "volume", "inspect", volume]).returncode != 0:
            self.rt.run(["docker", "volume", "create", volume])
        self.rt.run(["docker", "run", "--rm", "-i", "--network", "none", "-v", f"{volume}:/v", "alpine:3", "sh", "-c",
                     f"cat > /v/{name} && chown -R {owner} /v && chmod 0600 /v/{name}"], input=text)

    def _secret(self) -> str:
        path = config_dir(self.rt.env) / "searxng-secret"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            old = os.umask(0o077)
            try:
                path.write_text(secrets.token_hex(24))
            finally:
                os.umask(old)
        return path.read_text().strip()

    def private_names(self, agents: Mapping[str, Mapping[str, Any]]) -> Tuple[List[str], List[str]]:
        from gateway import brain_reader
        path = config_dir(self.rt.env) / "home-names.txt"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(NAMES_TEMPLATE)
            path.chmod(0o600)
        names, exact = parse_names(path.read_text())
        names += own_names(agents)
        for name in agents:                       # who each brain knows: its people/ pages
            try:
                listing = brain_reader.list_pages(self.rt, name)
            except Exception:
                continue
            names += [p["title"] for p in listing.get("pages") or [] if p.get("folder") == "people"]
        return names, exact

    def up(self) -> bool:
        """Start or update both containers. True when either was (re)started."""
        agents = {n: self.rt.load(n) for n in self.rt.defined()}
        web_agents = {n: a for n, a in agents.items() if uses_web(a)}
        if not web_agents:
            return False
        if NETWORK not in self.rt.run(["docker", "network", "ls", "--format", "{{.Name}}"], check=False).split():
            self.rt.run(["docker", "network", "create", NETWORK])
        changed = self._up_searxng()
        tokens = {n: self.token_for(n) for n in web_agents}
        names, exact = self.private_names(agents)
        config = json.dumps(reader_config(agents, tokens, names, exact), indent=1, sort_keys=True, ensure_ascii=False)
        stamp = hashlib.sha256((config + SERVICE_FILE.read_text() + READER_IMAGE).encode()).hexdigest()[:16]
        if self._running(READER) and self._label(READER) == stamp:
            return changed
        self._write_volume(READER_VOLUME, "config.json", config, "1000:1000")
        if self.rt.runner.run(["docker", "volume", "inspect", NAMES_VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", NAMES_VOLUME])
            self.rt.run(["docker", "run", "--rm", "--network", "none", "-v", f"{NAMES_VOLUME}:/v", "alpine:3",
                         "sh", "-c", "touch /v/added.txt && chown -R 1000:1000 /v && chmod 0600 /v/added.txt"])
        self.rt.run(["docker", "rm", "-f", READER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", READER, "--restart", "unless-stopped",
                     "--label", f"logos.web.stamp={stamp}", "--network", NETWORK, "--user", "1000:1000",
                     "--memory", "128m", "--read-only", "--cap-drop", "ALL",
                     "-p", f"{self.bind_address()}:{PORT}:{PORT}",
                     "-v", f"{READER_VOLUME}:/data:ro", "-v", f"{NAMES_VOLUME}:/names",
                     "-v", f"{SERVICE_FILE}:/app/web_service.py:ro",
                     "-e", "WEB_CONFIG=/data/config.json", "-e", f"WEB_PORT={PORT}",
                     "-e", "WEB_ADDED_NAMES=/names/added.txt",
                     "--entrypoint", "python3", READER_IMAGE, "/app/web_service.py"])
        self._wait()
        return True

    def _up_searxng(self) -> bool:
        settings = SETTINGS.format(secret=self._secret())
        stamp = hashlib.sha256((settings + SEARXNG_IMAGE).encode()).hexdigest()[:16]
        if self._running(SEARXNG) and self._label(SEARXNG) == stamp:
            return False
        self._write_volume(SEARXNG_VOLUME, "settings.yml", settings, SEARXNG_UID)
        self.rt.run(["docker", "rm", "-f", SEARXNG], check=False)
        self.rt.run(["docker", "run", "-d", "--name", SEARXNG, "--restart", "unless-stopped",
                     "--label", f"logos.web.stamp={stamp}", "--network", NETWORK, "--user", SEARXNG_UID,
                     "--memory", "512m", "--read-only", "--tmpfs", "/var/cache/searxng:uid=977,gid=977",
                     "--tmpfs", "/tmp", "--cap-drop", "ALL",
                     "-v", f"{SEARXNG_VOLUME}:/etc/searxng:ro", SEARXNG_IMAGE])
        return True

    def _wait(self, tries: int = 30) -> None:
        for _ in range(tries):
            try:
                self.rt.runner.http(f"http://{self.bind_address()}:{PORT}/health", timeout=3)
                return
            except Exception:
                self.rt.runner.sleep(1)
        raise TimeoutError("the web reader did not start; see: docker logs " + READER)

    def down(self) -> None:
        for c in (READER, SEARXNG):
            self.rt.run(["docker", "rm", "-f", c], check=False)

    def status(self) -> str:
        parts = [f"{c}: {'running' if self._running(c) else 'not running'}" for c in (SEARXNG, READER)]
        return "web reader: " + ", ".join(parts) + f" (agents reach it at {self.url()})"
