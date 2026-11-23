// Show the last copy at once, then fresh (Greg, 23 Nov: Home and Notifications do not change often).
// Each answer is kept on this device with the time it came; a screen draws the kept one straight away
// and asks again only when it is older than that screen's own limit (lights: always). Sign out clears
// all of it, so nothing stays behind on a shared computer.
import { getJSON } from './util.js';

const PREFIX = 'logos.cache.';
const mem = new Map();

export function peek(key) {
  if (mem.has(key)) return mem.get(key);
  try {
    const v = JSON.parse(localStorage.getItem(PREFIX + key) || 'null');
    if (v && typeof v.t === 'number') { mem.set(key, v); return v; }
  } catch (e) { /* nothing kept */ }
  return null;
}

export function keep(key, value) {
  const v = { t: Date.now(), v: value };
  mem.set(key, v);
  try { localStorage.setItem(PREFIX + key, JSON.stringify(v)); } catch (e) { /* full or private: memory only */ }
}

export function forget(key) {
  mem.delete(key);
  try { localStorage.removeItem(PREFIX + key); } catch (e) { /* fine */ }
}

export function clearAll() {
  mem.clear();
  try { for (const k of Object.keys(localStorage)) if (k.startsWith(PREFIX) && k !== PREFIX + 'owner') localStorage.removeItem(k); } catch (e) { /* fine */ }
}

// draw(value, stale) with the kept copy (stale: true) if there is one, then with a fresh one when the kept
// copy is missing or older than maxAge ms. A failed fetch keeps what is shown; with nothing kept it draws
// the fallback. Answers that are errors are drawn but not kept.
export async function swr(url, maxAge, draw, fallback = { error: 'unavailable' }) {
  const kept = peek(url);
  if (kept) draw(kept.v, true);
  if (kept && Date.now() - kept.t < maxAge) return;
  let fresh;
  try { fresh = await getJSON(url); } catch (e) { if (!kept) draw(fallback, false); return; }
  if (fresh && !fresh.error) keep(url, fresh);
  draw(fresh, false);
}

// Whose copies these are: someone else signing in on this browser (a session that ran out, say) starts
// with none of the last person's.
export function owner(username) {
  try {
    const was = localStorage.getItem(PREFIX + 'owner');
    if (was !== username) {
      clearAll();
      localStorage.removeItem('logos-home-last');
      localStorage.setItem(PREFIX + 'owner', username);
    }
  } catch (e) { /* no storage: memory only */ }
}
