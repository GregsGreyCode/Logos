"""The model gateway: one place every agent's model calls go through (S2.9).

It is a Bifrost container (https://github.com/maximhq/bifrost, pinned by
digest) that Logos configures entirely from files, so nobody has to click
through a UI:

* each agent whose definition has ``model.gateway: true`` gets its own key and
  a monthly budget (``budget.monthly_usd``). Over budget, the gateway answers
  HTTP 402 until the month rolls over;
* the local model server (LM Studio) is always a provider. Local calls cost
  nothing, so they never use up a budget;
* a paid provider is switched on by putting its API key in
  ``~/.config/logos/models.env`` (for example ``ANTHROPIC_API_KEY=...``). The
  real key stays in the gateway container; agents only ever hold their own
  capped key, and only agents that list the provider under ``model.allow`` may
  use it.

The gateway listens on the Docker bridge address, which sandboxes can reach
and the LAN cannot. Its admin API stays locked (no setup token is set).

Like :mod:`gateway.agent_runtime`, this module imports nothing from the rest of
``gateway`` and does every side effect through the runtime's ``Runner``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

IMAGE = "maximhq/bifrost@sha256:5ec9873acaf1687556b6e4dd854b92b539d9debf87a5a683b66338648b84d3c8"
CONTAINER = "logos-models"
VOLUME = "logos-models"
PORT = 8089
HELPER = "alpine:3"
GATEWAY_UID = "1000:0"                 # `appuser` in the Bifrost image
LOCAL_PROVIDER = "lm-studio"
KEY_ENV = "LOGOS_MODEL_KEY"            # the agent's own gateway key, kept in its home volume .env
DEFAULT_MONTHLY_USD = 5.0
GUARD_CONTAINER = "logos-model-guard"  # refuses at once when the local model server is off
GUARD_PORT = 8094
GUARD_FILE = Path(__file__).with_name("model_guard.py")
GUARD_UID = "10000:10000"
#: API-key variable in models.env -> Bifrost provider it switches on.
PAID_PROVIDERS = {
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
    "OPENROUTER_API_KEY": "openrouter",
    "GEMINI_API_KEY": "gemini",
}


def uses_gateway(agent: Mapping[str, Any]) -> bool:
    return bool((agent.get("model") or {}).get("gateway"))


def keys_file(env: Mapping[str, str]) -> Path:
    """Where paid provider keys live on the host (never in the repo)."""
    if env.get("LOGOS_MODELS_ENV"):
        return Path(env["LOGOS_MODELS_ENV"])
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".config")
    return Path(base) / "logos" / "models.env"


def paid_providers(env_text: str) -> Dict[str, str]:
    """``{provider: ENV_NAME}`` for every known key present in ``models.env`` text."""
    found: Dict[str, str] = {}
    for line in (env_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name in PAID_PROVIDERS and value.strip():
            found[PAID_PROVIDERS[name]] = name
    return found


def local_base_url(agent: Mapping[str, Any]) -> str:
    """LM Studio's server root (Bifrost adds ``/v1`` itself)."""
    return str(agent["model"]["base_url"]).rstrip("/").removesuffix("/v1")


def gateway_model(agent: Mapping[str, Any]) -> str:
    """The model name an agent asks the gateway for: ``<provider>/<model>``.

    A local model is prefixed with the local provider; a model already written
    as ``anthropic/claude-...`` (one of the paid providers) is passed through.
    """
    m = agent["model"]
    default = str(m["default"])
    head = default.split("/", 1)[0]
    if head in set(PAID_PROVIDERS.values()) and head in (m.get("allow") or []):
        return default
    return f"{LOCAL_PROVIDER}/{default}"


def local_server(agents: Mapping[str, Mapping[str, Any]]) -> str:
    """The one local model server the agents behind the gateway share (its ``/v1`` URL)."""
    bases = sorted({local_base_url(a) for a in agents.values()})
    if len(bases) > 1:
        raise ValueError("agents behind the gateway must share one local model server, found: " + ", ".join(bases))
    return bases[0]


