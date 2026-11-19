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

**Agent rules** live in `souls/_shared/calendar.md`:
- Your own person's requests go straight in.
- Anything another agent proposes waits for your person's yes.
- Agents never answer "what's on" from memory.

Every call is logged in `docker logs logos-calendar-service`: the agent, the action and the event id, never the event details. Every change is also in Radicale's git history, as "Changes by logos-service".

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
   - URL: `https://logos-prod.taila8ee9b.ts.net:8443/`
   - your user name and password
4. Tick the calendars to sync: your own and Household.
5. They now appear in the phone's calendar app next to any other accounts.

## Moving events in from Google (story C2)

1. Export each Google calendar from Google Calendar on the web: Settings → Import & export → Export. This gives one `.ics` file per calendar.
2. Import the file into the matching calendar. Pick one way:
   - on the phone, open the `.ics` file and choose the DAVx5 calendar
   - upload it with `curl -u <name> -X PUT` per event
3. Hide the Google calendars in the phone's calendar app once everything has arrived.
