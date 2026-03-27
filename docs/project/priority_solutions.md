# Priority Issues — Solutions & Long-Term Direction

> Companion to `priority_issues.md`. Each solution is evaluated for correctness, user impact, and whether an unsafe deployment mode should be gated until it's fixed.

---

## Issue 1: DockerSandboxExecutor has zero test coverage

### The problem in context

The DockerSandboxExecutor is the **only container isolation path on Windows**. It was implemented in v0.5.80 and shipped without tests. The local process executor and OpenShell executor both have test suites. The k8s executor has integration tests. The Docker executor has nothing.

Specific risks:
- Port allocation race (two concurrent spawns could bind the same port)
- State file (`docker_instances.json`) has no file locking
- `container_id` parsing assumes `docker run` stdout format
- No test for spawn-health-check-delete lifecycle
- No test for what happens when Docker daemon is unreachable

### Solution: Test suite + gate untested modes in setup UI

**Phase 1 — Unit tests (do now):**

Write tests mirroring `tests/unit/test_executors.py` (which tests LocalProcessExecutor):
- Mock `subprocess.run` for `docker run`, `docker inspect`, `docker stop`
- Test port allocation with occupied ports
- Test state file load/save/prune lifecycle
- Test error handling when Docker CLI is missing
- Test concurrent spawn safety

**Phase 2 — Integration tests (do next):**

Mark as `@pytest.mark.integration` (requires Docker daemon):
- Spawn a real container, health-check it, delete it
- Verify container is actually removed after delete
- Verify no host filesystem is mounted
- Verify env vars are passed correctly

**Phase 3 — Gate in setup UI:**

Until Phase 1 is complete, the setup wizard should label Docker sandbox as **"Container sandbox (experimental)"** with a note: "This isolation mode is new and under active testing. For production use, prefer Kubernetes."

### Decision framework

| Question | Answer |
|----------|--------|
| Should we grey out Docker sandbox until tested? | **No** — it's still safer than local process. But label it "experimental". |
| Should we block shipping until tests exist? | **Yes for v0.6** — tests should be a release gate for the next minor version. |
| Is the executor architecturally sound? | Yes. It follows the same Protocol as the other executors. The implementation is straightforward `docker run` — the risk is in edge cases, not design. |

---

## Issue 2: Dangerous command patterns are bypassable

### The problem in context

The pattern matcher in `tools/approval.py` is **one layer in a defense-in-depth stack**. The other layers are:

1. **Workspace scoping** (`tools/workspace.py`) — prevents file access outside the workspace directory. Symlink-safe. Well-tested.
2. **Toolset enforcement** — agents can only call tools in their enabled toolset. Validated at two layers (agent + registry).
3. **API key filtering** — terminal subprocesses don't receive provider secrets (`tools/environments/local.py:29-63`).
4. **Container isolation** — in Docker/OpenShell/k8s modes, the host filesystem isn't accessible at all.
5. **Policy levels** — `WORKSPACE_ONLY`, `READ_ONLY`, etc. restrict what the agent can do.

The pattern matcher (layer 6) catches obvious destructive commands, but it's bypassable with interpreter one-liners. This matters most in **local process mode** where layers 1-5 are the real protection and the pattern matcher gives users a false sense of additional security.

### Three possible approaches

**Option A: Improve the patterns (diminishing returns)**

Add more patterns:
```python
(r'\bimport\s+(shutil|os|subprocess)', "Python filesystem/process import"),
(r'require\s*\(\s*["\']fs["\']\s*\)', "Node.js fs module"),
```

Problem: This is an arms race. Every new pattern has a bypass. The attacker is an LLM that can be prompted to use any language or encoding. Pattern matching against arbitrary code execution is fundamentally unsolvable.

**Option B: Sandbox everything — make local process mode the exception, not the default**

Instead of trying to make local process mode safe with patterns, make the default always be a sandbox:
- Docker sandbox on Windows/macOS (already implemented)
- OpenShell sandbox on Linux (already implemented)
- K8s sandbox for server deployments (already implemented)
- Local process mode only available via explicit opt-in with a clear warning

The pattern matcher stays as a **convenience layer** (catches accidental `rm -rf` from a confused model) but is not presented as a security boundary.

**Option C: Honest labelling + policy enforcement (recommended)**

Keep the pattern matcher but change how it's presented:

1. **Rename in UI**: "Command review" not "dangerous command protection" — it reviews obvious destructive commands, not all possible harmful actions.

2. **Gate local process mode** in the setup wizard: when no sandbox is available, show an explicit warning:
   > "Local process mode provides no OS-level isolation. The agent runs with your user's full permissions. Command review catches common destructive patterns but cannot prevent all harmful actions. For untrusted inputs, use a sandboxed mode."

3. **Add Tirith integration as the real command-level defense** — Tirith does semantic analysis of commands, not regex matching. When Tirith is available (Linux/macOS), it becomes the real command security layer. The pattern matcher is the fallback.

### Decision framework

