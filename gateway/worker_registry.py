"""
Worker Registry — tracks connected OpenShell sandbox workers.

Every agent runs inside its own OpenShell sandbox and connects back to the
gateway over WebSocket (/ws/worker). This module is the source of truth for
"which sandbox workers are alive" — it replaces the old pattern of listing
k8s deployments or local PIDs AND the even-older in-process "self" worker
that used to run inside the gateway process itself. The gateway is
infrastructure now; it does not advertise itself as a chat-capable agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

HEARTBEAT_TIMEOUT = 90  # seconds — mark unhealthy after this


@dataclass
class WorkerEntry:
    """A connected OpenShell sandbox worker (always remote via WebSocket)."""
    worker_id: str
    ws: web.WebSocketResponse
    soul: str = "general"
    toolsets: list = field(default_factory=list)
    instance_label: str = ""
    requester: str = ""
    status: str = "idle"          # idle | busy | error
    registered_at: float = 0.0
    last_heartbeat: float = 0.0
    current_task_id: Optional[str] = None

    @property
    def healthy(self) -> bool:
        return (time.time() - self.last_heartbeat) < HEARTBEAT_TIMEOUT

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "soul": self.soul,
            "toolsets": self.toolsets,
            "instance_label": self.instance_label,
            "requester": self.requester,
            "status": self.status,
            "healthy": self.healthy,
            "uptime_s": int(time.time() - self.registered_at),
            "current_task_id": self.current_task_id,
        }


class WorkerRegistry:
    """Manages connected workers via WebSocket."""

    def __init__(self):
        self._workers: Dict[str, WorkerEntry] = {}
        self._pending_tasks: Dict[str, asyncio.Future] = {}
        self._task_streams: Dict[str, asyncio.Queue] = {}  # task_id → event queue

    @property
    def workers(self) -> Dict[str, WorkerEntry]:
        return self._workers

    def get(self, worker_id: str) -> Optional[WorkerEntry]:
        return self._workers.get(worker_id)

    def list_workers(self) -> list[dict]:
        return [w.to_dict() for w in self._workers.values()]

    def list_healthy(self) -> list[WorkerEntry]:
        return [w for w in self._workers.values() if w.healthy]

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /ws/worker — called per incoming worker connection."""
        # heartbeat=600 (10 min). The worker uses a custom TunnelWebSocket
        # whose frame parser only runs while the main loop is calling
        # receive_json(). When _handle_task is awaiting an inference call
        # to LM Studio, NO frames are being processed — including aiohttp's
        # WS-level pings. With heartbeat=30, any inference longer than
        # ~30s caused the gateway to close the connection because no pong
        # came back, even though the worker was alive and busy. The user
        # got the response to their first chat (the worker still sent
        # task_result once inference finished) but every subsequent chat
        # silently failed because the WebSocket was already gone.
        #
        # Workers also send their own application-level heartbeat
        # ("type": "heartbeat") every 30s while idle, so the gateway
        # doesn't actually need WebSocket-level keepalive. The 600s
        # ceiling is a safety net for genuinely-stuck connections.
        ws = web.WebSocketResponse(heartbeat=600)
        await ws.prepare(request)

        worker_id = None
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue

                    msg_type = data.get("type")

                    if msg_type == "register":
                        worker_id = data.get("worker_id", "")
                        if not worker_id:
                            await ws.send_json({"type": "error", "message": "worker_id required"})
                            continue

                        now = time.time()
                        entry = WorkerEntry(
                            worker_id=worker_id,
                            ws=ws,
                            soul=data.get("soul", "general"),
                            toolsets=data.get("toolsets", []),
                            instance_label=data.get("instance_label", ""),
                            requester=data.get("requester", ""),
                            registered_at=now,
                            last_heartbeat=now,
                        )
                        self._workers[worker_id] = entry
                        logger.info("Worker registered: %s (soul=%s)", worker_id, entry.soul)
                        await ws.send_json({
                            "type": "registered",
                            "worker_id": worker_id,
                        })

                    elif msg_type == "heartbeat":
                        if worker_id and worker_id in self._workers:
                            w = self._workers[worker_id]
                            w.last_heartbeat = time.time()
                            w.status = data.get("status", w.status)

                    elif msg_type == "task_result":
                        if worker_id and worker_id in self._workers:
                            w = self._workers[worker_id]
                            w.status = "idle"
                            w.current_task_id = None
                            # Resolve the pending dispatch_task future
                            task_id = data.get("task_id", "")
                            fut = self._pending_tasks.get(task_id)
                            if fut and not fut.done():
                                fut.set_result(data)

                    elif msg_type in ("token", "tool_progress", "thinking"):
                        # Stream events to the waiting gateway consumer
                        task_id = data.get("task_id", "")
                        q = self._task_streams.get(task_id)
                        if q:
                            try:
                                q.put_nowait(data)
                            except asyncio.QueueFull:
                                pass  # drop if consumer is slow

                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break

        except Exception as exc:
            logger.warning("Worker WebSocket error for %s: %s", worker_id, exc)
        finally:
            if worker_id and worker_id in self._workers:
                del self._workers[worker_id]
                logger.info("Worker disconnected: %s", worker_id)

        return ws

    async def dispatch_task(
        self, worker_id: str, task: dict, timeout: float = 300,
        on_stream_event=None,
    ) -> dict:
        """Dispatch a task to an OpenShell sandbox worker and await the result.

        Sends the task payload over the worker's WebSocket and streams
        intermediate events (tokens, tool_start/end, thinking) through
        *on_stream_event* while waiting for the final task_result message.
        """
        entry = self._workers.get(worker_id)
        if not entry:
            raise ConnectionError(f"Worker {worker_id} not connected")
        if not entry.ws or entry.ws.closed:
            raise ConnectionError(f"Worker {worker_id} WebSocket closed")
        if entry.status == "busy":
            raise RuntimeError(f"Worker {worker_id} is busy with task {entry.current_task_id}")

        task_id = task.get("task_id", "")
        entry.status = "busy"
        entry.current_task_id = task_id

        result_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_tasks[task_id] = result_future

        # Set up stream queue for intermediate events
        stream_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._task_streams[task_id] = stream_queue

        try:
            await entry.ws.send_json(task)

            # Consume stream events while waiting for final result
            deadline = asyncio.get_event_loop().time() + timeout
            while not result_future.done():
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"Worker {worker_id} did not respond within {timeout}s")

                # Drain any queued stream events
                while not stream_queue.empty():
                    event = stream_queue.get_nowait()
                    if on_stream_event:
                        try:
                            await on_stream_event(event)
                        except Exception:
                            pass

                # Short wait for more events or the final result
                try:
                    await asyncio.wait_for(
                        asyncio.shield(result_future),
                        timeout=min(0.1, remaining),
                    )
                except asyncio.TimeoutError:
                    continue  # not done yet, keep polling

            # Drain any remaining events
            while not stream_queue.empty():
                event = stream_queue.get_nowait()
                if on_stream_event:
                    try:
                        await on_stream_event(event)
                    except Exception:
                        pass

            return result_future.result()
        finally:
            self._pending_tasks.pop(task_id, None)
            self._task_streams.pop(task_id, None)
            entry.status = "idle"
            entry.current_task_id = None

    async def send_to_worker(self, worker_id: str, message: dict) -> bool:
        """Send a message to a specific worker. Returns False if not connected."""
        entry = self._workers.get(worker_id)
        if not entry or entry.ws.closed:
            return False
        try:
            await entry.ws.send_json(message)
            return True
        except Exception:
            return False

    async def broadcast(self, message: dict):
        """Send a message to all connected workers."""
        for entry in list(self._workers.values()):
            if not entry.ws.closed:
                try:
                    await entry.ws.send_json(message)
                except Exception:
                    pass
