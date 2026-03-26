# Kubernetes Agent Runtime — Architecture Plan

## Current State

Logos today is a **monolithic gateway with optional k8s-backed agent spawning**. Agent instances are created imperatively via `http_api.py:_spawn_instance()` (~250 lines of coupled logic mixing soul resolution, toolset policy, ConfigMap building, and direct k8s API calls). There is no reconciliation loop — if the gateway pod restarts mid-spawn, state is lost. There are no CRDs, no controller, no inter-agent communication layer.

What works well:
- Multi-platform message routing (Telegram, Discord, Slack, etc.)
- Policy-enforced tool access via soul/toolset system
- Isolated single-agent sessions with shared memory injection
- Provider-agnostic model interface

What's missing for a proper runtime control layer:
- CRD-based desired state for agents
- Controller reconciling actual vs. desired state
- Programmatic sub-agent spawning (agents can't spawn agents)
- Inter-agent communication
- Quarantine without full deletion
- Fleet visibility (`kubectl get agents`)

---

## Target Architecture

### Layer 1 — Custom Resource Definitions

Two CRDs define the agent model at the k8s API level:

**`AgentInstance` CRD**
```yaml
apiVersion: logos.ai/v1alpha1
kind: AgentInstance
metadata:
  name: companion-greg
  namespace: logos
spec:
  soul: companion           # references a soul definition
  model: balanced           # LLM_MODEL alias
  owner: greg               # user ID from auth DB
  toolset: standard         # tool tier
  parentRef: ""             # set when spawned by another agent
status:
  phase: Running            # Pending | Running | Quarantined | Failed | Terminating
  podName: companion-greg-7f8b9-xxxxx
  startedAt: "2026-03-26T10:00:00Z"
  conditions:
    - type: Ready
      status: "True"
```

**`AgentWorkflow` CRD** (future — maps to existing workflow_definitions table)
```yaml
apiVersion: logos.ai/v1alpha1
kind: AgentWorkflow
metadata:
  name: research-pipeline
spec:
  steps:
    - name: gather
      agentSoul: researcher
    - name: summarise
      agentSoul: writer
      dependsOn: [gather]
```

### Layer 2 — Controller

A single controller (operator) watches `AgentInstance` objects and reconciles k8s resources to match. Written in Python using `kopf` to stay in the existing Python stack.

**Reconciliation loop:**
```
Watch AgentInstance CRDs
  → on CREATE: build Deployment + Service + PVC + ConfigMap, set status.phase=Pending
  → on UPDATE: detect spec drift, rolling-update the Deployment
  → on DELETE: tear down Deployment + Service (retain PVC for 24h for inspection)
  → on Pod failure: update status.phase=Failed, emit k8s Event
  → on quarantine annotation: scale Deployment to 0, set status.phase=Quarantined
```

The controller owns all k8s resource creation for agents. `http_api.py` becomes a thin writer of `AgentInstance` objects — no direct k8s API calls for agent lifecycle.

**Soul/toolset resolution** happens in the API layer before writing the CRD spec. The controller receives a fully-hydrated spec and is intentionally dumb — it never touches the auth DB.

### Layer 3 — EventHub

A lightweight internal event bus connecting k8s events, API events, and MCP signals.

**Sources:**
- k8s informer on Pod/Deployment status changes (agent health)
- Logos HTTP API (user messages, admin actions)
- MCP server callbacks (tool completion signals)
- Cron ticker (scheduled agent triggers)

**Consumers:**
- Controller (pod failure → CRD status update)
- Notification platform (agent failure → Telegram alert)
- Workflow executor (step completion → trigger next step)

This replaces the current hook registry (`gateway/hooks.py`) with a proper event stream, or extends it.

### Layer 4 — Inter-Agent Communication

Agents today are isolated — no message passing. The target model:

- Each `AgentInstance` gets a stable internal DNS name: `{name}.logos.svc.cluster.local`
- A lightweight internal RPC layer (HTTP + SSE or gRPC) lets agents call each other
- Parent-child tracking via `spec.parentRef` enables spawning trees
- Agents can programmatically create `AgentInstance` objects (subject to policy) — a researcher agent can spawn a sub-agent to run a long search while it continues thinking

### Layer 5 — Quarantine Path

Currently: failed/hung agents are deleted. With the controller:

- Quarantine = scale to 0, preserve PVC, set `status.phase=Quarantined`
- Quarantined agents appear in `kubectl get agentinstances` with their last-known state
- Admin can inspect logs, snapshot memory, then delete or revive
- Automatic quarantine trigger: agent exceeds token budget, tool error rate threshold, or wall-clock timeout

---

## Migration Path

Phased to avoid breaking the running system:

### Phase 1 — Extract spawn logic ✅ COMPLETE (merged 2026-03-26)
`_spawn_instance()` extracted from `http_api.py` into `gateway/executors/kubernetes.py`. Soul registry extracted to `gateway/souls.py`. Both spawn paths (manual + queue retry) now use `executor.spawn(InstanceConfig)`. `InstanceExecutor` Protocol defined in `gateway/executors/base.py`.

**Files:** `gateway/executors/kubernetes.py`, `gateway/executors/base.py`, `gateway/souls.py`, `gateway/http_api.py`

### Phase 2 — Add AgentInstance CRD (schema only)
Define the CRD YAML and install it in the cluster. No controller yet. Manually apply CRDs alongside current flow to validate schema.

**Files:** `k8s/20-agentinstance-crd.yaml`

### Phase 3 — Write the controller (kopf)
Build the reconciliation loop. Initially it runs alongside the imperative spawn path — both create Deployments, giving us a side-by-side comparison period.

**Files:** `controller/` (new directory), `k8s/21-controller-deployment.yaml`, `k8s/22-controller-rbac.yaml`

### Phase 4 — Switch API layer to write CRDs
`http_api.py` stops calling k8s directly; writes `AgentInstance` objects and lets the controller reconcile. Imperative path removed.

**Files:** `gateway/http_api.py`, `gateway/executors/kubernetes.py`

### Phase 5 — EventHub + inter-agent comms
Build the event bus. Wire Pod status → CRD status. Add agent-to-agent HTTP endpoints. Enable programmatic sub-agent spawning with policy guard.

### Phase 6 — Workflow executor
Wire the existing `workflow_definitions` DB tables to the `AgentWorkflow` CRD and implement the step executor. This is the long-term multi-agent orchestration layer.

---

## Tech Choices

| Component | Choice | Rationale |
|---|---|---|
| Controller framework | `kopf` (Python) | Stays in the existing Python stack; no Go required |
| CRD API group | `logos.ai/v1alpha1` | Alpha while schema stabilises |
| Inter-agent transport | HTTP/SSE | Consistent with existing platform adapter pattern |
| Event bus | In-process asyncio queue initially | Can swap to Redis Streams or NATS later without API change |
| Workflow engine | Custom (Python) | Existing DB schema already defined; no new dependencies |

---

## What This Unlocks

- `kubectl get agentinstances -n logos` — full fleet visibility
- Agents survive gateway pod restarts — controller re-converges
- Quarantine without kill — inspect agent state post-failure
- Programmatic sub-agent spawning — agents can delegate
- Workflow DAGs — multi-step agentic pipelines with human-in-the-loop approvals
- Horizontal scaling — controller can spread agents across nodes based on resource headroom
