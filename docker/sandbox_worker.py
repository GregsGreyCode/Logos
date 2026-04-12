"""
Sandbox Worker — per-task ``AIAgent.run_conversation`` bootstrap.

Runs inside the sandbox pod, invoked fresh for every task dispatch via
``openshell sandbox exec --no-tty --name <sandbox> -- python3
/app/sandbox_worker.py``. The gateway pipes one task JSON on stdin +
closes stdin, the worker reads it, instantiates ``AIAgent``, runs
``agent.run_conversation(...)`` in a thread executor (streaming
token/thinking/tool_progress frames to stdout via callbacks), emits
the terminal ``task_result`` frame, and exits.

M10 restoration (2026-04-12): earlier Plan-A-prime versions of this
file were a naive chat-completion forwarder — they POSTed directly to
``inference.local/v1/chat/completions`` with no ``tools`` field, never
imported ``AIAgent``, and skipped memory writes / skill management /
tool use / delegation / run recording / nudges entirely. This version
restores the full Hermes agent loop inside the sandbox, making memory
and skill writes happen during chats for the first time since the
original Plan A reverse-WebSocket worker was retired.

Cold-start cost per dispatch: ~0.5–1s — Python interpreter + OpenAI
SDK + ``agents.hermes.agent`` import. Amortized over multi-second
inference calls. The sandbox pod stays alive between dispatches
(``sleep infinity``); only this worker process is ephemeral.

Why one-shot and not a persistent stdin/stdout loop
────────────────────────────────────────────────────
Directly tested and documented in ``gateway/worker_registry.py:25-33``:
``openshell sandbox exec --no-tty`` refuses to invoke the in-sandbox
command until stdin reaches EOF. Writing bytes without closing the
pipe does NOT unblock it. Any design that keeps stdin open for ongoing
dispatch or bidirectional control is physically impossible on this
transport. The per-task subprocess model matches the primitive's
actual contract — each task gets a fresh process, stdin closes on
task delivery, stdout streams until task_result, process exits.

Protocol (line-delimited JSON on stdin/stdout) — unchanged from the
previous revision so ``gateway/worker_registry.py:dispatch_task``
parses the same frames:

    Gateway → worker (stdin, one line then EOF):
        {"type":"run_conversation" (or "task"),
         "task_id":"<uuid>",
         "session_id":"<session-key>",
         "message":"<user message>",
         "history":[<prior messages>],
         "context_prompt":"<soul + user context, built host-side>",
         "toolsets":["hermes-cli", ...],
         "max_iterations":90,
         "model":"<model name>" (optional override)}

    Worker → gateway (stdout):
        {"type":"ready","worker_id":"..."}                 (sanity, first line)
        {"type":"thinking","task_id":"...","content":"..."}  (streamed reasoning)
        {"type":"token","task_id":"...","content":"..."}     (streamed text deltas)
        {"type":"tool_progress","task_id":"...",
         "tool":"<name>","preview":"..."}                    (per-tool)
        {"type":"task_result","task_id":"...","status":"ok",
         "final_response":"...","api_calls":N,
         "tools_used":[...]}                                 (last line, ok)
        {"type":"task_result","task_id":"...","status":"error",
         "final_response":"","error":"..."}                  (last line, err)

Worker exits cleanly (returncode=0) after emitting task_result.
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
#
# M10 restoration (2026-04-12): instantiates ``AIAgent`` per dispatch and
# calls ``run_conversation`` with streaming callbacks that emit protocol
# frames to stdout. The earlier chat-completion forwarder that lived here
# is gone — see the module docstring for the full story.


async def _handle_task(task: Dict[str, Any], config: Dict[str, Any]) -> None:
    """Execute one task end-to-end via ``AIAgent.run_conversation``.

    Instantiates an ephemeral ``AIAgent`` with streaming callbacks wired
    to ``emit()`` so tool progress, reasoning content, and response text
    stream back to the gateway as protocol frames during the run. The
    terminal ``task_result`` frame reports the final response plus
    metadata (api_calls, tools_used) when the agent returns.

    Persistent state (memories, skills, sessions, logs) lives on disk
    under ``$HERMES_HOME`` inside the sandbox pod and survives across
    dispatches because the pod runs ``sleep infinity`` between them.
    Only the Python process is ephemeral, not the data.

    Scope (Phase 1): the agent uses ``ephemeral_system_prompt`` = the
    ``context_prompt`` the gateway built (which already includes the
    soul + user/session context via ``build_agent_system_prompt``), so
    ``skip_context_files=True`` is set to avoid re-reading SOUL.md /
    AGENTS.md from the sandbox filesystem. Memory IS loaded
    (``skip_memory=False``, the default) so ``memory_tool`` writes
    persist to ``$HERMES_HOME/memories/``.

    Error paths always emit a terminal ``task_result`` frame with
    ``status="error"`` and the error string so the gateway can render
    the failure as an in-chat error bubble instead of hanging.
    """
    task_id = task.get("task_id", "")
    message = task.get("message", "")
    history = task.get("history", [])
    context_prompt = task.get("context_prompt", "")
    session_id = task.get("session_id") or task_id

    logger.info(
        "Task %s: message=%r session=%s history=%d",
        task_id, message[:80], session_id, len(history) if isinstance(history, list) else 0,
    )

    def _emit_error(reason: str) -> None:
        try:
            emit({
                "type": "task_result",
                "task_id": task_id,
                "status": "error",
                "final_response": "",
                "error": reason,
            })
        except BrokenPipeError:
            raise

    # ── Import the agent lazily so a clean startup failure (missing
    # dep, broken venv) surfaces as a task_result error rather than a
    # Python traceback on stderr that the gateway has to guess at.
    try:
        from agents.hermes.agent import AIAgent
    except Exception as exc:
        logger.exception("Task %s: failed to import AIAgent", task_id)
        _emit_error(f"AIAgent import failed: {exc}")
        return

    # ── Resolve inference config ──
    # Task payload overrides the instance config when fields are present.
    # The final inference call goes to inference.local via the OpenShell
    # privacy router, which injects the provider credential at egress —
    # the sandbox never sees the real API key.
    model = (
        task.get("model")
        or config.get("model")
        or os.environ.get("HERMES_MODEL", "")
    )
    base_url = os.environ.get("OPENAI_BASE_URL", "https://inference.local/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "unused")
    toolsets = task.get("toolsets") or config.get("toolsets") or ["hermes-cli"]
    try:
        max_iterations = int(
            task.get("max_iterations")
            or os.environ.get("HERMES_MAX_ITERATIONS", "90")
        )
    except (TypeError, ValueError):
        max_iterations = 90

    if not model:
        _emit_error(
            "No model configured — set `model` in the task payload or "
            "/tmp/hermes/instance-config.json, or set HERMES_MODEL in the "
            "sandbox environment."
        )
        return

    # ── Streaming callbacks ──
    # AIAgent invokes these from its main thread during response
    # streaming and tool execution. Each callback emits one protocol
    # frame per event. BrokenPipeError propagates up so the outer
    # loop can exit cleanly if the gateway tears down mid-task.
    def _on_tool_progress(tool_name: str, preview: Optional[str] = None,
                          args: Optional[dict] = None) -> None:
        try:
            emit({
                "type": "tool_progress",
                "task_id": task_id,
                "tool": tool_name or "",
                "preview": preview or "",
            })
        except BrokenPipeError:
            raise
        except Exception as exc:
            logger.debug("tool_progress emit failed: %s", exc)

    def _on_thinking(content: str) -> None:
        if not content:
            return
        try:
            emit({"type": "thinking", "task_id": task_id, "content": content})
        except BrokenPipeError:
            raise
        except Exception as exc:
            logger.debug("thinking emit failed: %s", exc)

    def _on_stream(delta: str) -> None:
        # ``stream_callback`` fires with each text delta during response
        # streaming. AIAgent's upstream use case is TTS; we repurpose it
        # for SSE token events so the browser gets the typewriter effect.
        if not delta:
            return
        try:
            emit({"type": "token", "task_id": task_id, "content": delta})
        except BrokenPipeError:
            raise
        except Exception as exc:
            logger.debug("token emit failed: %s", exc)

    # ── Instantiate AIAgent ──
    try:
        agent = AIAgent(
            model=model,
            base_url=base_url,
            api_key=api_key,
            enabled_toolsets=toolsets,
            session_id=session_id,
            ephemeral_system_prompt=context_prompt or None,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            # Gateway already built the full system prompt including
            # soul + session context — skip re-reading context files
            # from the sandbox filesystem to avoid duplication.
            skip_context_files=True,
            # Memory IS loaded so memory_tool has a backing store inside
            # the sandbox; writes persist to $HERMES_HOME/memories/.
            # This is the M10 core change — memory writes during chats.
            skip_memory=False,
            tool_progress_callback=_on_tool_progress,
            thinking_callback=_on_thinking,
        )
    except Exception as exc:
        logger.exception("Task %s: AIAgent instantiation failed", task_id)
        _emit_error(f"AIAgent init failed: {exc}")
        return

    # ── Run the conversation in a thread executor ──
    # run_conversation is synchronous (returns a dict). Running it in
    # the default thread executor keeps the asyncio event loop free so
    # emit() writes and logger calls don't block on the agent's I/O.
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: agent.run_conversation(
                user_message=message,
                conversation_history=history if isinstance(history, list) else [],
                task_id=task_id,
                stream_callback=_on_stream,
            ),
        )
    except BrokenPipeError:
        raise
    except Exception as exc:
        logger.exception("Task %s: run_conversation raised", task_id)
        _emit_error(f"run_conversation failed: {exc}")
        return

    # ── Terminal task_result frame ──
    if not isinstance(result, dict):
        logger.warning("Task %s: run_conversation returned non-dict %r", task_id, type(result))
        _emit_error(f"run_conversation returned unexpected type: {type(result).__name__}")
        return

    final_response = result.get("final_response", "") or ""
    api_calls = result.get("api_calls", 0) or 0
    tools_used = result.get("tools_used", []) or []
    error_str = result.get("error", "") or ""
    completed = result.get("completed", True)
    interrupted = result.get("interrupted", False)

    # Treat interrupt as a success with a truncated response; only
    # explicit errors (exception or error field) produce status=error.
    if error_str and not interrupted:
        status = "error"
    else:
        status = "ok"

    out: Dict[str, Any] = {
        "type": "task_result",
        "task_id": task_id,
        "status": status,
        "final_response": final_response,
        "api_calls": api_calls,
        "tools_used": tools_used,
        "completed": bool(completed),
    }
    if error_str:
        out["error"] = error_str
    if interrupted:
        out["interrupted"] = True

    try:
        emit(out)
    except BrokenPipeError:
        raise


# ── One-shot entry point ──────────────────────────────────────────────────

async def run_one_task(config: Dict[str, Any]) -> int:
    """Process exactly one task from stdin, emit results, return.

    Flow:
      1. Emit ``{"type":"ready"}`` as a sanity first line so the gateway
         can tell the process actually booted and reached the dispatch
         code path (useful for distinguishing import errors from
         inference errors in logs).
      2. Wrap stdin as an async StreamReader and read one JSON line.
         ``read_stdin_line`` already skips blank/malformed lines and
         returns None on EOF.
      3. Dispatch based on ``type``:
           - ``task`` / ``run_conversation``: run inference, emit
             streaming events + task_result.
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

    # Wrap stdin as an async StreamReader so read_stdin_line's
    # asyncio.StreamReader interface works unchanged.
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    try:
        task = await read_stdin_line(reader)
    except asyncio.CancelledError:
        logger.info("Worker cancelled — exiting")
        return 0
    except Exception as exc:
        logger.error("read_stdin_line failed: %s", exc)
        try:
            emit({
                "type": "task_result",
                "task_id": "",
                "status": "error",
                "error": f"stdin read failed: {exc}",
            })
        except BrokenPipeError:
            pass
        return 1

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
            await _handle_task(task, config)
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
            pass

    exit_code = 0
    try:
        exit_code = loop.run_until_complete(run_one_task(config))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
