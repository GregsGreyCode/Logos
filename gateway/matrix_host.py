"""Run the household's own Matrix server and give agents accounts on it.

Matrix is the way to talk to agents: the server runs here, the phones
reach it only over Tailscale, it never federates with other servers, and hermes's Matrix
adapter speaks end-to-end encrypted. People use the Element app.

The server is Continuwuity (one container, embedded database) in the ``logos-matrix``
container and volume. It listens on localhost (for ``tailscale serve``, which gives it its
HTTPS address on the tailnet) and on the Docker bridge (for agent sandboxes). The LAN
cannot reach it.

Settings live in ``~/.config/logos/matrix.env`` (never in the repo)::

    MATRIX_SERVER_NAME=example.org          # permanent: part of every Matrix ID
    MATRIX_CLIENT_URL=https://<device>.<tailnet>.ts.net  # where Element reaches it (tailscale serve)
    MATRIX_REGISTRATION_TOKEN=...               # people sign up in Element with this
    MATRIX_ADMIN_PASSWORD=...                   # @logos-admin, the server's first account

The server name never has to resolve: with federation off, nothing looks it up. People type
the client URL into Element; the server's /.well-known answers with that same URL.

An agent with ``matrix: {enabled: true}`` gets its own account (``@<name>-agent``) on its
first ``up``. The login is kept in the agent's home volume.
"""

from __future__ import annotations

import hashlib
import json
import time
import os
import re
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


# continuwuity's default denylist with 172.16.0.0/12 split so that 172.17.0.0/16 (the Docker bridge,
# where the dashboard listens) is left out.
IP_DENYLIST = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/16", "172.18.0.0/15", "172.20.0.0/14", "172.24.0.0/13",
               "192.168.0.0/16", "100.64.0.0/10", "192.0.0.0/24", "169.254.0.0/16", "192.88.99.0/24", "198.18.0.0/15",
               "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "::1/128", "fe80::/10", "fc00::/7",
               "2001:db8::/32", "ff00::/8", "fec0::/10"]


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
        "CONDUWUIT_NEW_USER_DISPLAYNAME_SUFFIX": "",                  # no emoji added to new accounts' names
        "CONDUWUIT_ALLOW_ANNOUNCEMENTS_CHECK": "false",               # no periodic call to continuwuity.org
        # Outgoing requests may not go to private addresses, as by default, except the Docker bridge:
        # the dashboard there is the push gateway for Logos's phone notifications (F37). With
        # federation off, push is the only thing the server sends anywhere.
        "CONDUWUIT_IP_RANGE_DENYLIST": json.dumps(IP_DENYLIST),
    }


def agent_display_name(agent: Mapping[str, Any]) -> str:
    """What the agent is called in Element: ``matrix.display_name``, else its name with a robot."""
    mx = agent.get("matrix") or {}
    if mx.get("display_name"):
        return str(mx["display_name"])
    return f"{agent.get('display_name') or agent.get('name')} \U0001F916"


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

    def _call(self, path: str, body: Optional[dict] = None, token: str = "", method: str = "") -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        try:
            extra = {"method": method} if method else {}
            raw = self.rt.runner.http(f"http://127.0.0.1:{PORT}{path}", data=data, headers=headers, timeout=15,
                                      **extra)
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

    def reset_password(self, localpart: str, sleep=time.sleep) -> str:
        """Give someone a new Matrix password (they forgot theirs) and return it. Their other sign-ins stay
        signed in (no --logout) and their encryption is untouched; the recovery key is separate.

        Done as @logos-admin with Continuwuity's own admin command in the server's admin room. The command
        and the server's answer are redacted straight after, so the password does not stay in that room's
        history; the new password is proven by signing in with it once."""
        if not re.fullmatch(r"[a-z0-9._=-]{1,64}", localpart or ""):
            raise MatrixError("give the part before the colon, e.g. bere")
        if localpart == ADMIN:
            raise MatrixError(f"@{ADMIN}'s own password lives in the settings file, not here")
        st = self.settings()
        server = st["MATRIX_SERVER_NAME"]
        admin_pw = st.get("MATRIX_ADMIN_PASSWORD")
        if not admin_pw:
            raise MatrixError("MATRIX_ADMIN_PASSWORD is not set, so Logos cannot act as the server's admin")
        login = self._call("/_matrix/client/v3/login", {"type": "m.login.password",
                                                        "identifier": {"type": "m.id.user", "user": ADMIN}, "password": admin_pw})
        token = login.get("access_token")
        if not token:
            raise MatrixError(f"@{ADMIN} could not sign in: {login.get('error') or login}")
        password = secrets.token_urlsafe(15)
        try:
            alias = urllib.parse.quote(f"#admins:{server}")
            room = self._call(f"/_matrix/client/v3/directory/room/{alias}", token=token).get("room_id")
            if not room:
                raise MatrixError("the server's admin room (#admins) was not found")
            rid = urllib.parse.quote(room)
            sent = self._call(f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/logos-reset-{secrets.token_hex(6)}",
                              {"msgtype": "m.text", "body": f"!admin users reset-password {localpart} {password}"},
                              token=token, method="PUT")
            answer, answer_ids = "", []
            for _ in range(60):                                  # up to 30 s: a busy server answers late
                sleep(0.5)
                # Newest first: only what came after our command (before it in this list) is its answer;
                # an older message from the server (an earlier reset, a notice) is never taken for it.
                for ev in self._call(f"/_matrix/client/v3/rooms/{rid}/messages?dir=b&limit=20", token=token).get("chunk", []):
                    if ev.get("event_id") == sent.get("event_id"):
                        break
                    if ev.get("sender") != login.get("user_id") and ev.get("type") == "m.room.message":
                        answer = (ev.get("content") or {}).get("body", "")
                        answer_ids.append(ev.get("event_id"))
                if answer_ids:
                    break
            for eid in [sent.get("event_id"), *answer_ids]:      # the password does not stay in the room
                if eid:
                    self._call(f"/_matrix/client/v3/rooms/{rid}/redact/{urllib.parse.quote(eid)}/logos-redact-{secrets.token_hex(6)}",
                               {"reason": "password reset"}, token=token, method="PUT")
        finally:
            self._call("/_matrix/client/v3/logout", {}, token=token)
        check = self._call("/_matrix/client/v3/login", {"type": "m.login.password",
                                                        "identifier": {"type": "m.id.user", "user": localpart}, "password": password,
                                                        "initial_device_display_name": "Logos password check"})
        if "access_token" not in check:
            raise MatrixError(f"the reset did not take: {answer.splitlines()[0] if answer else check.get('error') or check}")
        self._call("/_matrix/client/v3/logout", {}, token=check["access_token"])
        return password

    def set_display_name(self, user_id: str, token: str, name: str) -> bool:
        """Set an account's display name when it differs. True when it was changed."""
        path = f"/_matrix/client/v3/profile/{urllib.parse.quote(user_id)}/displayname"
        if self._call(path, token=token).get("displayname") == name:
            return False
        done = self._call(path, {"displayname": name}, token=token, method="PUT")
        if done.get("errcode"):
            raise MatrixError(f"could not rename {user_id}: {done.get('error') or done}")
        return True

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
