---
sidebar_position: 4
title: "Updating & Uninstalling"
description: "How to update Logos to the latest version or uninstall it"
---

# Updating & Uninstalling

## Updating

### Windows desktop app

Logos checks for updates automatically in the background. When a new version is available:

1. A notification appears in the system tray
2. Open the tray menu or the **account menu** (top-right of the dashboard) — both show the available version
3. Click **Download update…** to fetch the installer, then **Install & restart now** once it is ready

Logos stops itself, releases all file locks, and launches the installer silently. It restarts automatically once the install completes.

### Source install (Linux / macOS / WSL2)

```bash
cd /path/to/logos
git pull origin main
git submodule update --init --recursive

# Reinstall (picks up new dependencies)
export VIRTUAL_ENV="$(pwd)/venv"
uv pip install -e ".[all]"
```

Or use the built-in update command:

```bash
logos update
```

### Docker Compose

```bash
cd /path/to/logos
git pull origin main
docker compose build
docker compose up -d
```

---

## Uninstalling

### Windows desktop app

Use **Add or Remove Programs** → **Logos** → Uninstall. Your configuration files in `%USERPROFILE%\.logos\` are kept by default — delete that folder manually to remove all data.

### Source install

```bash
# Remove the virtual environment and source
rm -rf /path/to/logos/venv
rm -rf /path/to/logos

# Optional — remove config and session data
rm -rf ~/.logos
```

:::info
If you installed the gateway as a system service, stop and disable it first:
```bash
logos gateway stop
# Linux: systemctl --user disable logos-gateway
# macOS: launchctl remove ai.logos.gateway
```
:::

### Docker Compose

```bash
cd /path/to/logos
docker compose down -v    # Stop containers and remove volumes
```

### Kubernetes

```bash
kubectl delete -f k8s/
```
