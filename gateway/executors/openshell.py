"""
OpenShellExecutor — runs Hermes agent instances as OpenShell sandboxes.

Integration model (Plan A, host-drives-sandbox — TASKS.md #24)
──────────────────────────────────────────────────────────────
1.  A sandbox image (``hermes-sandbox``) contains the Python worker
    (``/app/sandbox_worker.py``) and its dependencies (aiohttp for the
    inference.local HTTPS call, Python 3.12). The entrypoint is
    ``sleep infinity`` — the worker is NOT started at container boot.

2.  ``spawn()`` creates a named OpenShell sandbox with:
    - An uploaded instance config at ``/tmp/hermes/instance-config.json``
    - A network policy allowing access to inference.local (host gateway
      entry removed — no outbound traffic to the host anymore)
    - The sandbox just idles until the host calls ensure_worker

3.  ``spawn()`` then calls ``WorkerRegistry.ensure_worker`` which runs
    ``openshell sandbox exec --no-tty --name <sandbox> -- python3
    /app/sandbox_worker.py`` as a long-running ``asyncio`` subprocess
    on the gateway side. The subprocess's stdin/stdout form the
    bidirectional control channel over OpenShell's blessed gRPC/mTLS
    exec transport — no reverse WebSocket, no HTTP CONNECT tunnel, no
    custom proxy bypass. The old approach (sandbox opens a WebSocket
    back to ``/ws/worker`` through an HTTP CONNECT tunnel) was retired
    after OpenShell's L7 proxy tightening broke it post-upgrade.

4.  The worker's first line on stdout is
    ``{"type":"ready","worker_id":...}``. ``ensure_worker`` blocks on
    that line and then registers a ``WorkerEntry`` in the
    ``WorkerRegistry``. Chat dispatches are written as JSON lines to
    the subprocess's stdin; tokens/tool_progress/task_result stream
    back as JSON lines on stdout.

5.  ``delete_instance()`` destroys the sandbox — ``openshell sandbox
    delete`` tears down the in-pod process, which closes the gRPC
    exec stream, which causes our subprocess's stdin/stdout to EOF,
    which causes ``_read_stdout_loop`` to call ``_cleanup_worker``
    which drops the entry from the registry.

Prerequisites
─────────────
- Docker running.
- ``openshell`` CLI installed.
- The sandbox image built and imported (see docker/Dockerfile.hermes-sandbox).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from .base import InstanceConfig, ResourceHeadroom, SpawnedInstance

logger = logging.getLogger(__name__)

# Process-group registry of in-flight `openshell` CLI invocations. Each
# Popen runs with start_new_session=True so the CLI + its ssh-proxy
# subprocess become a fresh process group whose leader pid == proc.pid.
# We track those pgids so the gateway shutdown handler can kill any
# leaked groups in one shot via `os.killpg`. Without this, an openshell
# call that hung (or a SIGTERM that arrived mid-call) would leave the
# ssh-proxy children orphaned and reparented to init.
_active_procs: Set[int] = set()
_active_procs_lock = threading.Lock()

_HERMES_HOME = Path(
    os.getenv("LOGOS_HOME") or os.getenv("HERMES_HOME") or str(Path.home() / ".logos")
)
_STATE_FILE = _HERMES_HOME / "openshell_instances.json"
# Sibling lock file used to serialize prune-and-save cycles between
# concurrent spawn() and list_instances() callers. See _state_lock().
_STATE_LOCK_FILE = _HERMES_HOME / "openshell_instances.lock"

# Grace window during which a freshly-created or still-provisioning state
# entry is exempt from pruning even if `openshell sandbox list` doesn't
# yet show its CR. 90s matches the worker-registration deadline used by
# the http_api restart handler — long enough for an openshell create to
# finish on a cold cluster, short enough that genuinely-stuck records
# get cleaned up on the next list_instances() pass.
_PRUNE_GRACE_SECONDS = 90.0

# Default sandbox image source. Two modes:
#   1. A pre-built image tag (e.g. "ghcr.io/myorg/hermes-sandbox:1.0") — used
#      when the image is published to a registry the cluster can pull from.
#   2. An absolute path to a Dockerfile — OpenShell builds and imports it
#      into the gateway on demand. Used for dev/local and one-off setups.
#
# We default to mode #2 by computing the path to docker/Dockerfile.hermes-sandbox
# in the repo (../../docker/ relative to this file). Override either with the
# LOGOS_OPENSHELL_IMAGE env var.
_REPO_ROOT = Path(__file__).parent.parent.parent
_BUNDLED_DOCKERFILE = _REPO_ROOT / "docker" / "Dockerfile.hermes-sandbox"
_DEFAULT_IMAGE = os.getenv(
    "LOGOS_OPENSHELL_IMAGE",
    str(_BUNDLED_DOCKERFILE) if _BUNDLED_DOCKERFILE.exists() else "hermes-sandbox:local",
)

# Path to the default egress policy applied to every sandbox.
_DEFAULT_POLICY = Path(__file__).parent.parent / "policies" / "openshell_default.yaml"

# Gateway port — must match what Logos is listening on
_GATEWAY_PORT = int(
    os.getenv("LOGOS_PORT") or os.getenv("HERMES_PORT") or "8091"
)

# How long to wait for the worker to register after sandbox creation
_WORKER_REGISTER_TIMEOUT = 60


# ── State persistence ──────────────────────────────────────────────────────

def _load_state() -> List[dict]:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_state(instances: List[dict]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(instances, indent=2), encoding="utf-8")


@contextmanager
def _state_lock() -> Iterator[None]:
    """Exclusive ``fcntl.flock`` on ``_STATE_LOCK_FILE``.

    Serializes load → modify → save cycles in ``spawn()`` and
    ``list_instances()`` so two callers racing on the state file can't
    trash each other's writes (which is exactly how we ended up with
    state-file drift before this — list_instances() pruning a
    provisioning entry that spawn() had inserted seconds earlier).

    Linux-only (fcntl.flock). The critical sections are tiny — just
    the JSON load, the in-place mutation, and the JSON save — so a
    blocking exclusive lock is fine. Spawns and lists are infrequent
    relative to the lock duration.
    """
    _STATE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(_STATE_LOCK_FILE, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


# ── OpenShell CLI helpers ──────────────────────────────────────────────────

def _openshell(*args: str, gateway: Optional[str] = None,
               check: bool = True, capture: bool = True,
               timeout: float = 600.0) -> subprocess.CompletedProcess:
    """Run ``openshell <args>`` and return the CompletedProcess.

    When ``gateway`` is provided, the CLI is invoked with ``-g <gateway>``
    so the command operates on a specific OpenShell gateway. Required for
    multi-route routing — without scoping, ``openshell sandbox create``
    targets whatever gateway is currently selected by ``openshell gateway
    select``, which is global state we can't rely on.

    Two safety nets layered on top of plain ``subprocess.run``:

    * ``start_new_session=True`` — every invocation gets its own process
      group (pgid == pid because of setsid). The openshell CLI internally
      forks an ssh-proxy that talks to the OpenShell cluster; without
      grouping them, killing the CLI parent leaves the ssh-proxy children
      orphaned and reparented to init. With grouping we can ``killpg`` the
      whole tree.

    * ``timeout`` (default 600s = 10 min) — a hung openshell call would
      otherwise pin the calling thread forever (we used to spend whole
      sessions chasing wedged spawns). On timeout we ``killpg`` the
      group and raise ``subprocess.TimeoutExpired`` so callers see the
      failure explicitly. 600s is the upper bound for ``sandbox
      create`` against a freshly provisioned gateway that needs to
      build a Dockerfile image and bring up a new k3s pod; the
      original 120s default killed those legitimate cold-starts. Most
      operations against a warm gateway finish in 1-2s, so the higher
      ceiling only matters for the rare slow path.

    The pgid is added to the module-level ``_active_procs`` registry for
    the lifetime of the call so ``shutdown_openshell_children()`` can
    reap any survivors during gateway shutdown.
    """
    exe = shutil.which("openshell")
    if not exe:
        raise FileNotFoundError(
            "openshell CLI not found on PATH.  "
            "Install it: curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh"
        )
    cmd = [exe]
    if gateway:
        cmd.extend(["-g", gateway])
    cmd.extend(args)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        start_new_session=True,
    )
    pgid = proc.pid  # setsid → leader, pgid == pid
    with _active_procs_lock:
        _active_procs.add(pgid)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(
                "openshell call timed out after %.0fs — killing pgid %d: %s",
                timeout, pgid, " ".join(cmd[1:]),
            )
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = proc.communicate()
            raise
        retcode = proc.returncode
        if check and retcode != 0:
            raise subprocess.CalledProcessError(retcode, cmd, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(cmd, retcode, stdout, stderr)
    finally:
        with _active_procs_lock:
            _active_procs.discard(pgid)


def shutdown_openshell_children(grace_seconds: float = 0.5) -> None:
    """Kill any leaked openshell CLI process groups still running.

    Called from the gateway shutdown sequence (``run.py``) so no
    ssh-proxy children outlive the gateway. Best-effort: SIGTERM the
    whole group, wait briefly, then SIGKILL anything that didn't exit.
    Safe to call when the registry is empty.

    Notes / caveats:

    * Only handles graceful shutdowns (SIGTERM/SIGINT to the gateway).
      A hard SIGKILL of the gateway skips this entire path — for that
      case the only real fix is a systemd cgroup that kills all
      descendants when the leader dies, which is out of scope here.

    * Uses ``os.killpg`` rather than ``proc.terminate()`` because the
      openshell CLI forks an ssh-proxy in the same group; terminating
      the CLI parent alone leaves the ssh-proxy stranded.
    """
    with _active_procs_lock:
        pgids = list(_active_procs)
    if not pgids:
        return
    logger.info(
        "shutdown_openshell_children: SIGTERM %d in-flight openshell process group(s)",
        len(pgids),
    )
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            logger.warning("killpg(%d, SIGTERM) denied: %s", pgid, exc)
    if grace_seconds > 0:
        time.sleep(grace_seconds)
    for pgid in pgids:
        try:
            os.killpg(pgid, 0)  # probe — raises if already gone
        except ProcessLookupError:
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
            logger.warning("openshell pgid %d did not exit on SIGTERM, sent SIGKILL", pgid)
        except ProcessLookupError:
            continue
    with _active_procs_lock:
        _active_procs.clear()


def reap_orphan_openshell_processes() -> int:
    """SIGTERM any openshell-related processes left over from a prior gateway run.

    Called at gateway startup BEFORE the WorkerRegistry comes up so stale
    workers from a SIGKILL'd or crashed prior gateway can't re-register
    with their old (stale) sandbox names. That was the day-long
    "Hermes thinks it's Ani" investigation: a previous gateway died
    ungracefully, its openshell CLI children + ssh-proxy subprocesses
    were reparented to init, and when the new gateway came up the
    orphaned workers reconnected with their old worker_ids and the
    new gateway routed chats to the wrong agent.

    ``shutdown_openshell_children()`` (task #10) handles the GRACEFUL
    shutdown path. This function is its safety net for the SIGKILL /
    crash / power-loss path that ``shutdown_openshell_children`` can
    never run for.

    Detection rule: PPID == 1 (reparented to init — the classic signature
    of an orphan whose original parent died) AND ``"openshell"`` appears
    anywhere in the command line. The PPID==1 filter is what makes this
    safe to call at every startup: a user running ``openshell sandbox
    list`` in another terminal is still parented to a shell (PPID > 1)
    and won't be touched.

    Returns the number of process groups SIGTERM'd. Best-effort: errors
    are logged and swallowed; this function never raises so a startup
    failure here can't block the gateway from coming up.
    """
    import subprocess as _sp

    try:
        result = _sp.run(
            ["ps", "-eo", "pid,pgid,ppid,cmd"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        logger.warning("reap_orphan_openshell: ps failed: %s", exc)
        return 0
    if result.returncode != 0:
        logger.warning(
            "reap_orphan_openshell: ps exited %d: %s",
            result.returncode, (result.stderr or "")[:200],
        )
        return 0

    own_pid = os.getpid()
    killed_pgids: set = set()

    for line in result.stdout.splitlines()[1:]:  # skip header
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
            ppid = int(parts[2])
        except ValueError:
            continue
        cmd = parts[3]

        # Only kill orphans (parent died, reparented to init).
        if ppid != 1:
            continue
        if pid == own_pid:
            continue
        if "openshell" not in cmd.lower():
            continue
        # Don't double-kill processes whose group we already SIGTERM'd.
        if pgid in killed_pgids:
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
            logger.info(
                "reap_orphan_openshell: SIGTERM pgid=%d (pid=%d): %s",
                pgid, pid, cmd[:120],
            )
            killed_pgids.add(pgid)
        except (ProcessLookupError, PermissionError) as exc:
            logger.debug(
                "reap_orphan_openshell: killpg(%d, SIGTERM) failed: %s",
                pgid, exc,
            )

    if killed_pgids:
        # Brief grace period before SIGKILL fallback. Same pattern as
        # shutdown_openshell_children — give the children a chance to
        # exit cleanly on SIGTERM before forcing them.
        time.sleep(0.5)
        for pgid in list(killed_pgids):
            try:
                os.killpg(pgid, 0)  # probe — raises if already gone
            except ProcessLookupError:
                continue
            try:
                os.killpg(pgid, signal.SIGKILL)
                logger.warning(
                    "reap_orphan_openshell: SIGKILL pgid=%d (didn't exit on SIGTERM)",
                    pgid,
                )
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                logger.warning(
                    "reap_orphan_openshell: killpg(%d, SIGKILL) denied: %s",
                    pgid, exc,
                )
        logger.info(
            "reap_orphan_openshell: reaped %d orphan process group(s) from a "
            "prior gateway run", len(killed_pgids),
        )

    return len(killed_pgids)


def _sanitize_sandbox_name(name: str) -> str:
    """
    Coerce ``name`` into a valid RFC 1123 subdomain so the underlying
    Kubernetes Sandbox CR accepts it. Lowercase, replace any character
    that isn't [a-z0-9.-] with '-', collapse runs of '-', strip leading
    and trailing non-alphanumerics, and truncate to 63 characters.
    """
    import re
    s = name.lower()
    s = re.sub(r"[^a-z0-9.-]+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-.")
    if not s:
        s = "agent"
    if not s[0].isalnum():
        s = "a" + s
    if len(s) > 63:
        s = s[:63].rstrip("-.")
    return s


def _query_sandbox_names(gateway: Optional[str] = None) -> Optional[Set[str]]:
    """Return the set of live sandbox names in ``gateway``, or ``None`` if
    the query itself failed.

    The ``None`` return value is the load-bearing distinction that #11
    fixes: previously every helper here returned ``False`` / ``[]`` on
    any exception, conflating "the gateway is reachable and there are no
    matching sandboxes" with "we couldn't even ask". The state-file
    pruner then deleted entries from a gateway that was momentarily
    unreachable, losing the ``openshell_name`` mapping forever.

    Three outcomes:
      * ``set[str]`` — the CLI ran cleanly; this is the authoritative
        live set for that gateway. Empty set means the gateway is up
        but contains no sandboxes.
      * ``None`` — the CLI failed (timeout, missing binary, OS error,
        non-zero exit). Callers must NOT treat this as "no sandboxes";
        they should keep any state entries belonging to this gateway
        rather than risk a destructive prune on transient errors.
    """
    try:
        result = _openshell(
            "sandbox", "list", "--names",
            gateway=gateway, check=False, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("query gateway %r failed: %s", gateway, exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "query gateway %r exited %d: %s",
            gateway, result.returncode, (result.stderr or "").strip()[:200],
        )
        return None
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def _list_sandbox_names(gateway: Optional[str] = None) -> List[str]:
    """Back-compat shim: return live sandbox names in ``gateway`` or ``[]``.

    Used by ``_list_all_sandbox_names_with_gateway()`` which only needs
    "what's alive right now" for the resurrection pass — it doesn't make
    destructive decisions, so the unknown-vs-empty distinction doesn't
    matter. Internally delegates to ``_query_sandbox_names()`` and flattens
    ``None`` to ``[]``.
    """
    names = _query_sandbox_names(gateway=gateway)
    return sorted(names) if names is not None else []


def _build_sandbox_index(gateways: Set[str]) -> Dict[str, Optional[Set[str]]]:
    """Query every distinct gateway exactly once and return a snapshot.

    Returns ``{gateway: set | None}`` — set means "queried successfully,
    here are the live names", None means "query failed, treat as
    unknown". Used by ``_prune_state_against_index()`` so the pruner
    can make per-gateway decisions instead of calling the CLI once per
    state entry (which is both slow AND prone to per-entry conflation
    of missing-vs-unreachable).
    """
    return {gw: _query_sandbox_names(gateway=gw) for gw in gateways}


def _is_within_grace_period(record: dict, grace: float = _PRUNE_GRACE_SECONDS) -> bool:
    """Whether ``record`` is exempt from pruning due to its age/phase.

    Returns True if either:
      * ``record["phase"] == "provisioning"`` — spawn() is mid-flight,
        the openshell CR may not be visible yet.
      * ``record["created_at"]`` is within ``grace`` seconds — covers
        the race where spawn() inserted the record but
        ``openshell sandbox list`` hasn't picked it up yet, AND covers
        legacy entries that never got their phase flipped to "ready".
    """
    if record.get("phase") == "provisioning":
        return True
    created = record.get("created_at")
    if isinstance(created, (int, float)) and (time.time() - created) < grace:
        return True
    return False


def _prune_state_against_index(
    instances: List[dict],
    index: Dict[str, Optional[Set[str]]],
    primordial_name: str,
) -> Tuple[List[dict], int]:
    """Apply the prune rules. Returns ``(kept_entries, num_pruned)``.

    Rules, in order:
      1. Grace-period entries are kept regardless (phase=="provisioning"
         or created within ``_PRUNE_GRACE_SECONDS``).
      2. If the entry's gateway has a None index entry (query failed,
         or the gateway wasn't queried at all), the entry is kept —
         we don't have authoritative info to drop it.
      3. Otherwise the entry is dropped iff its sandbox_name is not in
         the index set for its gateway.

    The point of returning the prune count is so the caller can decide
    whether to bother saving the state file at all — saving on a
    no-op prune is a waste of disk I/O and risks racing concurrent
    writers (which is partly why we now hold ``_state_lock()`` around
    the whole load-modify-save cycle).
    """
    kept: List[dict] = []
    pruned = 0
    for inst in instances:
        if _is_within_grace_period(inst):
            kept.append(inst)
            continue
        gw = inst.get("openshell_name") or primordial_name
        live = index.get(gw)
        if live is None:
            # Either the query failed or this gateway wasn't in the
            # batch we were asked to query. Either way: don't make a
            # destructive decision on missing data.
            kept.append(inst)
            continue
        if inst.get("sandbox_name", "") in live:
            kept.append(inst)
        else:
            pruned += 1
            logger.info(
                "prune state entry name=%r sandbox=%r gateway=%r — sandbox confirmed missing",
                inst.get("name"), inst.get("sandbox_name"), gw,
            )
    return kept, pruned


def _list_all_sandbox_names_with_gateway() -> List[tuple[str, str]]:
    """Enumerate every live sandbox across every known OpenShell gateway.

    Returns ``[(sandbox_name, gateway_name), ...]``. Iterates over the
    model_routes table so each provisioned gateway is queried exactly
    once. Falls back to the bootstrap gateway when the routes table is
    empty (the path before /setup populates routes).

    Used by orphan-prune and missing-sandbox-resurrect passes that need
    a global view of "what sandboxes exist anywhere" before they can
    decide which ones to clean up or recreate.
    """
    from gateway.openshell_routes import BOOTSTRAP_PRIMORDIAL_NAME
    from gateway.auth import db as auth_db

    gateways_to_query: set[str] = set()
    try:
        for r in auth_db.list_model_routes():
            name = r.get("openshell_name")
            if name:
                gateways_to_query.add(name)
    except Exception as exc:
        logger.warning("could not enumerate model_routes: %s", exc)
    if not gateways_to_query:
        gateways_to_query.add(BOOTSTRAP_PRIMORDIAL_NAME)

    out: List[tuple[str, str]] = []
    for gw in gateways_to_query:
        for name in _list_sandbox_names(gateway=gw):
            out.append((name, gw))
    return out


# ── Route resolution ───────────────────────────────────────────────────────

def _resolve_route(config: "InstanceConfig") -> tuple[str, str]:
    """Resolve (openshell_gateway_name, effective_model) for a spawn.

    Returns the OpenShell gateway the sandbox should land inside and the
    model name baked into ``/tmp/hermes/instance-config.json`` for the
    sandbox worker. Resolution order:

      1. ``config.model_route_id`` is set → look up the model_routes row,
         use its (openshell_name, model). The route's model takes priority
         over ``config.model`` because the OpenShell gateway is forcing
         that exact model anyway — sending a different value in the
         worker's chat-completions call would be ignored by OpenShell's
         privacy router.

      2. ``model_routes.is_default = 1`` row exists → use it. This is the
         path agents created via /admin/agents take when the user picks
         "Auto (use default)" instead of an explicit route.

      3. Fall through to the legacy path: bootstrap OpenShell gateway
         (default name ``logos-openshell``) with the env-resolved model.
         This handles the case where model_routes is empty (the user
         hasn't run /setup yet under the new architecture, or DB was
         just migrated and routes haven't been populated).
    """
    from gateway.openshell_routes import get_primordial_name
    from gateway.auth import db as auth_db

    # 1. Explicit binding
    if getattr(config, "model_route_id", None):
        route = auth_db.get_model_route(config.model_route_id)
        if route:
            return route["openshell_name"], route["model"]
        logger.warning(
            "spawn: agent has model_route_id=%r but row not found — falling back",
            config.model_route_id,
        )

    # 2. Default route
    try:
        default = auth_db.get_default_model_route()
        if default:
            return default["openshell_name"], default["model"]
    except Exception as exc:
        logger.warning("spawn: get_default_model_route failed: %s", exc)

    # 3. Bootstrap fallback — primordial gateway with env/config-resolved model
    resolved_model = (config.model or "").strip()
    if not resolved_model:
        resolved_model = (
            os.environ.get("LOGOS_MODEL")
            or os.environ.get("HERMES_MODEL")
            or os.environ.get("LLM_MODEL")
            or ""
        ).strip()
    if resolved_model and not getattr(config, "model_route_id", None):
        logger.info(
            "spawn(%s): no model_routes binding — using primordial gateway with model=%r",
            config.name, resolved_model,
        )
    return get_primordial_name(), resolved_model


# ── Executor ──────────────────────────────────────────────────────────────

class OpenShellExecutor:
    """
    Manages Hermes agent instances as OpenShell sandboxes.

    Each sandbox runs a WebSocket worker that connects back to the Logos
    gateway.  No SSH tunnels or port forwarding needed.
    """

    def __init__(
        self,
        sandbox_image: str = _DEFAULT_IMAGE,
        policy_file: Optional[str] = None,
    ):
        self.sandbox_image = sandbox_image
        self.policy_file = policy_file or (str(_DEFAULT_POLICY) if _DEFAULT_POLICY.exists() else None)

    def spawn(self, config: InstanceConfig) -> SpawnedInstance:
        from gateway.openshell_routes import get_primordial_name

        # OpenShell sandboxes are backed by Kubernetes Sandbox CRs, so the
        # sandbox name must be a valid RFC 1123 subdomain: lowercase
        # [a-z0-9.-], must start/end with alphanumeric, max 63 chars.
        sandbox_name = _sanitize_sandbox_name(f"hermes-{config.name}")
        worker_id = sandbox_name  # worker registers with this ID

        # Resolve which OpenShell gateway this sandbox should land inside,
        # and what model the worker should request from inference.local.
        # See _resolve_route() for the lookup order.
        openshell_gw, resolved_model = _resolve_route(config)

        logger.info(
            "Creating OpenShell sandbox '%s' in gateway '%s' (model=%s) from image '%s'",
            sandbox_name, openshell_gw, resolved_model or "<none>", self.sandbox_image,
        )

        # Pre-flight provider sync — re-push the credential + URL from
        # auth.db.machines to the target sub-gateway's provider record so
        # the worker's chat completion call (which goes through OpenShell's
        # privacy router with the stored credential) sees the same value
        # as ``ensure_loaded`` does (which reads auth.db.machines directly).
        # Without this, the two paths can drift after a key rotation:
        # ensure_loaded keeps working but the worker's chat call gets
        # rejected with stale auth. See
        # ``openshell_routes.ensure_provider_configured`` for details.
        try:
            from gateway.openshell_routes import ensure_provider_configured
            from gateway.auth import db as _auth_db
            provider_name = "lmstudio"
            if getattr(config, "model_route_id", None):
                _route = _auth_db.get_model_route(config.model_route_id)
                if _route:
                    provider_name = _route.get("provider") or provider_name
            ensure_provider_configured(openshell_gw, provider_name)
        except Exception as exc:
            logger.warning(
                "Pre-spawn provider sync raised (continuing anyway): %s", exc
            )

        # Write instance config to a temp file for upload
        instance_config = {
            "worker_id": worker_id,
            "instance_name": config.name,
            "gateway_url": f"http://host.openshell.internal:{_GATEWAY_PORT}",
            "soul": config.soul_name or "general",
            "toolsets": config.toolsets or [],
            "model": resolved_model,
        }

        record = {
            "name": config.name,
            "sandbox_name": sandbox_name,
            "worker_id": worker_id,
            "source": "openshell",
            "soul_name": config.soul_name,
            "model": resolved_model,
            "openshell_name": openshell_gw,
            "model_route_id": getattr(config, "model_route_id", None),
            "requester": config.requester,
            "toolsets": config.toolsets or [],
            "policy": config.policy or "",
            "sandbox_image": self.sandbox_image,
            "created_at": time.time(),
            "phase": "provisioning",
        }

        # Phase 1 (under lock): prune dead entries and insert the new
        # provisioning record. Same prune rules as list_instances() —
        # batch query per gateway, keep grace-period entries, never
        # drop entries from gateways whose query failed. The new
        # record gets a phase="provisioning" tag so concurrent
        # list_instances() calls won't clobber it before openshell
        # finishes creating the CR.
        with _state_lock():
            instances = _load_state()
            primordial = get_primordial_name()
            gateways = {
                (i.get("openshell_name") or primordial) for i in instances
            }
            gateways.add(openshell_gw)
            index = _build_sandbox_index(gateways)
            instances, _pruned = _prune_state_against_index(
                instances, index, primordial
            )
            instances.append(record)
            _save_state(instances)

        # ── Spawn flow: 3 separate openshell CLI calls ─────────────
        # The previous bundled-create flow was:
        #
        #   openshell sandbox create --upload <local>:<dest> -- /app/entrypoint.sh
        #
        # which has a fatal architectural flaw: `openshell sandbox
        # create` blocks for the LIFETIME of the trailing command. The
        # trailing command here is /app/entrypoint.sh which exec's into
        # the worker (a long-running WebSocket loop). So the create CLI
        # never returns. subprocess.run(check=True) blocks forever.
        # Even with the to_thread wrap from commit 3ea6d6e the gateway
        # leaks one worker thread + one openshell child process per
        # spawn — and the chat M dropdown's restart-handler response
        # never lands, so the frontend's "switching..." badge hangs
        # forever even though the worker DID actually register.
        #
        # The fix is to split into three independent CLI invocations,
        # each of which actually returns:
        #
        #   1. openshell sandbox create
        #        — no --upload, no trailing command
        #        — returns when the CR is provisioned (~5-15s)
        #
        #   2. openshell sandbox upload
        #        — uploads instance-config.json (and optional SOUL.md)
        #        — returns when the upload completes (~1-2s)
        #
        #   3. openshell sandbox exec -- bash -c '<bg launch>'
        #        — runs `nohup /app/entrypoint.sh & disown` so the
        #          entrypoint is detached from the SSH session
        #        — bash exits immediately, the SSH tunnel closes,
        #          the entrypoint keeps running because it was
        #          disowned. The worker registers with the gateway
        #          shortly after via WebSocket.
        #        — returns immediately (~1s)
        #
        # Total spawn time: ~10-20s. The function actually returns.
        # The caller (e.g. _handle_sandbox_restart) is responsible
        # for waiting for the worker to register via the gateway's
        # worker_registry — see the wait_for_worker helper there.
        config_tmpfile = None
        try:
            # ── Step 1: create the sandbox CR ──────────────────────
            create_args = [
                "sandbox", "create",
                "--name", sandbox_name,
                "--from", self.sandbox_image,
                "--no-auto-providers",
            ]
            if self.policy_file and Path(self.policy_file).exists():
                create_args += ["--policy", self.policy_file]
            # CRITICAL: trailing `-- true` is required.
            #
            # `openshell sandbox create` with NO trailing command (after
            # `--`) defaults to opening an interactive PTY shell once
            # the CR is ready, and the create call blocks for the
            # lifetime of that shell. Without a trailing command we get
            # `ssh -tt -o RequestTTY=force` zombies that never exit and
            # the create call hangs until the timeout reaper kills it
            # — exactly the symptom that motivated task #9, just on a
            # different code path.
            #
            # Passing `-- true` runs `/usr/bin/true` inside the sandbox
            # which exits in milliseconds. Without `--no-keep` the
            # sandbox stays alive after the initial command exits, so
            # we can run the upload + worker exec steps below as
            # independent calls. NB: do NOT add `--no-keep` here, that
            # would tear the sandbox down when `true` returns.
            create_args += ["--", "true"]
            result = _openshell(*create_args, gateway=openshell_gw, check=True)
            logger.debug("openshell sandbox create stdout: %s", result.stdout.strip())

            # ── Step 2: upload the instance config (and soul) ──────
            config_tmpfile = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="hermes-config-", delete=False,
            )
            json.dump(instance_config, config_tmpfile)
            config_tmpfile.close()
            _openshell(
                "sandbox", "upload", sandbox_name,
                config_tmpfile.name, "/tmp/hermes/instance-config.json",
                gateway=openshell_gw, check=True,
            )

            if config.soul_name and config.soul_name != "default":
                soul_dir = _HERMES_HOME / "souls"
                soul_file = soul_dir / f"{config.soul_name}.md"
                if soul_file.exists():
                    _openshell(
                        "sandbox", "upload", sandbox_name,
                        str(soul_file), "/tmp/hermes/SOUL.md",
                        gateway=openshell_gw, check=True,
                    )

            # ── Step 3: launch the worker via WorkerRegistry.ensure_worker ──
            #
            # Plan A (TASKS.md #24): the sandbox container no longer
            # auto-launches the worker. Instead, the host gateway
            # manages the worker's lifetime by spawning `openshell
            # sandbox exec --no-tty --name <sandbox> -- python3
            # /app/sandbox_worker.py` as a long-running asyncio
            # subprocess. Stdin/stdout of that subprocess become the
            # control channel.
            #
            # We're inside asyncio.to_thread here (executor.spawn is a
            # sync method called via thread pool), so the main event
            # loop is running in a different thread. Use
            # asyncio.run_coroutine_threadsafe to schedule ensure_worker
            # on the main loop and block this thread on the result.
            #
            # Read the runner/loop via ``gateway.runtime_state`` — NOT
            # via ``gateway.run``. ``gateway/run.py`` is started with
            # ``python -m gateway.run`` which loads it as ``__main__``,
            # and a subsequent ``import gateway.run`` from this module
            # loads the same file *again* as a second module object with
            # its own independent globals. Assignments inside ``main()``
            # mutate the ``__main__`` module's globals, so the copy
            # imported here keeps seeing ``None``. ``runtime_state`` is
            # a standalone module (never run as ``__main__``) so every
            # importer sees the same object — single source of truth.
            try:
                from gateway import runtime_state as _runtime_state
                from gateway.worker_registry import WORKER_READY_TIMEOUT
                _current_runner = _runtime_state.current_runner
                _current_loop = _runtime_state.current_loop
            except Exception:
                _current_runner = None
                _current_loop = None
                WORKER_READY_TIMEOUT = 60.0

            if _current_runner and _current_loop and not _current_loop.is_closed():
                try:
                    ensure_future = asyncio.run_coroutine_threadsafe(
                        _current_runner.worker_registry.ensure_worker(
                            sandbox_name,
                            soul=config.soul_name or "general",
                            toolsets=config.toolsets or [],
                            instance_label=config.name,
                            requester=config.requester or "",
                            env={
                                "OPENAI_BASE_URL": os.environ.get(
                                    "OPENAI_BASE_URL",
                                    "https://inference.local/v1",
                                ),
                                "OPENAI_API_KEY": os.environ.get(
                                    "OPENAI_API_KEY", "unused"
                                ),
                                "HERMES_MODEL": resolved_model or "",
                                "HERMES_WORKER_ID": sandbox_name,
                            },
                        ),
                        _current_loop,
                    )
                    # Budget: WORKER_READY_TIMEOUT + 5s slack for the
                    # subprocess spawn itself (openshell CLI startup,
                    # gRPC auth, exec dispatch).
                    ensure_future.result(timeout=WORKER_READY_TIMEOUT + 5)
                except ConnectionError as exc:
                    # RAISE — don't swallow. /setup/complete blocks on
                    # spawn so it can surface this to the user. The
                    # sandbox CR is already created at this point, so
                    # the caller should delete it before retrying.
                    logger.error(
                        "ensure_worker failed for %s: %s", sandbox_name, exc,
                    )
                    raise RuntimeError(
                        f"Sandbox '{sandbox_name}' was created but the "
                        f"Plan A worker subprocess failed to come up: {exc}"
                    ) from exc
                except Exception as exc:
                    logger.exception(
                        "ensure_worker raised unexpectedly for %s: %s",
                        sandbox_name, exc,
                    )
                    raise
            else:
                # No runner bound to the module globals. In production
                # this should never happen (run.py sets them before the
                # HTTP API can accept requests), so treat it as a hard
                # error rather than silently skipping. The old behaviour
                # was to log a warning and return — which let /setup
                # show "done" while the sandbox sat there with no worker.
                raise RuntimeError(
                    f"ensure_worker cannot launch for '{sandbox_name}': "
                    f"no current GatewayRunner bound on the main loop. "
                    f"This usually means spawn() was called before "
                    f"gateway.run.main() set _current_runner, or after "
                    f"the event loop was closed during shutdown."
                )

            # Phase 3 (under lock): flip the record's phase to "ready"
            # so subsequent list_instances() prunes apply normal rules.
            # Re-load the state file because something else may have
            # mutated it while we were running the openshell CLI calls.
            with _state_lock():
                cur = _load_state()
                for i in cur:
                    if i.get("sandbox_name") == sandbox_name:
                        i["phase"] = "ready"
                        break
                else:
                    # Our record vanished mid-spawn (e.g. user deleted
                    # the agent, or another spawn ran a stale prune
                    # on a gateway that had just temporarily failed).
                    # Re-insert with phase=ready so the worker is
                    # discoverable.
                    record["phase"] = "ready"
                    cur.append(record)
                _save_state(cur)

        except subprocess.CalledProcessError as exc:
            # Roll back the state record on failure. Best-effort
            # cleanup of any partial sandbox CR — we may have
            # successfully created the CR but failed at upload or
            # exec, leaving a half-baked sandbox like the ones we
            # were chasing all session before this refactor landed.
            with _state_lock():
                cur = _load_state()
                cur = [i for i in cur if i.get("sandbox_name") != sandbox_name]
                _save_state(cur)
            try:
                _openshell(
                    "sandbox", "delete", sandbox_name,
                    gateway=openshell_gw, check=False,
                )
            except Exception:
                pass
            raise RuntimeError(
                f"Failed to create OpenShell sandbox '{sandbox_name}' in gateway "
                f"'{openshell_gw}': {exc.stderr or exc.stdout or str(exc)}"
            ) from exc
        finally:
            if config_tmpfile:
                try:
                    os.unlink(config_tmpfile.name)
                except OSError:
                    pass

        return SpawnedInstance(
            name=config.name,
            url="",  # no direct URL — routed through gateway via worker_id
            port=0,
            source="openshell",
            soul_name=config.soul_name,
            model=resolved_model,
            requester=config.requester,
            healthy=False,  # caller polls worker_registry to confirm registration
        )

    def list_instances(self) -> List[dict]:
        from gateway.openshell_routes import get_primordial_name

        # Hardened pruning: batch one CLI query per gateway, keep
        # grace-period entries, never drop entries from a gateway
        # whose query failed. See _prune_state_against_index() for
        # the full rule list.
        with _state_lock():
            instances = _load_state()
            primordial = get_primordial_name()
            gateways = {
                (i.get("openshell_name") or primordial) for i in instances
            }
            index = _build_sandbox_index(gateways)
            kept, pruned = _prune_state_against_index(instances, index, primordial)
            if pruned > 0:
                _save_state(kept)
        return kept

    def delete_instance(self, name: str) -> None:
        # Resolve the sandbox name authoritatively from the agent name
        # — do NOT rely on the local state file for the SANDBOX NAME. The
        # state file goes out of sync whenever spawn() races with
        # list_instances() (the latter prunes entries whose CR isn't
        # visible yet), which left orphan OpenShell sandboxes for deleted
        # agents. Always issue the delete; clean up any matching state
        # entries afterwards.
        #
        # However, we DO need the state file (or, failing that, the
        # primordial fallback) to find which gateway the sandbox lives
        # inside — `openshell sandbox delete <name>` without `-g` only
        # checks the CLI's currently-selected gateway and silently
        # succeeds if the sandbox isn't there. Best-effort lookup: scan
        # the state file for an entry matching by name OR sandbox_name,
        # use its openshell_name; otherwise default to the primordial.
        from gateway.openshell_routes import get_primordial_name

        sandbox_name = _sanitize_sandbox_name(f"hermes-{name}")
        primordial = get_primordial_name()
        target_gw = primordial
        for inst in _load_state():
            if inst.get("name") == name or inst.get("sandbox_name") == sandbox_name:
                target_gw = inst.get("openshell_name") or primordial
                break

        try:
            _openshell("sandbox", "delete", sandbox_name,
                        gateway=target_gw, check=False)
            logger.info(
                "Deleted OpenShell sandbox '%s' from gateway '%s'",
                sandbox_name, target_gw,
            )
        except FileNotFoundError:
            logger.warning("Cannot delete sandbox '%s' — openshell CLI not on PATH", sandbox_name)
        except Exception as exc:
            logger.warning("Error deleting sandbox '%s': %s", sandbox_name, exc)

        # Drop any matching state record so the dashboard stops showing it.
        # Held under the state lock so a concurrent spawn() can't re-insert
        # while we're partway through the filter.
        with _state_lock():
            instances = _load_state()
            remaining = [
                i for i in instances
                if i.get("name") != name and i.get("sandbox_name") != sandbox_name
            ]
            if len(remaining) != len(instances):
                _save_state(remaining)

    def get_headroom(self) -> ResourceHeadroom:
        """Estimate available resources for spawning more sandboxes."""
        try:
            import psutil
            cpu_free = psutil.cpu_count(logical=True) * (1 - psutil.cpu_percent(interval=0.1) / 100)
            mem_free_gb = psutil.virtual_memory().available / 1024**3
            can_spawn = cpu_free >= 1.0 and mem_free_gb >= 1.0
            return ResourceHeadroom(
                available_cpu=cpu_free,
                available_mem_gb=mem_free_gb,
                can_spawn=can_spawn,
                reason="" if can_spawn else "Low host resources",
            )
        except Exception:
            return ResourceHeadroom(can_spawn=True)

    def get_resources(self) -> dict:
        headroom = self.get_headroom()
        return {
            "free_cpu": round(headroom.available_cpu, 2),
            "free_mem": int(headroom.available_mem_gb * 1024**3),
            "can_spawn": headroom.can_spawn,
            "reason": headroom.reason,
            "executor": "openshell",
        }
