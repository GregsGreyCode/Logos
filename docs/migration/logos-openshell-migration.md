# LOGOS × NVIDIA OpenShell — Engineering Migration & Refactorisation Plan

> **Superseded — k3s/external-k8s sandbox paths removed on branch `remove-legacy-deploy`.**
> OpenShell is now the only supported agent-sandbox runtime. The
> `DockerSandboxExecutor`, the k3s auto-install flow, the external-Kubernetes
> kubeconfig path, and the k8s-based MCP deployer have all been deleted.
> See `docs/openshell-integration.md` and `docker/sandbox_worker.py` for the
> shipped architecture; this file remains as the original planning record.

**Version 1.0 | April 2026**
**Author:** Greg (GregsGreyCode)
**Repo:** github.com/GregsGreyCode/Logos

| | |
|---|---|
| **Status** | **HISTORICAL — Migration largely complete.** OpenShell is now the default and only first-class runtime. The Kubernetes pod-per-agent path described in §1 has been deleted entirely (commit `f6f0972`). The reverse-connection sandbox worker model that actually shipped is documented in `docs/openshell-integration.md` and `docker/sandbox_worker.py`; this file is preserved as the original planning record. |
| **Target** | Logos v0.11.x (next minor cycle) |
| **Scope** | Replace custom sandbox + egress policy with OpenShell; integrate Privacy Router as Model Router backend |

---

## 1. Executive Summary

Logos currently implements agent sandboxing through three parallel isolation backends: a bare Docker container mode, an OpenShell CLI integration (Linux/macOS only), and a Kubernetes pod-per-agent model via k3s or external clusters. The custom container sandbox uses `--cap-drop=ALL` and `--security-opt=no-new-privileges` but provides no egress policy enforcement. The OpenShell integration exists but is treated as one option among several, with the gateway doing its own container lifecycle management around it.

This document proposes a deeper integration where NVIDIA OpenShell becomes the canonical sandbox runtime for Logos, replacing the bespoke Docker container management code and unifying the policy model. The key insight is that OpenShell's four-component architecture (Gateway, Sandbox, Policy Engine, Privacy Router) maps directly onto Logos's STAMP model, and adopting it would eliminate significant custom code while gaining kernel-level Landlock isolation, per-binary egress control, and managed inference routing that Logos currently lacks or implements less robustly.

The migration is structured in four phases, each independently shippable, with the goal that no existing deployment mode breaks during the transition. The WinShell project (WIN-020 spike and related tickets) is explicitly descoped in favour of waiting for OpenShell to ship Windows support, which removes a substantial parallel workstream.

---

## 2. Architecture Alignment: STAMP ↔ OpenShell

The following mapping shows how each STAMP dimension either maps to, is augmented by, or remains independent of an OpenShell component.

| STAMP Dimension | Current Logos Implementation | OpenShell Component | Migration Action |
|---|---|---|---|
| **Soul (S)** | SOUL.md file, hot-reloaded per message. Persona definition: tone, reasoning style, behavioural constraints. | No equivalent — OpenShell is runtime-agnostic and does not manage agent personas. | **No change.** Soul remains a Logos-only concern. SOUL.md continues to be mounted into the sandbox filesystem as a read-only path. |
| **Tools (T)** | Toolset registry in gateway, per-session enablement, MCP gateway with category-based approval tiers. | Sandbox policy controls which binaries can reach which network endpoints. The policy engine operates at the connection level, not the tool abstraction level. | **Refactor:** Logos tool-level policy becomes a layer above OpenShell network policy. MCP tool access requests continue through the Logos gateway; outbound connections from MCP servers are governed by OpenShell egress rules. |
| **Agent (A)** | Hermes runtime (primary), adapter interface for additional runtimes. Agent processes run as children of the gateway or in sandbox containers. | Sandbox runtime — the isolated container where the agent executes. OpenShell supports Claude Code, OpenCode, and OpenClaw out of the box. | **Refactor:** Agent processes launch inside OpenShell sandboxes rather than raw Docker containers. The Hermes runtime becomes a community sandbox image. The agent adapter interface gains an OpenShell lifecycle hook. |
| **Model (M)** | Model Router dispatching to Ollama, Anthropic, OpenAI, OpenRouter. Per-user priority profiles, local network scanning, benchmark-driven selection. | Privacy Router — strips sandbox credentials, injects backend credentials, forwards to managed endpoints via `inference.local`. Supports OpenAI-compatible and Anthropic-compatible patterns. | **Integrate:** Model Router delegates `inference.local` traffic to OpenShell's Privacy Router for sandboxed agents. Direct-to-cloud routing continues for non-sandboxed sessions. Local Ollama routing integrates via OpenShell provider configuration. |
| **Policy (P)** | Four workspace isolation levels (FULL_ACCESS through READ_ONLY), command approval regex, Tirith scanning, container `--cap-drop`, per-binary API key filtering. | Declarative YAML policy: `filesystem_policy` (Landlock LSM), `network_policies` (per-binary egress allowlists with optional TLS termination and HTTP method/path rules), process identity (non-root enforcement, seccomp). | **Replace + Extend:** Logos policy levels translate to OpenShell policy YAML presets. Command approval and Tirith remain as application-layer checks above the kernel-level enforcement. The combination is strictly stronger than either alone. |

