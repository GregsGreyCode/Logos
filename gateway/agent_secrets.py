"""An agent's secrets, and which of them it never sees (story E7, docs/secrets.md).

Each agent holds tokens for the Logos services it uses (noticeboard, voice, calendar, ...), its own
model-gateway key and its Matrix login. Until E7 every one of them sat in plain text in the agent's
home volume (``/opt/data/.env`` and ``config.yaml``), where the agent itself, or anything that took
over its tools, could read and repeat them.

OpenShell 0.1.8 credential placeholders change that. For each secret switched on here:

* the real value is kept on the host, in ``~/.config/logos/agent-secrets/<agent>.env`` (0600), which
  is also where the Logos services read it to check the agent's requests;
* Logos gives it to OpenShell as a provider (``logos-<agent>-<secret>``) whose profile binds it to
  exactly the endpoints the agent's sandbox rules already allow for that service;
* inside the sandbox the variable holds only a placeholder (``openshell:resolve:env:s..._NAME``).
  The sandbox proxy swaps in the real value in the ``Authorization`` header of a request to one of
  those endpoints, made by the hermes interpreter, and nowhere else. The same placeholder sent to
  another address is refused (``credential_endpoint_mismatch``), and one left in a request body is
  never resolved.

The placeholder is "stable" (the profile says ``stable_placeholder: true``), so rotating a secret
(``openshell provider update``, see ``scripts/logos-rotate``) reaches a running agent without a restart.

Secrets the agent itself must hold stay in its ``.env``: ``API_SERVER_KEY`` (it checks requests *to* it
with it) and ``MATRIX_RECOVERY_KEY`` (it decrypts its own cross-signing keys with it). The Matrix
password, which only Logos needs, now lives on the host only.
"""

from __future__ import annotations

import os
import secrets as _secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

PREFIX = "openshell:resolve:env:"

#: Switch name -> the variable the agent reads it from. Order is the order of the docs table.
SECRETS: Dict[str, str] = {
    "matrix": "MATRIX_ACCESS_TOKEN",
    "model": "LOGOS_MODEL_KEY",
    "noticeboard": "LOGOS_NOTICEBOARD_TOKEN",
    "voice": "LOGOS_VOICE_TOKEN",
    "calendar": "LOGOS_CALENDAR_TOKEN",
    "web": "LOGOS_WEB_TOKEN",
    "changes": "LOGOS_CHANGES_TOKEN",
    "house": "LOGOS_HOUSE_TOKEN",
    "mail": "LOGOS_MAIL_TOKEN",
    "advisor": "LOGOS_ADVISOR_TOKEN",
    "alert": "LOGOS_ALERT_TOKEN",
    "speaker_say": "LOGOS_SPEAKER_SAY_TOKEN",
}

#: On unless an agent (or agents/all.local.yaml) says otherwise: the ones proven end to end on the
#: dev machine (docs/secrets.md, "Placeholders"). The others are ready but wait for a check on the
#: machine that has the service (Home Assistant, a mailbox, the advisor): switch them on per agent.
DEFAULT_ON = frozenset({"matrix", "model", "noticeboard", "voice", "calendar", "web", "changes", "alert"})

#: Host-only: values Logos needs and the agent never does (moved out of the home volume's .env).
HOST_ONLY = ("LOGOS_MATRIX_PASSWORD", "LOGOS_MATRIX_ACCESS_TOKEN")

STORE_DIR = "agent-secrets"

#: Shell run first thing in the sandbox (agent_runtime.START): every placeholder OpenShell handed the
#: sandbox is written into /opt/data/.env, so hermes (which loads .env over its environment) and the
#: job scripts (which read .env directly) see the placeholder too. Nothing secret passes through it.
START_HOOK = (
    "e=/opt/data/.env; p=/tmp/.logos-placeholders; "
    "env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=" + PREFIX + "[A-Za-z0-9_]+$' > \"$p\" || true; "
    "if [ -s \"$p\" ]; then touch \"$e\"; "
    "awk -F= 'NR==FNR{k[$1]=1;next} !($1 in k)' \"$p\" \"$e\" > \"$e.new\" && cat \"$p\" >> \"$e.new\" && "
    "chmod 600 \"$e.new\" && mv \"$e.new\" \"$e\"; fi; rm -f \"$p\"; ")


def is_placeholder(value: Optional[str]) -> bool:
    return str(value or "").startswith(PREFIX)


def ref(env_var: str) -> str:
    """How config.yaml names a placeholder secret: hermes expands ``${VAR}`` from its environment."""
    return "${%s}" % env_var


