"""Docker-container lifecycle for MCP tool servers.

Replaces the removed ``gateway/mcp_deploy.py`` (k8s-based). Each managed
MCP server runs as a Docker container named ``mcp-<name>`` on the host
Docker daemon. The container listens on its native port inside the
container (``port`` field in the catalogue entry), which we map to a
dynamically-chosen free host port bound to ``127.0.0.1`` only — no LAN
exposure.

The gateway's MCP proxy (``/mcp/<name>``) forwards agent requests to
``http://127.0.0.1:<host_port><mcp_path>``. From the sandbox side
nothing changes: sandboxes call ``host.openshell.internal:8091/mcp/<name>``
as they do for in-process and external servers.

Rationale for shelling out to the ``docker`` CLI instead of the Python
Docker SDK: the repo has a top-level ``docker/`` directory which takes
import precedence over the ``docker`` PyPI package, so ``from docker
import from_env`` doesn't work cleanly. ``gateway/executors/openshell.py``
already shells out to the CLI for the same reason.

Functions:
    deploy_container(name, image, port, env_vars, mcp_path, ...)
        -> {"url", "container_id", "host_port", "status"}
    undeploy_container(name) -> bool
    restart_container(name) -> bool
    container_status(name) -> str ("running"|"exited"|"missing"|...)
    container_logs(name, tail=200) -> str
"""

from __future__ import annotations

import logging
import random
import shlex
import socket
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Docker container name prefix. Matches the k8s naming from the old
# mcp_deploy.py so existing code paths that log ``mcp-<name>`` still
# read the same.
_CONTAINER_PREFIX = "mcp-"

# Label applied to every MCP container so ``docker ps -f label=...``
# can enumerate them without scraping names. Mirrors the k8s label
# vocabulary from the old deployer.
_LABEL_KEY = "logos.io/mcp-server"
_LABEL_NAME = "logos.io/mcp-name"

# Default port range to pick a free host port from. Kept disjoint from
# the openshell gateway's 9000-9999 allocation.
_HOST_PORT_MIN = 23000
_HOST_PORT_MAX = 23999


def _container_name(name: str) -> str:
    """Return the docker container name for an MCP server."""
    return f"{_CONTAINER_PREFIX}{name}"


def _pick_free_port(
    min_port: int = _HOST_PORT_MIN,
    max_port: int = _HOST_PORT_MAX,
    attempts: int = 20,
) -> int:
    """Return a free TCP port in the given range.

    Tries up to ``attempts`` random ports; raises RuntimeError if none
    are free. The small range keeps MCP deploys out of the way of
    openshell gateway ports and any user-chosen service ports.
    """
    for _ in range(attempts):
        port = random.randint(min_port, max_port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"Could not find a free port between {min_port} and {max_port} after {attempts} tries"
    )


