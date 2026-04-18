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

    message.delta (delta: "…")   → {"type":"token","task_id":…,"content":"…"}
    reasoning.available (text:)  → {"type":"thinking","task_id":…,"content":"…"}
    tool.start  (tool: "…")      → {"type":"tool_start","task_id":…,"tool":"…"}
    tool.end    (tool, duration) → {"type":"tool_end","task_id":…,"tool":"…","duration":…}
    run.completed (output,usage) → {"type":"task_result","task_id":…,"status":"ok","final_response":"…","usage":{…}}
    run.failed                   → {"type":"task_result","task_id":…,"status":"error","error":"…"}

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
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TASK_TIMEOUT = 600.0   # generous; gateway owns its own per-run timeouts

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
        elif kind == "tool.start":
            emit({"type": "tool_start", "task_id": task_id,
                  "tool": ev.get("tool"), "timestamp": ev.get("timestamp")})
        elif kind == "tool.end":
            emit({"type": "tool_end", "task_id": task_id,
                  "tool": ev.get("tool"), "duration": ev.get("duration"),
                  "error": ev.get("error", False)})
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

    cmd = [
        "openshell", "sandbox", "exec", "--no-tty",
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
