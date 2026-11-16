# Windows Desktop Packaging — Engineering Plan

> Analysis and implementation plan for shipping Logos as a Windows `.exe` desktop app
> while preserving the existing Kubernetes/Docker server deployment path.

**Date:** 2026-03-23
**Status:** Phases 1–3, 5–6 complete. Phase 4 (KubernetesExecutor extraction) outstanding — see OUTSTANDING_FEATURES.md.

---

## 1. Deployment Targets

Two distinct deployment shapes, maintained from the same codebase:

| Target | Artifact | Who uses it |
|---|---|---|
| Server / Homelab | Docker image (`:canary`, `:latest`, `:{sha}`) | k8s cluster, `docker run`, bare-metal |
| Windows Desktop | `LogosSetup.exe` (Inno Setup → PyInstaller bundle) | Local workstation users |

The Docker image path is **unchanged**. The `.exe` adds a second execution backend; it does not replace or compromise the server path.

---

## 2. Is a Local (Non-Kubernetes) Execution Path Viable?

**Yes.** The core agent loop (`agents/hermes/agent.py`) is pure Python with no k8s dependency. The only k8s-coupled code is:

| Location | What it does | Coupling |
|---|---|---|
| `gateway/http_api.py:209-595` | Spawn/list/delete Instances pods | Hard k8s |
| `gateway/http_api.py:55,73` | `_AI_ROUTER_BASE`, `_HERMES_NAMESPACE` constants | Soft — `AI_ROUTER_BASE` now env-configurable; proxy endpoints fail gracefully |
| `gateway/http_api.py:867-868` | Instances tab UI | UI-only |

Everything else (chat, sessions, tools, routing, auth, setup wizard) is k8s-agnostic.

**Cross-platform blockers identified:**

| File | Issue | Fix |
|---|---|---|
| `tools/process_registry.py:269-270` | `/tmp/` paths inside shell cmds + `nohup bash -c` pattern | Windows-local terminal backend will replace this entirely |
| `environments/tool_context.py:195` | `/tmp/_hermes_upload.b64` inside shell cmd string | Same — shell cmd context, not host Python |
| Storage paths | `Path.home() / ".hermes"` — already cross-platform | None needed |
| PTY | Already conditional: `pywinpty` on Windows, `ptyprocess` on Unix | None needed |

---

## 3. Chosen Architecture

### Runtime mode flag

A new `runtime.mode` config key (`"local"` | `"kubernetes"`) selects the execution backend at startup.

- **`kubernetes`** (default for server): existing behaviour unchanged
- **`local`**: agents run as supervised Python subprocesses; no cluster required

### Executor abstraction (`gateway/executors/`)

```
gateway/executors/
├── __init__.py       — factory: build_executor(mode) → InstanceExecutor
├── base.py           — InstanceExecutor Protocol, dataclasses
├── kubernetes.py     — KubernetesExecutor (extracted from http_api.py)
└── local.py          — LocalProcessExecutor (new)
```

`InstanceExecutor` Protocol:
```python
class InstanceExecutor(Protocol):
    def spawn(self, config: InstanceConfig) -> SpawnedInstance: ...
    def list_instances(self) -> list[dict]: ...
    def delete_instance(self, name: str) -> None: ...
    def get_headroom(self) -> ResourceHeadroom: ...
```

`LocalProcessExecutor`:
- Allocates ports from `runtime.local_port_range` (default `[8081, 8199]`)
- Starts each instance as `subprocess.Popen(["python", "-m", "gateway.run", "--port", str(port)])`
- Tracks PIDs + ports in `~/.logos/instances.json`
- Uses `psutil` for resource headroom instead of cluster metrics
- Health-polls `http://127.0.0.1:{port}/health` before marking instance ready

### UI gating

The **Agents tab** (formerly Instances) is visible in **all runtime modes**. In `local` mode it shows psutil-based system resources (free CPU cores + free RAM) instead of cluster totals, and instance cards show per-process CPU% and RAM usage. In `kubernetes` mode it shows cluster-wide CPU/RAM bars with used/total breakdown.

`runtime.mode` flows: `config.yaml` → `run.py` (bridge to `HERMES_RUNTIME_MODE`) → `http_api.py` (reads env, injects into `window.__LOGOS__`) → Alpine (`runtimeMode` data property).

---

## 4. Windows `.exe` Packaging Stack

### Short-term (implemented)

