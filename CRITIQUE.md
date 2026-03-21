# Staff Engineer Review: Logos Platform

> Critical architecture audit prepared for technical interview / pre-public review.
> Every finding references specific files and line numbers in the codebase.

---

## 1. Critical Issues (Fix Before Public / Interview)

**1. No health check endpoint.**
The K8s deployment has no liveness or readiness probe. Kubernetes has no signal beyond "container is running." A hung gateway process that's no longer processing messages will never be restarted. Every production service needs `/healthz`. This is table stakes.

**2. Tool execution has no top-level timeout.**
`gateway/run.py:1544` calls `_run_agent()` with no timeout wrapper. Individual tools have timeouts, but the agent loop itself does not. One hung MCP call or blocked network request stalls the entire platform adapter thread. On a single-process gateway, that means Telegram stops responding while Discord keeps working — users see intermittent failures with no signal.

**3. SQLite single connection shared across all platform threads.**
`hermes_state.py:176` — `check_same_thread=False` with a single connection object, no connection pool, no `threading.Lock()`. WAL mode supports concurrent readers but only one writer. Under concurrent load from multiple platform adapters, write contention causes `OperationalError: database is locked` at 10-second timeouts. The timeout is per-query, not per-transaction. This is a correctness risk at any meaningful concurrency level.

**4. Session and run state have no atomicity guarantees.**
Sessions are dual-persisted to SQLite and JSONL (`gateway/session.py:396-417`), but the two writes are not transactional. A crash between them leaves the stores out of sync. Runs are recorded incrementally (`runs.py:80-94`) with exceptions suppressed (`runs.py:122-126`). Tools execute with real side effects, but if the process dies before the DB write completes, the run record is incomplete with no recovery path.

**5. Workspace isolation is Python-level only — explicitly stated in the code.**
`tools/workspace.py:4-34` includes its own disclaimer that a sufficiently capable agent can escape via direct syscalls, shell builtins, or symlink tricks. The `_REDIRECT_WRITE_RE` pattern at line 183 doesn't catch subshells (`(cat > /etc/file)`), heredocs, or eval-wrapped redirects. This is not a sandbox — it's a soft guardrail. Calling it "sandboxed execution" in the README will get challenged immediately.

---

## 2. Architectural Weaknesses

**Dual persistence without a source of truth.**
Sessions are written to both SQLite and JSONL. There's no defined primary store. The fallback logic in `session.py:788-792` tries SQLite first, then JSONL. When they diverge (they will, under failure), you don't know which is correct. Pick one. SQLite is the right answer — JSONL is a crutch from an earlier iteration and should be removed.

**AI router embedded in a Kubernetes ConfigMap.**
`k8s/05-ai-router-main-py.yaml` embeds the entire router Python source as a ConfigMap value. You lose git diff readability on the logic, can't unit test it in isolation, and changes don't go through normal code review. The router should be a proper module with its own image or at minimum a tested module mounted into the pod.

**No message durability before processing.**
Messages arrive from platform adapters, are queued in memory, and then processed. If the gateway crashes after acknowledging receipt from Telegram but before the agent finishes, the message is gone. The platform user sees no error. The correct pattern is: persist the message as `pending` to the DB first, process it, mark it `done`. Without this, any crash during processing silently drops work.

**Cron delivery is not atomic.**
`cron/scheduler.py:380-392` — output is saved, then delivery is attempted. If delivery fails, the job is marked complete in the DB with no retry. The user never receives the output. A job that "ran" but whose output was never delivered is indistinguishable from a job that ran successfully.

**Signal uses unauthenticated HTTP to a local daemon.**
`gateway/platforms/signal.py:27-28` — the gateway talks to `signal-cli` over `127.0.0.1:8080` with no token, no mTLS. Any process on the host can read or inject Signal messages by hitting the same endpoint. The threat model assumes a trusted host, but that assumption is never documented and the channel is not encrypted.

**No circuit breaker on model backends.**
The AI router routes based on availability checks, but there's no circuit breaker. If a backend starts returning 500s slowly (not timing out), the router keeps sending traffic to it. Every request fails with a slow failure before the next health check. Retry amplification is a real risk here.

