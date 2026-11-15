# Opening a brain in Obsidian

Each agent's brain is a folder of markdown on a Docker volume. `scripts/logos-sync` runs one
Syncthing container per agent that shares that folder with its person's devices, where Obsidian
opens it as a normal vault.

## Server (once per agent)

```shell
scripts/logos-sync up greg        # prints this server's device ID
scripts/logos-sync status greg    # folder state, connected and pending devices
```

Syncthing 2.1.7, host networking, GUI bound to `127.0.0.1:8384`, files owned by the sandbox user.
Usage reporting and crash reporting are off. Global discovery and relays are on, so a device can
sync away from home; traffic is encrypted between devices either way.

## Each device

1. Install Syncthing (Windows: Syncthing Tray or the official installer; Android: a maintained
   Syncthing app from the Play Store or F-Droid).
2. In Syncthing, add a device and enter or scan the server's device ID.
3. On the server, the device shows up as pending. Allow it and share the brain:
   `scripts/logos-sync add greg <DEVICE-ID> "Greg's phone"`
4. On the device, accept the folder "greg brain" and choose where it lives.
5. In Obsidian: "Open folder as vault" and pick that folder.

## What is and isn't synced

| Path | Synced | Why |
| --- | --- | --- |
| Notes (`inbox/`, `daily/`, `people/`, `projects/`, `topics/`, `README.md`) | Yes, both ways | The brain itself |
| `.obsidian/` settings | Yes | Same Obsidian setup on every device |
| `.obsidian/workspace*.json` | No | Per-device window state; changes constantly |
| `.git/` | No | History stays on the server; the agent commits hourly |

## Editing rules

- The person's edits win. The agent is told never to undo them and to re-read a page before editing.
- If both sides change the same file at once, Syncthing keeps both and names one
  `*.sync-conflict-*`. The agent leaves both alone and reports the page.
- Anything a device deletes or overwrites is kept on the server for 30 days in `.stversions/`,
  and the hourly git commit keeps the long-term history.
