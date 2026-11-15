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
