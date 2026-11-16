# Opening a brain in Obsidian

Each agent's brain is a folder of markdown on a Docker volume. `scripts/logos-sync` runs one
Syncthing container (`logos-sync`) that shares each brain as its own folder with the devices
it was given to, where Obsidian opens it as a normal vault.

A device only receives the brains it was added to. Greg's phone gets `brain-greg`, Béré's gets
`brain-bere`, and both can be given `brain-household`. The server has one device ID, so each
device pairs with it once.

## Server (once per agent)

```shell
scripts/logos-sync up bere        # share this brain; prints the server's device ID
scripts/logos-sync status         # every folder, who it is shared with, connected and pending devices
```

Syncthing 2.1.7, host networking, GUI bound to `127.0.0.1:8384`, files owned by the sandbox user.
Usage reporting and crash reporting are off. Global discovery and relays are on, so a device can
sync away from home; traffic is encrypted between devices either way.

## Each device

1. Install Syncthing (Windows: Syncthing Tray or the official installer; Android: a maintained
   Syncthing app from the Play Store or F-Droid) and Obsidian.
2. In Syncthing, add a device and enter or scan the server's device ID.
3. On the server, the device shows up as pending in `scripts/logos-sync status`. Allow it and
   share the brain: `scripts/logos-sync add bere <DEVICE-ID> "Béré's phone"`
4. On the device, accept the folder "bere brain" and choose where it lives. If no prompt
   appears (common on Android), add the folder by hand: folder ID `brain-bere` exactly, turn the
   server device on for it, and make sure it is not paused.
5. In Obsidian: "Open folder as vault" and pick that folder.

To give the same device a second brain, repeat steps 3 to 5 with the other agent's name.
Some phones stop Syncthing in the background; notes made on the phone then upload when the
Syncthing app is next opened, unless battery optimisation is turned off for it.

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
