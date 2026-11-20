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
