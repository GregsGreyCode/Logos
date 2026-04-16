"""Capabilities — user-facing collapse of toolsets + presets + credentials.

End users don't think "is the browserless preset applied" — they think "can
my agent browse the web". This module loads the human-readable capability
catalogue from ``gateway/policies/capabilities.yaml`` and exposes:

  * ``load_catalogue()`` — parsed YAML, cached after first load.
  * ``compute_state(agent_id)`` — for each capability, returns
    {enabled, missing_creds, ready} so the UI knows which toggles are on,
    off, or off-but-blocked-by-missing-credentials.
  * ``apply(agent_id, cap_id, enabled)`` — atomically toggles all toolsets +
    presets the capability bundles. Returns the recomputed state so the
    caller (the toggle endpoint) can re-render without a follow-up GET.

The capability YAML is the single source of truth for the bundling. Do
not call ``policies.apply_preset`` / ``toggleToolset`` directly from the
capability layer — go through ``apply()`` so the bundling stays atomic.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from gateway.auth import db as auth_db
from gateway import policies as gp

logger = logging.getLogger(__name__)

_CATALOGUE_PATH = Path(__file__).parent / "policies" / "capabilities.yaml"
_cached: Optional[dict] = None


def load_catalogue() -> dict:
    """Load + cache the capability YAML. Re-reads only on import (cheap)."""
    global _cached
    if _cached is None:
        try:
            with open(_CATALOGUE_PATH) as f:
                _cached = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("capabilities.yaml not found at %s — empty catalogue", _CATALOGUE_PATH)
            _cached = {"version": 1, "always_on": [], "capabilities": [], "power_tools": []}
    return _cached


def _all_caps_flat() -> list[dict]:
    """All capabilities (always_on + capabilities + power_tools) flat."""
    cat = load_catalogue()
    return [
        *(cat.get("always_on") or []),
        *(cat.get("capabilities") or []),
        *(cat.get("power_tools") or []),
    ]


def find(cap_id: str) -> Optional[dict]:
    """Return one capability by id, or None."""
    return next((c for c in _all_caps_flat() if c.get("id") == cap_id), None)


def _agent_state(agent_id: str) -> tuple[set[str], set[str]]:
    """Read agent's current (toolsets, presets) as sets."""
    agent = auth_db.get_agent(agent_id) or {}
    raw_ts = agent.get("toolsets") or "[]"
    try:
        toolsets = set(json.loads(raw_ts) if isinstance(raw_ts, str) else raw_ts)
    except (json.JSONDecodeError, TypeError):
        toolsets = set()
    presets = set(gp.get_applied_presets(agent_id))
    return toolsets, presets


def _creds_present(creds: list[str], any_one: bool = False) -> tuple[bool, list[str]]:
    """Check which required env vars are set. Returns (ok, missing).

    ``any_one=True`` (legacy ``creds_any``): treat the list as alternatives
    where any single one satisfies the requirement. Used to be relevant
    for the merged Cloud AI capability — kept for forward compatibility
    if a capability ever wants this semantics again.
    """
    if not creds:
        return True, []
    if any_one:
        ok = any(os.environ.get(k) for k in creds)
        missing = [] if ok else creds
    else:
        missing = [k for k in creds if not os.environ.get(k)]
        ok = not missing
    return ok, missing


def compute_state(agent_id: str) -> dict:
    """Return the per-capability enabled/ready map for one agent.

    Output shape (consumed by the UI):

        {
          "always_on": [{id, name, icon, description, ...}, ...],
          "capabilities": [
            {
              id, name, icon, description, trust, trust_note,
              toolsets, presets, creds, layer1,
              enabled: bool,         # all toolsets+presets currently applied?
              ready: bool,           # creds requirement satisfied?
              missing_creds: [...],  # blocker for going from off → ready
            },
            ...
          ],
          "power_tools": [...same shape as capabilities...],
        }

    A capability is ``enabled`` when every toolset and every preset it
    declares is currently applied to the agent (full bundle, not partial).
    A capability is ``ready`` when its required env vars are present, so
    the UI can disable the toggle and surface a "needs API key" hint
    rather than letting the user toggle into a known-broken state.
    """
    toolsets_have, presets_have = _agent_state(agent_id)
    cat = load_catalogue()

    def annotate(cap: dict) -> dict:
        ts = set(cap.get("toolsets") or [])
        ps = set(cap.get("presets") or [])
        creds = cap.get("creds") or []
        enabled = ts.issubset(toolsets_have) and ps.issubset(presets_have)
        ready, missing = _creds_present(creds, any_one=bool(cap.get("creds_any")))
        out = dict(cap)
        out["enabled"] = enabled
        out["ready"] = ready
        out["missing_creds"] = missing
        return out

    return {
        "version": cat.get("version", 1),
        "always_on": [annotate(c) for c in (cat.get("always_on") or [])],
        "capabilities": [annotate(c) for c in (cat.get("capabilities") or [])],
        "power_tools": [annotate(c) for c in (cat.get("power_tools") or [])],
    }


