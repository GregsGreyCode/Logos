# Dispatch worker model (Plan A-prime)

**Status:** current as of 2026-04-15, OpenShell `m-dev` (post-v0.0.29 rolling)
**Decision:** Spawn a fresh `python3 /tmp/sandbox_worker.py` subprocess via
`openshell sandbox exec --no-tty` for **every dispatch** (one turn = one
subprocess). No persistent in-sandbox worker process.

## Problem

Earlier versions of Plan A kept a single persistent worker per sandbox
with a stdin protocol loop: the gateway would write a JSON task frame,
the worker would process it and emit response frames on stdout, then
block waiting for the next frame. That architecture was impossible on
top of `openshell sandbox exec` because **the exec primitive refuses to
start the in-sandbox command until stdin reaches EOF** (proven directly;
verified in OpenShell `m-dev` / v0.0.29 source — see below).

A persistent stdin protocol requires stdin to stay open across many
frames. The exec gate treats that as "still waiting for input" and the
in-sandbox process never actually launches. The gateway leaked one
blocked thread and one orphan `openshell` CLI process per would-be
worker, while clients saw spawn calls hang for 300s+ until the reaper
killed them.

## What we chose

Per-dispatch spawn (Plan A-prime):

```
gateway.worker_registry.dispatch_task(sandbox_name, task_json)
  → openshell sandbox exec --no-tty --name <sandbox> --
      bash -c "HERMES_HOME=/tmp/hermes exec python3 /tmp/sandbox_worker.py"
  → write task_json to stdin
  → close stdin   (the EOF unblocks openshell's exec gate)
  → stream frames from stdout, stderr, wait for exit
```

Each dispatch is a fresh Python interpreter, fresh AIAgent import,
fresh MCP client connections, fresh everything. Conversation history
travels in the task JSON, not in process state.

## Tradeoffs — honest inventory

### Costs we accepted

- **Cold-start tax per dispatch.** Measured ~1.3-2.8s on the current
  image (from first stderr log to "Worker starting" ready) — dominated
  by Python + `run_agent` + transitive hermes tool imports. The code
  comment at `gateway/executors/openshell.py:28` claims "~0.2s for
  python + aiohttp import" but that predates the heavier AIAgent
  import and is **stale** — the real number is multiples of that.
  Chromium launch adds another ~2s when the agent has the `browser`
  toolset.

- **No warm state between turns.** Can't keep LLM tokenizer caches,
  browser context, MCP client sessions, or compiled regex cached
  across messages. Each dispatch pays full init cost for every
  subsystem.

- **MCP reconnect per turn.** `discover_mcp_tools()` runs at module
  load (`run_agent` imports `model_tools` → imports `tools.mcp_tool`
  → connects to every configured MCP server, initializes the session,
  runs `tools/list`). For N servers this is roughly N × 150ms of
  wall-clock overhead added to every dispatch. Real-world measurement
  (echo-test): 3 POSTs to the gateway proxy adding ~150ms total.
  Scales linearly.

- **Conversation history is re-serialized every turn.** For long chats
  the stdin payload grows. Not a problem today; becomes one at ~50+
  turn chats if tool outputs are verbose.

- **Debugging is awkward.** No live process to attach gdb/py-spy to.
  You can't `strace` an in-flight MCP call unless you catch the short
  window between exec start and task completion.

### What we got in exchange

- **Crash isolation.** A segfault or OOM in one dispatch tears down
  that subprocess only. The next message starts fresh.

- **Config changes take effect immediately.** No "reload worker"
  dance — the next message re-reads `/tmp/hermes/instance-config.json`
  and `~/.hermes/config.yaml` from scratch. Toggling an MCP server
  or a credential in the UI is live on the next turn.

- **No protocol state machine.** The old design had a stdin-framed
  RPC protocol with frames for ready/task/cancel/shutdown. Per-
  dispatch spawn makes that a single task-in, frames-out, exit
  flow. Much less code to keep correct.

- **Architectural match to what OpenShell actually offers.** Fighting
  the exec primitive's stdin-EOF contract cost us more than the
  cold-start tax does.

