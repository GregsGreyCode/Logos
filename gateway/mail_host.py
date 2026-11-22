"""Run the Logos mail service (story E1): each person's agent reads its own person's email.

``scripts/logos-mail connect <person>`` writes the mailbox login to
``~/.config/logos/mail/<person>.json`` (mode 0600, never in git or a sandbox):

    Outlook.com: {"kind": "outlook", "address": ..., "client_id": ..., "refresh_token": ...}
    Gmail:       {"kind": "gmail", "address": ..., "app_password": ...}

either with an optional "exclude": ["mybank.example", "someone@example.com"] (never copied).

The service (``gateway/mail_service.py``, container ``logos-mail``) gets those files as one
read-only config, keeps its copy of the mail and Microsoft's rotating token in the
``logos-mail-state`` volume, and answers the agents on the Docker bridge (MCP, one token per agent).
An agent definition opts in with ``mail: {enabled: true, person: greg}``; the household agent has none.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping

CONTAINER = "logos-mail"
VOLUME = "logos-mail"                 # config.json, read only in the container
STATE = "logos-mail-state"            # the mail copy and Microsoft's token
NETWORK = "logos-mail"                # its own bridge: it needs the internet (Microsoft, Google)
IMAGE = "python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"  # 3.12-alpine
SERVICE_FILE = Path(__file__).with_name("mail_service.py")
PORT = 8099
TOKEN_ENV = "LOGOS_MAIL_TOKEN"
URL_ENV = "LOGOS_MAIL_URL"


class MailError(Exception):
    pass


def uses_mail(agent: Mapping[str, Any]) -> bool:
    mail = agent.get("mail") or {}
    return bool(mail.get("enabled")) and bool(mail.get("person"))


def person_of(agent: Mapping[str, Any]) -> str:
    return str((agent.get("mail") or {}).get("person") or "")


def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos" / "mail"


def mailboxes(env: Mapping[str, str], people) -> Dict[str, dict]:
    """The connected mailboxes of these people (files written by scripts/logos-mail)."""
    out = {}
    for person in sorted(set(people)):
        path = config_dir(env) / f"{person}.json"
        try:
            box = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if box.get("kind") == "outlook" and box.get("client_id") and box.get("refresh_token"):
            out[person] = box
        elif box.get("kind") == "gmail" and box.get("address") and box.get("app_password"):
            out[person] = box
    return out


def service_config(agents: Mapping[str, Mapping[str, Any]], tokens: Mapping[str, str],
                   boxes: Mapping[str, dict]) -> dict:
    return {"agents": {n: {"token": tokens[n], "person": person_of(a)} for n, a in sorted(agents.items())},
            "mailboxes": dict(sorted(boxes.items()))}


class MailHost:
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
        return {n: a for n in self.rt.defined() for a in [self.rt.load(n)] if uses_mail(a)}

    def token_for(self, name: str) -> str:
        for line in self.rt.get(f"logos-home-{name}", ".env").splitlines():
            if line.startswith(TOKEN_ENV + "="):
                return line.split("=", 1)[1].strip()
        return ""

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f", '{{index .Config.Labels "logos.mail.stamp"}}'],
                           check=False).strip()

    def running(self) -> bool:
        return CONTAINER in self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                                        check=False).split()

    def up(self) -> bool:
        """Start or update the service. True when it was (re)started; False when nothing is connected."""
        agents = self.agents()
        tokens = {n: self.token_for(n) for n in agents}
        agents = {n: a for n, a in agents.items() if tokens[n]}       # joins on its first `up`
        boxes = mailboxes(self.rt.env, [person_of(a) for a in agents.values()])
        if not agents or not boxes:
            # Nothing (left) to read: the last mailbox was disconnected (F40), so stop the service too.
            if self.running():
                self.down()
            return False
        config = json.dumps(service_config(agents, tokens, boxes), indent=1, sort_keys=True, ensure_ascii=False)
        stamp = hashlib.sha256((config + SERVICE_FILE.read_text() + IMAGE).encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
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
        self.rt.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.mail.stamp={stamp}", "--network", NETWORK, "--user", "1000:1000",
                     "--memory", "256m", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                     "-p", f"{self.bind_address()}:{PORT}:{PORT}",
                     "-v", f"{VOLUME}:/data:ro", "-v", f"{STATE}:/state",
                     "-v", f"{SERVICE_FILE}:/app/mail_service.py:ro",
                     "-e", "MAIL_CONFIG=/data/config.json", "-e", "MAIL_STATE=/state", "-e", f"MAIL_PORT={PORT}",
                     "--entrypoint", "python3", IMAGE, "/app/mail_service.py"])
        for _ in range(30):
            try:
                self.rt.runner.http(f"http://{self.bind_address()}:{PORT}/health", timeout=3)
                return True
            except Exception:
                self.rt.runner.sleep(1)
        raise TimeoutError("the mail service did not start; see: docker logs " + CONTAINER)

    def forget_token(self, person: str) -> None:
        """After a fresh sign-in: drop the rotated token so the new one from the config is used."""
        if not person.replace("-", "").replace("_", "").isalnum():
            raise MailError("bad person name")
        self.rt.run(["docker", "run", "--rm", "--network", "none", "-v", f"{STATE}:/s", "alpine:3", "sh", "-c",
                     f"rm -f /s/outlook-{person}.token"], check=False)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
