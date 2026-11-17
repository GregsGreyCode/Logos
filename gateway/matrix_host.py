"""Run the household's own Matrix server and give agents accounts on it.

Matrix replaces Telegram as the way to talk to agents: the server runs here, the phones
reach it only over Tailscale, it never federates with other servers, and hermes's Matrix
adapter speaks end-to-end encrypted. People use the Element app.

The server is Continuwuity (one container, embedded database) in the ``logos-matrix``
container and volume. It listens on localhost (for ``tailscale serve``, which gives it its
HTTPS address on the tailnet) and on the Docker bridge (for agent sandboxes). The LAN
cannot reach it.

Settings live in ``~/.config/logos/matrix.env`` (never in the repo)::

    MATRIX_SERVER_NAME=example.org          # permanent: part of every Matrix ID
    MATRIX_CLIENT_URL=https://logos.<tailnet>.ts.net  # where Element reaches it (tailscale serve)
    MATRIX_REGISTRATION_TOKEN=...               # people sign up in Element with this
    MATRIX_ADMIN_PASSWORD=...                   # @logos-admin, the server's first account

The server name never has to resolve: with federation off, nothing looks it up. People type
the client URL into Element; the server's /.well-known answers with that same URL.

An agent with ``matrix: {enabled: true}`` gets its own account (``@<name>-agent``) on its
first ``up``. The login is kept in the agent's home volume, like the Telegram token.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

CONTAINER = "logos-matrix"
VOLUME = "logos-matrix"
#: Continuwuity v26.9.4, pinned by digest.
IMAGE = "ghcr.io/continuwuity/continuwuity@sha256:5f21a556290df5b6eda30c7d695288f5c4b06f9529a64e2cae040297287496c3"
PORT = 6167
ADMIN = "logos-admin"


def uses_matrix(agent: Mapping[str, Any]) -> bool:
    return bool((agent.get("matrix") or {}).get("enabled"))


def settings_file(env: Mapping[str, str]) -> Path:
    if env.get("LOGOS_MATRIX_ENV"):
        return Path(env["LOGOS_MATRIX_ENV"])
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".config")
    return Path(base) / "logos" / "matrix.env"


def parse_env(text: str) -> Dict[str, str]:
    return dict(l.split("=", 1) for l in (text or "").splitlines() if "=" in l and not l.startswith("#"))


def server_env(settings: Mapping[str, str]) -> Dict[str, str]:
    """The container's environment: a private, invite-by-token server with no federation."""
    name = settings["MATRIX_SERVER_NAME"]
    return {
        "CONDUWUIT_SERVER_NAME": name,
        "CONDUWUIT_DATABASE_PATH": "/var/lib/conduwuit",
        "CONDUWUIT_ADDRESS": '["0.0.0.0"]',
        "CONDUWUIT_PORT": str(PORT),
        "CONDUWUIT_ALLOW_FEDERATION": "false",
        "CONDUWUIT_TRUSTED_SERVERS": "[]",
        "CONDUWUIT_ALLOW_REGISTRATION": "true",
        "CONDUWUIT_REGISTRATION_TOKEN": settings["MATRIX_REGISTRATION_TOKEN"],
        "CONDUWUIT_ALLOW_ENCRYPTION": "true",
        "CONDUWUIT_ALLOW_PUBLIC_ROOM_DIRECTORY_OVER_FEDERATION": "false",
        "CONDUWUIT_URL_PREVIEW_DOMAIN_CONTAINS_ALLOWLIST": "[]",     # no link previews: nothing fetched
        "CONDUWUIT_WELL_KNOWN__CLIENT": settings.get("MATRIX_CLIENT_URL") or f"https://{name}",
    }


def first_user_token(logs: str) -> str:
    """The one-time token a fresh server prints for creating its first (admin) account."""
    import re
    clean = re.sub(r"\x1b\[[0-9;]*m", "", logs or "")
    m = re.search(r"using the registration token\W+(\w+)", clean)
    return m.group(1) if m else ""


class MatrixError(Exception):
    pass


