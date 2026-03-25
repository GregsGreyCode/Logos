# Windows vs Linux/Kubernetes Feature Matrix

> Ground-truth comparison derived from reading source code, not guessing.
> Last updated: v0.5.15

---

## Infrastructure & Execution

| Feature | Linux/k8s | Windows | Notes |
|---|---|---|---|
| Execution backend | ✅ Kubernetes pods | ✅ Local processes | `HERMES_RUNTIME_MODE` controls this |
| Multi-instance scaling | ✅ Full cluster | ✅ Host hardware only | Windows: 1 agent per free core, ≥1 GB RAM |
| Process termination | ✅ SIGTERM / SIGKILL | ✅ `taskkill /F` | |
| Port allocation | ✅ K8s service discovery | ✅ Pool 8081–8199 | |
| Resource limits | ✅ K8s CPU/memory enforced | ⚠️ psutil soft limits | |
| Workspace isolation | ✅ K8s pod volumes | ✅ `~/.hermes/workspaces/` | |
| Instance persistence | ✅ K8s etcd | ✅ `~/.hermes/instances.json` | |
| Health checks | ✅ K8s probes + `/health` | ✅ HTTP `/health` polling | |

---

## Terminal & Code Execution

| Feature | Linux/k8s | Windows | Notes |
|---|---|---|---|
| Terminal backends | ✅ local, docker, modal, ssh, daytona, singularity | ⚠️ local only | Containers via WSL2 only |
| PTY / interactive | ✅ `ptyprocess` | ✅ `pywinpty` | Different APIs, both supported |
| Background `/tmp` processes | ✅ nohup | ❌ Not available | Windows uses detached subprocess |
| Git Bash detection | N/A | ✅ Auto-finds in ProgramFiles | Falls back to `HERMES_GIT_BASH_PATH` |
| Code execution tool | ✅ | ✅ | Windows requires Git Bash for inline execution |
| Process registry (crash recovery) | ✅ | ✅ | Both checkpoint to `~/.hermes/processes.json` |

---

## Voice & Audio

| Feature | Linux/k8s | Windows | Notes |
|---|---|---|---|
| Edge TTS (free) | ✅ | ✅ | Default, no key needed |
| ElevenLabs TTS | ✅ | ✅ | Optional, key required |
| OpenAI TTS | ✅ | ✅ | Key required |
| Whisper STT (local) | ✅ | ✅ | `faster-whisper`, ~150 MB download on first use |
| Groq / OpenAI Whisper | ✅ | ✅ | Cloud fallback |
| Voice capture (mic) | ✅ if PortAudio | ⚠️ Requires PortAudio | Fails gracefully headless/WSL/SSH |
| Discord voice | ✅ | ⚠️ Needs audio hardware | |

---

## Messaging Platforms

| Feature | Linux/k8s | Windows | Notes |
|---|---|---|---|
| Telegram | ✅ | ✅ | Platform-agnostic |
| Discord (text) | ✅ | ✅ | |
| Discord (voice) | ✅ | ⚠️ Needs audio hardware | |
| Slack | ✅ | ✅ | |
| WhatsApp, Signal, Email | ✅ | ✅ | |
| Home Assistant | ✅ | ✅ | |
| **Setup UI for tokens** | ❌ env vars only | ❌ env vars only | Wizard flow not built yet — both platforms |

---

## Tools

| Feature | Linux/k8s | Windows | Notes |
|---|---|---|---|
| File operations | ✅ | ✅ | Cross-platform via pathlib |
| Web scraping (Firecrawl) | ✅ | ✅ | |
| Browser automation (Browserbase) | ✅ | ✅ | Cloud sandbox; minor `/tmp` path diff |
| Code execution | ✅ | ✅ | Windows needs Git Bash |
| MCP (stdio transport) | ✅ | ⚠️ Less robust | HTTP transport preferred on Windows |
| MCP (HTTP transport) | ✅ | ✅ | |
| MCP servers as K8s pods | 🔲 Planned | ❌ N/A | K8s-only future feature |
| Skills platform gating | ✅ | ✅ | `platform: windows/linux/macos` in skill frontmatter |
| Honcho memory integration | ✅ | ✅ | Optional dep |
| Session persistence | ✅ | ✅ | SQLite WAL, cross-platform |
| Evolution / cron runs | ✅ | ✅ | Both use croniter |

