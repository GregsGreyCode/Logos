#!/usr/bin/env bash
# ============================================================================
# Logos Fresh Uninstall
# ============================================================================
# Reverses everything `scripts/fresh-install.sh` does — cleans the install
# directory, ~/.logos config, CLI symlinks, and (optionally) the OpenShell
# binary + sysctl bumps.
#
# Does NOT touch:
#   - `uv` itself
#   - Docker or your docker-group membership
#   - Any running openshell-cluster containers (destroy them separately via
#     `openshell gateway destroy --force --gateway <name>` if desired)
#   - Files outside the paths listed below
#
# Usage:
#   ./scripts/fresh-uninstall.sh                   # prompt before each step
#   ./scripts/fresh-uninstall.sh --yes             # no prompts, wipe everything
#   PURGE_SYSCTL=1 ./scripts/fresh-uninstall.sh --yes   # also remove /etc/sysctl.d/99-openshell.conf
# ============================================================================

set -euo pipefail

readonly C_GREEN=$'\033[0;32m'
readonly C_YELLOW=$'\033[0;33m'
readonly C_CYAN=$'\033[0;36m'
readonly C_RED=$'\033[0;31m'
readonly C_DIM=$'\033[2m'
readonly C_RESET=$'\033[0m'

log()  { printf '%s\n' "${C_CYAN}▸${C_RESET} $*"; }
ok()   { printf '%s\n' "${C_GREEN}✓${C_RESET} $*"; }
warn() { printf '%s\n' "${C_YELLOW}⚠${C_RESET} $*" >&2; }
hdr()  { printf '\n%s\n%s\n' "${C_CYAN}$*${C_RESET}" "${C_DIM}$(printf '%.0s─' {1..60})${C_RESET}"; }

AUTO_YES=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) AUTO_YES=1 ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
    esac
done

confirm() {
    local prompt="$1"
    if [[ "$AUTO_YES" == "1" ]]; then return 0; fi
    read -r -p "${C_YELLOW}?${C_RESET} $prompt [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]]
}

REPO_DIR="${LOGOS_REPO_DIR:-$HOME/logos}"
LOGOS_HOME="${LOGOS_HOME:-$HOME/.logos}"
PURGE_SYSCTL="${PURGE_SYSCTL:-0}"

hdr "Logos Fresh Uninstall"
printf '  repo:       %s\n' "$REPO_DIR"
printf '  config:     %s\n' "$LOGOS_HOME"
printf '  CLI:        ~/.local/bin/logos, ~/.local/bin/openshell (optional)\n'
printf '  sysctl:     /etc/sysctl.d/99-openshell.conf (only with PURGE_SYSCTL=1)\n'

# ── 1. Stop a running gateway (cannot wipe files out from under it cleanly) ──
hdr "Stop gateway"
if pgrep -f "gateway\.run" >/dev/null 2>&1; then
    if confirm "a Logos gateway is running — stop it now?"; then
        pkill -TERM -f "gateway\.run" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            if ! pgrep -f "gateway\.run" >/dev/null 2>&1; then break; fi
            sleep 1
        done
        pkill -KILL -f "gateway\.run" 2>/dev/null || true
        ok "gateway stopped"
    else
        warn "leaving gateway running — files in $REPO_DIR may be held open"
    fi
else
    ok "no gateway running"
fi

# ── 2. Delete OpenShell sandboxes (optional) ──
if command -v openshell >/dev/null 2>&1 && command -v docker >/dev/null 2>&1; then
    SANDBOXES=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E "^(openshell-cluster-|hermes-)" | head -20 || true)
    if [[ -n "$SANDBOXES" ]]; then
        hdr "OpenShell containers"
        printf '%s\n' "$SANDBOXES" | sed 's/^/  /'
        if confirm "delete these openshell + sandbox containers?"; then
            while IFS= read -r c; do
                docker rm -f "$c" 2>&1 | tail -1
            done <<<"$SANDBOXES"
            ok "containers removed"
        else
            warn "leaving openshell containers running"
        fi
    fi
fi

# ── 3. Config ~/.logos ──
hdr "~/.logos config"
if [[ -d "$LOGOS_HOME" ]]; then
    if confirm "delete $LOGOS_HOME (auth.db, .env, sessions, memories, agents, logs)? this is NOT recoverable"; then
        rm -rf "$LOGOS_HOME"
        ok "$LOGOS_HOME removed"
    else
        warn "keeping $LOGOS_HOME — re-running fresh-install.sh will reuse it"
    fi
else
    ok "$LOGOS_HOME already absent"
fi

# ── 4. Repo directory ──
hdr "Repo directory"
if [[ -d "$REPO_DIR" ]]; then
    if confirm "delete $REPO_DIR (repo + venv + node_modules)? not recoverable"; then
        rm -rf "$REPO_DIR"
        ok "$REPO_DIR removed"
    else
        warn "keeping $REPO_DIR"
    fi
else
    ok "$REPO_DIR already absent"
fi

# ── 5. CLI symlinks ──
hdr "CLI symlinks"
for link in "$HOME/.local/bin/logos" "$HOME/.local/bin/hermes"; do
    if [[ -L "$link" || -e "$link" ]]; then
        rm -f "$link"
        ok "removed $link"
    fi
done

# openshell is a real binary (from the static tarball), not a symlink —
# ask separately so the user can keep it if they use it standalone.
if [[ -x "$HOME/.local/bin/openshell" ]]; then
    if confirm "remove ~/.local/bin/openshell?"; then
        rm -f "$HOME/.local/bin/openshell"
        ok "removed ~/.local/bin/openshell"
    fi
fi

# ── 6. Sysctl file (opt-in) ──
if [[ "$PURGE_SYSCTL" == "1" ]]; then
    hdr "Sysctl file"
    if [[ -f /etc/sysctl.d/99-openshell.conf ]]; then
        log "removing /etc/sysctl.d/99-openshell.conf (prompts for sudo) …"
        sudo rm -f /etc/sysctl.d/99-openshell.conf
        sudo sysctl --system >/dev/null
        ok "sysctl file removed + kernel defaults re-applied"
    else
        ok "no sysctl file to remove"
    fi
else
    printf '\n%s skipping /etc/sysctl.d/99-openshell.conf (pass PURGE_SYSCTL=1 to remove)\n' "${C_DIM}•${C_RESET}"
fi

# ── Done ──
hdr "Done"
printf "%s\n" "${C_GREEN}Logos uninstalled.${C_RESET}"
printf '\nTo reinstall:\n'
printf "  ${C_CYAN}curl -fsSL https://raw.githubusercontent.com/GregsGreyCode/Logos/main/scripts/fresh-install.sh \\\\\n"
printf "    | INSTALL_OPENSHELL=1 BUMP_INOTIFY=1 bash${C_RESET}\n"
