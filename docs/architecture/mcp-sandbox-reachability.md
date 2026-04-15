# MCP server reachability from sandboxes

**Status:** current as of 2026-04-15, OpenShell `m-dev` (post-v0.0.29 rolling)
**Decision:** Allow sandbox → gateway on `host.openshell.internal:8091` via
`allowed_ips: ["172.0.0.0/8"]` with L7 rules restricted to `/mcp/*`.

## Problem

Auto-granted MCP servers deploy as docker containers on the host, bound to
`127.0.0.1:<host_port>`. The Logos gateway proxies them at
`http://host.openshell.internal:8091/mcp/<name>` so sandboxes only need
one reachable host. From inside a Hermes sandbox, that dial-back was
failing with two distinct errors at two different enforcement layers:

1. **OpenShell network policy** — `policy_denied` (HTTP 403). The baseline
   policy (`gateway/policies/openshell_default.yaml`) only allowed
   `inference.local:443` and `*.openshell.internal:53` for DNS. No rule
   permitted HTTP to `host.openshell.internal:8091`.

2. **OpenShell L7 SSRF filter** — `ssrf_denied` (HTTP 403). Even after
   adding a bare `host.openshell.internal:8091` endpoint, OpenShell
   rejects requests to private-IP ranges (172.17.0.1 is the docker
   bridge) as SSRF unless explicitly allowlisted.

## What we rejected

- **`bypasses_l7: true`** — would disable L7 inspection entirely. Pokes
  a hole through OpenShell's security architecture. We'd lose path/
  method scoping and the audit trail for gateway-proxied calls.
- **`tls: passthrough`** — similar spirit, masks traffic from OpenShell
  inspection. Also doesn't apply cleanly since the gateway serves plain
  HTTP on the bridge.
- **Routing MCP through `inference.local`** — architecturally cleanest
  (matches the pattern TASKS.md describes for worker registration), but
  requires a config change to OpenShell's privacy router which we don't
  own. Parked as future work; see "Triggers to revisit" below.

## What we chose

Two-layer allowance in `openshell_default.yaml`:

```yaml
gateway_mcp:
  name: "Gateway MCP proxy (auto-granted tool servers)"
  endpoints:
    - host: host.openshell.internal
      port: 8091
      protocol: rest
      enforcement: enforce
      allowed_ips:
        - "172.0.0.0/8"        # sanctioned private-IP allowlist
      rules:                    # L7-restrict to the MCP proxy path
        - allow: {method: POST, path: /mcp/*}
        - allow: {method: GET,  path: /mcp/*}
  binaries:
    - path: /app/venv/bin/python
    - path: /app/venv/bin/python3
    # ...
```

`allowed_ips` is the **documented** OpenShell mechanism for this case —
see `knowledge-repos/openshell/architecture/security-policy.md` ("Private
IP Access via `allowed_ips`") and the e2e test
`knowledge-repos/openshell/e2e/rust/tests/forward_proxy_l7_bypass.rs:142-
150` for reference syntax. Load-time validation rejects overlap with
always-blocked ranges (loopback, link-local), so misconfigs fail early.

The L7 `rules` scope the capability tightly: the sandbox can only POST/
GET under `/mcp/*`. `/admin`, `/api/agents`, and every other gateway
endpoint remain blocked. A compromised MCP auto-grant cannot be turned
into a gateway-admin capability.

## How the pieces fit

1. Gateway registers auto-granted MCP server → writes `mcp_servers`
   entry into each sandbox's `/tmp/hermes/instance-config.json` with
   URL `http://host.openshell.internal:8091/mcp/<name>` + header
   `X-Session-Id: <worker_id>`.
2. `sandbox_worker.py` `load_config()` writes `~/.hermes/config.yaml`
   with that entry (url + transport + headers).
3. Gateway `grant_access(worker_id, <server>)` registers a session
   grant in `mcp_access._grants` so the proxy's auth check passes.
4. Sandbox imports `core.model_tools` → `discover_mcp_tools()` reads
   `~/.hermes/config.yaml` → connects streamable-HTTP through
   `host.openshell.internal:8091` → tools register under `mcp-<name>`
   toolset in the sandbox's tool registry.
5. AIAgent gets `enabled_toolsets=[..., "mcp-<name>"]` → tool call
   routes through tool registry handler → HTTP POST to gateway proxy
   → gateway forwards to docker container → response flows back.

## Confidence

- **High**: the `allowed_ips + path rules` syntax — copied verbatim from
  OpenShell's own e2e tests.
- **High**: the four layers involved (instance-config.json, config.yaml,
  grant registry, discover_mcp_tools) — traced end-to-end in live logs
  during debugging, 2026-04-15 session.
- **Medium**: whether `172.0.0.0/8` is the right CIDR long-term. Docker
  defaults to `172.17.0.0/16`, but user-defined networks can land
  elsewhere in 172.16/12. Using the /8 keeps bridge reconfigs from
  silently breaking MCP. Could tighten to /16 if we ever want stricter.

## Triggers to revisit

- **OpenShell adds MCP as a first-class primitive.** Zero references to
  MCP exist in the OpenShell repo as of 2026-04-15 (commit 355d845d).
  If they add `openshell sandbox mcp-deploy` or similar, we should
  migrate — the current docker-container approach is Logos-specific
  scaffolding around a gap.
- **Privacy-router HTTP routing becomes available.** TASKS.md item
  describes exposing gateway endpoints through `inference.local` the
  same way LLM traffic is routed. That would eliminate the need for
  `allowed_ips` entirely — the sandbox would reach MCP via an
  already-allowed TLS endpoint.
- **Bridge CIDR changes.** If the user reconfigures docker networking
  outside 172/8, the allowlist needs updating. Symptom would be
  `ssrf_denied` reappearing even after policy apply.

## Related

- Layer-7 enforcement flows: `knowledge-repos/openshell/architecture/
  security-policy.md`
- Proxy rejection codes: `knowledge-repos/openshell/crates/openshell-
  sandbox/src/proxy.rs` (lines 541, 585, 635 per OpenShell audit)
- Logos-side wiring: `gateway/executors/openshell.py:_auto_granted_mcp_*`
  helpers, `gateway/http_api.py` MCP startup rewire, `docker/
  sandbox_worker.py` `load_config()` YAML writer.