---

## 3. Overengineering / Unnecessary Complexity

**JSONL + SQLite dual persistence.**
This is complexity without benefit. JSONL gives you nothing that SQLite doesn't already provide with WAL mode. Remove JSONL. It is a liability, not a fallback.

**Mixture-of-Agents defaults are not overridable via `hermes model`.**
`tools/mixture_of_agents_tool.py:62-71` — four default reference models and an aggregator are defined as module constants. The tool function (line 219) does accept optional `reference_models` and `aggregator_model` overrides, and the docstring notes they can be changed at the top of the file. However, these defaults do not respect the user's active `hermes model` selection — MoA always routes to its own constants via OpenRouter regardless of what the primary agent is configured to use. This is an inconsistency worth documenting. Separately, the model name strings (`gpt-5.4-pro`, `gemini-3-pro-preview`) should be verified as valid current OpenRouter identifiers before going public.

**Eval suite as code rather than data.**
`evals/suites/` has three hardcoded Python eval cases. A real eval framework should be data-driven. Operators adding new policy rules shouldn't need to write Python to test them. This is early scaffolding dressed up as a framework.

---

## 4. Missing Pieces

| Gap | Why It Matters |
|---|---|
| `/healthz` endpoint | K8s probes, load balancer health, on-call runbooks all require this |
| Structured logging with trace IDs | Cross-platform debugging is currently grep-based |
| Message durability (outbox pattern) | Any crash during processing silently drops messages |
| Rate limiting on platform adapters | No protection against runaway agent sending spam |
| SQLite connection pooling | Current single-connection model breaks under concurrent writes |
| Circuit breaker on model backends | Slow failures amplify without one |
| Encrypted credential storage | Signal phone, WhatsApp session tokens are plaintext in env |
| Explicit retries on cron delivery | Failed delivery is silently dropped |
| Deduplication on incoming messages | Telegram retries on timeout; gateway has no idempotency key |

---

## 5. Failure Mode Analysis

**Network partition between gateway and model backend.**
The router health-checks backends, but if the check passes and then the backend becomes unavailable mid-request, the agent call hangs until the HTTP client timeout fires. There is no per-request timeout on the model call at the agent loop level. The gateway thread stalls for the full client timeout duration before failing. During that window, the platform adapter is frozen.

**Node failure mid-tool-execution.**
Tool ran. Side effect happened (file written, command executed, API called). Process dies before `runs.py:80-94` writes the tool record. On restart, the run is in `status='running'`, eventually surfaced as stuck. There is no idempotency key on tool calls, no compensation logic, no way to know whether to retry. The operator has to manually inspect what ran.

**Model unavailability (all backends down).**
The router returns an error. The gateway catches it and sends an error message to the user — this path is handled. The gap is: cron jobs that fire during a full model outage fail silently (output saved, delivery fails, marked complete). No alert for "N cron jobs failed delivery in the last hour."

**Long-running tool call (>30 seconds).**
Slow-tool warning is logged. The agent loop continues waiting with no upper bound. Telegram's webhook will retry after its own timeout — if the gateway hasn't acknowledged, it may deliver the message twice. The gateway has no deduplication on incoming messages.

**SQLite WAL growth under sustained load.**
If the WAL checkpoint never fires (process dies before checkpointing), the WAL file grows unboundedly. On restart, SQLite replays the full WAL on first connection — this can take seconds for a large file. No WAL size limit or periodic checkpoint is configured.

---

## 6. Security Risks

**Python-level workspace isolation is bypassable.**
An agent that can execute shell commands can escape via subshells, eval, heredocs, or `/proc` traversal. `tools/workspace.py` admits this in its own comments. If running untrusted agent configurations, this is not a sandbox. It requires a real container boundary (Docker, gVisor, or Firecracker) per run.

**No authentication between platform adapters and the agent gateway.**
Any process on the host can POST to the gateway HTTP endpoint and inject messages. No token, no mutual TLS, no peer validation. Acceptable on a fully trusted homelab network; not acceptable in any internet-exposed or multi-tenant deployment.