class MatrixHost:
    """``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`."""

    def __init__(self, runtime: Any):
        self.rt = runtime

    # ── facts ────────────────────────────────────────────────────────────

    def bind_address(self) -> str:
        out = self.rt.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"], check=False).strip()
        return out or "172.17.0.1"

    def url(self) -> str:
        """Where agents (and Logos) reach the server: the Docker bridge, plain HTTP."""
        return f"http://{self.bind_address()}:{PORT}"

    def settings(self) -> Dict[str, str]:
        path = settings_file(self.rt.env)
        found = parse_env(path.read_text()) if path.exists() else {}
        for key in ("MATRIX_SERVER_NAME", "MATRIX_REGISTRATION_TOKEN"):
            if not found.get(key):
                raise MatrixError(f"{key} is not set in {path} (see gateway/matrix_host.py)")
        return found

    def running(self) -> bool:
        out = self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                          check=False)
        return CONTAINER in out.split()

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f",
                            '{{index .Config.Labels "logos.matrix.stamp"}}'], check=False).strip()

    # ── lifecycle ────────────────────────────────────────────────────────

    def up(self) -> bool:
        """Start (or restart, when its settings changed) the server. True when (re)started."""
        env = server_env(self.settings())
        stamp = hashlib.sha256((json.dumps(env, sort_keys=True) + IMAGE).encode()).hexdigest()[:16]
        if self.running() and self._label() == stamp:
            return False
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", VOLUME])
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        cmd = ["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
               "--label", f"logos.matrix.stamp={stamp}",
               "-p", f"127.0.0.1:{PORT}:{PORT}", "-p", f"{self.bind_address()}:{PORT}:{PORT}",
               "-v", f"{VOLUME}:/var/lib/conduwuit"]
        for k, v in sorted(env.items()):
            cmd += ["-e", f"{k}={v}"]
        self.rt.run(cmd + [IMAGE])
        self.wait()
        self.ensure_admin()
        return True

    def wait(self, tries: int = 30) -> None:
        for _ in range(tries):
            try:
                self.rt.runner.http(f"http://127.0.0.1:{PORT}/_matrix/client/versions", timeout=3)
                return
            except OSError:
                self.rt.runner.sleep(1)
        raise TimeoutError("the Matrix server did not answer")

    # ── accounts ─────────────────────────────────────────────────────────

    def _call(self, path: str, body: Optional[dict] = None, token: str = "") -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        try:
            raw = self.rt.runner.http(f"http://127.0.0.1:{PORT}{path}", data=data, headers=headers, timeout=15)
        except urllib.error.HTTPError as e:
            raw = e.read()
        return json.loads(raw or b"{}")

    def user_id(self, localpart: str) -> str:
        return f"@{localpart}:{self.settings()['MATRIX_SERVER_NAME']}"

    def register(self, localpart: str, password: str, token: str = "") -> dict:
        """Create an account with the registration token. Returns the login (access token,
        device id) or raises MatrixError."""
        token = token or self.settings()["MATRIX_REGISTRATION_TOKEN"]
        first = self._call("/_matrix/client/v3/register", {"username": localpart, "password": password})
        if "access_token" in first:
            return first
        done = self._call("/_matrix/client/v3/register", {
            "username": localpart, "password": password,
            "initial_device_display_name": "Logos agent",
            "auth": {"type": "m.login.registration_token", "token": token, "session": first.get("session")}})
        if "access_token" not in done:
            raise MatrixError(f"could not create @{localpart}: {done.get('error') or done}")
        return done

    def ensure_admin(self) -> None:
        """Make @logos-admin the server's first account (its admin), when it does not exist.

        A new Continuwuity server prints a one-time token for that first sign-up in its log."""
        password = self.settings().get("MATRIX_ADMIN_PASSWORD")
        if not password:
            return
        login = self._call("/_matrix/client/v3/login", {
            "type": "m.login.password", "identifier": {"type": "m.id.user", "user": ADMIN},
            "password": password})
        if "access_token" in login:
            self._call("/_matrix/client/v3/logout", {}, token=login["access_token"])
            return
        p = self.rt.runner.run(["docker", "logs", CONTAINER])
        logs = (p.stdout or "") + (p.stderr or "")
        first = first_user_token(logs)
        if not first:
            raise MatrixError(f"@{ADMIN} cannot sign in and the server offers no first-user token")
        self.register(ADMIN, password, token=first)

    def agent_login(self, name: str, current: Mapping[str, str]) -> Dict[str, str]:
        """The MATRIX_* values for this agent's .env: the stored ones, or a new account."""
        if current.get("MATRIX_ACCESS_TOKEN") and current.get("MATRIX_USER_ID"):
            return {k: current[k] for k in ("MATRIX_ACCESS_TOKEN", "MATRIX_USER_ID", "MATRIX_DEVICE_ID",
                                            "MATRIX_PASSWORD") if k in current}
        localpart = f"{name}-agent"
        password = secrets.token_urlsafe(32)
        login = self.register(localpart, password)
        return {"MATRIX_ACCESS_TOKEN": login["access_token"], "MATRIX_USER_ID": login["user_id"],
                "MATRIX_DEVICE_ID": login.get("device_id", ""), "MATRIX_PASSWORD": password}
