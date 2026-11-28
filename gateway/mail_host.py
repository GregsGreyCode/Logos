"""Run the Logos mail service (story E1): each person's agent reads its own person's email.

A person may connect several mailboxes (F43, 27 Nov 2026), Gmail and Outlook mixed. Settings › Email
or ``scripts/logos-mail connect <person>`` writes one login per mailbox to
``~/.config/logos/mail/<person>/<id>.json`` (folder 0700, files 0600, never in git or a sandbox):

    Outlook.com: {"kind": "outlook", "address": ..., "client_id": ..., "refresh_token": ...}
    Gmail:       {"kind": "gmail", "address": ..., "app_password": ...}

each with its ``id`` (8 hex characters), when it was ``added``, and an optional
"exclude": ["mybank.example", "someone@example.com"] (never copied). Before F43 there was one file
per person, ``<person>.json``; it is moved into the folder the first time it is read, marked
``carried_over`` so the service keeps the mail it had already copied (no reconnecting).

The service (``gateway/mail_service.py``, container ``logos-mail``) gets those files as one
read-only config, keeps its copy of the mail and Microsoft's rotating token in the
``logos-mail-state`` volume, and answers the agents on the Docker bridge (MCP, one token per agent).
An agent definition opts in with ``mail: {enabled: true, person: greg}``; the household agent has none.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

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


BOX_ID = re.compile(r"^[0-9a-f]{8}$")
PERSON = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _person_dir(env: Mapping[str, str], person: str) -> Path:
    if not PERSON.match(person or ""):
        raise MailError("bad person name")
    return config_dir(env) / person


def _write(path: Path, box: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(box, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _stamp(t: float) -> str:
    """When a mailbox was added: they are listed in that order (to the microsecond, so two added in the
    same second keep their order)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1e6) % 1000000:06d}Z"


def _new_id(env: Mapping[str, str]) -> str:
    taken = {p.stem for p in config_dir(env).glob("*/*.json")} if config_dir(env).exists() else set()
    while True:
        bid = secrets.token_hex(4)
        if bid not in taken:
            return bid


def migrate(env: Mapping[str, str], person: str) -> Optional[str]:
    """F43: the one mailbox file a person had before (``<person>.json``) moves into their folder as it
    is, marked ``carried_over``: the service then keeps the mail it already copied and Microsoft's
    rotated token. The id it was given, or None when there was nothing to move."""
    old = config_dir(env) / f"{person}.json"
    try:
        box = json.loads(old.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(box, dict):
        return None
    bid = _new_id(env)
    box.update(id=bid, carried_over=True)
    box.setdefault("added", _stamp(old.stat().st_mtime))
    _write(_person_dir(env, person) / f"{bid}.json", box)
    old.unlink()
    return bid


def boxes_of(env: Mapping[str, str], person: str) -> List[dict]:
    """A person's mailboxes, oldest first, each with its ``id`` (an old single file is moved first)."""
    if not PERSON.match(person or ""):
        return []
    migrate(env, person)
    out = []
    d = config_dir(env) / person
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            box = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(box, dict) and BOX_ID.match(path.stem):
            box["id"] = path.stem
            out.append(box)
    return sorted(out, key=lambda b: (str(b.get("added") or ""), b["id"]))


def box_of(env: Mapping[str, str], person: str, box_id: str) -> Optional[dict]:
    return next((b for b in boxes_of(env, person) if b["id"] == box_id), None)


def same_mailbox(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return (a.get("kind") == b.get("kind") and bool(a.get("address"))
            and str(a.get("address")).lower() == str(b.get("address") or "").lower())


def save_box(env: Mapping[str, str], person: str, box: dict) -> dict:
    """Keep a login. With an ``id``, that mailbox is rewritten; without one, connecting a mailbox that
    is already there (same kind and address) replaces its login and keeps its id, private senders
    and copied mail; anything else is a new mailbox. The saved box, with its id."""
    box = dict(box)
    have = boxes_of(env, person)
    old = next((b for b in have if b["id"] == box.get("id")), None) if box.get("id") else \
        next((b for b in have if same_mailbox(b, box)), None)
    if box.get("id") and not old:
        raise MailError("no such mailbox")
    if old:
        for keep in ("exclude", "added", "carried_over"):
            if keep in old and keep not in box:
                box[keep] = old[keep]
        box["id"] = old["id"]
    else:
        box["id"] = _new_id(env)
        box.setdefault("added", _stamp(time.time()))
    _write(_person_dir(env, person) / f"{box['id']}.json", box)
    return box


def remove_box(env: Mapping[str, str], person: str, box_id: str) -> bool:
    if not BOX_ID.match(box_id or ""):
        return False
    migrate(env, person)
    try:
        (_person_dir(env, person) / f"{box_id}.json").unlink()
        return True
    except FileNotFoundError:
        return False


def usable(box: Mapping[str, Any]) -> bool:
    if box.get("kind") == "outlook":
        return bool(box.get("client_id") and box.get("refresh_token"))
    if box.get("kind") == "gmail":
        return bool(box.get("address") and box.get("app_password"))
    return False


def mailboxes(env: Mapping[str, str], people) -> Dict[str, dict]:
    """The connected mailboxes of these people, by mailbox id, each with its ``person``."""
    out = {}
    for person in sorted(set(people)):
        for box in boxes_of(env, person):
            if usable(box):
                out[box["id"]] = {**box, "person": person}
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
        if hasattr(self.rt, "secret"):           # E7: the host store first (gateway/agent_secrets.py)
            return self.rt.secret(name, TOKEN_ENV)
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

    def forget_token(self, box_id: str) -> None:
        """After a fresh sign-in: drop the rotated token so the new one from the config is used."""
        if not BOX_ID.match(box_id or ""):
            raise MailError("bad mailbox id")
        self.rt.run(["docker", "run", "--rm", "--network", "none", "-v", f"{STATE}:/s", "alpine:3", "sh", "-c",
                     f"rm -f /s/outlook-{box_id}.token"], check=False)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
