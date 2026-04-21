"""
OpenShell route management — provision and tear down per-(provider, model)
inference gateways.

OpenShell's privacy router enforces a *single* forced model per gateway via
``openshell inference set --provider <p> --model <m>``. The model field in
each request body is ignored and overwritten with the configured model. That
means you can't have multiple Logos agents on different models sharing one
OpenShell gateway — they'd all hit the same forced model.

The fix is to run multiple OpenShell gateways on the same host, one per
(provider, model) pair. Each gateway:

  * is named after its model (e.g. ``openai-gpt-oss-20b``,
    ``qwen-qwen3-5-9b``) — the sanitized form of the model id, no prefix.
  * has its own unique host port (``9090``, ``9091``, ``9092``, ...)
  * has its own k3s cluster (full Docker container)
  * is pinned to one provider+model via ``openshell inference set``
  * resolves ``inference.local`` (inside its sandboxes) to that one model

Logos's ``OpenShellExecutor.spawn`` reads the agent's ``model_route_id``,
looks up the row in ``model_routes``, and passes ``-g <openshell_name>``
to the spawn subprocess so the sandbox lands inside the right gateway.

History note — the "primordial" concept has been removed
────────────────────────────────────────────────────────
Earlier versions had a ``BOOTSTRAP_PRIMORDIAL_NAME = "logos-openshell"``
constant and an ``adopt_primordial`` function that detected a pre-
existing gateway (usually provisioned out-of-band by the user via
``openshell gateway start``) and "adopted" it into ``model_routes`` by
registering a client-side alias with a clean model-based name. That
approach was structurally broken: the alias worked for gRPC/exec calls
(endpoint-URL routed) but ``openshell sandbox create --from <Dockerfile>``
derives its target Docker container name from the gateway name, so
``-g <alias>`` looked for ``openshell-cluster-<alias>`` which didn't
exist and failed with ``404: No such container``. Every first-run /setup
on a machine with an existing ``logos-openshell`` container hit this.

Rather than patch the alias trick, we dropped the concept entirely:
Logos now always provisions gateways fresh via ``openshell gateway start
--name <sanitized-model>``, and the ``is_primordial`` deletion guard is
replaced with a "refuse to delete the last remaining route" check in
``destroy_route``. Users who had a standalone ``logos-openshell``
container from a pre-Logos OpenShell install need to destroy it first
(or let /setup collide and surface the port conflict clearly).

This module wraps the ``openshell`` CLI and the ``model_routes`` table.
HTTP endpoints (gateway/http_api.py) and the executor
(gateway/executors/openshell.py) call into here rather than touching the
CLI directly.

Trade-off — known and accepted: each gateway is a full k3s+Docker container
(~200-500 MB RAM idle). 5 routes ≈ 1-2.5 GB RAM overhead even when nothing
is running. A future optimisation could ``openshell gateway stop`` routes
with zero bound sandboxes after N idle minutes; that's deferred until the
basic flow is working.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import shutil
import subprocess
import time
from typing import Optional

from gateway.auth import db as auth_db

logger = logging.getLogger(__name__)


# Which cluster image to pin every Logos-provisioned gateway to. The
# openshell CLI ships with a baked-in default tag (as of the 0.0.29-era
# CLI binary Logos ships with, that default is "0.0.29" — we want
# newer). Setting ``OPENSHELL_CLUSTER_IMAGE`` in the env that
# ``openshell gateway start`` sees overrides that baked-in tag and
# brings up the target version. Without this, every time Logos
# provisions a new route via the UI the cluster spins up on the stale
# 0.0.29 image and misses months of sandbox/policy/inference fixes.
#
# Overridable via the ``LOGOS_OPENSHELL_CLUSTER_IMAGE`` env var so a
# host can pin a different version (downgrade for rollback, bump
# without a code change, point at a private registry mirror) without
# editing this file.
_DEFAULT_OPENSHELL_CLUSTER_IMAGE = "ghcr.io/nvidia/openshell/cluster:0.0.33"
OPENSHELL_CLUSTER_IMAGE = os.getenv(
    "LOGOS_OPENSHELL_CLUSTER_IMAGE",
    _DEFAULT_OPENSHELL_CLUSTER_IMAGE,
)


# ── Constants ───────────────────────────────────────────────────────────────

# Auto-allocation pool for OpenShell gateway host ports. The first route
# provisioned on a fresh install starts at 9090 and subsequent routes
# walk upward. Override per-host with LOGOS_OPENSHELL_PORT_START /
# LOGOS_OPENSHELL_PORT_END env vars when the default 50-port window is
# too small (or you want to move the range to avoid a port conflict).
# Each gateway is a thin proxy process — RAM cost is small; the real
# limit is the inference backend, not OpenShell itself.
# Default range 10100–10199. Chosen because:
#   * 9090/9091 = Prometheus, 9100 = node_exporter, 9200 = Elasticsearch
#     — the 9000s are crowded with metrics tooling on a typical homelab.
#   * 10100–10199 has no IANA-registered services and isn't claimed by
#     any common container ecosystem default.
# Existing routes that were allocated in the legacy 9090–9099 window
# keep working; only NEW allocations land in the new range. Override
# per-host with LOGOS_OPENSHELL_PORT_START / LOGOS_OPENSHELL_PORT_END.
_PORT_ALLOC_START = int(os.environ.get("LOGOS_OPENSHELL_PORT_START") or 10100)
_PORT_ALLOC_END = int(os.environ.get("LOGOS_OPENSHELL_PORT_END") or 10199)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _openshell_exe() -> Optional[str]:
    """Return the path to the openshell binary, or None if not on PATH."""
    return shutil.which("openshell")


def _sanitize_route_name(model: str) -> str:
    """Convert a model identifier to a valid OpenShell gateway name.

    Returns the sanitized model name with no prefix. OpenShell gateway
    names must be valid Kubernetes resource names (RFC 1123 subdomains):
    lowercase alphanumerics and dashes only, starting and ending with
    alphanumeric, max 63 characters.

    Examples:
      ``openai/gpt-oss-20b``  →  ``openai-gpt-oss-20b``
      ``qwen/qwen3.5-9b``     →  ``qwen-qwen3-5-9b``
    """
    s = model.lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "unknown"
    return s[:63].rstrip("-")


def get_default_gateway_name() -> Optional[str]:
    """Return the ``openshell_name`` of the default model route, if any.

    Replaces the removed ``get_primordial_name()``. Used by executor
    paths (prune, delete-by-name, state fallback) that need a gateway
    to consult when a state-file entry is missing an ``openshell_name``.

    Resolution order:
      1. The row with ``is_default=1`` — that's the user's chosen primary.
      2. The oldest row by ``created_at`` — covers the case where the
         default flag hasn't been set yet (e.g. fresh post-/setup state).
      3. ``None`` — caller is responsible for handling the "no routes
         configured" case. Historically this returned
         ``BOOTSTRAP_PRIMORDIAL_NAME = "logos-openshell"`` as a literal
         fallback, which masked the "/setup hasn't run yet" state; we
         prefer the explicit None so callers can tell the difference.
    """
    try:
        routes = auth_db.list_model_routes()
    except Exception as exc:
        logger.warning("get_default_gateway_name: list_model_routes failed: %s", exc)
        return None
    if not routes:
        return None
    # list_model_routes() orders by is_default DESC then created_at, so
    # the first row is already the correct default/fallback.
    return routes[0].get("openshell_name")


def _next_free_port() -> int:
    """Pick the next unused port in the OpenShell allocation range.

    Considers both model_routes rows AND any already-running OpenShell
    gateway containers — the latter so a fresh-install allocator doesn't
    collide with a pre-existing ``openshell-cluster-*`` container that
    was provisioned out-of-band. (Historically we reserved port 9090 as
    the primordial port; now we just check whether anything already
    owns it and skip if so.)
    """
    used: set[int] = {r["openshell_port"] for r in auth_db.list_model_routes()}

    # Also treat any port already bound by a running openshell-cluster-*
    # container as used, so the allocator never hands out a port that
    # `openshell gateway start` would immediately fail on.
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", "name=openshell-cluster-",
             "--format", "{{.Ports}}"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            # Ports lines look like "0.0.0.0:9090->30051/tcp" — grab the
            # first `:NNNN->` occurrence on each line.
            import re as _re
            for line in out.stdout.splitlines():
                m = _re.search(r":(\d+)->", line)
                if m:
                    used.add(int(m.group(1)))
    except Exception as exc:
        logger.debug("_next_free_port: docker ps probe failed (non-fatal): %s", exc)

    # Final guard: ask the kernel whether the port is actually free on this
    # host. The DB + docker-ps checks above only see ports we OR another
    # openshell container have allocated; they don't catch unrelated
    # services squatting in our range (node_exporter on 9100 is the
    # canonical example). Without this check the allocator hands out a
    # busy port, `openshell gateway start` silently binds nothing useful,
    # and `gateway info` then fails with "underlying gateway is unreachable".
    import socket as _socket

    def _host_port_free(port: int) -> bool:
        for family, addr in ((_socket.AF_INET, ("127.0.0.1", port)),
                             (_socket.AF_INET6, ("::1", port))):
            try:
                with _socket.socket(family, _socket.SOCK_STREAM) as s:
                    s.settimeout(0.3)
                    if s.connect_ex(addr) == 0:
                        return False  # something already accepts here
            except Exception:
                continue
        return True

    for p in range(_PORT_ALLOC_START, _PORT_ALLOC_END + 1):
        if p in used:
            continue
        if not _host_port_free(p):
            logger.info("_next_free_port: %d is taken by another process — skipping", p)
            used.add(p)
            continue
        return p
    raise RuntimeError(
        f"No free port in OpenShell allocation range "
        f"{_PORT_ALLOC_START}-{_PORT_ALLOC_END}. "
        f"Destroy an unused route or raise the cap "
        f"(LOGOS_OPENSHELL_PORT_START / LOGOS_OPENSHELL_PORT_END)."
    )


def _run_openshell(
    *args: str,
    gateway: Optional[str] = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Invoke the openshell CLI with optional --gateway scoping.

    Raises ``RuntimeError`` if the binary isn't on PATH (rather than the
    less-informative ``FileNotFoundError`` from subprocess). Sets a hard
    timeout because ``gateway start`` can hang on a misconfigured host.

    Env: injects ``OPENSHELL_CLUSTER_IMAGE`` into the subprocess env so
    any ``gateway start`` that happens to run through here uses Logos's
    pinned cluster version (``OPENSHELL_CLUSTER_IMAGE`` module constant,
    overridable via ``LOGOS_OPENSHELL_CLUSTER_IMAGE``) instead of the
    CLI binary's baked-in default. Setting the env var is a no-op for
    non-``gateway start`` subcommands, so we can safely always set it
    without branching on the args.
    """
    exe = _openshell_exe()
    if not exe:
        raise RuntimeError(
            "openshell CLI not found on PATH. Install it from "
            "https://github.com/NVIDIA/OpenShell"
        )
    cmd = [exe]
    if gateway:
        cmd.extend(["-g", gateway])
    cmd.extend(args)
    # Inject OPENSHELL_CLUSTER_IMAGE without clobbering anything else
    # the user has in their env (proxy vars, OPENSHELL_GATEWAY, etc).
    # Respect a caller-provided override if one is already set upstream.
    env = os.environ.copy()
    env.setdefault("OPENSHELL_CLUSTER_IMAGE", OPENSHELL_CLUSTER_IMAGE)
    logger.debug("running: %s (OPENSHELL_CLUSTER_IMAGE=%s)",
                 " ".join(cmd), env["OPENSHELL_CLUSTER_IMAGE"])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
        env=env,
    )