# ── the switch ──────────────────────────────────────────────────────────

def switches(agent: Mapping[str, Any]) -> Dict[str, bool]:
    """Which secrets use placeholders for ``agent``.

    ``credential_placeholders`` in the definition (or a ``.local.yaml`` over it): ``false`` turns them
    all off, ``true`` all on, a mapping ``{calendar: false, house: true}`` changes single ones over the
    defaults (``all: false`` first, then single ones, also works).
    """
    raw = agent.get("credential_placeholders", None)
    on = {k: k in DEFAULT_ON for k in SECRETS}
    if isinstance(raw, bool):
        return {k: raw for k in SECRETS}
    if isinstance(raw, Mapping):
        if isinstance(raw.get("all"), bool):
            on = {k: bool(raw["all"]) for k in SECRETS}
        for k, v in raw.items():
            if k in SECRETS:
                on[k] = bool(v)
    return on


def enabled(agent: Mapping[str, Any], secret: str) -> bool:
    return switches(agent).get(secret, False)


def provider_name(agent: str, secret: str) -> str:
    return f"logos-{agent}-{secret.replace('_', '-')}"


def profile_id(agent: str, secret: str) -> str:
    return provider_name(agent, secret)


# ── the host store ──────────────────────────────────────────────────────

def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".config")
    return Path(base) / "logos"


def store_path(env: Mapping[str, str], name: str) -> Path:
    return config_dir(env) / STORE_DIR / f"{name}.env"


def parse(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def render(values: Mapping[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in sorted(values.items()))


def load(env: Mapping[str, str], name: str) -> Dict[str, str]:
    try:
        return parse(store_path(env, name).read_text())
    except OSError:
        return {}


def save(env: Mapping[str, str], name: str, values: Mapping[str, str]) -> None:
    """Write the agent's host-side secrets: directory 0700, file 0600, replaced atomically."""
    path = store_path(env, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    clean = {k: v for k, v in values.items() if v and not is_placeholder(v)}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(render(clean))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(env: Mapping[str, str], name: str, changes: Mapping[str, Optional[str]]) -> Dict[str, str]:
    """Set (or, with ``None``, remove) some of the agent's host-side secrets; returns them all."""
    cur = load(env, name)
    for k, v in changes.items():
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = v
    save(env, name, cur)
    return cur


def remove(env: Mapping[str, str], name: str) -> None:
    store_path(env, name).unlink(missing_ok=True)


def new_token() -> str:
    return _secrets.token_urlsafe(32)


def lookup(rt, name: str, key: str) -> str:
    """The real value of ``key`` for agent ``name``: the host store first, else (an agent not yet
    moved, or with that secret's placeholder off) its home volume's .env. Never a placeholder."""
    value = load(rt.env, name).get(key, "")
    if value:
        return value
    for line in rt.get(f"logos-home-{name}", ".env").splitlines():
        if line.startswith(key + "="):
            value = line.split("=", 1)[1].strip()
            return "" if is_placeholder(value) else value
    return ""


# ── the OpenShell side ──────────────────────────────────────────────────

#: Endpoint keys copied from a sandbox rule into the provider profile (the rest are policy-only).
ENDPOINT_KEYS = ("host", "port", "protocol", "enforcement", "access", "allowed_ips", "rules", "mcp",
                 "allow_encoded_slash", "tls")


def profile(agent: str, secret: str, endpoints: Iterable[Mapping[str, Any]],
            binaries: Iterable[str]) -> Dict[str, Any]:
    """The provider profile for one agent's secret: bearer placement, bound to ``endpoints``.

    ``endpoints`` are the agent's own sandbox rule endpoints for that service. OpenShell unites a
    profile's endpoints with the sandbox policy, so copying them adds no access; it only says where
    the credential may be sent."""
    env_var = SECRETS[secret]
    eps: List[Dict[str, Any]] = []
    for e in endpoints:
        eps.append({k: e[k] for k in ENDPOINT_KEYS if k in e})
    return {
        "id": profile_id(agent, secret),
        "display_name": f"Logos {secret} ({agent})",
        "description": f"{agent}'s {secret} credential, only for its own Logos {secret} endpoints",
        "category": "other",
        "credentials": [{
            "name": "token",
            "description": f"{env_var} for agent {agent}",
            "env_vars": [env_var],
            "required": True,
            "stable_placeholder": True,
            "auth_style": "bearer",
            "header_name": "Authorization",
        }],
        "endpoints": eps,
        "binaries": list(binaries),
    }
