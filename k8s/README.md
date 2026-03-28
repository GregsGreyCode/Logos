# Logos Kubernetes Deployment

> **Security model** — For a full explanation of isolation boundaries, what agents can/cannot reach in each deployment mode, and how secrets are handled, see the [Security & deployment model](../README.md#-security--deployment-model) section in the main README.

## Namespace architecture

Logos uses two namespaces:

| Namespace | Purpose |
|-----------|---------|
| `logos` | The gateway process — HTTP API, dashboard, auth, routing, MCP service |
| `hermes` | Agent instances — each spawned agent runs as a Deployment or Job here |

This split isolates agent workloads from the gateway. The `logos` service account has RBAC permissions in both namespaces: it manages its own resources in `logos` and creates/deletes agent instances in `hermes`.

### RBAC footprint

`09-rbac.yaml` grants the `logos` ServiceAccount permission to manage Deployments, Services, PVCs, Jobs, and Pods in both the `logos` and `hermes` namespaces. It also grants cluster-wide read access to nodes and pods (for resource metrics). This is intentionally scoped — the service account cannot create resources in other namespaces or read cluster-wide secrets. Review this role before applying to a shared cluster.

---

Kubernetes manifests. Apply in filename order (00 → 11).

## Manifests

| File | Kind | Purpose |
|------|------|---------|
| `00-namespace.yaml` | Namespace | Creates the `logos` and `hermes` namespaces |
| `01-configmap-env.yaml` | ConfigMap `hermes-config` | Hermes env vars (model, URL, log level) |
| `02-secret.yaml` | Secret `hermes-secret` | API keys and internal token (placeholder — real values applied separately) |
| `03-configmap-hermes-config.yaml` | ConfigMap `hermes-config-yaml` | Full Hermes runtime config — MCP servers, SOUL.md, tool tiers |
| `05-pvc.yaml` | PersistentVolumeClaim `hermes-pvc` | Primary instance persistent storage (5Gi, local-path) |
| `05b-pvc-shared-memory.yaml` | PersistentVolumeClaim `hermes-shared-memory-pvc` | Shared memory PVC (1Gi) — primary writes, spawned instances read-only |
| `06-deployment.yaml` | Deployment `hermes` | Hermes pod (stable) — readiness+liveness probes on `/health :8080`, `/work` emptyDir scratch volume, shared memory at `~/.hermes-shared` |
| `07-service.yaml` | Service `hermes` | NodePort — port 80 → **30920** (main HTTP), port 8080 → **30910** (API/dashboard + chat) |
| `08-serviceaccount.yaml` | ServiceAccount `hermes` | Service account used by Hermes pod for K8s API access |
| `09-rbac.yaml` | ClusterRole + ClusterRoleBinding | Grants Hermes permission to manage Deployments, PVCs, ConfigMaps, and Services in the `hermes` namespace (required for spawning instances) |
| `10-hermes-canary-deployment.yaml` | Deployment `hermes-canary` | Canary pod for self-update testing — apply temporarily, delete after promote/rollback |
| `11-hermes-canary-service.yaml` | Service `hermes-canary` | In-cluster service for smoke-testing the canary |
| `12-hermes-canary-admin-secret.yaml` | Secret `hermes-canary-admin` | Canary-only admin credentials (template — fill in password before applying) |

Note: `08-configmap-providers.yaml` is managed by `scripts/apply-providers.sh`, not committed to the repo.

## Apply everything (fresh cluster)

```bash
# 1. Create secrets first (not in repo)
./scripts/create-k8s-secrets.sh

# 2. Apply providers ConfigMap
./scripts/apply-providers.sh

# 3. Apply stable manifests (skip canary — 10/11 are applied on demand)
kubectl apply -f hosts/k8s/k8_files/hermes_deployment/ \
  --ignore-not-found \
  $(ls hosts/k8s/k8_files/hermes_deployment/*.yaml | grep -v 10- | grep -v 11- | xargs -I{} echo -f {})
```

Or just apply all and ignore the canary being empty:
```bash
kubectl apply -f hosts/k8s/k8_files/hermes_deployment/
```

## Common operations

```bash
# Check status
kubectl get pods,svc,deployment -n hermes

# Tail Hermes logs
kubectl logs -n hermes deployment/hermes -f

# Tail logos gateway logs
kubectl logs -n logos deployment/logos -f

# Update routing config (edit providers.yaml, then:)
./scripts/apply-providers.sh
kubectl rollout restart deployment/logos -n logos

# Update Hermes MCP config or SOUL.md (no image rebuild)
kubectl apply -f hosts/k8s/k8_files/hermes_deployment/03-configmap-hermes-config.yaml
kubectl rollout restart deployment/hermes -n hermes

# Force redeploy both
kubectl rollout restart deployment/logos -n logos
kubectl rollout status deployment/logos -n logos
```

## Soul Registry

Hermes can spawn additional agent instances from the dashboard (Instances tab). Each instance is created with a **soul** — a preset that sets the agent's behavioral character and toolset policy.

Souls are defined in `hosts/ai/hermes/hermes-agent/souls/` and baked into the Docker image at build time. Each soul requires two files:

| File | Purpose |
|------|---------|
| `soul.manifest.yaml` | Machine-readable: id, slug, name, description, toolset policy (enforced/default_enabled/optional/forbidden) |
| `soul.md` | Behavioral character injected as the instance's SOUL.md |

Current souls catalog (v1.3):

| Slug | Name | Category |
|------|------|----------|
| `general` | General | general |
| `companion` | Companion | primary — personalised companion, edit soul.md to tailor to your user |
| `homelab-investigator` | Homelab Investigator | infrastructure |
| `homelab-code-fix` | Homelab Code Fix | infrastructure |
| `news-anchor` | News Anchor | research |
| `planning-life` | Planning Life | personal |
| `relationship-counseling` | Relationship Counseling | personal |
| `app-development` | App Development | development |
| `studying` | Studying | education |

To add a new soul: create `souls/{slug}/soul.md` and `souls/{slug}/soul.manifest.yaml`, then rebuild the image.

## Shared Memory

The `hermes-shared-memory-pvc` PVC holds memory that primary instances write and spawned instances read. This gives every spawned agent access to the primary's `MEMORY.md` and `USER.md` at session start as a read-only "SHARED MEMORY" block in their system prompt.

- **Primary instances**: mount PVC read-write at `~/.hermes-shared/`; config has `shared_write_dir` set so every memory save is mirrored there
- **Spawned instances**: mount PVC read-only at same path; config has `shared_memory_dir` set for read-only injection at session start
- **Pod affinity**: spawned instances prefer the same node as the primary (required for local-path RWO PVC sharing)

The shared memory is a frozen snapshot at session start — mid-session changes from the primary aren't visible to running spawned instances until they restart.

## Building Hermes

The build context is `hosts/ai/hermes/` — the Dockerfile expects `hermes-agent/` relative to that directory:

```bash
cd hosts/ai/hermes
docker build -t ghcr.io/your-org/hermes:latest .
docker push ghcr.io/your-org/hermes:latest

# For canary testing:
docker build -t ghcr.io/your-org/hermes:canary .
docker push ghcr.io/your-org/hermes:canary
```

## Self-update / canary workflow

Hermes can improve its own code and deploy a canary alongside itself. The full protocol is in SOUL.md. Summary:

1. Hermes edits code → commits → pushes
2. Builds `ghcr.io/your-org/hermes:canary` from `hosts/ai/hermes`
3. Applies `10-hermes-canary-deployment.yaml` + `11-hermes-canary-service.yaml`
4. Waits for rollout, curls `http://hermes-canary.hermes.svc.cluster.local/health`
5a. Promote: updates image in `06-deployment.yaml` → apply → restart → delete canary
5b. Rollback: `kubectl_delete_deployment_tool(hermes-canary)`

Canary resources are half of stable (250m/512Mi requests, 2000m/2Gi limits).

## Important addresses

| Service | In-cluster DNS | External |
|---------|---------------|----------|
| hermes (HTTP API + dashboard) | `http://hermes.hermes.svc.cluster.local:8080` | `http://YOUR_K8S_NODE_IP:30910` |
| hermes-canary | `http://hermes-canary.hermes.svc.cluster.local` | temporary — apply on demand |

## Secrets

Five secrets required — none committed to the repo. Use `scripts/create-k8s-secrets.sh` to create on a fresh cluster:

| Secret | Key | Source |
|--------|-----|--------|
| `hermes-secret` | `OPENAI_API_KEY`, `HERMES_INTERNAL_TOKEN`, `INSPECTOR_TOKEN` | Placeholder values in `02-secret.yaml`; `INSPECTOR_TOKEN` must match `MCP_CLIENT_TOKEN` in `inspector-mcp/.env` |
| `hermes-telegram` | `TELEGRAM_BOT_TOKEN` | Main Hermes bot — from @BotFather |
| `hermes-canary-telegram` | `TELEGRAM_BOT_TOKEN` | Canary/test bot — separate @BotFather bot |
| `hermes-notifications-telegram` | `TELEGRAM_BOT_TOKEN` | Homelab_Home_Notifications bot |
| `ghcr-creds` | docker registry auth | GitHub PAT with `read:packages` scope |
| `hermes-canary-admin` | `HERMES_ADMIN_EMAIL`, `HERMES_ADMIN_PASSWORD`, `HERMES_ADMIN_NAME` | Canary-only admin login — see below |

### Canary admin credentials

The canary starts with a fresh `auth.db` (no PVC) and seeds its admin user from `hermes-canary-admin`. This is intentionally separate from production so the canary has known, fixed login credentials.

```
Email:    admin@hermes-canary
Password: (stored in the hermes-canary-admin k8s secret)
```

To create/recreate the secret:
```bash
kubectl create secret generic hermes-canary-admin \
  --namespace hermes \
  --from-literal=HERMES_ADMIN_EMAIL=admin@hermes-canary \
  --from-literal=HERMES_ADMIN_PASSWORD=YOUR_CANARY_PASSWORD \
  --from-literal=HERMES_ADMIN_NAME="Canary Admin"
```

To check the current password:
```bash
kubectl get secret hermes-canary-admin -n hermes -o jsonpath='{.data.HERMES_ADMIN_PASSWORD}' | base64 -d
```

**Inspector token flow**: `02-secret.yaml` sets `INSPECTOR_TOKEN` → the `seed-config` initContainer substitutes it into `config.yaml` via `sed` at pod startup → Hermes reads `config.yaml` and sends `Authorization: Bearer <token>` to `inspector-mcp`. Changing the token requires updating both the k8s secret and `inspector-mcp/.env`, then restarting both services.

```bash
./scripts/create-k8s-secrets.sh
```

## Home notifications

Both stable and canary Hermes post proactive notifications to the Homelab Notifications Telegram group (`TELEGRAM_HOME_CHANNEL=-5152225827`). The group contains the main bot, canary bot, and Homelab_Home_Notifications bot.
