# Security & Binary Verification

## ⚠️ Why Windows shows a warning

Logos is currently unsigned. Code signing certificates is either quite expensive at this time or seems to require an identity validation process that is not yet available to us in the UK.

We publish full build transparency instead.

When you first run the installer, Windows SmartScreen may show:

> **"Windows protected your PC"**

Click **"More info"**, then **"Run anyway"**.

This is safe once you have verified the SHA256 hash of your download matches the published value (see below).

---

## 🔐 Build transparency

Logos binaries are built exclusively via GitHub Actions — no local machines, no manual steps, no hidden stages.

- Source → build → artifact pipeline is fully public
- Every release links to the exact CI run that produced the binary
- [View all Windows builds](https://github.com/GregsGreyCode/logos/actions/workflows/build-windows.yml)

Anyone can audit the full build log to confirm exactly what commands ran and in what order.

---

## 🔑 SHA256 verification

Every release publishes SHA256 hashes for both the installer and the inner `Logos.exe` binary. These appear in:

- The **GitHub Release notes** for each tag
- A **`SHA256SUMS.txt`** file attached to each release
- A **`.sha256` sidecar file** next to the installer download

All three are produced by the same CI run that built the binary.

### Verify on Windows

```powershell
# certutil (built into all Windows versions):
certutil -hashfile LogosSetup-X.Y.Z.exe SHA256

# Or with PowerShell's Get-FileHash:
(Get-FileHash "LogosSetup-X.Y.Z.exe" -Algorithm SHA256).Hash
```

Compare the output to the hash in the GitHub Release notes. They must match exactly.

### Verify on macOS / Linux

```bash
sha256sum LogosSetup-X.Y.Z.exe
```

---

## 🧪 VirusTotal scan

Each release includes a VirusTotal link in the GitHub Release notes, constructed from the installer's SHA256 hash. The link format is:

```
https://www.virustotal.com/gui/file/{sha256}
```

You can also drag-and-drop your downloaded file at [virustotal.com](https://www.virustotal.com) to run your own scan at any time.

---

## Building from source

If you prefer not to trust any pre-built binary, you can build it yourself:

```bash
# Prerequisites: Python 3.11, uv, Inno Setup 6 (Windows only)
git clone https://github.com/GregsGreyCode/logos.git
cd logos
uv venv .venv --python 3.11
.venv\Scripts\activate
pip install -e ".[messaging,cron,pty,mcp,acp]"
pip install pyinstaller pystray Pillow
pyinstaller launcher\hermes_launcher.spec --noconfirm
# Then build the installer:
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\logos.iss
```

The output will be in `installer\output\`.

---

## Reporting a vulnerability

If you find a security issue in Logos, open a [GitHub Issue](https://github.com/GregsGreyCode/logos/issues) with **[SECURITY]** in the title, or contact the maintainer directly. Please do not disclose vulnerabilities publicly until they have been addressed.
