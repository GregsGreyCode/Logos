"""
LOG-44 Phase 1 prototype — dispatch_task_v2

Host-side dispatcher that replaces the per-task `sandbox_worker.py` subprocess
with an HTTP call into a long-lived `hermes gateway run` inside the sandbox.

Transport: `openshell sandbox exec --no-tty --name <sandbox> -- sh -c "..."`
(same primitive as v1, zero new attack surface). The shell script inside
the sandbox runs a minimal Python client that:
  1. POSTs the task to `http://127.0.0.1:8642/v1/runs` (auth: Bearer)
  2. Opens `GET /v1/runs/<run_id>/events` (SSE)
  3. Emits one-per-line JSON frames on stdout matching the shape Logos's
     existing `dispatch_task` outer layer already consumes.

Frame translation:
  Hermes SSE event            → Logos frame
  -----------------------------+-------------------------
  message.delta (delta: "…")  → {"type":"token","task_id":…,"content":"…"}
  reasoning.available (text:) → {"type":"thinking","task_id":…,"content":"…"}
  tool.start  (tool: "…")     → {"type":"tool_start","task_id":…,"tool":"…"}
  tool.end    (tool, duration)→ {"type":"tool_end","task_id":…,"tool":"…","duration":…}
  run.completed (output, usage)→ {"type":"task_result","task_id":…,"status":"ok","final_response":"…","usage":{…}}
  : stream closed             → (terminator; stop reading)

Run against a hermes-henry sandbox that already has `hermes gateway run`
spun up (see prototype doc — launched manually for now; Phase 2 will
wire it into OpenShellExecutor.spawn).

Usage:
    python3 dispatch_v2.py "say hi in 3 words"
"""

import asyncio
import base64
import json
import shlex
import subprocess
import sys
import uuid
from typing import Any, AsyncIterator, Dict, Optional

SANDBOX_NAME = "hermes-henry"
HERMES_HOST = "127.0.0.1"
HERMES_PORT = 8642
API_SERVER_KEY = "proto-test-key-abc123"

# The Python client that runs INSIDE the sandbox. It reads the task JSON
# from its own stdin (piped in through openshell sandbox exec), makes the
# HTTP call, and emits frames to stdout. No other deps beyond stdlib.
#
# Care needed: openshell sandbox exec rejects args containing literal
# newlines, so we base64-encode this whole script and decode on the other
# side via `sh -c "echo $B64 | base64 -d | python3 -"`.
IN_SANDBOX_CLIENT = r"""
import sys, json, urllib.request, urllib.error

BASE = "http://%(host)s:%(port)d"
AUTH = {"Authorization": "Bearer %(key)s", "Content-Type": "application/json"}

def emit(frame):
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()

def main():
    raw = sys.stdin.read()
    try:
        task = json.loads(raw)
    except Exception as e:
        emit({"type": "task_result", "task_id": "?", "status": "error",
              "error": f"malformed task JSON on stdin: {e}"})
        return 2
    task_id = task.get("task_id") or "?"

    # Start the run
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
        emit({"type": "task_result", "task_id": task_id, "status": "error",
              "error": f"runs POST {e.code}: {e.read().decode()[:200]}"})
        return 3
    except Exception as e:
        emit({"type": "task_result", "task_id": task_id, "status": "error",
              "error": f"runs POST failed: {type(e).__name__}: {e}"})
        return 3

    run_id = run.get("run_id") or run.get("id")
    if not run_id:
        emit({"type": "task_result", "task_id": task_id, "status": "error",
              "error": f"runs POST returned no run_id: {run}"})
        return 4

    # Emit a ready-ish frame so the outer dispatch knows we're alive
    emit({"type": "ready", "task_id": task_id, "run_id": run_id})

    # Stream events
    ev_req = urllib.request.Request(
        BASE + "/v1/runs/" + run_id + "/events",
        headers={"Authorization": AUTH["Authorization"]},
    )
    try:
        ev_r = urllib.request.urlopen(ev_req, timeout=600)
    except Exception as e:
        emit({"type": "task_result", "task_id": task_id, "status": "error",
              "error": f"events GET failed: {type(e).__name__}: {e}"})
        return 5

    accumulated = []
    final_output = None
    final_usage = None
    final_error = None
    for raw_line in ev_r:
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        if line.startswith(": "):
            # SSE comment line (e.g. ": stream closed")
            if "stream closed" in line or "done" in line:
                break
            continue
        if line.startswith("data: "):
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            kind = ev.get("event")
            if kind == "message.delta":
                delta = ev.get("delta", "")
                if delta:
                    accumulated.append(delta)
                    emit({"type": "token", "task_id": task_id, "content": delta})
            elif kind == "reasoning.available":
                txt = ev.get("text", "")
                if txt:
                    emit({"type": "thinking", "task_id": task_id, "content": txt})
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
        emit({"type": "task_result", "task_id": task_id, "status": "error",
              "error": final_error})
        return 6

    emit({
        "type": "task_result",
        "task_id": task_id,
        "status": "ok",
        "final_response": final_output if final_output is not None else "".join(accumulated),
        "usage": final_usage or {},
    })
    return 0

sys.exit(main())
""" % {"host": HERMES_HOST, "port": HERMES_PORT, "key": API_SERVER_KEY}