def _stderr_or_stdout(exc: subprocess.CalledProcessError) -> str:
    """Pick the most informative output for an error log line."""
    return ((exc.stderr or exc.stdout) or str(exc)).strip()[:500]


# ── Provider config resolution ──────────────────────────────────────────────

def _resolve_lmstudio_provider_args() -> tuple[str, str]:
    """Resolve the (cred_arg, config_arg) pair for an LM Studio provider create.

    Centralizes the resolution that ``finish_provisioning``,
    ``adopt_primordial``, and ``ensure_provider_configured`` all need so a
    future change has to land here.

    URL resolution order (no silent default — raises if exhausted):
      1. First enabled machine in auth.db with an endpoint_url
      2. ``OPENAI_BASE_URL`` env var
      3. **Raises ``RuntimeError``** — refusing to invent a URL is the
         whole point. The previous hardcoded
         ``http://host.docker.internal:1234/v1`` default turned a
         "we don't know your URL" failure into a "we silently configured
         the wrong URL" success on systems where LM Studio runs on a
         different host. The /setup flow always seeds a machine row in
         step 1 (Connect model server) before any OpenShell gateway is
         provisioned, so a missing machine here is a real configuration
         error, not the normal happy path.

    Key resolution order (default OK — no silent breakage):
      1. Same machine row's api_key column
      2. ``OPENAI_API_KEY`` env var
      3. literal ``"lm-studio"`` — LM Studio's documented placeholder
         token; no-auth instances accept it, auth instances need a
         real token in the machine row.
    """
    base_url = ""
    api_key: Optional[str] = None
    try:
        machines = auth_db.list_machines() if hasattr(auth_db, "list_machines") else []
        for m in machines:
            if m.get("enabled") and m.get("endpoint_url"):
                base_url = m["endpoint_url"]
                if m.get("api_key"):
                    api_key = m["api_key"]
                break
    except Exception as exc:
        logger.warning("could not read machines from auth.db: %s", exc)
    if not base_url:
        import os as _os
        base_url = _os.environ.get("OPENAI_BASE_URL") or ""
    if not base_url:
        raise RuntimeError(
            "Cannot resolve LM Studio URL — no enabled machine with an "
            "endpoint_url in auth.db, and OPENAI_BASE_URL env var is not "
            "set. Configure a machine via Settings → Inference."
        )

    if api_key:
        cred_value = api_key
    else:
        import os as _os
        cred_value = _os.environ.get("OPENAI_API_KEY") or "lm-studio"

    return f"OPENAI_API_KEY={cred_value}", f"OPENAI_BASE_URL={base_url}"