---

## 3. What OpenShell Gives Us That We Don't Have

### 3.1 Kernel-Level Filesystem Isolation (Landlock LSM)

Logos currently relies on workspace scoping via `realpath` checks in Python — a symlink-safe but application-layer enforcement. A sufficiently capable agent using terminal tools could bypass this by executing code that calls the filesystem directly. OpenShell enforces filesystem restrictions at the kernel level using Landlock, meaning even if the agent executes arbitrary code inside the sandbox, it physically cannot access paths outside the declared `read_only` and `read_write` sets. This is a qualitative security improvement.

### 3.2 Per-Binary Network Egress Control

The current Logos architecture has no egress filtering on the Docker container sandbox mode at all — the README explicitly states this. The k8s mode can apply NetworkPolicy but this is coarse-grained (port/protocol level, not per-binary). OpenShell's `network_policies` map specific binaries to specific endpoints, optionally with TLS termination and HTTP method/path inspection. This means you can allow `node` to reach `registry.npmjs.org:443` while denying `curl` from doing the same — a granularity Logos cannot currently achieve.

### 3.3 Managed Inference Routing with Credential Isolation

Logos currently filters API keys from subprocess environments, but the keys still exist in the gateway process memory and are passed via environment variables in Docker. OpenShell's Privacy Router takes a fundamentally different approach: the sandbox never receives inference credentials at all. Agents call `https://inference.local`, and the Privacy Router (running outside the sandbox) injects the real credentials. This eliminates the entire class of credential leakage through sandbox escape.

### 3.4 Declarative, Version-Controllable Policy

Logos policy is currently split across `config.yaml` (workspace isolation level), Python code (command approval regex, Tirith integration), and Docker flags (`--cap-drop`, `--security-opt`). OpenShell consolidates all sandbox-level policy into a single YAML file that can be version-controlled, diffed, code-reviewed, and audited. This aligns directly with Logos's STAMP philosophy of making every run reproducible and inspectable.

### 3.5 Elimination of the WinShell Project

The WinShell project (WIN-001 through WIN-020) was a planned Windows-native equivalent of OpenShell using WFP, AppContainers, Job Objects, and a Rust policy proxy. This is a substantial engineering effort. OpenShell does not yet support Windows, but NVIDIA has significantly more resources to solve this problem than a solo developer. Deferring to OpenShell's eventual Windows support eliminates the WinShell backlog entirely and lets you focus on the platform layer where Logos adds unique value.

---

## 4. What Logos Keeps (Non-Negotiable)

OpenShell is an agent runtime sandbox. Logos is an agent control plane. The following capabilities are Logos-specific and are not replaced by OpenShell:

