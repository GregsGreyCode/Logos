# Logos open work

A snapshot of pending work captured during the long debug session of
2026-04-09 / 04-10. Written so we don't lose context if a session
crashes again.

## Pending — infra regression

### #24 Sandbox worker registration silently fails over CONNECT tunnel — REGRESSION
Observed 2026-04-11. Confirmed a regression — "we had this working before". Spent a long debugging session on it and hit a wall without observability tooling (see MISSING.md M6 for why that was the real blocker).

**Symptom**: In `/admin/sandboxes`, workers show **disconnected / unhealthy / uptime 0s**. Chat is blocked on the UI with "sandbox is provisioning, chat will unlock when ready." Both `hermes-hemette` and `hermes-kai` reproduce.

**What works**:
- OpenShell sandboxes spawn cleanly and reach `Ready` phase.
- The sandbox's `openshell-sandbox` supervisor (PID 1 inside the pod) runs.
- `python3 /app/sandbox_worker.py` launches when manually invoked with `/tmp/hermes/instance-config.json` populated.
- `TunnelWebSocket.connect()` completes — HTTP CONNECT returns 200, WebSocket upgrade returns 101 Switching Protocols through the tunnel.
- Worker logs `"Connected to gateway via CONNECT tunnel"` successfully.
- Worker calls `await ws.send_json({"type":"register",...})` and it returns (write succeeds — proven via DEBUG-pre/DEBUG-post instrumentation).
- **Gateway-side handler works correctly when hit directly**: `./venv/bin/python` with a plain `aiohttp.ClientSession.ws_connect("http://localhost:8091/ws/worker")` → send register → receive `{"type":"registered",...}` in under 1 second. Verified multiple times on the running gateway.

**What doesn't work**:
- After the worker's register `send_json` returns, a **raw `ws._reader.read(2)` with a 5-second timeout** gets **zero bytes** from the gateway. Not a partial frame, not a close, nothing. Proven with a patched `/tmp/sw.py` inside the sandbox. So the reverse direction of the CONNECT tunnel is dropping the gateway's registration reply somewhere in the NAT path.

**Network topology** (confirmed from inside `hermes-hemette`):
```
hermes-hemette pod (10.42.0.22)
  default gw 10.200.0.1 (veth-s-4ffe69c9, the k3s CNI veth)
  /etc/hosts: 172.17.0.1 → host.openshell.internal (HostAlias, not DNS)
  → cluster container's network stack
  → Docker bridge default gateway 172.17.0.1
  → host's :8091 (Logos gateway process PID ... listening)
```
There is **no explicit proxy** in this path — it's kernel-level Docker NAT + k3s CNI forwarding. If TCP establishes, bytes should flow both directions. They do not, at least not for register replies.

**Things tried that did NOT fix it**:
1. Swapping the gateway venv from `./.venv/` (uv-managed, 3.11) back to `./venv/` (system, 3.12) to match the original running process — see `feedback_check_venvs_before_launch.md`. Both venvs have aiohttp 3.13.x, only off by a patch. Not the cause.
2. `docker restart openshell-cluster-logos-openshell` to reset the nested-k3s cluster container. Wiped sandbox pod `/tmp` state but did NOT change the tunnel behavior. The reverse-direction silence reproduces identically on a freshly-rebuilt sandbox post-restart.
3. Verifying `openshell-sandbox` binary md5 inside the cluster container — it's still the **patched** version (`b7e682634c95bf62d210fd4269c639f3`) from #22/#23, so the 600s router timeout patch didn't regress.
4. Killing and relaunching worker processes inside the sandbox, both the default `/app/sandbox_worker.py` and a patched `/tmp/sw.py`. Same silence.

**ROOT CAUSE (proven, not just suspected)**: `TunnelWebSocket` in `docker/sandbox_worker.py:57-122` is a **custom HTTP CONNECT escape hatch** that was written to bypass the OpenShell L7 proxy. Its own docstring is explicit: *"The OpenShell L7 proxy intercepts plain HTTP requests (including WebSocket upgrades) which breaks the handshake. Using a CONNECT tunnel creates a raw TCP pipe that passes the WebSocket upgrade through unmodified."*

