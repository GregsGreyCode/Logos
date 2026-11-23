// This browser as one of your encrypted Matrix devices (story M1). The Matrix files are Logos's own
// pinned copies (gateway/ui/vendor/matrix, story M0); the server is reached through the dashboard
// (/_matrix/..., gateway/matrix_proxy.py). The access token and the device's keys stay in this browser:
// the session in localStorage, the encryption keys in IndexedDB. The Matrix password is used once,
// to sign in, and for signing out other devices; it is never kept.
// The session is kept per dashboard account: a browser only ever uses the Matrix session of whoever
// is signed in to the dashboard (owner, from /api/v2/me), never one a previous person left behind.
import * as reach from './reach.js';

const LEGACY = 'logos-mx-session';
let owner = null;
const key = () => `logos-mx-session:${owner}`;
let sdkP = null, client = null, secretKey = null, startP = null;

// Called once the dashboard knows who is signed in (app.js), before any screen uses Matrix.
export function bind(matrixId) {
  owner = matrixId || null;
  try {
    const old = JSON.parse(localStorage.getItem(LEGACY) || 'null');
    if (old && owner && old.userId === owner && !localStorage.getItem(key())) localStorage.setItem(key(), JSON.stringify(old));
    if (old && (!owner || old.userId === owner)) localStorage.removeItem(LEGACY);
  } catch (e) { /* private window */ }
}

const sdk = () => (sdkP ||= import('/ui/vendor/matrix/matrix.js'));
const base = () => location.origin;
// The Matrix library sends its requests without cookies; the pass-through needs the dashboard's
// session cookie (same origin only), alongside the Matrix token in the Authorization header.
const fetchFn = (url, opts = {}) => fetch(url, { ...opts, credentials: 'same-origin' }).then(
  (r) => { reach.ok(); return r; },
  (e) => { if (!(opts.signal && opts.signal.aborted)) reach.lost(); throw e; });   // reach.js

export function saved() {
  if (!owner) return null;
  try {
    const s = JSON.parse(localStorage.getItem(key()) || 'null');
    return s && s.userId === owner ? s : null;
  } catch (e) { return null; }
}
function remember(s) {
  if (!owner || s.userId !== owner) throw new Error(`Signed in to Matrix as ${s.userId}, not ${owner}.`);
  try { localStorage.setItem(key(), JSON.stringify(s)); } catch (e) { /* private window */ }
}

async function make(s) {
  const m = await sdk();
  // Threads (conversations, M3) are grouped by the page from each message's thread mark, so they do
  // not depend on the library's thread mode or the server's thread API.
  const c = m.createClient({ baseUrl: base(), fetchFn, userId: s.userId, accessToken: s.accessToken, deviceId: s.deviceId,
    cryptoCallbacks: { getSecretStorageKey: async ({ keys }) => (secretKey ? [Object.keys(keys)[0], secretKey] : null) } });
  await c.initRustCrypto({ useIndexedDB: true, cryptoDatabasePrefix: `logos-${s.userId}` });
  return c;
}

// The running client for this browser, started once; null when this browser is not connected.
export async function get() {
  const s = saved();
  if (!s) return null;
  if (client) return client;
  startP ||= (async () => {
    const m = await sdk();
    const c = await make(s);
    await new Promise((ok) => { c.once(m.ClientEvent.Sync, () => ok()); c.startClient({ initialSyncLimit: 20 }); });
    client = c;
    return c;
  })().catch((e) => { startP = null; throw e; });
  return startP;
}

// Step 1: sign in with the Matrix password (used once) and become a new device.
export async function connect(userId, password) {
  const m = await sdk();
  const tmp = m.createClient({ baseUrl: base(), fetchFn });
  const r = await tmp.login('m.login.password', { identifier: { type: 'm.id.user', user: userId }, password,
    initial_device_display_name: `Logos (${browserName()})` });
  remember({ userId: r.user_id, deviceId: r.device_id, accessToken: r.access_token, at: Date.now() });
  return get();
}

