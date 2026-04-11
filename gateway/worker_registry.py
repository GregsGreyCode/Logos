"""
Worker Registry — manages per-sandbox subprocess workers via `openshell sandbox exec`.

Plan A architecture (TASKS.md #24): instead of the sandbox initiating a
reverse WebSocket back to the gateway (the old approach, which broke
when OpenShell's L7 proxy tightened CONNECT-tunnel handling), the
gateway launches ``openshell sandbox exec --no-tty --name <sandbox> --
python3 /app/sandbox_worker.py`` as a long-running subprocess per
sandbox. The subprocess's stdin and stdout become the bidirectional
control channel:

    gateway → subprocess stdin → openshell gRPC → sandbox worker
    sandbox worker → openshell gRPC → subprocess stdout → gateway

This keeps OpenShell's blessed gRPC/mTLS transport in the hot path and
eliminates the need for any sandbox-initiated network egress. The
``openshell sandbox exec`` primitive was empirically rock-solid
throughout the 2026-04-11 debugging session that led to this refactor.

Public interface (preserved from the old WebSocket registry so no
external call sites change):

    worker_registry.workers                   # dict[sandbox_name, WorkerEntry]
    worker_registry.get(sandbox_name)         # Optional[WorkerEntry]
    worker_registry.list_workers()            # list[dict]
    worker_registry.list_healthy()            # list[WorkerEntry]
    worker_registry.dispatch_task(sandbox_name, task, timeout, on_stream_event)  # await result
    worker_registry.ensure_worker(sandbox_name, soul=..., toolsets=..., ...)     # NEW

New method: ``ensure_worker`` — called by ``OpenShellExecutor.spawn``
after the sandbox reaches Ready phase. Launches the exec subprocess,
waits for the worker's "ready" event on stdout, registers it. Idempotent:
calling it when a worker is already running is a cheap no-op.

WorkerEntry.healthy semantics change slightly from the old WebSocket
version: ``healthy`` now means "the underlying subprocess.returncode is
None" (process still alive). The old ``last_heartbeat < HEARTBEAT_TIMEOUT``
check doesn't apply because there's no separate heartbeat protocol —
the subprocess itself is the heartbeat. If you need finer-grained
liveness (e.g., "is the worker actually responsive, not just alive"),
send a ``{"type":"ping"}`` over dispatch_task and check for a pong.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# How long to wait for the worker's "ready" event after spawning the
# subprocess. OpenShell sandbox exec has overhead (gRPC handshake, auth
# resolution, process spawn inside the pod) — measured ~1-2s typically
# but capped high for slow cold-start paths.
WORKER_READY_TIMEOUT = 60.0

# How long dispatch_task will wait for a task_result before giving up.
# Matches the patched openshell-router 600s ceiling (#22) so the whole
# chain agrees on the upper bound for an inference call.
DEFAULT_TASK_TIMEOUT = 600.0


# ── Data class ─────────────────────────────────────────────────────────────


@dataclass
class WorkerEntry:
    """One subprocess-per-sandbox worker entry.

    ``worker_id`` and ``sandbox_name`` are the same value in Plan A —
    there's a 1:1 mapping between sandbox pods and worker processes.
    The separate field is kept for callers that grep on ``sandbox_name``
    for clarity.
    """
    worker_id: str
    sandbox_name: str
    process: asyncio.subprocess.Process
    soul: str = "general"
    toolsets: list = field(default_factory=list)
    instance_label: str = ""
    requester: str = ""
    status: str = "idle"          # idle | busy | error
    registered_at: float = 0.0
    last_seen: float = 0.0        # updated on every stdout frame we receive
    current_task_id: Optional[str] = None

    # Internal — set by WorkerRegistry.ensure_worker during startup.
    _stdin_lock: Optional[asyncio.Lock] = None
    _reader_task: Optional[asyncio.Task] = None

    @property
    def healthy(self) -> bool:
        """Return True iff the underlying subprocess is still alive.

        Plan A uses subprocess liveness as the definitive health signal —
        if openshell's exec subprocess is still running, the worker
        inside the sandbox is still running (openshell closes the gRPC
        stream when the in-sandbox process exits, which terminates our
        subprocess).
        """
        return self.process.returncode is None

    def to_dict(self) -> dict:
        """UI-facing shape (consumed by admin_handlers.handle_agents_list)."""
        return {
            "worker_id": self.worker_id,
            "sandbox_name": self.sandbox_name,
            "soul": self.soul,
            "toolsets": self.toolsets,
            "instance_label": self.instance_label,
            "requester": self.requester,
            "status": self.status,
            "healthy": self.healthy,
            "uptime_s": int(time.time() - self.registered_at) if self.registered_at else 0,
            "current_task_id": self.current_task_id,
            "pid": self.process.pid,
        }


# ── Registry ───────────────────────────────────────────────────────────────


class WorkerRegistry:
    """Manages subprocess-per-sandbox workers launched via openshell sandbox exec.

    Preserves the public interface of the old WebSocket-based registry
    (``get``, ``list_workers``, ``dispatch_task``, ``.workers``) so
    existing call sites in ``http_api.py``, ``admin_handlers.py``, and
    ``gateway/run.py`` keep working without modification.
    """

    def __init__(self) -> None:
        self._workers: Dict[str, WorkerEntry] = {}
        self._pending_tasks: Dict[str, asyncio.Future] = {}
        self._task_streams: Dict[str, asyncio.Queue] = {}  # task_id → event queue
        # Registry-level lock for ensure_worker — prevents two concurrent
        # spawn attempts racing each other to create the same subprocess.
        self._ensure_lock = asyncio.Lock()

    # ─── Read accessors (backwards-compat with WebSocket version) ───────

    @property
    def workers(self) -> Dict[str, WorkerEntry]:
        return self._workers

    def get(self, worker_id: str) -> Optional[WorkerEntry]:
        return self._workers.get(worker_id)

    def list_workers(self) -> List[dict]:
        return [w.to_dict() for w in self._workers.values()]

    def list_healthy(self) -> List[WorkerEntry]:
        return [w for w in self._workers.values() if w.healthy]

    # ─── Subprocess lifecycle ───────────────────────────────────────────

    async def ensure_worker(
        self,
        sandbox_name: str,
        *,
        soul: str = "general",
        toolsets: Optional[list] = None,
        instance_label: str = "",
        requester: str = "",
        env: Optional[Dict[str, str]] = None,
        worker_script: str = "/app/sandbox_worker.py",
    ) -> WorkerEntry:
        """Launch (or return) the worker subprocess for ``sandbox_name``.

        Idempotent: if a healthy worker is already registered for this
        sandbox, return it unchanged. Otherwise, spawn a new subprocess
        via ``openshell sandbox exec --no-tty --name <sandbox> -- python3
        <worker_script>``, wait for the ``{"type":"ready"}`` line on
        stdout, and register the entry.

        Raises:
            ConnectionError: if the subprocess fails to spawn or the
                             worker doesn't emit ``ready`` within
                             WORKER_READY_TIMEOUT.
        """
        async with self._ensure_lock:
            existing = self._workers.get(sandbox_name)
            if existing is not None and existing.healthy:
                logger.debug(
                    "ensure_worker(%s): healthy existing worker (pid=%d) — reusing",
                    sandbox_name,
                    existing.process.pid,
                )
                return existing
            if existing is not None:
                # Stale dead entry — clean it up before respawning.
                logger.info(
                    "ensure_worker(%s): existing worker is dead (returncode=%s) — replacing",
                    sandbox_name,
                    existing.process.returncode,
                )
                await self._cleanup_worker(existing)

            # Build the command line. Using --no-tty is critical: with a
            # TTY, openshell allocates a PTY and stdin/stdout become
            # non-blocking terminal streams which breaks line-buffered
            # JSON protocol. --no-tty gives us a clean pipe.
            cmd = [
                "openshell",
                "sandbox",
                "exec",
                "--no-tty",
                "--name",
                sandbox_name,
                "--",
                "python3",
                worker_script,
            ]

            # Merge any caller-provided env on top of the gateway's own
            # environment. Callers (OpenShellExecutor.spawn) pass
            # OPENAI_BASE_URL, HERMES_MODEL, HERMES_WORKER_ID, etc.
            subprocess_env = os.environ.copy()
            if env:
                subprocess_env.update(env)

            logger.info(
                "ensure_worker(%s): spawning `%s`",
                sandbox_name,
                " ".join(shlex.quote(c) for c in cmd),
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=subprocess_env,
                )
            except Exception as exc:
                raise ConnectionError(
                    f"Failed to spawn `openshell sandbox exec` for {sandbox_name}: {exc}"
                ) from exc

            now = time.time()
            entry = WorkerEntry(
                worker_id=sandbox_name,
                sandbox_name=sandbox_name,
                process=process,
                soul=soul,
                toolsets=toolsets or [],
                instance_label=instance_label or sandbox_name,
                requester=requester,
                registered_at=now,
                last_seen=now,
                _stdin_lock=asyncio.Lock(),
            )
            self._workers[sandbox_name] = entry

            # Start background tasks: stdout reader (drives the dispatch
            # result futures + stream queues) and stderr drain (keeps the
            # pipe from filling up and dead-locking the subprocess).
            entry._reader_task = asyncio.create_task(
                self._read_stdout_loop(entry),
                name=f"worker-reader-{sandbox_name}",
            )
            asyncio.create_task(
                self._drain_stderr_loop(entry),
                name=f"worker-stderr-{sandbox_name}",
            )

            # Wait for the "ready" event. The worker emits this as its
            # first line on stdout, so by the time _read_stdout_loop sees
            # it, the worker is alive and listening on stdin.
            ready_future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending_tasks[f"__ready__:{sandbox_name}"] = ready_future
            try:
                await asyncio.wait_for(ready_future, timeout=WORKER_READY_TIMEOUT)
                logger.info(
                    "ensure_worker(%s): worker ready (pid=%d, subprocess_pid via openshell)",
                    sandbox_name,
                    process.pid,
                )
                return entry
            except asyncio.TimeoutError:
                logger.warning(
                    "ensure_worker(%s): worker did not emit ready within %.0fs — killing",
                    sandbox_name,
                    WORKER_READY_TIMEOUT,
                )
                await self._cleanup_worker(entry)
                raise ConnectionError(
                    f"Worker for {sandbox_name} did not emit ready within "
                    f"{WORKER_READY_TIMEOUT:.0f}s (see gateway log and "
                    f"`openshell sandbox exec --name {sandbox_name} -- cat /tmp/worker.log`)"
                )
            finally:
                self._pending_tasks.pop(f"__ready__:{sandbox_name}", None)

    async def stop_worker(self, sandbox_name: str, *, graceful: bool = True) -> None:
        """Stop the worker subprocess for ``sandbox_name``.

        If graceful=True (default), sends ``{"type":"shutdown"}`` on
        stdin, waits up to 5s for clean exit, then falls back to
        SIGTERM. Not an error if the worker isn't registered.
        """
        entry = self._workers.pop(sandbox_name, None)
        if entry is None:
            return

        if graceful and entry.process.returncode is None:
            try:
                assert entry._stdin_lock is not None
                async with entry._stdin_lock:
                    if entry.process.stdin and not entry.process.stdin.is_closing():
                        entry.process.stdin.write(b'{"type":"shutdown"}\n')
                        await entry.process.stdin.drain()
                        entry.process.stdin.close()
                try:
                    await asyncio.wait_for(entry.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "stop_worker(%s): graceful shutdown timed out — sending SIGTERM",
                        sandbox_name,
                    )
                    await self._cleanup_worker(entry)
                    return
            except Exception as exc:
                logger.warning(
                    "stop_worker(%s): graceful shutdown failed (%s) — sending SIGTERM",
                    sandbox_name,
                    exc,
                )
                await self._cleanup_worker(entry)
                return
        else:
            await self._cleanup_worker(entry)

    async def _cleanup_worker(self, entry: WorkerEntry) -> None:
        """Force-kill the subprocess and cancel the reader task."""
        if entry.process.returncode is None:
            try:
                entry.process.terminate()
                try:
                    await asyncio.wait_for(entry.process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    entry.process.kill()
                    await entry.process.wait()
            except ProcessLookupError:
                pass  # already dead
            except Exception as exc:
                logger.debug("cleanup terminate failed: %s", exc)
        if entry._reader_task and not entry._reader_task.done():
            entry._reader_task.cancel()
        # Reject any pending tasks for this worker. Matches the old
        # WebSocket registry's #47b5472 fix: if a worker dies mid-dispatch,
        # the user should see the error immediately instead of waiting
        # for the full timeout.
        if entry.current_task_id:
            fut = self._pending_tasks.get(entry.current_task_id)
            if fut and not fut.done():
                fut.set_exception(
                    ConnectionError(
                        f"Worker {entry.worker_id} disconnected before "
                        f"task {entry.current_task_id} completed"
                    )
                )
        self._workers.pop(entry.worker_id, None)

    # ─── Background I/O loops ──────────────────────────────────────────

    async def _read_stdout_loop(self, entry: WorkerEntry) -> None:
        """Consume stdout line-by-line and dispatch JSON messages.

        This is the inverse of the old WebSocket ``handle_ws``. It runs
        as long as the subprocess is alive; when stdout hits EOF it
        cleans up the worker entry and rejects any pending futures.
        """
        assert entry.process.stdout is not None
        stdout = entry.process.stdout
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except Exception as exc:
                    logger.warning(
                        "stdout read failed for %s: %s", entry.worker_id, exc
                    )
                    break
                if not raw:
                    # EOF — subprocess exited.
                    break
                entry.last_seen = time.time()
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "stdout parse failed for %s (%s): %r",
                        entry.worker_id,
                        exc,
                        text[:200],
                    )
                    continue

                self._handle_worker_message(entry, msg)
        finally:
            logger.info(
                "reader loop exiting for %s (returncode=%s)",
                entry.worker_id,
                entry.process.returncode,
            )
            # Ensure we cleanup if we haven't already (e.g., subprocess
            # died on its own, wasn't stopped via stop_worker).
            if self._workers.get(entry.worker_id) is entry:
                await self._cleanup_worker(entry)

    async def _drain_stderr_loop(self, entry: WorkerEntry) -> None:
        """Drain stderr to the gateway's own logger so worker diagnostics
        flow into ~/.logos/logs/unified.jsonl via the M6 pipeline.

        We log at INFO level with a source tag so `logos debug tail
        --filter worker_id=<sandbox>` catches them.
        """
        assert entry.process.stderr is not None
        stderr = entry.process.stderr
        try:
            while True:
                try:
                    raw = await stderr.readline()
                except Exception:
                    break
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    # Log on the gateway side, tagged with the worker id.
                    # This surfaces sandbox-side log lines in the unified
                    # log without needing a separate forwarder (M6 M6.2
                    # structured forwarding is still a separate stretch).
                    logger.info(
                        "[worker:%s] %s",
                        entry.worker_id,
                        text,
                        extra={"worker_id": entry.worker_id},
                    )
        except asyncio.CancelledError:
            pass

    def _handle_worker_message(self, entry: WorkerEntry, msg: Dict[str, Any]) -> None:
        """Dispatch a parsed JSON message from worker stdout.

        Mirrors the old ``handle_ws`` inner dispatch — same message types,
        same routing. ``ready`` is new (replaces the old ``registered``
        from the WS protocol).
        """
        msg_type = msg.get("type")

        if msg_type == "ready":
            # Resolve the one-shot ready future set by ensure_worker.
            ready_key = f"__ready__:{entry.worker_id}"
            fut = self._pending_tasks.get(ready_key)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        if msg_type == "pong":
            # Liveness check responses — nothing to dispatch, last_seen
            # is already updated in the read loop.
            return

        if msg_type == "task_result":
            entry.status = "idle"
            entry.current_task_id = None
            task_id = msg.get("task_id", "")
            fut = self._pending_tasks.get(task_id)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        if msg_type in ("token", "tool_progress", "thinking"):
            task_id = msg.get("task_id", "")
            q = self._task_streams.get(task_id)
            if q:
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass  # drop if consumer is slow
            return

        if msg_type == "error":
            task_id = msg.get("task_id")
            if task_id:
                fut = self._pending_tasks.get(task_id)
                if fut and not fut.done():
                    fut.set_exception(RuntimeError(msg.get("error", "worker error")))
            else:
                logger.warning("worker %s emitted untagged error: %s", entry.worker_id, msg.get("error"))
            return

        logger.debug("worker %s emitted unknown message: %r", entry.worker_id, msg_type)

    # ─── Task dispatch ──────────────────────────────────────────────────

    async def dispatch_task(
        self,
        worker_id: str,
        task: dict,
        timeout: float = DEFAULT_TASK_TIMEOUT,
        on_stream_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> dict:
        """Write a task JSON line to the worker's stdin and await its result.

        Matches the signature of the old WebSocket-based dispatch_task
        so callers (``_handle_chat``, ``run.py`` agent dispatch) don't
        need to change. Intermediate events (token/thinking/tool_progress)
        are forwarded to ``on_stream_event`` as they arrive.

        Raises:
            ConnectionError: worker is not registered or subprocess died.
            TimeoutError: no task_result within ``timeout`` seconds.
            RuntimeError: worker emitted an error message for this task.
        """
        entry = self._workers.get(worker_id)
        if entry is None:
            raise ConnectionError(f"Worker {worker_id} not connected")
        if not entry.healthy:
            raise ConnectionError(
                f"Worker {worker_id} subprocess exited "
                f"(returncode={entry.process.returncode})"
            )
        if entry.status == "busy":
            raise RuntimeError(
                f"Worker {worker_id} is busy with task {entry.current_task_id}"
            )
        assert entry.process.stdin is not None
        if entry.process.stdin.is_closing():
            raise ConnectionError(f"Worker {worker_id} stdin is closed")

        task_id = task.get("task_id", "")
        if not task_id:
            raise ValueError("dispatch_task requires task_id in the task payload")

        # Ensure the worker gets a "type" field. Callers that built the
        # task from the old WebSocket flow pass "type": "run_conversation"
        # — sandbox_worker.py accepts both "task" and "run_conversation"
        # so legacy dispatch code keeps working.
        if "type" not in task:
            task = {"type": "task", **task}

        entry.status = "busy"
        entry.current_task_id = task_id

        result_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_tasks[task_id] = result_future

        stream_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._task_streams[task_id] = stream_queue

        try:
            # Serialise concurrent stdin writes within this worker (the
            # reader task might try to write a shutdown message in
            # parallel with a dispatch — lock to prevent interleaved JSON).
            assert entry._stdin_lock is not None
            async with entry._stdin_lock:
                payload = json.dumps(task).encode("utf-8") + b"\n"
                entry.process.stdin.write(payload)
                await entry.process.stdin.drain()

            # Consume stream events while waiting for the final task_result.
            deadline = asyncio.get_event_loop().time() + timeout
            while not result_future.done():
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Worker {worker_id} did not respond within {timeout}s"
                    )

                # Drain any queued stream events.
                while not stream_queue.empty():
                    event = stream_queue.get_nowait()
                    if on_stream_event is not None:
                        try:
                            await on_stream_event(event)
                        except Exception as exc:
                            logger.debug("on_stream_event raised: %s", exc)

                try:
                    await asyncio.wait_for(
                        asyncio.shield(result_future),
                        timeout=min(0.1, remaining),
                    )
                except asyncio.TimeoutError:
                    continue

            # Drain any trailing events.
            while not stream_queue.empty():
                event = stream_queue.get_nowait()
                if on_stream_event is not None:
                    try:
                        await on_stream_event(event)
                    except Exception as exc:
                        logger.debug("on_stream_event raised: %s", exc)

            return result_future.result()
        finally:
            self._pending_tasks.pop(task_id, None)
            self._task_streams.pop(task_id, None)
            if entry.current_task_id == task_id:
                entry.status = "idle"
                entry.current_task_id = None
