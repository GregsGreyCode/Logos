"""
OpenShellExecutor — runs Hermes agent instances as OpenShell sandboxes.

Integration model (Plan A-prime, per-task exec — TASKS.md #24)
──────────────────────────────────────────────────────────────
1.  A sandbox image (``hermes-sandbox``) contains the Python worker
    (``/app/sandbox_worker.py``) and its dependencies (aiohttp for the
    inference.local HTTPS call, Python 3.12). The entrypoint is
    ``sleep infinity`` — the sandbox pod stays alive indefinitely as a
    passive execution environment.

2.  ``spawn()`` creates a named OpenShell sandbox with:
    - An uploaded instance config at ``/tmp/hermes/instance-config.json``
    - A network policy allowing access to inference.local
    - An uploaded SOUL.md at ``/tmp/hermes/SOUL.md`` (optional)
    Then it marks the state-file record ``phase=ready`` and returns.
    **No persistent worker is launched** — each chat dispatch spawns
    its own subprocess via ``WorkerRegistry.dispatch_task``.

3.  ``WorkerRegistry.dispatch_task`` spawns a fresh ``openshell sandbox
    exec --no-tty --name <sandbox> -- python3 /app/sandbox_worker.py``
    subprocess for every task. It pipes the task JSON to the
    subprocess's stdin, closes stdin (the EOF is what unblocks
    openshell's exec gate — without it the in-sandbox process never
    starts, proven directly), streams token/thinking/task_result
    frames from stdout, and waits for the subprocess to exit.

4.  Cold-start tax per dispatch: ~0.2s for python + aiohttp import +
    config load. Negligible compared to 2–30s inference calls. No
    persistent worker registry, no stdin protocol loop, no ready
    handshake, no ``ensure_worker`` bridging between event loop and
    thread pool.

5.  ``delete_instance()`` destroys the sandbox — ``openshell sandbox
    delete`` tears down the pod. Any in-flight dispatch_task
    subprocess gets its exec stream killed and raises.

History: earlier versions of Plan A kept a persistent stdin/stdout
loop per sandbox, expecting the subprocess to live for the whole
session. That was impossible on ``openshell sandbox exec``: the exec
primitive refuses to invoke the in-sandbox command until stdin
reaches EOF, so a persistent worker sat blocked on gRPC forever.
Per-task exec matches the transport's actual contract.

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
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

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

# Default sandbox image. M11: references a pre-built image tag that exists
# in the local Docker daemon (built from docker/Dockerfile.hermes-upstream).
# The spawn flow ensures the image is imported into the target OpenShell
# cluster's containerd before creating the sandbox (see _ensure_image_in_cluster).
# Override with the LOGOS_OPENSHELL_IMAGE env var.
_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_IMAGE = os.getenv("LOGOS_OPENSHELL_IMAGE", "hermes-sandbox:m12")

# Logos agent image registry. When set, the spawn flow will `docker pull`
# from this registry before importing into the cluster. This ensures images
# persist across containerd garbage collection cycles. Users will eventually
# browse and install agent images from a UI backed by this registry.
_REGISTRY_URL = os.getenv("LOGOS_REGISTRY_URL", "localhost:5000")

# Path to the default egress policy applied to every sandbox.
_DEFAULT_POLICY = Path(__file__).parent.parent / "policies" / "openshell_default.yaml"

# ── State persistence ──────────────────────────────────────────────────────

def _load_state() -> List[dict]:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_state(instances: List[dict]) -> None:
    """Write the executor state file, deduplicated by ``sandbox_name``.

    Two independent heal paths — ``_resurrect_missing_sandboxes`` and
    ``sandbox_heal`` auto-respawn — can each append a record for the
    same sandbox if they run within the same window. The downstream
    ``_SandboxHealthEntry`` pipeline builds a dict keyed on
    ``worker_id``, and the LAST entry in iteration order wins — which
    is usually the newer, ``phase=provisioning`` one, even though the
    older entry has already flipped to ``phase=ready``. Net effect:
    the UI shows "provisioning" indefinitely while the sandbox is
    actually healthy and responding to chat.
    Dedup at the write boundary so every persisted state has at most
    one entry per sandbox, preferring (in order) the entry that is
    already ``phase=ready``, then the most recently-created. Callers
    that append without checking remain correct; the last write wins.
    """
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    by_name: Dict[str, dict] = {}
    for inst in instances:
        name = inst.get("sandbox_name") or ""
        if not name:
            # Entries without a sandbox_name are malformed but we
            # don't want to drop them silently — write them through
            # with a synthetic key so _load_state still sees them.
            by_name[f"__nameless_{id(inst)}"] = inst
            continue
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = inst
            continue
        # Prefer ready over provisioning, then newer over older.
        ep = existing.get("phase")
        ip = inst.get("phase")
        if ep == "ready" and ip != "ready":
            continue  # keep existing
        if ip == "ready" and ep != "ready":
            by_name[name] = inst
            continue
        try:
            e_ts = float(existing.get("created_at") or 0)
        except (TypeError, ValueError):
            e_ts = 0.0
        try:
            i_ts = float(inst.get("created_at") or 0)
        except (TypeError, ValueError):
            i_ts = 0.0
        if i_ts >= e_ts:
            by_name[name] = inst
    _STATE_FILE.write_text(json.dumps(list(by_name.values()), indent=2), encoding="utf-8")


def _hermes_server_setup_dict(setup) -> dict:
    """Serialise a ``HermesServerSetup`` dataclass for the state file."""
    return {
        "api_key": setup.api_key,
        "base_url": setup.base_url,
        "hermes_home": setup.hermes_home,
    }


def persist_hermes_server_setup(sandbox_name: str, setup) -> bool:
    """Write ``hermes_server_setup`` into the state record for ``sandbox_name``.

    LOG-61 helper: the spawn path's inline write (see ``spawn()``) and
    the resurrect path's reconcile branch both need the same "overwrite
    the setup field under a state lock" operation. Factored out so both
    call sites produce identical state — without this, resurrected or
    reconciled agents silently fall back to v1 dispatch because
    ``_load_server_setup`` returns None.

    Returns True if the write found a matching state entry and applied;
    False if no such entry exists yet (caller should insert one first).
    """
    with _state_lock():
        cur = _load_state()
        for i in cur:
            if i.get("sandbox_name") == sandbox_name:
                i["hermes_server_setup"] = _hermes_server_setup_dict(setup)
                _save_state(cur)
                return True
    return False


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


# ── Image management helpers ──────────────────────────────────────────────

def _ensure_image_in_cluster(
    image: str,
    gateway: str,
    progress_cb: Optional[Callable[..., None]] = None,
) -> bool:
    """Ensure a Docker image is available in the target OpenShell cluster's containerd.

    OpenShell runs k3s inside a Docker container. Each cluster has its own
    containerd image store that is separate from the host Docker daemon.
    Images get garbage-collected when no pods reference them, so we must
    re-import before every sandbox create.

    Flow:
      1. Resolve the cluster container name from the gateway name.
      2. Check if the image already exists in the cluster's containerd.
      3. If not, ``docker save <image> | docker exec -i <cluster> ctr import -``.

    Returns True if the image had to be imported (slow path, ~60-120s) and
    False if it was already present (fast path, ~1s). Callers use this to
    distinguish warm vs cold spawns when recording duration metrics.

    The image must already exist in the host Docker daemon (pulled from the
    Logos registry or built locally).

    ``progress_cb`` (optional) is invoked with a human-readable status string
    at the two slow phases (check + import). During the import it's
    called once per ~500 ms with a ``sub_percent`` kwarg so the caller
    can drive a progress bar instead of just a text label — the import
    typically takes 60-300 s on a cold first install and feels frozen
    without intra-step feedback.

    Callback signature: ``progress_cb(label, *, substage='', sub_percent=0)``.
    Callers written against the older ``progress_cb(label)`` shape keep
    working because the extra kwargs only surface on the import path.
    """
    def _emit(label: str, *, substage: str = "", sub_percent: int = 0) -> None:
        if progress_cb:
            try: progress_cb(label, substage=substage, sub_percent=sub_percent)
            except TypeError:
                # Legacy callback (label-only). Fall back so older callers
                # don't break when this helper gains kwargs.
                try: progress_cb(label)
                except Exception: pass
            except Exception: pass

    cluster_container = f"openshell-cluster-{gateway}"

    # Check if image already exists in containerd
    # docker.io/library/ prefix is added by containerd for unqualified names
    check_ref = image
    if "/" not in image:
        check_ref = f"docker.io/library/{image}"
    _emit("Checking sandbox image in cluster\u2026", substage="check_image")
    try:
        result = subprocess.run(
            ["docker", "exec", cluster_container, "ctr", "-n", "k8s.io",
             "images", "check", f"name=={check_ref}"],
            capture_output=True, text=True, timeout=10,
        )
        if check_ref in (result.stdout or ""):
            logger.debug("_ensure_image_in_cluster: %s already in %s", image, cluster_container)
            return False
    except Exception:
        pass  # check failed, proceed with import

    # Resolve the image size up-front so we can report percent during
    # the streaming import below. ``docker image inspect`` returns the
    # virtual size in bytes; the tar emitted by ``docker save`` is in
    # the same ballpark (slightly larger due to manifest/config overhead
    # and layer duplication), so using it as the denominator gives a
    # reasonable percent we cap at 99% until the import process exits.
    total_bytes = 0
    try:
        size_proc = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Size}}"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        total_bytes = int((size_proc.stdout or "0").strip() or "0")
    except Exception:
        total_bytes = 0  # fall back to label-only (no percent)

    _emit(
        "Staging image into cluster (first install: 3\u20135 min)\u2026",
        substage="import_image", sub_percent=0,
    )
    logger.info(
        "_ensure_image_in_cluster: importing %s (%.1f GB) into %s",
        image, (total_bytes / (1024 ** 3)) if total_bytes else 0.0, cluster_container,
    )
    try:
        save = subprocess.Popen(
            ["docker", "save", image],
            stdout=subprocess.PIPE, bufsize=4 * 1024 * 1024,
        )
        load = subprocess.Popen(
            ["docker", "exec", "-i", cluster_container,
             "ctr", "-n", "k8s.io", "images", "import", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=4 * 1024 * 1024,
        )
        # Hand-rolled progress-counting pipe: read from docker save,
        # write to ctr import, emit sub_percent every ~500 ms. 4 MB
        # chunks keep CPU overhead negligible vs raw pipe throughput.
        bytes_read = 0
        last_emit_ts = 0.0
        try:
            while True:
                chunk = save.stdout.read(4 * 1024 * 1024)
                if not chunk:
                    break
                try:
                    load.stdin.write(chunk)
                except BrokenPipeError:
                    break
                bytes_read += len(chunk)
                now = time.time()
                if total_bytes and (now - last_emit_ts) > 0.5:
                    pct = min(99, int(bytes_read * 100 / total_bytes))
                    _emit(
                        f"Staging image into cluster\u2026 {pct}% "
                        f"({bytes_read // (1024 ** 2)} / {total_bytes // (1024 ** 2)} MB)",
                        substage="import_image", sub_percent=pct,
                    )
                    last_emit_ts = now
        finally:
            try: save.stdout.close()
            except Exception: pass
            try: load.stdin.close()
            except Exception: pass
        # Wait for both sides to finish. We can't use ``load.communicate()``
        # here because it calls ``self.stdin.flush()`` internally — and we
        # already closed stdin above (required to signal EOF so ctr import
        # starts processing). Instead wait() and read stdout/stderr directly
        # post-exit; their buffered output on a successful import is tiny
        # (one "unpacking..." + "Loaded image" line) so the OS pipe buffer
        # can't fill up and deadlock.
        save.wait(timeout=30)
        try:
            load.wait(timeout=600)
        except subprocess.TimeoutExpired:
            load.kill()
            raise
        load_stderr = b""
        if load.stderr is not None:
            try: load_stderr = load.stderr.read() or b""
            except Exception: pass
            try: load.stderr.close()
            except Exception: pass
        if load.stdout is not None:
            try: load.stdout.close()
            except Exception: pass
        if load.returncode != 0:
            logger.warning(
                "_ensure_image_in_cluster: ctr import returned %d: %s",
                load.returncode, load_stderr.decode(errors="replace").strip(),
            )
        else:
            logger.info(
                "_ensure_image_in_cluster: imported %s into %s (%d MB streamed)",
                image, cluster_container, bytes_read // (1024 ** 2),
            )
            _emit(
                "Staging image into cluster\u2026 100%",
                substage="import_image", sub_percent=100,
            )
    except subprocess.TimeoutExpired:
        logger.error("_ensure_image_in_cluster: timed out importing %s", image)
        raise
    except Exception as exc:
        logger.error("_ensure_image_in_cluster: failed for %s: %s", image, exc)
        raise
    return True


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
    fallback_gateway: Optional[str],
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

    ``fallback_gateway`` is used only when a state-file entry is missing
    an ``openshell_name`` (stale pre-multi-gateway entries). If no
    fallback is available (``None``), we treat such entries as "gateway
    unknown" and keep them rather than dropping them on uncertain info.

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
        gw = inst.get("openshell_name") or fallback_gateway
        if gw is None:
            # No gateway known and no fallback — don't prune on missing info.
            kept.append(inst)
            continue
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
    once. Returns an empty list when no routes are configured — if
    /setup hasn't run yet there are no Logos-managed gateways to query.

    Used by orphan-prune and missing-sandbox-resurrect passes that need
    a global view of "what sandboxes exist anywhere" before they can
    decide which ones to clean up or recreate.
    """
    from gateway.auth import db as auth_db

    gateways_to_query: set[str] = set()
    try:
        for r in auth_db.list_model_routes():
            name = r.get("openshell_name")
            if name:
                gateways_to_query.add(name)
    except Exception as exc:
        logger.warning("could not enumerate model_routes: %s", exc)

    out: List[tuple[str, str]] = []
    for gw in gateways_to_query:
        for name in _list_sandbox_names(gateway=gw):
            out.append((name, gw))
    return out