def format_agent_prompt_block(agent_id: str) -> str:
    """Compact, markdown-ish summary of the agent's permissions for the
    system prompt.

    Each dispatch ships this in instance_config so the sandbox worker can
    prepend it to the agent's context. Without it, agents don't know
    which capabilities are disabled — so when a user asks for something
    gated behind a disabled permission (e.g. Firecrawl search or fal
    image-gen), the agent either fails opaquely or hallucinates. With
    this block, the agent can tell the user exactly which toggle they
    need to flip in the P dropdown to unlock the request.

    Kept deliberately small (~200-300 tokens) and positive-voiced for
    smaller models that pattern-match "restricted" to "give up".

    Returns an empty string on any error so a capability-system outage
    never blocks dispatches.
    """
    try:
        state = compute_state(agent_id)
    except Exception as exc:
        logger.debug("format_agent_prompt_block: compute_state failed: %s", exc)
        return ""

    # Map the three-tier trust taxonomy to short user-facing labels.
    # Keep the labels terse so the prompt block stays compact even with
    # a dozen capabilities.
    trust_label = {
        "sandbox":       "sandbox",
        "local_service": "local",
        "third_party":   "cloud",
    }

    lines: list[str] = [
        "## Your permissions",
        "",
        "Your current permissions — granted per-agent (not per-session), "
        "editable via the green **P** badge at the top of the chat header. "
        "The P dropdown is the ONLY place users toggle permissions; "
        "never send them to `Settings → Tools` or `Config → Tools` "
        "unless a missing API key specifically needs configuring there.",
        "",
    ]

    # Enabled first — positive framing, shows what the agent CAN do.
    enabled = [c for c in state.get("capabilities", []) if c.get("enabled")]
    enabled += [c for c in state.get("power_tools", []) if c.get("enabled")]
    if enabled:
        lines.append("Enabled now:")
        for c in enabled:
            pill = trust_label.get(c.get("trust", "sandbox"), "sandbox")
            lines.append(f"- {c.get('icon', '·')} **{c.get('name', c['id'])}** ({pill}) — {c.get('description', '').strip()}")
        lines.append("")

    # Disabled — each with the concrete reason (missing cred vs just off)
    # so the agent can tell the user exactly what to do.
    disabled = [c for c in state.get("capabilities", []) if not c.get("enabled")]
    disabled += [c for c in state.get("power_tools", []) if not c.get("enabled")]
    if disabled:
        lines.append("Not enabled (tick the box in the **P** dropdown to turn on):")
        for c in disabled:
            pill = trust_label.get(c.get("trust", "sandbox"), "sandbox")
            missing = c.get("missing_creds") or []
            if missing:
                hint = (
                    f"user must first set {', '.join(missing)} in Config → Tools, "
                    "then tick this row in the P dropdown"
                )
            else:
                hint = "tick this row in the P dropdown"
            lines.append(
                f"- {c.get('icon', '·')} **{c.get('name', c['id'])}** ({pill}) — {c.get('description', '').strip()} ({hint})"
            )
        lines.append("")

    # Always-on block — short reminder these don't need a toggle, so the
    # agent doesn't redundantly ask the user to "enable Memory".
    always = state.get("always_on") or []
    if always:
        names = ", ".join(f"{c.get('icon', '')} {c.get('name', c['id'])}" for c in always)
        lines.append(f"Always on (no toggle needed): {names}")
        lines.append("")

    lines.append(
        "If the user asks for something that needs a disabled permission, "
        "name it exactly — e.g. \"the 🔎 Search the web locally (SearxNG) "
        "toggle in the P dropdown\" — rather than vague phrasing like "
        "\"enable it in settings\". Don't describe the capability as "
        "broken or unavailable; it's just off."
    )

    return "\n".join(lines).strip()


