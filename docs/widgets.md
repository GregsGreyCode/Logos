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
| **Proxmox** (VMs and containers) | whoever manages Logos | Nothing more: it uses the Servers setup. |
| **Backups** | whoever manages Logos | Nothing: Logos's own nightly export always; Proxmox's backup runs once Servers is set up. Failed, or late (Logos over 26 h, Proxmox over 8 days), needs you. |
| **Ad blocking** | everyone at home | Pi-hole (6+, an app password) or AdGuard Home (its admin login), and the address. |
| **Now playing** | everyone at home | Jellyfin (an API key) or Plex (an X-Plex-Token), and the address. |
| **Downloads** | everyone at home | Sonarr and/or Radarr: address and API key (Settings › General). A stalled download needs you. |
| **Agents today** | everyone at home | Nothing: Logos counts itself. Messages each of your agents sent today and when the last went, and whether the AI computer is on. Never what was said. Also exposed to Prometheus: see [metrics.md](metrics.md). |
| **Las Vegas sign** | everyone at home | Text that scrolls across the top of Home, between the date and the pencil, on a wide screen (not shown on a phone). Your own lines, one per line, and RSS or Atom feeds, one address per line, up to 5; each feed item's title scrolls past, so a stock or crypto price feed works as a ticker ("BTC $64,210 (+1.2%)"). Speed: slow, medium or fast. Pauses when pointed at. A feed that fails is skipped, the rest still scroll. Feeds are fetched by the Logos server, never by a browser. |
| **Bookmarks** | everyone at home | One per line: `Name, address`. |
| **Energy** | everyone at home | Which Home Assistant sensors: power now, energy today, and optionally a price and the UPS (battery, status, time left). Uses the Home Assistant account Logos already has for the lights; Test lists sensors when one is not found. On battery needs you. |
| **Network** | whoever manages Logos | Speedtest Tracker's address and a read-only API token. Under half the usual download speed needs you. |
| **Cameras** | whoever manages Logos | Frigate's address (and its login, if it asks). Snapshots come through Logos; the browser never talks to Frigate. |

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

All the widgets in the design are built.

A kind may share another's setup (`uses="servers"`: Proxmox reads and edits the Servers token), read
another's alongside its own (`extra={"with": "servers"}`: Backups), or need none (no fields).

## Lists: Compact, Full, and ticking things off

Edit (the pencil) shows **Compact / Full** in the Lists widget's bar, each person's own choice. Compact is a tile
per list. Full shows each list's things one under another, with when they are due. In Full each thing has a box:
tap it and the item is ticked off in Home Assistant (through the house service, as the household agent), with
**Undo** for a few seconds. The lists are read live from Home Assistant; if it cannot be reached the copy the
household agent keeps (refreshed every 15 minutes) shows instead, to read only. `GET /api/v2/home/lists`,
`POST /api/v2/home/lists/tick {list, item, done?}`.

## Arranging Home on a phone

While you arrange Home, the bar at the bottom (where Ask is) becomes one big **Done**, always in view.
