# Security & Binary Verification

## Windows installer

### Why does Windows show a warning?

Logos is an early-stage open-source project. Code signing certificates require an identity validation process that is not yet available to us. Rather than pay a third party for an unvalidatable certificate, we publish full build transparency instead.

When you first run the installer, Windows SmartScreen may show:

> **"Windows protected your PC"**

Click **"More info"**, then **"Run anyway"**.

This is safe **if and only if** the SHA256 hash of your download matches the published hash (see below). If the hashes match, the file is byte-for-byte identical to what was produced by our public CI pipeline.

---

### Verify SHA256 hash

Every release publishes SHA256 hashes for the installer and the inner `Logos.exe` binary. These are listed in:

- The **GitHub Release notes** for each tag
- A **`SHA256SUMS.txt`** file attached to each release
- A **`.sha256` sidecar file** next to the installer download

#### On Windows (PowerShell)

```powershell
# Download the installer, then:
certutil -hashfile LogosSetup-X.Y.Z.exe SHA256

# Or with Get-FileHash:
(Get-FileHash "LogosSetup-X.Y.Z.exe" -Algorithm SHA256).Hash
```

Compare the output to the hash published in the release notes. They must match exactly.

#### On macOS / Linux

```bash
sha256sum LogosSetup-X.Y.Z.exe
```

---

### Build transparency

Every Logos Windows binary is built by GitHub Actions on a fresh `windows-latest` runner — no local machines, no manual steps.

- **Source → binary pipeline:** `.github/workflows/build-windows.yml`
- **Every release links to its exact workflow run** in the release notes
- The full build log is publicly visible — you can see every command that ran

This means the hash in the release notes is produced by the same public source code you can read and build yourself.

---

### Building from source

If you prefer not to trust the pre-built binary at all, you can build it yourself:

```bash
# Prerequisites: Python 3.11, uv, Inno Setup 6 (Windows)
git clone https://github.com/GregsGreyCode/logos.git
cd logos
uv venv .venv --python 3.11
.venv\Scripts\activate
pip install -e ".[messaging,cron,pty,mcp,acp]"
pip install pyinstaller pystray Pillow
pyinstaller launcher\hermes_launcher.spec --noconfirm
# Then run Inno Setup: ISCC installer\logos.iss
```

---

### VirusTotal

Before each release we upload the installer to VirusTotal. The scan link is included in the GitHub Release notes where available.

You can also scan any downloaded file yourself at [virustotal.com](https://www.virustotal.com).

---

### Reporting a vulnerability

If you find a security issue in Logos, please open a [GitHub Issue](https://github.com/GregsGreyCode/logos/issues) marked **[SECURITY]**, or email the maintainer directly. Do not disclose vulnerabilities publicly until they have been patched.