| Capability | Rationale |
|---|---|
| **Soul system (SOUL.md)** | Agent persona management has no OpenShell equivalent. This is the S in STAMP. |
| **STAMP recording & observability** | OpenShell does not record structured run traces. Logos's SQLite run log, tool trace, token counting, and replay system remain core differentiators. |
| **MCP Gateway (centralised)** | OpenShell sandboxes can reach MCP servers, but Logos's centralised MCP gateway with category-based approval tiers is a higher-level abstraction that stays. |
| **Multi-frontend ingress** | Telegram, web dashboard, ACP/IDE integration — OpenShell has no user-facing interface. |
| **Local model benchmarking** | The candidate selection, speed benchmark, and capability eval system is Logos-specific. |
| **Evolution system** | Agent self-improvement proposals against your fork — not an OpenShell concern. |
| **Workflow engine** | DAG-based task graphs with approval gates are a platform feature above the sandbox. |
| **Memory system** | Agent-curated persistent memory, FTS5 search, and LLM summarisation. |
| **Command approval (Tirith + regex)** | Application-layer command review. Runs above OpenShell's kernel-level controls for defense in depth. |

---

## 5. Migration Phases

*Each phase is independently shippable. No existing deployment mode breaks during the transition.*

### Phase 1: OpenShell as a First-Class Sandbox Backend

**Goal: Replace the bespoke Docker container sandbox code with OpenShell's sandbox lifecycle management.**

Currently, the gateway manages Docker containers directly for sandbox mode — pulling images, setting cap-drop flags, binding ports, and cleaning up on exit. This code is in the gateway's executor module and duplicates work that OpenShell handles natively.

#### 1.1 Changes

1. **Add OpenShell CLI as a dependency.** Detection logic already exists in the setup wizard; extend it to manage the gateway lifecycle (`openshell gateway start` if not running).
2. **Create a Logos community sandbox image.** This packages the Hermes runtime, mini-swe-agent, tool dependencies, and faster-whisper into an OCI image that OpenShell can provision. Publish to GHCR.
3. **Refactor the executor interface.** The existing `logos/agent/interface.py` gains an `OpenShellExecutor` alongside the current `LocalExecutor`, `DockerExecutor`, and `K8sExecutor`. The `OpenShellExecutor` calls `openshell sandbox create` with the Logos community image and a generated policy YAML.
4. **Mount SOUL.md as a read-only path.** The `filesystem_policy.read_only` list includes the path where SOUL.md is bind-mounted, ensuring the agent can read its persona definition but cannot modify it.
5. **Deprecate (but do not remove) the raw DockerExecutor.** It remains available for users who cannot install OpenShell, but the setup wizard defaults to OpenShell when available.

#### 1.2 Files Affected

```
gateway/executor/   → new openshell_executor.py
gateway/setup/      → wizard step 4 updated
docker/             → new Dockerfile.sandbox (community image)
.github/workflows/  → new build-sandbox-image.yml
```

#### 1.3 Risk: OpenShell Gateway vs Logos Gateway Name Collision

Both Logos and OpenShell have a component called "gateway". The Logos gateway is the always-on HTTP server handling auth, routing, and the web dashboard. The OpenShell gateway is the control-plane API for sandbox lifecycle. These are complementary, not competing: the Logos gateway calls the OpenShell gateway to provision sandboxes. The code and documentation must be explicit about which gateway is being referenced. Suggested naming convention: `logos-gateway` vs `openshell-gateway` in all config and logs.

---

### Phase 2: Policy Translation Layer

**Goal: Map Logos's four policy levels to OpenShell policy YAML presets.**

#### 2.1 Policy Mapping

| Logos Policy Level | `filesystem_policy` | `network_policies` | `process` |
|---|---|---|---|
| **FULL_ACCESS** | `read_write: [/sandbox, /tmp, /home]` · `read_only: [/usr, /lib, /etc, /proc]` | Permissive: all binaries allowed to all common endpoints (PyPI, npm, GitHub, inference providers) | `run_as_user: sandbox` |
| **WORKSPACE_ONLY** | `read_write: [/sandbox, /tmp]` · `read_only: [/usr, /lib, /etc, /proc]` · `include_workdir: true` | Moderate: agent binaries + node + python allowed to configured endpoints. curl/wget restricted. | `run_as_user: sandbox` |
| **REPO_SCOPED** | `read_write: [/sandbox/repo, /tmp]` · `read_only: [/usr, /lib, /etc, /sandbox]` | Strict: only agent binary allowed to `inference.local` + configured MCP gateway endpoint. Package managers denied. | `run_as_user: sandbox` |
| **READ_ONLY** | `read_write: [/tmp]` · `read_only: [/usr, /lib, /etc, /sandbox]` | Minimal: agent binary to `inference.local` only. All other egress denied. | `run_as_user: sandbox` |

