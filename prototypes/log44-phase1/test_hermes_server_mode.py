"""
Exercises gateway/executors/hermes_server_mode.py against a live sandbox.

Uses HERMES_HOME=/tmp/hermes-srv-home (separate from the earlier
/tmp/hermes-proto-home) so it co-exists with the prototype scripts
without conflict. Stops any pre-existing hermes-server-mode process
first to exercise the cold-start path.
"""

import logging
import subprocess
import sys
import urllib.request
from pathlib import Path

# Make gateway.executors.hermes_server_mode importable
REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from gateway.executors.hermes_server_mode import (  # noqa: E402
    HERMES_BIND_HOST,
    HERMES_BIND_PORT,
    HERMES_HOME_IN_SANDBOX,
    enable_hermes_server_mode,
    is_enabled,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SANDBOX = "hermes-henry"


class StubConfig:
    """Minimal InstanceConfig stand-in for the helper's interface."""
    def __init__(self, name: str, model_name: str, system_prompt: str | None = None):
        self.name = name
        self.model_name = model_name
        self.system_prompt = system_prompt


def _stop_prior_bg_gateway() -> None:
    import base64
    script = """
pkill -9 -f 'hermes gateway run' 2>/dev/null || true
sleep 1
pgrep -f 'hermes gateway run' >/dev/null && echo 'still running' || echo 'clean'
"""
    b64 = base64.b64encode(script.encode()).decode()
    r = subprocess.run(
        ["openshell", "sandbox", "exec", "--no-tty", "--name", SANDBOX,
         "--", "sh", "-c", f"echo {b64} | base64 -d | sh"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
    )
    print(f"[pre-stop] {r.stdout.strip()}")


def test_probe_api(setup) -> None:
    """After enable_hermes_server_mode returns, probe the API from a host-side
    python client via `openshell sandbox exec python3 -c ...`."""
    import base64
    probe = f'''
import urllib.request, json, sys
hdr = {{"Authorization": "Bearer {setup.api_key}"}}
# /health (public)
r = urllib.request.urlopen("{setup.base_url}/health", timeout=3)
print("HEALTH", r.status, r.read().decode()[:100])
# /v1/models (auth required)
req = urllib.request.Request("{setup.base_url}/v1/models", headers=hdr)
r = urllib.request.urlopen(req, timeout=3)
print("MODELS", r.status, r.read().decode()[:200])
'''
    b64 = base64.b64encode(probe.encode()).decode()
    r = subprocess.run(
        ["openshell", "sandbox", "exec", "--no-tty", "--name", SANDBOX,
         "--", "sh", "-c", f"echo {b64} | base64 -d | python3 -"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
    )
    print("--- API probe ---")
    print(r.stdout)
    if r.returncode != 0:
        print("stderr:", r.stderr)
    assert r.returncode == 0, f"probe failed rc={r.returncode}"
    assert "HEALTH 200" in r.stdout, "health check did not return 200"
    assert "MODELS 200" in r.stdout, "models endpoint did not return 200 with bearer"


def main():
    assert not is_enabled(), "shouldn't be running — is_enabled() checks env var"
    print(f"Test target: sandbox={SANDBOX}")
    print(f"HERMES_HOME inside sandbox: {HERMES_HOME_IN_SANDBOX}")
    print(f"Target bind: {HERMES_BIND_HOST}:{HERMES_BIND_PORT}\n")

    print("[1/3] stop any prior hermes gateway process in sandbox")
    _stop_prior_bg_gateway()

    print("\n[2/3] enable_hermes_server_mode — deploy config + launch + health")
    cfg = StubConfig(
        name="henry",
        model_name="gpt-oss-20b",
        system_prompt="You are a test agent. Be concise.",
    )
    setup = enable_hermes_server_mode(SANDBOX, cfg)
    print(f"\n  → setup: api_key={setup.api_key[:12]}… base_url={setup.base_url}")

    print("\n[3/3] probe API from inside sandbox")
    test_probe_api(setup)

    print("\n" + "=" * 60)
    print("RESULT: PASS — hermes_server_mode helper module is sound")
    print("=" * 60)


if __name__ == "__main__":
    main()
