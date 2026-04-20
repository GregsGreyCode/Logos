"""
WorkerRegistry V2 — LOG-44 Phase 1 dispatch over `hermes gateway run`.

Sibling dispatcher to `worker_registry.WorkerRegistry.dispatch_task`.
Where v1 spawns a per-task `openshell sandbox exec -- python3 sandbox_worker.py`
subprocess that instantiates AIAgent in-process, v2 expects a long-lived
`hermes gateway run` HTTP server already running inside the sandbox (see
``gateway/executors/hermes_server_mode.py`` for the setup helpers) and
dispatches over HTTP:

    host → `openshell sandbox exec --no-tty -- sh -c "python3 -"`
         → in-sandbox python client reads task JSON on stdin
         → POST http://127.0.0.1:8642/v1/runs with the Bearer key
         → GET /v1/runs/{run_id}/events (SSE stream)
         → emit translated JSON-line frames on stdout
    host ← parses stdout frames
          ← invokes on_stream_event callback per frame
          ← returns the task_result frame

Frame translation (Hermes SSE → Logos frame):

    message.delta  (delta: "…")                → {"type":"token","task_id":…,"content":"…"}
    reasoning.available (text:)                → {"type":"thinking","task_id":…,"content":"…"}
    tool.started   (tool: name, preview)       → {"type":"tool_start","task_id":…,"tool":name,"preview":…}
    tool.completed (tool: name, duration, err) → {"type":"tool_end","task_id":…,"tool":name,"duration":…,"error":…}
    run.completed  (output,usage)              → {"type":"task_result","task_id":…,"status":"ok","final_response":"…","usage":{…}}
    run.failed                                 → {"type":"task_result","task_id":…,"status":"error","error":"…"}

Earlier versions of this module mapped ``tool.start``/``tool.end``
(no -ed/-ed suffix), which was never correct — hermes upstream has
always emitted the -ed forms (see LOG-51.6 fix). The bug stayed
hidden because the Live Executions UI could fall through to the
task_result summary and the tool_sequence column stayed NULL
everywhere. LOG-60.1 agent_events ingestion made the miss visible:
first real run produced zero tool_start events and v1-fallback
tool_end rows polluted with call_id strings. The names below are
the shapes observed by direct SSE capture on 2026-04-19.

Signature is intentionally the same shape as ``WorkerRegistry.dispatch_task``
so `_handle_chat` can route between v1 and v2 with a single env-var check.

Usage pattern (intended integration in `_handle_chat`):

    if os.getenv("LOGOS_DISPATCH_V2") == "1" and _sandbox_has_server(sandbox_name):
        result = await dispatch_task_v2(sandbox_name, task, timeout, on_stream_event)
    else:
        result = await worker_registry.dispatch_task(sandbox_name, task, timeout, on_stream_event)

See ``docs/architecture/hermes-as-server-prototype.md`` for the full
design + validation notes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# Host-side runaway guard. If the same (tool_name, preview) pair sees
# this many consecutive failed tool.completed frames with no intervening
# success and no different-tool failure, dispatch_task_v2 aborts the run
# via cancel_task + a synthetic task_result. Agent-agnostic: watches the
# translated SSE stream, so the guard works regardless of which agent
# runtime is baked into the sandbox image.
_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5


@dataclass
class _CircuitBreaker:
    """Per-run tracker for consecutive identical tool failures.

    Call :meth:`observe` with every parsed frame from the SSE-translated
    stream. Returns True when the run should be aborted (``threshold``
    identical ``(tool, preview)`` failures in a row).

    Reset conditions:
    - Any tool.completed with ``error=False``/missing (success).
    - Any tool.completed whose ``(tool, preview)`` differs from the
      most-recent failure — reset, then record the new failure as the
      window's first entry.

    The ``preview`` field on tool.started is a 40-char truncated string
    (hermes side, ``agent/display.py``). That's coarse — long paths can
    collide — but it's sufficient for catching a stuck loop where the
    agent keeps calling the same tool with the same args. For exact
    dedup the hermes event shape would need an args digest; noted as a
    follow-up under LOG-45's runtime-contract work.
    """

    threshold: int = _CIRCUIT_BREAKER_FAILURE_THRESHOLD
    _last_preview_by_tool: Dict[str, str] = field(default_factory=dict)
    _recent_failures: Deque[Tuple[str, str]] = field(init=False)

    def __post_init__(self) -> None:
        self._recent_failures = deque(maxlen=self.threshold)

    def observe(self, frame: Dict[str, Any]) -> bool:
        """Update state from ``frame`` and return True iff the run should abort."""
        ftype = frame.get("type")
        if ftype == "tool_start":
            tool = str(frame.get("tool") or "")
            preview = str(frame.get("preview") or "")
            self._last_preview_by_tool[tool] = preview
            return False
        if ftype != "tool_end":
            return False

        tool = str(frame.get("tool") or "")
        # Hermes emits ``error=False`` for success, and a truthy
        # string/bool for failure.
        is_err = bool(frame.get("error"))
        if not is_err:
            self._recent_failures.clear()
            return False

        preview = self._last_preview_by_tool.get(tool, "")
        key = (tool, preview)
        if self._recent_failures and self._recent_failures[-1] != key:
            self._recent_failures.clear()
        self._recent_failures.append(key)
        return len(self._recent_failures) >= self.threshold

    @property
    def tripped_tool(self) -> str:
        return self._recent_failures[-1][0] if self._recent_failures else ""

    @property
    def tripped_preview(self) -> str:
        return self._recent_failures[-1][1] if self._recent_failures else ""

    @property
    def count(self) -> int:
        return len(self._recent_failures)


def _default_task_timeout() -> float:
    """Per-dispatch wall-clock timeout for v2 runs.

    Env-overridable via ``LOGOS_V2_DISPATCH_TIMEOUT_S`` so operators can
    raise it without editing code when agents routinely do long
    tool-using loops. Default bumped from the original 600s (10 min)
    to 1800s (30 min) after a live 207-message essay run timed out at
    10 min while still producing meaningful work — tool-using agents
    on smaller models easily exceed 10 min once context compression
    and multi-step reasoning stack up.

    Clamped to [60, 7200] — shorter than a minute isn't useful for
    anything real, longer than 2 hours means the dispatch is
    effectively never timing out and the user is better served by
    Stop or a gateway restart than waiting on us to give up.
    """
    raw = os.environ.get("LOGOS_V2_DISPATCH_TIMEOUT_S")
    if not raw:
        return 1800.0
    try:
        v = float(raw)
    except ValueError:
        logger.warning(
            "LOGOS_V2_DISPATCH_TIMEOUT_S=%r is not a number — falling back to 1800s",
            raw,
        )
        return 1800.0
    return max(60.0, min(7200.0, v))


DEFAULT_TASK_TIMEOUT = _default_task_timeout()

# LOG-51.3: in-flight task registry so /chat/{task_id}/cancel can reach
# the v2 dispatch subprocess. Keyed by the logos task_id (not hermes's
# run_id — the cancel endpoint only knows the former). Populated at the
# top of dispatch_task_v2 and popped in its finally, so the map
# reflects actively-running dispatches.
#
# Cancelling takes two steps (the signal doesn't propagate through
# ``openshell sandbox exec`` so terminating the host-side subprocess
# alone leaves the in-sandbox client running):
#   1. Host-side: ``proc.terminate()`` on the asyncio subprocess.
#   2. In-sandbox: ``openshell sandbox exec -- pkill -f`` matching
#      the task-specific ``_disp_v2_client_<task_id>.py`` filename.
# Step 2's disconnect is what actually triggers the LOG-51.2
# monkeypatch's interrupt path inside hermes.
_INFLIGHT: Dict[str, Dict[str, Any]] = {}


def _load_server_setup(sandbox_name: str) -> Optional[Dict[str, str]]:
    """Retrieve the HermesServerSetup info for a sandbox from the
    executor state file.

    Returns the dict that OpenShellExecutor.spawn stashed under
    ``record["hermes_server_setup"]`` after calling
    ``enable_hermes_server_mode``, or None if not present (meaning this
    sandbox was spawned without LOGOS_HERMES_SERVER_MODE=1).
    """
    try:
        from gateway.executors.openshell import _load_state
    except ImportError:
        return None
    for inst in _load_state():
        if inst.get("sandbox_name") == sandbox_name:
            setup = inst.get("hermes_server_setup")
            if isinstance(setup, dict) and setup.get("api_key") and setup.get("base_url"):
                return setup
            return None
    return None


# The in-sandbox Python client that drives the HTTP request + SSE parse.
# Base64-encoded on the host (openshell exec rejects newlines in args),
# decoded on the other side via `sh -c "echo $B64 | base64 -d | python3 -"`.
# Reads task JSON on stdin, emits frames on stdout, stdlib-only.
_IN_SANDBOX_CLIENT = r"""
import sys, json, urllib.request, urllib.error, os