async def dispatch_task_v2(
    sandbox_name: str,
    task: Dict[str, Any],
    timeout: float = 300.0,
) -> AsyncIterator[Dict[str, Any]]:
    """Async generator yielding frames from the in-sandbox dispatch.

    The generator yields all non-terminal frames (ready, token, thinking,
    tool_start, tool_end) and finishes with a single task_result frame.
    """
    task_id = task.get("task_id") or f"t_{uuid.uuid4().hex[:12]}"
    task.setdefault("task_id", task_id)

    client_b64 = base64.b64encode(IN_SANDBOX_CLIENT.encode()).decode()
    # The shell stub: decode our python client, pipe task JSON into it on stdin
    stub = (
        f"echo {client_b64} | base64 -d > /tmp/_disp_v2_client.py && "
        f"python3 /tmp/_disp_v2_client.py"
    )

    cmd = [
        "openshell", "sandbox", "exec", "--no-tty",
        "--name", sandbox_name,
        "--", "sh", "-c", stub,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )

    # Write task JSON on stdin and close
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(task).encode())
    proc.stdin.write(b"\n")
    await proc.stdin.drain()
    proc.stdin.close()

    try:
        async def _read_frames() -> AsyncIterator[Dict[str, Any]]:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    yield json.loads(line.decode("utf-8").rstrip())
                except json.JSONDecodeError:
                    # swallow garbage; log to stderr of parent
                    print(f"[v2] malformed stdout line: {line!r}", file=sys.stderr)
                    continue

        terminal_seen = False
        async for frame in _read_frames():
            yield frame
            if frame.get("type") == "task_result":
                terminal_seen = True
                break

        if not terminal_seen:
            # Subprocess exited without emitting a task_result — surface as error
            stderr = await proc.stderr.read() if proc.stderr else b""
            rc = await proc.wait()
            yield {
                "type": "task_result",
                "task_id": task_id,
                "status": "error",
                "error": f"subprocess exited rc={rc} without task_result. stderr tail: {stderr.decode()[-500:]}",
            }
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()


async def _main():
    message = sys.argv[1] if len(sys.argv) > 1 else "Say hi in 3 words"
    task = {
        "task_id": f"proto_{uuid.uuid4().hex[:8]}",
        "message": message,
        "system_prompt": "Be concise.",
    }
    print(f"[v2] dispatching: {task['message']!r}", file=sys.stderr)
    async for frame in dispatch_task_v2(SANDBOX_NAME, task):
        t = frame.get("type")
        if t == "token":
            sys.stdout.write(frame["content"])
            sys.stdout.flush()
        elif t == "thinking":
            print(f"\n[thinking] {frame['content'][:100]}", file=sys.stderr)
        elif t == "tool_start":
            print(f"\n[tool.start] {frame.get('tool')}", file=sys.stderr)
        elif t == "tool_end":
            print(f"\n[tool.end] {frame.get('tool')} ({frame.get('duration')}s)", file=sys.stderr)
        elif t == "ready":
            print(f"[ready] run_id={frame.get('run_id')}", file=sys.stderr)
        elif t == "task_result":
            print(f"\n[task_result] status={frame.get('status')} usage={frame.get('usage')}", file=sys.stderr)
            if frame.get("status") == "error":
                print(f"  error: {frame.get('error')}", file=sys.stderr)
        else:
            print(f"[?] {frame}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(_main())
