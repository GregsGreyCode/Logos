"""
Sandbox Worker — one-shot task dispatcher for OpenShell sandboxes.

Runs inside the sandbox pod, invoked fresh for every task by the Logos
gateway via ``openshell sandbox exec --no-tty --name <sandbox> --
bash -c "PYTHONPATH=/opt/hermes exec python3 /app/sandbox_worker.py"``.
The gateway pipes one task JSON on stdin + closes stdin, the worker
reads it, instantiates an AIAgent from the upstream hermes image,
runs ``run_conversation()`` with streaming callbacks wired to the
JSON-lines stdout protocol, and exits.

M11: replaced the dumb aiohttp LLM proxy with hermes AIAgent.  The
agent now runs the full agentic loop (tool calls, multi-turn iteration,
context compression) inside the sandbox.  Streaming is real — each
text delta fires a ``token`` event via ``stream_callback``, each
reasoning delta fires a ``thinking`` event via ``reasoning_callback``.

Why one-shot and not a persistent stdin/stdout loop
────────────────────────────────────────────────────
Earlier versions of this file ran a persistent loop reading multiple
tasks from stdin. That was impossible on ``openshell sandbox exec``:
the exec primitive refuses to invoke the in-sandbox process until
stdin reaches EOF. Writing bytes isn't enough — the gRPC exec stream
sits blocked waiting for the stdin write end to close. Proven with
direct side-by-side tests:

    openshell sandbox exec --no-tty ... -- python3 -u -c 'print("x")'
        < /dev/null              → runs in ~1s, "x" on stdout
        < empty_fifo_stay_open   → timeout, nothing on stdout ever

So any design that keeps stdin open for ongoing dispatch is a dead
end on this transport. Instead we spawn one subprocess per task, pipe
the task + close stdin immediately, and stream stdout until exit. The
gateway's ``WorkerRegistry.dispatch_task`` handles that lifecycle.

Why stdin/stdout and not HTTP:
    * ``openshell sandbox exec`` is the blessed gRPC/mTLS control path
      and gives us a ready-made bidirectional stream — no new transport
      to build, no port-forward to manage, no CORS, no TLS certs.
    * Matches OpenShell's isolation model: sandbox is a passive
      execution environment, the gateway drives.
    * Simple to debug: ``echo '{"type":"task",...}' | openshell sandbox
      exec -n <name> -- python3 /app/sandbox_worker.py`` is the exact
      same primitive a human operator uses at the shell.

Protocol (line-delimited JSON on stdin/stdout):

    Gateway → worker (stdin, one line then EOF):
        {"type":"task","task_id":"<id>","message":"...","history":[...],
         "context_prompt":"...","model":"..."}

    Worker → gateway (stdout):
        {"type":"ready","worker_id":"..."}           (sanity, first line)
        {"type":"thinking","task_id":"...","content":"..."}  (streamed)
        {"type":"token","task_id":"...","content":"..."}     (streamed)
        {"type":"task_result","task_id":"...","status":"ok",
         "final_response":"..."}                              (last line, ok)
        {"type":"task_result","task_id":"...","status":"error",
         "error":"..."}                                       (last line, err)

Worker exits cleanly (returncode=0) after emitting task_result.
Logging goes to **stderr** (separate from the stdout protocol channel)
plus a structured JSON sink at /tmp/worker.jsonl for future log
forwarding to the gateway's unified.jsonl (MISSING.md M6 stretch).
"""

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


def read_task_from_stdin() -> Optional[Dict[str, Any]]:
    """Read one JSON task object from stdin (blocking). Returns None on EOF."""
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        logger.warning("malformed JSON on stdin (%s): %r", exc, raw[:200])
        return None


# ── AIAgent import ────────────────────────────────────────────────────────
#
# The M11 wrapper Dockerfile (Dockerfile.hermes-upstream) re-installs
# hermes-agent as a non-editable package so it lands in site-packages
# (readable by the sandbox user). The upstream editable install at
# /opt/hermes is blocked by OpenShell's sandbox policy.

