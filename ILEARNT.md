# I Learnt

Lessons discovered while building and debugging Logos.

---

## Kubernetes pod IPs are not the node's LAN IP

**Problem:** The subnet scanner (`handle_setup_scan`) excluded the gateway's own port (8080) from scan results to avoid Logos appearing as a discovered model server. It did this by comparing each scanned IP against `_own_ips()`. On bare-metal/Windows this works because the app runs directly on the machine, so its LAN IP is in `own_ips`. In k8s the pod's cluster IP (e.g. `10.42.x.x`) is what `_own_ips()` returns — completely different from the node's LAN IP (e.g. `192.168.1.x`). So when scanning the node's subnet, port 8080 on the host was never skipped, and any service running there (including Logos via NodePort, or the Windows app) was probed and returned as a duplicate LM Studio entry.

**Fix:** Include `NODE_IP` (injected into k8s pods via the downward API as `status.hostIP`) in the `_own_ips()` set so the exclusion applies to the host node as well as the pod itself.

**File:** `gateway/setup_handlers.py` — `_own_ips()` (v0.5.13)

---

## k8s network policy must explicitly allow inference ports on the logos pod

**Problem:** The logos pod's egress policy allowed ports 80 and 443, and the ai-router pod had ports 1234/11434/8000 open. But machine setup probes (health checks, model discovery) run from the logos pod directly — not via the ai-router. Without those ports on the logos pod's egress, all machine probes silently failed and the setup wizard showed no model servers.

**Fix:** Add egress rules for ports 1234 (LM Studio), 11434 (Ollama), and 8000 (vLLM) to the logos pod's NetworkPolicy.

**File:** `k8s/16-network-policy.yaml` (v0.5.11)

---

## `HERMES_RUNTIME_MODE` must be set in the k8s configmap

**Problem:** `SessionContext` reads `HERMES_RUNTIME_MODE` from the environment to tell the agent whether it's running in Kubernetes or locally. The k8s configmap didn't set it, so it defaulted to `"local"` and the agent's system prompt incorrectly reported "Local (Linux)" instead of "Kubernetes (Linux)".

**Fix:** Add `HERMES_RUNTIME_MODE: "kubernetes"` to `k8s/01-configmap-env.yaml` and wire it into the deployment env.

**File:** `k8s/01-configmap-env.yaml`, `k8s/06-deployment.yaml` (v0.5.12)

---

## Benchmark candidate picker must deduplicate by base model name

**Problem:** When Logos spawns a second agent instance it registers the same model under a suffixed name (e.g. `qwen/qwen3.5-9b:2`). The benchmark candidate picker treated this as a distinct model and included it as a benchmark target, wasting a slot and producing a redundant result.

**Fix:** Strip `:N` instance suffixes from model IDs before bucketing in `_pick_compare_candidates`, keeping only the first occurrence of each base model name.

**File:** `gateway/setup_handlers.py` — `_pick_compare_candidates()` (v0.5.12)
