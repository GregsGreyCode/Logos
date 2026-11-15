# Logos v2 agent image

`logos-agent:<hermes version>` is upstream `nousresearch/hermes-agent`, pinned by digest and
unmodified, plus a thin layer so it runs as an OpenShell 0.1.8 sandbox.

| Item | Value |
| --- | --- |
| Base | `nousresearch/hermes-agent:v0.23.1@sha256:276d4468…` (published 2026-11-10) |
| Size | 4.56 GB on disk (v1's `hermes-sandbox:m12` was 9.9 GB) |
| Workload user | `hermes` (uid/gid 10000) |
| Workspace | `/sandbox`, with the brain volume at `/opt/data/brain` |
| Hermes home | `/opt/data` (its own volume) |
| Bundled extras | upstream's image already includes messaging (Telegram), Anthropic, OTLP, documents |

## Build

```shell
docker build -t logos-agent:0.23.1 infra/agent-image
```

The local gateway's Docker driver uses local images directly, so nothing needs pushing for a
single-host setup. Publishing to GHCR is not set up yet.

## Run as a sandbox

```shell
for v in logos-home-<agent> logos-brain-<agent>; do
  docker volume create \
    --label openshell.ai/sandbox-attachable=true \
    --label openshell.ai/sandbox-attachable-workspace=default "$v"
  docker run --rm -v "$v":/v alpine:3 chown 10000:10000 /v   # fresh volumes are root-owned
done

openshell sandbox create --name <agent> --detach --restart-policy always \
  --from logos-agent:0.23.1 \
  --policy infra/agent-image/policy.yaml \
  --driver-config-json '{"docker":{"mounts":[
      {"type":"volume","source":"logos-home-<agent>","target":"/opt/data","read_only":false},
      {"type":"volume","source":"logos-brain-<agent>","target":"/opt/data/brain","read_only":false}]}}' \
  -- sh -c '/opt/hermes/docker/stage2-hook.sh; exec hermes gateway run'
```

S3.1 wraps this in one command driven by `agents/<agent>.yaml`.

## Why the extra layer

OpenShell replaces the image ENTRYPOINT/CMD with its supervisor, requires a non-root OCI `USER`,
and treats a non-`/` `WORKDIR` as the workspace. Upstream runs as root and drops privileges inside
its s6 entrypoint, with `WORKDIR /opt/hermes`. Running upstream's `stage2-hook.sh` as `hermes`
still seeds `/opt/data`; its root-only steps (UID remap, chown) print warnings and are skipped.

## Policy

`policy.yaml` grants read-only access to the system paths and `/opt/hermes`, and read-write to
`/opt/data`, `/sandbox` and `/tmp`, with Landlock as a hard requirement.

Network is deny-by-default. Only the hermes Python interpreter
(`/opt/hermes/tools/python-*/bin/python3*`; OpenShell matches real paths, not the venv symlink) may
connect, and only to:

| Rule | Destination | Notes |
| --- | --- | --- |
| `lm_studio` | `192.168.1.117:1234` (HTTP) | Private address, so pinned with `allowed_ips` |
| `anthropic` | `api.anthropic.com:443` | Nightly consolidation on Claude |
| `telegram` | `api.telegram.org:443` | The agent's own bot |

`boundary.yaml` is the most any agent may ever be granted. Check a policy against it before use:

```shell
openshell-prover check --boundary infra/agent-image/boundary.yaml infra/agent-image/policy.yaml
# result: within_boundary
```

Denials show up in `openshell logs <sandbox> --since 5m --source sandbox` (don't filter by level).

## Verified (2026-11-15)

- `hermes --version` inside the sandbox: `Hermes Agent v0.23.1 (2026.9.24) · upstream 3b421578`.
- `stage2-hook.sh` as uid 10000 exits 0 and seeds `/opt/data`; expected warnings: XDG runtime
  chown, config backend detection, dependency refresh (sealed venv, no network).
- `hermes gateway run` starts its API server and stays up until stopped.
- Network (S2.4): from Python in the sandbox, LM Studio `/v1/models` returns 200, Anthropic and the
  Telegram bot API answer 401 (reached, no credentials). example.com, other LAN ports and `curl`
  are denied. The prover reports the policy `within_boundary`, and a policy adding example.com
  `exceeds_boundary`.