def _docker(*args: str, timeout: float = 30.0, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``docker <args>`` and return the CompletedProcess.

    Thin wrapper that keeps the invocation style consistent across this
    module and lets one spot handle logging / default timeouts.
    """
    cmd = ["docker", *args]
    logger.debug("mcp_docker_deploy: %s", " ".join(shlex.quote(a) for a in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def _container_exists(name: str) -> bool:
    """True if a container with this exact name exists (running or not)."""
    try:
        r = _docker(
            "ps", "-a", "--format", "{{.Names}}",
            "--filter", f"name=^{_container_name(name)}$",
            check=False,
        )
        return _container_name(name) in (r.stdout or "").splitlines()
    except Exception:
        return False


def deploy_container(
    name: str,
    image: str,
    port: int,
    env_vars: Optional[Dict[str, str]] = None,
    mcp_path: str = "/mcp",
    host_port: Optional[int] = None,
) -> Dict[str, Any]:
    """Deploy an MCP server as a Docker container.

    Pulls the image if it isn't already present, removes any existing
    container of the same name (idempotent redeploy), then starts a
    fresh container with the requested env vars and a host-port
    mapping on 127.0.0.1 only.

    Args:
        name: MCP server name (used to derive the container name).
        image: Docker image ref, e.g. ``ghcr.io/foo/bar:latest``.
        port: Port the MCP server listens on inside the container.
        env_vars: Environment variables to inject. Secret and non-
            secret are treated identically by Docker — the caller is
            responsible for not logging values.
        mcp_path: URL path suffix the container serves MCP on (e.g.
            ``/mcp``). Included in the returned URL.
        host_port: Optional explicit host port; if omitted, a free one
            is picked from the reserved MCP range.

    Returns:
        {
            "url": "http://127.0.0.1:<host_port><mcp_path>",
            "container_id": "<short id>",
            "host_port": <int>,
            "status": "running" (or an error label on failure),
        }

    Raises:
        RuntimeError: if the container fails to start. Caller should
            log and surface to the user via the MCP server's DB row.
    """
    env_vars = env_vars or {}
    container = _container_name(name)

    # Remove any stale container with the same name so redeploy is
    # idempotent. --force handles both running and stopped.
    if _container_exists(name):
        logger.info("mcp_docker_deploy: removing stale container %s before redeploy", container)
        _docker("rm", "--force", container, check=False)

    # Pull the image. If pull fails (image missing from the registry,
    # registry auth, or the tag is ``:local`` for a locally-built
    # image), fall back to checking whether the image already exists
    # in the host Docker daemon — if so we proceed without a pull.
    # The verification ``:local`` flow depends on this: the echo test
    # image is built from docker/testing/mcp-echo/ and never pushed.
    try:
        _docker("pull", image, timeout=300.0)
    except subprocess.CalledProcessError as pull_err:
        try:
            _docker("image", "inspect", image, check=True, timeout=10.0)
            logger.info(
                "mcp_docker_deploy: pull failed for %s but image exists locally — using the local copy",
                image,
            )
        except subprocess.CalledProcessError:
            raise RuntimeError(
                f"docker pull {image} failed and no local image with that tag exists: "
                f"{pull_err.stderr.strip()[:200] if pull_err.stderr else pull_err}"
            )

    # Pick a host port if the caller didn't. _pick_free_port binds and
    # releases, so there's a tiny TOCTOU window before ``docker run``
    # claims it — acceptable for a dev tool, and docker run will fail
    # cleanly if the port is taken by the time it tries to bind.
    if host_port is None:
        host_port = _pick_free_port()

    run_args = [
        "run", "-d",
        "--name", container,
        "--restart", "unless-stopped",
        "--label", f"{_LABEL_KEY}=true",
        "--label", f"{_LABEL_NAME}={name}",
        # 127.0.0.1-bind keeps the port off the LAN. Agents reach the
        # container through the gateway's MCP proxy, not directly.
        "-p", f"127.0.0.1:{host_port}:{port}",
    ]
    for k, v in env_vars.items():
        run_args.extend(["-e", f"{k}={v}"])
    run_args.append(image)

    try:
        r = _docker(*run_args, timeout=60.0)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"docker run for {container} failed: {exc.stderr.strip()[:200] if exc.stderr else exc}"
        )

    container_id = (r.stdout or "").strip()[:12]

    url = f"http://127.0.0.1:{host_port}{mcp_path}"
    logger.info(
        "mcp_docker_deploy: deployed %s (image=%s port=%d->%d url=%s container=%s)",
        name, image, port, host_port, url, container_id,
    )

    return {
        "url": url,
        "container_id": container_id,
        "host_port": host_port,
        "status": "running",
    }


def undeploy_container(name: str) -> bool:
    """Stop and remove an MCP container. Idempotent.

    Returns True if a container was actually removed, False if it
    didn't exist. Never raises — errors are logged and swallowed so
    the caller's deletion path isn't blocked by a half-dead container.
    """
    container = _container_name(name)
    if not _container_exists(name):
        return False
    try:
        _docker("rm", "--force", container, timeout=30.0)
        logger.info("mcp_docker_deploy: removed container %s", container)
        return True
    except Exception as exc:
        logger.warning("mcp_docker_deploy: rm %s failed: %s", container, exc)
        return False


def restart_container(name: str) -> bool:
    """Restart an MCP container. Returns True on success."""
    container = _container_name(name)
    if not _container_exists(name):
        return False
    try:
        _docker("restart", container, timeout=30.0)
        return True
    except Exception as exc:
        logger.warning("mcp_docker_deploy: restart %s failed: %s", container, exc)
        return False


def container_status(name: str) -> str:
    """Return the container's docker status string ("running", "exited", etc.).

    Returns "missing" if the container doesn't exist. Never raises.
    """
    container = _container_name(name)
    try:
        r = _docker(
            "inspect", "-f", "{{.State.Status}}", container,
            check=False,
        )
        if r.returncode != 0:
            return "missing"
        return (r.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def container_image(name: str) -> str:
    """Return the image ref of an existing container, or '' if missing.

    Used by the reconfigure flow so we can redeploy a container whose
    catalogue entry we've lost track of (e.g. Docker Hub sources
    aren't in BUILTIN_CATALOGUE so catalogue lookup misses).
    """
    container = _container_name(name)
    try:
        r = _docker(
            "inspect", "-f", "{{.Config.Image}}", container,
            check=False,
        )
        if r.returncode != 0:
            return ""
        return (r.stdout or "").strip()
    except Exception:
        return ""


def container_restart_count(name: str) -> int:
    """Return the container's restart counter. 0 if missing/error.

    Used by the UI to flag crash-loops (``restarting`` state plus a
    non-trivial restart count) without shelling out a second time.
    """
    container = _container_name(name)
    try:
        r = _docker(
            "inspect", "-f", "{{.RestartCount}}", container,
            check=False,
        )
        if r.returncode != 0:
            return 0
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


def container_logs(name: str, tail: int = 200) -> str:
    """Return the last ``tail`` log lines from an MCP container."""
    container = _container_name(name)
    try:
        r = _docker(
            "logs", "--tail", str(tail), container,
            check=False, timeout=10.0,
        )
        return (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return f"(logs unavailable: {exc})"
