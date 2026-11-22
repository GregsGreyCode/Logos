# Phone notifications (F37, 22 Nov 2026)

Logos's web app on an Android phone (opened from its home-screen icon in Chrome) shows a notification
for each new message that Matrix says should notify you: your agents' replies and briefings, and the
people and agents in your groups. Element X is no longer needed for this. iPhone is not supported
for now (Greg, 22 Nov).

A notification says **who** wrote (and the group's name), never **what**. Messages are end-to-end
encrypted, and Logos does not read, keep or forward their content. Tapping a notification opens
that chat. A new message from the same chat replaces the earlier notification, and opening the chat
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
   - the text ("New message", or "N new messages");
   - a link to `#/chat/room/<room>`, which the app turns into the agent's chat or the group.

   It then sends an RFC 8291 (aes128gcm) Web Push signed with VAPID (RFC 8292). The VAPID key
   lives in `push-vapid.pem`, owner-only, beside `auth.db`. Its contact is
   `mailto:push@logos.invalid`, so no one's address leaves the machine.
3. **When a phone is gone,** the push service answers 404 or 410. Logos then forgets that phone and
   returns its pushkey to Matrix as rejected, so Matrix drops the pusher.
4. **When a room is read,** Matrix sends just its unread count, and Logos sends nothing. Android
   shows a generic notice for any push that shows nothing, so the page clears the notification when
   the chat is opened instead.

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
