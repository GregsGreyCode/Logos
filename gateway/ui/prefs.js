// Your options (ST1), kept with your login so they follow you between devices: the chat panel
// ("slide" away after a moment, or stay "open"), Show thinking, Show scheduled jobs.
// This browser keeps a copy, used before sign-in has answered and if saving fails.
import { api } from './util.js';

const LOCAL = { thinking: 'logos-show-thinking', jobs: 'logos-show-jobs', panel: 'logos-side-pinned' };
let saved = null;

function local(key) {
  try {
    const v = localStorage.getItem(LOCAL[key]);
    if (v === null) return undefined;
    return key === 'panel' ? (v === '1' ? 'open' : 'slide') : v === '1';
  } catch (e) { return undefined; }
}

export function adoptPrefs(me) {
  saved = { ...((me && me.prefs) || {}) };
  for (const k of Object.keys(LOCAL)) if (k in saved) remember(k, saved[k]);
}

function remember(key, value) {
  if (!(key in LOCAL)) return;                // colours, motion: kept with the login only
  try { localStorage.setItem(LOCAL[key], (key === 'panel' ? value === 'open' : value) ? '1' : '0'); } catch (e) { /* private window */ }
}

export function getPref(key, fallback) {
  if (saved && key in saved) return saved[key];
  const v = local(key);
  return v === undefined ? fallback : v;
}

export async function setPref(key, value) {
  if (saved) saved[key] = value;
  remember(key, value);
  const r = await api('/api/v2/me/prefs', { method: 'PUT', body: { [key]: value } }).catch(() => null);
  return !!(r && r.ok);
}
