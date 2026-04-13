# Agent Images for OpenShell Sandboxes

Any container image running inside an OpenShell sandbox must satisfy two requirements that upstream agent images typically don't include:

## 1. `sandbox` user (uid/gid 10001)

OpenShell runs the sandbox workload as a non-root user named `sandbox`. If the image doesn't include this user, the pod fails at startup with:

```
sandbox user 'sandbox' not found in image; all sandbox images must include
a 'sandbox' user and group
```

Fix:

```dockerfile
RUN groupadd -g 10001 sandbox && \
    useradd -u 10001 -g sandbox -m -s /bin/bash sandbox
```

## 2. `iproute2` package

OpenShell's in-sandbox agent creates a network namespace for traffic isolation via the privacy router (`inference.local`). This requires the `ip` command from `iproute2`. Without it:

```
Network namespace creation failed and proxy mode requires isolation.
Ensure CAP_NET_ADMIN and CAP_SYS_ADMIN are available and iproute2 is
installed. Error: No such file or directory (os error 2)
```

Fix (Debian/Ubuntu-based images):

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*
```

## Wrapper Dockerfile pattern

Rather than forking upstream images, create a thin wrapper:

```dockerfile
FROM <upstream-agent-image>:<tag>

RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 10001 sandbox && \
    useradd -u 10001 -g sandbox -m -s /bin/bash sandbox

# Directories the Logos gateway uploads into at spawn time
RUN mkdir -p /tmp/hermes && chown sandbox:sandbox /tmp/hermes
RUN mkdir -p /app && chown sandbox:sandbox /app
```

See `docker/Dockerfile.hermes-upstream` for the live example used by Hermes agents.

## Image tagging

Do **not** use the `:latest` tag for sandbox images. Kubernetes sets `imagePullPolicy: Always` for `:latest`, which forces a registry pull even when the image exists locally in containerd. Use a pinned version tag (e.g. `hermes-sandbox:m11`) so k8s uses `IfNotPresent` and starts the pod immediately from the local image store.

## Importing images into the OpenShell cluster

OpenShell runs its own containerd inside a Docker container. Images must be imported into this nested containerd:

```bash
# Find the cluster container name
docker ps | grep openshell-cluster

# Import (streams the full image, takes ~1-2 min per GB)
docker save <image>:<tag> | docker exec -i <cluster-container> ctr -n k8s.io images import -
```

After the first import, subsequent sandbox creates from the same tag are near-instant.

**Note:** The gateway's spawn flow now calls `_ensure_image_in_cluster()` automatically before creating a sandbox. Manual imports are only needed for debugging.

## Logos Agent Image Registry

Logos runs a local container registry (`registry:2`) at `localhost:5000` as the durable store for agent images. This solves two problems:

1. **Persistence** — containerd inside OpenShell clusters garbage-collects images when no pods reference them. The registry ensures images survive GC cycles.
2. **Catalog** — the registry serves as the source of truth for which agent images are available. A future UI page will let users browse and install agent images from this registry.

### Registry naming convention

Images are pushed to the registry under the `logos/` namespace:

```
localhost:5000/logos/<agent-type>:<version>
```

Examples:
- `localhost:5000/logos/hermes-agent:m11` — Hermes agent (M11 proof-of-concept)
- `localhost:5000/logos/claude-code:v1` — Claude Code agent (future)
- `localhost:5000/logos/openclaw:v1` — OpenClaw agent (future)

### Publishing an agent image

```bash
# Build the wrapper image
docker build -f docker/Dockerfile.hermes-upstream -t hermes-sandbox:m11 docker/

# Tag for the registry
docker tag hermes-sandbox:m11 localhost:5000/logos/hermes-agent:m11

# Push
docker push localhost:5000/logos/hermes-agent:m11
```

### Image flow at spawn time

```
Registry (localhost:5000)          Host Docker daemon          OpenShell cluster containerd
  logos/hermes-agent:m11  ──pull──> hermes-sandbox:m11  ──import──> docker.io/library/hermes-sandbox:m11
                                      (cache)                         (used by k8s pod)
```

The gateway's `_ensure_image_in_cluster()` handles the import step automatically.
