#!/usr/bin/env bash
# ============================================================================
# Logos Fresh Install
# ============================================================================
# One-shot installer for a prepared Linux/WSL2/macOS host.
#
# Runs from anywhere — clones the repo, installs every dep, seeds ~/.logos,
# symlinks the `logos` CLI into ~/.local/bin, and leaves you one command
# away from launching the gateway.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/logos/main/scripts/fresh-install.sh | bash
#
#   OR (from the repo directory):
#   ./scripts/fresh-install.sh
#
# Flags (env):
#   LOGOS_REPO_DIR    Where to clone (default: $HOME/logos)
#   LOGOS_BRANCH      Branch to check out (default: main)
#   INSTALL_OPENSHELL Set to "1" to also install the openshell CLI binary
#                     from ghcr.io/nvidia/openshell (needed for sandboxed
#                     multi-agent setups). Default: "0" — skip.
#   BUMP_INOTIFY      Set to "1" to sysctl-bump fs.inotify.max_user_instances
#                     to 8192 (needed for ≥8 openshell routes). Requires
#                     sudo. Default: "0" — skip.
#   SKIP_NPM          Set to "1" to skip `npm install` (browser automation +
#                     WhatsApp bridge won't work, but everything else will).
#   START_AFTER       Set to "1" to launch `logos gateway start` at the end.
#                     Default: "0" — print the command instead.
#
# Idempotent: safe to re-run. Each step checks if work is already done and
# skips if so, so this script is also a "repair" tool.
# ============================================================================

set -euo pipefail

# ── Colours ──
readonly C_GREEN=$'\033[0;32m'
readonly C_YELLOW=$'\033[0;33m'
readonly C_CYAN=$'\033[0;36m'
readonly C_RED=$'\033[0;31m'
readonly C_DIM=$'\033[2m'
readonly C_RESET=$'\033[0m'

log()  { printf '%s\n' "${C_CYAN}▸${C_RESET} $*"; }
ok()   { printf '%s\n' "${C_GREEN}✓${C_RESET} $*"; }
warn() { printf '%s\n' "${C_YELLOW}⚠${C_RESET} $*" >&2; }
die()  { printf '%s\n' "${C_RED}✗${C_RESET} $*" >&2; exit 1; }
hdr()  { printf '\n%s\n%s\n' "${C_CYAN}$*${C_RESET}" "${C_DIM}$(printf '%.0s─' {1..60})${C_RESET}"; }

# ── Config ──
REPO_URL="${LOGOS_REPO_URL:-https://github.com/GregsGreyCode/logos.git}"
REPO_DIR="${LOGOS_REPO_DIR:-$HOME/logos}"
BRANCH="${LOGOS_BRANCH:-main}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
INSTALL_OPENSHELL="${INSTALL_OPENSHELL:-0}"
BUMP_INOTIFY="${BUMP_INOTIFY:-0}"
SKIP_NPM="${SKIP_NPM:-0}"
START_AFTER="${START_AFTER:-0}"

# ── Preflight ──
hdr "Preflight"

command -v git >/dev/null 2>&1 || die "git not found. Install git first (e.g. \`apt install git\`)."
ok "git present"

# If running inside an existing clone, use that instead of cloning again.
if [[ -f "$(pwd)/pyproject.toml" && -d "$(pwd)/gateway" ]]; then
    REPO_DIR="$(pwd)"
    ok "running inside existing Logos clone at $REPO_DIR — skipping clone"
    SKIP_CLONE=1
else
    SKIP_CLONE=0
fi

# ── uv ──
hdr "uv (Python package manager)"
if command -v uv >/dev/null 2>&1; then
    ok "uv already installed: $(uv --version)"
else
    log "installing uv …"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to $HOME/.local/bin — make it visible in this shell
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null || die "uv install failed — check the installer output above."
    ok "uv installed"
fi

# ── Clone ──
if [[ "$SKIP_CLONE" == "0" ]]; then
    hdr "Clone repo"
    if [[ -d "$REPO_DIR/.git" ]]; then
        log "existing clone at $REPO_DIR — pulling latest …"
        (cd "$REPO_DIR" && git fetch origin && git checkout "$BRANCH" && git pull --ff-only origin "$BRANCH")
    else
        log "cloning into $REPO_DIR (branch=$BRANCH) …"
        git clone --recurse-submodules --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
    fi
    ok "repo at $REPO_DIR"
fi

cd "$REPO_DIR"

# Submodules — ensure they're initialised even on a re-run
hdr "Submodules"
git submodule update --init --recursive
ok "submodules synced"

