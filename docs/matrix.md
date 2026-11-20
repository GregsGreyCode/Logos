# Matrix: talking to agents privately

Matrix is how people talk to their agents. Every message stays on our own machines and is end-to-end encrypted. (Telegram, used until 19 Nov 2026, has been removed.)

- **Server:** Continuwuity, in the `logos-matrix` container and volume (`gateway/matrix_host.py`).
  - It listens on localhost, so `tailscale serve` can give it an HTTPS address on the tailnet.
  - It also listens on the Docker bridge, which is how agent sandboxes reach it.
  - It is not reachable from the LAN or the internet.
- **Federation is off.** The server never talks to any other Matrix server, and nothing ever looks up its name.
- **Server name:** `gregandbere.com`. It is permanent and part of every ID (`@bere:gregandbere.com`). It does not have to resolve.
- **Signing in:** people type the tailnet address into Element (prod: `logos-prod.taila8ee9b.ts.net`). The server's `/.well-known/matrix/client` answers with that same address.

## Settings

Settings live in `~/.config/logos/matrix.env` on each machine, never in the repo. See the docstring in `gateway/matrix_host.py`.

- On first start, Logos creates `@logos-admin`, the server's first account and its admin. It uses the one-time token the server prints in its log.
- One-time setup per machine:

  ```
  sudo tailscale set --operator=$USER
  tailscale serve --bg --https=443 http://127.0.0.1:6167
  ```

## Agents

Each agent is set up in `agents/<name>.yaml`:

```yaml
matrix: {enabled: true, e2ee: required, allowed_users: []}
```

Put the people allowed to chat with it in the untracked `agents/<name>.local.yaml` as `@name:gregandbere.com`.

On its first `up`, the agent gets its own account, `@<name>-agent`. Its login is kept in the agent's home volume.

- **Encryption:** the agent sets up cross-signing on first connect. It writes a recovery key to `/opt/data/matrix-recovery-key` (mode 0600) and is given that key on every later start, so it stays verified when its sandbox is rebuilt.
- **Sandbox policy:** the `matrix` rule in `infra/agent-image/policy.yaml` allows encoded slashes in request paths. Matrix secret-storage key IDs need them; without this, cross-signing fails with "Server disconnected".
- **Reminders and digests:** scheduled messages (reminders, digests, urgent noticeboard alerts) go to the agent's Matrix `home_room` (`delivery()` in `gateway/agent_runtime.py`).

## Adding a person

1. Create their account with a temporary password. Use `MatrixHost.register` or Element's sign-up with the registration token.
2. They install Tailscale and Element X, sign in to the tailnet address, and change the password.
3. Add them to the agent's `allowed_users` and run `scripts/logos-agent up <name>`.
4. They start a chat with `@<name>-agent:gregandbere.com`. The agent joins by itself.

## The household room

The household agent talks to both people in one shared room.

1. Someone creates a private room, for example "Household", in Element X.
2. They invite the other person and `@household-agent:gregandbere.com`. The agent joins by itself.

In the room it answers only when @mentioned (`require_mention: true`). Once it replies in a thread, follow-ups in that thread need no mention. A one-to-one chat with the agent always gets an answer.

It answers only people in its `allowed_users`, and any room they invite it to works.

## Someone forgot their Matrix password

```
ssh logos@192.168.1.226            # or, already on the server as yourself: sudo -iu logos
cd ~/logos && scripts/logos-matrix reset-password bere
```

It runs on the server as the `logos` user (Logos lives in /home/logos/logos). It asks first, then gives them a new random password using the server's own
admin command as `@logos-admin`. The command and the server's answer are redacted from the admin room
straight away, so the password does not stay in its history; the new password is checked by signing in
once, then printed in your terminal only. Their phone stays signed in (no other sign-in is logged out),
and their encryption is untouched: the **recovery key** is separate. If the recovery key is lost too,
make a new one in Element X on their phone (Settings › Security & Privacy › Recovery), which still holds
their keys.
