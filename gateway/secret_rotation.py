"""Rotate the secrets Logos makes itself (story E7; the runbook is docs/secrets.md).

Behind ``scripts/logos-rotate``. Each rotation makes a new value, puts it on both sides, and restarts
only what has to restart:

* a secret the agent holds as an OpenShell placeholder (gateway/agent_secrets.py): the host store and
  the agent's provider get the new value (``openshell provider update --wait``: the running agent uses
  it from its next request, with no restart), then the one service that checks it is restarted;
* a secret in the agent's ``.env``: the new value goes there and the agent is recreated
  (``up --no-model-load``), which also restarts the services it uses.

Never prints a secret value.
"""

from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

#: Per-agent secrets: name -> (env var, the host module and class whose ``up()`` restarts the checker).
SERVICE_SECRETS: Dict[str, tuple] = {
    "noticeboard": ("LOGOS_NOTICEBOARD_TOKEN", "noticeboard_host", "NoticeboardHost"),
    "voice": ("LOGOS_VOICE_TOKEN", "voice_host", "VoiceHost"),
    "calendar": ("LOGOS_CALENDAR_TOKEN", "calendar_host", "CalendarHost"),
    "web": ("LOGOS_WEB_TOKEN", "web_host", "WebHost"),
    "changes": ("LOGOS_CHANGES_TOKEN", "web_host", "WebHost"),
    "house": ("LOGOS_HOUSE_TOKEN", "house_host", "HouseHost"),
    "mail": ("LOGOS_MAIL_TOKEN", "mail_host", "MailHost"),
    "advisor": ("LOGOS_ADVISOR_TOKEN", "advisor_host", "AdvisorHost"),
    "model": ("LOGOS_MODEL_KEY", "model_gateway", "ModelGateway"),
}
#: Also per agent, but special: the agent's own API key, and its Matrix access token.
AGENT_SECRETS = tuple(SERVICE_SECRETS) + ("api", "matrix")


class RotateError(Exception):
    pass


def new_value(secret: str) -> str:
    token = _secrets.token_urlsafe(32)
    return "sk-bf-" + token if secret == "model" else token