# ── MCP auto-grant toolset injection ──────────────────────────────────────

def _auto_granted_mcp_rows() -> List[dict]:
    """Return DB rows for every auto-granted, running, docker-deployed MCP server.

    Internal shared helper behind :func:`_auto_granted_mcp_toolsets` and
    :func:`_auto_granted_mcp_configs`. Kept as one query instead of two
    so every caller sees a consistent snapshot of which servers are
    "eligible right now."

    External-URL servers are excluded even when auto_grant is set —
    they require a per-session access grant regardless, because the
    gateway side doesn't control their container lifecycle and can't
    assert the same "you clicked Deploy = consent" invariant.
    """
    try:
        from gateway.auth import db as _mcp_auth_db
    except Exception:
        return []
    try:
        rows = _mcp_auth_db.list_mcp_servers() or []
    except Exception as exc:
        logger.debug("auto_granted_mcp_rows: list_mcp_servers failed: %s", exc)
        return []
    return [
        row for row in rows
        if row.get("deploy_mode") == "docker"
        and row.get("auto_grant")
        and row.get("status") == "running"
        and row.get("name")
    ]


def _auto_granted_mcp_toolsets() -> List[str]:
    """Return ``mcp-<name>`` toolset names for every auto-granted MCP server."""
    return [f"mcp-{row['name']}" for row in _auto_granted_mcp_rows()]