## Does newer OpenShell change any of this?

**As of 2026-04-15 (commit 355d845d, post-v0.0.29): no.**

Verified against the fresh `knowledge-repos/openshell` clone:

- `proto/openshell.proto:357-378` defines `ExecSandboxRequest` with a
  single `stdin: bytes` field. Not a stream, not a reusable channel.
  Write-once.

- `crates/openshell-server/src/grpc/sandbox.rs:412-471` handles exec:
  it passes the stdin payload to SSH transport at line 458 and closes
  the channel immediately after the command exits. There is no
  persistent-stdin mode.

- No `--stdin-open`, `--interactive`, `--persistent`, or streaming-
  RPC variant of exec exists. `--tty` exists but doesn't help — it
  still closes the stream at command exit.

- **The only alternative for persistent in-sandbox processes is
  `CreateSshSessionRequest`** (a different RPC). That opens an SSH
  session to the sandbox that the caller manages directly. Would let
  us keep a worker alive, but we'd be implementing a new gRPC client
  layer to replace what we have via the exec shim, and SSH session
  management adds its own failure modes (heartbeats, reconnect,
  session table cleanup on gateway restart).

**Triggers to revisit:**

- OpenShell ships a "long-running workload" primitive — streaming
  RPC or persistent-exec mode — that lets us keep a single worker
  alive per sandbox without owning the SSH session lifecycle.

- Cold-start tax becomes a UX problem. Currently ~1.5-3s is hidden
  inside the first-token-latency budget, which itself is 2-30s for
  inference. When fast local models (e.g. sub-second first token)
  become the common case, the init overhead will dominate and we'll
  need to solve it.

- We move to an architecture where a single sandbox serves multiple
  agents. Per-dispatch spawn multiplied by number of agents × rate
  of concurrent messages stops making sense.

## Subtle footguns we've hit

- **`HERMES_HOME` vs `$HOME/.hermes`.** The dispatch command sets
  `HERMES_HOME=/tmp/hermes`. Upstream hermes's `get_hermes_home()`
  returns that as the home directly (not appending `.hermes`). Any
  code that writes config to `$HOME/.hermes/<file>` is writing to
  the wrong path during dispatch — upstream reads from `$HERMES_HOME/
  <file>`. Cost us ~30 minutes debugging "config works in test but
  not in dispatch" on 2026-04-15. Fix: check `HERMES_HOME` first,
  fall back to `$HOME/.hermes` only when unset. See `docker/sandbox_
  worker.py:load_config()`.

- **Module-load-time discovery.** Anything expensive or fragile that
  runs at `import run_agent` time (notably `discover_mcp_tools`) runs
  on every dispatch. Breaking it — a stale config file, an
  unreachable server, etc. — breaks every dispatch silently because
  the exception is caught-and-debug-logged inside `model_tools.py`.
  Watch for `tools.mcp_tool: MCP: registered N tool(s) from M
  server(s) (K failed)` on every dispatch; zero/K failed is a red
  flag even when the dispatch "succeeds."

- **Logging visibility.** The worker's stderr is line-tagged with
  `[worker:<name>]` and sent to the gateway log. Module-load-time
  logs from imports (e.g. MCP discovery) appear there but only if
  they've been emitted to stderr by the time stderr is drained. One
  consequence: `tools.mcp_tool: MCP server ... registered N tool(s)`
  shows up in every dispatch log **if** discovery runs. Missing that
  line is a signal, not noise.

## Related code

- `gateway/worker_registry.py` — dispatch_task, stderr drain loop,
  subprocess lifecycle.
- `gateway/executors/openshell.py` — spawn/delete/refresh_instance_
  config, spawn-time bookkeeping.
- `docker/sandbox_worker.py` — the worker entry point, load_config,
  run_one_task.
- `knowledge-repos/openshell/proto/openshell.proto` — ExecSandboxRequest
  shape.
- `knowledge-repos/openshell/crates/openshell-server/src/grpc/
  sandbox.rs` — exec handler that closes stdin post-exit.
