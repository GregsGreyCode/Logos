"""
Sandbox Worker — WebSocket reverse-connection client for OpenShell sandboxes.

Runs inside the sandbox, connects OUT to the Logos gateway at /ws/worker,
registers as a remote worker, receives chat tasks, and streams responses back.

The WebSocket connection goes through OpenShell's HTTP CONNECT proxy at
10.200.0.1:3128 via a CONNECT tunnel (required because the L7 proxy doesn't
support WebSocket upgrade on plain HTTP forwarding).

For LLM inference, the worker calls https://inference.local/v1 which is routed
by OpenShell's privacy router to the configured inference provider.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
import struct
import sys
import time
from urllib.parse import urlparse

class _SandboxJsonFormatter(logging.Formatter):
    """Structured JSON-lines formatter for sandbox worker logs.

    Mirrors the JsonRedactingFormatter in gateway/run.py so that when
    sandbox logs are eventually forwarded upstream to the gateway's
    unified.jsonl (MISSING.md M6 stretch goal), records arrive in a
    shape that's compatible with the rest of the unified stream.

    Emits one JSON object per log record. Source is tagged "sandbox-worker"
    so `logos debug tail --filter source=sandbox-worker` singles them out.
    worker_id is pulled from the environment (HERMES_WORKER_ID, falls back
    to WORKER_ID, falls back to "-") so every record is self-identifying
    even before a registration exchange has happened.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "source": "sandbox-worker",
            "worker_id": os.environ.get("HERMES_WORKER_ID")
                         or os.environ.get("WORKER_ID")
                         or "-",
            "pid": record.process,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:
            return json.dumps({
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": "<formatter error>",
                "source": "sandbox-worker",
            })


# The sandbox worker emits logs in TWO formats intentionally:
#   1. Text (via basicConfig, stdout) — for humans reading /tmp/worker.log
#      directly inside the sandbox, and for the openshell-sandbox supervisor
#      to capture in its own logs.
#   2. JSON (via the dedicated file handler below) — for future forwarding
#      to the gateway's unified.jsonl, and for `logos debug tail` to parse
#      once a forwarder is in place.
# Both handlers are attached to the same logger so every log call writes
# to both sinks.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sandbox_worker")
# Also attach a JSON file handler to /tmp/worker.jsonl for structured output.
# This file can be tailed by the gateway (once sandbox log forwarding lands)
# or scraped via `openshell sandbox exec -- cat /tmp/worker.jsonl` in the
# interim. Failures to open the file are swallowed — the sandbox might be
# running in an environment where /tmp is read-only or full.
try:
    _json_handler = logging.FileHandler("/tmp/worker.jsonl", mode="a")
    _json_handler.setFormatter(_SandboxJsonFormatter())
    logging.getLogger().addHandler(_json_handler)
except Exception:
    pass

CONFIG_PATH = "/tmp/hermes/instance-config.json"
HEARTBEAT_INTERVAL = 30
RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60

# OpenShell proxy address (set by the sandbox supervisor)
PROXY_HOST = "10.200.0.1"
PROXY_PORT = 3128


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("Config file %s not found, using defaults", CONFIG_PATH)
        return {}
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s: %s", CONFIG_PATH, e)
        return {}


# ── WebSocket over CONNECT tunnel ─────────────────────────────────────────