# Mapping of Logos provider names → (OpenShell --type, credential env-var name,
# default base URL). The OpenShell `--type anthropic` provider knows how to
# speak Anthropic's API natively; openrouter is OpenAI-compatible so it uses
# `--type openai` with the OpenRouter base URL.
# Why Anthropic uses ``openshell_type: openai`` (not "anthropic"):
#
# OpenShell's ``--type anthropic`` provider only exposes Anthropic's
# native ``POST /v1/messages`` API. Hermes's config.yaml sets
# ``model.provider: custom`` + ``base_url: https://inference.local/v1``,
# which means hermes ALWAYS speaks OpenAI-compat (``/v1/chat/completions``)
# regardless of the actual upstream. When hermes hit the anthropic-type
# provider, OpenShell returned ``400 "no compatible inference route
# available"`` because that path didn't exist.
#
# Anthropic ships an OpenAI-compat shim at
# ``https://api.anthropic.com/v1/`` that accepts the same Anthropic API
# key as a Bearer token and handles ``/v1/chat/completions`` natively.
# Registering the provider as ``--type openai`` with that base URL makes
# OpenShell a transparent forwarder — hermes's OpenAI-shaped request
# lands on Anthropic's OpenAI shim and comes back with a
# chat-completions envelope hermes already knows how to parse.
#
# Existing provisioned ``anthropic``-type providers need to be re-created
# (see ``finish_provisioning`` — it calls ``provider create`` on cold
# start and ``provider update`` on reuse; neither path changes ``--type``,
# so a full destroy + recreate of the gateway is the easiest
# remediation path for pre-2026-04-21 Anthropic routes).
_CLOUD_PROVIDER_PROFILES = {
    "anthropic":  {"openshell_type": "openai",    "cred_key": "OPENAI_API_KEY",    "default_base": "https://api.anthropic.com/v1/"},
    "openai":     {"openshell_type": "openai",    "cred_key": "OPENAI_API_KEY",    "default_base": "https://api.openai.com/v1"},
    "openrouter": {"openshell_type": "openai",    "cred_key": "OPENAI_API_KEY",    "default_base": "https://openrouter.ai/api/v1"},
}


