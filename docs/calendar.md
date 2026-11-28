# Calendar: our own CalDAV server

Personal and household calendars live on our own server, not with Google (decided 18 Nov 2026; see the Calendar tab of the spec doc). Stories C1 (the server) and C3 (agents). Agents never get a calendar password: they use the Logos calendar service, which decides what each one may see.

- **Server:** Radicale 3.9.2, in the `logos-calendar` container and volume (`gateway/calendar_host.py`).
  - It listens on localhost only. `tailscale serve` gives it an HTTPS address on the tailnet.
  - It is not reachable from the LAN or the internet. The container has no DNS, so it cannot call out.
- **Who sees what:**
  - Each person sees their own `personal` calendar.
  - The shared **Household** calendar shows up in both people's accounts, linked in by a symlink.
  - Nobody can open another person's calendar.
- **History:** every change is committed to git inside the volume (`collections/.git`), with who made it. This is the backup and the undo.
- **Passwords:** stored as bcrypt hashes in the volume. A new password is written once to `~/.config/logos/calendar-<name>-password` (mode 0600). Type it into DAVx5, then delete the file.

## Agents (story C3)

The Logos calendar service (`gateway/calendar_service.py`) runs in the `logos-calendar-service` container.

- It speaks MCP on the Docker bridge (port 8095). Each agent has its own token in its home volume.
- It uses Radicale with its own account, `logos-service`, over a private Docker network called `logos-calendar`. Sandboxes cannot reach Radicale itself.
- That account may read and write events, but cannot create or delete calendars.

Each agent definition says what that agent sees:

```yaml
calendar: {enabled: true, person: greg}                  # Greg's own calendar + Household, in full;
                                                         # Béré's calendar read only (seen, never changed)
calendar: {enabled: true, free_busy: {greg: Greg, bere: Béré}}   # Household in full; each person's busy times only
```

A person's agent sees the other people's own calendars in full but read only (decided 19 Nov),
so it can tell when someone is free. Any change to them is refused ("someone else's
calendar"); to get something into someone else's calendar it uses Household or asks their
agent through the noticeboard. The household agent still sees only busy times.

The dashboard's Home reads with each person's own read-only key (`person:<name>`), which
sees what their phone sees: their calendar, Household and the other people's.

| Tool | What it does |
| --- | --- |
| `list_calendars` | What the agent can use, and how |
| `list_events` | Events in a date range, repeats expanded. Busy-only calendars give times, never titles |
| `add_event` | Add to `personal` or `household` |
| `update_event` | Move or rename. Moving keeps the length |
| `cancel_event` | Delete one event |
| `find_free_time` | Free slots across everything the agent can see. Books nothing |

**Repeating events are read-only for agents.** They are changed on the phone, so an agent can never move or delete a whole series by mistake.

**Morning briefing** (story C4): each person's agent sends the day's events and weather in
Element X at 07:00 on weekdays and 09:00 at weekends. See `docs/briefing.md`.

**Agent rules** live in `souls/_shared/calendar.md`:
- Your own person's requests go straight in.
- Anything another agent proposes waits for your person's yes.
- Agents never answer "what's on" from memory.

**Reading fast** (27 Nov). `list_events` and `find_free_time` used to ask Radicale to expand
repeating events (`<C:expand>`); Radicale then ignores the time range and parses every event in
the calendar, seconds for a calendar imported from Google with years of history. Now:
- the service asks only for the events in the range, as stored (Radicale answers that from its
  index), and expands repeats itself with the same library Radicale uses (vobject), applying
  EXDATE, RDATE and moved or cancelled instances. All-day events count by the reader's own days.
- each calendar's events are kept in memory a month at a time, with the calendar's
  last-modified time when read. Each call first asks for that time (one cheap PROPFIND per
  calendar); unchanged months are not read again. A change by this service clears that
  calendar at once; a change on a phone shows on the next call. A calendar changed in the last
  second is always read afresh (the time has one-second steps).
- the calendars are read at the same time (up to four at once).
- the service's Radicale account has a cheap password hash (cost 4; its password is 32 random
  bytes), because Radicale checks it on every request. `up` rehashes it. People keep cost 12.
- Who may see which calendar is decided before the cache is used, so a calendar never reaches
  an agent through the cache that it could not read directly.

