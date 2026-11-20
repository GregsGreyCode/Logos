"""Run the Logos advisor (story A1): Greg's and Béré's agents may ask Claude, with approval.

``gateway/advisor_service.py`` runs in the ``logos-advisor`` container (image ``logos-advisor:1``,
built from ``infra/advisor`` with the pinned Anthropic SDK). Its config (agent tokens, the
home-names list, the dashboard's token) goes in on stdin into the read-only ``logos-advisor``
volume; the ledger lives in ``logos-advisor-state``. The Anthropic key is read from
``~/.config/logos/advisor.env`` (``ANTHROPIC_API_KEY=...``, mode 0600) with ``--env-file``, so it is
never on a command line, in git, a sandbox or a chat. Without that file the service still runs and
says it has no key; nothing can be sent.

An agent definition opts in with ``advisor: {enabled: true, person: greg}``. The household agent
never does (decided 18 Nov 2026).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Mapping

CONTAINER = "logos-advisor"
VOLUME = "logos-advisor"
STATE = "logos-advisor-state"
NETWORK = "logos-advisor"             # its own bridge: it needs api.anthropic.com
IMAGE = "logos-advisor:1"
BUILD_DIR = Path(__file__).resolve().parent.parent / "infra" / "advisor"
SERVICE_FILE = Path(__file__).with_name("advisor_service.py")
FILTER_FILE = Path(__file__).with_name("web_service.py")
PORT = 8100
TOKEN_ENV = "LOGOS_ADVISOR_TOKEN"
KEY_FILE = "advisor.env"              # in ~/.config/logos
DASHBOARD_TOKEN_FILE = "advisor-dashboard-token"


class AdvisorError(Exception):
    pass


def uses_advisor(agent: Mapping[str, Any]) -> bool:
    a = agent.get("advisor") or {}
    return bool(a.get("enabled")) and bool(a.get("person")) and agent.get("role") != "household"


def person_of(agent: Mapping[str, Any]) -> str:
    return str((agent.get("advisor") or {}).get("person") or "")


def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos"


def dashboard_token(env: Mapping[str, str]) -> str:
    """The token the dashboard uses for approvals; made once, kept in ~/.config/logos (0600)."""
    path = config_dir(env) / DASHBOARD_TOKEN_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32) + "\n")
        path.chmod(0o600)
    return path.read_text().strip()


class AdvisorHost:
    def __init__(self, runtime: Any):
        self.rt = runtime

    def bind_address(self) -> str:
        out = self.rt.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"], check=False).strip()
        return out or "172.17.0.1"

    def url(self) -> str:
        return f"http://{self.bind_address()}:{PORT}/mcp"

    def base(self) -> str:
        return f"http://{self.bind_address()}:{PORT}"

    def agents(self) -> Dict[str, dict]:
        return {n: a for n in self.rt.defined() for a in [self.rt.load(n)] if uses_advisor(a)}

    def token_for(self, name: str) -> str:
        for line in self.rt.get(f"logos-home-{name}", ".env").splitlines():
            if line.startswith(TOKEN_ENV + "="):
                return line.split("=", 1)[1].strip()
        return ""

    def running(self) -> bool:
        return CONTAINER in self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                                        check=False).split()

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f", '{{index .Config.Labels "logos.advisor.stamp"}}'],
                           check=False).strip()

    def up(self) -> bool:
        agents = self.agents()
        tokens = {n: self.token_for(n) for n in agents}
        agents = {n: a for n, a in agents.items() if tokens[n]}
        if not agents:
            return False
        wh = _web_host()
        names, exact = wh.WebHost(self.rt).private_names({n: self.rt.load(n) for n in self.rt.defined()})
        config = json.dumps({"agents": {n: {"token": tokens[n], "person": person_of(a)} for n, a in sorted(agents.items())},
                             "names": sorted(set(names)), "capital_only": sorted(set(exact)),
                             "dashboard_token": dashboard_token(self.rt.env)}, indent=1, sort_keys=True, ensure_ascii=False)
        key = config_dir(self.rt.env) / KEY_FILE
        key_stamp = hashlib.sha256(key.read_bytes()).hexdigest() if key.exists() else "none"
        stamp = hashlib.sha256((config + SERVICE_FILE.read_text() + FILTER_FILE.read_text() + IMAGE + key_stamp)
                               .encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        if self.rt.runner.run(["docker", "image", "inspect", IMAGE]).returncode != 0:
            self.rt.run(["docker", "build", "-q", "-t", IMAGE, str(BUILD_DIR)])
        for vol in (VOLUME, STATE):
            if self.rt.runner.run(["docker", "volume", "inspect", vol]).returncode != 0:
                self.rt.run(["docker", "volume", "create", vol])
        self.rt.run(["docker", "run", "--rm", "-i", "--network", "none", "-v", f"{VOLUME}:/v", "-v", f"{STATE}:/s",
                     "alpine:3", "sh", "-c",
                     "cat > /v/config.json && chown -R 1000:1000 /v /s && chmod 0600 /v/config.json && chmod 0700 /s"],
                    input=config)
        if NETWORK not in self.rt.run(["docker", "network", "ls", "--format", "{{.Name}}"], check=False).split():
            self.rt.run(["docker", "network", "create", NETWORK])
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        cmd = ["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
               "--label", f"logos.advisor.stamp={stamp}", "--network", NETWORK, "--user", "1000:1000",
               "--memory", "256m", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
               "-p", f"{self.bind_address()}:{PORT}:{PORT}",
               "-v", f"{VOLUME}:/data:ro", "-v", f"{STATE}:/state",
               "-v", f"{SERVICE_FILE}:/app/advisor_service.py:ro", "-v", f"{FILTER_FILE}:/app/web_service.py:ro",
               "-e", "ADVISOR_CONFIG=/data/config.json", "-e", "ADVISOR_STATE=/state", "-e", f"ADVISOR_PORT={PORT}"]
        if key.exists():
            cmd += ["--env-file", str(key)]
        self.rt.run(cmd + ["--entrypoint", "python3", IMAGE, "/app/advisor_service.py"])
        for _ in range(30):
            try:
                self.rt.runner.http(f"{self.base()}/health", timeout=3)
                return True
            except Exception:
                self.rt.runner.sleep(1)
        raise TimeoutError("the advisor did not start; see: docker logs " + CONTAINER)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)


def _web_host():
    import importlib.util
    import sys
    name = "logos_web_host"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("web_host.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
