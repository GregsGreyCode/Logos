"""
Sandbox Worker — stdin/stdout task dispatcher for OpenShell sandboxes.

Runs inside the sandbox pod, launched by the Logos gateway via
`openshell sandbox exec --no-tty --name <sandbox> -- python3 /app/sandbox_worker.py`.

The gateway writes task JSON lines to this process's stdin; the worker
reads each line, runs an LLM call via `https://inference.local/v1`
(OpenShell's privacy router), streams back token/thinking/result events
as JSON lines on stdout, and waits for the next task.

This is the **Plan A** architecture from TASKS.md #24 — the reverse-
connection WebSocket approach (old `TunnelWebSocket` class) was
unsupported by OpenShell's L7 proxy after an upstream change. `openshell
sandbox exec` is the blessed gRPC/mTLS control path and was empirically
verified rock-solid throughout the 2026-04-11 debugging session.

Why stdin/stdout and not HTTP:
    * OpenShell's `sandbox exec` gives us a ready-made bidirectional
      gRPC stream — no new transport to maintain.
    * No port-forwarding, no sandbox-initiated network egress, no
      CONNECT tunnels.
    * Matches OpenShell's isolation model: sandbox is a passive
      execution environment, gateway drives.
    * Simple to debug: `openshell sandbox exec -n <name> -- ...` is
      the same primitive a human operator uses.

Protocol (line-delimited JSON on stdin/stdout):

    Gateway → worker (stdin):
        {"type":"task","task_id":"<id>","message":"...","history":[...],
         "context_prompt":"...","model":"..."}

    Worker → gateway (stdout):
        {"type":"ready","worker_id":"..."}           (once, at startup)
        {"type":"thinking","task_id":"...","content":"..."}  (streamed)
        {"type":"token","task_id":"...","content":"..."}     (streamed)
        {"type":"task_result","task_id":"...","status":"ok",
         "final_response":"..."}                              (once per task)
        {"type":"task_result","task_id":"...","status":"error",
         "error":"..."}                                       (on failure)

stdin EOF means "gateway is done with me" → clean exit.
Logging goes to **stderr** (separate from the stdout protocol channel)
plus a structured JSON sink at /tmp/worker.jsonl for future log
forwarding to the gateway's unified.jsonl (MISSING.md M6 stretch).
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, Optional


# ── Logging setup ──────────────────────────────────────────────────────────
#
# Critical: logs must go to STDERR, not stdout. Stdout is our protocol
# channel — any accidental print() or logger output there would corrupt
# the gateway's line-delimited JSON parser.

class _SandboxJsonFormatter(logging.Formatter):
    """Structured JSON-lines formatter for sandbox worker logs.

    Mirrors the JsonRedactingFormatter in gateway/run.py so that when
    sandbox logs are eventually forwarded upstream to the gateway's
    unified.jsonl (MISSING.md M6 stretch goal), records arrive in a
    shape that's compatible with the rest of the unified stream.

    Emits one JSON object per log record. Source is tagged "sandbox-worker"
    so `logos debug tail --filter source=sandbox-worker` singles them out.
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


# Text handler → stderr (humans tailing /tmp/worker.log or the openshell
# sandbox exec --tty pass-through). Explicit stream=sys.stderr so logs
# never hit stdout even if stream= default ever changes.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("sandbox_worker")

# JSON handler → /tmp/worker.jsonl for structured output. Compatible with
# the gateway's JsonRedactingFormatter so a future forwarder can simply
# `cat /tmp/worker.jsonl >> ~/.logos/logs/unified.jsonl` server-side.
try:
    _json_handler = logging.FileHandler("/tmp/worker.jsonl", mode="a")
    _json_handler.setFormatter(_SandboxJsonFormatter())
    logging.getLogger().addHandler(_json_handler)
except Exception:
    # Swallow: /tmp might be read-only in some test environments.
    pass


# ── Configuration ─────────────────────────────────────────────────────────

CONFIG_PATH = "/tmp/hermes/instance-config.json"