#### 2.2 Changes

1. **Create policy presets directory:** `logos/policies/` containing four YAML files (`full_access.yaml`, `workspace_only.yaml`, `repo_scoped.yaml`, `read_only.yaml`).
2. **Implement a policy compiler** that takes a Logos policy level + the user's `config.yaml` (configured providers, MCP servers, tool endpoints) and generates a complete OpenShell policy YAML. This handles dynamic elements like adding network rules for each configured MCP server.
3. **Retain Tirith + regex command approval** as a Logos-internal layer. These run inside the sandbox as part of the Hermes runtime, providing application-level defense above OpenShell's kernel-level enforcement.
4. **Add policy diffing to the dashboard.** When a user changes their policy level, show the concrete OpenShell YAML diff before applying. This makes the STAMP's P dimension fully transparent.

#### 2.3 Key Design Decision: Additive Not Replacing

Logos's command approval system (Tirith scanning, regex patterns) and OpenShell's kernel-level policy are complementary layers, not alternatives. The command approval catches intent ("this looks like `rm -rf /`") while OpenShell enforces access ("this binary cannot write to `/`"). Keeping both provides defense in depth: even if a sophisticated agent bypasses the regex check by using a Python one-liner, Landlock prevents the filesystem operation from succeeding.

---

### Phase 3: Inference Routing Integration

**Goal: Wire Logos's Model Router to use OpenShell's Privacy Router for sandboxed agent sessions.**

#### 3.1 Current State

The Logos Model Router currently handles all inference dispatching: it selects a backend (Ollama, Anthropic, OpenAI, OpenRouter) based on model class, availability, and per-user priority profiles, then makes the API call with credentials from the gateway's environment. For sandboxed agents, this means API keys are either passed as environment variables to the container or the agent makes requests back to the gateway which proxies them.

#### 3.2 Target State

For agents running in OpenShell sandboxes, inference requests go to `https://inference.local` inside the sandbox. The OpenShell Privacy Router intercepts these, strips any sandbox-supplied credentials, and forwards to the backend configured on the OpenShell gateway (provider + model). The Logos gateway configures the OpenShell provider to point at whatever backend the Logos Model Router would have selected for that session.

#### 3.3 Changes

1. When creating a sandbox, the `OpenShellExecutor` configures the OpenShell provider to match the Logos Model Router's selection for that session. This uses `openshell provider set` with the appropriate credentials and endpoint.
2. For local Ollama inference, configure the OpenShell provider to point at the Ollama endpoint. OpenShell supports any OpenAI-compatible endpoint, which Ollama provides.
3. For cloud providers, configure OpenShell with the appropriate Anthropic or OpenAI credentials. This means the sandbox never sees the API keys — a strict improvement over the current environment variable approach.
4. Non-sandboxed sessions (bare metal, local process) continue to use the Logos Model Router directly. This is not a regression — those sessions don't have sandbox isolation anyway.

#### 3.4 Limitation: Single Provider Per Gateway

OpenShell currently configures one provider and one model per gateway — all sandboxes on that gateway share the same `inference.local` backend. Logos's Model Router supports per-user and per-session model selection. The workaround for Phase 3 is: for sessions that need a different model, the Logos gateway reconfigures the OpenShell provider before sandbox creation (this takes effect within ~5 seconds per the OpenShell docs). For true multi-model concurrency, we'd need multiple OpenShell gateways or a feature request upstream. Track this as a known limitation.

---

### Phase 4: MCP Gateway Cross-Sandbox Routing