def build_config(agents: Mapping[str, Mapping[str, Any]], keys: Mapping[str, str],
                 paid: Mapping[str, str], local_url: Optional[str] = None) -> dict:
    """The Bifrost ``config.json`` for these agents.

    ``agents`` maps name -> definition (only agents that use the gateway),
    ``keys`` maps name -> that agent's gateway key, ``paid`` is
    :func:`paid_providers`. ``local_url`` is where local model calls are sent: the model
    guard when it runs, otherwise the model server itself.
    """
    if not agents:
        raise ValueError("no agent uses the model gateway (set model.gateway: true in its definition)")
    local_url = local_url or local_server(agents)
    providers: Dict[str, Any] = {
        LOCAL_PROVIDER: {
            "keys": [{"name": "local", "value": "local", "models": ["*"], "weight": 1.0}],
            "network_config": {"base_url": local_url, "default_request_timeout_in_seconds": 900,
                               "allow_private_network": True},
            "custom_provider_config": {"base_provider_type": "openai", "allowed_requests": {
                "chat_completion": True, "chat_completion_stream": True}},
        }
    }
    for provider, env_name in sorted(paid.items()):
        providers[provider] = {"keys": [{"name": f"{provider}-key", "value": f"env.{env_name}",
                                         "models": ["*"], "weight": 1.0}]}
    vks: List[dict] = []
    budgets: List[dict] = []
    for name in sorted(agents):
        agent = agents[name]
        allowed = [p for p in ((agent.get("model") or {}).get("allow") or []) if p in paid]
        vks.append({
            "id": f"vk-{name}", "name": f"agent-{name}", "value": keys[name], "is_active": True,
            "provider_configs": [{"provider": p, "weight": 1.0, "allowed_models": ["*"], "key_ids": ["*"]}
                                 for p in [LOCAL_PROVIDER, *allowed]],
        })
        monthly = float((agent.get("budget") or {}).get("monthly_usd", DEFAULT_MONTHLY_USD))
        if monthly <= 0:
            raise ValueError(f"{name}: budget.monthly_usd must be above 0")
        budgets.append({"id": f"budget-{name}", "virtual_key_id": f"vk-{name}",
                        "max_limit": monthly, "reset_duration": "1M"})
    return {
        "client": {"enforce_auth_on_inference": True},
        "providers": providers,
        "governance": {"virtual_keys": vks, "budgets": budgets},
    }