def probe_service(cap: dict) -> Optional[dict]:
    """Probe a local_service capability's health endpoint before enabling.

    Returns ``None`` when the capability either has no ``service_probe``
    field or the probe succeeds. Returns ``{"ok": False, "hint": ...,
    "url": ..., "detail": ...}`` when the service isn't reachable or
    isn't returning the expected response shape.

    By default probes the URL in ``service_probe.url`` and treats any
    2xx as healthy. When ``service_probe.expect_contains`` is set, the
    probe additionally requires the response BODY to contain that
    substring — catches "service is up but misconfigured" cases (e.g.
    SearxNG with JSON format disabled, Ollama with no models loaded).

    Without expect_contains the probe is shallow: a service that binds
    a port but returns 403 on the feature URL passes, which is how a
    silently-broken install escapes detection until first use. Setting
    expect_contains = a string unique to a successful response
    (e.g. '"results"' for SearxNG JSON) turns the probe into a true
    functional check.
    """
    probe = cap.get("service_probe") or {}
    if not probe:
        return None
    default_url = probe.get("url")
    env_override = probe.get("env_override")
    probe_url = (env_override and os.environ.get(env_override)) or default_url
    if not probe_url:
        return None
    # Allow users to set SEARXNG_URL=http://host:port without the
    # probe path — append the path from the capability's probe URL
    # so the override still works.
    if env_override and os.environ.get(env_override) and default_url:
        from urllib.parse import urlsplit
        default_parts = urlsplit(default_url)
        default_path_and_query = default_parts.path or "/"
        if default_parts.query:
            default_path_and_query += "?" + default_parts.query
        override_parts = urlsplit(probe_url)
        if not override_parts.path or override_parts.path == "/":
            probe_url = probe_url.rstrip("/") + default_path_and_query
    expect_contains = probe.get("expect_contains")
    hint = probe.get("install_hint") or "Service isn't reachable."
    import urllib.request
    import urllib.error

    def fail(detail: str) -> dict:
        return {"ok": False, "url": probe_url, "hint": hint, "detail": detail}

    def _read(resp) -> bytes:
        try:
            return resp.read(65536)
        except Exception:
            return b""

    try:
        req = urllib.request.Request(probe_url, method="GET")
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            body = _read(resp)
            if resp.status >= 500:
                return fail(f"HTTP {resp.status}")
            if expect_contains and expect_contains not in body.decode(errors="replace"):
                # HTTP-wise the service answered, but the response
                # doesn't look like a working install. Most common
                # cause: a feature the capability relies on (e.g.
                # JSON output) isn't enabled in the service's config.
                return fail(
                    f"HTTP {resp.status} but response missing {expect_contains!r} — "
                    f"service is up but the feature isn't working."
                )
            return None
    except urllib.error.HTTPError as exc:
        # 4xx means the service answered but rejected the probe. When
        # there's no expect_contains we accept 4xx as "alive" (some
        # services 404 on /healthz but are fine). When we DO expect
        # specific content, 4xx is a failure — we want a functional
        # endpoint, not just any HTTP response.
        if expect_contains:
            return fail(f"HTTP {exc.code} {exc.reason}")
        if 200 <= exc.code < 500:
            return None
        return fail(f"HTTP {exc.code} {exc.reason}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return fail(str(exc))


def _host_port_in_use(port: int) -> Optional[str]:
    """Return a short description of who owns ``port`` on the host, or None
    if the port is free. Used by install_service to fail fast with an
    actionable error before shelling docker compose — the raw docker
    "port is already allocated" output doesn't tell the user what's
    holding it.

    Best-effort — we only report the process name / PID when lsof or
    /proc works; otherwise we just say "port is in use" and let the
    admin investigate.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            pass
        else:
            return None  # bind succeeded → port free
    # Port is bound — try to identify the process holding it.
    import subprocess
    for cmd in (
        ["lsof", "-iTCP", f":{port}", "-sTCP:LISTEN", "-nP"],
        ["ss", "-tlnp", f"sport = :{port}"],
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().splitlines()[-1][:200]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return f"port {port} is already in use"


def install_service(cap: dict, compose_dir: Optional[str] = None) -> dict:
    """Bring a capability's backing compose service up and wait for probe.

    Returns a dict shape:
        {"ok": True, "log": "..."}             on success
        {"ok": False, "error": "...", "log": ...}  on failure

    Only runs when the capability declares ``service_install``. The
    gateway's working directory (or the passed ``compose_dir``) must
    contain the docker-compose.yml that defines the target service —
    same file that ships with Logos.

    Pre-flight: if ``service_install.host_port`` is set, checks the
    port is free on the host before shelling docker compose. Raw
    docker conflicts ("Bind for 0.0.0.0:8080 failed") don't tell the
    user what's holding the port; we do.

    Post-success: if ``service_install.sandbox_env`` declares env vars,
    they're persisted to the services credential store so agent
    sandboxes see them on their next spawn — same mechanism other
    tool integrations use.

    Fails gracefully when Docker isn't reachable (rootless deployments,
    missing socket, wrong cwd) — the UI shows the error so users can
    fall back to a manual ``docker compose up`` command.
    """
    install = cap.get("service_install") or {}
    profile = install.get("docker_compose_profile")
    service = install.get("docker_compose_service")
    timeout_s = int(install.get("timeout_seconds") or 60)
    host_port = install.get("host_port")
    sandbox_env = install.get("sandbox_env") or {}
    if not profile or not service:
        return {"ok": False, "error": "capability has no service_install metadata"}
    # Pre-flight port check — fail fast with a useful message if
    # something else is already bound to the host port. Docker's
    # "port is already allocated" doesn't tell the user which
    # process to kill or which port to pick instead.
    if host_port:
        holder = _host_port_in_use(int(host_port))
        if holder is not None:
            return {
                "ok": False,
                "error": f"host port {host_port} is already in use",
                "detail": holder,
                "hint": f"Free port {host_port}, or set SEARXNG_PORT=<other port> in .env and retry.",
            }
    # Resolve compose dir — caller-provided > cwd. Cwd is what the
    # gateway already runs from for a standard repo-root launch.
    cwd = compose_dir or os.getcwd()
    import subprocess
    cmd = ["docker", "compose", "--profile", profile, "up", "-d", service]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "docker CLI not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"`docker compose up` timed out after {timeout_s}s"}
    except Exception as exc:
        return {"ok": False, "error": f"`docker compose up` failed: {exc}"}
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return {"ok": False, "error": f"docker compose exited {proc.returncode}", "log": log}
    # Poll the reachability probe for up to timeout_s so the caller can
    # apply the capability straight after — the service usually needs a
    # few seconds to come up even after `up -d` returns.
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        probe = probe_service(cap)
        if probe is None:
            # Persist any declared sandbox-env so agent sandboxes pick
            # it up on next dispatch. Best-effort; log + continue on
            # failure so a DB hiccup doesn't undo a successful start.
            if sandbox_env:
                try:
                    from gateway import services as _services
                    for k, v in sandbox_env.items():
                        _services.set_credential(k, str(v))
                except Exception as exc:
                    logger.warning("install_service: persisting sandbox_env failed: %s", exc)
            return {"ok": True, "log": log}
        _time.sleep(1.0)
    return {
        "ok": False,
        "error": "service started but probe still failing after install",
        "log": log,
    }


def apply(agent_id: str, cap_id: str, enabled: bool) -> dict:
    """Atomically apply or remove all toolsets + presets for one capability.

    Returns the recomputed full state (same shape as ``compute_state``)
    so the toggle endpoint can return it directly and the UI can re-render
    without a follow-up GET.

    Bundling: a single capability may map to multiple toolsets and presets;
    we apply them all (or remove them all) so the agent never lands in a
    half-state where e.g. the toolset is enabled but the matching network
    grant is missing.
    """
    cap = find(cap_id)
    if not cap:
        raise ValueError(f"unknown_capability:{cap_id}")

    agent = auth_db.get_agent(agent_id)
    if not agent:
        raise ValueError(f"agent_not_found:{agent_id}")

    target_toolsets = set(cap.get("toolsets") or [])
    target_presets = set(cap.get("presets") or [])

    # ── Toolsets: read–mutate–write the JSON column on agents.toolsets
    raw_ts = agent.get("toolsets") or "[]"
    try:
        current_ts = json.loads(raw_ts) if isinstance(raw_ts, str) else list(raw_ts)
        if not isinstance(current_ts, list):
            current_ts = []
    except json.JSONDecodeError:
        current_ts = []
    current_ts_set = set(current_ts)
    if enabled:
        new_ts = sorted(current_ts_set | target_toolsets)
    else:
        # Don't strip a toolset that another (still-enabled) capability
        # also wants. Compute "still wanted" by walking the catalogue.
        keep = _other_capabilities_still_using_toolsets(agent_id, cap_id, "toolsets")
        new_ts = sorted((current_ts_set - target_toolsets) | keep)
    if new_ts != current_ts:
        auth_db.update_agent(agent_id, toolsets=json.dumps(new_ts))

    # ── Presets: each one round-trips through gateway.policies which also
    #    pushes the merged effective policy to the running sandbox.
    if enabled:
        for preset in target_presets:
            try:
                gp.apply_preset(agent_id, preset)
            except gp.PresetNotFound:
                logger.warning(
                    "capability %s references unknown preset %s — skipping",
                    cap_id, preset,
                )
            except Exception as exc:
                logger.warning(
                    "capability %s: apply_preset(%s) failed: %s",
                    cap_id, preset, exc,
                )
    else:
        keep_presets = _other_capabilities_still_using_toolsets(agent_id, cap_id, "presets")
        for preset in target_presets:
            if preset in keep_presets:
                continue
            try:
                gp.remove_preset(agent_id, preset)
            except Exception as exc:
                logger.warning(
                    "capability %s: remove_preset(%s) failed: %s",
                    cap_id, preset, exc,
                )

    return compute_state(agent_id)


def apply_initial_defaults(agent_id: str) -> list[str]:
    """Apply B-tier defaults to a freshly-created agent.

    B-tier = "safe by default, dangerous by choice":
      - every capability in ``always_on`` (local-only internals: memory,
        files, skills, schedule, plan, clarify, delegate, world)
      - every capability flagged ``default_on_create: true`` in the YAML
        (today: web + code_execution — both local-only, broadly useful)

    Called once from ``handle_agents_post`` so every new agent spawns
    with a useful toolbox instead of an empty one. Third-party
    capabilities (anything that sends data outside the machine) stay
    off until the user explicitly toggles them in STAMP → P.

    Returns the list of capability ids applied so the caller can log
    them for audit / debugging.
    """
    cat = load_catalogue()
    applied: list[str] = []
    for cap in (cat.get("always_on") or []):
        try:
            apply(agent_id, cap["id"], True)
            applied.append(cap["id"])
        except Exception as exc:
            logger.warning(
                "apply_initial_defaults: always_on %s failed: %s",
                cap.get("id"), exc,
            )
    for cap in (cat.get("capabilities") or []):
        if cap.get("default_on_create"):
            try:
                apply(agent_id, cap["id"], True)
                applied.append(cap["id"])
            except Exception as exc:
                logger.warning(
                    "apply_initial_defaults: default_on_create %s failed: %s",
                    cap.get("id"), exc,
                )
    return applied


def _other_capabilities_still_using_toolsets(agent_id: str, exclude_cap_id: str, key: str) -> set[str]:
    """Compute the set of toolsets/presets that OTHER enabled capabilities
    on this agent still want, so we don't strip a shared dependency when
    the user toggles one capability off.

    `key` is "toolsets" or "presets".
    """
    state = compute_state(agent_id)
    keep: set[str] = set()
    for bucket in ("always_on", "capabilities", "power_tools"):
        for c in state.get(bucket) or []:
            if c.get("id") == exclude_cap_id:
                continue
            # always_on is always wanted; on/off doesn't matter here.
            if bucket == "always_on" or c.get("enabled"):
                keep.update(c.get(key) or [])
    return keep