def load_config() -> dict:
    """Load per-agent config written by the gateway via `openshell sandbox upload`."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("Config file %s not found, using defaults", CONFIG_PATH)
        return {}
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s: %s", CONFIG_PATH, e)
        return {}


# ── Protocol I/O ──────────────────────────────────────────────────────────
#
# Stdout writes are line-delimited JSON. Every `emit` call must flush so
# the gateway's subprocess.stdout reader sees the bytes without waiting
# for buffer fill. Python's sys.stdout is line-buffered when writing to
# a pipe on CPython 3.9+, but we flush explicitly to be safe against
# future changes and to ensure deterministic ordering.

def emit(msg: Dict[str, Any]) -> None:
    """Write one JSON-line protocol message to stdout and flush.

    Catches I/O errors (stdout closed because gateway killed the exec
    subprocess) and re-raises as BrokenPipeError so the outer loop can
    exit cleanly. Logs to stderr, never to stdout — same channel
    discipline as everywhere else in this file.
    """
    try:
        line = json.dumps(msg, default=str, ensure_ascii=False)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        # Gateway closed stdout — nothing more we can do. Re-raise so
        # the caller can abort the task handler.
        raise
    except Exception as exc:
        logger.error("emit() failed for msg type=%s: %s", msg.get("type"), exc)


async def read_stdin_line(
    reader: asyncio.StreamReader,
) -> Optional[Dict[str, Any]]:
    """Read one JSON object from stdin. Returns None on EOF.

    Malformed lines are logged and skipped (we read the next line instead
    of aborting) — this keeps the worker resilient to a broken gateway.
    """
    raw = await reader.readline()
    if not raw:
        return None  # EOF — gateway closed stdin
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return await read_stdin_line(reader)  # skip blank lines
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("malformed JSON on stdin (%s): %r", exc, text[:200])
        return await read_stdin_line(reader)  # skip and try next line


# ── Task handler ──────────────────────────────────────────────────────────

async def _handle_task(task: Dict[str, Any], config: Dict[str, Any]) -> None:
    """Execute one task end-to-end, streaming events to stdout."""
    task_id = task.get("task_id", "")
    message = task.get("message", "")
    history = task.get("history", [])
    context_prompt = task.get("context_prompt", "")

    logger.info("Task %s: message=%r", task_id, message[:80])

    try:
        response = await _run_inference(message, history, context_prompt, config, task_id)
        emit({
            "type": "task_result",
            "task_id": task_id,
            "status": "ok",
            # Canonical key the gateway's _handle_chat looks for. Sending
            # "response" here instead of "final_response" meant the
            # assistant turn was never appended to the transcript —
            # every subsequent turn the model saw history as
            # user, user, user, … (see commit comment on the original
            # _handle_task for the full story).
            "final_response": response,
        })
    except BrokenPipeError:
        # Gateway closed stdout mid-task. Let the main loop handle exit.
        raise
    except Exception as exc:
        logger.error("Task %s failed: %s", task_id, exc)
        try:
            emit({
                "type": "task_result",
                "task_id": task_id,
                "status": "error",
                "error": str(exc),
            })
        except BrokenPipeError:
            raise


# ── Inference ─────────────────────────────────────────────────────────────

async def _run_inference(
    message: str,
    history: list,
    context_prompt: str,
    config: Dict[str, Any],
    task_id: str,
) -> str:
    """Call the LLM via OpenAI-compatible API, streaming tokens back via emit()."""
    import aiohttp

    base_url = os.environ.get("OPENAI_BASE_URL", "https://inference.local/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "unused")
    model = config.get("model", os.environ.get("HERMES_MODEL", ""))

    if not model:
        fallback = (
            f"[sandbox worker {config.get('worker_id', '?')}] "
            f"Connected! No model configured — set model in agent config to enable inference."
        )
        emit({"type": "token", "task_id": task_id, "content": fallback})
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

    # ClientTimeout=600 (10 min) matches the patched OpenShell router ceiling
    # in crates/openshell-router/src/lib.rs (was 60s, bumped to 600s — see
    # TASKS.md #22 for the full debug story). Real inference calls beyond 120s
    # would otherwise get clipped client-side even though the proxy now
    # allows up to 10 minutes. Worker and proxy budgets are aligned.
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
                    #   * `reasoning_content` — qwen3.5-9b streaming
                    #   * `reasoning`         — gpt-oss-20b non-streaming
                    # Read both so we don't silently drop the entire output
                    # of any model that picks the other field.
                    reasoning = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    if reasoning:
                        accumulated_reasoning += reasoning
                        emit({"type": "thinking", "task_id": task_id, "content": reasoning})
                    content = delta.get("content", "")
                    if content:
                        accumulated += content
                        emit({"type": "token", "task_id": task_id, "content": content})
                except json.JSONDecodeError:
                    continue

    # Fallback: if the model emitted ONLY reasoning and no actual content
    # (qwen3.5 has been observed doing this on short prompts under certain
    # chat templates), surface the reasoning as the visible reply so the
    # caller sees something instead of an empty string.
    if not accumulated and accumulated_reasoning:
        logger.info(
            "Inference returned only reasoning_content (%d chars) — "
            "falling back to reasoning as visible reply",
            len(accumulated_reasoning),
        )
        emit({
            "type": "token", "task_id": task_id,
            "content": accumulated_reasoning,
        })
        return accumulated_reasoning
    return accumulated


# ── Main loop ─────────────────────────────────────────────────────────────

async def run_worker(config: Dict[str, Any]) -> int:
    """Read tasks from stdin until EOF, execute each, and write results to stdout.

    Returns the exit code the process should use (0 = clean EOF, 1 = fatal).
    """
    worker_id = config.get("worker_id") or os.environ.get("HERMES_WORKER_ID") or f"sandbox-{os.getpid()}"
    soul = config.get("soul", "general")
    logger.info("Worker %s starting in stdin-mode (soul=%s)", worker_id, soul)

    # Announce readiness on stdout. The gateway's subprocess reader
    # waits for this message before marking spawn complete.
    try:
        emit({
            "type": "ready",
            "worker_id": worker_id,
            "soul": soul,
            "pid": os.getpid(),
            "started_at": time.time(),
        })
    except BrokenPipeError:
        logger.error("Gateway closed stdout before we could announce ready — aborting")
        return 1

    # Wrap stdin as an async StreamReader. Python doesn't give us one
    # directly for sys.stdin, so we connect to the file descriptor.
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            task = await read_stdin_line(reader)
        except asyncio.CancelledError:
            logger.info("Worker cancelled — exiting")
            return 0
        except Exception as exc:
            logger.error("read_stdin_line failed: %s", exc)
            return 1

        if task is None:
            logger.info("Stdin EOF — gateway closed the exec subprocess, exiting cleanly")
            return 0

        msg_type = task.get("type")
        if msg_type == "task" or msg_type == "run_conversation":
            # Accept both the new "task" type and the legacy "run_conversation"
            # the old WebSocket protocol used, so the gateway can roll
            # out its own rewrite gradually without breaking compat.
            try:
                await _handle_task(task, config)
            except BrokenPipeError:
                logger.info("Gateway closed stdout during task — exiting")
                return 0
        elif msg_type == "ping":
            # Cheap liveness check. Gateway sends `{"type":"ping","id":n}`,
            # we echo back `{"type":"pong","id":n}`.
            try:
                emit({"type": "pong", "id": task.get("id")})
            except BrokenPipeError:
                return 0
        elif msg_type == "shutdown":
            logger.info("Shutdown requested via stdin — exiting cleanly")
            return 0
        else:
            logger.warning("Unknown message type on stdin: %r", msg_type)
            try:
                emit({
                    "type": "error",
                    "task_id": task.get("task_id"),
                    "error": f"unknown message type {msg_type!r}",
                })
            except BrokenPipeError:
                return 0


def main() -> None:
    config = load_config()
    logger.info(
        "Config: worker_id=%s, soul=%s, model=%s",
        config.get("worker_id", "?"),
        config.get("soul", "general"),
        config.get("model", "(env fallback)"),
    )

    loop = asyncio.new_event_loop()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info("Received %s, shutting down", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown, sig)
        except NotImplementedError:
            # Windows / non-standard environments — fall back to default handlers
            pass

    exit_code = 0
    try:
        exit_code = loop.run_until_complete(run_worker(config))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
