#!/usr/bin/env bash
# ============================================================================
# Logos Fresh Uninstall
# ============================================================================
# Fully uninstalls Logos. One top-level confirmation, then it wipes
# everything Logos owns — containers, config, DB, repo, CLI symlink, and
# the OpenShell cluster + sandbox Docker images.
#
# Per-step prompts were removed after repeatedly producing partial states:
# e.g. cluster containers deleted but auth.db.model_routes rows kept, or
# docker pulled a newer :latest but a stale :0.0.33 image tag stayed
# cached and openshell's bootstrap picked the older tag on next start.
# "Fully uninstall" is what the script name promises; if you want a soft
# reset, back up the bits you care about first.
#
# Always wiped (no prompt):
#   - Running Logos gateway process
#   - All openshell-cluster-* and hermes-* Docker containers
#   - All openshell-cluster-* and hermes-* Docker named volumes
#     (reason: each cluster container mounts /var/lib/rancher/k3s from a
#     named volume — that's where k3s stores etcd, PVC data, and pod
#     records. Nuking containers alone leaves the volumes intact, so on
#     reinstall the new cluster container re-mounts the old state and
#     previously-provisioned sandboxes walk right back out of etcd.
#     Symptom: auth.db has 1 agent but `openshell sandbox list` shows four.)
#   - Docker images: ghcr.io/nvidia/openshell/cluster:* and hermes-sandbox:*
#     (reason: version skew between a stale cluster image and the current
#     openshell CLI produces a cryptic "supervisor session not connected"
#     error with no obvious root cause. Wiping forces a clean re-pull.)
#   - $LOGOS_HOME (~/.logos: auth.db, .env, sessions, memories, agents, logs)
#   - $REPO_DIR (~/logos: repo + venv + node_modules)
#   - ~/.local/bin/logos
#   - ~/.config/openshell (gateway metadata + mTLS certs — stale entries
#     pointing at deleted containers confuse the next provision)
#
# Still prompted (separate tool, not exclusively Logos):
#   - ~/.local/bin/openshell
#
# Still opt-in via env var (system-level, may affect other users):
#   - /etc/sysctl.d/99-openshell.conf   (PURGE_SYSCTL=1)
#
# Never touched:
#   - Docker itself, docker-group membership
#   - uv, python, the OS
#   - Other Docker images (we only remove Logos/OpenShell-tagged ones)
#
# Usage:
#   ./scripts/fresh-uninstall.sh                        # one confirmation
#   ./scripts/fresh-uninstall.sh --yes                  # skip confirmation
#   PURGE_SYSCTL=1 ./scripts/fresh-uninstall.sh --yes   # also remove sysctl file
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
            sed -n '2,47p' "$0"
            exit 0
            ;;
    esac
done

REPO_DIR="${LOGOS_REPO_DIR:-$HOME/logos}"
LOGOS_HOME="${LOGOS_HOME:-$HOME/.logos}"
OPENSHELL_CONFIG="${OPENSHELL_CONFIG:-$HOME/.config/openshell}"
PURGE_SYSCTL="${PURGE_SYSCTL:-0}"

hdr "Logos Fresh Uninstall"
printf '  repo:        %s\n' "$REPO_DIR"
printf '  config:      %s\n' "$LOGOS_HOME"
printf '  openshell:   %s\n' "$OPENSHELL_CONFIG"
printf '  CLI:         ~/.local/bin/logos\n'
printf '  containers:  openshell-cluster-*, hermes-*\n'
printf '  volumes:     openshell-cluster-*, hermes-*\n'
printf '  images:      ghcr.io/nvidia/openshell/cluster:*, hermes-sandbox:*\n'
printf '  gateway:     any running logos gateway.run process\n'
printf '\n  prompted:    ~/.local/bin/openshell (separate tool)\n'
printf '  opt-in:      /etc/sysctl.d/99-openshell.conf (PURGE_SYSCTL=1)\n'

# Single top-level safety gate. --yes bypasses it.
if [[ "$AUTO_YES" != "1" ]]; then
    printf '\n%sThis will permanently delete everything above. Continue?%s [y/N] ' \
        "${C_YELLOW}" "${C_RESET}"
    read -r ans
    if [[ ! "$ans" =~ ^[Yy]$ ]]; then
        printf '\n%s aborted — nothing was changed.\n' "${C_DIM}•${C_RESET}"
        exit 0
    fi
fi

# ── 1. Stop a running gateway (always) ──────────────────────────────────
hdr "Stop gateway"
if pgrep -f "gateway\.run" >/dev/null 2>&1; then
    pkill -TERM -f "gateway\.run" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        if ! pgrep -f "gateway\.run" >/dev/null 2>&1; then break; fi
        sleep 1
    done
    pkill -KILL -f "gateway\.run" 2>/dev/null || true
    ok "gateway stopped"
else
    ok "no gateway running"
fi

# ── 2. Delete OpenShell + sandbox containers (always) ───────────────────
hdr "OpenShell containers"
if command -v docker >/dev/null 2>&1; then
    SANDBOXES=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E "^(openshell-cluster-|hermes-)" || true)
    if [[ -n "$SANDBOXES" ]]; then
        printf '%s\n' "$SANDBOXES" | sed 's/^/  /'
        while IFS= read -r c; do
            docker rm -f "$c" >/dev/null 2>&1 && ok "removed $c" || warn "could not remove $c"
        done <<<"$SANDBOXES"
    else
        ok "no openshell / sandbox containers to remove"
    fi
