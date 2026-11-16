# Hermes API client (`gateway/hermes_client.py`)

Logos talks to a v2 agent (upstream hermes v0.23.1 in an OpenShell sandbox) through
hermes' own HTTP API, using one small async client. The client is **not wired into
`gateway/http_api.py` yet**; a later story does that. It takes a `base_url` and an API
key and knows nothing about how that URL reaches the sandbox, which is the open
question at the bottom of this note.

**Why the runs API and not `/v1/chat/completions`:** `/v1/runs` gives a run id up front,
a stop endpoint, a resumable event stream with tool and reasoning events, and a
pollable status. Chat completions gives text deltas only.

## Upstream endpoints used

References are to the source in the `logos-agent:0.23.1` image under
`/opt/hermes/gateway/platforms/`. Every path, method, payload and event name below was
read from that source, not inferred.

| Client method | Upstream endpoint | Route | Handler |
|---|---|---|---|
| `health()` | `GET /health` (no auth) | `api_server.py:1555` | `api_server.py:2266-2268` |
| `start_run()` | `POST /v1/runs` → 202 `{"run_id","status":"started","replayed"}` | `api_server_runs.py:286` | `api_server_runs.py:768`, reply `:549-555` |
| `get_run()` | `GET /v1/runs/{run_id}` | `api_server_runs.py:286` | `api_server_runs.py:1249-1253` |
| `stream_events()` | `GET /v1/runs/{run_id}/events` (SSE) | `api_server_runs.py:287` | `api_server_runs.py:1256-1346` |
| `stop_run()` | `POST /v1/runs/{run_id}/stop` | `api_server_runs.py:292` | `api_server_runs.py:1497-1528` |
| `list_sessions()` | `GET /api/sessions` | `api_server.py:1563` | `api_server.py:2520-2571` |
| `get_session()` | `GET /api/sessions/{session_id}` | `api_server.py:1565` | `api_server.py:2645-2650` |
| `get_session_messages()` | `GET /api/sessions/{session_id}/messages` | `api_server.py:1568` | `api_server.py:2730-2763` |

`chat()` is `start_run()` followed by `stream_events()`.

Cross-cutting:

- **Auth**: `Authorization: Bearer <API_SERVER_KEY>`; failure is 401 with
  `error.code = "gateway_auth_failed"` (`api_server.py:1407-1411`, `:1413-1435`). The
  server refuses to start without a key of at least 16 characters
  (`api_server.py:3906-3935`). `scripts/logos-agent` generates the key per agent
  (`scripts/logos-agent:137`).
- **Errors**: OpenAI-style envelope `{"error": {"message","type","param","code"}}`
  (`api_server.py:740-743`). Too many concurrent runs is 429 (`api_server.py:3565-3567`).
- **Listen address**: port 8642 by default (`api_server.py:155`).

### `POST /v1/runs` body

`input` is required: a string, or a list of `{"role","content"}` messages whose last
item is the user turn (`api_server_runs.py:793`, `:718-728`). There is no `messages`
field on this route; the client's `messages=` argument is sent as `input`. Optional:
`session_id` (`:806`), `instructions` (`:498`), `conversation_history` (`:501-511`),
`previous_response_id` (`:499`), `model` (`:807`), and the `Idempotency-Key` header
(`:786-790`).

Two behaviours worth knowing before wiring this in:

- Without `session_id`, the run id becomes the session id (`api_server_runs.py:841`).
  The 202 does not return the session id; read it from `GET /v1/runs/{run_id}`.
- A multi-message `input` is treated as history-plus-last-turn
  (`api_server_runs.py:522-529`) and then takes precedence over the stored history of
  `session_id` (`:851-852`). To continue a session, send only the new user text.

### Event stream

Frames are `id: <seq>\ndata: <json>\n\n` with no `event:` line; the name is the
`"event"` key of the JSON, next to `run_id`, `timestamp` and `seq`
(`api_server.py:195-201`, `api_server_runs.py:224-226`, `:1294-1297`). The stream
opens with a `: open` comment (`:1307`), sends `: keepalive` every 10 s while idle
(`:1330`, `api_server.py:173`), and `: stream closed` before a normal end (`:1319`,
`:1323`, `:1336`). `Last-Event-ID` resumes from a 1000-event backlog (`:1277-1281`,
`:167`).

| Upstream event | Normalised type | Source |
|---|---|---|
| `message.delta` `{delta}` | `token` | `api_server_runs.py:1073-1077` |
| `message.interim` `{text, already_streamed}` | `message` | `api_server_runs.py:1079-1087` |
| `reasoning.available` `{text}` | `reasoning` | `api_server_runs.py:145` |
| `tool.started` `{tool, preview, tool_call_id?}` | `tool_started` | `api_server_runs.py:141` |
| `tool.completed` `{tool, duration, error, preview, tool_call_id?}` | `tool_completed`, or `tool_failed` when `error` is true | `api_server_runs.py:142-144`, `:387-388` |
| `subagent.start` / `subagent.complete` | `subagent_started` / `subagent_completed` | `api_server_runs.py:390-400` |
| `approval.request` | `approval_request` | `api_server.py:90-102` |
| `run.queued` | `run_queued` (not terminal) | `api_server_runs.py:1177-1182` |
| `run.completed` `{output, usage, runtime, ...}` | `run_completed` | `api_server_runs.py:1089-1100` |
| `run.failed` `{error, ...}` | `run_failed` | same |
| `run.cancelled` | `run_cancelled` | same |
| `run.interrupted` (gateway shutdown) | `run_interrupted` | same; terminal set `api_server_run_idempotency.py:21` |
| anything else (`replay.truncated`, control prompts, future names, undecodable frames) | `other` | - |

