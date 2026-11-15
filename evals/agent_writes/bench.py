#!/usr/bin/env python3
"""Compare local models on whether memory and file writes really happen (story S3.4).

For each model: load it in LM Studio with the agent's context length, recreate the `bench`
agent on it with empty volumes, run evals/agent_writes/run_in_sandbox.py inside the sandbox,
and append the results to a JSON file.

Usage: evals/agent_writes/bench.py OUT.json REPS MODEL [MODEL...]
"""
import json, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
import yaml

REPO = Path(__file__).resolve().parents[2]
DEF = REPO / "agents" / "bench.yaml"
AGENT = [sys.executable, str(REPO / "scripts" / "logos-agent")]


def lm(base, path, body=None, timeout=600):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LM Studio {path} -> HTTP {e.code}: {e.read()[:200].decode(errors='replace')}") from e


def main(out, reps, models):
    agent = yaml.safe_load(DEF.read_text())
    base = agent["model"]["base_url"].rsplit("/v1", 1)[0]
    ctx = int(agent["model"]["context_length"])
    results = json.loads(Path(out).read_text()) if Path(out).exists() else {}
    for model in models:
        entry = {"started": time.strftime("%H:%M:%S")}
        try:
            for m in lm(base, "/api/v0/models")["data"]:
                if m.get("state") == "loaded":
                    lm(base, "/api/v1/models/unload", {"instance_id": m["id"]})
            entry["load_s"] = lm(base, "/api/v1/models/load", {"model": model, "context_length": ctx}).get("load_time_seconds")
            agent["model"]["default"] = model
            DEF.write_text(yaml.safe_dump(agent, sort_keys=False))
            subprocess.run([*AGENT, "down", "bench"], capture_output=True)
            subprocess.run(["docker", "volume", "rm", "logos-home-bench", "logos-brain-bench"], capture_output=True)
            up = subprocess.run([*AGENT, "up", "bench"], capture_output=True, text=True)
            if up.returncode != 0:
                raise RuntimeError("agent up failed: " + (up.stdout + up.stderr)[-200:])
            # First boot on empty volumes seeds the hermes home; wait for the API, don't guess.
            for _ in range(60):
                h = subprocess.run(["openshell", "sandbox", "exec", "--name", "bench", "--no-tty", "--", "sh", "-c",
                                    "curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:8642/health"],
                                   capture_output=True, text=True)
                if h.stdout.strip().endswith("200"):
                    break
                time.sleep(3)
            else:
                raise RuntimeError("hermes API did not come up in 180 s")
            subprocess.run(["openshell", "sandbox", "upload", "bench", str(Path(__file__).with_name("run_in_sandbox.py")),
                            "/tmp/bench"], capture_output=True)
            run = subprocess.run(["openshell", "sandbox", "exec", "--name", "bench", "--no-tty", "--", "sh", "-c",
                                  f"python $(find /tmp/bench -name run_in_sandbox.py | head -1) {reps}"],
                                 capture_output=True, text=True, timeout=reps * 1800)
            entry["tasks"] = json.loads(run.stdout.strip().splitlines()[-1])
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        results[model] = entry
        Path(out).write_text(json.dumps(results, indent=1))
        print(model, "done" if "tasks" in entry else entry.get("error"), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3:])
