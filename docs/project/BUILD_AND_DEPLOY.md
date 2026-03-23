# Build, Deploy, and Distribution

> Current state of the Logos development pipeline, observed pain points, and a prioritised improvement roadmap.

---

## Current Process

### 1. Development

All code lives in `github.com/GregsGreyCode/Logos` on the `main` branch. There is no PR workflow — changes are committed and pushed directly to main. Two GitHub Actions fire on every push:

| Workflow | Trigger | What it does |
|---|---|---|
| `tests.yml` | push / PR to main | Runs `pytest tests/` (unit tests only, no integration) |
| `deploy-site.yml` | push to main (website/ or landingpage/ only) | Builds Docusaurus + landing page, deploys to GitHub Pages |

Neither workflow builds or publishes the container image.

### 2. Build

The container is built manually on the developer's local machine:

```bash
docker buildx build --platform linux/amd64 \
  -t ghcr.io/gregsgreycode/logos:canary \
  --push .
```

- Single `Dockerfile` at the repo root
- Targets `linux/amd64` (the homelab node architecture)
- Pushes directly to GitHub Container Registry (`ghcr.io`)
- Takes ~60–90 seconds end-to-end including push

**Layer caching problem:** `COPY . /app/` happens before `uv pip install`, so every code change invalidates the dependency layer. All 156 packages re-download on each build even if `pyproject.toml` hasn't changed. This adds 30–45 seconds that shouldn't be there.

### 3. Deploy

Three deployments exist in the `logos` Kubernetes namespace on the homelab cluster:

| Deployment | NodePort | Image tag | Purpose |
|---|---|---|---|
| `logos` | 30902 | `ghcr.io/gregsgreycode/logos:latest` | Production (stable) |
| `logos-canary` | 30903 | `ghcr.io/gregsgreycode/logos:canary` | Canary / testing new features |
| `logos-setup-test` | 30904 | `ghcr.io/gregsgreycode/logos:canary` | Setup wizard iteration — wipes state on every start |

Deploying the canary (after building and pushing):

```bash
kubectl rollout restart deployment/logos-canary -n logos
kubectl rollout status deployment/logos-canary -n logos --timeout=120s
```

Deploying the setup-test pod (wipes all state automatically via `HERMES_WIPE_ON_START=true`):

```bash
kubectl apply -f k8s/13-logos-setup-test-deployment.yaml -f k8s/14-logos-setup-test-service.yaml
```

Deploying to production requires building the `:latest` tag and applying `k8s/06-deployment.yaml`.

The `logos-setup-test` deployment uses a generic SOUL.md (`15-logos-generic-config.yaml`) with no homelab-specific content, safe for testing with untrusted users.

### 4. Verification

Verification is manual: open the canary URL in a browser and walk through the setup wizard or dashboard. There are no automated smoke tests run post-deploy. If something is broken it is found by the developer clicking around.

---

## Pain Points

### ~~No CI container build~~ (resolved)
`.github/workflows/build-image.yml` now builds and pushes on every `v*` tag. Versioned SHA tags, `:canary`, and `:latest` are all produced automatically.

### Dockerfile layer ordering wastes cache
```dockerfile
COPY . /app/          # ← invalidates everything below on any file change
RUN uv pip install …  # ← re-runs even when pyproject.toml didn't change
```
The fix is two lines: copy `pyproject.toml` first, install, then copy the rest.

### ~~No versioned image tags~~ (resolved)
CI now tags each image with the semver tag (e.g. `:v0.4.11`) in addition to `:canary` and `:latest`. Rollback by SHA tag is possible.

### Namespace naming
K8s resources are in the `logos` namespace. All deployments (`logos`, `logos-canary`, `logos-setup-test`) match the manifest names. Resolved.

### No canary → production promotion workflow
There is no documented or scripted process for saying "canary looks good, promote to production." The developer has to remember the correct commands, the right image tag, and the right deployment name.

### No post-deploy smoke test
After rolling out, there is no automated check that the service is actually responding correctly. `kubectl rollout status` only confirms the pod started — it does not confirm the app is serving traffic or that the setup wizard loads.

### Context loss between sessions
Deploy commands, namespace names, and image registry paths are not written down anywhere in the repo. This forces rediscovery every session, which is what caused the confusion in this conversation.

### No easy way to share a demo
There is no one-command path for someone else to run Logos locally without setting up a Kubernetes cluster. The install script exists but is not the primary tested path.

---

## Improvements (Prioritised)

### Priority 1 — Do these now (low effort, high impact)

**Fix Dockerfile layer ordering**

```dockerfile
# Before COPY . — install dependencies first so they cache
COPY pyproject.toml uv.lock* ./
RUN uv venv venv --python 3.11 && uv pip install -e ".[all]"

# Now copy code — only this layer rebuilds on code changes
COPY . /app/
```

Saves 30–45 seconds per build. One of the highest ROI changes available.

