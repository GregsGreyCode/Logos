# Phone notifications (F37, 22 Nov 2026)

Logos's web app on an Android phone (opened from its home-screen icon in Chrome) shows a notification
for each new message that Matrix says should notify you: your agents' replies and briefings, and the
people and agents in your groups. Element X is no longer needed for this. iPhone is not supported
for now (Greg, 22 Nov).

A notification says **who** wrote (and the group's name). For an **agent's** reply it also says
**what**: the agent hands Logos the first words as it sends them (see "What an agent said" below).
A **person's** message stays "New message": messages are end-to-end encrypted, and Logos cannot
read them. Whoever chose private alerts in Settings gets only "Logos · New message". Tapping a
notification opens that chat. A new message from the same chat replaces the earlier notification, and opening the chat
in Logos removes it.

## Turning it on

Settings › Phone alerts › **Turn on**, on each phone. The phone asks for permission once. **Send a test**
checks that it works. **Turn off** removes the phone. The browser must be connected to Matrix first
(Settings › Matrix device).

## How it works

```
message ──► Matrix server (Continuwuity) ──push rules──► POST /_matrix/push/v1/notify
                                                         (the dashboard, on the Docker bridge)
            gateway/push.py: who wrote + room ──Web Push, encrypted to the phone──► Google's push
            service ──► the phone's service worker (gateway/ui/sw.js) shows the notification
```

1. **Turn on** (`gateway/ui/push.js`) has four steps:
   1. The phone asks for permission.
   2. It subscribes to Web Push with Logos's public key (`GET /api/v2/push`).
   3. It posts the subscription to `POST /api/v2/push/subscribe`. Logos keeps it in `push.db`
      beside `auth.db`, under a random **pushkey**.
   4. It registers that pushkey on your Matrix account as an HTTP pusher (`app_id` `logos.web`)
      whose gateway is `http://<docker bridge>:<dashboard port>/_matrix/push/v1/notify`.
2. **When a message notifies you,** Matrix posts to that gateway. Only the Docker bridge and this
   machine may post there. For each Logos pushkey in the request, `gateway/push.py` builds:
   - the title (the group's name, or the sender's name);
   - the text: an agent's first words when it handed them over, otherwise "New message" or
     "N new messages";
   - a link to `#/chat/room/<room>`, which the app turns into the agent's chat or the group.

   It then sends an RFC 8291 (aes128gcm) Web Push signed with VAPID (RFC 8292). The VAPID key
   lives in `push-vapid.pem`, owner-only, beside `auth.db`. Its contact is
   `mailto:push@logos.invalid`, so no one's address leaves the machine.
3. **When a phone is gone,** the push service answers 404 or 410. Logos then forgets that phone and
   returns its pushkey to Matrix as rejected, so Matrix drops the pusher.
4. **When a room is read,** Matrix sends just its unread count, and Logos sends nothing. Android
   shows a generic notice for any push that shows nothing, so the page clears the notification when
   the chat is opened instead.

## What an agent said (23 Nov 2026, option 1)

```
agent sends a reply ──► Matrix server ──push──► dashboard waits up to 2.5 s for ─┐
      └─ logos-alert-text plugin ──POST /api/v2/alert-text──► the reply's words ──┴─► the alert
```

- **Who can hand words over.** Each agent's plugin (`infra/agent-plugins/logos-alert-text`) posts
  `{agent, token, event_id, text}` after its message is sent. The token is an HMAC of the agent's
  name under `~/.config/logos/alert-text.key` (owner-only), so Logos checks it without reading the
  agent's files. The sandbox policy (`alerts`) lets the agent reach only that one path, POST only.
- **Only its own messages.** The words are used only when Matrix says the message's sender is
  `@<that agent>-agent`. An agent cannot put words in another agent's or a person's alert.
- **Kept briefly.** Logos keeps the first 180 characters (Markdown marks removed, cut at a word)
  in memory for two minutes, for the push that follows. Never on disk, never logged.
- **When it doesn't arrive.** If the words come too late, or the plugin can't reach Logos, the
  alert says "New message", as before. A reply is never delayed or stopped by this.
- **Only text.** Photos, files and voice notes keep the plain alert. Edits notify nobody.
- **The privacy trade.** The agents run on this server, so their replies are written here anyway:
  this adds no new place the text lives. On its way to the phone, the alert is encrypted to that
  phone (RFC 8291), so Google's push service cannot read it.

### Later: option 2, the phone reads the message itself

Option 2 would also show what **people** write (Béré writing to Greg, a group), which option 1
cannot: only the phone has the keys. The service worker would fetch the event and decrypt it with
the phone's own Matrix keys. Not built, because:

- **Two writers on one key store.** The keys live in the app's IndexedDB. The service worker and an
  open tab would both use them, and two writers can corrupt the store. The result is "Unable to
  decrypt" messages, which are hard to recover from. Element X has had this class of bug in its
  own notification code.
- **A few seconds of budget.** Android gives the worker a few seconds per push, and it would need to
  load the crypto code (several MB of WASM), open the store, fetch and decrypt. When it is too slow,
  Chrome shows its own "This site has been updated in the background" notice.
- **Keys that arrive late.** A key can be missing (just after signing in, or when it is shared late),
  so it would still need the "New message" fallback.
- **Hard to test.** It depends on real push timing on a real phone.

If built, the safe shape is likely: the worker decrypts **read-only** (never writes to the key store;
a key it lacks means "New message"), with a hard time limit, and only when no tab is open. Option 1
would stay for agents, since it needs no keys on the phone.

## The Matrix server's setting

Continuwuity refuses to call private addresses by default (`ip_range_denylist`). Logos keeps that
default but leaves out `172.17.0.0/16`, the Docker bridge where the dashboard listens
(`gateway/matrix_host.py` `IP_DENYLIST`). With federation off, push is the only thing the server
sends out. The deploy restarts the Matrix server when its settings change; chat reconnects within
seconds.

## Checked

- **Unit tests** (`tests/unit/test_push.py`):
  - encryption, checked against an RFC 8291 decryption written separately in the test;
  - the VAPID signature;
  - the key file is owner-only;
  - subscriptions;
  - what a notification says, and that message content never appears in it;
  - only the bridge may post;
  - gone phones are rejected;
  - a read room sends nothing.
- **On dev, end to end** (22 Nov), with the real Continuwuity:
  1. two test accounts and a pusher pointing at `gateway/push.py` on the bridge;
  2. a message sent;
  3. a fake push service decrypted the push with the "phone's" key: the sender, link and unread
     count were there, and the words of the message were not;
  4. the VAPID signature checked out.