def build_memory_write_event(
    tool_name: Optional[str],
    success: bool,
    result: Optional[str],
    task_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build a memory_write SSE event dict, or None when not applicable.

    Chat UI (main_app.html:7382) renders this as a "saved a memory" card
    inline with the assistant turn. The preview field carries the first
    200 chars of the written memory content so the card shows what was
    saved — an empty preview forces the UI into a bland fallback string.
    Kept pure + module-level so it's unit-testable without spinning up
    the full _handle_task closure.
    """
    if tool_name != "memory" or not success:
        return None
    evt: Dict[str, Any] = {
        "type": "memory_write",
        "preview": str(result or "")[:200],
    }
    if task_id:
        evt["task_id"] = task_id
    return evt


def _import_aiagent():
    """Import AIAgent from the upstream hermes installation.

    Returns the AIAgent class, or raises ImportError with a helpful
    message if the import fails (e.g. image doesn't have hermes).
    """
    try:
        from run_agent import AIAgent
        return AIAgent
    except ImportError as exc:
        logger.error(
            "Failed to import AIAgent from /opt/hermes: %s. "
            "Is this running inside the hermes-upstream image?", exc,
        )
        raise


# ── Task handler ──────────────────────────────────────────────────────────

def _handle_task(task: Dict[str, Any], config: Dict[str, Any]) -> None:
    """Execute one task end-to-end using AIAgent, streaming events to stdout."""
    task_id = task.get("task_id", "")
    message = task.get("message", "")
    history = task.get("history", [])
    context_prompt = task.get("context_prompt", "")

    logger.info("Task %s: message=%r", task_id, message[:80])

    model = config.get("model", os.environ.get("HERMES_MODEL", ""))
    if not model:
        fallback = (
            f"[sandbox worker {config.get('worker_id', '?')}] "
            f"Connected! No model configured — set model in agent config to enable inference."
        )
        emit({"type": "token", "task_id": task_id, "content": fallback})
        emit({
            "type": "task_result",
            "task_id": task_id,
            "status": "ok",
            "final_response": fallback,
        })
        return

    AIAgent = _import_aiagent()

    # Build streaming callbacks that emit JSON-lines protocol events.
    # These fire from inside AIAgent's synchronous agentic loop.
    def on_token(delta: str) -> None:
        """Called for each text token delta during streaming."""
        if delta:
            try:
                emit({"type": "token", "task_id": task_id, "content": delta})
            except BrokenPipeError:
                raise

    def on_reasoning(delta: str) -> None:
        """Called for each reasoning/thinking delta."""
        if delta:
            try:
                emit({"type": "thinking", "task_id": task_id, "content": delta})
            except BrokenPipeError:
                raise

    # Counter for generating unique call IDs per tool invocation
    _tool_call_counter = [0]

    def on_tool_progress(tool_name, preview=None, args=None):
        """Called when a tool invocation STARTS. Emits tool_start for live UI.

        Returns a call_id that AIAgent passes to on_tool_complete later.
        Signature: callback(tool_name, preview, args) -> call_id
        """
        _tool_call_counter[0] += 1
        call_id = f"{task_id}_{_tool_call_counter[0]}"
        try:
            emit({"type": "tool_start", "task_id": task_id, "call_id": call_id,
                  "tool": tool_name or "", "preview": str(preview or "")[:200]})
        except BrokenPipeError:
            raise
        return call_id

    def on_tool_complete(tool_name, call_id, success, duration_ms, error=None, result=None):
        """Called when a tool invocation FINISHES. Emits tool_end + memory_write.

        Signature from AIAgent: callback(tool_name, call_id, success, duration_ms,
        error=<preview on failure>, result=<preview on success>).
        """
        try:
            emit({"type": "tool_end", "task_id": task_id, "call_id": call_id or "",
                  "tool": tool_name or "", "success": bool(success),
                  "duration_ms": duration_ms})
        except BrokenPipeError:
            raise
        mw = build_memory_write_event(tool_name, success, result, task_id=task_id)
        if mw is not None:
            try:
                emit(mw)
            except BrokenPipeError:
                raise

    # Resolve toolsets from the instance config. The soul manifest defines
    # enforced/default_enabled/optional/forbidden toolsets; by the time
    # they reach instance_config["toolsets"] the gateway has resolved them
    # to a flat list of enabled toolset names (e.g. ["web", "memory"]).
    toolsets = config.get("toolsets") or None

    agent = AIAgent(
        model=model,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://inference.local/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", "unused"),
        max_iterations=90,
        quiet_mode=True,
        enabled_toolsets=toolsets,
        stream_delta_callback=on_token,
        reasoning_callback=on_reasoning,
        tool_progress_callback=on_tool_progress,
        tool_complete_callback=on_tool_complete,
    )

    # Convert history to the format AIAgent expects
    conversation_history = []
    for h in history:
        role = h.get("role", "user")
        content = h.get("content", "")
        if content:
            conversation_history.append({"role": role, "content": content})

    try:
        result = agent.run_conversation(
            user_message=message,
            system_message=context_prompt or None,
            conversation_history=conversation_history if conversation_history else None,
            task_id=task_id,
        )

        final_response = result.get("final_response", "") or ""
        completed = result.get("completed", True)
        api_calls = result.get("api_calls", 0)

        logger.info(
            "Task %s finished: completed=%s api_calls=%d response_len=%d",
            task_id, completed, api_calls, len(final_response),
        )

        emit({
            "type": "task_result",
            "task_id": task_id,
            "status": "ok",
            "final_response": final_response,
        })

    except BrokenPipeError:
        raise
    except Exception as exc:
        logger.error("Task %s failed: %s", task_id, exc, exc_info=True)
        try:
            emit({
                "type": "task_result",
                "task_id": task_id,
                "status": "error",
                "error": str(exc),
            })
        except BrokenPipeError:
            raise


# ── One-shot entry point ──────────────────────────────────────────────────

def run_one_task(config: Dict[str, Any]) -> int:
    """Process exactly one task from stdin, emit results, return.

    Flow:
      1. Emit ``{"type":"ready"}`` as a sanity first line so the gateway
         can tell the process actually booted and reached the dispatch
         code path (useful for distinguishing import errors from
         inference errors in logs).
      2. Read one JSON object from stdin (blocking, full read until EOF).
      3. Dispatch based on ``type``:
           - ``task`` / ``run_conversation``: instantiate AIAgent, run
             the full agentic loop with streaming callbacks, emit
             task_result.
           - any other type: emit a task_result with an error payload
             so the gateway always gets a terminal line.
      4. Return 0.

    If stdin reaches EOF before delivering a task, emit a task_result
    error and return 0 anyway — the gateway will treat the missing
    terminal frame as a hard error, but at least we don't leave it
    blocked on readline().
    """
    worker_id = config.get("worker_id") or os.environ.get("HERMES_WORKER_ID") or f"sandbox-{os.getpid()}"
    soul = config.get("soul", "general")
    logger.info("Worker %s starting (one-shot, soul=%s)", worker_id, soul)

    # Sanity ready line — useful in logs. Not load-bearing any more
    # (the gateway doesn't gate the dispatch on it since we're one-shot
    # and the subprocess's existence is its own handshake).
    try:
        emit({
            "type": "ready",
            "worker_id": worker_id,
            "soul": soul,
            "pid": os.getpid(),
            "started_at": time.time(),
        })
    except BrokenPipeError:
        logger.error("Gateway closed stdout before ready emit — aborting")
        return 1

    task = read_task_from_stdin()

    if task is None:
        logger.warning("Stdin EOF before any task received — exiting cleanly")
        try:
            emit({
                "type": "task_result",
                "task_id": "",
                "status": "error",
                "error": "stdin EOF before task received",
            })
        except BrokenPipeError:
            pass
        return 0

    msg_type = task.get("type")
    task_id = task.get("task_id", "")
    if msg_type == "task" or msg_type == "run_conversation":
        try:
            _handle_task(task, config)
        except BrokenPipeError:
            logger.info("Gateway closed stdout during task — exiting")
            return 0
    else:
        logger.warning("Unknown message type on stdin: %r", msg_type)
        try:
            emit({
                "type": "task_result",
                "task_id": task_id,
                "status": "error",
                "error": f"unknown message type {msg_type!r}",
            })
        except BrokenPipeError:
            pass

    return 0


def main() -> None:
    config = load_config()
    logger.info(
        "Config: worker_id=%s, soul=%s, model=%s, toolsets=%s",
        config.get("worker_id", "?"),
        config.get("soul", "general"),
        config.get("model", "(env fallback)"),
        config.get("toolsets", []),
    )

    def _shutdown(sig_num, frame):
        logger.info("Received signal %s, shutting down", sig_num)
        sys.exit(1)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (OSError, ValueError):
            pass

    exit_code = run_one_task(config)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