class TunnelWebSocket:
    """Minimal WebSocket client over an HTTP CONNECT tunnel.

    The OpenShell L7 proxy intercepts plain HTTP requests (including WebSocket
    upgrades) which breaks the handshake.  Using a CONNECT tunnel creates a
    raw TCP pipe that passes the WebSocket upgrade through unmodified.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self.closed = False

    @classmethod
    async def connect(cls, target_host: str, target_port: int) -> "TunnelWebSocket":
        """Establish CONNECT tunnel through proxy, then do WebSocket handshake."""
        proxy_host = os.environ.get("OPENSHELL_PROXY_HOST", PROXY_HOST)
        proxy_port = int(os.environ.get("OPENSHELL_PROXY_PORT", str(PROXY_PORT)))

        reader, writer = await asyncio.open_connection(proxy_host, proxy_port)

        # CONNECT request
        connect_req = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n"
            f"\r\n"
        )
        writer.write(connect_req.encode())
        await writer.drain()

        response_line = await asyncio.wait_for(reader.readline(), timeout=15)
        # Drain remaining headers
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break

        if b"200" not in response_line:
            writer.close()
            raise ConnectionError(f"CONNECT failed: {response_line.decode().strip()}")

        # WebSocket upgrade through the tunnel
        ws_key = base64.b64encode(os.urandom(16)).decode()
        upgrade_req = (
            f"GET /ws/worker HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        writer.write(upgrade_req.encode())
        await writer.drain()

        ws_response = await asyncio.wait_for(reader.readline(), timeout=15)
        while True:
            line = await reader.readline()
            if line.strip() == b"":
                break

        if b"101" not in ws_response:
            writer.close()
            raise ConnectionError(f"WebSocket upgrade failed: {ws_response.decode().strip()}")

        return cls(reader, writer)

    async def send_json(self, data: dict):
        """Send a JSON message as a WebSocket text frame."""
        payload = json.dumps(data).encode()
        await self._send_frame(0x1, payload)  # opcode 1 = text

    async def _send_frame(self, opcode: int, payload: bytes):
        frame = bytearray()
        frame.append(0x80 | opcode)  # FIN + opcode
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(mask)
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame.extend(masked)
        self._writer.write(bytes(frame))
        await self._writer.drain()

    async def receive_json(self, timeout: float = None) -> dict | None:
        """Receive a WebSocket text frame and parse as JSON.

        Returns None when the timeout window elapses without a frame
        arriving — that's normal idle behaviour, NOT a disconnect (the
        gateway only pushes frames when it has work to dispatch). The
        caller treats None as "keep waiting".

        Only raises ConnectionError when the underlying socket is
        actually closed (peer EOF, which _recv_frame_inner detects by
        a short read and sets self.closed = True).
        """
        data = await self._recv_frame(timeout)
        if data is None:
            if self.closed:
                raise ConnectionError("WebSocket closed")
            return None  # timeout, socket still open
        return json.loads(data)

    async def _recv_frame(self, timeout: float = None) -> bytes | None:
        try:
            coro = self._recv_frame_inner()
            if timeout:
                return await asyncio.wait_for(coro, timeout)
            return await coro
        except asyncio.TimeoutError:
            return None

    async def _recv_frame_inner(self) -> bytes | None:
        header = await self._reader.read(2)
        if len(header) < 2:
            self.closed = True
            return None
        fin = header[0] & 0x80
        opcode = header[0] & 0x0F
        masked = header[1] & 0x80
        length = header[1] & 0x7F

        if opcode == 0x8:  # close
            self.closed = True
            return None
        if opcode == 0x9:  # ping
            await self._send_frame(0xA, b"")  # pong
            return await self._recv_frame_inner()

        if length == 126:
            ext = await self._reader.readexactly(2)
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = await self._reader.readexactly(8)
            length = struct.unpack(">Q", ext)[0]

        if masked:
            mask = await self._reader.readexactly(4)
            payload = await self._reader.readexactly(length)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        else:
            payload = await self._reader.readexactly(length)

        return payload

    async def close(self):
        if not self.closed:
            try:
                await self._send_frame(0x8, b"")
            except Exception:
                pass
            self.closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


# ── Worker loop ────────────────────────────────────────────────────────────

async def run_worker(config: dict):
    gateway_url = config.get("gateway_url", "")
    if not gateway_url:
        logger.error("gateway_url not set in config")
        sys.exit(1)

    parsed = urlparse(gateway_url)
    target_host = parsed.hostname
    target_port = parsed.port or 8091
    worker_id = config.get("worker_id", f"sandbox-{os.getpid()}")
    soul = config.get("soul", "general")
    toolsets = config.get("toolsets", [])
    instance_label = config.get("instance_name", worker_id)

    logger.info("Worker %s connecting to %s:%d (soul=%s)", worker_id, target_host, target_port, soul)

    delay = RECONNECT_DELAY
    while True:
        try:
            ws = await TunnelWebSocket.connect(target_host, target_port)
            logger.info("Connected to gateway via CONNECT tunnel")
            delay = RECONNECT_DELAY

            await ws.send_json({
                "type": "register",
                "worker_id": worker_id,
                "soul": soul,
                "toolsets": toolsets,
                "instance_label": instance_label,
            })

            heartbeat_task = asyncio.create_task(_heartbeat_loop(ws, worker_id))
            try:
                while not ws.closed:
                    data = await ws.receive_json(timeout=HEARTBEAT_INTERVAL + 10)
                    if data is None:
                        continue

                    msg_type = data.get("type")
                    if msg_type == "registered":
                        logger.info("Registered as worker %s", data.get("worker_id"))
                    elif msg_type == "run_conversation":
                        await _handle_task(ws, data, config)
                    elif msg_type == "error":
                        logger.error("Gateway error: %s", data.get("message"))
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                await ws.close()

        except ConnectionError as e:
            logger.warning("Connection failed: %s (retrying in %ds)", e, delay)
        except Exception as e:
            logger.error("Unexpected error: %s (retrying in %ds)", e, delay)

        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_RECONNECT_DELAY)


async def _heartbeat_loop(ws: TunnelWebSocket, worker_id: str):
    try:
        while not ws.closed:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if ws.closed:
                break
            await ws.send_json({
                "type": "heartbeat",
                "worker_id": worker_id,
                "status": "idle",
            })
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug("Heartbeat stopped: %s", e)


async def _handle_task(ws: TunnelWebSocket, task: dict, config: dict):
    task_id = task.get("task_id", "")
    message = task.get("message", "")
    history = task.get("history", [])
    context_prompt = task.get("context_prompt", "")

    logger.info("Task %s: message=%r", task_id, message[:80])

    try:
        response = await _run_inference(message, history, context_prompt, config, ws, task_id)
        await ws.send_json({
            "type": "task_result",
            "task_id": task_id,
            "status": "ok",
            # Use the canonical key the gateway looks for. Sending "response"
            # here meant http_api._handle_chat received an empty final, so the
            # assistant turn was never appended to the transcript — every new
            # turn the model saw history as user, user, user, … with no
            # interleaved assistant messages, and started replying to the
            # oldest unanswered question instead of the current one.
            "final_response": response,
        })
    except Exception as e:
        logger.error("Task %s failed: %s", task_id, e)
        await ws.send_json({
            "type": "task_result",
            "task_id": task_id,
            "status": "error",
            "error": str(e),
        })


async def _run_inference(
    message: str, history: list, context_prompt: str,
    config: dict, ws: TunnelWebSocket, task_id: str,
) -> str:
    """Call the LLM via OpenAI-compatible API, streaming tokens back."""
    import aiohttp

    base_url = os.environ.get("OPENAI_BASE_URL", "https://inference.local/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "unused")
    model = config.get("model", os.environ.get("HERMES_MODEL", ""))

    if not model:
        fallback = f"[sandbox worker {config.get('worker_id', '?')}] Connected! No model configured — set model in agent config to enable inference."
        await ws.send_json({"type": "token", "task_id": task_id, "content": fallback})
        return fallback

    messages = []
    if context_prompt:
        messages.append({"role": "system", "content": context_prompt})
    for h in history:
        role = h.get("role", "user")
        content = h.get("content", "")
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # max_tokens=16384 — generous ceiling so reasoning models like
    # qwen3.5-9b have room to finish thinking AND emit a visible answer.
    # Without this LM Studio's default cuts the response off mid-reasoning
    # (~600-1000 tokens) and the user sees a truncated thinking trace
    # with no actual reply. The model still stops naturally on its own
    # </think> + answer; this is an upper bound, not a target.
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": 16384,
    }
    accumulated = ""
    accumulated_reasoning = ""

    ssl_ctx = None
    ca_bundle = "/etc/openshell-tls/ca.crt"
    if os.path.exists(ca_bundle):
        import ssl
        ssl_ctx = ssl.create_default_context(cafile=ca_bundle)

    # Use trust_env for inference (HTTPS goes through CONNECT automatically)
    #
    # ClientTimeout=600 (10 min) matches the patched OpenShell router ceiling
    # in `crates/openshell-router/src/lib.rs` (was 60s, bumped to 600s — see
    # TASKS.md #22 for the full debug story). The previous 120s worker
    # timeout was the bottleneck after the OpenShell patch landed: real
    # inference calls beyond 120s would still get clipped client-side even
    # though the proxy now allows up to 10 minutes. Worker and proxy budgets
    # are now aligned.
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.post(
            f"{base_url}/chat/completions", json=payload, headers=headers,
            ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"LLM API returned {resp.status}: {body[:200]}")

            async for line in resp.content:
                line = line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    # Reasoning models (qwen3.5, gpt-oss, etc) emit their
                    # internal thinking via LM Studio's reasoning extension
                    # to the OpenAI-compat format. The field name varies
                    # by model + LM Studio version:
                    #
                    #   * `reasoning_content` — qwen3.5-9b in streaming mode
                    #     (the original LM Studio extension name)
                    #   * `reasoning`         — gpt-oss-20b non-streaming
                    #     responses (verified 2026-04-10), and likely the
                    #     newer convention LM Studio is migrating toward
                    #
                    # Read both so we don't silently drop the entire output
                    # of any model that picks the other field. Without this
                    # the worker would return "" while LM Studio happily
                    # streams the model's reasoning into a field we ignore.
                    reasoning = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    if reasoning:
                        accumulated_reasoning += reasoning
                        await ws.send_json({"type": "thinking", "task_id": task_id, "content": reasoning})
                    content = delta.get("content", "")
                    if content:
                        accumulated += content
                        await ws.send_json({"type": "token", "task_id": task_id, "content": content})
                except json.JSONDecodeError:
                    continue

    # Fallback: if the model emitted ONLY reasoning and no actual content
    # (qwen3.5 has been observed doing this on short prompts under certain
    # chat templates), surface the reasoning as the visible reply so the
    # caller sees something instead of an empty string. Better a verbose
    # answer than silence.
    if not accumulated and accumulated_reasoning:
        logger.info(
            "Inference returned only reasoning_content (%d chars) — "
            "falling back to reasoning as visible reply",
            len(accumulated_reasoning),
        )
        await ws.send_json({
            "type": "token", "task_id": task_id,
            "content": accumulated_reasoning,
        })
        return accumulated_reasoning
    return accumulated


def main():
    config = load_config()
    logger.info("Config: worker_id=%s, gateway=%s",
                config.get("worker_id", "?"), config.get("gateway_url", "?"))

    loop = asyncio.new_event_loop()

    def _shutdown(sig):
        logger.info("Received %s, shutting down", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        loop.run_until_complete(run_worker(config))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
