"""Run the Logos advisor (story A1): Greg's and Béré's agents may ask Claude, with approval.

``gateway/advisor_service.py`` runs in the ``logos-advisor`` container (image ``logos-advisor:1``,
built from ``infra/advisor`` with the pinned Anthropic SDK). Its config (agent tokens, the
home-names list, the dashboard's token) goes in on stdin into the read-only ``logos-advisor``
volume; the ledger lives in ``logos-advisor-state``. The Anthropic key is read from
``~/.config/logos/advisor.env`` (``ANTHROPIC_API_KEY=...``, mode 0600) with ``--env-file``, so it is
never on a command line, in git, a sandbox or a chat. Without that file the service still runs and
says it has no key; nothing can be sent.

An agent definition opts in with ``advisor: {enabled: true, person: greg}``; ``admin: true`` marks
the person who may change budgets (Greg). The household agent never does (decided 18 Nov 2026).

Story A2: the advisor's own Matrix account (``@advisor``) and one private, invite-only, unencrypted
room per person ("Claude Advisor") are made on the first ``up`` and kept in
``~/.config/logos/advisor-matrix.json`` (0600). A person's own Matrix id is the first of their
agent's ``matrix.allowed_users``; only that account can approve in their room.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import urllib.parse
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
MATRIX_FILE = "advisor-matrix.json"
LOCALPART = "advisor"
DISPLAY_NAME = "Claude Advisor"
ROOM_NAME = "Claude Advisor"


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


def admin_of(agents: Mapping[str, Mapping[str, Any]]) -> str:
    return next((person_of(a) for a in agents.values() if (a.get("advisor") or {}).get("admin")), "")


def mxid_of(agent: Mapping[str, Any]) -> str:
    users = (agent.get("matrix") or {}).get("allowed_users") or []
    return str(users[0]) if users else ""


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

    def matrix(self, agents: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        """The @advisor login and a room per person, made once. {} when Matrix is not in use here."""
        mh = _sibling("matrix_host")
        people = {person_of(a): mxid_of(a) for a in agents.values() if mxid_of(a)}
        if not people:
            return {}
        host = mh.MatrixHost(self.rt)
        try:
            if not host.running():
                return {}
            host.settings()
        except Exception:
            return {}
        path = config_dir(self.rt.env) / MATRIX_FILE
        state = json.loads(path.read_text()) if path.exists() else {}
        if not state.get("token"):
            password = secrets.token_urlsafe(32)
            login = host.register(LOCALPART, password)
            state = {"user_id": login["user_id"], "token": login["access_token"], "password": password, "rooms": {}}
        # The name shown in Element: set each time, so a rename reaches the account and rooms already made.
        host.set_display_name(state["user_id"], state["token"], DISPLAY_NAME)
        for person, have in (state.get("rooms") or {}).items():
            if have.get("room") and have.get("named") != ROOM_NAME:
                host._call(f"/_matrix/client/v3/rooms/{urllib.parse.quote(have['room'])}/state/m.room.name",
                           {"name": ROOM_NAME}, token=state["token"], method="PUT")
                have["named"] = ROOM_NAME
        for person, mxid in sorted(people.items()):
            have = state["rooms"].get(person) or {}
            if have.get("room") and have.get("mxid") == mxid:
                continue
            made = host._call("/_matrix/client/v3/createRoom", {
                "preset": "private_chat", "visibility": "private", "is_direct": True, "invite": [mxid],
                "name": ROOM_NAME, "topic": "Questions your agent wants to ask Claude. Reply send or no."},
                token=state["token"])
            if not made.get("room_id"):
                raise AdvisorError(f"could not make the advisor room for {person}: {made.get('error') or made}")
            state["rooms"][person] = {"room": made["room_id"], "mxid": mxid, "named": ROOM_NAME}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.chmod(0o600)
        os.replace(tmp, path)
        return {"url": host.url(), "token": state["token"], "user_id": state["user_id"],
                "rooms": {p: v for p, v in state["rooms"].items() if p in people}}

    def _matrix_or_none(self, agents: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        """Matrix approval is optional: a problem there leaves approval on the dashboard only."""
        try:
            return self.matrix(agents)
        except Exception as e:
            import sys
            print(f"warning: advisor Matrix room not set up ({e}); approval stays on the dashboard",
                  file=sys.stderr)
            return {}

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
                             "dashboard_token": dashboard_token(self.rt.env),
                             "admin": admin_of(agents), "matrix": self._matrix_or_none(agents)},
                            indent=1, sort_keys=True, ensure_ascii=False)
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


def _sibling(module: str):
    import importlib.util
    import sys
    name = f"logos_{module}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{module}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


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