# ── venv ──
hdr "Python venv (${PYTHON_VERSION})"
if [[ -d "venv" ]] && [[ -x "venv/bin/python" ]]; then
    ok "venv already exists at $REPO_DIR/venv"
else
    log "creating venv via uv …"
    uv venv venv --python "$PYTHON_VERSION"
    ok "venv created"
fi
export VIRTUAL_ENV="$REPO_DIR/venv"

# ── Python deps ──
hdr "Python dependencies"
log "installing logos + all extras …"
uv pip install -e ".[all]"
if [[ -d "mini-swe-agent" ]]; then
    log "installing mini-swe-agent submodule …"
    uv pip install -e "./mini-swe-agent"
fi
ok "Python deps installed"

# ── Node deps ──
if [[ "$SKIP_NPM" != "1" ]]; then
    hdr "Node dependencies (browser + WhatsApp)"
    if command -v npm >/dev/null 2>&1; then
        log "installing node modules …"
        # Use ``npm ci`` when a lockfile is present — it's the reproducible
        # path and doesn't rewrite package-lock.json, so re-running the
        # installer no longer leaves the clone dirty with lockfile churn.
        # Fall back to ``npm install`` when there's no lockfile yet.
        if [[ -f "package-lock.json" ]]; then
            npm ci --silent || warn "npm ci emitted warnings — check output"
        else
            npm install --silent || warn "npm install emitted warnings — check output"
        fi
        ok "node deps installed"
    else
        warn "npm not found — browser tools / WhatsApp bridge won't work. Install Node.js ≥20 to enable them."
    fi
else
    warn "SKIP_NPM=1 — skipping npm install (browser + WhatsApp disabled)"
fi

# ── ~/.logos layout ──
hdr "~/.logos directory"
mkdir -p "$HOME/.logos"/{cron,sessions,logs,memories,skills,pairing,hooks,image_cache,audio_cache,whatsapp/session,agents}
if [[ ! -f "$HOME/.logos/config.yaml" ]]; then
    if [[ -f "cli-config.yaml.example" ]]; then
        cp cli-config.yaml.example "$HOME/.logos/config.yaml"
        ok "copied default config.yaml"
    else
        : > "$HOME/.logos/config.yaml"
        ok "created empty config.yaml (no template found)"
    fi
else
    ok "config.yaml already present — left unchanged"
fi
[[ -f "$HOME/.logos/.env" ]] || touch "$HOME/.logos/.env"
ok "$HOME/.logos layout ready"

# ── Symlink logos into PATH ──
hdr "CLI symlinks"
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO_DIR/venv/bin/logos" "$HOME/.local/bin/logos"
[[ -x "$REPO_DIR/venv/bin/hermes" ]] && ln -sf "$REPO_DIR/venv/bin/hermes" "$HOME/.local/bin/hermes" || true
ok "symlinked logos → ~/.local/bin/logos"

# If ~/.local/bin isn't on PATH, add it for this shell and suggest a durable fix
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    warn "~/.local/bin is not on your PATH. Add this to your shell rc:"
    printf '    echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc\n'
    printf '    source ~/.bashrc\n'
fi

