# ADR 0001: Agent brains live on labelled Docker named volumes

- Status: accepted
- Date: 2026-11-15
- Story: S2.1 (epic E2, OpenShell 0.1 foundation)
- Spec: https://claude.ai/artifact/JpSGvsY25zM7VHPdce6v6P

## Context

Each Logos v2 agent (Greg's, Béré's, Household) keeps a markdown "brain" vault that must outlive its
sandbox, survive host reboots, and be reachable from the host for git backups and Obsidian sync.
Agents run in OpenShell 0.1.8 sandboxes on the Docker compute driver.

OpenShell 0.1.8 offers two ways to give a sandbox persistent storage beyond its workspace:

| Option | What it needs | Trade-off |
| --- | --- | --- |
| Docker named volume | `allow_driver_config = true`; the volume carries the admission labels `openshell.ai/sandbox-attachable=true` and `openshell.ai/sandbox-attachable-workspace=<workspace>` | Resource admission stays on; host access goes through a helper container |
| Host bind mount | `allow_driver_config = true`, `enable_bind_mounts = true` and resource admission switched **off** for the driver | Simple host access, but the docs warn it bypasses workspace isolation and filesystem policy |

## Decision

Use one labelled Docker named volume per agent (`logos-brain-<agent>`), mounted read-write at
`/opt/data/brain`. Keep resource admission enabled. Reach the files from the host with a short-lived
helper container (`docker run --rm -v logos-brain-<agent>:/v ...`) for git commits, backups and sync.

## Spike results (2026-11-15, OpenShell 0.1.8, Docker 29.9.3)

| Check | Result |
| --- | --- |
| Labelled volume passes resource admission and mounts read-write | Pass |
| Sandbox can write to a fresh volume | **Fail until fixed**: a new volume is owned by root and the default sandbox user is uid 1000. Fix: chown the volume root to the sandbox uid with a helper container when the volume is created |
| Data survives `sandbox stop` / `sandbox start` | Pass |
| Data survives deleting the sandbox and creating a new one on the same volume | Pass |
| Host helper container can read the files | Pass |
| `--restart-policy always`: container killed with `docker kill` | Back to Ready in about 6 s |
| Gateway restart (`systemctl --user restart openshell-gateway`) | Sandbox unaffected |
| Simulated host reboot: gateway stopped, sandbox containers stopped, gateway started | Back to Ready in about 4 s, data intact |

A real host reboot was not tested. It depends on the gateway user service starting at boot, which
needs systemd lingering for the user (`loginctl enable-linger`, now enabled on the dev host).

## Consequences

- Volume creation is part of agent creation (S3.1): create with both labels, then chown to the
  sandbox image's uid.
- Sandboxes are created with `--restart-policy always`.
- Git commits of the vault (S4.3) and backups (S7.1) run through a helper container, not host paths.
- Obsidian sync (S4.6) needs a sync tool that can run in a container against the volume.
- If Podman replaces Docker later, the same labels apply; Podman volume mounts don't support `subpath`.
