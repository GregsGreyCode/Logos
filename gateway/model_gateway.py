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

REQUEST_TIMEOUT = 900                   # seconds: a whole reply, and the longest silence within one
IMAGE = "maximhq/bifrost@sha256:5ec9873acaf1687556b6e4dd854b92b539d9debf87a5a683b66338648b84d3c8"
CONTAINER = "logos-models"
VOLUME = "logos-models"
PORT = 8089
HELPER = "alpine:3"
GATEWAY_UID = "1000:0"                 # `appuser` in the Bifrost image
LOCAL_PROVIDER = "lm-studio"
KEY_ENV = "LOGOS_MODEL_KEY"            # the agent's own gateway key, kept in its home volume .env
DEFAULT_MONTHLY_USD = 5.0
GUARD_CONTAINER = "logos-model-guard"  # waits up to a minute for the local model server, then refuses
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


#: Every agent request carries its memories and brain index, so any request that reaches a
#: paid provider would take the brain out of the house. Until a separate brain-free advisor
#: exists (one that only ever sees questions the private agent writes for it), agents are
#: local-only: no fallbacks, no paid models, no provider keys. Not a setting: changing this
#: is a design decision, with its own review.
BRAIN_STAYS_LOCAL = True
LOCAL_ONLY_PROVIDERS = ("lmstudio",)
#: OpenShell provider profiles that would give the sandbox a cloud model key of its own.
MODEL_PROVIDER_PROFILES = {"anthropic", "openai", "openrouter", "gemini", "google", "nous", "xai",
                           "deepseek", "mistral", "groq", "together", "fireworks"}


def local_only_problems(name: str, agent: Mapping[str, Any]) -> List[str]:
    """Why this agent definition would let a request leave the house (empty when it would not)."""
    model = agent.get("model") or {}
    problems = []
    if model.get("fallbacks"):
        problems.append("model.fallbacks lists paid models")
    paid = set(PAID_PROVIDERS.values())
    if any(p in paid for p in (model.get("allow") or [])):
        problems.append("model.allow lists a paid provider")
    model_keys = [p for p in (agent.get("providers") or []) if str(p) in MODEL_PROVIDER_PROFILES]
    if model_keys:
        problems.append("providers attaches a model provider key to the sandbox: " + ", ".join(model_keys))
    if str(model.get("provider") or "") not in LOCAL_ONLY_PROVIDERS:
        problems.append(f"model.provider is {model.get('provider')!r}, not a local model server")
    for job in ("consolidation", "digest"):
        cfg = agent.get(job) or {}
        if cfg.get("provider") and str(cfg["provider"]) not in LOCAL_ONLY_PROVIDERS:
            problems.append(f"{job}.provider is {cfg['provider']!r}")
        if "/" in str(cfg.get("model") or "") and str(cfg["model"]).split("/", 1)[0] in paid:
            problems.append(f"{job}.model is a paid model")
    return [f"{name}: {p}" for p in problems]


def require_local_only(name: str, agent: Mapping[str, Any]) -> None:
    """Raise ValueError when the definition could send a request (and so the brain) out."""
    if BRAIN_STAYS_LOCAL:
        problems = local_only_problems(name, agent)
        if problems:
            raise ValueError("the brain stays local, so agents use only the local model. Remove: "
                             + "; ".join(problems))


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


def fallback_chain(agent: Mapping[str, Any], paid: Mapping[str, str]) -> List[dict]:
    """The paid models to try, in order, when the local model cannot answer.

    From ``model.fallbacks`` in the agent definition. Each entry is ``provider/model`` or
    ``{model: provider/model, monthly_usd: 2}``. Entries whose provider has no key in
    ``models.env`` are left out, so a definition can name a provider before its key exists.

    The agent's ``budget.monthly_usd`` is its total for the month. An entry with its own
    ``monthly_usd`` gets that much; the rest is shared equally by the entries without one.
    Returns ``[{"provider", "model", "monthly_usd"}]``; raises ValueError when the amounts
    do not fit inside the total.
    """
    name = str(agent.get("name") or "agent")
    total = float((agent.get("budget") or {}).get("monthly_usd", DEFAULT_MONTHLY_USD))
    chain: List[dict] = []
    for entry in (agent.get("model") or {}).get("fallbacks") or []:
        spec = entry if isinstance(entry, Mapping) else {"model": entry}
        full = str(spec.get("model") or "")
        provider, _, model = full.partition("/")
        if provider not in set(PAID_PROVIDERS.values()) or not model:
            raise ValueError(f"{name}: fallback {full!r} must be <provider>/<model> with provider one of "
                             + ", ".join(sorted(set(PAID_PROVIDERS.values()))))
        if provider in paid:
            chain.append({"provider": provider, "model": model, "monthly_usd": spec.get("monthly_usd")})
    fixed = sum(float(c["monthly_usd"]) for c in chain if c["monthly_usd"] is not None)
    open_entries = [c for c in chain if c["monthly_usd"] is None]
    if fixed > total + 1e-9 or (open_entries and total - fixed <= 0):
        raise ValueError(f"{name}: the fallbacks' monthly_usd add up to more than budget.monthly_usd (${total:g})")
    for c in chain:
        c["monthly_usd"] = round(float(c["monthly_usd"]) if c["monthly_usd"] is not None
                                 else (total - fixed) / len(open_entries), 4)
        if c["monthly_usd"] <= 0:
            raise ValueError(f"{name}: every fallback needs a monthly_usd above 0")
    return chain