```
PyInstaller
  └── bundles Python interpreter + all packages
      └── hermes_launcher.py  (pystray tray app)
          ├── starts gateway/run.py in subprocess
          ├── opens browser on first run
          └── provides tray icon (Open / Restart / Quit)

Inno Setup
  └── wraps PyInstaller output into LogosSetup.exe
      ├── installs to %LOCALAPPDATA%\Logos
      ├── creates Start Menu entry
      └── optional startup-with-Windows entry
```

### Long-term option

Tauri (Rust shell + webview) — eliminates the "embed Python" overhead, produces a ~10MB installer vs ~80MB PyInstaller bundle. Viable once the web UI is stable; not a blocker.

---

## 5. Key Design Decisions

| Decision | Chosen | Rationale |
|---|---|---|
| Packaging tool | PyInstaller | No extra toolchain; ships today |
| Installer | Inno Setup | Industry standard, free, widely trusted on Windows |
| Tray app | pystray | Pure Python, no C# dependency |
| K8s coupling scope | Instances tab only | Proven by audit; rest of app is clean |
| Config flag | `runtime.mode: local\|kubernetes` | Consistent with existing `terminal.backend` pattern |
| Executor protocol | Protocol (structural subtyping) | Avoids ABC inheritance; matches existing style |

---

## 6. Implementation Phases

### Phase 1 — Runtime mode config (done)
- [x] Add `runtime: {mode: "local", local_port_range: [8081, 8199]}` to `DEFAULT_CONFIG`
- [x] Bump `_config_version` to 8
- [x] Bridge `runtime.mode` → `HERMES_RUNTIME_MODE` env var in `gateway/run.py`

### Phase 2 — UI gating + executor skeleton (done)
- [x] Inject `runtimeMode` into `window.__LOGOS__` in `_handle_index`
- [x] Add `runtimeMode` Alpine data property
- [x] Create `gateway/executors/` package (base, kubernetes stub, local skeleton)
- [x] Agents tab visible in all runtime modes (renamed from Instances; k8s gate removed)

### Phase 3 — LocalProcessExecutor (done)
- [x] Implement port allocation from pool
- [x] Implement `spawn()` — `subprocess.Popen` + PID tracking
- [x] Implement `list_instances()` — read `~/.logos/instances.json` + health check + per-process psutil CPU/RAM
- [x] Implement `delete_instance()` — SIGTERM + SIGKILL fallback + cleanup
- [x] Implement `get_headroom()` — psutil CPU/RAM with 1-core / 1-GB thresholds
- [x] Implement `get_resources()` — system-wide free CPU + RAM for UI resource bar
- [x] Wire executor into gateway startup via `build_executor(mode)`
- [x] Normalize local instance shape to match k8s shape (single UI template serves both)
- [x] Local instances auto-appear in chat agent selector; Chat → button links to `http://127.0.0.1:{port}`

### Phase 4 — KubernetesExecutor extraction
- [ ] Extract `_spawn_instance()`, `_list_hermes_instances()`, `_cluster_resources()`, `_delete_instance()` from `http_api.py` into `gateway/executors/kubernetes.py`
- [ ] Replace calls in `http_api.py` with `request.app["executor"].method()`

### Phase 5 — Desktop launcher (done)
- [x] `launcher/hermes_launcher.py` — pystray tray app
- [x] `launcher/hermes_launcher.spec` — PyInstaller spec
- [x] `installer/logos.iss` — Inno Setup script

### Phase 6 — Windows CI (done)
- [x] `.github/workflows/build-windows.yml` — build `.exe` on push to main
- [x] Upload artifact to GitHub Releases with SHA256 hashes + VirusTotal link

---

## 7. Out of Scope

- Making Kubernetes run on Windows desktop (rejected — wrong direction)
- WSL2 as a requirement (acceptable as optional power-user path, not the default)
- Replacing Docker image with Windows service (server users stay on Docker/k8s)
- Code signing (post-MVP; needed for Defender SmartScreen bypass in distribution)

---

## 8. File Map (post-implementation)

```
logos/
├── gateway/
│   ├── executors/
│   │   ├── __init__.py      — build_executor() factory
│   │   ├── base.py          — InstanceExecutor Protocol + dataclasses
│   │   ├── kubernetes.py    — KubernetesExecutor (Phase 4)
│   │   └── local.py         — LocalProcessExecutor
│   └── http_api.py          — runtimeMode gating; k8s fns delegated to executor
├── launcher/
│   ├── hermes_launcher.py   — pystray tray app
│   └── hermes_launcher.spec — PyInstaller spec
└── installer/
    └── logos.iss            — Inno Setup script (Phase 6)
```
