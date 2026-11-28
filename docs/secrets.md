# Secrets (story E7)

What Logos keeps secret, where each secret lives, who can read it, and how to rotate it. Most
per-agent tokens now reach an agent only as an **OpenShell credential placeholder**: the agent
never holds the real value, so it cannot read it out or pass it on.

- Code: `gateway/agent_secrets.py` (placeholders, the host store), `gateway/secret_rotation.py` and
  `scripts/logos-rotate` (rotation), `gateway/agent_runtime.py` (`split_secrets`, `sync_providers`).
- Status of every agent, with no values shown: `scripts/logos-rotate status`.

## How placeholders work (OpenShell 0.1.8)

For every secret switched to a placeholder, `scripts/logos-agent up <agent>` does four things:

1. It keeps the real value on the host, in `~/.config/logos/agent-secrets/<agent>.env`. The folder
   is 0700 and the file 0600. The Logos services read the value from there to check the agent's
   requests (`AgentRuntime.secret`).
2. It creates a provider profile `logos-<agent>-<secret>` (rendered under `.rendered/<agent>.providers/`,
   which holds no secrets). The profile has one credential, `auth_style: bearer`,
   `header_name: Authorization`, `stable_placeholder: true`. Its endpoints are copied from the
   agent's own sandbox rules for that service, and its binary is the hermes interpreter.
3. It creates or updates the provider `logos-<agent>-<secret>` from that profile. The value is
   passed through the `openshell` process's environment (`--credential KEY`), never on the command
   line, and a failure message has the value masked.
4. It creates the sandbox with `--provider logos-<agent>-<secret>`.

Inside the sandbox the variable holds `openshell:resolve:env:s<hash>_<NAME>`. The first step of
the sandbox start (`START_HOOK`) writes each placeholder into `/opt/data/.env`. hermes loads that
file over its environment, and the job scripts read it directly, so both see the placeholder too.
`config.yaml` says `Authorization: Bearer ${LOGOS_<SERVICE>_TOKEN}` and
`stt.openai.api_key: ${LOGOS_VOICE_TOKEN}`; hermes expands these from its environment. The model
key is already named by `key_env`.

The sandbox proxy puts in the real value only when all three of these hold:

- the request goes to one of the endpoints in that secret's profile,
- the request comes from the hermes interpreter,
- the placeholder is in the `Authorization` header.

This was checked on this machine with OpenShell 0.1.8, using the `openshell` CLI, `profile lint`
and probe sandboxes against an echo server:

| What was sent | What OpenShell did |
| --- | --- |
| `Authorization: Bearer <placeholder>` to the bound endpoint, plain HTTP, `protocol: rest` or `mcp` | The real value reached the server |
| The same placeholder to another allowed endpoint | `403 credential_endpoint_mismatch`; nothing was sent |
| Placeholder of credential A to the endpoint of credential B | `403 credential_endpoint_mismatch` |
| Placeholder as `?access_token=` (query) with a bearer-style credential | `403 credential_placement_mismatch` |
| Placeholder in a request body | It is never resolved. A body sent to an unbound host goes out with the placeholder text as it is |
| A placeholder that is not registered for this sandbox (wrong form) | `500 credential_unavailable`; nothing was sent |
| `openshell provider update <p> --credential K --wait` | The running sandbox used the new value on its next request, with the same (stable) placeholder and no restart. Without `stable_placeholder` the placeholder changes with each provider revision (`v<rev>_NAME`), which would break a running agent |
| Profile endpoint rules and sandbox policy rules for the same host:port | Combined (a union). The profile's endpoints are a copy of the agent's own rules, so they add no access |

`auth_style` can be `header`, `bearer`, `query`, `path` or `basic`. `path` matches one exact
`path_template` per credential, which is why the Telegram Bot API token never fitted (the token is
in the path of every method).

## Per-secret switch