def budget_ids(config: Mapping[str, Any]) -> List[str]:
    """Every budget id a generated config declares, on keys and on provider configs."""
    gov = config.get("governance") or {}
    ids = [b["id"] for b in gov.get("budgets") or []]
    for vk in gov.get("virtual_keys") or []:
        for pc in vk.get("provider_configs") or []:
            ids += [b["id"] for b in pc.get("budgets") or []]
    return sorted(ids)


#: Run inside a Python image against the gateway's volume, while the gateway is stopped.
#: Bifrost adds what the config declares but never removes a budget that the config no longer
#: has, and a left-over limit on a whole key keeps being enforced: once reached it would
#: refuse the free local model too. argv[1] is the JSON list of budget ids to keep.
PRUNE_BUDGETS = """
import json, os, sqlite3, sys
keep = set(json.loads(sys.argv[1]))
if os.path.exists("/v/config.db"):
    db = sqlite3.connect("/v/config.db")
    for bid, model_config in db.execute(
            "select id, model_config_id from governance_budgets where id like 'budget-%'").fetchall():
        if bid not in keep:
            db.execute("delete from governance_budgets where id = ?", (bid,))
            if model_config:
                db.execute("delete from governance_model_configs where id = ?", (model_config,))
            print("removed left-over budget", bid)
    db.commit()
"""

# Bifrost keeps providers it has ever been given in its own database (config.db), even after
# config.json stops listing them. Remove every provider config.json does not name, with its keys,
# models, virtual-key grants and routing targets, so the gateway knows only what Logos configures.
PRUNE_PROVIDERS = """
import json, os, sqlite3, sys
keep = set(json.loads(sys.argv[1]))
if os.path.exists("/v/config.db"):
    db = sqlite3.connect("/v/config.db")
    for pid, name in db.execute("select id, name from config_providers").fetchall():
        if name in keep:
            continue
        keys = [k for (k,) in db.execute("select id from config_keys where provider_id = ? or provider = ?", (pid, name))]
        vkpc = [v for (v,) in db.execute("select id from governance_virtual_key_provider_configs where provider = ?", (name,))]
        for v in vkpc:
            db.execute("delete from governance_virtual_key_provider_config_keys where table_virtual_key_provider_config_id = ?", (v,))
        for k in keys:
            db.execute("delete from governance_virtual_key_provider_config_keys where table_key_id = ?", (k,))
        db.execute("delete from governance_virtual_key_provider_configs where provider = ?", (name,))
        db.execute("delete from routing_targets where provider = ?", (name,))
        db.execute("delete from config_models where provider_id = ?", (pid,))
        db.execute("delete from config_keys where provider_id = ? or provider = ?", (pid, name))
        db.execute("delete from config_providers where id = ?", (pid,))
        print("removed left-over provider", name)
    db.commit()
"""