**Signal/WhatsApp credentials in plaintext environment.**
Signal account number and WhatsApp session tokens are stored as environment variables or in `~/.hermes/`. No encryption at rest. Host compromise = full access to those messaging identities.

**SOUL.md as an injection surface.**
SOUL.md is injected directly into the LLM system prompt (`agent/prompt_builder.py:55`). A compromised or maliciously crafted SOUL.md can direct the agent to exfiltrate data, approve dangerous commands, or bypass policy — bounded only by the active toolset. The injection detection in `prompt_builder.py:20-55` has known gaps: unicode homoglyphs, base64-encoded instructions, YAML anchors, and subshell-wrapped secrets are not caught.

**Approval system pattern bypass.**
`tools/approval.py:64-68` — dangerous command detection is regex substring matching with no shell parsing. Patterns can be evaded via metacharacters, encoding, or command composition. The permanent allowlist has no validation; an agent could add a permissive pattern that effectively whitelists broad command classes.

**No rate limiting.**
A runaway or compromised agent can send unlimited messages to all connected platforms. This risks account bans, API key revocation, and real-money costs on paid API tiers.

---

## 7. Observability Gaps

**No trace IDs across the request path.**
A message enters via Telegram → gateway → agent loop → tool calls → response. None of these steps share a correlation ID. Debugging a user-reported issue means time-based grepping across mixed logs.

**No per-platform error rates.**
`metrics.py` aggregates across all platforms. You cannot see "Telegram is failing at 40%, Discord is healthy" without reading raw logs.

**Cron delivery failures not surfaced.**
A cron job that ran but whose output was never delivered is counted as successful in the metrics DB.

**No SQLite health metrics.**
WAL file size, query latency, and lock contention are not measured or exported. You will not know the DB is the bottleneck until requests start timing out.

**No active alerting on stuck runs.**
Stuck runs (>1 hour) appear in `hermes metrics` but there is no push alert. An on-call engineer has to poll, not get paged.

**Interrupted runs are invisible.**
`metrics.py:120` — metrics only track completed/failed runs. Runs killed mid-execution (SIGTERM, OOM, crash) leave silent gaps in run history.

---

## 8. Suggested Improvements (Highest Impact)

| Change | Impact | Effort |
|---|---|---|
| Add `/healthz` endpoint | Enables K8s probes and on-call runbooks | Low |
| Wrap `_run_agent()` in a hard timeout | Prevents hung threads freezing adapters | Low |
| Atomic cron: deliver first, mark done after | Eliminates silent delivery failures | Low |
| Add per-platform error rate metrics | Enables platform-specific alerting | Low |
| Add trace ID to all log events | Makes debugging tractable | Medium |
| Per-request SQLite connection or pool | Eliminates write contention correctness risk | Medium |
| Outbox pattern for incoming messages | Prevents silent message loss on crash | Medium |
| Remove JSONL, use SQLite as sole session store | Eliminates dual-persist inconsistency | Medium |
| Encrypt credentials at rest | Removes trivial credential theft on host compromise | Medium |
| Container boundary per run (Docker/gVisor) | Makes workspace isolation real, not aspirational | High |

---

## 9. Interview Questions

**Architecture**

1. You have a single SQLite database shared across platform adapters. Walk me through what happens when five users send messages simultaneously across Telegram, Discord, and Slack. Where does it break?
   - *Follow-up:* WAL mode helps readers, but you have concurrent writers. What's the actual serialisation point? Have you measured P95 write latency under load?
   - *Follow-up:* At what point would you migrate off SQLite? What's the trigger and what would you migrate to?

2. Your AI router routes based on model class and availability. What happens if a backend starts returning 500s slowly — not timing out, just reliably failing? How long before the router stops sending it traffic?
   - *Follow-up:* What's the retry policy? Who bears the cost of the retry — the router or the caller?

