# Logos Platform v0.4.21 (2026.3.24)

**Release Date:** March 24, 2026

---

## Highlights

- **Agents tab in all runtime modes** — spawn, list, and monitor agent instances on Windows desktop (local mode) and Kubernetes alike. Per-process CPU% and RAM shown for local instances.
- **Hue-cycle phase lock** — browser favicon, all open tabs, and the Windows tray icon now animate in exact sync using a server-side epoch anchor.
- **VPC / remote inference guide** — setup wizard step 1 now includes a full guide for connecting Ollama or vLLM running on a cloud VM or VPC, with SSH tunnel fallback and networking checklist.
- **Kubeconfig drag-and-drop** — the k8s setup panel now accepts file drops, a browse button, and paste — in addition to the existing textarea.
- **Login → main logo animation fix** — the logo now flies to the correct top-left nav position after login.
- **Profile dropdown fix** — nav bar stacking context corrected; dropdown is now always clickable and correctly backgrounds across all themes.

---

## What's New

### Platform UI

#### Agents Tab (all modes)
- Renamed "Instances" → **"Agents"** tab; now visible regardless of runtime mode.
- **Local mode** resource bar shows psutil free CPU cores and free RAM.
- **k8s mode** resource bar unchanged (cluster-wide used/total with progress bars).
- Instance cards show per-process **CPU%** and **RAM (MB/GB)** for local processes (orange when >80% CPU or >14 GB RAM).
- Local instances normalized to the same API shape as k8s instances — one UI template handles both.
- **"Add Agent" button** added to the chat sidebar — navigates directly to the spawn panel.
- **Chat →** button in the Agents tab works for local instances (`http://127.0.0.1:{port}`).
- Local instances auto-populate the chat agent selector on the Sessions tab.

#### Hue Cycle Sync
- `_HUE_EPOCH_MS` server epoch injected into every page as `window._hueEpochMs`.
- Browser JS seeds `_hueOffset` from the epoch so all tabs open with the same hue phase.
- Favicon updated every ~100ms via `CanvasRenderingContext2D.filter = hue-rotate(Xdeg)` — no separate SVG or server round-trip.
- New public route `GET /api/hue` returns `{epoch_ms, rate}`.
- Windows tray icon animation queries `/api/hue` on startup and computes hue from the shared epoch (6°/s = 60s full cycle), replacing the independent 8s step-based loop.

#### Profile Dropdown
- Nav bar container now has `position:relative; z-index:10` — prevents tab content from covering the dropdown.
- `bg-gray-900` added as explicit Tailwind class on the dropdown for belt-and-suspenders background on all themes.

#### Login Animation
- Fixed 24px Y offset bug: `phase='loggedin'` queues Alpine DOM update asynchronously, so `getBoundingClientRect()` still sees the `logo-up` class transform at read time. The `targetY` calculation now subtracts 24px to compensate.

#### Layout
- Live Executions right panel narrowed from `w-72` (288px) to `w-60` (240px) for more chat space.

### Setup Wizard

#### Step 1 — Connect inference servers
- Heading updated: "Connect your inference server(s)" with subtitle clarifying remote servers are supported.
- Step description in intro panel updated to mention VPC/cloud VM support.
- **Manual add panel** now shows a blue info callout: *"No Logos installation needed on the inference machine — just the inference software and a reachable IP/port."*
- **Server type dropdown** now includes **vLLM** and **OpenAI-compatible** alongside Ollama and LM Studio.
- Placeholder URLs adapt per type (e.g. `http://gpu-vm.vpc:8000` for vLLM).
- Custom name placeholder updated to include VPC examples.

#### New Setup Guide: "Running on a VPC, cloud VM, or remote server"
Collapsible guide covering:
- **Ollama on Linux VM** — one-liner install + `OLLAMA_HOST=0.0.0.0 ollama serve` with copy buttons.
- **vLLM on GPU VM** — `pip install vllm` + serve command with model name.
- **Networking checklist** — firewall/security group rules for AWS/GCP/Azure.
- **SSH tunnel fallback** — `ssh -L 11434:localhost:11434 user@your-vm-ip` for private VPCs with no firewall changes.

#### k8s Setup — Kubeconfig Input
- Kubeconfig textarea now wrapped in a dashed drop zone.
- Supports **drag-and-drop** (`.yaml`, `.yml`, `.conf`, `text/plain`), **file browse** button, and **paste** — all three populate the same `kubeconfig` field.
- Drop zone highlights on hover (`border-indigo-600`).
- `/api/setup/test-k8s` added to `_PUBLIC_PATHS` — was incorrectly requiring auth during the pre-login setup flow.

### Windows Tray
- Tray icon animation rate changed from 8s/cycle to **60s/cycle** (matching browser idle rate).
- Phase-locked to server epoch via `/api/hue` — tray and all browser tabs stay in sync.
- `urllib.request` used for the epoch fetch (stdlib only, no new dependency).

### Routing / Proxy
- `_AI_ROUTER_BASE` is now configurable via `AI_ROUTER_BASE` environment variable (default: `http://ai-router.hermes.svc.cluster.local:9001`). Set this on Windows or outside-cluster deployments to point at a reachable router.

---

## Bug Fixes

| Area | Fix |
|---|---|
| Auth middleware | `/api/setup/test-k8s` missing from `_PUBLIC_PATHS` — blocked k8s connection test during initial setup |
| Login animation | Logo lands 24px too low due to async Alpine DOM update race on `logo-up` class removal |
| Profile dropdown | Nav stacking context placed tab content above dropdown, blocking clicks on all themes |
| Tray icon | Rate mismatch (8s vs 60s) caused visible phase drift vs browser UI |
| Local executor | `list_instances()` did not include per-process CPU/RAM stats |

---

## Internal / Infrastructure

- `gateway/executors/local.py` — `list_instances()` now calls `psutil.Process(pid).cpu_percent()` and `.memory_info().rss` per live instance.
- `gateway/auth/middleware.py` — `/api/hue` and `/api/setup/test-k8s` added to `_PUBLIC_PATHS`.
- `launcher/hermes_launcher.py` — epoch-based phase lock replaces step counter; `_fetch_epoch()` uses stdlib `urllib.request` only.
- `docs/project/WINDOWS_DESKTOP.md` — Phase 3 marked complete; UI gating section updated to reflect Agents tab visibility in all modes.
