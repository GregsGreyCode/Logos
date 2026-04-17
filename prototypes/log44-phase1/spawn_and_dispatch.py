"""
LOG-44 Phase 1 — end-to-end cycle test

Exercises the full spawn-to-dispatch path against a live OpenShell sandbox:

  1. Pick an existing sandbox (hermes-henry) — Phase 2 will add sandbox creation.
  2. Write /tmp/hermes-proto-home/{config.yaml,.env} via openshell sandbox exec.
  3. Launch `hermes gateway run` in background via openshell sandbox exec nohup.
  4. Poll /health until 200 (readiness gate).
  5. Dispatch a chat via dispatch_task_v2, stream the response.
  6. Leave gateway running for manual follow-up (do NOT tear it down —
     the test sandbox is shared with the running Logos gateway's v1 path).

Usage:
    python3 spawn_and_dispatch.py
    python3 spawn_and_dispatch.py --message "What's 2+2?"
    python3 spawn_and_dispatch.py --stop    # stop the bg gateway only

This is the script OpenShellExecutor.spawn should eventually do inline.
"""

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Make dispatch_v2 importable
sys.path.insert(0, str(Path(__file__).parent))
from dispatch_v2 import dispatch_task_v2, API_SERVER_KEY  # noqa: E402

SANDBOX_NAME = "hermes-henry"
HERMES_PORT = 8642


def exec_in_sandbox(script: str, sandbox: str = SANDBOX_NAME, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a shell script in the sandbox via openshell sandbox exec.

    Base64-encodes to bypass the `no-newlines-in-args` limitation of exec.
    Closes stdin so exec actually invokes the in-sandbox process.
    """
    b64 = base64.b64encode(script.encode()).decode()
    cmd = [
        "openshell", "sandbox", "exec", "--no-tty",
        "--name", sandbox,
        "--", "sh", "-c", f"echo {b64} | base64 -d | sh",
    ]
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )


def deploy_config(sandbox: str = SANDBOX_NAME) -> None:
    """Write config.yaml + .env inside the sandbox."""
    script = """
set -e
mkdir -p /tmp/hermes-proto-home/memories /tmp/hermes-proto-home/sessions /tmp/hermes-proto-home/logs
printf '%s\\n' 'model:' '  default: gpt-oss-20b' '  provider: custom' '  base_url: https://inference.local/v1' 'api_server:' '  enabled: true' '  host: 127.0.0.1' '  port: 8642' > /tmp/hermes-proto-home/config.yaml
printf '%s\\n' 'API_SERVER_KEY=proto-test-key-abc123' 'OPENAI_API_KEY=lm-studio' 'OPENAI_BASE_URL=https://inference.local/v1' > /tmp/hermes-proto-home/.env
echo '[config] written'
cat /tmp/hermes-proto-home/config.yaml
"""
    r = exec_in_sandbox(script)
    print(r.stdout, end="")
    if r.returncode != 0:
        print(f"[config] deploy failed: rc={r.returncode}")
        print(r.stderr, file=sys.stderr)
        sys.exit(2)


def launch_gateway(sandbox: str = SANDBOX_NAME) -> None:
    """Launch `hermes gateway run` in background. Idempotent: if already running, no-op."""
    script = """
if pgrep -f 'hermes gateway run' > /dev/null 2>&1; then
  echo '[launch] already running'
  exit 0
fi
rm -f /tmp/hermes-gw.log
HERMES_HOME=/tmp/hermes-proto-home nohup hermes gateway run -v > /tmp/hermes-gw.log 2>&1 &
sleep 1
echo "[launch] bg pid=$(pgrep -f 'hermes gateway run' | head -1)"
"""
    r = exec_in_sandbox(script)
    print(r.stdout, end="")
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        sys.exit(3)


def stop_gateway(sandbox: str = SANDBOX_NAME) -> None:
    script = """
pkill -9 -f 'hermes gateway run' 2>/dev/null || true
sleep 1
if pgrep -f 'hermes gateway run' > /dev/null 2>&1; then
  echo '[stop] still running'
  exit 1
fi
echo '[stop] gone'
"""
    r = exec_in_sandbox(script)
    print(r.stdout, end="")


def wait_for_health(sandbox: str = SANDBOX_NAME, timeout_s: int = 30) -> bool:
    """Poll /health until 200 or timeout. Returns True on success."""
    deadline = time.time() + timeout_s
    probe = """
python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://127.0.0.1:8642/health', timeout=2)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
"
"""
    while time.time() < deadline:
        r = exec_in_sandbox(probe, timeout=10)
        if r.returncode == 0:
            elapsed = timeout_s - (deadline - time.time())
            print(f"[health] ready after {elapsed:.1f}s")
            return True
        time.sleep(1)
    print(f"[health] TIMEOUT after {timeout_s}s")
    return False


async def dispatch_one(message: str) -> int:
    task = {
        "task_id": f"e2e_{uuid.uuid4().hex[:8]}",
        "message": message,
        "system_prompt": "Be concise.",
    }
    print(f"[dispatch] task_id={task['task_id']} message={message!r}")
    ok = False
    async for frame in dispatch_task_v2(SANDBOX_NAME, task):
        t = frame.get("type")
        if t == "ready":
            print(f"[dispatch] ready run_id={frame.get('run_id')}")
        elif t == "token":
            sys.stdout.write(frame["content"])
            sys.stdout.flush()
        elif t == "task_result":
            print(f"\n[dispatch] status={frame.get('status')} usage={frame.get('usage')}")
            if frame.get("status") == "error":
                print(f"[dispatch] error: {frame.get('error')}")
                return 1
            ok = True
    return 0 if ok else 2


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", default="What is 2+2? One sentence.")
    ap.add_argument("--stop", action="store_true", help="just stop the bg gateway and exit")
    ap.add_argument("--skip-config", action="store_true", help="skip config redeploy (use existing)")
    args = ap.parse_args()

    if args.stop:
        stop_gateway()
        return 0

    print("=" * 60)
    print("LOG-44 Phase 1 end-to-end cycle test")
    print("=" * 60)
    if not args.skip_config:
        print("\n[1/4] deploy config")
        deploy_config()
    print("\n[2/4] launch hermes gateway run")
    launch_gateway()
    print("\n[3/4] wait for /health")
    if not wait_for_health():
        print("[FAIL] gateway not ready")
        return 4
    print("\n[4/4] dispatch task via v2")
    rc = await dispatch_one(args.message)
    print("\n" + "=" * 60)
    print(f"RESULT: {'PASS' if rc == 0 else 'FAIL'} (rc={rc})")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