**Goal: Ensure the centralised MCP gateway works seamlessly with OpenShell sandbox networking.**

#### 4.1 Current State

Logos's MCP servers boot once inside the Logos gateway process and are shared across all agent sessions. Agents request access via `request_mcp_access`, and tool calls are routed over HTTP to `/mcp/{server-name}` on the gateway. The gateway already resolves the right endpoint URL based on execution mode (localhost for bare metal, `host.docker.internal` for Docker, service DNS for k8s).

#### 4.2 Changes

1. **Add a `network_policies` entry for MCP gateway access.** Each sandbox policy YAML must include a rule allowing the agent binary to reach the Logos gateway's MCP port (default 8081) at the appropriate host (`host.docker.internal` for local OpenShell, or the gateway service address for remote).
2. **For OpenShell sandboxes, add a TLS-terminated `rest` endpoint** in the network policy for the MCP gateway, with rules scoped to `POST /mcp/*` only. This ensures the agent can call MCP tools but cannot make arbitrary requests to the Logos gateway's other API endpoints.
3. **The policy compiler** (from Phase 2) dynamically generates these MCP network rules based on which MCP servers are configured and their approval tiers.

---

## 6. Deployment Modes After Migration

| Mode | Isolation | Egress Policy | Inference Routing | Status |
|---|---|---|---|---|
| **Local process** | OS process boundary | None | Logos Model Router (direct) | Unchanged — fallback when no container runtime available |
| **OpenShell (local)** | Docker + Landlock + seccomp + egress proxy | OpenShell YAML policy | `inference.local` via Privacy Router | **NEW — default for Linux/macOS** |
| **OpenShell (remote)** | Remote Docker + full policy stack | OpenShell YAML policy | `inference.local` via Privacy Router | **NEW — for GPU servers** |
| **Docker (legacy)** | Container boundary, no egress | None | Logos Model Router (direct) | Deprecated — available but not default |
| **Docker Compose** | Container boundary | None | Logos Model Router (direct) | Unchanged — for users who want simplicity |
| **k3s / External k8s** | Pod boundary + NetworkPolicy + RBAC | Kubernetes NetworkPolicy | Logos Model Router (direct, or `inference.local` if OpenShell sidecar) | Unchanged for now; OpenShell k8s integration is a future phase |

---

## 7. Code Changes Summary

| Directory / File | Change Type | Description |
|---|---|---|
| `gateway/executor/openshell_executor.py` | New | OpenShell sandbox lifecycle management: create, connect, destroy, policy application |
| `gateway/executor/__init__.py` | Modify | Register `OpenShellExecutor` in the executor factory |
| `gateway/setup/wizard.py` | Modify | Update step 4 to default to OpenShell when CLI is detected |
| `gateway/model_router.py` | Modify | Add `inference.local` delegation path for OpenShell sessions |
| `gateway/mcp_gateway.py` | Modify | Add network policy generation for MCP endpoints |
| `logos/policies/` | New | Policy preset YAML files (4 levels) + policy compiler |
| `logos/policies/compiler.py` | New | Generates complete OpenShell YAML from Logos config + policy level |
| `docker/Dockerfile.sandbox` | New | Community sandbox image: Hermes + tools + mini-swe-agent |
| `.github/workflows/build-sandbox-image.yml` | New | CI: build and push community sandbox image to GHCR |
| `gateway/dashboard/` | Modify | Policy diff viewer in web dashboard |
| `tests/test_openshell_executor.py` | New | Unit tests for OpenShell executor |
| `tests/test_policy_compiler.py` | New | Unit tests for policy YAML generation |
| `e2e/test_openshell_sandbox.py` | New | Integration test: full sandbox lifecycle with policy enforcement |
| `docs/openshell.md` | New | User-facing documentation for OpenShell integration |
| `README.md` | Modify | Update security & deployment model section, isolation modes table |

---

