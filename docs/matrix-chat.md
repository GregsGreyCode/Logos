# Chat as your Matrix conversation (option 3)

The dashboard's chat is becoming an end-to-end-encrypted Matrix client in the browser, so the web
chat and Element X are one conversation. Stories M0 to M5 are in the spec doc's tab "Settings, fixes,
Matrix chat". The spike on 21 Nov 2026 showed it works (a browser device talked to the dev agent,
verified itself, restored history with the recovery key, and used threads).

## M0 findings (21 Nov 2026)

**Threads are separate conversations.** In the deployed hermes (v0.23.1,
`plugins/platforms/matrix/adapter.py`), `MATRIX_SESSION_SCOPE=room` (Logos's setting) keeps one
conversation for the main room, and a message posted inside a real Matrix thread keeps that thread's
id, so hermes runs it as its own session. "New conversation" in the dashboard will start a thread.

**Verified devices only (M5).** hermes has no switch for it: it shares keys with unverified devices on
purpose. But every decrypted message carries the sending device's trust from the Matrix library
(`event["mautrix"]["trust_state"]`: unverified 0, cross-signed by its owner 100 (TOFU), 200, verified
300). Logos's own agent plugin will wrap the adapter's `_on_room_message` and drop messages below
"cross-signed by its owner" (what verifying a device in Element gives), once Greg's and Béré's
browsers are verified.

**The Matrix files are served by Logos, not a CDN.** `scripts/logos-vendor-matrix` builds them into
`gateway/ui/vendor/matrix/`: matrix-js-sdk 45.1.0 as one ES module (esbuild), and the encryption module
@matrix-org/matrix-sdk-crypto-wasm 18.9.2 (the version the SDK pins), with gzip copies and a
`MANIFEST` of sha256 checksums (checked by `tests/unit/test_vendor_matrix.py`; `--check` re-verifies).
About 2.4 MB is downloaded once, then cached. The page maps the encryption module with an import map:

```html
<script type="importmap">{"imports":{"@matrix-org/matrix-sdk-crypto-wasm":"/ui/vendor/matrix/crypto/index.mjs"}}</script>
```

Checked on dev: served gzipped with the right types (`application/wasm`), and the encryption starts
in a headless browser in about 0.3 s with no request leaving Logos.

## M5: the switch-over (21 Nov 2026)

Greg's and Béré's browsers are connected and verified (checked from the server: their Element X,
Element and Logos devices all carry their own cross-signing signature). Since then:

- **Chat is only in the Matrix room.** A browser that is not connected sees "Connect this browser to chat
  here" (Settings › Matrix device) and the old web chats of the last 30 days, read only. Agents without
  Matrix (none on prod) keep the web chat.
- **The agents answer verified devices only** (`matrix.verified_only: true` on greg, bere and household;
  plugin `infra/agent-plugins/logos-verified-only`). A message from a device below "cross-signed by its
  owner", or an unencrypted one, is dropped and logged ("not from a verified device"). So a Matrix
  password alone is not enough to talk to an agent: a new device must be verified first, in Element
  (or with the recovery key in Logos). Tested on dev: a verified browser's messages were let through;
  an unverified device of the same account, same password, was ignored.
- If a person's own Element X stops getting answers, check its device is verified: Element X › Settings ›
  Sessions, or Logos › Settings › Matrix device (it lists each device as verified or not).

## F36: groups (22 Nov 2026)

A group is a Matrix room you share with at least one of your agents and someone else, such as the
Household Group. Logos adds no group mechanism of its own; the dashboard just shows the rooms that
already exist.

- **Where:** the Conversations list under the floating agents holds each agent's chat and each group,
  newest first, with its last message and an unread count (22 Nov). A group opens at
  `#/chat/<agent>/group/<room id>`, where the agent is the first of yours in the room. Notifications
  for a group message name the group and link to it.
- **What is different from an agent's chat:**
  - The header shows the group's name and who is in it.
  - Each run of someone else's messages starts with their name.
  - Threads show inline, and a scheduled post's thread start is a small label.
  - There are no conversation chips, and no archive of old web chats.
- **Asking an agent:** agents in a group answer only when @mentioned (`MATRIX_REQUIRE_MENTION`).
  Above the message box is one "@ <agent>" chip per agent in the room. With a chip on, the message
  starts with the agent's name and carries `m.mentions` (which hermes goes by) and a mention pill.
  With none on, only the people read it. The choice is remembered per group in this browser. "…is
  thinking" shows only after a message that asks an agent.
- **New group** (F36 part 2): "+ New group" in Conversations asks for a name, people and agents (at
  least one agent). It creates an encrypted room and invites them. An agent joins by itself when
  someone allowed to use it invites it (hermes `_on_invite`). A private agent therefore joins only
  when its owner adds it, and in a group it still answers only its owner. People see the invitation
  at the top of their Conversations, with Join and Decline.

## Voice notes, images and files (22 Nov 2026)

Logos plays, shows and sends them itself, so Element X is not needed for them (`gateway/ui/media.js`).

- **How a file is protected:** in an encrypted room it is encrypted on its own, using the Matrix
  attachment format v2 (AES-256-CTR, with the key, counter and SHA-256 in the message, which is
  itself end-to-end encrypted).
- **Receiving:** the browser downloads the ciphertext through the dashboard's Matrix pass-through,
  from the authenticated media API (`/_matrix/client/v1/media/download`, which refuses requests
  without a login). It checks the hash and decrypts the file in the page. Nothing readable passes
  through the server.
- **Sending:** 📎 works the same way back. The file is encrypted, then the ciphertext is uploaded.
  In a group, the "@ agent" choice goes with the file.
- **In a chat:**
  - a voice note has ▶, its length and a bar to drag;
  - an image shows as a thumbnail; a tap opens it full screen (double-tap or pinch to zoom, Save,
    ✕ or Escape or a swipe down to close);
  - a file shows its name and size, and a tap opens or downloads it;
  - a video plays in the bubble.
- **What the agents do with a photo:** the agents' model is a vision model (LM Studio lists it as
  one). `model.vision: true` in an agent's definition becomes `model.supports_vision` in hermes's
  config, so a photo reaches the model as an image rather than a description.

Checked:
- **The format:** a file encrypted in the browser was decrypted by an independent implementation
  of the spec, and a tampered file was refused.
- **The dev server:** an upload downloaded through the client v1 API with a login, and was refused
  (401) without one.