BASE = os.environ["HERMES_BASE_URL"]
AUTH = {"Authorization": "Bearer " + os.environ["HERMES_API_KEY"],
        "Content-Type": "application/json"}

def emit(f):
    sys.stdout.write(json.dumps(f) + "\n")
    sys.stdout.flush()

def err(task_id, msg, rc):
    emit({"type": "task_result", "task_id": task_id, "status": "error",
          "error": msg})
    sys.exit(rc)

def main():
    raw = sys.stdin.read()
    try:
        task = json.loads(raw)
    except Exception as e:
        err("?", f"bad task JSON: {e}", 2)
    task_id = task.get("task_id") or "?"
    body = json.dumps({
        "input": task.get("message", ""),
        "instructions": task.get("system_prompt") or task.get("context_prompt"),
        "session_id": task.get("session_id") or task_id,
    }).encode()
    req = urllib.request.Request(BASE + "/v1/runs", data=body, headers=AUTH)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        run = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err(task_id, f"runs POST {e.code}: {e.read().decode()[:200]}", 3)
    except Exception as e:
        err(task_id, f"runs POST failed: {type(e).__name__}: {e}", 3)

    run_id = run.get("run_id") or run.get("id")
    if not run_id:
        err(task_id, f"runs POST returned no run_id: {run}", 4)
    emit({"type": "ready", "task_id": task_id, "run_id": run_id})

    ev_req = urllib.request.Request(
        BASE + "/v1/runs/" + run_id + "/events",
        headers={"Authorization": AUTH["Authorization"]},
    )
    try:
        ev_r = urllib.request.urlopen(ev_req, timeout=600)
    except Exception as e:
        err(task_id, f"events GET failed: {type(e).__name__}: {e}", 5)

    accum = []
    final_output = None
    final_usage = None
    final_error = None
    for raw_line in ev_r:
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        if line.startswith(": "):
            if "stream closed" in line or "done" in line:
                break
            continue
        if not line.startswith("data: "):
            continue
        try:
            ev = json.loads(line[6:])
        except Exception:
            continue
        kind = ev.get("event")
        if kind == "message.delta":
            d = ev.get("delta", "")
            if d:
                accum.append(d)
                emit({"type": "token", "task_id": task_id, "content": d})
        elif kind == "reasoning.available":
            t = ev.get("text", "")
            if t:
                emit({"type": "thinking", "task_id": task_id, "content": t})
        elif kind == "tool.started":
            emit({"type": "tool_start", "task_id": task_id,
                  "tool": ev.get("tool"), "preview": ev.get("preview"),
                  "timestamp": ev.get("timestamp")})
        elif kind == "tool.completed":
            emit({"type": "tool_end", "task_id": task_id,
                  "tool": ev.get("tool"), "duration": ev.get("duration"),
                  "error": ev.get("error", False),
                  "timestamp": ev.get("timestamp")})
        elif kind == "run.completed":
            final_output = ev.get("output")
            final_usage = ev.get("usage")
        elif kind == "run.failed":
            final_error = ev.get("error") or "run.failed"

    if final_error:
        err(task_id, final_error, 6)

    emit({
        "type": "task_result", "task_id": task_id, "status": "ok",
        "final_response": final_output if final_output is not None else "".join(accum),
        "usage": final_usage or {},
    })