// Step 2: the recovery key proves this browser is you (cross-signs it) and unlocks earlier messages.
export async function unlock(recoveryKey) {
  const m = await sdk();
  const c = await get();
  secretKey = m.decodeRecoveryKey(recoveryKey.trim());
  try {
    const crypto = c.getCrypto();
    await crypto.bootstrapCrossSigning({});
    // Holding the keys is not enough: sign this browser with them, as Element does on "verify".
    await crypto.crossSignDevice(c.getDeviceId());
    let restored = null;
    try {
      await crypto.loadSessionBackupPrivateKeyFromSecretStorage();
      restored = await crypto.restoreKeyBackup();
    } catch (e) { restored = { error: e.message }; }
    // Nudge the library to re-read your identity and devices, so other screens see the new signature soon.
    crypto.getUserVerificationStatus(c.getUserId()).catch(() => {});
    return { verified: await isVerified(), restored };
  } finally {
    secretKey = null;                     // the key itself is not kept, only what it unlocked
  }
}

// Verified: the server-confirmed status, or (sooner and just as sure) this browser holding your
// cross-signing keys, which only the right recovery key gives it, and having signed itself with them.
export async function isVerified() {
  const c = await get();
  if (!c) return false;
  const crypto = c.getCrypto();
  const v = await crypto.getDeviceVerificationStatus(c.getUserId(), c.getDeviceId());
  if (v && v.crossSigningVerified) return true;
  const s = await crypto.getCrossSigningStatus();
  return !!(s.publicKeysOnDevice && s.privateKeysCachedLocally.masterKey && s.privateKeysCachedLocally.selfSigningKey);
}

// Your devices: name, last seen, verified or not, and which one is this browser.
export async function devices() {
  const c = await get();
  const { devices: list } = await c.getDevices();
  const crypto = c.getCrypto();
  return Promise.all(list.map(async (d) => {
    const v = await crypto.getDeviceVerificationStatus(c.getUserId(), d.device_id);
    return { id: d.device_id, name: d.display_name || d.device_id, lastSeen: d.last_seen_ts || 0,
      verified: !!(v && v.crossSigningVerified), here: d.device_id === c.getDeviceId() };
  }));
}

// Sign out another device: the server asks for your Matrix password again.
export async function signOut(deviceId, password) {
  const c = await get();
  try {
    await c.deleteDevice(deviceId);
  } catch (e) {
    const flows = e.data && e.data.session;
    if (!flows) throw e;
    await c.deleteDevice(deviceId, { type: 'm.login.password', identifier: { type: 'm.id.user', user: c.getUserId() },
      password, session: e.data.session });
  }
}

// Disconnect this browser: sign this device out and forget its keys here.
export async function disconnect() {
  const s = saved();
  try { const c = await get(); if (c) { c.stopClient(); await c.logout(true); } } catch (e) { /* signed out already */ }
  await wipe(s);
}

async function wipe(s) {
  client = null; startP = null;
  try { localStorage.removeItem(key()); } catch (e) { /* private window */ }
  if (s && window.indexedDB && indexedDB.databases) {
    for (const db of await indexedDB.databases()) if (db.name && db.name.startsWith(`logos-${s.userId}`)) indexedDB.deleteDatabase(db.name);
  }
}

// Signing out of the dashboard signs this browser's Matrix device out too and forgets its keys, so
// the next person to use this browser cannot read or send as you. Done straight with the token (no
// sync first) and before the dashboard session ends, since the pass-through needs that session.
export async function forget() {
  const s = saved();
  if (!s) return;
  if (client) client.stopClient();
  await fetch('/_matrix/client/v3/logout', { method: 'POST', credentials: 'same-origin',
    headers: { Authorization: `Bearer ${s.accessToken}`, 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
  await wipe(s);
}

function browserName() {
  const ua = navigator.userAgent;
  const b = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'browser';
  const os = /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iPhone' : /Windows/.test(ua) ? 'Windows' : /Mac OS/.test(ua) ? 'Mac' : /Linux/.test(ua) ? 'Linux' : '';
  return os ? `${b} on ${os}` : b;
}
