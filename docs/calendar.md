# Calendar: our own CalDAV server

Personal and household calendars live on our own server, not with Google (decided 18 Nov 2026; see the Calendar tab of the spec doc). This is story C1. Agents get calendar access in C3, through a Logos calendar service, never with a calendar password of their own.

- **Server:** Radicale 3.9.2, in the `logos-calendar` container and volume (`gateway/calendar_host.py`).
  - It listens on localhost only. `tailscale serve` gives it an HTTPS address on the tailnet.
  - It is not reachable from the LAN or the internet. The container has no DNS, so it cannot call out.
- **Who sees what:**
  - Each person sees their own `personal` calendar.
  - The shared **Household** calendar shows up in both people's accounts, linked in by a symlink.
  - Nobody can open another person's calendar.
- **History:** every change is committed to git inside the volume (`collections/.git`), with who made it. This is the backup and the undo.
- **Passwords:** stored as bcrypt hashes in the volume. A new password is written once to `~/.config/logos/calendar-<name>-password` (mode 0600). Type it into DAVx5, then delete the file.

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
   - URL: `https://logos-prod.tailnet.ts.net:8443/`
   - your user name and password
4. Tick the calendars to sync: your own and Household.
5. They now appear in the phone's calendar app next to any other accounts.

## Moving events in from Google (story C2)

1. Export each Google calendar from Google Calendar on the web: Settings → Import & export → Export. This gives one `.ics` file per calendar.
2. Import the file into the matching calendar. Pick one way:
   - on the phone, open the `.ics` file and choose the DAVx5 calendar
   - upload it with `curl -u <name> -X PUT` per event
3. Hide the Google calendars in the phone's calendar app once everything has arrived.
