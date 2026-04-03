"""
Agent Worker — lightweight headless agent process.

Connects to the main Logos gateway via WebSocket, registers itself, and
waits for task dispatches.  Runs AIAgent in response to tasks and streams
results back to the gateway.

Usage:
    logos worker run --connect ws://gateway:8080/ws/worker --name my-agent

Phase 1: registration + heartbeat only.
Phase 2: task dispatch + AIAgent execution.
Phase 3: token streaming + tool progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class AgentWorker:
    """Headless agent worker that connects to a Logos gateway."""

    def __init__(
        self,
        gateway_url: str,
        worker_id: str,
        soul: str = "general",
        toolsets: list | None = None,
        instance_label: str = "",
        requester: str = "",
        hermes_home: str | None = None,
    ):
        self.gateway_url = gateway_url
        self.worker_id = worker_id
        self.soul = soul
        self.toolsets = toolsets or []
        self.instance_label = instance_label
        self.requester = requester
        self.hermes_home = Path(hermes_home) if hermes_home else Path(
            os.getenv("HERMES_HOME", Path.home() / ".hermes")
        )
        self._ws = None
        self._running = True
        self._status = "idle"
        self._reconnect_delay = 1  # exponential backoff

    async def run(self):
        """Main loop — connect, register, heartbeat, reconnect on failure."""
        # Handle signals
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown)

        logger.info("Worker %s starting — connecting to %s", self.worker_id, self.gateway_url)

        while self._running:
            try:
                await self._connect_and_run()
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "Worker connection failed: %s — reconnecting in %ds",
                    exc, self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30)

        logger.info("Worker %s shut down.", self.worker_id)

    async def _connect_and_run(self):
        """Single connection lifecycle: connect → register → message loop."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.gateway_url, heartbeat=30) as ws:
                self._ws = ws
                self._reconnect_delay = 1  # reset backoff on successful connect
                logger.info("Connected to gateway")

                # Register
                await ws.send_json({
                    "type": "register",
                    "worker_id": self.worker_id,
                    "soul": self.soul,
                    "toolsets": self.toolsets,
                    "instance_label": self.instance_label,
                    "requester": self.requester,
                })

                # Start heartbeat task
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_message(ws, json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                            break
                finally:
                    heartbeat_task.cancel()
                    self._ws = None

    async def _heartbeat_loop(self, ws):
        """Send periodic heartbeats to the gateway."""
        while True:
            try:
                await asyncio.sleep(30)
                if ws.closed:
                    break
                await ws.send_json({
                    "type": "heartbeat",
                    "worker_id": self.worker_id,
                    "status": self._status,
                    "uptime_s": int(time.time()),
                })
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def _handle_message(self, ws, data: dict):
        """Handle an incoming message from the gateway."""
        msg_type = data.get("type")

        if msg_type == "registered":
            logger.info("Registration confirmed by gateway")

        elif msg_type == "run_conversation":
            task_id = data.get("task_id", "")
            logger.info("Received task %s: %s", task_id, (data.get("message", ""))[:80])
            self._status = "busy"
            self._current_agent = None
            try:
                result = await self._execute_task(data)
                await ws.send_json({
                    "type": "task_result",
                    "task_id": task_id,
                    "status": "done",
                    **result,
                })
            except Exception as exc:
                logger.exception("Task %s failed", task_id)
                await ws.send_json({
                    "type": "task_result",
                    "task_id": task_id,
                    "status": "error",
                    "error": str(exc),
                })
            finally:
                self._status = "idle"
                self._current_agent = None

        elif msg_type == "interrupt":
            logger.info("Interrupt received for task %s", data.get("task_id"))
            if self._current_agent and hasattr(self._current_agent, "interrupt"):
                self._current_agent.interrupt(data.get("new_message", ""))

        elif msg_type == "shutdown":
            logger.info("Shutdown requested by gateway")
            self._shutdown()

        elif msg_type == "error":
            logger.warning("Gateway error: %s", data.get("message"))

    async def _execute_task(self, task: dict) -> dict:
        """Run AIAgent.run_conversation() in a thread pool.

        The gateway sends everything the agent needs: message, history,
        model config, toolsets.  We create an ephemeral AIAgent, run it,
        and return the result dict.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_agent_sync, task)

    def _run_agent_sync(self, task: dict) -> dict:
        """Synchronous agent execution — runs in a thread."""
        from agents.hermes.agent import AIAgent

        message = task.get("message", "")
        history = task.get("history", [])
        model = task.get("model", "")
        model_kwargs = task.get("model_kwargs", {})
        toolsets = task.get("toolsets", self.toolsets or ["hermes-cli"])
        max_iterations = task.get("max_iterations", 90)
        ephemeral_prompt = task.get("context_prompt", "")
        session_id = task.get("session_id", "")
        task_id = task.get("task_id", "")
        reasoning_config = task.get("reasoning_config")

        # Stream callback — sends tool progress and thinking events to the
        # gateway via WebSocket so the user sees real-time updates.
        def _progress_callback(tool_name: str, preview: str = None, args: dict = None):
            if self._ws and not self._ws.closed:
                event = {
                    "type": "tool_progress",
                    "task_id": task_id,
                    "tool": tool_name,
                    "preview": preview or tool_name,
                }
                # Schedule the send on the event loop (we're in a thread)
                try:
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda: asyncio.ensure_future(self._ws.send_json(event))
                    )
                except Exception:
                    pass

        agent = AIAgent(
            model=model,
            api_key=model_kwargs.get("api_key", "not-needed"),
            base_url=model_kwargs.get("base_url"),
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            enabled_toolsets=toolsets,
            ephemeral_system_prompt=ephemeral_prompt or None,
            reasoning_config=reasoning_config,
            session_id=session_id,
            tool_progress_callback=_progress_callback,
        )
        self._current_agent = agent

        result = agent.run_conversation(
            message,
            conversation_history=history,
            task_id=session_id,
        )

        return {
            "final_response": result.get("final_response", ""),
            "api_calls": result.get("api_calls", 0),
            "tools_used": result.get("tools_used", []),
            "messages": result.get("messages", []),
            "error": result.get("error", ""),
        }

    def _shutdown(self):
        """Signal the worker to shut down gracefully."""
        self._running = False
        if self._ws and not self._ws.closed:
            asyncio.ensure_future(self._ws.close())


async def run_worker(
    gateway_url: str,
    name: str,
    soul: str = "general",
    instance_label: str = "",
    requester: str = "",
):
    """Entry point for `logos worker run`."""
    worker = AgentWorker(
        gateway_url=gateway_url,
        worker_id=name,
        soul=soul,
        instance_label=instance_label,
        requester=requester,
    )
    await worker.run()
