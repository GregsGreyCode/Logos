"""
End-to-end test of the full LOG-44 Phase 1 integration:
  1. enable_hermes_server_mode(sandbox)  → HermesServerSetup
  2. dispatch_task_v2(sandbox, task, setup) → streams frames → task_result

Exercises the real production integration shape — same signature that
WorkerRegistry.dispatch_task uses, same callback pattern.
"""

import asyncio
import logging
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from gateway.executors.hermes_server_mode import enable_hermes_server_mode  # noqa: E402
from gateway.worker_registry_v2 import dispatch_task_v2  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SANDBOX = "hermes-henry"


class StubConfig:
    def __init__(self, name, model_name, system_prompt=None):
        self.name = name
        self.model_name = model_name
        self.system_prompt = system_prompt


def _stop_prior():
    import base64
    script = "pkill -9 -f 'hermes gateway run' 2>/dev/null || true; sleep 1; echo ok"
    b64 = base64.b64encode(script.encode()).decode()
    subprocess.run(
        ["openshell", "sandbox", "exec", "--no-tty", "--name", SANDBOX,
         "--", "sh", "-c", f"echo {b64} | base64 -d | sh"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
    )


async def main():
    print(f"[1/3] clean prior gateway state in {SANDBOX}")
    _stop_prior()

    print(f"\n[2/3] enable_hermes_server_mode({SANDBOX})")
    cfg = StubConfig(name="henry", model_name="gpt-oss-20b",
                     system_prompt="Be concise and truthful.")
    setup = enable_hermes_server_mode(SANDBOX, cfg)
    setup_dict = {
        "api_key": setup.api_key,
        "base_url": setup.base_url,
        "hermes_home": setup.hermes_home,
    }
    print(f"  setup: api_key={setup.api_key[:12]}… base_url={setup.base_url}")

    print(f"\n[3/3] dispatch_task_v2 — streaming")
    task = {
        "task_id": f"v2_{uuid.uuid4().hex[:8]}",
        "message": "What year did the Apollo 11 mission land on the moon? One word.",
        "system_prompt": "Be terse.",
    }

    tokens_seen = []
    thinking_seen = []
    tool_events_seen = []

    def on_event(frame):
        t = frame.get("type")
        if t == "token":
            tokens_seen.append(frame["content"])
            sys.stdout.write(frame["content"])
            sys.stdout.flush()
        elif t == "thinking":
            thinking_seen.append(frame["content"])
        elif t in ("tool_start", "tool_end"):
            tool_events_seen.append(frame)
        elif t == "ready":
            print(f"[ready] run_id={frame.get('run_id')}")

    result = await dispatch_task_v2(
        SANDBOX, task,
        timeout=90.0,
        on_stream_event=on_event,
        setup=setup_dict,  # pass through explicitly, skip state-file lookup
    )

    print("\n")
    print("=" * 60)
    status = result.get("status")
    final = result.get("final_response", "")
    usage = result.get("usage", {})
    print(f"status={status}")
    print(f"final_response={final!r}")
    print(f"usage={usage}")
    print(f"token frames seen: {len(tokens_seen)}")
    print(f"thinking frames seen: {len(thinking_seen)}")
    print(f"tool events seen: {len(tool_events_seen)}")
    assert status == "ok", f"expected ok, got {status}: {result.get('error')}"
    assert "1969" in final, f"expected '1969' in response, got: {final!r}"
    print("\nPASS — v2 dispatch end-to-end works")


if __name__ == "__main__":
    asyncio.run(main())