## 8. Risks and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| **OpenShell is early-stage / breaking changes** | High — could require rework | Medium | Pin to a specific OpenShell version. Abstract all CLI calls through the executor module so changes are localised. |
| **No Windows support in OpenShell** | Medium — Windows users stuck on legacy Docker mode | High (confirmed) | Document clearly. Legacy Docker mode remains available. Monitor OpenShell roadmap for Windows support. |
| **Single provider per OpenShell gateway** | Medium — limits multi-model concurrency | High (by design) | Reconfigure provider per session (5s propagation). For parallel multi-model, fall back to Logos Model Router directly. |
| **OpenShell gateway port conflicts with Logos gateway** | Low | Medium | Use non-overlapping default ports. OpenShell gateway on its default; Logos gateway on 8080. Document clearly. |
| **Hermes runtime compatibility in sandbox** | Medium — tools may break in restricted filesystem | Medium | Build and test the community sandbox image in CI. Run the capability eval suite inside the sandbox. |
| **Community sandbox image size** | Low — large image = slow first pull | Medium | Multi-stage Docker build. Separate base image (Python + Node) from tool layer. Use GHCR for fast pulls. |

---

## 9. Estimated Timeline

*Solo developer estimates. Adjust for available hours.*

| Phase | Work Items | Estimated Effort | Dependencies |
|---|---|---|---|
| **Phase 1: Sandbox Backend** | OpenShellExecutor, community image, wizard update, SOUL.md mounting | 2–3 weeks | OpenShell CLI installed and functional on dev machine |
| **Phase 2: Policy Translation** | Policy presets, compiler, dashboard diff viewer | 1–2 weeks | Phase 1 complete |
| **Phase 3: Inference Routing** | Privacy Router integration, provider configuration per session, local Ollama wiring | 1–2 weeks | Phase 1 complete (can parallel with Phase 2) |
| **Phase 4: MCP Cross-Sandbox** | Network policy generation for MCP endpoints, e2e testing | 1 week | Phases 1 + 2 complete |
| **Testing & Documentation** | e2e test suite, README update, docs/openshell.md, STAMP recording validation | 1 week | All phases complete |

**Total estimated: 6–10 weeks, shipping incrementally per phase.**

---

## 10. Explicitly Descoped

| Item | Reason |
|---|---|
| **WinShell project (WIN-001 through WIN-020)** | Defer to OpenShell's eventual Windows support. Solo dev effort is better spent on platform-layer value. |
| **OpenShell Kubernetes integration** | OpenShell is currently Docker-only. The existing k3s/external k8s modes in Logos remain untouched. Revisit when OpenShell adds k8s support. |
| **Multi-gateway OpenShell deployment** | For multi-model concurrency across sandboxes. Requires OpenShell to support multiple providers per gateway, or Logos managing multiple OpenShell gateway instances. Defer until usage patterns clarify. |
| **OpenShell cloud gateway mode** | Logos is self-hosted first. Cloud gateway support is nice-to-have for VPS deployments but not essential for the core migration. |

---

## 11. Success Criteria

- An agent running in an OpenShell sandbox cannot read files outside its declared `filesystem_policy` paths, verified by an e2e test that attempts to read `/etc/shadow` and confirms `EACCES`.
- An agent running in an OpenShell sandbox cannot make outbound HTTP requests to destinations not in its `network_policies`, verified by an e2e test that attempts `curl` to a non-allowed host and confirms connection refused.
- The agent can reach `inference.local` and receive model responses without holding any API keys in its environment.
- The agent can access MCP tools through the Logos gateway, with network policy scoped to `POST /mcp/*` only.
- All four Logos policy levels (`FULL_ACCESS`, `WORKSPACE_ONLY`, `REPO_SCOPED`, `READ_ONLY`) produce valid OpenShell policy YAML that passes `openshell policy validate`.
- STAMP records for OpenShell-sandboxed sessions capture the policy YAML hash, enabling exact reproducibility of the run's security context.
- The local model benchmarking suite passes when the model endpoint is reached via `inference.local` rather than directly.
- Existing Docker Compose, k3s, and bare metal deployment modes continue to work without modification.
- The setup wizard correctly detects OpenShell availability and defaults to it when present, falling back gracefully when absent.

---

*CONFIDENTIAL — April 2026*