class Rotator:
    def __init__(self, rt, say: Callable[[str], None] = print):
        self.rt = rt
        self.say = say
        self.sec = rt_sibling(rt, "agent_secrets")

    # ── helpers ───────────────────────────────────────────────────────────

    def _env(self, name: str) -> Dict[str, str]:
        return self.sec.parse(self.rt.get(f"logos-home-{name}", ".env"))

    def _set_env(self, name: str, changes: Mapping[str, str]) -> None:
        cur = self._env(name)
        cur.update(changes)
        self.rt.put(f"logos-home-{name}", ".env", self.sec.render(cur), mode="0600")

    def _placeholder(self, name: str, key: str) -> bool:
        """True when ``key`` reaches agent ``name`` as a placeholder (its provider exists)."""
        secret = next((s for s, k in self.sec.SECRETS.items() if k == key), None)
        return bool(secret) and self.sec.provider_name(name, secret) in self.rt.providers_for(name) \
            and bool(self.sec.load(self.rt.env, name).get(key))

    def _push_provider(self, name: str, key: str, value: str) -> None:
        secret = next(s for s, k in self.sec.SECRETS.items() if k == key)
        pname = self.sec.provider_name(name, secret)
        self.rt._openshell_secret(["openshell", "provider", "update", pname, "--credential", key, "--wait"],
                                  key, value)
        self.say(f"{name}: {pname} updated; the running agent uses the new value from its next request")

    def _recreate(self, name: str) -> None:
        self.say(f"{name}: recreating the agent so it reads the new value")
        if self.rt.up(name, load_model=False) != 0:
            raise RotateError(f"{name}: the agent did not come back Ready; see scripts/logos-agent status {name}")

    def _restart_service(self, module: str, cls: str) -> None:
        mod = rt_sibling(self.rt, module)
        started = getattr(mod, cls)(self.rt).up()
        self.say(f"{module.replace('_', ' ')}: {'restarted with the new value' if started else 'already current'}")

    # ── rotations ─────────────────────────────────────────────────────────

    def rotate(self, name: str, secret: str) -> None:
        if name not in self.rt.defined():
            raise RotateError(f"no agent {name}")
        if secret == "api":
            return self.rotate_api(name)
        if secret == "matrix":
            return self.rotate_matrix(name)
        if secret not in SERVICE_SECRETS:
            raise RotateError(f"unknown secret {secret}; one of: {', '.join(AGENT_SECRETS)}")
        key, module, cls = SERVICE_SECRETS[secret]
        if not self.rt.secret(name, key):
            raise RotateError(f"{name} has no {secret} secret (is the service on for it?)")
        value = new_value(secret)
        if self._placeholder(name, key):
            self.sec.update(self.rt.env, name, {key: value})
            self._push_provider(name, key, value)
            self._restart_service(module, cls)
        else:
            self._set_env(name, {key: value})
            self._recreate(name)
        self.say(f"{name}: {secret} rotated")

    def rotate_api(self, name: str) -> None:
        """The agent checks it itself, so it always restarts; the dashboard re-reads it with the new link."""
        self._set_env(name, {"API_SERVER_KEY": _secrets.token_urlsafe(32)})
        self._recreate(name)
        self.say(f"{name}: api rotated (if its chat in the dashboard says it is not allowed in, restart "
                 "the dashboard: systemctl --user restart logos-gateway)")

    def rotate_matrix(self, name: str) -> None:
        """A new access token for the same device: its encryption keys and verified status stay."""
        host = self.sec.load(self.rt.env, name)
        env = self._env(name)
        user = env.get("LOGOS_MATRIX_USER_ID") or env.get("MATRIX_USER_ID", "")
        device = env.get("LOGOS_MATRIX_DEVICE_ID") or env.get("MATRIX_DEVICE_ID", "")
        password = host.get("LOGOS_MATRIX_PASSWORD") or env.get("LOGOS_MATRIX_PASSWORD", "")
        mh = rt_sibling(self.rt, "matrix_host")
        try:
            token = mh.MatrixHost(self.rt).relogin(user, password, device)
        except (mh.MatrixError, OSError) as e:
            raise RotateError(f"{name}: matrix: {e}")
        self.say(f"{name}: new Matrix access token for device {device}; the server ended the old one")
        if self._placeholder(name, "MATRIX_ACCESS_TOKEN"):
            self.sec.update(self.rt.env, name, {"MATRIX_ACCESS_TOKEN": token, "LOGOS_MATRIX_ACCESS_TOKEN": token})
            self._push_provider(name, "MATRIX_ACCESS_TOKEN", token)
        else:
            self.sec.update(self.rt.env, name, {"LOGOS_MATRIX_ACCESS_TOKEN": token})
            self._set_env(name, {"MATRIX_ACCESS_TOKEN": token})
            self._recreate(name)
        self.say(f"{name}: matrix rotated")

    def rotate_alert_key(self, running: Optional[List[str]] = None) -> None:
        """A new alert key: every agent's phone-alert and speaker tokens change (they are HMACs of it)."""
        at = rt_sibling(self.rt, "alert_text")
        ss = rt_sibling(self.rt, "speaker_say")
        path = Path(at.key_path(self.rt.env)) if hasattr(at, "key_path") else None
        if path is None:
            raise RotateError("this Logos has no alert key path")
        tmp = path.with_name(path.name + ".new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(_secrets.token_urlsafe(32) + "\n")
        os.replace(tmp, path)
        self.say("alert key replaced; the dashboard checks with it from now on")
        phases = self.rt.phases()
        for name in running if running is not None else [n for n in self.rt.defined() if phases.get(n) == "Ready"]:
            agent = self.rt.load(name)
            if not (agent.get("matrix") or {}).get("enabled"):
                continue
            if self._placeholder(name, at.TOKEN_ENV):
                changes = {at.TOKEN_ENV: at.token_for(name, self.rt.env)}
                if ss.speaks(agent) and self._placeholder(name, ss.TOKEN_ENV):
                    changes[ss.TOKEN_ENV] = ss.token_for(name, key=at.key(self.rt.env))
                elif ss.speaks(agent):
                    self._recreate(name)
                    continue
                self.sec.update(self.rt.env, name, changes)
                for k, v in changes.items():
                    self._push_provider(name, k, v)
            else:
                self._recreate(name)

    # ── what is where ─────────────────────────────────────────────────────

    def status(self, name: str) -> List[tuple]:
        """``(secret, where)`` for agent ``name``: "placeholder", ".env", "host" or "-". No values."""
        host = self.sec.load(self.rt.env, name)
        env = self._env(name)
        providers = set(self.rt.providers_for(name))
        rows = []
        for secret, key in self.sec.SECRETS.items():
            if self.sec.provider_name(name, secret) in providers and host.get(key):
                where = "placeholder (real value on the host only)"
            elif env.get(key) and not self.sec.is_placeholder(env[key]):
                where = ".env in the home volume"
            else:
                where = "-"
            rows.append((secret, where))
        rows.append(("api", ".env in the home volume" if env.get("API_SERVER_KEY") else "-"))
        rows.append(("matrix password", "host only" if host.get("LOGOS_MATRIX_PASSWORD")
                     else ".env in the home volume" if env.get("LOGOS_MATRIX_PASSWORD") else "-"))
        rows.append(("matrix recovery key", ".env + matrix-recovery-key in the home volume"
                     if env.get("MATRIX_RECOVERY_KEY") else "-"))
        return rows


def rt_sibling(rt, module: str):
    """A gateway module loaded the way agent_runtime loads its neighbours."""
    import sys
    mod = sys.modules.get(type(rt).__module__)
    loader = getattr(mod, "_sibling", None)
    if loader is None:
        import importlib
        return importlib.import_module(f"gateway.{module}")
    return loader(module)