3. You describe workspace isolation as sandboxed execution. Walk me through exactly what prevents an agent from writing to `/etc/passwd`.
   - *Follow-up:* What about `sh -c 'echo root >> /etc/passwd'`? Or a heredoc? Or a Python subprocess?
   - *Follow-up:* Is this a sandbox or an audit log? What's your actual threat model?

4. Sessions are dual-persisted to SQLite and JSONL. Why both? Which is the source of truth after a partial write failure?

5. Your STAMP model records every run in full. What's your retention policy? How large does the runs table get after six months of daily use? Have you modelled that?

**Reliability**

6. A cron job fires, the agent runs, and then the Hermes process crashes before the response is delivered. What does the user see? What does the metrics DB show? How do you detect and remediate this?
   - *Follow-up:* The job is marked complete in the DB. How do you distinguish "ran and delivered" from "ran and lost"?

7. A tool call hangs indefinitely. Walk me through what the system does over the next 5 minutes.
   - *Follow-up:* The Telegram adapter is frozen waiting for that call. New messages from that user are queued where? What's the queue depth limit?

8. Your gateway process restarts mid-conversation. Which messages are lost? How does the user know?

**Security**

9. A malicious user sends a SOUL.md containing base64-encoded instructions. Walk me through how your injection detection handles it.
   - *Follow-up:* The soul is user-configurable. What prevents a user from configuring a soul that exfiltrates other users' data?

10. Signal messages transit from `signal-cli` to your gateway over `localhost:8080` HTTP with no authentication. What's the threat model? Who is that protecting against?

11. The permanent command allowlist can be modified by the agent itself. Under what conditions could an agent add a pattern that effectively whitelists `rm -rf /`?

**Observability**

12. A user reports their Telegram messages stopped getting responses three hours ago. Walk me through how you diagnose this. What tools do you have? Where does your investigation start?
    - *Follow-up:* How long would that investigation take today? What would make it faster?

13. You export Prometheus metrics. What's your SLO? What alert fires when the system is failing its SLO?

**Design**

14. The Mixture-of-Agents tool hardcodes four specific commercial model names. What happens when OpenAI renames GPT-5.4-pro? What happens when a user runs an entirely local setup with no cloud API keys?

15. You have an eval framework with three test suites. How do you test a new policy rule before deploying it? How do you prevent regressions when you change the approval system?

---

## 12. Socraticode Zombie Process Leak

**Every agent call to socraticode leaks one zombie `[sh]` process.**

`socraticode` spawns shell subprocesses internally (likely for `.gitignore` evaluation via `git ls-files` or similar) using Node's `child_process.exec()` or `spawn()`. It does not attach an `'exit'`/`'close'` event listener to the child handle, so libuv never calls `waitpid()` and the process stays in `Z` state until its parent socraticode instance is killed.

As of 2026-03-21 there are 49 zombie `[sh]` processes on the host, each parented to a distinct `node /usr/local/bin/socraticode` instance — a 1:1 ratio confirming one leak per MCP session. One `[node]` zombie also appears, suggesting the same pattern affects a nested subprocess. Zombies accumulate from Mar 17 onward, consistent with Hermes agent activity.

Zombies consume no CPU or memory, but each holds a PID. The default Linux PID limit is 32768. At current call volume this is not immediately dangerous, but a long-lived homelab deployment will exhaust PIDs over weeks. `docker compose restart socraticode-mcp` clears them.

**Theoretical fix in socraticode (upstream):**

Any `child_process.spawn()` or `exec()` call without an attached `exit` handler leaves a zombie. The fix is to ensure all spawned children are reaped:

```js
// Pattern causing zombies — no exit listener, child handle dropped
const child = spawn('sh', ['-c', cmd]);

// Fix — attach exit listener so libuv calls waitpid()
const child = spawn('sh', ['-c', cmd]);
child.on('exit', () => {});  // minimal; or use child.on('close', cb)

// Or use the promise form which handles this automatically
const { stdout } = await execPromise(cmd);
```

The correct approach is to audit every `spawn`/`exec` call in socraticode's source and ensure either: (a) an `'exit'` or `'close'` handler is attached, or (b) the call is `await`ed via `util.promisify(exec)`. A linter rule (`no-floating-promises`, `node/handle-callback-err`) would catch regressions.

