---
sidebar_position: 3
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

### Manual update (source install)

```bash
cd /path/to/logos
git pull origin main
git submodule update --init --recursive

# Reinstall (picks up new dependencies)
uv pip install -e ".[all]"
```

---

## Uninstalling

### Windows desktop app

Use **Add or Remove Programs** → **Logos** → Uninstall. Your configuration files in `%USERPROFILE%\.hermes\` are kept by default.

### Source install

```bash
# Remove the virtual environment and source
rm -rf /path/to/logos/venv
rm -rf /path/to/logos

# Optional — remove config and session data
rm -rf ~/.hermes
```

:::info
If you installed the gateway as a system service, stop and disable it first:
```bash
hermes gateway stop
# Linux: systemctl --user disable hermes-gateway
# macOS: launchctl remove ai.hermes.gateway
```
:::