class ModelGateway:
    """Run and configure the gateway container. ``runtime`` is an
    :class:`~gateway.agent_runtime.AgentRuntime` (for its runner, env and volume helpers)."""

    def __init__(self, runtime: Any):
        self.rt = runtime

    # ── facts ────────────────────────────────────────────────────────────

    def bind_address(self) -> str:
        """The Docker bridge address: reachable from sandboxes, not from the LAN."""
        out = self.rt.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"], check=False).strip()
        return out or "172.17.0.1"

    def base_url(self) -> str:
        return f"http://{self.bind_address()}:{PORT}/v1"

    def running(self) -> bool:
        out = self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                          check=False)
        return CONTAINER in out.split()

    def agents(self) -> Dict[str, dict]:
        """Every defined agent that goes through the gateway."""
        found = {}
        for name in self.rt.defined():
            agent = self.rt.load(name)
            if uses_gateway(agent):
                found[name] = agent
        return found

    def key_for(self, name: str) -> str:
        home = f"logos-home-{name}"
        for line in self.rt.get(home, ".env").splitlines():
            if line.startswith(KEY_ENV + "="):
                return line.split("=", 1)[1].strip()
        return ""

    def _keys_text(self) -> str:
        path = keys_file(self.rt.env)
        return path.read_text() if path.exists() else ""

    # ── lifecycle ────────────────────────────────────────────────────────

    def up(self) -> bool:
        """Write the config and (re)start the container if anything changed.
        Returns True when the container was started or restarted."""
        agents = self.agents()
        keys = {n: self.key_for(n) for n in agents}
        # An agent that has never been started has no key yet; it joins on its first `up`.
        agents = {n: a for n, a in agents.items() if keys[n]}
        if not agents:
            raise ValueError("no agent behind the gateway has a key yet (run scripts/logos-agent up <name> once)")
        keys_text = self._keys_text()
        image = next((str(a["image"]) for a in agents.values() if a.get("image")), "")
        guard_url = self.guard_up(local_server(agents), image) if image else None
        config = json.dumps(build_config(agents, keys, paid_providers(keys_text), guard_url),
                            indent=1, sort_keys=True)
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", VOLUME])
            self.rt.run(["docker", "run", "--rm", "-v", f"{VOLUME}:/v", HELPER, "chown", GATEWAY_UID, "/v"])
        current = self.rt.runner.run(["docker", "run", "--rm", "-v", f"{VOLUME}:/v:ro", HELPER,
                                      "cat", "/v/config.json"])
        changed = current.returncode != 0 or current.stdout != config
        # The keys file is passed as the container's environment, so a new or
        # changed provider key also needs a fresh container.
        stamp = hashlib.sha256(keys_text.encode()).hexdigest()[:16]
        if changed:
            self.rt.run(["docker", "run", "--rm", "-i", "-v", f"{VOLUME}:/v", HELPER, "sh", "-c",
                         f"cat > /v/config.json && chown {GATEWAY_UID} /v/config.json && chmod 0600 /v/config.json"],
                        input=config)
        if self.running() and not changed and self._label() == stamp:
            return False
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        cmd = ["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
               "--label", f"logos.models.keys={stamp}",
               "-p", f"{self.bind_address()}:{PORT}:8080", "-v", f"{VOLUME}:/app/data"]
        path = keys_file(self.rt.env)
        if path.exists():
            cmd += ["--env-file", str(path)]
        self.rt.run(cmd + [IMAGE])
        self.wait()
        return True

    def guard_up(self, upstream: str, image: str) -> str:
        """Run the model guard in front of ``upstream`` and return the URL the gateway should use.

        The guard (:mod:`gateway.model_guard`) passes calls through, and answers at once
        with a clear error when the model server is not accepting connections. It runs in
        the agent image, which already has Python, with its one file mounted read-only.
        """
        stamp = hashlib.sha256((upstream + GUARD_FILE.read_text() + image).encode()).hexdigest()[:16]
        names = self.rt.run(["docker", "ps", "--filter", f"name=^{GUARD_CONTAINER}$", "--format", "{{.Names}}"],
                            check=False).split()
        label = self.rt.run(["docker", "inspect", GUARD_CONTAINER, "-f",
                             '{{index .Config.Labels "logos.guard.stamp"}}'], check=False).strip()
        url = f"http://{self.bind_address()}:{GUARD_PORT}/v1"
        if GUARD_CONTAINER in names and label == stamp:
            return url
        self.rt.run(["docker", "rm", "-f", GUARD_CONTAINER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", GUARD_CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.guard.stamp={stamp}", "--user", GUARD_UID, "--read-only",
                     "-p", f"{self.bind_address()}:{GUARD_PORT}:{GUARD_PORT}",
                     "-v", f"{GUARD_FILE}:/app/model_guard.py:ro",
                     "-e", f"GUARD_UPSTREAM={upstream.rsplit('/v1', 1)[0]}", "-e", f"GUARD_PORT={GUARD_PORT}",
                     "--entrypoint", "python3", image, "/app/model_guard.py"])
        return url

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f",
                            '{{index .Config.Labels "logos.models.keys"}}'], check=False).strip()

    def wait(self, tries: int = 40) -> None:
        url = f"http://{self.bind_address()}:{PORT}/v1/models"
        for _ in range(tries):
            try:
                self.rt.runner.http(url, timeout=3)
                return
            except Exception as e:  # 401 without a key means it is up
                if "401" in str(e) or "403" in str(e):
                    return
            self.rt.runner.sleep(1)
        raise TimeoutError("the model gateway did not start; see: docker logs " + CONTAINER)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        self.rt.run(["docker", "rm", "-f", GUARD_CONTAINER], check=False)

    def status(self) -> str:
        if not self.running():
            return "model gateway: not running"
        paid = sorted(paid_providers(self._keys_text()))
        lines = [f"model gateway: running at {self.base_url()}",
                 "paid providers: " + (", ".join(paid) if paid else "none (local models only)")]
        for name, agent in sorted(self.agents().items()):
            monthly = float((agent.get("budget") or {}).get("monthly_usd", DEFAULT_MONTHLY_USD))
            allow = [p for p in (agent["model"].get("allow") or []) if p in paid]
            lines.append(f"  {name}: ${monthly:.2f} a month, may use local"
                         + ("".join(f", {p}" for p in allow)))
        return "\n".join(lines)