**How to push the fix:**

Socraticode is an npm package. Check its source at `$(npm root -g)/socraticode/` or its GitHub repo. The fix is a small patch — file a GitHub issue with the zombie reproduction steps (spawn socraticode, call any tool that triggers a `sh` subprocess, observe `ps aux | awk '$8=="Z"'`), then submit a PR with the `exit` handler additions.

**Workaround on our side (logos/homelab-infra), no upstream change needed:**

Add `init: true` to `hosts/ai/socraticode/docker-compose.yml`:

```yaml
  socraticode-mcp:
    init: true   # adds tini as PID 1; reaps orphans if socraticode processes die
```

Note: `init: true` (tini) only reaps processes re-parented to PID 1 — it does *not* fix zombies whose parent is still alive. The real fix requires either upstream socraticode patching or periodically restarting the container. A cron `docker compose restart socraticode-mcp` daily is a low-effort mitigation until upstream is fixed.

---

## 11. Additional Issues (Post-Initial Audit)

### UI / Dashboard

**Chat scroll-to-bottom fires unconditionally on page load.**
The dashboard scrolls the chat pane to the bottom on every render/refresh, regardless of whether the content is taller than the viewport. If the conversation is short enough to fit entirely within the chat container, there is nothing to scroll — but the scroll fires anyway. This causes a jarring jump for short sessions and is perceptible whenever the page is refreshed. The correct behaviour is: only scroll to bottom if `scrollHeight > clientHeight` (i.e. the content actually overflows the container). A one-line guard before the scroll call fixes it.

---

### Privacy

**Honcho is a cloud integration that sends conversation data to a third party.**
`honcho_integration/client.py:77` — disabled by default, activates when `HONCHO_API_KEY` is present. When active it syncs conversation messages, `MEMORY.md`, `USER.md`, and `SOUL.md` to Plastic Labs' managed cloud service. There is no sanitisation path — the service requires actual conversation content to function. This is now documented under "Optional cloud integrations" in the README with an explicit privacy warning. The critique item here is operational: ensure the integration is genuinely inert when the key is absent, and that no code path silently enables it.

---

### Security

**MCP stdio servers run with full user permissions on install-script deployments.**
`scripts/install.sh` is a bare-metal host install — no Docker, no namespacing. Any MCP stdio server configured in `~/.hermes/config.yaml` (e.g. `npx -y @modelcontextprotocol/server-filesystem`) spawns as a subprocess of the Hermes Python process with the installing user's full filesystem and network access. The Docker path (`Dockerfile`) provides a container boundary and a non-root `hermes` user (uid 10001), so MCP subprocesses are scoped to mounted paths. Anyone installing via the script who configures a broad filesystem MCP path exposes their entire home directory to LLM-directed tool calls. This should be documented explicitly, and the install script should default users toward the Docker path for any non-personal deployment.

**`npx -y` MCP server invocations are a supply chain risk.**
Stdio MCP servers configured with `command: npx` and `args: ["-y", "..."]` download and execute npm packages at runtime without version pinning. A typosquatted or compromised package runs immediately with user-level permissions. All npx-based MCP server configs should pin to a specific version (e.g. `@modelcontextprotocol/server-filesystem@1.2.3`) and ideally verify package integrity before execution.

**Prompt injection via MCP tool results can trigger tool calls.**
The agent receives content from MCP tool results and external channels (web pages, files, messages) and acts on it. A malicious MCP server response or injected file content can direct the agent to call other tools — including destructive ones — without the user initiating it. MCP tool filtering (`tools.include` whitelists) reduces the callable surface but does not prevent injection; it only limits what an injected instruction can reach. The gateway auth boundary (who can message the agent) is the most important control here.

**MCP config (`~/.hermes/config.yaml`) stores API credentials in plaintext adjacent to a broad filesystem MCP path.**
If the filesystem MCP server is configured with a path that includes `~/.hermes/`, the agent can read its own config file and exfiltrate API keys to any connected channel or tool. The config path should be explicitly excluded from any filesystem MCP scope.

