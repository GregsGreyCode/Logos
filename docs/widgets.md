# Homelab widgets on Home (F42, 23 Nov 2026)

Design: https://claude.ai/artifact/DJ7eLVTkisqY2p7rWggQKs (adding a widget, the twelve widgets on a
phone, the states they share, desktop Home). Server: `gateway/widgets.py`. Page: `gateway/ui/widgets.js`.

## Using them

Home › the pencil › **Add** lists the widgets. One that is not set up shows a dashed card saying what
it shows. Whoever manages Logos taps **Set it up**, fills in the sheet, taps **Test**, then **Save**
(Save also tests first: nothing is saved that does not work).

| Widget | Who sees it | What setup asks |
|---|---|---|
| **Services** | everyone at home | One per line: `Name, http://address`. Any answer counts as up (a login page too); an error 5xx or no answer is down. |
| **Servers** | whoever manages Logos | Proxmox's address, an API token and its secret (below). |

### A read-only Proxmox token

1. Proxmox › Datacenter › Permissions › Users › Add: `logos@pve` (no password needed).
2. Permissions › Add › User permission: path `/`, user `logos@pve`, role **PVEAuditor** (read only).
3. API Tokens › Add: user `logos@pve`, token ID `home`, untick "Privilege separation". Copy the secret
   (Proxmox shows it once).
4. In Logos: address `https://<proxmox>:8006`, token ID `logos@pve!home`, the secret, and tick
   "Proxmox uses its own certificate" (Proxmox makes its own unless you gave it one).

## How it fits

- **Keys stay on the server**, in `~/.config/logos/widgets.json` (owner-only). A browser sees a secret
  only as "set"; agents never see any of it. The server asks the service and hands Home the numbers.
- **The five states** every widget shares: loading (placeholders in its shape), fine, needs you (the
  problem first, in red, and a line in Notifications), can't reach the source (the last copy, dimmed,
  "as of 01:40", with why), and needs setup (the dashed card).
- **Fetched at most once per refresh** (Services 60 s, Servers 30 s), whoever asks. The page keeps the
  last copy too, so Home opens at once.

## Adding a kind

1. In `gateway/widgets.py`: an async `fetch(conf, session, memo)` returning
   `{"state": "ok" | "needs", "summary": "…", ...its numbers}`, raising `Problem("plain words")` when it
   cannot; then a `Kind(id, name, who, refresh, fields, fetch, about=…)` in `KINDS`.
2. In `gateway/ui/widgets.js`: a `DRAW[id](data)` that returns its elements.
3. Tests: the fetch against a stand-in (tests/unit/test_widgets.py), and a screen check.

Next in the design's order: Proxmox VMs and containers (the same token), backups, energy and UPS (via
Home Assistant), Pi-hole/AdGuard, network, cameras (Frigate), now playing (Jellyfin/Plex), downloads
(*arr), bookmarks, and Agents today.