main()
"""


async def dispatch_task_v2(
    worker_id: str,
    task: Dict[str, Any],
    timeout: float = DEFAULT_TASK_TIMEOUT,
    on_stream_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    setup: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Dispatch a task to a sandbox running `hermes gateway run`.

    Signature-compatible with ``WorkerRegistry.dispatch_task``. The
    ``worker_id`` is the sandbox name (one-to-one mapping, matching
    v1's convention).

    ``setup`` can be supplied explicitly (dict with api_key/base_url
    keys); otherwise this function looks it up from the executor
    state file. If no setup is found, raises ``RuntimeError`` —
    calling `_handle_chat` should detect this and fall back to v1.

    Raises:
        RuntimeError: no server-mode setup found for this sandbox
                      (fall back to v1).
        TimeoutError: task didn't complete within ``timeout``.
        ConnectionError: the in-sandbox python client couldn't spawn
                         (openshell CLI missing / sandbox not found).
    """
    sandbox_name = worker_id
    task_id = task.get("task_id")
    if not task_id:
        raise ValueError("dispatch_task_v2 requires task_id in the task payload")

    if setup is None:
        setup = _load_server_setup(sandbox_name)
    if setup is None:
        raise RuntimeError(
            f"dispatch_task_v2({sandbox_name}): no hermes_server_setup on "
            f"state record — was the sandbox spawned with "
            f"LOGOS_HERMES_SERVER_MODE=1?"
        )

    # Base64-encode the python client (avoids openshell exec's no-newlines-in-args
    # limitation) and invoke via sh stub that decodes + pipes into python3 -.
    # LOG-51.3: use a task-specific filename so cancel_task can pkill
    # exactly this run without affecting other concurrent dispatches
    # on the same sandbox.
    client_b64 = base64.b64encode(_IN_SANDBOX_CLIENT.encode()).decode()
    client_path = f"/tmp/_disp_v2_client_{task_id}.py"
    stub = (
        f"echo {client_b64} | base64 -d > {client_path} && "
        f"HERMES_BASE_URL={setup['base_url']} "
        f"HERMES_API_KEY={setup['api_key']} "
        f"python3 {client_path}"
    )

    # Resolve the sandbox's owning gateway and pass it via ``-g``.
    # Without this the CLI falls back to ``~/.config/openshell/active_gateway``,
    # which in any multi-route install is usually the wrong cluster →
    # ``NotFound: sandbox not found``. v1 has always done this; v2
    # was missing it until this fix (see worker_registry.py's
    # ``resolve_sandbox_gateway`` for the full rationale).
    from gateway.worker_registry import resolve_sandbox_gateway
    target_gateway = resolve_sandbox_gateway(sandbox_name)
    if target_gateway is None:
        raise ConnectionError(
            f"dispatch_task_v2({sandbox_name}): no gateway resolvable "
            f"for this sandbox. Not in the state file, no default "
            f"route configured, /setup may not have run yet."
        )

    cmd = [
        "openshell", "-g", target_gateway,
        "sandbox", "exec", "--no-tty",
        "--name", sandbox_name,
        "--", "sh", "-c", stub,
    ]

    logger.info(
        "dispatch_task_v2(%s): task=%s → %s/v1/runs",
        sandbox_name, task_id, setup["base_url"],
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
    except FileNotFoundError as exc:
        raise ConnectionError(
            f"openshell CLI not found — cannot dispatch_task_v2 to {sandbox_name}"
        ) from exc
    except Exception as exc:
        raise ConnectionError(
            f"Failed to spawn `openshell sandbox exec` for {sandbox_name}: {exc}"
        ) from exc

    # LOG-51.3: record the in-flight subprocess keyed by task_id so
    # /chat/{task_id}/cancel can reach us. Popped in the finally below.
    _INFLIGHT[task_id] = {
        "proc": proc,
        "sandbox_name": sandbox_name,
        "client_path": client_path,
    }

    # Pipe task JSON + close stdin (EOF unblocks openshell's exec gate).
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(task).encode())
    proc.stdin.write(b"\n")
    await proc.stdin.drain()
    proc.stdin.close()

    final_result: Optional[Dict[str, Any]] = None
    deadline = asyncio.get_running_loop().time() + timeout
    breaker = _CircuitBreaker()

    try:
        assert proc.stdout is not None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                raise
            if not line:
                break
            try:
                frame = json.loads(line.decode("utf-8").rstrip())
            except json.JSONDecodeError:
                logger.warning(
                    "dispatch_task_v2(%s): malformed stdout line: %r",
                    sandbox_name, line,
                )
                continue

            # Observe before pumping — we want tool_end events that lead
            # up to a trip to flow to the callback (agent_events
            # captures them) before we synthesise the abort.
            tripped = breaker.observe(frame)

            # Pump non-terminal frames to caller's callback
            if on_stream_event is not None and frame.get("type") != "task_result":
                try:
                    res = on_stream_event(frame)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as cb_exc:
                    logger.warning(
                        "dispatch_task_v2(%s): on_stream_event raised %s",
                        sandbox_name, cb_exc,
                    )

            if frame.get("type") == "task_result":
                final_result = frame
                break

            if tripped:
                logger.warning(
                    "dispatch_task_v2(%s) circuit breaker tripped: "
                    "tool=%r × %d identical failures, preview=%r — "
                    "cancelling run",
                    sandbox_name, breaker.tripped_tool, breaker.count,
                    breaker.tripped_preview,
                )
                # Fire the in-sandbox cancel so the agent actually stops
                # iterating (host proc terminate alone doesn't propagate
                # into the sandbox — see cancel_task docstring).
                cancel_task(task_id)
                final_result = {
                    "type": "task_result",
                    "task_id": task_id,
                    "status": "error",
                    "error": (
                        f"circuit_breaker: '{breaker.tripped_tool}' failed "
                        f"{breaker.count} times in a row with identical "
                        f"preview. Run aborted to prevent a runaway loop."
                    ),
                }
                break

        if final_result is None:
            # Subprocess exited without emitting task_result — surface as error
            stderr_bytes = b""
            if proc.stderr is not None:
                try:
                    stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=2)
                except asyncio.TimeoutError:
                    pass
            rc = await proc.wait()
            return {
                "type": "task_result",
                "task_id": task_id,
                "status": "error",
                "error": (
                    f"subprocess exited rc={rc} without task_result. "
                    f"stderr tail: {stderr_bytes.decode(errors='replace')[-500:]}"
                ),
            }
        return final_result

    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"dispatch_task_v2({sandbox_name}) task={task_id} exceeded "
            f"timeout={timeout}s"
        ) from exc
    finally:
        # LOG-51.3: drop the inflight entry before reaping so a late
        # cancel_task_v2 doesn't terminate a process we're already
        # about to reap.
        _INFLIGHT.pop(task_id, None)
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()