# ── Optional: OpenShell CLI ──
if [[ "$INSTALL_OPENSHELL" == "1" ]]; then
    hdr "OpenShell CLI (optional)"
    if command -v openshell >/dev/null 2>&1; then
        ok "openshell already installed: $(openshell --version 2>&1 | head -1)"
    else
        log "fetching openshell release …"
        # Prefer the static Rust binary (musl tarball) over the Python
        # wheel. Reason: openshell 0.0.28+ ships wheels that require
        # Python >=3.12, but we pin our venv to 3.11 to keep the agent
        # runtime on a widely-available Python. The binary has no
        # Python dependency so it works regardless of venv version.
        OSH_JSON="$(curl -fsSL https://api.github.com/repos/NVIDIA/OpenShell/releases/latest || true)"
        OSH_TGZ="$(printf '%s' "$OSH_JSON" | grep -oE 'https://[^"]+x86_64-unknown-linux-musl\.tar\.gz' | head -1)"
        OSH_WHL="$(printf '%s' "$OSH_JSON" | grep -oE 'https://[^"]+manylinux[^"]*x86_64\.whl' | head -1)"
        if [[ -n "$OSH_TGZ" ]]; then
            log "installing openshell binary ($(basename "$OSH_TGZ"))"
            TMP=$(mktemp -d)
            curl -fsSL "$OSH_TGZ" | tar -xz -C "$TMP"
            BIN="$(find "$TMP" -type f -name openshell | head -1)"
            if [[ -n "$BIN" ]]; then
                install -m 0755 "$BIN" "$HOME/.local/bin/openshell"
                ok "openshell installed → ~/.local/bin/openshell"
            else
                warn "openshell binary not found in archive — install manually"
            fi
            rm -rf "$TMP"
        elif [[ -n "$OSH_WHL" ]]; then
            log "binary tarball not found — trying wheel ($(basename "$OSH_WHL"))"
            if uv pip install "$OSH_WHL" 2>&1 | tail -20; then
                ok "openshell installed into venv"
            else
                warn "wheel install failed (often: wheel requires newer Python than our 3.11 venv). Install manually from https://github.com/NVIDIA/OpenShell/releases"
            fi
        else
            warn "could not locate an openshell linux/x86_64 asset — install manually from https://github.com/NVIDIA/OpenShell/releases"
        fi
    fi
    # Docker check — openshell needs it
    if command -v docker >/dev/null 2>&1; then
        ok "docker present: $(docker --version)"
    else
        warn "openshell needs docker. Install it (e.g. \`curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker \$USER\`) and log out/in."
    fi
fi

# ── Optional: bump inotify ──
if [[ "$BUMP_INOTIFY" == "1" ]]; then
    hdr "Sysctl: fs.inotify.max_user_instances"
    CUR=$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)
    if [[ "$CUR" -ge 8192 ]]; then
        ok "max_user_instances=$CUR (already sufficient)"
    else
        log "raising max_user_instances from $CUR to 8192 (will prompt for sudo password) …"
        # Just try sudo — it prompts if needed. Previous check used
        # ``sudo -n`` which only succeeds with *passwordless* sudo and
        # bailed on every normal desktop. A single ``sudo -v`` pre-
        # authenticates so the three calls below don't re-prompt.
        if [[ $EUID -ne 0 ]]; then
            sudo -v || {
                warn "sudo authentication failed — run these manually:"
                printf "  echo 'fs.inotify.max_user_instances=8192' | sudo tee -a /etc/sysctl.d/99-openshell.conf\n"
                printf "  echo 'fs.inotify.max_user_watches=1048576'  | sudo tee -a /etc/sysctl.d/99-openshell.conf\n"
                printf "  sudo sysctl --system\n"
                exit 0
            }
        fi
        echo 'fs.inotify.max_user_instances=8192' | sudo tee -a /etc/sysctl.d/99-openshell.conf >/dev/null
        echo 'fs.inotify.max_user_watches=1048576'  | sudo tee -a /etc/sysctl.d/99-openshell.conf >/dev/null
        sudo sysctl --system >/dev/null
        ok "inotify limits bumped (persisted in /etc/sysctl.d/99-openshell.conf)"
    fi
fi

# ── Doctor / verify ──
hdr "Verify"
if "$REPO_DIR/venv/bin/logos" version >/dev/null 2>&1; then
    ok "logos CLI working: $("$REPO_DIR/venv/bin/logos" version | head -1)"
else
    warn "logos CLI returned non-zero — inspect output above"
fi

# ── Done ──
hdr "Done"
# Note: double-quoted printf strings so bash expands ${C_DIM} etc before
# printf ever sees them. Single quotes were swallowing the colour vars
# and dumping literal ${C_DIM} into the terminal.
printf "%s\n" "${C_GREEN}Logos installed at $REPO_DIR${C_RESET}"
printf "\nNext steps:\n"
printf "  1. Open a new shell (or run ${C_DIM}source ~/.bashrc${C_RESET}) so ${C_DIM}logos${C_RESET} is on PATH.\n"
printf "  2. Launch the gateway:            ${C_CYAN}logos gateway start${C_RESET}\n"
printf "  3. Open the setup wizard in your browser:\n"
printf "     ${C_CYAN}http://localhost:8091/setup${C_RESET}\n"
printf "     (it will discover LM Studio/Ollama, provision routes, and create your first agent.)\n"
printf "\nDirectly edit config:  ${C_DIM}%s/.env${C_RESET}\n" "$HOME/.logos"
printf "Install as a service:  ${C_DIM}logos gateway install${C_RESET}\n"
printf "Diagnostics:           ${C_DIM}logos doctor${C_RESET}\n"

if [[ "$START_AFTER" == "1" ]]; then
    hdr "Launching gateway"
    exec "$REPO_DIR/venv/bin/logos" gateway start
fi