---

## Setup & UI

| Feature | Linux/k8s | Windows | Notes |
|---|---|---|---|
| Setup wizard | ✅ | ✅ | Identical HTML/JS on both |
| Model server scan | ✅ | ✅ | Same code — only divergence was the `_own_ips()` k8s fix (v0.5.13) |
| LM Studio auth (API key) | ✅ | ✅ | Same probe logic; `auth_required` status on both |
| Benchmark | ✅ | ✅ | |
| K8s connection test | ✅ `/test-k8s` | ❌ N/A | Returns error in local mode |
| Tray launcher | ❌ | ✅ | pystray + PyInstaller |
| Splash screen | ❌ | ✅ | Separate port 8079 during startup |
| Code signing | ⚠️ Unsigned | ⚠️ SmartScreen warning | Deferred — UK identity verification not available |

---

## Developer Features

| Feature | Linux/k8s | Windows | Notes |
|---|---|---|---|
| Docker image | ✅ Canonical path | ❌ N/A | |
| Kubernetes manifests | ✅ `k8s/` directory | ❌ N/A | |
| ACP adapter (VS Code/Zed/JetBrains) | ✅ | ✅ | Platform-agnostic HTTP |
| `hermes` CLI | ✅ | ✅ | Uses prompt_toolkit |
| PyInstaller build | ✅ Linux CI | ✅ Windows CI | `.github/workflows/build-windows.yml` |
| Local Docker dev | ✅ | ⚠️ Requires WSL2 + Docker Desktop | No compose file yet |

---

## Windows-Specific Implementation Details

| Feature | Implementation | Status |
|---|---|---|
| Process spawn flag | `CREATE_DETACHED_PROCESS` (0x8) | ✅ |
| Force-kill | `taskkill /F /PID` or PowerShell `Get-CimInstance` fallback | ✅ |
| Git Bash detection | Searches ProgramFiles/Git/bin, ProgramFiles(x86), LOCALAPPDATA | ✅ |
| Tray launcher | pystray + PyInstaller bundle | ✅ |
| Installer | Inno Setup (`LogosSetup-{version}.exe`) | ✅ |
| Port isolation | LocalProcessExecutor pool 8081–8199 | ✅ |
| Instance tracking | `~/.hermes/instances.json` | ✅ |
| No `signal.SIGKILL` | Falls back to taskkill for force-kill | ✅ |

---

## Kubernetes-Specific Implementation Details

| Feature | Implementation | Status |
|---|---|---|
| Pod lifecycle | Kubernetes Deployment manifests in `k8s/` | ✅ |
| Node LAN IP injection | Downward API `status.hostIP` → `NODE_IP` env | ✅ |
| Inference port egress | NetworkPolicy allows 1234/11434/8000 from logos pod | ✅ |
| Resource requests/limits | CPU/memory per pod; `SPAWN_CPU/MEM_THRESHOLD` env | ✅ |
| Health probes | `/health/ready` readiness, `/health` liveness | ✅ |
| Canary deployment | Separate `logos-canary` pod; `/canary/status` endpoint | ✅ |
| Pod naming | `_safe_k8s_name()` sanitises to DNS-1123 | ✅ |
| Shared memory volume | `logos-shared-memory-pvc` mounted read-only by spawned instances | ✅ |

---

## Summary

**What's truly the same on both platforms:**
- Agent core loop, tool system, and all tool logic
- Chat API and SSE streaming
- Config structure (`config.yaml` + `.env`)
- SQLite session store and skill registry
- All messaging platform integrations
- TTS / STT pipeline
- Setup wizard HTML/JS
- MCP (HTTP transport)

**Key Windows limitations:**
- Terminal execution is **local only** — no Docker, Modal, SSH, Daytona backends
- No `/tmp`-based background process nohup pattern
- MCP stdio less reliable — use HTTP transport
- No Kubernetes deployment path (without WSL2 + kind/minikube)

**Key Linux/k8s advantages:**
- Full cluster scaling and resource enforcement
- All 6 terminal execution backends
- Cleaner process isolation via pod boundaries