def _resolve_provider_args(provider: str) -> tuple[str, str, str]:
    """Resolve (openshell_type, cred_arg, config_arg) for any provider.

    Dispatches by Logos provider name:
      - lmstudio / ollama: existing local-machine logic via
        ``_resolve_lmstudio_provider_args``. Returns OpenShell type=openai
        with the registered machine's URL + key.
      - anthropic / openai / openrouter: reads from the ``cloud_providers``
        table (where the user stored their API key via Config → Tools).
        Returns the appropriate OpenShell --type with provider-specific
        credential env-var name.

    Raises RuntimeError if the provider isn't configured (no enabled
    cloud_providers row with an api_key for cloud providers, or no
    machine for local providers).
    """
    if provider in ("lmstudio", "ollama"):
        cred_arg, config_arg = _resolve_lmstudio_provider_args()
        return "openai", cred_arg, config_arg

    profile = _CLOUD_PROVIDER_PROFILES.get(provider)
    if not profile:
        raise RuntimeError(
            f"Unknown provider {provider!r}. Supported: lmstudio, ollama, "
            "anthropic, openai, openrouter."
        )

    # Find the matching enabled cloud_providers row.
    rows = auth_db.list_cloud_providers() if hasattr(auth_db, "list_cloud_providers") else []
    prov = next(
        (p for p in rows if p.get("provider") == provider and p.get("enabled")),
        None,
    )
    if not prov or not prov.get("api_key"):
        raise RuntimeError(
            f"No enabled '{provider}' cloud provider with an API key. "
            f"Add one via Config → Tools → {provider.capitalize()}."
        )

    base_url = (prov.get("base_url") or "").rstrip("/") or profile["default_base"]
    cred_arg = f"{profile['cred_key']}={prov['api_key']}"
    # OpenShell convention: config keys mirror the credential's prefix
    # (OPENAI_BASE_URL for the openai-compatible types, ANTHROPIC_BASE_URL
    # for the anthropic type). Anthropic only needs the URL when pointing
    # at a non-default endpoint, but always passing it keeps the wiring
    # symmetric with LM Studio.
    config_key = profile["cred_key"].replace("_API_KEY", "_BASE_URL")
    config_arg = f"{config_key}={base_url}"
    return profile["openshell_type"], cred_arg, config_arg


def ensure_provider_configured(gateway_name: str, provider_name: str) -> bool:
    """Re-sync an OpenShell provider's credential + config from auth.db.

    This is called pre-spawn so every sandbox lands in a sub-gateway whose
    provider has the CURRENT machine row's credential and URL — not a
    snapshot from whenever the sub-gateway was first provisioned.

    Why "always re-sync" instead of "detect-then-heal":

      Two real-world stale-state cases bit us during the qwen3.5 / Tildi
      / Hermette-copy debug session:

        1. **Stale URL** — sub-gateway was provisioned before commit
           ``5390da5`` and stored no OPENAI_BASE_URL at all (CONFIG_KEYS=0).
           Worker registers, then crashes ~16s after first inference call.

        2. **Stale credential** — sub-gateway was provisioned with an
           older LM Studio API key, which the user later rotated. The
           machines table got updated (via /setup or the admin Machines
           page), the primordial gateway got updated, but the
           sub-gateway's stored provider credential did not. Worker
           registers, ``ensure_loaded`` (which uses auth.db.machines.api_key
           directly) loads the model fine, then the worker's chat call
           through the privacy router gets rejected because OpenShell
           forwards with the stale stored credential.

      Detecting case 1 is cheap (``provider list`` shows CONFIG_KEYS).
      Detecting case 2 is hard (CLI doesn't expose credential values).
      Just always re-syncing both fields catches both cases for the price
      of a single ``provider update`` call (~50-200ms) per spawn. Spawns
      are rare and ``provider update`` is idempotent, so the cost is fine.

    Returns True if the update landed (or the provider was already in
    sync), False if the update failed or no machine row is configured.
    Logged but not raised so the spawn flow can proceed either way.
    """
    try:
        # provider_name is the same string we used at provision time
        # (lmstudio / anthropic / openai / openrouter), so it dispatches
        # cleanly through the same resolver. Earlier this hardcoded
        # _resolve_lmstudio_provider_args, which silently overwrote
        # cloud-provider creds with LM Studio's on every spawn — that's
        # how we ended up with an "anthropic" provider pointing at port 1234.
        _osh_type, cred_arg, config_arg = _resolve_provider_args(provider_name)
    except RuntimeError as exc:
        logger.error(
            "Cannot sync provider '%s' on gateway '%s' — %s",
            provider_name, gateway_name, exc,
        )
        return False

    try:
        _run_openshell(
            "provider", "update", provider_name,
            "--credential", cred_arg,
            "--config", config_arg,
            gateway=gateway_name,
        )
    except subprocess.CalledProcessError as exc:
        logger.error(
            "Failed to sync provider '%s' on gateway '%s': %s",
            provider_name, gateway_name, _stderr_or_stdout(exc),
        )
        return False

    logger.debug(
        "Synced provider '%s' on gateway '%s' from auth.db",
        provider_name, gateway_name,
    )
    return True