**Handoff tool `policy_scope` is enforced by prompt, not by toolset filtering.**
`tools/handoff_tool.py:112-120` — when `policy_scope` is `"restricted"` or `"read_only"`, the enforcement is a system prompt instruction: *"You are operating in RESTRICTED mode. Read-only operations only."* The child agent's actual toolset is not modified. A child with file write tools available can still use them — the restriction only works if the model follows instructions. This is not policy enforcement; it is a polite request. Real enforcement requires filtering the allowed toolsets at dispatch time based on the declared scope.

**K8s secret manifest uses `stringData`.**
`k8s/02-secret.yaml:7` — the Secret uses `stringData`, meaning values are committed as plaintext YAML. The current file contains placeholder values (`REPLACE_WITH_*`), so the template itself is safe. The risk is operational: when a user fills in real credentials, the file with plaintext secrets in git becomes the failure mode. The comment on line 11 correctly says to generate with `openssl rand -hex 32`, but there is nothing preventing someone from committing the filled-in file. Consider Sealed Secrets, SOPS, or at minimum a `.gitignore` entry and a warning in the file.

**No Kubernetes NetworkPolicy.**
There is no `NetworkPolicy` manifest in `k8s/`. All Hermes pods can reach all other pods in the cluster and make arbitrary outbound connections. A compromised Hermes pod has unrestricted lateral movement. A NetworkPolicy restricting egress to known endpoints (inference backends, platform APIs) and ingress to the gateway port would significantly reduce blast radius.

---

### Reliability

**Telegram silently drops queued messages on restart.**
`gateway/platforms/telegram.py:155` — `drop_pending_updates=True` is passed to `start_polling()`. Any messages that arrived while the gateway was down are discarded without notification to the user. This is a deliberate choice that trades correctness for avoiding a backlog storm on restart, but it should be a known, documented trade-off, not a silent behaviour.

**K8s PVC uses node-local storage.**
`k8s/05-pvc.yaml` uses `local-path` as the storage class, which binds to a single node's local disk. If that node is replaced, drained, or fails, the PVC is lost — taking with it skills, memory, session history, and the entire Hermes home directory. For a homelab this may be acceptable, but it needs to be explicit. There is no snapshot schedule, no backup job, and no documentation of the data loss risk. Anyone running this on a managed K8s cluster expecting persistent storage will be surprised.

**Skill creation proceeds if the security scan raises an exception.**
`tools/skill_manager_tool.py:56-67` — the security scan is called but if it raises, the exception is caught and logged, and skill creation continues. The intended behaviour when the scanner is unavailable should be to block creation, not to silently skip the check. Fail-closed is the right default for a security gate.

---

### Design

**Handoff output schema validation is shallow.**
`tools/handoff_tool.py:69-98` — `_validate_output_against_schema()` checks top-level required fields and basic type matching, but does not recurse into nested objects or arrays, and does not validate enum values or string patterns. A schema requiring `{"severity": "enum of [low, medium, high]"}` will accept `{"severity": "banana"}`. The docstring calls this "structured I/O validation" — the implementation does not deliver that.

---

## 13. Future Ideas / Deferred Work

**Weekly self-improvement cron skill.**
The agent should run a weekly cron job that reviews its own recent run history, identifies one high-priority error or improvement opportunity, performs a dry-run of the proposed fix using the existing `dry_run` infrastructure, and — if the dry-run passes — surfaces the fix to the operator for approval before applying it. This closes the loop between observability (runs are already recorded) and self-correction. Implementation path: a skill that queries `hermes runs list`, picks the highest-signal failure, drafts a patch, runs it through `dry_run_simulate`, then sends a formatted proposal via the configured delivery channel.

**VS Code (and editor) agent integration.**
The ACP adapter (`acp_adapter/`) already implements the protocol needed for VS Code, Zed, and JetBrains to connect Logos as an in-editor assistant. What's missing is a polished setup path: a one-command install that configures the ACP server URL in the user's editor settings, documents the token/auth flow, and verifies the connection. The goal is that a developer can `hermes setup --vscode` and have Logos appear as an assistant in their editor within 60 seconds. The underlying protocol work is done; this is packaging and documentation.