Every call is logged in `docker logs logos-calendar-service`: the agent, the action and the event id, never the event details. Every change is also in Radicale's git history, as "Changes by logos-service".

## The Calendar screen in the dashboard

Béré asked for it on 27 Nov, and Greg made it a full app the same day. It is in the side menu on a desktop and the fourth tab on a phone (after Home, Agents and Notifications), and Home's calendar widgets open it.

- **Views:** Month (the default), Week, Day and List (the next four weeks, only the days with something on).
  - On a phone the month shows dots, and the day you tap is listed under it. A swipe moves a month or a week.
  - On a desktop the month shows the events by name (two lines at most), fills the screen's height, and the day you pick sits beside it. Day and List keep their words in a readable column with the month beside them (a day there opens it) and the key under it.
  - The period shown (the month, the week, the day) is the screen's heading; tap it to go to a date.
  - Day shows each event whole: title, whose, where and the notes, line by line. Week and List wrap long titles; nothing is cut short with "…".
- **Fast:** the calendar is always fetched a whole month at a time (as the month view shows it, so a week or a day is inside it): switching Month, Week, Day and List never waits. This month and the ones either side are fetched quietly when the app opens; two months either side of the one shown are fetched ahead. What the device kept (five minutes) draws at once; a fresh copy replaces it in place only if something changed, keeping the picked day and an open event. A first visit shows a placeholder. `tests/ui/checks/calendar_speed.mjs` times every switch with the calendar answering in 1.6 s.
- **Calendars:** each has its colour and a switch: yours, Household, then everyone else's. The key with the switches sits under the calendar (under the month grid, above the day's list). The switches are remembered on that device.
- **An event:** tap it to see all of it (when, calendar, where, notes, whether it repeats).
- **Who sees what:** exactly what Home shows. It reads with your own read-only key, so you see your calendar, Household and the other people's. Someone without a calendar account sees what their agent sees: busy times show as "Busy".
- **Adding, changing, cancelling:** these go through your own agent. That is an agent that is yours alone and has `calendar: {person: <you>}`. It has the same rights as when you ask it in chat:
  - only your calendar and Household
  - never someone else's (their events say so)
  - never a repeating event (change those on the phone)
  - The calendar service checks all of this again. Changes show in Radicale's history as the service's.
  - With no such agent, **Add** opens your agent's chat with "Add an event on …:" typed in, ready to finish.
- **Home:**
  - The **Month** widget (Edit → + Month) shows this month small, with today's events under it (Full), or just the month and its dots (Compact).
  - Tapping a line in Today or Coming up opens the Calendar at that day, with the event open. Tapping a heading opens that day or the list.
- **API:**
  - `GET /api/v2/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD` covers both days, at most 62. It returns `events`, `busy`, `calendars` (`key`, `name`), `today`, `agent` and `can_change`.
  - `POST /api/v2/calendar/add|update|cancel` makes a change.
  - The dashboard keeps each answer for a minute, and the page keeps it for five, so moving between months costs one call per new range. A change clears both.
- Code: `gateway/home.py` (`calendar_range`, `calendar_change`), `gateway/ui/calendar.js`, `gateway/ui/calendar.css`. Screen check: `tests/ui/checks/calendar.mjs`.

## Setting it up on a machine

```
scripts/logos-calendar up
scripts/logos-calendar add-person greg Greg
scripts/logos-calendar add-person bere "Béré"
tailscale serve --bg --https=8443 http://127.0.0.1:5232
```

After that, `scripts/logos-deploy` keeps the server up to date.

## On a phone (Android)

1. Keep Tailscale on.
2. Install DAVx5 (F-Droid or Play Store).
3. In DAVx5, add an account with **Login with URL and user name**:
   - URL: `https://logos.<tailnet>.ts.net:8443/`
   - your user name and password
4. Tick the calendars to sync: your own and Household.
5. They now appear in the phone's calendar app next to any other accounts.

## Moving events in from Google (story C2)

1. Export each Google calendar from Google Calendar on the web: Settings → Import & export → Export. This gives one `.ics` file per calendar.
2. Import the file into the matching calendar. Pick one way:
   - on the phone, open the `.ics` file and choose the DAVx5 calendar
   - upload it with `curl -u <name> -X PUT` per event
3. Hide the Google calendars in the phone's calendar app once everything has arrived.