def cancel_task(task_id: str) -> bool:
    """Abort an in-flight v2 dispatch.

    Two-step termination because ``openshell sandbox exec`` does not
    propagate signals to the in-sandbox child:

    1. **Host side**: terminate the asyncio subprocess so
       ``dispatch_task_v2``'s read loop unwinds and returns an error.
    2. **In-sandbox side**: ``pkill -f`` the task-specific
       ``/tmp/_disp_v2_client_<task_id>.py`` process so the Python
       client dies, closes its SSE connection to hermes, and triggers
       the LOG-51.2 monkeypatch's disconnect branch — which calls
       ``agent.interrupt()`` so the agent actually stops iterating.

    Either step alone is insufficient: step 1 without step 2 leaves
    the agent running in the sandbox; step 2 without step 1 leaves a
    zombie host subprocess. Both are required.

    Returns True if a matching in-flight task was found and signalled,
    False if no such task exists (caller returns 404 / tries v1).
    The in-sandbox pkill is best-effort — if openshell is
    unreachable, cancel still returns True (host side was stopped);
    hermes will eventually complete the run and the agent will time
    out naturally at ``max_iterations``.
    """
    entry = _INFLIGHT.pop(task_id, None)
    if entry is None:
        return False

    proc = entry.get("proc")
    sandbox_name = entry.get("sandbox_name")
    client_path = entry.get("client_path") or ""

    # Step 1 — host side.
    if proc is not None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass  # already exited between our lookup and terminate
        except Exception as exc:
            logger.warning(
                "cancel_task(%s): host terminate raised: %s", task_id, exc,
            )

    # Step 2 — reach into the sandbox. pkill matches on the
    # task-specific client filename so we don't touch other concurrent
    # dispatches. Timeout is short: if the openshell CLI hangs, we'd
    # rather return and let the host side finish unwinding than block
    # the cancel endpoint.
    if sandbox_name and client_path:
        pattern = client_path.replace(".", "\\.")
        kill_cmd = [
            "openshell", "sandbox", "exec", "--no-tty",
            "--name", sandbox_name,
            "--", "sh", "-c", f"pkill -f {pattern} || true",
        ]
        try:
            r = subprocess.run(
                kill_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            if r.returncode != 0:
                logger.debug(
                    "cancel_task(%s): in-sandbox pkill rc=%d stderr=%r",
                    task_id, r.returncode,
                    (r.stderr or b"")[:200].decode(errors="replace"),
                )
        except subprocess.TimeoutExpired:
            logger.warning(
                "cancel_task(%s): in-sandbox pkill timed out after 5s "
                "(openshell CLI wedged?); agent may keep running to "
                "completion inside %s",
                task_id, sandbox_name,
            )
        except FileNotFoundError:
            logger.warning(
                "cancel_task(%s): openshell CLI not on PATH — "
                "cannot reach into sandbox to interrupt agent",
                task_id,
            )
        except Exception as exc:
            logger.warning(
                "cancel_task(%s): in-sandbox pkill raised: %s", task_id, exc,
            )

    logger.info(
        "cancel_task(%s): terminated v2 dispatch (host + in-sandbox %s)",
        task_id, sandbox_name or "?",
    )
    return True


def sandbox_has_server_mode(sandbox_name: str) -> bool:
    """Return True if the sandbox was spawned with Hermes-server mode
    and has a stashed HermesServerSetup on its state record.

    Use this in `_handle_chat` to decide v1 vs v2 routing without
    catching RuntimeError from dispatch_task_v2 on every chat.
    """
    return _load_server_setup(sandbox_name) is not None


def is_dispatch_v2_enabled() -> bool:
    """Return True iff the LOG-44 dispatch-v2 routing is active for this run."""
    return os.getenv("LOGOS_DISPATCH_V2", "") == "1"


__all__ = [
    "dispatch_task_v2",
    "cancel_task",
    "sandbox_has_server_mode",
    "is_dispatch_v2_enabled",
    "DEFAULT_TASK_TIMEOUT",
]
