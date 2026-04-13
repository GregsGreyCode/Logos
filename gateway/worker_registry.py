"""
Worker Registry — per-task dispatch via ``openshell sandbox exec``.

Plan A-prime architecture (TASKS.md #24): instead of keeping a long-
running subprocess per sandbox (the original Plan A, which hit a hard
wall on ``openshell sandbox exec --no-tty`` refusing to start the
in-sandbox process until stdin reaches EOF), we spawn a fresh
``openshell sandbox exec`` subprocess for every task dispatch:

    1. ``openshell sandbox exec --no-tty --name <sandbox> --
       python3 /app/sandbox_worker.py``
    2. Write the task JSON to stdin + **close stdin** (the EOF is what
       unblocks openshell's exec gate so the in-sandbox process
       actually starts — proven empirically).
    3. Read JSON lines from stdout; dispatch streaming events
       (``token``, ``thinking``, ``tool_progress``) to the caller's
       ``on_stream_event`` callback as they arrive.
    4. Collect the ``task_result`` frame as the final result.
    5. Wait for the subprocess to exit (the in-sandbox python returns
       after emitting ``task_result``) and return the result.

Cold-start tax: ~0.2s per dispatch for python import + config load.
Negligible compared to 2–30s inference calls.

Why not a persistent worker
────────────────────────────
Directly tested — ``openshell sandbox exec --no-tty`` blocks the
in-sandbox command until stdin reaches EOF. Writing bytes to stdin
without closing the pipe does NOT unblock it. That makes any design
that keeps stdin open for ongoing task delivery physically
impossible on this transport. The persistent-worker variant of this
module sat spinning for 60 seconds before timing out on every single
spawn. One-shot-per-task matches the primitive's actual contract.

Public interface (preserved from the prior persistent-worker
registry so external call sites don't change):

    worker_registry.workers                   # always {} (stateless)
    worker_registry.get(sandbox_name)         # Optional[_SandboxHealthEntry]
    worker_registry.list_workers()            # list[dict]
    worker_registry.list_healthy()            # list[dict]
    await worker_registry.dispatch_task(sandbox_name, task, timeout, on_stream_event)

Health reporting: ``get()`` / ``list_workers()`` now read from the
executor state file (``~/.logos/openshell_instances.json``) rather
than an in-memory subprocess map. "Healthy" means the sandbox CR is
in ``phase == "ready"`` — the subprocess lifetime is ephemeral, so
there's nothing else to check. A future improvement (MISSING.md M7)
can query ``openshell sandbox list`` live for a richer phase field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default dispatch budget — a task has up to this many seconds to
# return its ``task_result`` frame. Matches the patched openshell-router
# 600s ceiling (TASKS.md #22) so the whole chain agrees on the upper
# bound for an inference call.
DEFAULT_TASK_TIMEOUT = 600.0


# ── Health entry shim ─────────────────────────────────────────────────────
#
# The old persistent-worker ``WorkerEntry`` exposed ``.healthy``,
# ``.status``, ``.toolsets``, ``.soul`` etc. so callers (admin_handlers,
# http_api chat dispatch) could poll per-worker state. Post-refactor
# there is no persistent worker object — health is sourced from the
# executor state file. This shim wraps a state-file dict and serves
# the same attribute surface so the callers don't need to change.

@dataclass
class _SandboxHealthEntry:
    """Read-only view on a sandbox's state-file entry.

    Preserves the attribute surface that http_api chat dispatch and
    admin_handlers.handle_agents_list expect from a legacy WorkerEntry
    object. ``.healthy`` is True iff the sandbox is in ``phase ==
    "ready"``; transient "provisioning" entries read as unhealthy.
    """
    _state_entry: dict

    @property
    def worker_id(self) -> str:
        return self._state_entry.get("worker_id", "") or self._state_entry.get("sandbox_name", "")

    @property
    def sandbox_name(self) -> str:
        return self._state_entry.get("sandbox_name", "")

    @property
    def healthy(self) -> bool:
        return self._state_entry.get("phase") == "ready"

    @property
    def status(self) -> str:
        return "idle" if self.healthy else (self._state_entry.get("phase") or "unknown")

    @property
    def soul(self) -> str:
        return self._state_entry.get("soul_name") or "general"

    @property
    def toolsets(self) -> list:
        return self._state_entry.get("toolsets") or []

    @property
    def instance_label(self) -> str:
        return self._state_entry.get("name", "")

    @property
    def requester(self) -> str:
        return self._state_entry.get("requester", "")

    @property
    def registered_at(self) -> float:
        """Unix timestamp when the sandbox record was written to the
        state file. Back-compat with the old persistent-worker API —
        http_api uses it as a cache-buster / incarnation tag so the
        frontend can detect sandbox restarts."""
        val = self._state_entry.get("created_at")
        try:
            return float(val) if val else 0.0
        except (TypeError, ValueError):
            return 0.0

    def to_dict(self) -> dict:
        """UI-facing shape consumed by admin_handlers.handle_agents_list."""
        created = self._state_entry.get("created_at") or 0
        return {
            "worker_id": self.worker_id,
            "sandbox_name": self.sandbox_name,
            "soul": self.soul,
            "toolsets": self.toolsets,
            "instance_label": self.instance_label,
            "requester": self.requester,
            "status": self.status,
            "healthy": self.healthy,
            "uptime_s": int(time.time() - created) if created else 0,
            "current_task_id": None,
            "pid": None,
        }


# ── Registry ──────────────────────────────────────────────────────────────


class WorkerRegistry:
    """Stateless dispatcher — spawns one ``openshell sandbox exec``
    subprocess per task. Keeps no persistent per-sandbox state.

    Health queries (``get``/``list_workers``) are answered from the
    executor state file so the API surface matches the old persistent
    registry without maintaining a parallel cache.
    """

    def __init__(self) -> None:
        # No persistent worker state. Old call sites that introspected
        # ``.workers`` got a dict — we reconstruct that on demand from
        # the executor state file.
        #
        # ``_active_tasks`` is an in-flight-dispatch counter keyed by
        # sandbox_name. Incremented at the top of ``dispatch_task``,
        # decremented in its ``finally`` block. Exposed via
        # ``active_task_count()`` so admin_handlers can surface
        # "agent is currently processing N tasks" to the UI — which
        # the world view uses to render a thought-bubble indicator
        # (MISSING.md — Phase A of the dispatch-activity tracking
        # work). A ``defaultdict(int)`` would work but the plain dict
        # + explicit ``get(..., 0)`` keeps the semantics obvious: the
        # dict only holds non-zero entries; a missing key means zero
        # in-flight tasks for that sandbox.
        self._active_tasks: Dict[str, int] = {}

    # ─── Read accessors (back-compat with old persistent-worker API) ────

    @property
    def workers(self) -> Dict[str, _SandboxHealthEntry]:
        """Return a dict of every state-file sandbox, keyed by worker_id.

        Previously this was the in-memory map of subprocess-per-sandbox
        worker entries. Post-refactor every value is a read-only shim
        over the state-file row.
        """
        from gateway.executors.openshell import _load_state
        out: Dict[str, _SandboxHealthEntry] = {}
        try:
            for inst in _load_state():
                key = inst.get("worker_id") or inst.get("sandbox_name") or ""
                if key:
                    out[key] = _SandboxHealthEntry(inst)
        except Exception as exc:
            logger.warning("worker_registry.workers: load_state failed: %s", exc)
        return out

    def get(self, worker_id: str) -> Optional[_SandboxHealthEntry]:
        """Look up a sandbox by worker_id / sandbox_name. Returns None
        if the state file has no matching entry (agent not yet spawned,
        or the entry was pruned)."""
        from gateway.executors.openshell import _load_state
        try:
            for inst in _load_state():
                if (inst.get("worker_id") == worker_id
                        or inst.get("sandbox_name") == worker_id):
                    return _SandboxHealthEntry(inst)
        except Exception as exc:
            logger.warning("worker_registry.get(%r): load_state failed: %s", worker_id, exc)
        return None

    def list_workers(self) -> List[dict]:
        return [e.to_dict() for e in self.workers.values()]

    def list_healthy(self) -> List[_SandboxHealthEntry]:
        return [e for e in self.workers.values() if e.healthy]

    def active_task_count(self, sandbox_name: str) -> int:
        """Return the number of dispatches currently in flight against
        the given sandbox.

        Used by ``admin_handlers.handle_agents_list`` to expose
        ``active_tasks`` per-agent in ``/admin/agents`` responses so
        the world view can render a thought-bubble animation on
        agents that are currently processing a task.
        """
        return self._active_tasks.get(sandbox_name, 0)

    # ─── Task dispatch ──────────────────────────────────────────────────

    async def dispatch_task(
        self,
        worker_id: str,
        task: dict,
        timeout: float = DEFAULT_TASK_TIMEOUT,
        on_stream_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> dict:
        """Spawn a fresh ``openshell sandbox exec`` subprocess, pipe the
        task to its stdin, close stdin, and stream stdout back until the
        subprocess exits. Return the final ``task_result`` frame.

        ``worker_id`` is the OpenShell sandbox name (in Plan A-prime the
        worker_id and sandbox_name are the same; the separate parameter
        is kept for signature compatibility with the old persistent
        registry).

        Raises:
            ConnectionError: if the subprocess fails to spawn (e.g.
                             openshell CLI missing, sandbox not found).
            TimeoutError:    if no ``task_result`` arrives within ``timeout``.
            RuntimeError:    if the worker exits without emitting a
                             ``task_result`` frame at all, or emits an
                             error terminal frame.
        """
        sandbox_name = worker_id  # one-to-one mapping
        task_id = task.get("task_id", "")
        if not task_id:
            raise ValueError("dispatch_task requires task_id in the task payload")

        # Mark this sandbox as "actively processing a task" for the
        # duration of the dispatch. See active_task_count() for the
        # rationale — admin_handlers surfaces this counter to the
        # frontend which uses it to render a thought-bubble animation
        # on agents that are currently working. Decrement in the outer
        # finally so every exit path (success, error, cancel) clears
        # the count correctly.
        self._active_tasks[sandbox_name] = self._active_tasks.get(sandbox_name, 0) + 1

        try:
            return await self._dispatch_task_impl(
                sandbox_name, task_id, task, timeout, on_stream_event,
            )
        finally:
            remaining = self._active_tasks.get(sandbox_name, 1) - 1
            if remaining <= 0:
                self._active_tasks.pop(sandbox_name, None)
            else:
                self._active_tasks[sandbox_name] = remaining

    async def _dispatch_task_impl(
        self,
        sandbox_name: str,
        task_id: str,
        task: dict,
        timeout: float,
        on_stream_event: Optional[Callable[[Dict[str, Any]], Any]],
    ) -> dict:
        """Internal implementation of dispatch_task — the actual
        subprocess-per-task machinery. Split out from the public
        ``dispatch_task`` so the entry/exit counter update in the
        outer wrapper stays readable and can't be accidentally
        bypassed by an early return in the middle of the body."""
        # CRITICAL: look up which OpenShell gateway this sandbox lives
        # inside, and pass it via -g to the CLI. Without -g, the CLI
        # uses whatever gateway is "currently selected" in
        # ~/.config/openshell/gateways/ — which is whichever one was
        # provisioned / selected most recently. That means a dispatch
        # to hermes-tali (living in qwen-qwen3-5-9b) would actually
        # run against openai-gpt-oss-20b if that was the last gateway
        # the user added, and openshell returns
        # ``status: NotFound, message: "sandbox not found"`` because
        # the target sandbox isn't in the selected cluster.
        #
        # This bit us in the wild on 2026-04-11: agent Tali chats
        # worked fine until agent Grace was re-bound to a new route,
        # at which point openai-gpt-oss-20b became the active gateway
        # and every Tali dispatch failed with NotFound. Grace worked
        # by coincidence because her sandbox happened to be in the
        # now-active gateway.
        target_gateway = self._resolve_sandbox_gateway(sandbox_name)
        if target_gateway is None:
            raise ConnectionError(
                f"dispatch_task({sandbox_name}): no gateway resolvable "
                f"for this sandbox. Not in the state file, no default "
                f"route configured, /setup may not have run yet."
            )

        # Build the exec command. --no-tty is critical — with a TTY,
        # openshell allocates a PTY and stdout becomes a terminal which
        # breaks JSON-line framing. -g forces exec into the correct
        # cluster regardless of whichever gateway is CLI-selected.
        cmd = [
            "openshell",
            "-g",
            target_gateway,
            "sandbox",
            "exec",
            "--no-tty",
            "--name",
            sandbox_name,
            "--",
            "bash", "-c",
            "HERMES_HOME=/tmp/hermes exec python3 /tmp/sandbox_worker.py",
        ]

        logger.debug(
            "dispatch_task(%s): spawning `%s` for task=%s",
            sandbox_name, " ".join(shlex.quote(c) for c in cmd), task_id,
        )

        # Normalise the task shape — sandbox_worker.py accepts either
        # "task" or the legacy "run_conversation" type, but default to
        # "task" for new dispatches.
        if "type" not in task:
            task = {"type": "task", **task}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ConnectionError(
                f"openshell CLI not found on PATH — cannot dispatch to {sandbox_name}"
            ) from exc
        except Exception as exc:
            raise ConnectionError(
                f"Failed to spawn `openshell sandbox exec` for {sandbox_name}: {exc}"
            ) from exc

        # Pipe the task + close stdin. **This close is critical.**
        # openshell's exec primitive waits for stdin EOF before
        # invoking the in-sandbox process; without it the subprocess
        # sits in a gRPC wait state forever and our stdout read hangs.
        try:
            assert proc.stdin is not None
            payload = json.dumps(task).encode("utf-8") + b"\n"
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception as exc:
            await self._cleanup_proc(proc)
            raise ConnectionError(
                f"Failed to pipe task to {sandbox_name}: {exc}"
            ) from exc

        # Drain stderr in parallel so the subprocess's pipe doesn't
        # fill up and dead-lock the process. Tag lines with the
        # sandbox name so they flow into the unified log correctly.
        stderr_task = asyncio.create_task(
            self._drain_stderr_loop(proc, sandbox_name),
            name=f"worker-stderr-{sandbox_name}",
        )

        final_result: Optional[dict] = None
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            assert proc.stdout is not None
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Task {task_id} on {sandbox_name} did not "
                        f"return task_result within {timeout}s"
                    )
                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"Task {task_id} on {sandbox_name} timed out "
                        f"waiting for the next stdout line ({timeout}s)"
                    )
                if not line_bytes:
                    # stdout EOF — subprocess has exited (or closed stdout).
                    break
                text = line_bytes.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "dispatch_task(%s) malformed stdout line (%s): %r",
                        sandbox_name, exc, text[:200],
                    )
                    continue

                msg_type = msg.get("type")
                if msg_type == "task_result":
                    final_result = msg
                    # keep reading — the worker should exit immediately
                    # after emitting task_result, so the next readline
                    # should return b"" (EOF).
                    continue

                if msg_type == "ready":
                    # Sanity line from the worker's first emit. Forward
                    # as a stream event so on_stream_event callers that
                    # want to surface "worker started" can do so, but
                    # it's optional — most callers ignore it.
                    if on_stream_event is not None:
                        try:
                            await on_stream_event(msg)
                        except Exception as exc:
                            logger.debug("on_stream_event(ready) raised: %s", exc)
                    continue

                # Stream-relay frames: forward the full envelope to the
                # SSE client so the chat UI's live-tool panel can render
                # tool_start / tool_end (with previews + duration_ms) and
                # the streaming token / thinking ticker can update.
                #
                # tool_start and tool_end were dropped from this list
                # during the Plan A-prime per-task exec refactor (the
                # previous persistent-worker WS forwarded everything
                # generically); re-adding them here restores the live
                # execution display in the chat header.
                if msg_type in (
                    "token",
                    "thinking",
                    "tool_progress",
                    "tool_start",
                    "tool_end",
                ):
                    if on_stream_event is not None:
                        try:
                            await on_stream_event(msg)
                        except Exception as exc:
                            logger.debug(
                                "on_stream_event(%s) raised: %s", msg_type, exc,
                            )
                    continue

                if msg_type == "error":
                    # Legacy error frame (non-terminal). Synthesize a
                    # task_result with the error so callers get a
                    # terminal frame.
                    final_result = {
                        "type": "task_result",
                        "task_id": task_id,
                        "status": "error",
                        "final_response": "",
                        "error": msg.get("error", "worker emitted error"),
                    }
                    continue

                logger.debug(
                    "dispatch_task(%s) unknown stdout type %r — ignoring",
                    sandbox_name, msg_type,
                )

            # Wait for the subprocess to exit cleanly.
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "dispatch_task(%s) subprocess didn't exit within 5s — killing",
                    sandbox_name,
                )
                await self._cleanup_proc(proc)
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except Exception:
                pass

        if final_result is None:
            raise RuntimeError(
                f"Worker for {sandbox_name} exited (returncode="
                f"{proc.returncode}) without emitting task_result. "
                f"Check `openshell sandbox exec --name {sandbox_name} "
                f"-- cat /tmp/worker.jsonl` for the in-sandbox trace."
            )

        # NB: we do NOT raise on status=error — the http_api chat
        # dispatcher expects a dict back with status/error fields so
        # it can render the failure as an in-chat error bubble rather
        # than an opaque 500. Only the "no frame at all" case above is
        # a hard failure worth raising.

        # ── Sync memories back from sandbox to host ──────────────
        #
        # After each task, download the agent's /tmp/hermes/memories/
        # directory to the per-agent host directory so memories persist
        # across sandbox restarts. Best-effort — download failure must
        # not block the chat response.
        try:
            await self._sync_memories_from_sandbox(
                sandbox_name, target_gateway,
            )
        except Exception as exc:
            logger.debug(
                "dispatch_task(%s): memory sync-back failed (non-fatal): %s",
                sandbox_name, exc,
            )

        # ── Cost log ────────────────────────────────────────────────────
        # Write one row per dispatch with token usage + derived USD cost.
        # Never raises — a logging failure must never block a reply.
        try:
            self._record_cost_entry(
                final_result=final_result,
                sandbox_name=sandbox_name,
                task_id=task_id,
                session_id=session_id,
            )
        except Exception as exc:
            logger.debug(
                "dispatch_task(%s): cost log write failed (non-fatal): %s",
                sandbox_name, exc,
            )

        return final_result

    def _record_cost_entry(
        self,
        final_result: Optional[dict],
        sandbox_name: str,
        task_id: str,
        session_id: Optional[str],
    ) -> None:
        """Write a cost_log row from a completed task's usage stats.

        Reads token counts from ``final_result.usage`` (populated by
        sandbox_worker after ``agent.run_conversation`` returns), looks
        up pricing via gateway.pricing (OpenRouter-sourced), and inserts
        into auth.db's cost_log table. Local models (lmstudio/ollama)
        still get a row with cost=0 so the dashboard can show activity
        volume without implying $0 means "ran for free but not counted".
        """
        if not isinstance(final_result, dict):
            return
        usage = final_result.get("usage") or {}
        model = usage.get("model") or ""
        if not model:
            return

        # Resolve agent identity for the row. sandbox_name is "hermes-<Name>";
        # match to the agent record via name lookup (best-effort).
        agent_id = None
        agent_name = None
        provider = None
        try:
            from gateway.auth import db as _adb
            if sandbox_name.startswith("hermes-"):
                agent_name = sandbox_name[len("hermes-"):]
                agent_row = _adb.get_agent_by_name(agent_name)
                if agent_row:
                    agent_id = agent_row.get("id")
                    route_id = agent_row.get("model_route_id")
                    if route_id:
                        route = _adb.get_model_route(route_id)
                        provider = (route or {}).get("provider")
        except Exception:
            pass

        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_tokens", 0) or 0)
        cache_write = int(usage.get("cache_write_tokens", 0) or 0)

        cost = None
        pricing_known = False
        if input_tokens or output_tokens or cache_read or cache_write:
            try:
                from gateway import pricing as _pricing
                cost = _pricing.cost_for_usage(
                    model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                )
                pricing_known = cost is not None
            except Exception as exc:
                logger.debug("cost lookup failed for %s: %s", model, exc)

        from gateway.auth import db as _adb
        _adb.insert_cost_entry(
            agent_id=agent_id,
            agent_name=agent_name,
            session_id=session_id,
            task_id=task_id,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost or 0.0,
            pricing_known=pricing_known,
        )

    # ─── Sandbox state sync ──────────────────────────────────────────────
    #
    # Three tiers of data sync from sandbox → gateway host:
    #   1. Memories  — synced after dispatch, only when files changed (mtime)
    #   2. Logs      — synced every 30 minutes by background task
    #   3. Sessions  — synced every 60 minutes by background task
    #
    # All syncs are best-effort: failures never block chat responses.

    # Track last-known mtime per sandbox for change detection
    _memory_mtimes: Dict[str, float] = {}

    def _download_sandbox_dir(
        self,
        sandbox_name: str,
        gateway: str,
        sandbox_path: str,
        host_dir: Path,
    ) -> bool:
        """Download a directory from a sandbox to the host. Returns True on success."""
        host_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "openshell", "-g", gateway,
                "sandbox", "download",
                sandbox_name, sandbox_path,
                str(host_dir),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0

    def _resolve_agent_info(self, sandbox_name: str):
        """Return (agent_name, gateway) for a sandbox, or (None, None)."""
        from gateway.executors.openshell import _load_state
        for inst in _load_state():
            if inst.get("sandbox_name") == sandbox_name:
                return inst.get("name"), inst.get("openshell_name", "")
        return None, None

    def _agent_host_dir(self, agent_name: str) -> Path:
        """Return the per-agent host directory: ~/.logos/agents/<name>/"""
        from gateway.executors.openshell import _HERMES_HOME
        return _HERMES_HOME / "agents" / agent_name

    async def _sync_memories_from_sandbox(
        self,
        sandbox_name: str,
        gateway: str,
    ) -> None:
        """Sync memories only if the sandbox has newer files than last sync.

        Uses ``openshell sandbox exec -- stat`` to check the mtime of the
        memories directory. Only downloads if mtime is newer than the last
        recorded sync. This avoids downloading an empty/unchanged directory
        on every dispatch — memories only sync when the agent actually
        wrote to them via the memory tool.
        """
        agent_name, _ = self._resolve_agent_info(sandbox_name)
        if not agent_name:
            return

        # Check mtime of the memories directory inside the sandbox
        try:
            stat_result = await asyncio.to_thread(
                subprocess.run,
                [
                    "openshell", "-g", gateway,
                    "sandbox", "exec", "--no-tty",
                    "--name", sandbox_name, "--",
                    "stat", "-c", "%Y", "/tmp/hermes/memories/",
                ],
                capture_output=True, text=True, timeout=10,
                input="",
            )
            if stat_result.returncode != 0:
                return
            current_mtime = float(stat_result.stdout.strip())
        except Exception:
            return

        last_mtime = self._memory_mtimes.get(sandbox_name, 0.0)
        if current_mtime <= last_mtime:
            return  # no changes since last sync

        host_dir = self._agent_host_dir(agent_name) / "memories"
        try:
            ok = await asyncio.to_thread(
                self._download_sandbox_dir,
                sandbox_name, gateway,
                "/tmp/hermes/memories/", host_dir,
            )
            if ok:
                self._memory_mtimes[sandbox_name] = current_mtime
                n_files = len([f for f in host_dir.iterdir() if f.is_file()])
                if n_files > 0:
                    logger.info(
                        "memory sync: %s → %s (%d file(s))",
                        sandbox_name, host_dir, n_files,
                    )
        except Exception as exc:
            logger.debug("memory sync: failed for %s: %s", sandbox_name, exc)

    async def _periodic_sync_loop(
        self,
        sync_type: str,
        sandbox_path: str,
        host_subdir: str,
        interval_seconds: int,
    ) -> None:
        """Background loop that syncs a sandbox directory for all agents on a schedule.

        Args:
            sync_type: Label for logging (e.g. "logs", "sessions")
            sandbox_path: Path inside sandbox (e.g. "/tmp/hermes/logs/")
            host_subdir: Subdirectory under ~/.logos/agents/<name>/ (e.g. "logs")
            interval_seconds: Seconds between sync cycles
        """
        await asyncio.sleep(60)  # initial delay — let gateway fully start
        while True:
            try:
                from gateway.executors.openshell import _load_state
                for inst in _load_state():
                    if inst.get("phase") != "ready":
                        continue
                    sandbox_name = inst.get("sandbox_name", "")
                    agent_name = inst.get("name", "")
                    gw = inst.get("openshell_name", "")
                    if not sandbox_name or not agent_name or not gw:
                        continue
                    host_dir = self._agent_host_dir(agent_name) / host_subdir
                    try:
                        ok = await asyncio.to_thread(
                            self._download_sandbox_dir,
                            sandbox_name, gw, sandbox_path, host_dir,
                        )
                        if ok:
                            n = len([f for f in host_dir.iterdir() if f.is_file()])
                            if n > 0:
                                logger.debug(
                                    "%s sync: %s → %s (%d file(s))",
                                    sync_type, sandbox_name, host_dir, n,
                                )
                    except Exception as exc:
                        logger.debug(
                            "%s sync: failed for %s: %s",
                            sync_type, sandbox_name, exc,
                        )
            except Exception as exc:
                logger.debug("periodic %s sync cycle failed: %s", sync_type, exc)
            await asyncio.sleep(interval_seconds)

    def start_background_sync_tasks(self) -> None:
        """Launch the periodic log and session sync background tasks.

        Called once at gateway startup from start_http_api().
        """
        asyncio.create_task(
            self._periodic_sync_loop(
                sync_type="logs",
                sandbox_path="/tmp/hermes/logs/",
                host_subdir="logs",
                interval_seconds=1800,  # 30 minutes
            ),
            name="sandbox-sync-logs",
        )
        asyncio.create_task(
            self._periodic_sync_loop(
                sync_type="sessions",
                sandbox_path="/tmp/hermes/sessions/",
                host_subdir="sessions",
                interval_seconds=3600,  # 1 hour
            ),
            name="sandbox-sync-sessions",
        )

    # ─── Gateway routing helper ─────────────────────────────────────────

    def _resolve_sandbox_gateway(self, sandbox_name: str) -> Optional[str]:
        """Look up which OpenShell gateway a sandbox lives in.

        Resolution order:
          1. Exact match in the executor state file
             (``~/.logos/openshell_instances.json``). This is the
             source of truth for multi-gateway installs — each
             state-file entry carries its ``openshell_name``.
          2. Match by agent name in ``auth.db.agents`` → the bound
             ``model_route_id`` → ``model_routes.openshell_name``.
             Covers the case where the state file got pruned but
             the agent row still exists.
          3. Default route from ``get_default_gateway_name()``. Only
             a sane answer if there's exactly one route configured;
             otherwise the caller should have gotten a state-file
             hit via the agent name.
          4. ``None`` — caller raises a clear error.

        The key reason this helper exists: ``openshell sandbox exec``
        without ``-g`` uses whatever gateway is CLI-selected
        (``~/.config/openshell/gateways/current``), which is whichever
        one was added most recently. That means a dispatch to a
        sandbox in gateway A silently targets gateway B if B was
        provisioned more recently — and openshell returns
        ``status: NotFound, message: "sandbox not found"``.
        Looking up the target gateway explicitly and passing ``-g``
        makes dispatch routing deterministic regardless of CLI state.
        """
        # 1. State file lookup
        try:
            from gateway.executors.openshell import _load_state
            for inst in _load_state():
                if (inst.get("sandbox_name") == sandbox_name
                        or inst.get("worker_id") == sandbox_name):
                    gw = inst.get("openshell_name")
                    if gw:
                        return gw
        except Exception as exc:
            logger.warning(
                "_resolve_sandbox_gateway: load_state failed: %s", exc,
            )

        # 2. Agent row lookup by inferred agent name.
        # Sandbox names are ``hermes-<sanitized-agent-name>`` (see
        # OpenShellExecutor._sanitize_sandbox_name), so strip the
        # prefix and try the sanitized match against agent rows.
        try:
            from gateway.auth import db as auth_db
            from gateway.executors.openshell import _sanitize_sandbox_name
            prefix = "hermes-"
            if sandbox_name.startswith(prefix):
                base = sandbox_name[len(prefix):]
                for agent in auth_db.list_agents():
                    if _sanitize_sandbox_name(f"hermes-{agent.get('name', '')}") == sandbox_name:
                        route_id = agent.get("model_route_id")
                        if route_id:
                            route = auth_db.get_model_route(route_id)
                            if route and route.get("openshell_name"):
                                return route["openshell_name"]
        except Exception as exc:
            logger.warning(
                "_resolve_sandbox_gateway: agent/route lookup failed: %s", exc,
            )

        # 3. Default route fallback (single-gateway install)
        try:
            from gateway.openshell_routes import get_default_gateway_name
            default_gw = get_default_gateway_name()
            if default_gw:
                return default_gw
        except Exception as exc:
            logger.warning(
                "_resolve_sandbox_gateway: default gateway lookup failed: %s", exc,
            )

        return None

    # ─── Subprocess lifecycle helpers ───────────────────────────────────

    async def _drain_stderr_loop(
        self,
        proc: asyncio.subprocess.Process,
        sandbox_name: str,
    ) -> None:
        """Forward subprocess stderr line-by-line to the gateway logger.

        Tagging each line with the sandbox name means
        ``logos debug tail --filter worker_id=hermes-<agent>`` catches
        them. Runs until stderr EOF or the task is cancelled.
        """
        assert proc.stderr is not None
        try:
            while True:
                try:
                    line = await proc.stderr.readline()
                except Exception:
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.info(
                        "[worker:%s] %s",
                        sandbox_name, text,
                        extra={"worker_id": sandbox_name},
                    )
        except asyncio.CancelledError:
            pass

    async def _cleanup_proc(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Best-effort kill of a subprocess on error/timeout paths."""
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.debug("_cleanup_proc: unexpected error: %s", exc)
