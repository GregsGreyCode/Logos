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

## Arranging Home on a desktop (F49)

On a wide screen (1100 px and up) Home is a 12-column grid. In Edit (the pencil): drag a widget by its handle to anywhere,
resize it by its corner (double-click the corner: as tall as its content again), and the others make room and pack upward.
The layout is each person's own (`widget_grid`); the phone's order follows it, reading down then across. A phone keeps its
single column, in that order.

- **Compact or Full, every widget** (24 Nov): each widget's bar in Edit has **Compact / Full**, each person's own
  (`widget_sizes`, `{widget: "compact" | "full"}`), on a phone too. A widget nobody chose for looks as it always did: Lists
  Compact, every other one Full. What Compact leaves out:

  | Widget | Compact |
  |---|---|
  | Next | the word "Next"; "40 min" instead of "in 40 min" |
  | Weather | a line a place: now, the town, tomorrow's high |
  | Calendar | today only, what is still to come, without whose and where |
  | Messages | who and when, not the message's first words |
  | Coming up | the next three, without whose |
  | Lights | smaller pills with the room's name only (the fill still shows how bright) |
  | Lists | a tile per list (Full: every thing, with a box to tick it off) |
  | Services | four at most, problems first, no times for the ones that are up |
  | Servers | a line a server: CPU, memory and disk as numbers, no bars |
  | Proxmox | a row a guest, running or stopped, without CPU and memory |
  | Backups | "fine", or when it ran for one that is not |
  | Ad blocking | the ring and the share blocked, without the counts and the top blocked |
  | Now playing | what and where, a line each, without the poster, bar and times |
  | Downloads | the first three, without bars and what is coming this week |
  | Bookmarks | the names as a row of links, without the tiles |
  | Energy | using now and today, without the cost; the UPS only when it is on battery |
  | Network | down and up only |
  | Cameras | the first two snapshots, without the note |
  | Agents today | each agent and how many messages today |

  The Las Vegas sign lives in the title band, not on the board, and has no sizes.
- **Each widget's own minimum size**, Compact and Full, in pixels (`MIN` in `gateway/ui/home-grid.js`), turned into
  columns for the board's width (a minimum is more columns on a small screen). Measured, not guessed: the screen check
  `tests/ui/checks/widget_minimums.mjs` draws every widget with real-looking content at widths from 160 to 640 px and keeps
  the narrowest from which nothing more spills out, is cut off or wraps onto more lines than with room; the height shows
  what the widget is for at that width (all of a fixed-shape widget, the first rows of a list). The check fails when a
  widget no longer fits its own minimum; `MEASURE=1 node tests/ui/checks/widget_minimums.mjs out` (with `serve.py`
  running) prints a new table. A widget is never smaller than its minimum: not by its corner, not by Alt+arrows, and a
  place saved before (or a Compact widget turned Full) grows to it.
- **Weather in more than one place** (24 Nov): Edit › **Places** lists your places in order, each with ↑ ↓ ✕, and finds
  another by name (up to 8; `weather_places`, a list of `{name, lat, lon}`). Weather shows a row a place, each with its
  own now, today and tomorrow, in that order. Someone who chose one town before keeps it, as the first. `GET
  /api/v2/weather` answers `{"places": [...]}` in your order (one that cannot be read says so; the others still show),
  with the first place's numbers at the top as well. Only each town's position, rounded to about a kilometre, goes to
  Open-Meteo, and each place is kept 20 minutes whoever asks.
- **While dragging or resizing**: a dashed ghost where the widget will land, the 12 columns faintly, and a guide line
  wherever one of its edges lines up with another widget's (or the board's edge).
- **Tidy up** (beside Reset layout, desktop only): everything packed neatly in the same order. The board is cut into equal
  tracks (the screen's usual widget width: 6 columns on a laptop, 4 on 1920, 3 on a very wide screen), each widget is made
  a whole number of tracks wide (the nearest to its width, never under its minimum), then, in order, goes to the highest
  free spot that still comes after the one before it. Heights you set are kept.
- **Agents today**: each agent opens its chat (`#/chat/<agent>`), by tap, click or Enter; not while arranging.