**Full sandbox security validation skill.**
A skill that runs a structured security audit of the agent's workspace isolation on demand — attempting known escape vectors (subshells, heredocs, `/proc` traversal, symlink tricks) in a controlled environment and reporting which ones succeed. The output should be a clear pass/fail per vector with remediation notes. Currently `tools/workspace.py` admits its own limitations in comments; this skill would make those limitations measurable and trackable over time rather than static documentation. Pairs with the container boundary upgrade path (Docker/gVisor per run) described in §8.

**Network-accessible `logos` CLI.**
Currently the `hermes` CLI only works locally on the machine where Logos is installed. A `logos` CLI that can be installed anywhere on your network (via `pip install logos-cli` or a curl install script) and connect to a remote Logos gateway over HTTP/WebSocket would be a strong UX improvement. The gateway already exposes a streaming `/chat` SSE endpoint and a `/status` endpoint — the CLI would authenticate with a token, stream responses to the terminal, and support all `hermes config` / `hermes model` admin operations via the REST API. This turns any machine on the network into a first-class Logos terminal without needing SSH access.

**Container user path still named `hermes` in K8s manifests.**
`k8s/06-deployment.yaml` and `k8s/10-hermes-canary-deployment.yaml` reference filesystem paths like `/home/hermes`, `/home/hermes/.hermes`, and mount points like `/hermes-shared/memories`. These paths are tied to the container user (`runAsUser: 10001`) whose home directory is set to `/home/hermes` (via `HOME: /home/hermes` env var) inside the pod. Renaming these to `/home/logos` requires coordinating three things: (1) updating the `HOME` env var in the deployment, (2) updating the `Dockerfile` to create a `logos` user at uid 10001 instead of `hermes`, and (3) updating the volume mount paths. Until this is done the k8s manifests are partially rebranded — resource names say `logos` but the in-pod filesystem still says `hermes`. **Deferred until the Dockerfile is updated.**

---

## 10. Defensible System Narrative

> Clean, sharp version for explaining this project to a senior engineer.

Logos is a self-hosted agent control plane built for operators who need to run AI agents on their own infrastructure with verifiable behaviour, not just configurable prompts.

The core design decision is that every agent execution is defined by five explicit, independently-variable dimensions: the persona (soul), the capability surface (tools), the runtime adapter (agent), the model, and the enforcement policy. These are recorded as a single STAMP record per run in SQLite, alongside the full tool call sequence and approval events. This makes every run auditable, comparable across configurations, and replayable — the property that differentiates this from wrapping an API call in a prompt.

The gateway is a multi-platform message broker — Telegram, Discord, Slack, WhatsApp, Signal, email, Home Assistant — running as a single long-lived async process. Each platform adapter is independently managed; the agent loop is shared. Incoming messages create or resume sessions; sessions carry conversation history and tool context. The agent runtime is pluggable via a typed adapter interface; Hermes is the current implementation.

Policy enforcement is layered: filesystem access is scoped to per-run workspaces with path boundary enforcement, dangerous commands go through an approval callback before execution, and policy behaviour is validated via a purpose-built eval suite. The current workspace enforcement is Python-level — it is a guardrail, not a kernel sandbox. Containerised execution is the intended upgrade path for untrusted or multi-tenant workloads.

The AI routing layer is a separate service running in Kubernetes that distributes model requests across multiple inference backends based on model class, machine availability, and per-user priority. It handles load distribution and preferential routing; it does not yet implement circuit breaking, which is a known gap.

Known production gaps stated directly: SQLite requires connection pooling before it scales to concurrent multi-user load. Cron delivery failure does not currently trigger a retry. There is no health check endpoint. Incoming messages have no durability guarantee before processing begins. These are the next four things to fix — they are operational hardening issues, not design flaws.

The architecture is sound. The feature surface is real and code-backed. The gaps are known, specific, and fixable.