**OpenShell has closed that escape hatch.** The CONNECT tunnel still passes enough to complete the HTTP upgrade (the gateway's access log shows `"GET /ws/worker HTTP/1.1" 101` for every sandbox connection) but the post-upgrade WebSocket frames no longer reach the gateway's `async for msg in ws:` parser. openshell-docs (at `~/homelab-infra/projects/knowledge-repos/openshell-docs`) contains **zero mentions of HTTP CONNECT** as a supported mechanism — it describes the gateway as *"providing the SSH tunnel endpoint"*, SSH only. The CONNECT hack was undocumented by design, and the proxy has tightened.

**Proof chain**:
1. Gateway's `/ws/worker` works perfectly for any **direct** aiohttp client from the host (`./venv/bin/python aiohttp.ClientSession.ws_connect` → register → `Worker registered` logged in `~/.logos/logs/gateway.log` within ~100ms). Verified multiple times: `test-probe-1`, `test-probe-2`, `hermes-hemette-probe`.
2. Gateway's WebSocket handler parses TunnelWebSocket-format frames **correctly** — confirmed by running a host-side `TunnelWebSocket.connect_direct("127.0.0.1", 8091)` variant (bypassing the OpenShell CONNECT tunnel, going straight TCP to localhost) and successfully receiving `{"type": "registered", ...}` back. So the frame format isn't the bug.
3. Gateway's access log shows `172.28.0.2 [...] GET /ws/worker [...] 101` for every sandbox-originated connection — the 101 upgrade IS reaching the gateway from the sandbox. The TCP path exists.
4. **No "Worker registered" log entry** appears for any sandbox-originated `172.28.0.2` connection, only for direct `127.0.0.1` connections. This means `worker_registry.handle_ws`'s `async for msg in ws:` loop **never yields a TEXT message** for sandbox workers even though the WS is alive.
5. The sandbox-side patched worker confirms `send_json` returns successfully, but a raw `ws._reader.read(2)` with a 5s timeout gets **zero bytes back**. So the sandbox's TCP writes go out and reply bytes never come back through the tunnel.

Conclusion: bytes leave the sandbox, the OpenShell L7 proxy intercepts them *after* the HTTP upgrade completes, and either drops the reply frames or holds them until connection close (which matches the 15-minute access-log lag we see — aiohttp writes the access log when the connection closes, and those logged-delays match exactly the `async for msg in ws` sitting idle waiting for messages that never arrive until the sandbox worker times out and disconnects).

**Real fix direction** (for a future session, not today):
1. **Stop using HTTP CONNECT tunneling entirely.** Per openshell-docs/configureinferencerouting.md:
   - `https://inference.local` is the gateway-managed HTTPS endpoint that sandboxes can reach. *"HTTPS only: inference.local is intercepted only for HTTPS traffic."* This is the **supported** channel from a sandbox to the gateway. Plain HTTP to arbitrary hosts is intercepted by the L7 proxy and subject to `network_policies`.
   - *"External inference endpoints go through sandbox network_policies. Refer to Policies for details."* So any outbound to a non-inference-local endpoint is policy-gated, and that gating may now be more strict than when the CONNECT hack was written.
   - Read `openshell-docs/gateways.md`, `configureinferencerouting.md`, `Python.md`, and `policies.md` for the supported patterns before touching this.
2. **Concrete fix options**, ranked by feasibility:
   - **(A) Expose worker registration via `inference.local`.** Route `/ws/worker` (or equivalent) through the privacy router so sandboxes call `https://inference.local/ws/worker` instead of `http://host.openshell.internal:8091/ws/worker`. Requires a provider/config addition on the primordial gateway. Biggest architectural alignment with OpenShell's intended model.
   - **(B) Invert the control flow.** Instead of the sandbox initiating a WebSocket out to the gateway, have the gateway initiate a persistent bidirectional control stream INTO the sandbox via `openshell sandbox exec` or similar gRPC primitive. `openshell sandbox exec --no-tty` has been rock-solid throughout this debugging session — it's clearly the supported gateway→sandbox channel. Would require rewriting `sandbox_worker.py` as a reverse-pull loop: worker spawns on sandbox startup, publishes its status file to a well-known path, gateway polls via sandbox exec + writes tasks via sandbox exec stdin.
   - **(C) Host-side worker.** Move `python3 /app/sandbox_worker.py` out of the sandbox entirely. Run the worker on the HOST alongside the gateway, where it can trivially reach `localhost:8091`. The sandbox becomes a pure execution environment that the host worker talks to via `openshell sandbox exec` for tool calls. Simplest fix if the sandbox doesn't need to run its own Python.
   - **(D) SSH port-forward.** The docs mention SSH tunnel endpoints. An `openshell ssh-proxy` subprocess on the host forwards a loopback port into the sandbox; the sandbox worker dials the forwarded socket. Historically supported — `gateway/worker_registry.py` and `gateway/executors/openshell.py` already have code for reaping orphan `openshell ssh-proxy` processes, so this was the transport at some point. Option (D) is likely what broke when OpenShell upgraded — investigate `openshell ssh-proxy` CLI to see if it's still the blessed path.
3. **Bisect `b486c74 feat: openshell debug session`** against the last known working sandbox worker commit. That 300-line commit touched `docker/sandbox_worker.py`, `gateway/worker_registry.py`, and `gateway/executors/openshell.py` all at once — `git show b486c74 --stat` has the full file list. Look specifically for changes to `TunnelWebSocket` or the CONNECT upgrade request format. Also cross-reference with OpenShell version bump timing.
4. **As a short-term workaround** for today, option (C) is the fastest — host-side worker — if we need chat working before the proper fix. Requires: worker loop in-process inside the gateway, sandbox becomes exec target not WS peer.

**Policy tweaks tried (did NOT fix it)**:
1. **Policy v2** — removed `protocol: rest` from the `logos_gateway` endpoint, keeping `access: full` and `enforcement: enforce`. Hot-reloaded via `openshell policy set --wait`. Confirmed loaded as active revision 2. **Worker still could not register** — fresh post-policy worker connection hit the same silent hang after `Connected via CONNECT tunnel`.
2. **Policy v3** — also removed `access: full` and `enforcement: enforce`, matching the `npm_registry` example in `policyschemareference.md` exactly (just host + port + allowed_ips + binaries, no HTTP-layer fields whatsoever). Hot-reloaded, confirmed active revision 3. **Still could not register.** Same silent hang.

**Emerging theory**: OpenShell's L7 proxy **auto-detects HTTP traffic by byte peeking**, regardless of the policy `protocol:` field. The schema reference explicitly says *"The proxy auto-detects TLS by peeking the first bytes of each connection"* — HTTP is almost certainly the same. Once the proxy sees `GET /ws/worker HTTP/1.1...` on the wire, it enters HTTP-inspection mode and treats the connection as request/response, **regardless of whether you asked it to**. The policy `protocol:` field is a hint, not the sole trigger. This means **no policy tweak can fix WebSocket-over-HTTP-upgrade** on this proxy — the fix must be at the transport layer.

**Fix direction — RESOLVED by NemoClaw reference (2026-04-11 late afternoon)**:

Cloned `NVIDIA/NemoClaw` into `knowledge-repos/NemoClaw` and read the full sandbox entry stack. **NemoClaw is NVIDIA's official reference for running a Hermes-family agent inside an OpenShell sandbox**, and it directly demonstrates the supported pattern that Logos should be using. The `agents/hermes/manifest.yaml` even declares the agent as `https://github.com/NousResearch/hermes-agent` — *literally the same Hermes that Logos is built on*.

**Adopt NemoClaw's architecture wholesale**. The right fix is not a transport patch — it's a structural swap from "reverse-connection sandbox worker" to "sandbox hosts the full agent, host port-forwards in". Concrete diffs:

| Aspect | Logos today (broken) | NemoClaw reference (works) |
|---|---|---|
| In-sandbox process | `python3 /app/sandbox_worker.py` — custom message pump | `hermes gateway run` — the **full** Hermes binary (same one Logos runs on the host) |
| Control transport | Reverse WebSocket via `TunnelWebSocket` (HTTP CONNECT tunnel hack) | **OpenShell port-forward** of the agent's HTTP API port (8642) — host-initiated calls |
| Direction of initiative | Sandbox → gateway (UNSUPPORTED) | Gateway → sandbox (SUPPORTED) |
| Inference channel | `https://inference.local/v1` | `https://inference.local/v1` (same — this part is fine) |
| How host reaches sandbox | `/ws/worker` WebSocket dispatch | `POST http://localhost:<forwarded>/v1/chat/completions` |
| Port in sandbox | N/A | 18642 internal (Hermes binds 127.0.0.1 due to upstream bug) → socat → 8642 external |
| Outbound HTTP shim | None | `decode-proxy.py` URL-decodes `%3A` → `:` for OpenShell placeholder tokens (needed because httpx URL-encodes colons) |
| Config integrity | None | SHA-256 hash file verified at entry, Landlock read-only `/sandbox/.hermes` |
| Privilege separation | None (worker + agent share user) | Separate `gateway` user via `gosu`, drops `cap_net_raw`, `cap_dac_override`, etc. via `capsh` |

### Concrete refactor (well-scoped, not research)

1. **Delete** `docker/sandbox_worker.py` entirely (including `TunnelWebSocket`, `run_worker`, `_handle_task`, `_run_inference` — all of it). The sandbox no longer needs a Python worker stub; it runs the full Hermes binary.
2. **Rewrite** `docker/Dockerfile.hermes-sandbox` to install the hermes binary (`curl install.sh | bash`), copy a NemoClaw-style `start.sh`, install `socat` and the decode-proxy. Reference: `knowledge-repos/NemoClaw/agents/hermes/Dockerfile` + `knowledge-repos/NemoClaw/agents/hermes/Dockerfile.base`.
3. **Adopt** `knowledge-repos/NemoClaw/agents/hermes/start.sh` as the sandbox entrypoint. Apache-2.0 licensed; can be copied wholesale with attribution. Handles: config integrity verification, capability drop, decode-proxy startup, hermes gateway launch, socat forwarder, port 8642 exposure, graceful shutdown.
4. **Copy** `knowledge-repos/NemoClaw/agents/hermes/decode-proxy.py` to `docker/decode-proxy.py`. Apache-2.0 licensed; ~90 lines. Handles Python httpx URL-encoding of `%3A` → the OpenShell placeholder pattern.
5. **Delete** `gateway/worker_registry.py`'s `handle_ws` and the `/ws/worker` route registration at `gateway/http_api.py:3669`. Workers no longer register. Keep the `WorkerRegistry` class as a sandbox-directory lookup (backed by `openshell sandbox list`) but strip all WebSocket code.
6. **Rewrite** `gateway/executors/openshell.py::OpenShellExecutor.spawn` to:
   a. `openshell sandbox create` with the NemoClaw-style blueprint policy
   b. Wait for sandbox `Ready` state
   c. Establish OpenShell port-forward for port 8642 → a local ephemeral port
   d. Store `(sandbox_name, local_port)` in the runtime registry
7. **Rewrite** `gateway/http_api.py::_handle_chat`: instead of `worker_registry.dispatch_task()` over WebSocket, make a normal `aiohttp.ClientSession.post()` to `http://127.0.0.1:<local_forward_port>/v1/chat/completions` with the agent's model. Stream the response back as SSE exactly as we do today. The only change is the transport — message shape, response handling, and everything else stays the same.
8. **Update** `gateway/policies/openshell_default.yaml` — remove the `logos_gateway` network_policy entry entirely (no more outbound traffic from sandbox → host). Keep `inference_local` and `dns`. Model the policy on `knowledge-repos/openshell-community/sandboxes/openclaw-nvidia/policy.yaml`.
9. **Test via** `/setup` wizard walkthrough from scratch. M6 unified log (`logos debug tail --follow`) makes this trivial to observe.

**Effort estimate**: 1-3 days of focused work. **Risk**: medium — significant refactor but every hard sub-problem (decode-proxy, socat forwarder, integrity check, capability drop) is already solved upstream and Apache-2.0 licensed. Primary work is: (a) deleting Logos-specific code, (b) re-wiring `_handle_chat` for the new transport, (c) integration testing through `/setup`.

**Rejected alternatives** (kept for history):
- **(A) OpenShell version upgrade to 0.0.26** — doesn't address the fundamental mismatch. The reverse-WebSocket pattern is unsupported on any version, and upgrading would lose the `openshell-sandbox` 600s timeout patch from #22/#23 without solving the real problem.
- **(C) Non-HTTP transport** (raw protobuf TCP) — same architectural mismatch. Requires inventing another escape hatch instead of using the blessed one.

**Things NOT tried (work for next session)**:
- **`tcpdump` inside the cluster container on the veth bridge** — capture actual bytes on the wire for sandbox→gateway and gateway→sandbox, definitively localize the drop point.
- **`docker logs openshell-cluster-logos-openshell`** — CHECKED. Container logs are dominated by k3s kubelet output (orphaned pod cgroups, normal nested-k8s bookkeeping) and contain **zero worker/tunnel/websocket/proxy events**. The openshell-server and openshell-router inside the container do not log individual forwarded frames. Not useful for this diagnosis.
- **OpenShell version upgrade to 0.0.26** — option (A) above. Not attempted because we'd lose the local 600s timeout patch, and because we're pivoting to M6 (unified logging) as the higher-leverage investment.
- **Remove `allowed_ips` from the `logos_gateway` policy endpoint.** 2026-04-11 late: cloned NVIDIA/OpenShell-Community into `knowledge-repos/openshell-community` and read `sandboxes/openclaw-nvidia/policy.yaml` as the reference. The minimal TCP-passthrough example uses **only** `host` + `port` + `binaries` — no `allowed_ips`. That field is not in `policyschemareference.md` either. It may be a Logos-invented field that the current OpenShell policy engine silently rejects (or treats as an unknown constraint that never matches). Next session: try a v4 policy with `allowed_ips` removed, keep only `host: host.openshell.internal`, `port: 8091`, and the `binaries` list. If that works, the fix is ONE field.

**Reference resources now available locally**:
- `knowledge-repos/openshell-community/sandboxes/openclaw-nvidia/policy.yaml` — canonical policy example from NVIDIA/OpenShell-Community. Compare Logos's `openshell_default.yaml` against this when debugging policy issues.
- `knowledge-repos/openshell-community/sandboxes/base/README.md` and other sandbox READMEs — reference implementations of sandbox-side workloads.
- `knowledge-repos/openshell-community/brev/welcome-ui/SERVER_ARCHITECTURE.md` — not yet read in this session but may contain proxy internals.
- `knowledge-repos/openshell-docs/` — official docs mirror with `policyschemareference.md`, `configureinferencerouting.md`, `gateways.md`, `providersandcreds.md`. Release notes file is just a stub pointing at GitHub.
- **Git bisect on suspect commits** — commit `b486c74 feat: openshell debug session` was the latest to touch BOTH `docker/sandbox_worker.py` AND `gateway/worker_registry.py` and is the prime suspect. The diff shows changes to `_run_inference` (aiohttp `ClientSession(trust_env=True)`, `ClientTimeout(total=600)`, max_tokens bump) but the TunnelWebSocket class itself appears unchanged. Needs full-diff review. Earlier commits worth eyeing: `47b5472`, `edb61d5`, `899b5e4`, `15e4884`, `1a2eaa2` (the original worker PR sequence).
- **Check if `trust_env=True` is interacting with env vars inside the sandbox** that change routing — e.g. `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` — causing aiohttp to route the WebSocket handshake differently than what the TunnelWebSocket expects.
- **OpenShell docs** at `~/homelab-infra/projects/knowledge-repos/openshell-docs` — may document the expected CONNECT-tunnel ↔ WebSocket upgrade contract. Not consulted yet.
- **Destroy + recreate the sandboxes** via the Logos UI (option 2 from the debug session). Would test whether the bug is per-sandbox-state or global.

**Minimal reproducer** (for next session, paste directly):

```bash
# Assumes gateway is running on :8091 and hermes-hemette sandbox exists
# 1. Patch a debug worker in the sandbox:
SCRIPT='set -e
cp /app/sandbox_worker.py /tmp/sw.py
sed -i "/logger.info(.Connected to gateway via CONNECT tunnel.)/a\\            logger.info(\"DEBUG-pre: about to send register\")" /tmp/sw.py
sed -i "/heartbeat_task = asyncio.create_task/i\\            logger.info(\"DEBUG-post: send returned\")\n            try:\n                header = await asyncio.wait_for(ws._reader.read(2), timeout=5.0)\n                logger.info(\"DEBUG-raw: got %d bytes: %r\", len(header), header)\n            except asyncio.TimeoutError:\n                logger.info(\"DEBUG-raw: NO bytes from gateway after 5s\")" /tmp/sw.py
mkdir -p /tmp/hermes
printf "%s" "{\"gateway_url\":\"http://host.openshell.internal:8091\",\"worker_id\":\"hermes-hemette\",\"soul\":\"general\",\"toolsets\":[],\"instance_name\":\"hermes-hemette\"}" > /tmp/hermes/instance-config.json
pkill -9 -f sw.py 2>/dev/null || true
nohup python3 /tmp/sw.py > /tmp/sw.log 2>&1 &
sleep 6
cat /tmp/sw.log'
B64=$(printf '%s' "$SCRIPT" | base64 -w0)
openshell sandbox exec --no-tty --name hermes-hemette -- sh -c "echo $B64 | base64 -d | sh"

# Expected on a broken (current) system:
#   ...Connected to gateway via CONNECT tunnel
#   ...DEBUG-pre: about to send register
#   ...DEBUG-post: send returned
#   ...DEBUG-raw: NO bytes from gateway after 5s
#
# Expected on a working system:
#   ...DEBUG-raw: got 2 bytes: b'\x81\x2b'   (or similar — first 2 bytes of the registered reply frame)
```

**Known confounder**: The `logos gateway run` CLI's stdout is dominated by ANSI spinner escape sequences that mask `logger.info` output. I could not confirm whether `logger.info("Worker registered: %s")` at `gateway/worker_registry.py:130` fires for tunneled workers because the log is unreadable. **This is the root cause of why debugging stalled** — blocked on observability, not logic. Fix M6 in MISSING.md first.

**Related**: `feedback_check_venvs_before_launch.md` (the venv confusion distraction during this session). MISSING.md M6 (unified logging — this bug's real blocker). Pass-3 S1 changes in main_app.html are **not related** to this regression — they're HTML/Alpine-only and do not touch the worker WS path. Verified by running the debug script on an unmodified `/app/sandbox_worker.py`.

---

## Pending — feature work

### `get_current_time` MCP tool (later)
For agents that need to actively query time (scheduling, "in 3
hours", relative dates). The prompt-injection in `24e3ad8` covers
the passive "what time is it?" case. The MCP tool is for explicit
lookups (e.g. "remind me in 3 hours" → agent computes target).

### /setup IANA timezone dropdown (future, low priority)
Browser-side `new Date().getHours()` already self-configures the
world view to the user's local tz, and the gateway's
`datetime.now().astimezone()` handles the prompt injection. A
manual tz override in `/setup` is only needed if a user wants to
display the world in a different tz than their browser — punted
unless someone asks for it.

## Pending — infra / cleanup

### #17 Cache sandbox details to prevent blank flash on tab click
When the user clicks on a sandbox in `/admin/sandboxes` the detail
panel sometimes shows empty momentarily before the next poll fills
it in. Cache the last-known values per-sandbox in Alpine state so
the panel never goes blank — refresh in place when fresh data
arrives instead of clearing first then re-populating.

### #19 Model-route switch breaks worker on switched agent — FIXED
Reproduced 2026-04-10 with Tildi, then again with Hermette-copy.
Two compounding stale-state bugs in the OpenShell sub-gateway
provider records:

  1. **Stale URL** — sub-gateways provisioned before commit
     `5390da5` had no `OPENAI_BASE_URL` on the provider config
     (`CONFIG_KEYS=0`). Worker registers, then dies on first
     request through inference.local because the privacy router
     has no upstream URL.
  2. **Stale credential** — even after the URL was healed,
     sub-gateways were holding API keys from prior LM Studio
     key rotations. `ensure_loaded` (which reads
     `auth.db.machines.api_key` directly) kept working, but the
     worker's chat completion call through the privacy router
     used the stored stale credential and got rejected by
     LM Studio (with the misleading "Unexpected endpoint or
     method" 200 response, not a clean 401).

**Fix** (`gateway/openshell_routes.py` + `gateway/admin_handlers.py`
+ `gateway/executors/openshell.py`):

  * `ensure_provider_configured(gateway, provider)` always
    re-pushes both `--credential` and `--config` from the auth.db
    machines table to the target sub-gateway. No detection step;
    cheap (one CLI call per spawn); idempotent.
  * Called pre-spawn from `OpenShellExecutor.spawn` so every
    sandbox lands in a sub-gateway with current credential+URL.
  * Called from `handle_machines_patch` as a background task
    when the user updates `api_key` or `endpoint_url` in the
    Machines admin page — propagates the new value to every
    existing sub-gateway immediately, so the user doesn't have
    to wait until the next spawn.
  * `adopt_primordial` and `finish_provisioning` both refactored
    to use shared `_resolve_lmstudio_provider_args()` helper
    (no more silent `host.docker.internal` fallback — raises if
    no machine row + no env var, since /setup populates the
    machine row in step 1, well before any gateway provision).
  * The resolver previously had a hardcoded
    `http://host.docker.internal:1234/v1` fallback that turned
    "we don't know your URL" into "we configured the wrong URL".
    Removed.

### #21 Reasoning models silently return empty replies — FIXED
Observed 2026-04-10. With Hermette-copy bound to qwen3.5-9b,
the worker would dispatch, qwen3.5 would think for ~60-76s,
and the user would see an empty `{"type":"message","content":""}`
event with `prompt_tokens: 0`. No visible reply, no tokens
generated by the worker side, no error.

**Root cause**: LM Studio's OpenAI-compat endpoint at
`/v1/chat/completions` splits reasoning-model output into TWO
delta fields:

  * `delta.content` — the visible reply (after thinking)
  * `delta.reasoning_content` — the model's thinking phase
    (LM Studio's extension to the OpenAI spec)

The worker at `docker/sandbox_worker.py:382-397` only read
`delta.get("content", "")`. For qwen3.5 it would receive 600+
`reasoning_content` chunks containing the model's full thinking
trace and discard every single one. If qwen3.5 ran out of tokens
before finishing reasoning (common on short prompts because the
chat template keeps it deeply in reasoning mode), `accumulated`
stayed empty and the worker returned `""`.

**Fix** (`docker/sandbox_worker.py`):

  * Worker now reads `delta.reasoning_content` alongside
    `delta.content`. Reasoning chunks are forwarded to the
    gateway as `thinking` events (the gateway already had a
    handler for those at `http_api.py:3138-3142`).
  * If the stream ends with empty `accumulated` content but
    non-empty accumulated reasoning, the worker emits the
    accumulated reasoning as the visible reply. Better a verbose
    answer than silence — and lets the user see qwen3.5's chain
    of thought instead of staring at an empty chat box.

**Verified end-to-end** by direct `/chat` POST to a freshly
restarted Hermette-copy sandbox (qwen3.5 model). 620 thinking
events streamed live, qwen3.5 never emitted any `content`,
fallback fired and surfaced the reasoning as the visible
message. Testing reasoning models is now possible.

Future improvement: a UI affordance (`<details>` collapse?) to
hide the thinking stream by default and let users expand it
for debugging — currently the entire reasoning trace is
displayed inline.


### #20 World map stale "loading" hourglass — likely FIXED as side-effect
Verified 2026-04-11 that `admin_handlers.py:503` sanitizes
`hermes-{name}` correctly and `main_app.html:_worldAgentList()` reads
the right fields. No explicit fix commit, but the name-mismatch
suspicion doesn't match current code — the plumbing is correct.
Probably resolved as a side-effect of the #16 orphan-reaper fix
(stale ghost workers were the likely cause of the name/state
divergence). **Retest before reopening.**

### #18 Standardize openshell gateway naming to model-only — FIXED
Old scheme: bootstrap gateway was `logos-openshell` (the original
out-of-band install), new gateways were `logos-os-<sanitized-model>`.
Inconsistent and the bootstrap name leaked into the M-pill dropdown
("logos-openshell" next to a model id was confusing).

New scheme: every gateway is named after its model with no prefix.

  | Old                              | New                  |
  |---                               |---                   |
  | `logos-openshell`                | `openai-gpt-oss-20b` |
  | `logos-os-qwen-qwen3-5-9b`       | `qwen-qwen3-5-9b`    |

**How** (`gateway/openshell_routes.py`):
  * `_sanitize_route_name(model)` returns the sanitized model id with
    no prefix.
  * `BOOTSTRAP_PRIMORDIAL_NAME` constant (`"logos-openshell"`) is the
    discovery candidate during the very first /setup run; once the
    bootstrap gateway is adopted it gets aliased to its model name and
    `get_primordial_name()` returns the new alias from then on.
  * `_ensure_gateway_alias(name, endpoint)` wraps
    `openshell gateway add --local --name <name> https://127.0.0.1:<port>`
    — this is a client-side rename that registers a new entry in
    `~/.config/openshell/gateways/<name>/` pointing at the same
    physical container. The old name remains valid in openshell's
    metadata so existing state-file entries keep working during the
    transition.
  * `adopt_primordial(provider, model)` registers the alias before the
    `provider create` and `inference set` calls, so the bootstrap
    gateway is referred to by its model name from the very first
    moment Logos talks to it.
  * `migrate_routes_to_model_names()` is the idempotent backfill for
    existing installs — runs at gateway startup
    (`gateway/run.py:start_gateway`), checks every model_routes row,
    aliases + renames anything still on the old scheme. Safe to run
    repeatedly.
  * `auth_db.rename_model_route_openshell_name(route_id, new_name)` is
    the only sanctioned way to mutate `openshell_name` after creation;
    `update_model_route` still treats it as immutable.

All call sites that previously imported `PRIMORDIAL_NAME` as a
hardcoded constant (`http_api.py`, `executors/openshell.py`) now read
the current name through `get_primordial_name()` so the rest of the
codebase tracks the rename without further changes. The old constant
is kept as a back-compat alias pointing at `BOOTSTRAP_PRIMORDIAL_NAME`
for any callers we may have missed.

Migration runs automatically on the next `start_gateway()` — no
manual DB surgery required.

## Documentation / known limitations

### Reasoning toggle on LM Studio is detection-only
We can detect which models support reasoning toggle by reading
`capabilities.reasoning.allowed_options` from
`/api/v1/models`. **But empirically tested** (2026-04-10), none of
the candidate parameter names work for actually disabling reasoning
on qwen3.5-9b through LM Studio's OpenAI-compat endpoint:

| Param | Result |
|---|---|
| `reasoning: "off"` | no change (290 reasoning tokens) |
| `reasoning_effort: "low"` | hit max_tokens during reasoning |
| `enable_thinking: false` | minor decrease (213 tokens), not zero |
| `thinking: false` | no change |
| `chat_template_kwargs: {enable_thinking: false}` | hit max_tokens |
| `/no_think` suffix in user msg | hit max_tokens |

Workarounds for users who want a snappier qwen3.5 chat experience:
modify the chat template in LM Studio's UI manually, use a model
that doesn't have built-in reasoning, or wait for LM Studio to
expose a reasoning param in their compat endpoint.

The /setup benchmark could add a "trivial answer TTFT" metric to
surface this kind of model-specific UX gap upfront.

### #23 Save openshell-router timeout patch as personal fork — DONE
The 60s → 600s patch in `crates/openshell-router/src/lib.rs` now
lives at `github.com/GregsGreyCode/OpenShell` on branch
`local/router-streaming-timeout` (commit `06f71de`), annotated with
tag `streaming-timeout-fix-v1` which also records the matching
`openshell-sandbox` binary md5 (`b7e682634c95bf62d210fd4269c639f3`)
for build reproducibility.

Local clone at `~/homelab-infra/projects/knowledge-repos/openshell`
has `fork` remote pointing at the fork via SSH; the branch tracks
`fork/local/router-streaming-timeout` so plain `git push`/`git pull`
work.

**Still open**:
  * Consider opening an upstream PR — the change is small, but
    hardcoding 600s is probably not PR-friendly. A configurable
    `OPENSHELL_ROUTER_TIMEOUT_SECS` env var would be the mergeable
    version.
  * **k8s persistence gap (unrelated to the fork)**: `imagePullPolicy`
    on the `openshell` StatefulSet has been patched from `Always` to
    `Never` in both clusters so pod restarts use the local patched
    image instead of pulling upstream from ghcr.io. If gateway
    containers are recreated from scratch
    (`openshell gateway destroy && start`), the StatefulSet manifest
    gets re-applied with `imagePullPolicy: Always` and the next pull
    resets the image. The patched `openshell-sandbox` binary on the
    cluster node filesystem (`/opt/openshell/bin/openshell-sandbox`)
    has the same problem — it's only mounted via hostPath, not baked
    into any image. Options: bake a derived cluster image from the
    fork, or redo `docker cp` on every gateway recreate.

### #22 OpenShell 60s hard cap on inference.local requests — FIXED
Empirically confirmed 2026-04-10 with two parallel aiohttp streaming
requests from inside a sandbox to ``https://inference.local/v1/chat/completions``
using a long prompt + ``max_tokens=16384`` + ``ClientTimeout(total=300)``.
Both requests finished at **EXACTLY 60.01s**, same instant, with
``max_gap`` between chunks of only ~10s. Streams were alive the whole
time — this is a TOTAL request timeout, not an idle timeout.

**Root cause**: hardcoded ``Duration::from_secs(60)`` in
``crates/openshell-router/src/lib.rs::Router::new()``. The reqwest
client's total timeout governs the entire upstream request including
streaming body reads, so any single inference call that needed >60s
wall-clock got truncated mid-stream regardless of whether bytes were
still flowing.

**Two false starts**:
  1. First we patched the binary that runs in the **gateway pod**
     (``/usr/local/bin/openshell-server``). No effect — that binary
     doesn't actually execute the proxy code path. Validation test
     still capped at 60.01s with the patched md5 in place.
  2. The actual inference proxy lives in the **sandbox supervisor**
     (``/opt/openshell/bin/openshell-sandbox``), which is the PID 1
     process inside every agent container. The supervisor crate
     (``openshell-sandbox``) is what calls
     ``router.proxy_with_candidates_streaming(...)`` from
     ``crates/openshell-sandbox/src/proxy.rs:998``. The gateway
     pod's openshell-server doesn't even sit on the inference path
     for this request type.

**Fix**:
  * Patched ``crates/openshell-router/src/lib.rs:48`` from
    ``Duration::from_secs(60)`` → ``Duration::from_secs(600)`` on
    branch ``local/router-streaming-timeout`` of the openshell
    knowledge-repos clone.
  * Built ``deploy/docker/Dockerfile.images`` ``--target supervisor-builder``
    to produce a patched ``openshell-sandbox`` binary
    (md5 ``b7e682634c95bf62d210fd4269c639f3``, vs unpatched
    ``9c972341e3d8b3ba726619f0fca80995``).
  * The supervisor binary lives on the cluster node filesystem at
    ``/opt/openshell/bin/openshell-sandbox`` and is mounted into every
    sandbox pod via a read-only hostPath. So deployment is just
    ``docker cp`` into the cluster container at that path; existing
    pods are unaffected until they bounce, new pods pick it up
    automatically.

**Verified end-to-end** 2026-04-11 by running the same parallel test
that originally exposed the cap:
  * Hermette (gpt-oss-20b in primordial gateway) and Atlas
    (qwen3.5-9b in qwen sub-gateway) firing simultaneously, each from
    its own sandbox so the requests hit two different sub-gateways
    and two different LM Studio models.
  * Both ran for the full **300.01s / 300.32s** of the test's
    ``ClientTimeout(total=300)`` without truncation. The OpenShell
    cap is gone — the only stop now is the test's own client-side
    timeout.

**Compare-tab parallel toggle**: still relevant, since long parallel
inference is GPU-bound and slow, but it's no longer load-bearing for
correctness — sequential mode is now a performance choice, not a
truncation workaround.

**Persistence concern**: see #23 — the patched supervisor binary is
only on the cluster node filesystem, not baked into any image or
fork. If either cluster container is destroyed and recreated, the
hostPath gets re-populated from ``ghcr.io/nvidia/openshell/cluster:0.0.23``'s
unpatched copy and the cap comes back. The fix is to either save
this as a personal fork + bake a derived cluster image, or to redo
the docker cp on every gateway recreate.

### Worker WebSocket frame parser blocks during inference
The sandbox worker uses a custom `TunnelWebSocket` whose frame
parser only runs while the main loop is in `receive_json()`. While
`_handle_task` is awaiting an inference call to LM Studio,
incoming WS pings can't be answered. With `heartbeat=30` (the
default), connections dropped after ~30s of inference. We bumped
to `heartbeat=600` as a safety net but the proper fix is to
process WebSocket frames in a separate task from the message
handler. That's a bigger refactor — left for later.

## Worker WS disconnect — diagnosis 2026-04-10

**TL;DR**: not the bug I thought it was. Most "disconnects" I had been
investigating were caused by my own gateway restarts (now logged in
the no-restart-during-chat memory feedback). The single legitimate
mid-inference disconnect from the user's report (10:00:35 yesterday)
was a one-off race I couldn't reproduce in 10+ controlled stress
tests including:

  * 5 parallel chats
  * 5 sequential chats
  * Cold-load model (qwen3.5 unloaded then chat)
  * Sandbox deletion immediately followed by chat
  * Worker SIGKILL immediately followed by chat
  * Mid-stream worker SIGKILL

**What I did find** is a real race window: when a worker WebSocket
dies AFTER `dispatch_task` has called `send_json` but BEFORE the
worker can respond, the gateway's pending `result_future` would
hang for the full 300s timeout because nothing rejected it. The
user-visible symptom is a chat that streams nothing for ~5 minutes
then errors out vaguely.

**Fix**: in `worker_registry.handle_ws`'s `finally` block, when a
worker is removed from `_workers`, find its `current_task_id` and
reject the corresponding pending future with a `ConnectionError`.
The dispatch unwinds immediately. Plus: `_handle_chat`'s error
classifier now recognises `ConnectionError`/`"disconnected before"`
and emits a specific `sandbox_disconnected` SSE error type with a
clear "Agent connection lost mid-reply" message instead of the
generic "Something went wrong" fallback.

**The fast-path check at line 3126 of `_handle_chat` already
handles the case where the worker is gone BEFORE the dispatch
starts** — it returns `sandbox_unavailable` immediately. The
`finally`-block fix is the safety net for the dispatch-then-die
race.

**Heartbeat=600 from the earlier round still applies** as belt-and-
braces against actual heartbeat-related drops, but it's not
load-bearing for the disconnect scenarios I tested.

## Recent fixes (just in case we crash and need context)

| Commit | Fix |
|---|---|
| `4e6b079` | LM Studio `/api/v1/models` field names corrected (`models[*].key`, `loaded_instances`) — was the cause of "every chat reloads the model" |
| `7732a8e` | WS heartbeat 30 → 600 |
| `68a4988` | Auto-select first agent on /chats land |
| `11f2ae9` | Local DM session_key includes chat_id (was `agent:main:local:dm` for everyone, causing cross-agent transcript bleed) |
| `5eaac73` | On-demand LM Studio `ensure_loaded` from `_handle_chat` |
| `4c4a09f` | Use `lm-studio` placeholder token instead of `unused` (initial fix, since superseded by reading user's machine.api_key) |
| `47b5472` | Reject pending futures when worker WS dies mid-dispatch (the 5-min stall race documented above) |
| `24e3ad8` | Real local-time day/night cycle in WorldScene + `Current time` line injected into session context prompt |
| `b486c74` | #13 side-by-side AB compare panel (drag pills → dual chat panes, seq/parallel toggle) + #16 orphan openshell ssh-proxy reaper on gateway startup |
| `06f71de` | #23 openshell-router 60s→600s timeout patch saved to fork `GregsGreyCode/OpenShell` on branch `local/router-streaming-timeout`, tag `streaming-timeout-fix-v1` |

## What's currently a "dead end" we're aware of

- Reasoning toggle on LM Studio (see above)
- The openshell `sandbox logs` subcommand (doesn't exist; we now
  read `/tmp/worker.log` via `sandbox exec` instead — fixed)
- Single-LM-Studio-instance VRAM ceiling (user can load 2 qwen + 2
  gpt-oss-20b max — that's an LM Studio config the user controls)