# ── Status query ────────────────────────────────────────────────────────────

def gateway_is_alive(openshell_name: str) -> bool:
    """Return True if `openshell gateway info -g <name>` exits cleanly."""
    try:
        result = _run_openshell("gateway", "info", gateway=openshell_name,
                                check=False, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def wait_for_gateway_ready(
    openshell_name: str,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
) -> bool:
    """Block until a sub-gateway accepts sandbox-create RPCs, or ``timeout`` s pass.

    Why this exists: ``provision_or_reuse_route`` kicks off the sub-gateway
    container asynchronously and returns as soon as the container is *up*,
    but the gateway's sandbox service can take several seconds more to
    start accepting gRPC. Callers that immediately issue
    ``openshell sandbox create -g <name>`` in that window hit the openshell
    CLI's "No gateway found — auto-start primordial" fallback and the
    sandbox lands in the wrong cluster (observed 2026-04-17 during a
    fresh /setup run).

    The readiness probe is ``openshell sandbox list -g <name>``, which is
    what ``sandbox create`` ultimately RPCs against — a successful list
    means create is safe. ``gateway_is_alive`` above only tests metadata
    reachability (``gateway info``) which returns OK earlier and is
    therefore too lenient.

    Returns True on success, False on timeout. Callers decide whether to
    abort or push through (the setup wizard logs + continues, since the
    user's second-click retry already demonstrates the gateway does come
    up eventually).
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = _run_openshell(
                "sandbox", "list", gateway=openshell_name,
                check=False, timeout=5,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass  # timeout or other transient — keep polling
        time.sleep(poll_interval)
    return False


def refresh_status(route_id: str) -> Optional[dict]:
    """Re-check OpenShell for the route's gateway status and update the row.

    Used by:
      * /admin/model-routes list refresh (commit 4)
      * background reconciliation if a route is stuck in 'provisioning'

    Status transitions written here:
      * ``ready``        → ``error``  (gateway info failed — route went bad)
      * ``error``        → ``ready``  (gateway came back up)

    Intentionally does NOT touch rows in ``provisioning``. Those
    belong to an in-flight background ``finish_provisioning`` task;
    that task is the authoritative writer and will flip the row to
    ``ready`` (or ``error``) when it completes. Stomping on it here
    was the cause of a nasty bug where clicking the Refresh button
    on a freshly-provisioned row mid-cold-start would overwrite
    ``provisioning`` → ``error`` with "gateway info reported the
    underlying gateway is unreachable" (literally true at that
    instant — the gateway was still being started — but wrong to
    record as a terminal error state). The background task would
    then overwrite with ``ready`` a few seconds later and the row
    would flash red→green in the UI.
    """
    route = auth_db.get_model_route(route_id)
    if not route:
        return None
    current_status = route.get("status")
    if current_status == "provisioning":
        # Background task owns this row — don't touch.
        return route
    if gateway_is_alive(route["openshell_name"]):
        new_status = "ready"
        new_detail = None
    else:
        new_status = "error"
        new_detail = "gateway info reported the underlying gateway is unreachable"
    if new_status != current_status or new_detail != route.get("status_detail"):
        auth_db.update_model_route(
            route_id, status=new_status, status_detail=new_detail,
        )
    return auth_db.get_model_route(route_id)


# ── Route lifecycle ────────────────────────────────────────────────────────
#
# Note: the earlier ``adopt_primordial`` / ``_ensure_gateway_alias`` /
# ``migrate_routes_to_model_names`` helpers are gone — see the module
# docstring at the top of this file for the full explanation. Logos now
# always provisions gateways fresh via ``provision_new_route``.

def create_route_provisioning_row(provider: str, model: str) -> dict:
    """Step 1 of cold provision — insert the model_routes row in
    'provisioning' status and return it.

    This is the FAST half of provisioning (just a DB insert + a name and
    port pick). Splitting it out from the slow gateway-start lets the
    HTTP handler respond immediately so the admin UI can show the new
    row in the table while the actual openshell calls finish in a
    background task. The user no longer has to stare at a wedged
    "provisioning…" modal for 60s.
    """
    name = _sanitize_route_name(model)
    port = _next_free_port()
    try:
        return auth_db.create_model_route(
            provider=provider,
            model=model,
            openshell_name=name,
            openshell_port=port,
            status="provisioning",
        )
    except Exception as exc:
        raise RuntimeError(
            f"could not register route for {provider}/{model}: {exc}"
        )


def finish_provisioning(route_id: str, set_as_default: bool = False) -> dict:
    """Steps 2-5 of cold provision — actually start the openshell gateway,
    register the provider, pin the inference route, and mark the row
    'ready'.

    Assumes ``create_route_provisioning_row`` has already inserted the
    row in 'provisioning' status. On any failure the row is updated to
    'error' with status_detail populated, the underlying gateway is
    best-effort destroyed, and the exception is re-raised.

    Designed to be called from a background task (via
    ``asyncio.to_thread``) so the HTTP request that triggered the
    provision can return long before this finishes.
    """
    route = auth_db.get_model_route(route_id)
    if not route:
        raise RuntimeError(f"finish_provisioning: route {route_id} not found")

    name = route["openshell_name"]
    port = route["openshell_port"]
    provider = route["provider"]
    model = route["model"]

    def _diagnose(detail: str) -> str:
        """Rewrite cryptic openshell failures into actionable hints.

        Known patterns that confuse users:
          - "Gateway failed to start" with no container logs → k3s
            healthcheck loop, almost always inotify exhaustion.
          - "all predefined address pools have been fully subnetted"
            → Docker network IPAM ceiling; user needs to prune or
            raise default-address-pools.
        """
        low = (detail or "").lower()
        if "gateway failed" in low or "underlying gateway is unreachable" in low:
            try:
                mui = int(pathlib.Path("/proc/sys/fs/inotify/max_user_instances").read_text().strip())
            except Exception:
                mui = 0
            try:
                import subprocess as _sp
                _n = int(_sp.run(["docker", "ps", "--filter", "name=openshell-cluster-",
                                  "--format", "{{.Names}}"],
                                 capture_output=True, text=True, timeout=5).stdout.strip().splitlines().__len__())
            except Exception:
                _n = 0
            if mui and _n and (_n * 70) > (mui * 0.8):
                return (
                    f"Gateway failed because the host's inotify limit is exhausted. "
                    f"fs.inotify.max_user_instances={mui}, "
                    f"but you already have {_n} OpenShell k3s clusters running "
                    f"(~{_n*70} instances in use). "
                    f"Fix:  sudo sysctl -w fs.inotify.max_user_instances=8192  "
                    f"(and persist: echo 'fs.inotify.max_user_instances=8192' | sudo "
                    f"tee -a /etc/sysctl.d/99-openshell.conf && sudo sysctl --system). "
                    f"Then destroy this failed route and retry."
                )
        if "predefined address pools" in low:
            return (
                "Gateway failed because Docker ran out of IPv4 subnets. "
                "Quick fix:  docker network prune -f. "
                "Durable fix: add default-address-pools to /etc/docker/daemon.json "
                "(e.g. {\"default-address-pools\":[{\"base\":\"172.80.0.0/12\",\"size\":24}]}) "
                "and restart Docker."
            )
        return detail

    def _fail(detail: str, cleanup_gateway: bool = False) -> None:
        rewritten = _diagnose(detail)
        if cleanup_gateway:
            try:
                _run_openshell("gateway", "destroy", "--force",
                                gateway=name, check=False, timeout=60)
            except Exception as cleanup_err:
                logger.warning(
                    "openshell gateway destroy cleanup failed for %s: %s",
                    name, cleanup_err,
                )
        auth_db.update_model_route(route_id, status="error", status_detail=rewritten[:500])

    # Step 2: Start the gateway (slow, ~30-60s on cold start)
    try:
        _run_openshell(
            "gateway", "start",
            "--name", name,
            "--port", str(port),
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        _fail(f"timeout waiting for `openshell gateway start --name {name}`",
              cleanup_gateway=True)
        raise RuntimeError(f"gateway start timed out for route {route_id}")
    except subprocess.CalledProcessError as exc:
        _fail(_stderr_or_stdout(exc), cleanup_gateway=True)
        raise RuntimeError(
            f"failed to start OpenShell gateway '{name}': {_stderr_or_stdout(exc)}"
        )

    # Step 3: Register the provider on the new gateway.
    #
    # CRITICAL: must pass BOTH the credential AND the base URL so the new
    # gateway knows where to send inference requests. ``_resolve_provider_args``
    # dispatches by provider name — local providers (lmstudio/ollama) get
    # the registered machine's URL + key; cloud providers (anthropic/openai/
    # openrouter) get the api_key + base URL stored in cloud_providers.
    # Earlier code hardcoded the LM Studio resolver + ``--type openai``
    # which silently broke ALL cloud-provider routes — they'd register an
    # openai-typed provider pointing at LM Studio's URL with LM Studio's
    # placeholder token, and the first inference call would 400 because
    # LM Studio doesn't have e.g. ``claude-sonnet-4-6``.
    try:
        openshell_type, cred_arg, config_arg = _resolve_provider_args(provider)
    except RuntimeError as exc:
        _fail(str(exc), cleanup_gateway=True)
        raise

    # Pre-check: if the provider already exists on this gateway, skip
    # `provider create` entirely. The previous version did the create
    # unconditionally and then swallowed any error mentioning "exists",
    # "in use", or "duplicate" — too lenient. A failed create whose
    # error happened to contain one of those words got silently ignored,
    # leaving the gateway with no provider; step 4 (`inference set`)
    # then failed with the cryptic "provider 'lmstudio' not found".
    # Probing first lets us treat any non-zero exit from `provider create`
    # as a real failure.
    _provider_exists = False
    try:
        _list_out = subprocess.run(
            ["openshell", "-g", name, "provider", "list"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if _list_out.returncode == 0:
            # openshell 0.0.23 prints a plain table — first column is NAME.
            # Match on word-boundary so e.g. "openai" doesn't false-match
            # against "openai-compat".
            for line in (_list_out.stdout or "").splitlines():
                tokens = line.split()
                if tokens and tokens[0] == provider:
                    _provider_exists = True
                    break
    except Exception as _probe_exc:
        logger.debug(
            "provider-exists probe failed (non-fatal, will attempt create): %s",
            _probe_exc,
        )

    if _provider_exists:
        # Re-sync credential + config so a stale URL/key from an older
        # provision run doesn't survive. ``provider update`` is idempotent.
        try:
            _run_openshell(
                "provider", "update",
                "--name", provider,
                "--credential", cred_arg,
                "--config", config_arg,
                gateway=name,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "provider update on existing %s/%s failed (non-fatal): %s",
                name, provider, _stderr_or_stdout(exc),
            )
    else:
        try:
            _run_openshell(
                "provider", "create",
                "--name", provider,
                "--type", openshell_type,
                "--credential", cred_arg,
                "--config", config_arg,
                gateway=name,
            )
        except subprocess.CalledProcessError as exc:
            _fail(_stderr_or_stdout(exc), cleanup_gateway=True)
            raise RuntimeError(
                f"failed to create provider on '{name}': {_stderr_or_stdout(exc)}"
            )

    # Step 4: Pin the inference route
    try:
        _run_openshell(
            "inference", "set",
            "--provider", provider,
            "--model", model,
            "--no-verify",
            gateway=name,
        )
    except subprocess.CalledProcessError as exc:
        _fail(_stderr_or_stdout(exc), cleanup_gateway=True)
        raise RuntimeError(
            f"failed to set inference on '{name}': {_stderr_or_stdout(exc)}"
        )

    # Step 5: Mark ready and optionally promote
    auth_db.update_model_route(route_id, status="ready", status_detail=None)
    if set_as_default:
        auth_db.set_default_model_route(route_id)

    # Step 6: Pre-warm the sandbox image into the new cluster's
    # containerd. Without this, the first agent that spawns into this
    # route eats ~90-180 s of ``docker save | ctr import`` for the 2.8
    # GB hermes-sandbox image — directly in the user's provisioning
    # banner. Pre-warm here runs in a background thread so
    # finish_provisioning returns as soon as the gateway is up, and
    # the first agent spawn later sees a warm cluster. Idempotent:
    # ``_ensure_image_in_cluster`` short-circuits if the image is
    # already present.
    try:
        import threading
        from gateway.executors.openshell import (
            _ensure_image_in_cluster, _DEFAULT_IMAGE,
        )
        def _prewarm():
            try:
                imported = _ensure_image_in_cluster(_DEFAULT_IMAGE, name)
                logger.info(
                    "prewarm: cluster '%s' image=%s imported=%s",
                    name, _DEFAULT_IMAGE, imported,
                )
            except Exception as exc:
                logger.warning(
                    "prewarm: _ensure_image_in_cluster(%s, %s) failed: %s "
                    "(first sandbox spawn will do it synchronously)",
                    _DEFAULT_IMAGE, name, exc,
                )
        threading.Thread(target=_prewarm, daemon=True,
                         name=f"prewarm-{name}").start()
    except Exception as exc:
        logger.warning("prewarm: could not kick off thread for %s: %s",
                       name, exc)

    return auth_db.get_model_route(route_id)


def provision_new_route(provider: str, model: str,
                         set_as_default: bool = False) -> dict:
    """Spin up a brand-new OpenShell gateway for (provider, model).

    Synchronous full provision — does both halves (create row + finish).
    Used by ``provision_or_reuse_route`` which is in turn called from
    ``/setup`` (an interactive wizard that already shows progress).
    The HTTP admin handler uses ``create_route_provisioning_row`` +
    ``finish_provisioning`` directly so it can return as soon as the
    row exists and finish the slow part in a background task.
    """
    route = create_route_provisioning_row(provider, model)
    return finish_provisioning(route["id"], set_as_default=set_as_default)


def provision_or_reuse_route(provider: str, model: str,
                              set_as_default: bool = False) -> dict:
    """Get an existing route for (provider, model) or provision a new one.

    Resolution order (simpler post-primordial-removal):
      1. If a model_routes row already exists for (provider, model), reuse
         it. Re-pin the underlying gateway's inference route as a side
         effect (cheap, idempotent) so the actual OpenShell state matches
         the DB.
      2. Otherwise, provision a fresh OpenShell gateway via
         ``provision_new_route`` (slow path — ~30-60s cold start).

    Used by /setup and the /admin/model-routes POST handler. Caller is
    expected to be in a non-blocking context — this function is sync and
    can take >60s on the cold-provision path.

    Note: the earlier "adopt the existing logos-openshell primordial"
    branch was removed. If a pre-existing out-of-band gateway is on the
    host, ``_next_free_port`` will see its port as occupied and pick the
    next free slot, so the new route gets its own container. Users who
    want to consolidate should destroy the old container manually.
    """
    # 1. Existing match
    existing = auth_db.get_model_route_by_provider_model(provider, model)
    if existing:
        # Re-pin the inference route in case OpenShell state drifted (e.g.
        # the gateway was restarted manually). Best-effort: errors are
        # surfaced via status_detail but don't prevent reuse.
        try:
            _run_openshell(
                "inference", "set",
                "--provider", provider,
                "--model", model,
                "--no-verify",
                gateway=existing["openshell_name"],
            )
            auth_db.update_model_route(existing["id"], status="ready",
                                        status_detail=None)
        except subprocess.CalledProcessError as exc:
            auth_db.update_model_route(existing["id"], status="error",
                                        status_detail=_stderr_or_stdout(exc))
            logger.warning(
                "re-pinning route %s failed: %s",
                existing["id"], _stderr_or_stdout(exc),
            )
        if set_as_default:
            auth_db.set_default_model_route(existing["id"])
        return auth_db.get_model_route(existing["id"])

    # 2. Fresh provision
    return provision_new_route(provider, model, set_as_default=set_as_default)


def restart_route(route_id: str) -> dict:
    """Stop and re-start the OpenShell gateway underlying a route.

    Useful when the gateway has wedged or after the host reboots without
    an autostart unit. Sandboxes will lose their connection during the
    restart and will need to respawn.
    """
    route = auth_db.get_model_route(route_id)
    if not route:
        raise RuntimeError(f"route {route_id} not found")

    auth_db.update_model_route(route_id, status="provisioning",
                                status_detail="restart in progress")

    name = route["openshell_name"]
    port = route["openshell_port"]

    try:
        _run_openshell("gateway", "stop", gateway=name, check=False, timeout=60)
        _run_openshell(
            "gateway", "start",
            "--name", name,
            "--port", str(port),
            timeout=300,
        )
        _run_openshell(
            "inference", "set",
            "--provider", route["provider"],
            "--model", route["model"],
            "--no-verify",
            gateway=name,
        )
        auth_db.update_model_route(route_id, status="ready", status_detail=None)
        return auth_db.get_model_route(route_id)
    except subprocess.CalledProcessError as exc:
        auth_db.update_model_route(route_id, status="error",
                                    status_detail=_stderr_or_stdout(exc))
        raise RuntimeError(f"restart failed for route {route_id}: {_stderr_or_stdout(exc)}")
    except subprocess.TimeoutExpired:
        auth_db.update_model_route(route_id, status="error",
                                    status_detail="restart timeout")
        raise RuntimeError(f"restart timed out for route {route_id}")


def destroy_route(route_id: str) -> bool:
    """Tear down an OpenShell gateway and remove its route record.

    Refuses if:
      * any agents are still bound to this route (caller must re-bind
        or delete those agents first via update_agent), or
      * this is the last remaining route (deleting it would leave Logos
        with no way to route inference at all, effectively bricking the
        install until /setup is re-run from scratch).

    The "last route" guard replaces the old ``is_primordial`` deletion
    guard — no more special-casing the bootstrap gateway; the rule is
    just "don't let the user paint themselves into a corner."
    """
    route = auth_db.get_model_route(route_id)
    if not route:
        return False
    bound = auth_db.count_agents_using_route(route_id)
    if bound > 0:
        raise RuntimeError(
            f"cannot destroy route {route_id}: {bound} agent(s) still bound. "
            f"Re-bind or delete those agents first."
        )
    remaining_after = [
        r for r in auth_db.list_model_routes() if r["id"] != route_id
    ]
    if not remaining_after:
        raise RuntimeError(
            f"cannot destroy route {route_id} ({route['openshell_name']}): "
            f"it's the only model route left. Provision another route via "
            f"Admin → Model Routes before destroying this one, or re-run /setup."
        )

    # Best-effort destroy of the underlying gateway. Even if openshell
    # complains (gateway already gone, etc.), we still want to drop the
    # DB row so the UI doesn't show a phantom route.
    #
    # Note: openshell 0.0.33 dropped the `--force` flag from `gateway
    # destroy` (used to skip the interactive confirmation prompt). The
    # CLI now defaults to non-interactive when stdin isn't a TTY — which
    # is always true for us since we're subprocess.run()-ing it — so
    # passing `--force` made every delete error out with rc=2
    # "unexpected argument" and silently leave the cluster container
    # behind. Our own orphan clusters came from exactly this path.
    rc = 0
    stderr_tail = ""
    try:
        result = _run_openshell(
            "gateway", "destroy",
            gateway=route["openshell_name"], check=False, timeout=120,
        )
        rc = result.returncode
        stderr_tail = (result.stderr or "").strip()[-500:]
    except Exception as exc:
        logger.warning(
            "openshell gateway destroy failed for %s: %s — dropping DB row anyway",
            route["openshell_name"], exc,
        )
    if rc != 0:
        logger.warning(
            "openshell gateway destroy for %s returned rc=%d: %s — "
            "cluster container may be left as an orphan; "
            "_prune_orphan_gateways at next gateway startup will catch it.",
            route["openshell_name"], rc, stderr_tail,
        )

    return auth_db.delete_model_route(route_id)
