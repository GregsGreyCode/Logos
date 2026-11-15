# OpenShell gateway (Logos v2)

Logos v2 runs each agent in an OpenShell **0.1.8** sandbox on the Docker compute driver.
The gateway runs on the host as a systemd **user** service.

## Install (once per host, needs sudo)

Requirements: Docker Engine 28.0 or later, systemd.

```shell
curl -fsSL -o /tmp/openshell-install.sh \
  https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.8/install.sh
less /tmp/openshell-install.sh            # read it first: it runs sudo apt/dpkg
OPENSHELL_VERSION=v0.1.8 sh /tmp/openshell-install.sh
```

The installer verifies package checksums, installs the Debian package, and enables the
`openshell-gateway` user service on `https://127.0.0.1:17670`.

If an OpenShell 0.0.x CLI is still installed, the installer refuses to continue until the old
runtime is cleaned up with the old CLI (`openshell sandbox delete --all`, then
`openshell gateway destroy`), because 0.1.x has no `gateway destroy`.

## Configure

```shell
mkdir -p ~/.config/openshell
cp infra/openshell/gateway.toml ~/.config/openshell/gateway.toml
openshell-gateway config preflight
systemctl --user restart openshell-gateway
openshell status          # expect: Status: Connected, Version: 0.1.8
```

`gateway.toml` turns on `allow_driver_config` so each agent's brain volume can be attached,
and leaves resource admission on (see `docs/adr/0001-agent-brains-on-docker-volumes.md`).

## Start at boot

A user service only starts at boot when lingering is enabled for that user:

```shell
loginctl enable-linger            # may need: sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger   # expect: Linger=yes
```

Sandboxes are created with `--restart-policy always`, so the gateway brings them back after a
reboot or a crash.

## Useful commands

```shell
systemctl --user status openshell-gateway
journalctl --user -u openshell-gateway -n 50 --no-pager
openshell sandbox list
```

## Providers (credentials the sandbox never sees)

A new 0.1.x gateway has no provider profiles; import the ones Logos uses, then store the secret:

```shell
openshell provider profile lint   -f infra/openshell/providers/anthropic.yaml
openshell provider profile import -f infra/openshell/providers/anthropic.yaml --global
ANTHROPIC_API_KEY=sk-ant-... openshell provider create \
  --name anthropic --type anthropic --credential ANTHROPIC_API_KEY
```

Agents attach it with `providers: [anthropic]` in `agents/<name>.yaml`. Inside the sandbox
`ANTHROPIC_API_KEY` is a placeholder (`openshell:resolve:env:...`); the gateway swaps in the real
key only in the `x-api-key` header of requests to `api.anthropic.com`, and only for the hermes
Python interpreter. Verified: the API answers 200 with the placeholder, and the same placeholder
sent to another host is refused (`credential_endpoint_mismatch`).

| Service | How its credential is handled |
| --- | --- |
| Anthropic | OpenShell provider `anthropic` (header placement). The sandbox holds a placeholder. |
| LM Studio | No credential. Reached through the `lm_studio` network rule only. |
| Telegram | **Not** a provider. OpenShell 0.1.8 path placement matches one exact URL path per credential, and the Bot API puts the token in the path of every method (`/bot<token>/<method>`). The token lives in the agent's private home volume (`/opt/data/.env`, mode 0600) and the network policy limits it to `api.telegram.org`. Revisit when OpenShell supports wildcard path templates (S7.3). |
