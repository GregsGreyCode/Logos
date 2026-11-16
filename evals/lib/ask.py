"""Runs inside an agent sandbox: send one message (argv[1], or stdin) to the local hermes API in a
fresh conversation and print the reply."""
import json, sys, urllib.request

KEY = dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l)["API_SERVER_KEY"]
prompt = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
body = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": prompt}]}).encode()
req = urllib.request.Request("http://127.0.0.1:8642/v1/chat/completions", data=body,
                             headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=600))["choices"][0]["message"].get("content") or "")