| Question | Answer |
|----------|--------|
| Should we grey out local process mode? | **No** — but label it clearly as "No isolation" and show the warning. Some users genuinely only want local process mode (personal use, trusted inputs). |
| Should we invest in more patterns? | **No** — diminishing returns. The real investment should be in making sandbox modes easier to set up. |
| Should we remove the pattern matcher? | **No** — it catches accidental damage from confused models. It's valuable as a convenience layer, just not as a security boundary. |
| What's the long-term answer? | Sandbox by default. Local process is the fallback. Tirith for command-level analysis when available. Patterns for obvious cases. |

---

## Issue 3: No NetworkPolicy for agent pods in `hermes` namespace

### The problem in context

The `logos` namespace has NetworkPolicies (`k8s/16-network-policy.yaml`):
- `logos-gateway`: ingress on 8080, egress to DNS/HTTPS/inference ports
- `logos-ai-router`: ingress from gateway only, egress to DNS/HTTPS/inference

But the `hermes` namespace (where agent instance pods run) has **zero NetworkPolicies**. Agent pods have unrestricted network access.

This matters because:
- Agent pods execute arbitrary tool calls including terminal commands
- A prompt injection could instruct the agent to scan the cluster network
- Agent pods could reach internal services (databases, monitoring, secret stores)
- Agent pods could exfiltrate data to any external endpoint

### The correct NetworkPolicy for `hermes`

Agent pods need:
- **DNS** (UDP/TCP 53) — required for any outbound resolution
- **HTTPS** (443) — model APIs, web tools, external services
- **HTTP to logos gateway** (8080) — for MCP proxy, approvals, status callbacks
- **HTTP to inference servers** (1234, 11434, 8000) — local model endpoints
- **Nothing else** — no access to cluster internal services, no access to the Kubernetes API, no arbitrary port scanning

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: hermes-agent-pods
  namespace: hermes
spec:
  podSelector: {}  # applies to ALL pods in hermes namespace
  policyTypes:
    - Ingress
    - Egress

  ingress:
    # Allow traffic from logos gateway (for health checks, chat proxy)
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: logos
          podSelector:
            matchLabels:
              app: logos
      ports:
        - protocol: TCP
          port: 8080

  egress:
    # DNS
    - ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }

    # HTTPS to external APIs
    - ports:
        - { protocol: TCP, port: 443 }

    # HTTP to logos gateway (MCP proxy, approvals)
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: logos
          podSelector:
            matchLabels:
              app: logos
      ports:
        - { protocol: TCP, port: 8080 }

    # Local inference ports (common defaults)
    - ports:
        - { protocol: TCP, port: 80 }
        - { protocol: TCP, port: 1234 }
        - { protocol: TCP, port: 11434 }
        - { protocol: TCP, port: 8000 }
```

### What this blocks

- Access to the Kubernetes API server (typically 6443)
- Access to other namespaces' services
- Access to node metadata endpoints (169.254.169.254)
- Arbitrary port scanning within the cluster
- Direct pod-to-pod communication within `hermes` namespace (agents can't talk to each other)

### What this allows

- Outbound HTTPS (required for cloud model APIs and web tools)
- Outbound HTTP to inference server ports (required for local models)
- Communication back to the logos gateway (required for MCP, approvals, health checks)
- DNS resolution (required for everything)

### Decision framework

| Question | Answer |
|----------|--------|
| Should we ship this now? | **Yes** — this is a clear security improvement with no functional downside. |
| Should we grey out k8s mode without NetworkPolicy? | **No** — k8s without NetworkPolicy is still better than local process mode (filesystem isolation, resource limits, pod boundary). But we should apply the policy by default. |
| Should the NetworkPolicy be optional? | It should be **applied by default** in `kubectl apply -f k8s/`. Users can remove it if they need custom networking. |
| What about user-configured inference on non-standard ports? | Document how to add custom egress rules. The default policy covers the common ports (1234, 11434, 8000, 80, 443). |
| Does this require a CNI that supports NetworkPolicy? | Yes. k3s with Flannel supports it. Most production CNIs (Calico, Cilium, Weave) support it. Document this requirement. |

---

## Summary: What to do now vs later

### Do now (this release cycle)

1. **Issue 3**: Write and apply the `hermes` namespace NetworkPolicy. No functional impact, pure security improvement.
2. **Issue 2**: Update setup UI copy for local process mode — honest labelling, clear warning.
3. **Issue 1**: Write unit tests for DockerSandboxExecutor. Label as "experimental" in UI until integration tests pass.

### Do for v0.6

1. **Issue 1**: Integration test suite for Docker executor (requires Docker in CI).
2. **Issue 2**: Make sandbox the default. Local process requires explicit opt-in.
3. **Issue 3**: Document NetworkPolicy requirements in k8s README.

### Do later (v0.7+)

1. **Issue 2**: Tirith integration for semantic command analysis when available cross-platform.
2. **Issue 1**: Remove "experimental" label once Docker executor has full test coverage.
