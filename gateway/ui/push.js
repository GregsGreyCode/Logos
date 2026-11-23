// Phone notifications (F37), this browser's side. Turning them on: permission, a Web Push
// subscription with Logos's key, the subscription handed to Logos (which keeps it under a random
// pushkey), and that pushkey registered with your Matrix account as an HTTP pusher whose gateway is
// the dashboard. Matrix then tells Logos about each message that should notify you, and Logos pushes
// to this phone who wrote (never what). See gateway/push.py and docs/push.md.
import { api, getJSON } from './util.js';
import * as mx from './mx.js';

const KEY = 'logos.push.pushkey';
const EP = 'logos.push.endpoint';            // the subscription Logos was given, to notice when Chrome swaps it
const saved = () => { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } };
const savedEndpoint = () => { try { return localStorage.getItem(EP) || ''; } catch (e) { return ''; } };
const keep = (v, endpoint = '') => {
  try {
    if (v) { localStorage.setItem(KEY, v); localStorage.setItem(EP, endpoint); } else { localStorage.removeItem(KEY); localStorage.removeItem(EP); }
  } catch (e) { /* kept server side */ }
};

export const supported = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window && window.isSecureContext;

const fromB64u = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4)), (c) => c.charCodeAt(0));

export async function worker() {
  return navigator.serviceWorker.register('/sw.js', { scope: '/' });
}

// The name the device shows under in Settings and in the Matrix account's pushers.
function deviceName() {
  const ua = navigator.userAgent;
  const model = (ua.match(/Android [\d.]+; ([^;)]+)/) || [])[1];
  return `Logos on ${model ? model.trim() : /Android/.test(ua) ? 'Android' : 'this browser'}`;
}

// 'on' (this browser gets them), 'off', 'blocked' (permission refused), or 'unsupported'.
export async function status() {
  if (!supported()) return 'unsupported';
  if (Notification.permission === 'denied') return 'blocked';
  const reg = await navigator.serviceWorker.getRegistration('/');
  const sub = reg && await reg.pushManager.getSubscription();
  if (!sub || !saved()) return 'off';
  const info = await getJSON('/api/v2/push').catch(() => null);
  return info && (info.devices || []).some((d) => d.pushkey === saved()) ? 'on' : 'off';
}

export async function turnOn() {
  if (!supported()) throw new Error('This browser cannot show notifications. Use Chrome, from the app on your home screen.');
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') throw new Error('Notifications are blocked for this site. Allow them in the browser settings, then try again.');
  const info = await getJSON('/api/v2/push');
  const reg = await worker();
  await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (sub) await sub.unsubscribe().catch(() => {});                // a fresh one, with this server's key
  try {
    sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: fromB64u(info.public_key) });
  } catch (e) {
    throw new Error(`This phone could not sign up for notifications (${e.message}). Try again from the Logos icon on your home screen.`);
  }
  return register(sub, info);
}

// Hand a subscription to Logos and register it with the Matrix account; the old one goes.
async function register(sub, info) {
  const device = deviceName();
  const r = await api('/api/v2/push/subscribe', { method: 'POST', body: { subscription: sub.toJSON(), device } });
  const { pushkey } = await r.json();
  if (!r.ok || !pushkey) throw new Error('Logos could not keep this phone\'s subscription.');
  const c = await mx.get();
  const old = saved();
  if (old && old !== pushkey) {
    await c.setPusher({ pushkey: old, app_id: info.app_id, kind: null }).catch(() => {});
    await api('/api/v2/push/unsubscribe', { method: 'POST', body: { pushkey: old } }).catch(() => {});
  }
  await c.setPusher({ kind: 'http', app_id: info.app_id, pushkey, app_display_name: 'Logos', device_display_name: device,
    lang: navigator.language || 'en', data: { url: info.gateway_url }, append: true });
  keep(pushkey, sub.endpoint);
  return pushkey;
}

// Alerts that stopped by themselves (23 Nov: both phones). Chrome sometimes swaps a phone's push
// subscription for a new one; the old one then answers "gone", Logos forgets it, and nothing told
// Logos about the new one. Each time the app opens (at most once an hour), a phone that had alerts
// turned on checks that Logos still has its current subscription, and quietly signs up again if not.
// A phone where they were turned off, or whose permission was taken away, is left alone.
let lastHeal = 0;
export async function heal() {
  if (!supported() || !saved() || Notification.permission !== 'granted' || !mx.saved()) return 'skip';
  if (Date.now() - lastHeal < 3600e3) return 'skip';
  lastHeal = Date.now();
  const info = await getJSON('/api/v2/push');
  const reg = await worker();
  await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  const known = (info.devices || []).some((d) => d.pushkey === saved());
  if (sub && known && sub.endpoint === savedEndpoint()) return 'fine';
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: fromB64u(info.public_key) });
  await register(sub, info);
  return 'renewed';
}

export async function turnOff() {
  const pushkey = saved();
  const info = await getJSON('/api/v2/push').catch(() => ({ app_id: 'logos.web' }));
  if (pushkey) {
    const c = await mx.get().catch(() => null);
    if (c) await c.setPusher({ pushkey, app_id: info.app_id, kind: null }).catch(() => {});
    await api('/api/v2/push/unsubscribe', { method: 'POST', body: { pushkey } }).catch(() => {});
  }
  const reg = supported() && await navigator.serviceWorker.getRegistration('/');
  const sub = reg && await reg.pushManager.getSubscription();
  if (sub) await sub.unsubscribe().catch(() => {});
  keep('');
}

export async function test() {
  const r = await api('/api/v2/push/test', { method: 'POST', body: { pushkey: saved() } });
  const d = await r.json().catch(() => ({}));
  return !!d.sent;
}

// Opening a chat takes its notification away (Logos sends no push when a chat is read).
export async function clearRoom(roomId) {
  if (!supported() || !roomId) return;
  try {
    const reg = await navigator.serviceWorker.getRegistration('/');
    for (const n of (reg ? await reg.getNotifications({ tag: roomId }) : [])) n.close();
  } catch (e) { /* nothing to clear */ }
}

// A tapped notification while the app is open: go to its chat.
export function listen() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'logos-open' && typeof e.data.link === 'string' && e.data.link.startsWith('#/')) location.hash = e.data.link;
    if (e.data && e.data.type === 'logos-push') mx.pushed();
  });
}