else
    warn "docker not available — skipping container cleanup"
fi

# ── 2b. Delete OpenShell + sandbox Docker volumes (always) ──────────────
# Each cluster container mounts /var/lib/rancher/k3s from a named volume
# (e.g. openshell-cluster-qwen3-5-9b). That path holds k3s's etcd, PVC
# data, and pod records. Without this step the volumes survive `docker rm`
# and the next reinstall's fresh cluster container re-mounts them,
# resurrecting every previously-provisioned sandbox. Run this AFTER the
# container removal above — Docker refuses to rm volumes still in use.
hdr "OpenShell volumes"
if command -v docker >/dev/null 2>&1; then
    VOLUMES=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E "^(openshell-cluster-|hermes-)" || true)
    if [[ -n "$VOLUMES" ]]; then
        printf '%s\n' "$VOLUMES" | sed 's/^/  /'
        while IFS= read -r v; do
            docker volume rm -f "$v" >/dev/null 2>&1 && ok "removed volume $v" || warn "could not remove volume $v"
        done <<<"$VOLUMES"
    else
        ok "no openshell / sandbox volumes to remove"
    fi
fi

# ── 3. Delete OpenShell cluster + sandbox Docker images (always) ────────
# Stale cluster images are the root cause of the "works on a fresh host
# but fails on upgrade" class of bug: openshell CLI 0.0.N expects cluster
# image 0.0.N, but if 0.0.M<N is still cached (survived prior uninstall
# since images aren't tied to containers in Docker's GC model), the CLI's
# bootstrap happily uses the older tag and the sandbox pods mount a
# mismatched supervisor binary → cryptic "supervisor session not
# connected" on every exec. Wipe all tags of these repos unconditionally
# so the next install starts with nothing cached and pulls fresh.
hdr "OpenShell + sandbox images"
if command -v docker >/dev/null 2>&1; then
    IMAGES=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | \
        grep -E "^(ghcr\.io/nvidia/openshell/cluster|hermes-sandbox):" || true)
    if [[ -n "$IMAGES" ]]; then
        while IFS= read -r img; do
            docker rmi -f "$img" >/dev/null 2>&1 && ok "removed $img" || warn "could not remove $img"
        done <<<"$IMAGES"
    else
        ok "no logos / openshell images to remove"
    fi
fi

# ── 4. Config ~/.logos (always) ─────────────────────────────────────────
hdr "Config directory"
if [[ -d "$LOGOS_HOME" ]]; then
    rm -rf "$LOGOS_HOME"
    ok "$LOGOS_HOME removed"
else
    ok "$LOGOS_HOME already absent"
fi

# ── 5. OpenShell CLI state (~/.config/openshell) (always) ───────────────
# Contains per-gateway metadata.json + mTLS cert pairs. After deleting
# cluster containers, stale metadata still points at them — next gateway
# start re-reads it, expects a live cluster at the stored endpoint, and
# can wedge the bootstrap.
hdr "OpenShell CLI state"
if [[ -d "$OPENSHELL_CONFIG" ]]; then
    rm -rf "$OPENSHELL_CONFIG"
    ok "$OPENSHELL_CONFIG removed"
else
    ok "$OPENSHELL_CONFIG already absent"
fi

# ── 6. Repo directory (always) ──────────────────────────────────────────
# cd out first so we don't rm the cwd underneath ourselves. Bash can
# survive it but the user's shell ends up in a deleted directory.
hdr "Repo directory"
cd /tmp
if [[ -d "$REPO_DIR" ]]; then
    rm -rf "$REPO_DIR"
    ok "$REPO_DIR removed"
else
    ok "$REPO_DIR already absent"
fi

# ── 7. CLI symlink (always) ─────────────────────────────────────────────
hdr "CLI symlink"
if [[ -L "$HOME/.local/bin/logos" || -e "$HOME/.local/bin/logos" ]]; then
    rm -f "$HOME/.local/bin/logos"
    ok "removed ~/.local/bin/logos"
else
    ok "~/.local/bin/logos already absent"
fi

# ── 8. OpenShell binary (prompted, separate tool) ───────────────────────
if [[ -x "$HOME/.local/bin/openshell" ]]; then
    hdr "OpenShell CLI"
    if [[ "$AUTO_YES" == "1" ]]; then
        rm -f "$HOME/.local/bin/openshell"
        ok "removed ~/.local/bin/openshell (--yes)"
    else
        printf '  ~/.local/bin/openshell is a standalone tool (version %s).\n' \
            "$(openshell --version 2>/dev/null | awk '{print $NF}' || echo unknown)"
        printf '  %sRemove it?%s [y/N] ' "${C_YELLOW}" "${C_RESET}"
        read -r ans
        if [[ "$ans" =~ ^[Yy]$ ]]; then
            rm -f "$HOME/.local/bin/openshell"
            ok "removed ~/.local/bin/openshell"
        else
            ok "keeping ~/.local/bin/openshell"
        fi
    fi
fi

# ── 9. Sysctl file (opt-in) ─────────────────────────────────────────────
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