**Document the deploy runbook**

Add a `RUNBOOK.md` (or section in this file) with copy-paste commands for:
- Build canary
- Deploy canary
- Promote canary → production
- Rollback to previous digest
- Check pod status and logs

This prevents the "what was the command again?" problem between sessions.

**Apply the namespace migration**

The manifests say `logos`, the cluster says `hermes`. Apply the updated manifests to migrate the namespace and eliminate the naming split. Until this is done, every deploy requires remembering that the cluster diverges from the repo.

---

### Priority 2 — Add CI container build

Add a GitHub Actions workflow that builds and pushes the image on every push to main:

```yaml
# .github/workflows/build.yml
name: Build and Push

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64
          push: true
          tags: |
            ghcr.io/gregsgreycode/logos:canary
            ghcr.io/gregsgreycode/logos:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

Benefits:
- Every commit to main builds automatically — no local machine required
- Each image is tagged with the git SHA, enabling precise rollbacks: `kubectl set image ... logos:abc1234`
- GitHub Actions cache (`type=gha`) makes subsequent builds much faster
- Build history is in the Actions tab, tied to the commit that produced it

The deploy step (kubectl) is intentionally left manual for now — see Priority 3 for automated promotion.

---

### Priority 3 — Versioned tags and rollback

Once CI builds are in place, update the deploy process to use SHA tags:

```bash
# Deploy a specific commit to canary
IMAGE=ghcr.io/gregsgreycode/logos:$(git rev-parse HEAD)
kubectl set image deployment/logos-canary logos=$IMAGE -n logos

# Rollback: find the previous SHA from git log, then
kubectl set image deployment/logos-canary logos=ghcr.io/gregsgreycode/logos:<previous-sha> -n logos
```

This makes rollback a 30-second operation rather than a manual image registry archaeology exercise.

---

### Priority 4 — Post-deploy smoke test

A simple script that runs after `kubectl rollout status` to confirm the app is actually serving:

```bash
#!/usr/bin/env bash
# scripts/smoke-test.sh <base-url>
BASE=${1:-http://localhost:8080}
set -e

echo "Checking /health..."
curl -sf "$BASE/health" | grep -q '"status":"ok"'

echo "Checking /auth/login page loads..."
curl -sf "$BASE/auth/login" | grep -q "self-hosted AI agent platform"

echo "Smoke test passed."
```

Run this after every canary deploy. If it fails, rollback immediately. Takes 5 seconds and catches the most obvious breakage.

---

### Priority 5 — Local Docker Compose path

For sharing and demos, add a `docker-compose.yml` at the repo root that starts Logos with sensible defaults:

```yaml
services:
  logos:
    image: ghcr.io/gregsgreycode/logos:canary
    ports:
      - "8080:8080"
    environment:
      HERMES_ADMIN_EMAIL: admin@localhost
      HERMES_ADMIN_PASSWORD: changeme
      HERMES_JWT_SECRET: dev-secret-not-for-production
    volumes:
      - logos-data:/home/hermes/.hermes

volumes:
  logos-data:
```

Then the getting-started experience becomes:

```bash
docker compose up
# open http://localhost:8080
```

This is the correct answer to "how do I show someone Logos without giving them kubectl access to my homelab." It also serves as the primary install path for non-homelab users, which COMPARISON.md identifies as a gap versus GoClaw's single-binary story.

---

### Priority 6 — Canary → production promotion workflow

Once namespaces are clean and CI tags are SHA-based, add a `promote.sh` script:

```bash
#!/usr/bin/env bash
# scripts/promote.sh
# Promotes the current canary image to production.

CANARY_IMAGE=$(kubectl get deployment logos-canary -n logos \
  -o jsonpath='{.spec.template.spec.containers[0].image}')

echo "Promoting $CANARY_IMAGE to production..."
kubectl set image deployment/logos logos="$CANARY_IMAGE" -n logos
kubectl rollout status deployment/logos -n logos
echo "Production updated."
```

One command, no copy-pasting, no possibility of promoting the wrong image.

---

## Summary Table

| Improvement | Effort | Impact | Priority |
|---|---|---|---|
| Fix Dockerfile layer ordering | 5 min | Saves 30–45s per build | 1 |
| Write deploy runbook | 30 min | Eliminates command-rediscovery | 1 |
| Apply namespace migration | 15 min | Eliminates cluster/repo divergence | 1 |
| CI container build (GitHub Actions) | 1–2 hours | Fully automated builds, SHA tags | 2 |
| Post-deploy smoke test script | 30 min | Catches obvious breakage in 5s | 3 |
| Docker Compose for local/demo use | 1 hour | Enables sharing without K8s | 4 |
| Canary → production promote script | 30 min | Safe, one-command promotion | 5 |
| Automated canary promotion on green CI | 2–3 hours | Full GitOps pipeline | Future |
