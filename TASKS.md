# Logos open work

A snapshot of pending work captured during the long debug session of
2026-04-09 / 04-10. Written so we don't lose context if a session
crashes again.

## Pending — feature work

### #13 Side-by-side AB chat panel with drag-drop pills
Drag agent pills from the agent column into the side-by-side panel.
Two pills → two chat boxes side-by-side, one per pane. Each pane is
its own independent chat session, but the user can send the same
prompt to both for AB-style comparison (e.g. how does qwen3.5
answer vs gpt-oss-20b on the same question).

Future scope (don't build now, but design for it): multi-agent
shared workspace where the dropped agents can see each other's
messages and work together. Probably needs a shared "scratchpad"
message stream both panes subscribe to.

### Day/night cycle with real local time + location dropdown in /setup
- `/setup` step: dropdown of common IANA timezones
  (`Europe/London`, `America/New_York`, etc.) with a "detect from
  browser" default button using
  `Intl.DateTimeFormat().resolvedOptions().timeZone`. Stored in
  user settings or platform_settings.
- World view: replace the 4-minute artificial sine cycle with a
  real 24-hour cycle keyed to the user's local hour.
  Dark 22:00–05:00, dawn 05:00–08:00, day 08:00–18:00,
  dusk 18:00–22:00.
- Inject `Current local time: 2026-04-10 09:32 (Europe/London)`
  into `build_session_context_prompt` so agents know the time
  on every dispatch. Cheap, single line, no extra code path.

### `get_current_time` MCP tool (later)
For agents that need to actively query time (scheduling, "in 3
hours", relative dates). The prompt-injection above covers the
"what time is it?" case. The MCP tool is for explicit lookups.

## Pending — infra / cleanup

### #16 Scan for orphan openshell ssh-proxies on gateway startup
Task #10's reaper only kills subprocess children of the CURRENT
gateway. Zombies from a previously-crashed gateway (or one killed
via SIGKILL) survive every restart and re-register as workers with
stale identities — that was the day-long "Hermes thinks it's Ani"
investigation. On gateway startup, scan for any
`openshell sandbox`, `openshell ssh-proxy`, or
`ssh ... openshell` processes that aren't owned by the current
gateway and SIGTERM them before the worker registry comes up.

### #17 Cache sandbox details to prevent blank flash on tab click
When the user clicks on a sandbox in `/admin/sandboxes` the detail
panel sometimes shows empty momentarily before the next poll fills
it in. Cache the last-known values per-sandbox in Alpine state so
the panel never goes blank — refresh in place when fresh data
arrives instead of clearing first then re-populating.

### #18 Standardize openshell gateway naming to model-only
Currently the first gateway is `logos-openshell` (the original
primordial that Logos adopts on first /setup) and subsequent
gateways are `logos-os-<sanitized-model>`. The user wants all
gateways named consistently after the model they serve. Options:
(a) destroy + re-provision the primordial under a new name
(destructive but clean), (b) leave the primordial alone and
rename the sub-gateway prefix (compromise), or (c) drop the
"primordial" concept and always provision named gateways from
/setup. Needs a migration story for existing installs.

## Documentation / known limitations

### Reasoning toggle on LM Studio is detection-only
We can detect which models support reasoning toggle by reading
`capabilities.reasoning.allowed_options` from
`/api/v1/models`. **But empirically tested** (2026-04-10), none of
the candidate parameter names work for actually disabling reasoning
on qwen3.5-9b through LM Studio's OpenAI-compat endpoint:

| Param | Result |
|---|---|
| `reasoning: "off"` | no change (290 reasoning tokens) |
| `reasoning_effort: "low"` | hit max_tokens during reasoning |
| `enable_thinking: false` | minor decrease (213 tokens), not zero |
| `thinking: false` | no change |
| `chat_template_kwargs: {enable_thinking: false}` | hit max_tokens |
| `/no_think` suffix in user msg | hit max_tokens |

Workarounds for users who want a snappier qwen3.5 chat experience:
modify the chat template in LM Studio's UI manually, use a model
that doesn't have built-in reasoning, or wait for LM Studio to
expose a reasoning param in their compat endpoint.

The /setup benchmark could add a "trivial answer TTFT" metric to
surface this kind of model-specific UX gap upfront.

### Worker WebSocket frame parser blocks during inference
The sandbox worker uses a custom `TunnelWebSocket` whose frame
parser only runs while the main loop is in `receive_json()`. While
`_handle_task` is awaiting an inference call to LM Studio,
incoming WS pings can't be answered. With `heartbeat=30` (the
default), connections dropped after ~30s of inference. We bumped
to `heartbeat=600` as a safety net but the proper fix is to
process WebSocket frames in a separate task from the message
handler. That's a bigger refactor — left for later.

## Recent fixes (just in case we crash and need context)

| Commit | Fix |
|---|---|
| `4e6b079` | LM Studio `/api/v1/models` field names corrected (`models[*].key`, `loaded_instances`) — was the cause of "every chat reloads the model" |
| `7732a8e` | WS heartbeat 30 → 600 |
| `68a4988` | Auto-select first agent on /chats land |
| `11f2ae9` | Local DM session_key includes chat_id (was `agent:main:local:dm` for everyone, causing cross-agent transcript bleed) |
| `5eaac73` | On-demand LM Studio `ensure_loaded` from `_handle_chat` |
| `4c4a09f` | Use `lm-studio` placeholder token instead of `unused` (initial fix, since superseded by reading user's machine.api_key) |

## What's currently a "dead end" we're aware of

- Reasoning toggle on LM Studio (see above)
- The openshell `sandbox logs` subcommand (doesn't exist; we now
  read `/tmp/worker.log` via `sandbox exec` instead — fixed)
- Single-LM-Studio-instance VRAM ceiling (user can load 2 qwen + 2
  gpt-oss-20b max — that's an LM Studio config the user controls)