# Hostname the sandbox resolves to the gateway. OpenShell's sandbox
# pod has an /etc/hosts entry mapping host.openshell.internal to the
# host-gateway IP (172.17.0.1 for the default docker bridge). The
# gateway's HTTP API — including the ``/mcp/<name>`` proxy — listens
# on that bridge IP so the sandbox can dial back.
_SANDBOX_GATEWAY_HOST = os.getenv("LOGOS_GATEWAY_HOST_FROM_SANDBOX") or "host.openshell.internal"
_SANDBOX_GATEWAY_PORT = int(
    os.getenv("LOGOS_GATEWAY_PORT")
    or os.getenv("HERMES_GATEWAY_PORT")
    or "8091"
)


def _auto_granted_mcp_configs(session_id: str) -> Dict[str, dict]:
    """Return an ``mcp_servers`` config dict for auto-granted MCP servers.

    Shape matches what the sandbox's ``tools/mcp_tool.py`` expects when
    it reads ``~/.hermes/config.yaml``: each entry has a ``url`` (the
    gateway's proxy path for that server), a ``transport`` hint so the
    MCP client uses streamable-HTTP, and an ``X-Session-Id`` header so
    the gateway's mcp_handlers proxy can tie the request back to a
    grant.

    The ``session_id`` is the sandbox's worker_id — a stable per-
    sandbox identifier. The caller is expected to register a grant
    via :func:`_grant_auto_mcp_access` before the sandbox's MCP
    client connects; otherwise the proxy rejects with 403.

    The URL target is the gateway's MCP proxy at
    ``http://host.openshell.internal:8091/mcp/<name>``, not the
    container's 127.0.0.1 port directly. The container binds to
    127.0.0.1 on the host (so it's off the LAN) but the sandbox can't
    reach the host's loopback. Going through the gateway's proxy
    means the sandbox only needs to know one host (the gateway), and
    the gateway does the 127.0.0.1:<host_port> translation.
    """
    base = f"http://{_SANDBOX_GATEWAY_HOST}:{_SANDBOX_GATEWAY_PORT}"
    headers = {"X-Session-Id": session_id} if session_id else {}
    return {
        row["name"]: {
            "url": f"{base}/mcp/{row['name']}",
            "transport": "streamable-http",
            "headers": headers,
        }
        for row in _auto_granted_mcp_rows()
    }