def load_windows(agents: Mapping[str, Mapping[str, Any]]) -> Dict[str, int]:
    """Each local model the agents use, and the window to load it with (the largest they ask for):
    the guard loads it so, before LM Studio would load it by itself at its own default."""
    out: Dict[str, int] = {}
    for a in agents.values():
        m = a.get("model") or {}
        if m.get("provider") == "lmstudio" and m.get("default"):
            # as local_models.load_window: load_context_length, never less than context_length
            w = max(int(m.get("context_length") or 0), int(m.get("load_context_length") or 0))
            if w:
                out[str(m["default"])] = max(out.get(str(m["default"]), 0), w)
    return out


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
    for name in sorted(agents):
        require_local_only(name, agents[name])
    if BRAIN_STAYS_LOCAL:
        paid = {}                       # no paid provider is configured at all
    local_url = local_url or local_server(agents)
    providers: Dict[str, Any] = {
        LOCAL_PROVIDER: {
            "keys": [{"name": "local", "value": "local", "models": ["*"], "weight": 1.0}],
            # A local model can go quiet for minutes while it reads a long prompt (more so when the
            # GPU is busy). Bifrost's own default cuts a stream after ~5 silent minutes and hermes
            # then starts the whole reply again, three times. Allow the same 15 minutes as the
            # request itself and the model guard; a model that is off is still caught in 3 s by the guard.
            "network_config": {"base_url": local_url, "default_request_timeout_in_seconds": REQUEST_TIMEOUT,
                               "stream_idle_timeout_in_seconds": REQUEST_TIMEOUT,
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
    rules: List[dict] = []
    for name in sorted(agents):
        agent = agents[name]
        monthly = float((agent.get("budget") or {}).get("monthly_usd", DEFAULT_MONTHLY_USD))
        if monthly <= 0:
            raise ValueError(f"{name}: budget.monthly_usd must be above 0")
        configs = [{"provider": LOCAL_PROVIDER, "weight": 1.0, "allowed_models": ["*"], "key_ids": ["*"]}]
        chain = fallback_chain({**agent, "name": name}, paid)
        if chain:
            # The money limit sits on each paid provider, never on the whole key: a limit on the
            # key would also refuse the free local model once it was reached. A provider whose
            # share is spent is skipped and the next one in the chain is tried.
            by_provider: Dict[str, dict] = {}
            for c in chain:
                slot = by_provider.setdefault(c["provider"], {"models": [], "usd": 0.0})
                slot["models"].append(c["model"])
                slot["usd"] += c["monthly_usd"]
            for provider, slot in by_provider.items():
                configs.append({"provider": provider, "weight": 1.0, "allowed_models": slot["models"],
                                "key_ids": ["*"],
                                "budgets": [{"id": f"budget-{name}-{provider}", "max_limit": round(slot["usd"], 4),
                                             "reset_duration": "1M"}]})
            rules.append({
                "id": f"fallback-{name}", "name": f"fallback-{name}", "enabled": True,
                "description": "Local model first; these paid models, in order, when it cannot answer.",
                "cel_expression": f'provider == "{LOCAL_PROVIDER}"',
                "targets": [{"weight": 1.0}],                    # keep the model the agent asked for
                "fallbacks": [f"{c['provider']}/{c['model']}" for c in chain],
                "scope": "virtual_key", "scope_id": f"vk-{name}", "priority": 0})
        else:
            # No chain: paid providers named in model.allow may be asked for directly, under one
            # limit for the whole key (the arrangement before fallbacks existed).
            for p in ((agent.get("model") or {}).get("allow") or []):
                if p in paid:
                    configs.append({"provider": p, "weight": 1.0, "allowed_models": ["*"], "key_ids": ["*"]})
            budgets.append({"id": f"budget-{name}", "virtual_key_id": f"vk-{name}",
                            "max_limit": monthly, "reset_duration": "1M"})
        vks.append({"id": f"vk-{name}", "name": f"agent-{name}", "value": keys[name], "is_active": True,
                    "provider_configs": configs})
    governance: Dict[str, Any] = {"virtual_keys": vks, "budgets": budgets}
    if rules:
        governance["routing_rules"] = rules
    return {
        "client": {"enforce_auth_on_inference": True},
        "providers": providers,
        "governance": governance,
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

    def health_url(self) -> str:
        """The model guard's health check: is LM Studio answering? (story D2)"""
        return f"http://{self.bind_address()}:{GUARD_PORT}/guard/health"

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
        guard_url = self.guard_up(local_server(agents), image, load_windows(agents)) if image else None
        built = build_config(agents, keys, paid_providers(keys_text), guard_url)
        config = json.dumps(built, indent=1, sort_keys=True)
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", VOLUME])
            self.rt.run(["docker", "run", "--rm", "-v", f"{VOLUME}:/v", HELPER, "chown", GATEWAY_UID, "/v"])
        current = self.rt.runner.run(["docker", "run", "--rm", "-v", f"{VOLUME}:/v:ro", HELPER,
                                      "cat", "/v/config.json"])
        changed = current.returncode != 0 or current.stdout != config
        # The keys file is passed as the container's environment, so a new or
        # changed provider key also needs a fresh container.
        stamp = "local-only" if BRAIN_STAYS_LOCAL else hashlib.sha256(keys_text.encode()).hexdigest()[:16]
        # The database clean-ups below run only on a (re)start, so a change to them needs one too.
        stamp += "-" + hashlib.sha256((PRUNE_BUDGETS + PRUNE_PROVIDERS).encode()).hexdigest()[:8]
        if changed:
            self.rt.run(["docker", "run", "--rm", "-i", "-v", f"{VOLUME}:/v", HELPER, "sh", "-c",
                         f"cat > /v/config.json && chown {GATEWAY_UID} /v/config.json && chmod 0600 /v/config.json"],
                        input=config)
        if self.running() and not changed and self._label() == stamp:
            return False
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        if image:
            out = self.rt.run(["docker", "run", "--rm", "--user", GATEWAY_UID, "-v", f"{VOLUME}:/v",
                               "--entrypoint", "python3", image, "-c", PRUNE_BUDGETS,
                               json.dumps(budget_ids(built))], check=False)
            if out.strip():
                print(out.strip())
            out = self.rt.run(["docker", "run", "--rm", "--user", GATEWAY_UID, "-v", f"{VOLUME}:/v",
                               "--entrypoint", "python3", image, "-c", PRUNE_PROVIDERS,
                               json.dumps(sorted(built["providers"]))], check=False)
            if out.strip():
                print(out.strip())
        cmd = ["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
               "--label", f"logos.models.keys={stamp}",
               "-p", f"{self.bind_address()}:{PORT}:8080", "-v", f"{VOLUME}:/app/data"]
        path = keys_file(self.rt.env)
        if path.exists() and not BRAIN_STAYS_LOCAL:
            cmd += ["--env-file", str(path)]
        self.rt.run(cmd + [IMAGE])
        self.wait()
        return True

    def guard_up(self, upstream: str, image: str, windows: Mapping[str, int] = None) -> str:
        """Run the model guard in front of ``upstream`` and return the URL the gateway should use.

        The guard (:mod:`gateway.model_guard`) passes calls through, and answers at once
        with a clear error when the model server is not accepting connections. It runs in
        the agent image, which already has Python, with its one file mounted read-only.
        """
        load = json.dumps(dict(windows or {}), sort_keys=True)
        stamp = hashlib.sha256((upstream + GUARD_FILE.read_text() + image + load).encode()).hexdigest()[:16]
        names = self.rt.run(["docker", "ps", "--filter", f"name=^{GUARD_CONTAINER}$", "--format", "{{.Names}}"],
                            check=False).split()
        label = self.rt.run(["docker", "inspect", GUARD_CONTAINER, "-f",
                             '{{index .Config.Labels "logos.guard.stamp"}}'], check=False).strip()
        url = f"http://{self.bind_address()}:{GUARD_PORT}"       # no /v1: Bifrost adds it itself
        if GUARD_CONTAINER in names and label == stamp:
            return url
        self.rt.run(["docker", "rm", "-f", GUARD_CONTAINER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", GUARD_CONTAINER, "--restart", "unless-stopped",
                     "--label", f"logos.guard.stamp={stamp}", "--user", GUARD_UID, "--read-only",
                     "-p", f"{self.bind_address()}:{GUARD_PORT}:{GUARD_PORT}",
                     "-v", f"{GUARD_FILE}:/app/model_guard.py:ro",
                     "-e", f"GUARD_UPSTREAM={upstream.rsplit('/v1', 1)[0]}", "-e", f"GUARD_PORT={GUARD_PORT}",
                     "-e", f"GUARD_LOAD={load}",                # each model's window (model_guard.Loader)
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
        attached = paid_providers(self._keys_text())
        for name, agent in sorted(self.agents().items()):
            monthly = float((agent.get("budget") or {}).get("monthly_usd", DEFAULT_MONTHLY_USD))
            chain = fallback_chain({**agent, "name": name}, attached)
            if chain:
                lines.append(f"  {name}: local, then " + ", then ".join(
                    f"{c['provider']}/{c['model']} (up to ${c['monthly_usd']:.2f} a month)" for c in chain))
                continue
            allow = [p for p in (agent["model"].get("allow") or []) if p in paid]
            lines.append(f"  {name}: ${monthly:.2f} a month, may use local"
                         + ("".join(f", {p}" for p in allow)))
        return "\n".join(lines)