Every normalised event keeps the upstream payload in `raw`.

Differences from what one might assume:

- **There is no `run.started` event** in v0.23.1; the 202 is the start signal.
  `chat()` synthesises a `run_started` event (marked `"synthetic": true` in `raw`) so a
  renderer gets the run id before the first token.
- **There is no streaming reasoning delta.** Reasoning arrives as whole
  `reasoning.available` blocks; hermes deliberately drops `_thinking` from this stream
  (`api_server_runs.py:381-382` comment).
- **Tool failure is a flag, not an event name**: `tool.completed` with `error: true`.
- **No tool arguments or full results** are on the stream, only a short preview
  (500 characters, secret-redacted, `api_server_runs.py:146-158`).

### Errors raised

All derive from `HermesError`: `HermesConnectionError` (refused, reset),
`HermesTimeoutError` (request timeout, or a stream silent past the idle timeout),
`HermesHTTPError` (any non-2xx; subclasses `HermesAuthError` for 401/403 and
`HermesNotFoundError` for 404), `HermesStreamError` (stream ended with neither a
terminal event nor `: stream closed`; carries `last_seq` for resuming) and
`HermesProtocolError` (2xx with an unexpected body).

## Not covered yet

- Approvals: `POST /v1/runs/{run_id}/approval` and `/clarify`
  (`api_server_runs.py:288-289`). `approval_request` events are surfaced, but the client
  cannot answer them. A run that asks for approval will wait until it is stopped.
- Steering (`/steer`), session create/patch/delete/fork, jobs, `/v1/responses`.
- Automatic reconnect. `stream_events(run_id, last_seq=...)` supports resuming; the
  retry policy belongs to the caller.

## Open question: how Logos reaches port 8642

The API listens on `127.0.0.1:8642` **inside** the sandbox. The Logos gateway runs on
the host. Something has to bridge the two. This note does not choose.

**Confidence:** the hermes side above is cited from source. The OpenShell side below
is from `scripts/logos-agent` (which already uses `openshell sandbox exec`,
`scripts/logos-agent:149`, `:156`) and from the earlier prototype note
(`hermes-as-server-prototype.md`). The exact OpenShell 0.1.8 forwarding flags and
their behaviour were **not verified** for this story (agents were told not to run
`openshell`); whoever picks this up should check them first.

### Option A: OpenShell port forward / service expose

Forward a host port to the sandbox's 8642 (`--forward` at sandbox creation, or the
service-expose mechanism), and give `HermesClient` `http://127.0.0.1:<host port>`.

Pros
- The client works as written: real HTTP, real SSE, streaming with no extra framing.
- One long-lived connection per stream; stop and status calls are cheap.
- Timeouts, cut-stream detection and `Last-Event-ID` resume behave as tested.

Cons
- Needs a host port per agent, allocated and recorded somewhere (`agents/<name>.yaml`
  or derived), and it must not collide with 8091 or anything else on the host.
- A host listener is new attack surface. It should bind to loopback only; the bearer
  key is then the only thing between any local process and a terminal-capable agent.
- The forward's lifetime is tied to OpenShell: whether it survives sandbox restart,
  gateway restart or host reboot needs checking, and Logos would need to detect a dead
  forward (it shows up as `HermesConnectionError`).
- Unverified whether forwarding is configurable after creation or only at create time,
  which decides whether existing sandboxes must be recreated.

### Option B: `openshell sandbox exec ... curl`

Run `curl` inside the sandbox for each call, as `scripts/logos-agent` already does for
its health check, and read the response from the subprocess's stdout.

Pros
- No host port, no new listener; reachability is exactly what exists today.
- Already proven for one-shot calls on this host.
- Nothing to keep alive between calls.

Cons
- `HermesClient` cannot be used as is: it needs an aiohttp-compatible transport. Either
  a second code path that shells out and feeds stdout to the same SSE parser and
  `normalise_event`, or a small local proxy that does so.
- One subprocess per request, and one held open for the length of each stream. Slower
  per call and more moving parts under load, though load here is two users.
- Streaming depends on `exec` passing stdout through unbuffered (`curl -N`); whether
  OpenShell 0.1.8 does so without a TTY is unverified.
- Error mapping gets worse: HTTP status, connection refused and a cut stream all have
  to be reconstructed from curl exit codes and `-w` output.
- The API key appears on a command line inside the sandbox unless passed by stdin or a
  file; minor, since the key already lives in the sandbox's environment.

### What would decide it

- Does OpenShell 0.1.8 forwarding bind loopback-only and survive restarts?
- Does `openshell sandbox exec` stream stdout promptly without a TTY?
- Does the later wiring story want one transport for both chat and the short calls
  (sessions, stop), or is a split acceptable?

A unix socket was also listed in the prototype note; hermes v0.23.1's API server binds
TCP only (`api_server.py:165-168`), so it would need a forwarder inside the sandbox and
is not considered further here.
