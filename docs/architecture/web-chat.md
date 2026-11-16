# Web chat: dashboard to agent

How a message typed in the dashboard reaches an agent (stories S2.8 and S3.5).

```
browser  --POST /chat-->  gateway  --openshell forward service-->  hermes API (127.0.0.1:8642 in the sandbox)
```

## Turning it on

Start the gateway with `LOGOS_SANDBOX_BACKEND=openshell` (for example in `~/.logos/.env`). The
host needs the `openshell` CLI and a running OpenShell gateway, and the agents are created with
`scripts/logos-agent up <name>` as before.

## Running it as a service

`infra/systemd/logos-gateway.service` is a user unit (install steps are in the file). On first
start there are no accounts and no default login. The gateway writes a one-time setup token and
prints a link; `logos gateway setup-link` shows it again. Setup is accepted only with that token
or from the server itself, and the token is deleted once the first account exists.

## What happens

1. **Agent list.** Every `agents/<name>.yaml` gets a row in the gateway's `agents` table, at boot
   and whenever the list is requested. A definition with `dashboard: false` is left out.
   `owners: [username, ...]` makes an agent visible only to those accounts. Admins get no
   exception. Without `owners`, every signed-in user sees the agent.
2. **Boot check.** At start the gateway logs, per agent, whether its sandbox is running and whether
   hermes answers `/health`.
3. **Link.** On first use the gateway starts `openshell forward service <name> --target-port 8642
   --local 127.0.0.1:0` and reads the chosen port. The API key is the `API_SERVER_KEY` in the
   agent's home volume. A dead forward is replaced on the next request, and forwards end when the
   gateway does. Code: `gateway/agent_links.py`.
4. **Turn.** `/chat` starts a hermes run and translates its events into the stream the dashboard
   reads (`token`, `thinking`, `tool_start`, `tool_end`, `done`, `error`). Code:
   `gateway/agent_chat.py`.
5. **Stop.** `/chat/<run id>/cancel` stops the run. The text already shown is kept.

## Sessions

Each dashboard conversation is one hermes session named `web-<user id>-<chat id>`, so the agent
keeps the history itself and two users can never continue each other's conversation. Telegram
conversations are different sessions of the same agent: memory and brain are shared, message
history is not.

## Known gaps

- The Logs and Mind panels still read v1 paths and show nothing useful.
- Reloading the page mid-reply loses the live stream; the run finishes inside the agent.
- Approval prompts from hermes are not shown in the dashboard yet.