```yaml
# agents/<name>.yaml, agents/<name>.local.yaml or agents/all.local.yaml (all agents)
credential_placeholders: false                      # all off: everything in .env as before E7
credential_placeholders: {house: true, mail: true}  # change single secrets over the defaults
credential_placeholders: {all: false, voice: true}  # only these
```

These are on by default because they were proven end to end on the dev machine: `matrix`,
`model`, `noticeboard`, `voice`, `calendar`, `web`, `alert` (the alert one needs the dashboard to
run E7 code too: an older one reads the token only from the request body).

`changes`, `house`, `mail`, `advisor` and `speaker_say` use exactly the same mechanism but are off
by default. Their services (the daily changes, Home Assistant, a real mailbox, the advisor, the
speaker) do not run on the dev machine. Switch them on per agent after a check on the machine that
runs them (see Rollout).

A secret becomes a placeholder only when its switch is on *and* the agent's rendered policy has
the rule to bind it to. Turning a switch off puts the value back in `.env` (the same value, so the
service does not restart) and deletes the provider.

## Inventory

Who can read: "the agent" means anything running in its sandbox, including a person or a prompt
that has taken over its tools. "Host user" means the account that runs Logos.

### Per-agent secrets

| Secret | Where it lives | Who can read it | How it is used | Rotate | Blast radius if leaked |
| --- | --- | --- | --- | --- | --- |
| `MATRIX_ACCESS_TOKEN` (the agent's chat login) | Host store; placeholder in the sandbox (off: `.env`) | Host user; the Matrix server | `Authorization` header to the household Matrix server | `logos-rotate <agent> matrix` (same device, live) | The agent's chat account: read and send in its rooms, including unencrypted history. Encrypted history also needs the device keys in the home volume |
| `LOGOS_MATRIX_PASSWORD` | Host store only (before E7: the agent's `.env`) | Host user | Logos signs the agent in again (`relogin`) | Matrix admin reset-password, then `logos-rotate <agent> matrix` | New logins as the agent from anywhere the server is reachable; it outlives a token rotation |
| `MATRIX_RECOVERY_KEY` | `.env` and `matrix-recovery-key` in the home volume (0600) | The agent; host user | The agent unlocks its cross-signing keys at each start | Manual (below) | Lets someone verify devices as the agent; together with its login, read its encrypted history |
| `API_SERVER_KEY` | `.env` in the home volume | The agent (it must: it checks requests to it); host user | The dashboard to the agent's API, through `openshell forward` | `logos-rotate <agent> api` (recreates the agent) | Only reachable through the forward on the host: talking to the agent as the dashboard |
| `LOGOS_MODEL_KEY` (model gateway virtual key) | Host store; placeholder (off: `.env`); `logos-models` config (0600) | Host user | hermes `key_env`; vault digest | `logos-rotate <agent> model` (live; restarts the model gateway) | Spending that agent's monthly model budget, from inside the sandbox network only |
| `LOGOS_NOTICEBOARD_TOKEN` | Host store; placeholder; noticeboard config | Host user | MCP header and `/pending`, `/urgent` polls | `logos-rotate <agent> noticeboard` | Reading and sending noticeboard messages as the agent |
| `LOGOS_VOICE_TOKEN` | Host store; placeholder; voice service config | Host user | STT `api_key` for voice notes | `logos-rotate <agent> voice` | Transcription on the shared voice service |
| `LOGOS_CALENDAR_TOKEN` | Host store; placeholder; calendar service config | Host user | MCP header; morning briefing | `logos-rotate <agent> calendar` | The agent's calendars: read and change events |
| `LOGOS_WEB_TOKEN` | Host store; placeholder; web reader config | Host user | MCP header (search); weather for the briefing | `logos-rotate <agent> web` | Searches through the household's filtered reader |
| `LOGOS_CHANGES_TOKEN` | `.env` (placeholder when switched on); web reader config | The agent; host user | `/changes`; the household agent's file_request and docs tools | `logos-rotate <agent> changes` | Reading recent changes and docs; filing requests |
| `LOGOS_HOUSE_TOKEN` | `.env` (placeholder when switched on); house service config | The agent; host user | MCP header; lists copy | `logos-rotate <agent> house` | The lights, scenes and lists that agent may use |
| `LOGOS_MAIL_TOKEN` | `.env` (placeholder when switched on); mail service config | The agent; host user | MCP header; briefing line | `logos-rotate <agent> mail` | Reading that person's mail (read-only tools) |
| `LOGOS_ADVISOR_TOKEN` | `.env` (placeholder when switched on); advisor config | The agent; host user | MCP header | `logos-rotate <agent> advisor` | Asking Claude questions, which each need the person's approval, within the budget |
| `LOGOS_ALERT_TOKEN` | HMAC of the agent's name under `alert-text.key`; placeholder (off: `.env`) | Host user | `Authorization` header to `POST /api/v2/alert-text` | `logos-rotate alert-key` (all agents) | Putting words in that agent's phone alerts for two minutes |
| `LOGOS_SPEAKER_SAY_TOKEN` | HMAC under the same key; `.env` (placeholder when switched on) | The agent; host user | Header to `POST /api/v2/speaker/say` | `logos-rotate alert-key` | Asking Logos to speak on the house speaker (Logos still applies quiet hours and private checks) |
| `ANTHROPIC_API_KEY` (OpenShell provider `anthropic`) | OpenShell only; not attached to any agent (`BRAIN_STAYS_LOCAL`) | Host user through the gateway | (unused by agents) | Console, then `openshell provider update anthropic --credential ANTHROPIC_API_KEY --wait` | Paid API use |

Since E7 the agent still holds three of these: `API_SERVER_KEY`, `MATRIX_RECOVERY_KEY` (and its
Matrix device keys in `platforms/matrix/store`), and whatever is switched off. The first two
cannot move. The agent uses them itself, not in an outgoing request, so a proxy cannot put them in
for it.

### Host-side secrets

All of these live under `~/.config/logos` (the folder should be 0700) or `~/.logos`. The agents'
sandboxes cannot reach them.

| Secret | Where | Read by | Rotate | Blast radius |
| --- | --- | --- | --- | --- |
| `alert-text.key` | `~/.config/logos/alert-text.key` (0600) | The dashboard, re-read for every request | `logos-rotate alert-key` | Forging any agent's alert or speaker tokens. An empty key is refused (it never signs) |
| Dashboard JWT secret | `~/.logos/.jwt_secret` (0600) or `LOGOS_JWT_SECRET` | The dashboard | Delete the file and restart the dashboard; everyone signs in again | Forging any dashboard session, admin included |
| Setup token | `~/.logos/.setup_token` | The first-run page | Deleted once setup is done | Making the first admin before setup is done |
| `LOGOS_INTERNAL_TOKEN` (if set) | The dashboard's environment | The auth middleware (compared in constant time) | Change it in the unit environment and restart | Admin access to the API |
| VAPID push key | `~/.logos/push-vapid.pem` (0600) | The dashboard | Delete it and restart; phones subscribe again | Sending push notifications to subscribed phones |
| Metrics token | Only its hash is kept, in `metrics.json` | The dashboard | Settings › Metrics › new token | Reading the Prometheus metrics |
| Voice-converse token | `voice-converse.token` (0600) | The dashboard; the Home Assistant integration holds a copy | Delete it, restart the dashboard, paste the new one into Home Assistant | Talking to the agents as the house speaker |
| STT token | `stt.token` (0600); passed to `logos-stt` as `-e` (visible in `docker inspect`) | The STT container; the dashboard | Delete it, then `up` an agent with the speaker (the container restarts) | The heard and voiceprint API on 127.0.0.1 |
| Matrix admin password and registration token | `matrix.env` (hand-made: `chmod 600` it) | The dashboard and CLI; the registration token is passed to the container as `-e` | Change it on the server (admin room), then in the file | Admin of the Matrix server; making accounts |
| `@advisor` Matrix login | `advisor-matrix.json` (0600) | The advisor container | Admin reset-password, then delete the token and `up` | The advisor's chat account |
| Advisor dashboard token | `advisor-dashboard-token` (0600) | The dashboard; the advisor config | Delete it, then advisor `up` | Approving advisor questions and budgets |
| Advisor Anthropic key | `advisor.env` (hand-made: `chmod 600`); `--env-file`, so visible in `docker inspect logos-advisor` | The advisor container | Anthropic console, then edit and `up` | Paid API use, within the advisor budget |
| Home Assistant token | `home-assistant` (Logos refuses it unless 0600) | The dashboard; the house container config (0600) | Home Assistant profile › long-lived tokens; edit the file; `up` the household agent | Whatever that (non-admin) Home Assistant user may control |
| Calendar passwords and read keys | `calendar-<person>-password`, `calendar-readers.json` (0600); bcrypt hashes in the calendar volume | Radicale; the calendar service | `scripts/logos-calendar … --reset`; delete `calendar-readers.json` and `up` | That person's calendars |
| Mail sign-ins (Outlook refresh token, Gmail app password) | `mail/<person>/<id>.json` (0600, folder 0700); the mail service config | The mail service | Reconnect in Settings › Email (Outlook), or a new app password (Gmail) | Reading that mailbox (`Mail.Read`) |
| GitHub token (daily changes) | `github-changes-token` (hand-made: `chmod 600`) | The web reader volume (0600) | Make a new fine-grained read-only token, replace the file, `up` | Reading two repositories |
| Docmost login | `docmost` (hand-made: `chmod 600`) | The web reader volume (0600) | Change it in Docmost and in the file, then `up` | The docs space |
| SearXNG secret | `searxng-secret` | The SearXNG settings (0600) | Delete it and `up`; nothing else changes | Low (session signing in SearXNG) |
| Widget credentials | `widgets.json` (0600) | The dashboard | At each service, then Settings › Widgets | Each homelab service's API |
| Model provider keys | `models.env` (hand-made) | Never passed while `BRAIN_STAYS_LOCAL` is on | At each provider | Paid API use |
| Backups | `$LOGOS_BACKUP_DIR/latest` (files 0600 since E7) | restic, offsite | Not a secret to rotate; see "If a backup leaked" | The Matrix database (every account's sessions), dashboard password hashes, conversations |

## Rotation runbook

Check first, and again after: `scripts/logos-rotate status <agent>`.

### A per-agent service token or the model key

```shell
scripts/logos-rotate <agent> noticeboard   # or voice calendar web changes house mail advisor model
```

- **As a placeholder:** the new value goes to the host store, then into the agent's provider
  (`openshell provider update … --wait`). The running agent uses it from its next request. Only
  that service restarts. Its requests may fail for the second or two between the provider update
  and the service restart.
- **In `.env`:** the new value goes into `.env` and the agent is recreated with
  `up --no-model-load`.

By hand, for a placeholder secret, the steps are: write the new value into
`~/.config/logos/agent-secrets/<agent>.env`, then
`KEY=<new> openshell provider update logos-<agent>-<secret> --credential KEY --wait`, then
`scripts/logos-agent up <agent> --no-model-load` (this restarts the service; recreating the agent
does no harm).

### The agent's Matrix access token

```shell
scripts/logos-rotate <agent> matrix
```

This signs in with the stored password and the agent's **existing device id**, so the device, its
keys and its verified status stay. The server ends the old token. As a placeholder, the provider
is updated and the running agent carries on: on the dev machine there was one sync retry, then it
synced normally. Otherwise the agent is recreated.

If the password is lost, or might have leaked: reset it in the Matrix admin room
(`scripts/logos-matrix` has the reset). Put the new one in `LOGOS_MATRIX_PASSWORD` in
`~/.config/logos/agent-secrets/<agent>.env` (0600), then run `logos-rotate <agent> matrix`.

### The agent's API key

```shell
scripts/logos-rotate <agent> api
```

The agent is recreated, because hermes reads the key when it starts. The dashboard reads the new
key when it rebuilds its link to the agent. If the agent's chat in the dashboard then says it is
not allowed in, run `systemctl --user restart logos-gateway`.

### The alert key (all agents' alert and speaker tokens)

```shell
scripts/logos-rotate alert-key
```

This writes a new key (0600). For each running Matrix agent it then either updates the placeholder
providers (live) or recreates the agent. Phone alerts say "New message" for the few seconds in
between.

### The Matrix recovery key (manual)

Only after a leak. In Element, signed in as the agent, or from an Element session for the agent's
account: Settings › Security › reset the cross-signing / secure backup, and keep the new recovery
key. Then:

1. Put the new key in the home volume's `matrix-recovery-key` (0600, owner 10000).
2. Run `scripts/logos-agent up <agent> --no-model-load`.
3. The people verify the agent again in Element X.

### Outside secrets

Home Assistant, Outlook/Gmail, GitHub, Docmost, Anthropic and the widgets: make the new secret at
the outside service, replace the file named in the inventory, then run `up` for the service that
reads it (or restart the dashboard, for the dashboard's own). Revoke the old one at the service
**after** the new one works.

### If an agent was taken over

Run `scripts/logos-rotate <agent> all`. With placeholders on, the tokens it could have shown anyone
are only `API_SERVER_KEY`, the switched-off ones, and its Matrix recovery and device keys. Rotate
the recovery key as well if the person thinks it was read.

### If a backup leaked

The Matrix database in it holds every account's sessions. Rotate:

- every agent's Matrix token (`logos-rotate <agent> matrix`),
- the advisor's Matrix token,
- the people's sessions: they sign out of other sessions in Element X,
- then delete old devices with the admin room.

`auth.db` holds password hashes: ask people to change their dashboard passwords.

## Rollout (once per machine)

1. Deploy as usual. The dashboard and the CLI must run the same code. A Logos older than E7 reads
   an agent's token from its `.env`, finds a placeholder there, and would configure the service
   with that placeholder.
2. Run `scripts/logos-agent up <agent> --no-model-load` for each agent. The first `up` moves the
   agent's existing tokens to the host store **with the same values**, so no service restarts
   for that. It creates the providers and recreates the sandbox. Check:
   `scripts/logos-rotate status`, `openshell provider list`, and a message to each agent.
3. Recommended once, because the old values sat in readable files for weeks (and in VM backups):
   `scripts/logos-rotate <agent> all` for each agent, then `scripts/logos-rotate alert-key`.
4. On production, after the house, mail and advisor were checked: switch them on in
   `agents/<agent>.local.yaml` with `credential_placeholders: {changes: true, house: true, mail: true, advisor: true, speaker_say: true}`,
   then run `up` again. Check that the lists copy, the morning briefing, the daily changes and the
   speaker still work. These are the scripts that read those tokens from `.env`.
5. To undo: `credential_placeholders: false` in `agents/all.local.yaml`, then `up` each agent. The
   values go back to `.env` unchanged and the providers are deleted.

## Known limits

- `config.yaml` in the home volume is set 0640 by hermes's own start script. It holds only
  placeholder references for the secrets switched on. The sandbox runs as a single user, so file
  modes inside it do not separate anything.
- The STT token and the Matrix registration token are passed to their containers as `docker run -e`,
  so they show in `docker inspect` on the host (host user only).
- The host store is covered by the nightly VM backup, not by the offsite export (neither was the
  `.env` before). If it is lost: `up` makes new service tokens on both sides by itself. For the
  Matrix login, reset the password in the admin room and rotate (above).