def _grant_auto_mcp_access(session_id: str) -> int:
    """Grant the given session access to every auto-granted MCP server.

    Pairs with :func:`_auto_granted_mcp_configs` — the sandbox's MCP
    client sends X-Session-Id: <session_id>, and the gateway's proxy
    checks the in-memory grant registry before forwarding. Without
    this call the sandbox's first request hits HTTP 403 and the
    mcp_tool registers zero tools.

    Called at spawn, refresh_instance_config, and startup rewire so
    every live sandbox has fresh grants against the current set of
    auto-granted servers. Returns the count granted (for logging).
    """
    if not session_id:
        return 0
    try:
        from gateway.mcp_access import grant_access
    except Exception:
        return 0
    n = 0
    for row in _auto_granted_mcp_rows():
        try:
            grant_access(session_id, row["name"])
            n += 1
        except Exception:
            pass
    return n


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

      2. Fall back to the default model route (``is_default=1``) so
         /admin/agents "Auto" binds work.

      3. Fall back to the first row in ``model_routes`` so partially
         provisioned installs still have a chance of spawning — any
         route is better than a hard error.

      4. If there are no model_routes at all, raise — the user hasn't
         run /setup yet, and spawning a sandbox with no gateway would
         just hang the caller on an invalid ``-g`` flag.
    """
    from gateway.openshell_routes import get_default_gateway_name
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

    # 3. First available route
    fallback_gw = get_default_gateway_name()
    if fallback_gw:
        # Pull the model from the route row for consistency so the worker
        # sees the exact model the gateway is pinned to.
        try:
            for r in auth_db.list_model_routes():
                if r.get("openshell_name") == fallback_gw:
                    return fallback_gw, (r.get("model") or "")
        except Exception:
            pass
        return fallback_gw, (config.model or "").strip()

    # 4. No routes configured at all
    raise RuntimeError(
        f"spawn({config.name}): cannot resolve an OpenShell gateway — "
        f"no rows in model_routes. Run /setup to provision a route first."
    )


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

    def spawn(
        self,
        config: InstanceConfig,
        *,
        progress_cb: Optional[Callable[..., None]] = None,
    ) -> SpawnedInstance:
        """Spawn a fresh sandbox for ``config``.

        ``progress_cb`` (optional) is invoked with a human-readable status
        string as the spawn moves through its phases (image check, image
        import, pod create, config upload, finalize). Signature:

            progress_cb(label, *, substage='', sub_percent=0)

        - ``label``    — user-facing sentence (live sub-label on the stepper)
        - ``substage`` — slug the frontend maps to a nested sub-stepper
                         row (check_image / import_image / create_pod /
                         upload_config / finalize)
        - ``sub_percent`` — 0-100 for intra-phase progress. Only the
                            image-import phase populates this today
                            (byte-level, sampled ~2 Hz).

        The /setup wizard uses this to surface a live heartbeat during
        the multi-minute first-install image import that otherwise looks
        frozen. Defaults to a no-op so non-setup callers (agent edit,
        admin re-spawn) are unaffected.
        """
        from gateway.openshell_routes import get_default_gateway_name

        # OpenShell sandboxes are backed by Kubernetes Sandbox CRs, so the
        # sandbox name must be a valid RFC 1123 subdomain: lowercase
        # [a-z0-9.-], must start/end with alphanumeric, max 63 chars.
        sandbox_name = _sanitize_sandbox_name(f"hermes-{config.name}")
        worker_id = sandbox_name  # worker registers with this ID

        def _emit(label: str, *, substage: str = "", sub_percent: int = 0) -> None:
            if progress_cb:
                try: progress_cb(label, substage=substage, sub_percent=sub_percent)
                except TypeError:
                    # Legacy label-only callbacks still work.
                    try: progress_cb(label)
                    except Exception: pass
                except Exception: pass
            # Persist the current substage into the state file so the
            # Chats-tab banner can render granular copy via
            # /api/admin/sandboxes polling (instead of one opaque
            # "provisioning" label for the whole multi-minute
            # pipeline). Best-effort — never block the spawn on a
            # state-file write hiccup, and never clobber phase.
            try:
                with _state_lock():
                    cur = _load_state()
                    for inst in cur:
                        if inst.get("sandbox_name") == sandbox_name:
                            inst["spawn_substage"] = substage or ""
                            inst["spawn_substage_label"] = label or ""
                            inst["spawn_substage_pct"] = int(sub_percent or 0)
                            break
                    _save_state(cur)
            except Exception:
                pass

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

        # Write instance config to a temp file for upload. The sandbox
        # worker reads this from /tmp/hermes/instance-config.json at
        # dispatch time via load_config() and uses it to resolve the
        # model + toolsets when the task payload doesn't supply them.
        #
        # M10 cleanup (2026-04-12): dropped the ``gateway_url`` field
        # that was carried over from the original Plan A reverse-
        # connection architecture. Plan A-prime's per-task exec transport
        # doesn't have the sandbox dial back into the gateway, and the
        # M10 Phase 1 sandbox_worker.py rewrite instantiates AIAgent
        # in-process (talking only to inference.local via the OpenShell
        # privacy router) — nothing reads gateway_url any more.
        # Bridge tool credentials from the gateway's services DB to the
        # sandbox via instance-config. The gateway and sandbox are
        # separate processes — gateway os.environ does NOT propagate —
        # so we ship the dict in the config and sandbox_worker.py
        # applies it to its own os.environ before constructing AIAgent.
        # Without this, tool code like firecrawl_search() reads None
        # from os.getenv("FIRECRAWL_API_URL") even after you set it
        # in Config → Tools.
        try:
            from gateway import services as _services
            _service_env = _services._get_credentials() or {}
        except Exception as _exc:
            logger.debug("instance-config: services credential lookup failed: %s", _exc)
            _service_env = {}

        # LOG-46: point the sandbox's OpenAI-compat auxiliary client
        # (compression / summarization / memory flush) at the gateway's
        # privacy-routed inference.local endpoint. Without this the
        # upstream auxiliary_client auto-detect hits every provider
        # chain (openrouter / nous / codex / custom) and silently gives
        # up — producing the "Auxiliary auto-detect: no provider
        # available" warning every dispatch. A placeholder key is fine
        # because OpenShell's router replaces it with the
        # gateway-configured credential before forwarding upstream.
        _service_env.setdefault("OPENAI_BASE_URL", "https://inference.local/v1")
        _service_env.setdefault("OPENAI_API_KEY", "lm-studio")

        # Per-agent messaging tokens override any global TELEGRAM_BOT_TOKEN /
        # DISCORD_BOT_TOKEN / SLACK_BOT_TOKEN / WHATSAPP_TOKEN. This means
        # send_message_tool (running inside the sandbox) uses THIS agent's
        # bot, not whatever the gateway has globally. If the agent has no
        # credential rows, the global value passes through unchanged —
        # keeps single-bot deployments working.
        try:
            _agent_for_env = auth_db.get_agent_by_name(config.name)
            if _agent_for_env:
                from gateway.services import get_agent_channel_env as _gace
                _service_env.update(_gace(_agent_for_env["id"]))
        except Exception as _env_exc:
            logger.debug("instance-config: per-agent channel env failed: %s", _env_exc)

        # Website blocklist (Layer 1 URL consent) — pull from agent record
        # and pass through. sandbox_worker writes it to ~/.hermes/config.yaml
        # where hermes's website_policy.py looks for it.
        _website_blocklist = None
        try:
            _agent_lookup = auth_db.get_agent_by_name(config.name)
            if _agent_lookup and _agent_lookup.get("website_blocklist"):
                _website_blocklist = json.loads(_agent_lookup["website_blocklist"])
        except Exception:
            _website_blocklist = None

        # Allowed-hosts visibility — derive from the effective policy so the
        # worker can inject "you can navigate these hosts: …" into the
        # agent's system prompt. Without this, agents trial-and-error
        # against the firewall (try coinmarketcap → 403 → try coingecko →
        # 403 → give up) which wastes API calls and produces wrong
        # answers. Best-effort; empty list is a safe no-op (worker just
        # skips the injection).
        _allowed_hosts: List[str] = []
        try:
            from gateway import policies as _gp_h
            _agent_for_hosts = auth_db.get_agent_by_name(config.name)
            if _agent_for_hosts:
                _allowed_hosts = _gp_h.get_allowed_hosts_for_agent(_agent_for_hosts["id"])
        except Exception as _hosts_exc:
            logger.debug("instance-config: allowed-hosts lookup failed: %s", _hosts_exc)

        # Per-agent permission summary — a small markdown block the
        # sandbox worker prepends to the system prompt so the agent
        # knows which capabilities are on/off and can tell the user
        # which Permissions toggle to flip when a request hits a
        # disabled capability. Built from capabilities.compute_state
        # so it mirrors what the UI shows.
        _capabilities_prompt = ""
        try:
            from gateway import capabilities as _gcap
            _agent_for_caps = auth_db.get_agent_by_name(config.name)
            if _agent_for_caps:
                _capabilities_prompt = _gcap.format_agent_prompt_block(_agent_for_caps["id"])
        except Exception as _caps_exc:
            logger.debug("instance-config: capabilities summary failed: %s", _caps_exc)

        # Merge auto-granted docker MCP server toolsets into the agent's
        # explicit toolset list. Preserves ordering and de-dupes so a
        # toolset the user manually enabled and an auto-grant both land
        # once. See _auto_granted_mcp_toolsets() for the selection rules.
        _effective_toolsets: List[str] = list(config.toolsets or [])
        for _ts in _auto_granted_mcp_toolsets():
            if _ts not in _effective_toolsets:
                _effective_toolsets.append(_ts)

        # Full MCP server configs — the sandbox writes these to
        # ``~/.hermes/config.yaml`` so ``discover_mcp_tools`` can
        # connect to each server and register the ``mcp_<name>_<tool>``
        # handlers. Without this, the toolset name is in the list but
        # has no tools behind it, and the agent hallucinates results.
        _mcp_servers_cfg = _auto_granted_mcp_configs(worker_id)
        _n_granted = _grant_auto_mcp_access(worker_id)
        if _n_granted:
            logger.info(
                "spawn(%s): granted MCP access to %d auto-granted server(s) for session=%s",
                config.name, _n_granted, worker_id,
            )

        instance_config = {
            "worker_id": worker_id,
            "instance_name": config.name,
            "soul": config.soul_name or "general",
            "toolsets": _effective_toolsets,
            "model": resolved_model,
            "env": _service_env,
            "website_blocklist": _website_blocklist,
            "allowed_hosts": _allowed_hosts,
            "capabilities_prompt": _capabilities_prompt,
            "mcp_servers": _mcp_servers_cfg,
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
            fallback_gw = get_default_gateway_name()
            gateways = {
                gw for gw in (
                    (i.get("openshell_name") or fallback_gw) for i in instances
                ) if gw
            }
            gateways.add(openshell_gw)
            index = _build_sandbox_index(gateways)
            instances, _pruned = _prune_state_against_index(
                instances, index, fallback_gw
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
        # M10 scope item 5: effective policy = baseline + applied presets.
        # Looked up by agent name (config.name → auth_db.get_agent_by_name)
        # so the InstanceConfig dataclass doesn't need a new field. Falls
        # back to self.policy_file (the raw baseline) if the lookup or
        # merge fails, so a broken preset can't block spawning — degrading
        # to the narrowest available policy is always preferred over
        # aborting the spawn.
        _effective_policy_tmp: Optional[Path] = None
        # Spawn-duration tracking — populated below and recorded on success.
        # We also stamp a per-phase split so the UI's banner can say
        # "usually Ns for the pod, then Ns for the agent" based on
        # learned medians instead of a single hand-waved number. The
        # phases are: pod-create (openshell sandbox create returns)
        # vs agent-boot (hermes HTTP answers /health).
        _spawn_started_at = time.time()
        _pod_ready_at: Optional[float] = None
        _agent_ready_at: Optional[float] = None
        _image_was_imported = False
        try:
            # ── Step 0: ensure the sandbox image is in the cluster ─
            #
            # OpenShell's k3s containerd is separate from the host
            # Docker daemon. Images get GC'd when no pods reference
            # them. This step imports the image if it's missing,
            # taking ~60-120s for a fresh import (layers are cached
            # after the first import, making subsequent spawns fast).
            # Pass our local _emit as the progress_cb so image-import
            # substages ("check_image", "import_image" with sub_percent)
            # also get persisted to the state file — the banner reads
            # from /api/admin/sandboxes polling and otherwise sees the
            # 90-180s image-import window as an opaque "provisioning".
            _image_was_imported = _ensure_image_in_cluster(
                self.sandbox_image, openshell_gw, progress_cb=_emit,
            )

            # ── Step 1: create the sandbox CR ──────────────────────
            #
            # Pre-create check: if a sandbox with this name already
            # exists and is Ready (e.g. from a previous gateway
            # instance that spawned it before crashing), skip the
            # create and go straight to uploads. This avoids the
            # openshell CLI hanging for 300s+ on a duplicate create.
            _sandbox_already_exists = False
            try:
                _list_result = _openshell(
                    "sandbox", "list",
                    gateway=openshell_gw, check=False, timeout=15,
                )
                if _list_result.returncode == 0 and sandbox_name in (_list_result.stdout or ""):
                    # Check if it's Ready (not Provisioning/Failed)
                    for _line in (_list_result.stdout or "").splitlines():
                        if sandbox_name in _line and "Ready" in _line:
                            logger.info(
                                "spawn(%s): sandbox already exists and is Ready — "
                                "skipping create, proceeding to uploads",
                                sandbox_name,
                            )
                            _sandbox_already_exists = True
                            break
            except Exception:
                pass  # list failed, proceed with create

            if not _sandbox_already_exists:
                _emit("Creating sandbox pod\u2026", substage="create_pod")
                create_args = [
                    "sandbox", "create",
                    "--name", sandbox_name,
                    "--from", self.sandbox_image,
                    "--no-auto-providers",
                ]
                # Compute the effective policy (baseline + applied presets)
                # for this agent. If the agent has any presets applied in
                # the DB, we write a merged YAML to a tempfile and pass THAT
                # instead of the raw baseline. gateway.policies is imported
                # inside the try so we don't reintroduce a circular import
                # at module load time — gateway.policies imports back from
                # this module for _openshell + _sanitize_sandbox_name.
                _policy_arg: Optional[str] = None
                try:
                    from gateway.auth import db as _policies_auth_db
                    _agent_row = _policies_auth_db.get_agent_by_name(config.name)
                    if _agent_row:
                        from gateway import policies as _gp
                        _effective_policy_tmp = _gp.write_effective_policy_to_tempfile(
                            _agent_row["id"]
                        )
                        _policy_arg = str(_effective_policy_tmp)
                        logger.info(
                            "spawn(%s): using effective policy with %d applied "
                            "preset(s)", sandbox_name,
                            len(_gp.get_applied_presets(_agent_row["id"])),
                        )
                except Exception as _policy_exc:
                    logger.warning(
                        "spawn(%s): effective policy computation failed, "
                        "falling back to baseline %s: %s",
                        sandbox_name, self.policy_file, _policy_exc,
                    )
                if not _policy_arg and self.policy_file and Path(self.policy_file).exists():
                    _policy_arg = self.policy_file
                if _policy_arg:
                    create_args += ["--policy", _policy_arg]
                # CRITICAL: trailing `-- true` is required.
                #
                # `openshell sandbox create` with NO trailing command
                # (after `--`) defaults to opening an interactive PTY
                # shell once the CR is ready, and the create call blocks
                # for the lifetime of that shell. Without a trailing
                # command we get `ssh -tt -o RequestTTY=force` zombies
                # that never exit and the create call hangs until the
                # timeout reaper kills it.
                #
                # Passing `-- true` runs `/usr/bin/true` inside the
                # sandbox which exits in milliseconds. Without
                # `--no-keep` the sandbox stays alive after the initial
                # command exits, so we can run the upload + worker exec
                # steps below as independent calls.
                create_args += ["--", "true"]
                # Pod-created-but-cli-hung recovery path. Openshell's
                # sandbox-create opens an SSH session to run the
                # trailing `true` and occasionally hangs on the
                # handshake under concurrent-spawn contention. The pod
                # comes up fine, but the CLI never returns and we
                # time out after 600 s. Before raising, check whether
                # the sandbox now exists and is Ready — if so, the pod
                # is there, the only thing missing is the Logos
                # pipeline continuation, which we can still do.
                # Catches ``subprocess.TimeoutExpired`` (the 600 s
                # reaper) AND ``CalledProcessError`` (non-zero exit
                # that might still have created the CR).
                try:
                    result = _openshell(*create_args, gateway=openshell_gw, check=True)
                    logger.debug("openshell sandbox create stdout: %s", result.stdout.strip())
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as _create_exc:
                    # Did the pod come up anyway?
                    _pod_came_up = False
                    try:
                        _verify = _openshell(
                            "sandbox", "list",
                            gateway=openshell_gw, check=False, timeout=15,
                        )
                        for _line in (_verify.stdout or "").splitlines():
                            if sandbox_name in _line and "Ready" in _line:
                                _pod_came_up = True
                                break
                    except Exception as _verify_exc:
                        logger.warning(
                            "spawn(%s): post-timeout sandbox-list probe failed: %s",
                            sandbox_name, _verify_exc,
                        )
                    if _pod_came_up:
                        logger.warning(
                            "spawn(%s): sandbox create %s but pod IS Ready — "
                            "adopting the pod and continuing the pipeline",
                            sandbox_name,
                            "timed out" if isinstance(_create_exc, subprocess.TimeoutExpired)
                                        else f"failed with rc={getattr(_create_exc, 'returncode', '?')}",
                        )
                        # Kill any hermes surviving from a prior pod so
                        # the fresh deploy_hermes_config (which mints a
                        # new API_SERVER_KEY) isn't left fighting an
                        # already-running hermes that still has the OLD
                        # key in memory. Without this, .env has key N+1
                        # and hermes in memory has key N → every Logos
                        # dispatch 401s until the user clicks Restart
                        # runtime. Harmless if no hermes was running
                        # (pkill -f returns 1, swallowed by `|| true`).
                        try:
                            subprocess.run(
                                ["openshell", "-g", openshell_gw, "sandbox", "exec",
                                 "-n", sandbox_name, "--no-tty", "--",
                                 "sh", "-c",
                                 "pkill -f 'hermes-srv-home/hermes gateway run|hermes_cancel_monkeypatch\\.py gateway run|/usr/local/bin/hermes gateway run' || true"],
                                capture_output=True, text=True, timeout=15,
                            )
                            logger.info(
                                "spawn(%s): killed any surviving hermes before adopt (prevents .env/key split-brain)",
                                sandbox_name,
                            )
                        except Exception as _kill_exc:
                            logger.warning(
                                "spawn(%s): pre-adopt hermes kill failed (non-fatal): %s",
                                sandbox_name, _kill_exc,
                            )
                        # Fall through — the pod exists, the rest of
                        # the pipeline (deploy_hermes_config / launcher
                        # / SOUL.md / hermes launch / health) runs
                        # against the existing pod and finishes the spawn.
                    else:
                        # Pod didn't come up — re-raise the original
                        # error so the normal cleanup path runs.
                        raise
                # Stamp pod-ready the moment openshell sandbox create
                # returns — that call blocks until the pod phase = Ready,
                # so this is the boundary between pod-spawn and agent-
                # boot for metrics purposes.
                _pod_ready_at = time.time()

            # ── Step 2: hermes-server mode setup ───────────────────
            _emit("Uploading agent configuration\u2026", substage="upload_config")

            # Hermes-server mode (Phase 2 default). Deploys hermes config,
            # launches `hermes gateway run` HTTP server in the sandbox,
            # waits for /health. Failures bubble up so a broken spawn is
            # visible at create-time instead of surfacing as silent v1
            # fallback at chat-time. Disable only via explicit
            # LOGOS_HERMES_SERVER_MODE=0 (opt-out). See
            # gateway/executors/hermes_server_mode.py + hermes-as-server-
            # prototype.md.
            from .hermes_server_mode import (
                is_enabled as _log44_on,
                enable_hermes_server_mode as _log44_go,
                build_channel_extra_env as _log44_build_env,
            )
            _hermes_srv_setup = None
            if _log44_on():
                # LOG-44.3: look up per-agent channel credentials +
                # auto-apply matching network policy presets. The helper
                # writes to both the returned dict (→ .env) and the
                # policies DB (→ sandbox egress rules).
                from gateway.auth import db as _log44_auth_db
                _log44_agent_rec = _log44_auth_db.get_agent_by_name(config.name) or {}
                _log44_agent_id = _log44_agent_rec.get("id")
                _log44_extra_env = (
                    _log44_build_env(
                        _log44_agent_id,
                        sandbox_name_for_log=sandbox_name,
                    )
                    if _log44_agent_id else {}
                )
                logger.info(
                    "spawn(%s): hermes-server mode enabled (Phase 2 default)",
                    sandbox_name,
                )
                _hermes_srv_setup = _log44_go(
                    sandbox_name, config,
                    extra_env=_log44_extra_env or None,
                    gateway=openshell_gw,
                )
                if _hermes_srv_setup is None:
                    raise RuntimeError(
                        f"spawn({sandbox_name}): hermes-server mode "
                        f"enabled but enable_hermes_server_mode returned "
                        f"None. Cannot continue — chat dispatch would "
                        f"silently regress to v1."
                    )
                # Stamp agent-ready: enable_hermes_server_mode returns
                # after wait_for_hermes_health confirms /health is 200,
                # so this is the boundary we want for the "agent-boot"
                # phase metric.
                _agent_ready_at = time.time()
            else:
                logger.warning(
                    "spawn(%s): LOGOS_HERMES_SERVER_MODE=0 — skipping "
                    "server-mode setup. Agent will need v1 (Plan A-Prime) "
                    "dispatch until the flag is flipped back.",
                    sandbox_name,
                )

            # Phase 3 (under lock): flip the record's phase to "ready"
            # so subsequent list_instances() prunes apply normal rules.
            # Re-load the state file because something else may have
            # mutated it while we were running the openshell CLI calls.
            _emit("Finalizing sandbox\u2026", substage="finalize")
            with _state_lock():
                cur = _load_state()
                for i in cur:
                    if i.get("sandbox_name") == sandbox_name:
                        i["phase"] = "ready"
                        if _hermes_srv_setup is not None:
                            i["hermes_server_setup"] = _hermes_server_setup_dict(
                                _hermes_srv_setup
                            )
                        break
                else:
                    # Our record vanished mid-spawn (e.g. user deleted
                    # the agent, or another spawn ran a stale prune
                    # on a gateway that had just temporarily failed).
                    # Re-insert with phase=ready so the worker is
                    # discoverable.
                    record["phase"] = "ready"
                    if _hermes_srv_setup is not None:
                        record["hermes_server_setup"] = _hermes_server_setup_dict(
                            _hermes_srv_setup
                        )
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
            if _effective_policy_tmp:
                try:
                    os.unlink(_effective_policy_tmp)
                except OSError:
                    pass

        # Record spawn duration so the UI banner can show a learned
        # estimate instead of a hardcoded guess. Best-effort — never
        # block return on a metrics write. We split the total into
        # two phases (pod-create → agent-boot) so the banner's two
        # banner states (provisioning pod vs. starting agent) each
        # get their own accurate learned median.
        try:
            from gateway import spawn_metrics as _sm
            _now = time.time()
            _pod_ms = int((_pod_ready_at - _spawn_started_at) * 1000) if _pod_ready_at else None
            _agent_ms = (
                int((_agent_ready_at - _pod_ready_at) * 1000)
                if _agent_ready_at and _pod_ready_at else None
            )
            _sm.record(
                gateway=openshell_gw,
                image=self.sandbox_image,
                duration_ms=int((_now - _spawn_started_at) * 1000),
                image_imported=_image_was_imported,
                agent_name=config.name,
                pod_ms=_pod_ms,
                agent_ms=_agent_ms,
            )
        except Exception:
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
        from gateway.openshell_routes import get_default_gateway_name

        # Hardened pruning: batch one CLI query per gateway, keep
        # grace-period entries, never drop entries from a gateway
        # whose query failed. See _prune_state_against_index() for
        # the full rule list.
        with _state_lock():
            instances = _load_state()
            fallback_gw = get_default_gateway_name()
            gateways = {
                gw for gw in (
                    (i.get("openshell_name") or fallback_gw) for i in instances
                ) if gw
            }
            index = _build_sandbox_index(gateways)
            kept, pruned = _prune_state_against_index(instances, index, fallback_gw)
            if pruned > 0:
                _save_state(kept)
        return kept

    def refresh_instance_config(self, name: str) -> bool:
        """Re-upload ``/tmp/hermes/instance-config.json`` from current DB state.

        Plan A-prime spawns a fresh ``sandbox_worker.py`` per dispatch and
        each subprocess re-reads the config file at startup, so the next
        chat after this call sees the updated toolsets / model / env
        without a sandbox restart. Used by the toolset toggle handler in
        ``admin_handlers`` so ticking a checkbox in STAMP T actually
        takes effect on the next message instead of requiring destroy
        + respawn. Also called when service credentials change so newly
        configured tool URLs reach the sandbox immediately.

        Returns True on successful upload, False on any failure (no
        sandbox / no gateway / openshell error). Best-effort by design —
        a stray toggle should never tank the request that triggered it.
        """
        from gateway.openshell_routes import get_default_gateway_name
        import gateway.auth.db as _adb

        agent = _adb.get_agent_by_name(name)
        if not agent:
            logger.warning("refresh_instance_config: no agent named %r", name)
            return False

        sandbox_name = _sanitize_sandbox_name(f"hermes-{name}")
        # Resolve the gateway the sandbox lives inside (state file first,
        # default route as fallback) — same approach as delete_instance
        # since sandbox-to-gateway routing isn't carried in the DB.
        fallback_gw = get_default_gateway_name()
        target_gw: Optional[str] = fallback_gw
        for inst in _load_state():
            if inst.get("name") == name or inst.get("sandbox_name") == sandbox_name:
                target_gw = inst.get("openshell_name") or fallback_gw
                break
        if not target_gw:
            logger.warning(
                "refresh_instance_config(%s): no gateway resolvable — skipping",
                name,
            )
            return False

        # Resolve model the same way spawn() does so the refreshed config
        # matches what a fresh spawn would have produced.
        resolved_model = agent.get("model") or ""
        route_id = agent.get("model_route_id")
        if route_id:
            try:
                route = _adb.get_model_route(route_id)
                if route and route.get("model"):
                    resolved_model = route["model"]
            except Exception:
                pass

        toolsets_raw = agent.get("toolsets") or "[]"
        try:
            toolsets = json.loads(toolsets_raw) if isinstance(toolsets_raw, str) else list(toolsets_raw)
        except json.JSONDecodeError:
            toolsets = []

        # Same MCP auto-grant merge as spawn(): refreshing without this
        # would strip docker MCP toolsets from an already-spawned agent
        # the next time anything touched its config (toggle in Tools UI,
        # credential change, etc.), silently disabling the MCP tools
        # until the next full respawn.
        for _ts in _auto_granted_mcp_toolsets():
            if _ts not in toolsets:
                toolsets.append(_ts)

        _mcp_servers_cfg = _auto_granted_mcp_configs(sandbox_name)
        _grant_auto_mcp_access(sandbox_name)

        # Same env-bridge as spawn(): credentials for tool code that
        # needs to dial out to user-configured services. Re-pulled here
        # so a refresh after saving a new credential picks it up.
        try:
            from gateway import services as _services
            _service_env = _services._get_credentials() or {}
        except Exception:
            _service_env = {}

        # Per-agent messaging token overrides — matches spawn() behavior
        # so a credential row rotation takes effect on refresh without a
        # full respawn.
        try:
            from gateway.services import get_agent_channel_env as _gace
            _service_env.update(_gace(agent["id"]))
        except Exception as _env_exc:
            logger.debug("refresh_instance_config: per-agent channel env failed: %s", _env_exc)

        # Website blocklist (Layer 1 URL consent). Same shape as spawn().
        _website_blocklist = None
        try:
            if agent.get("website_blocklist"):
                _website_blocklist = json.loads(agent["website_blocklist"])
        except Exception:
            _website_blocklist = None

        # Allowed-hosts visibility — same as spawn() so the worker can
        # inject the firewall allowlist into the agent's system prompt
        # after a capability toggle without needing a full respawn.
        _allowed_hosts: List[str] = []
        try:
            from gateway import policies as _gp_h2
            _allowed_hosts = _gp_h2.get_allowed_hosts_for_agent(agent["id"])
        except Exception:
            pass

        # Per-agent permission summary — see spawn() for rationale.
        # Refresh needs this too so a capability toggle gets reflected
        # in the agent's next turn without requiring a respawn.
        _capabilities_prompt = ""
        try:
            from gateway import capabilities as _gcap2
            _capabilities_prompt = _gcap2.format_agent_prompt_block(agent["id"])
        except Exception:
            pass

        instance_config = {
            "worker_id": sandbox_name,
            "instance_name": name,
            "soul": agent.get("soul_slug") or "general",
            "toolsets": toolsets,
            "model": resolved_model,
            "env": _service_env,
            "website_blocklist": _website_blocklist,
            "allowed_hosts": _allowed_hosts,
            "capabilities_prompt": _capabilities_prompt,
            "mcp_servers": _mcp_servers_cfg,
        }

        config_tmpfile = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="hermes-config-", delete=False,
        )
        try:
            json.dump(instance_config, config_tmpfile)
            config_tmpfile.close()
            _openshell(
                "sandbox", "upload", sandbox_name,
                config_tmpfile.name, "/tmp/hermes/instance-config.json",
                gateway=target_gw, check=True, timeout=30,
            )
            logger.info(
                "refresh_instance_config(%s): re-uploaded with %d toolset(s), %d env var(s) on gateway %s",
                name, len(toolsets), len(_service_env), target_gw,
            )
            return True
        except Exception as exc:
            logger.warning(
                "refresh_instance_config(%s): upload failed: %s", name, exc,
            )
            return False
        finally:
            try:
                os.unlink(config_tmpfile.name)
            except OSError:
                pass

    def refresh_all_instance_configs(self) -> int:
        """Re-upload instance-config.json to every running sandbox.

        Called after a credential change in Config → Tools so the new
        env var reaches every agent at once instead of requiring a
        per-agent toggle. Returns the number of successful refreshes;
        failures are logged but don't abort the loop.
        """
        import gateway.auth.db as _adb
        ok = 0
        for agent in _adb.list_agents():
            try:
                if self.refresh_instance_config(agent["name"]):
                    ok += 1
            except Exception as exc:
                logger.warning(
                    "refresh_all_instance_configs: agent %s failed: %s",
                    agent.get("name"), exc,
                )
        return ok

    def resurrect_hermes_server_mode(
        self,
        sandbox_name: str,
        config: "InstanceConfig",
    ) -> bool:
        """Restore v2 setup on a sandbox that Logos has forgotten.

        LOG-61 / LOG-62 fix. Two cases where a sandbox exists (or is
        about to be respawned) but has no usable ``hermes_server_setup``
        on its state record:

        1. **Reconcile.** Gateway restart leaves OpenShell sandboxes
           running from the previous generation, but Logos's state file
           got truncated. The sandbox is alive; its hermes process is
           running with an API key we no longer know. ``resurrect_missing
           _sandboxes`` creates a replacement state entry but can't
           populate ``hermes_server_setup`` without redeploying config
           and restarting hermes.

        2. **Fresh spawn that lost setup.** Defensive: if ``spawn()``'s
           own state-write race lost the ``hermes_server_setup`` field,
           this method reinstates it without reprovisioning the sandbox.

        Flow:
          - Deploy fresh ``.env`` + ``config.yaml`` + cancel monkeypatch +
            BOOT.md + memories via ``enable_hermes_server_mode``.
          - Force a pkill+relaunch so the running hermes picks up the
            new API_SERVER_KEY (``enable_hermes_server_mode``'s
            ``launch_hermes_gateway`` is idempotent and would otherwise
            no-op when hermes is already running with stale config).
          - Persist the new setup into state via
            ``persist_hermes_server_setup``.

        No-op (returns False) when ``LOGOS_HERMES_SERVER_MODE`` is
        disabled. Returns True on successful end-to-end setup.
        """
        from .hermes_server_mode import (
            is_enabled as _log44_on,
            enable_hermes_server_mode as _log44_go,
            restart_hermes_in_sandbox as _log44_restart,
            wait_for_hermes_health as _log44_wait_health,
            build_channel_extra_env as _log44_build_env,
        )
        if not _log44_on():
            return False

        # Resolve the owning gateway so subsequent CLI calls target the
        # right cluster in a multi-route deployment.
        target_gw: Optional[str] = None
        for inst in _load_state():
            if inst.get("sandbox_name") == sandbox_name:
                target_gw = inst.get("openshell_name") or None
                break

        try:
            from gateway.auth import db as _log44_auth_db
            agent_rec = _log44_auth_db.get_agent_by_name(config.name) or {}
            agent_id = agent_rec.get("id")
            extra_env = (
                _log44_build_env(agent_id, sandbox_name_for_log=sandbox_name)
                if agent_id else {}
            )
        except Exception as exc:
            logger.warning(
                "resurrect_hermes_server_mode(%s): channel env lookup "
                "failed, continuing without extras: %s",
                sandbox_name, exc,
            )
            extra_env = {}

        try:
            setup = _log44_go(
                sandbox_name, config,
                extra_env=extra_env or None,
                gateway=target_gw,
            )
        except Exception as exc:
            logger.warning(
                "resurrect_hermes_server_mode(%s): enable_hermes_server_mode "
                "failed: %s", sandbox_name, exc,
            )
            return False

        # Force a relaunch so the newly-deployed .env takes effect.
        # launch_hermes_gateway inside enable_hermes_server_mode is a
        # no-op when hermes was already running (reconcile case), so
        # without this explicit restart the new API key is on disk but
        # not in the running process.
        try:
            _log44_restart(sandbox_name, gateway=target_gw)
            _log44_wait_health(sandbox_name, gateway=target_gw)
        except Exception as exc:
            logger.warning(
                "resurrect_hermes_server_mode(%s): restart/health failed: %s",
                sandbox_name, exc,
            )
            return False

        if not persist_hermes_server_setup(sandbox_name, setup):
            logger.warning(
                "resurrect_hermes_server_mode(%s): persist failed — no "
                "matching state entry. State may need reconciliation "
                "before this call.",
                sandbox_name,
            )
            return False

        logger.info(
            "resurrect_hermes_server_mode(%s): setup restored, key=%s...",
            sandbox_name, setup.api_key[:12],
        )
        return True

    def refresh_channel_credentials(self, name: str) -> bool:
        """LOG-44.3.4 — hot-refresh a hermes-mode sandbox's channel creds.

        Credential change → rewrite ``.env`` inside the sandbox with
        the new token(s), then restart hermes-in-sandbox so it
        re-reads ``.env`` via ``_apply_env_overrides`` and re-enables
        its platform adapters. No pod destroy, so workspace files +
        hermes's SessionDB survive. In-flight dispatches will fail
        (hermes process bounces) but the sandbox stays.

        Skipped (returns False) when the sandbox has no
        ``hermes_server_setup`` on its state record — i.e. it's not
        running hermes-server mode, so channel delegation is N/A and
        the user's central Logos adapter handles it instead.

        Called by the channel-credential admin handlers on save /
        toggle / delete so the UI change takes effect immediately.
        Best-effort: a failure here shouldn't poison the DB write —
        callers should catch + log without bubbling.

        Returns True if the refresh actually ran end-to-end.
        """
        from .hermes_server_mode import (
            is_enabled as _log44_on,
            build_channel_extra_env,
            redeploy_hermes_env,
            restart_hermes_in_sandbox,
            wait_for_hermes_health,
        )
        if not _log44_on():
            logger.debug(
                "refresh_channel_credentials(%s): LOGOS_HERMES_SERVER_MODE "
                "off — nothing to refresh", name,
            )
            return False

        import gateway.auth.db as _adb
        agent = _adb.get_agent_by_name(name)
        if not agent:
            logger.warning(
                "refresh_channel_credentials: no agent named %r", name,
            )
            return False
        agent_id = agent.get("id")
        sandbox_name = _sanitize_sandbox_name(f"hermes-{name}")

        # Pull hermes_server_setup from state so we reuse the same
        # API_SERVER_KEY the sandbox was spawned with — a new key would
        # invalidate dispatch_v2's Bearer and every chat would 401.
        setup = None
        for inst in _load_state():
            if inst.get("sandbox_name") == sandbox_name:
                setup = inst.get("hermes_server_setup")
                break
        if not setup or not setup.get("api_key"):
            logger.info(
                "refresh_channel_credentials(%s): sandbox has no "
                "hermes_server_setup — not a hermes-mode agent, skipping",
                sandbox_name,
            )
            return False

        extra_env = build_channel_extra_env(
            agent_id, sandbox_name_for_log=sandbox_name,
        ) if agent_id else {}

        # Look up the openshell sub-gateway that owns this sandbox so the
        # CLI calls below target the right place. Multi-route deployments
        # keep separate gateways per model route — without this, the CLI
        # defaults to the user's active gateway and sees "sandbox not
        # found" when the sandbox actually lives elsewhere.
        target_gw: Optional[str] = None
        for inst in _load_state():
            if inst.get("sandbox_name") == sandbox_name:
                target_gw = inst.get("openshell_name")
                break

        try:
            redeploy_hermes_env(
                sandbox_name,
                api_key=setup["api_key"],
                extra_env=extra_env or None,
                gateway=target_gw,
            )
        except Exception as exc:
            logger.warning(
                "refresh_channel_credentials(%s): .env redeploy failed: %s",
                sandbox_name, exc,
            )
            return False

        try:
            restart_hermes_in_sandbox(sandbox_name, gateway=target_gw)
        except Exception as exc:
            logger.warning(
                "refresh_channel_credentials(%s): hermes restart failed: %s",
                sandbox_name, exc,
            )
            return False

        try:
            wait_for_hermes_health(sandbox_name, gateway=target_gw)
        except TimeoutError as exc:
            logger.warning(
                "refresh_channel_credentials(%s): health did not return "
                "after restart: %s", sandbox_name, exc,
            )
            return False

        logger.info(
            "refresh_channel_credentials(%s): complete — %d channel env "
            "keys live, hermes re-ready",
            sandbox_name, len(extra_env),
        )
        return True

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
        # default-route fallback) to find which gateway the sandbox lives
        # inside — `openshell sandbox delete <name>` without `-g` only
        # checks the CLI's currently-selected gateway and silently
        # succeeds if the sandbox isn't there. Best-effort lookup: scan
        # the state file for an entry matching by name OR sandbox_name,
        # use its openshell_name; otherwise default to the user's
        # default model route. If neither is available, there's nothing
        # to delete — the gateway is unknown and so is the sandbox.
        from gateway.openshell_routes import get_default_gateway_name

        sandbox_name = _sanitize_sandbox_name(f"hermes-{name}")
        fallback_gw = get_default_gateway_name()
        target_gw: Optional[str] = fallback_gw
        for inst in _load_state():
            if inst.get("name") == name or inst.get("sandbox_name") == sandbox_name:
                target_gw = inst.get("openshell_name") or fallback_gw
                break

        if not target_gw:
            logger.info(
                "delete_instance(%s): no gateway resolvable (no state entry, "
                "no default route) — skipping CLI delete, cleaning state only",
                name,
            )
        else:
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
