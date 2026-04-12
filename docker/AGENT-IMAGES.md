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
