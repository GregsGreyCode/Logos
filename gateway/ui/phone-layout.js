// Home on a phone (Greg, 28 Nov): "Same as desktop" (the desktop's widgets, at its sizes, in its order down then across), or
// "Its own": a list of its own (which widgets, their order, Compact or Full each), kept with your login (the "widget_phone"
// pref: [{id, size}]). Switching to Its own the first time starts from what the phone showed; switching back keeps the list,
// so switching again brings it back. The phone's layout is the one below 1100 px, where the desktop grid stops (home-grid.js).
// Home's Edit and Settings › Home both switch it.
import { getPref, setPref } from './prefs.js';

export const PHONE_MAX = 1099;              // px: below the desktop grid
// Everyone starts with Greg's own arrangement (23 Nov), then moves things as they like.
export const DEFAULT_WIDGETS = ['weather', 'calendar', 'messages', 'next', 'lists', 'lights', 'coming'];

export const phoneMode = () => (getPref('phone_layout', 'same') === 'own' ? 'own' : 'same');

// The desktop's list: its widgets in order (the phone's order when the layout is the same).
export function desktopOrder() {
  const w = getPref('widgets', null);
  if (!Array.isArray(w)) return DEFAULT_WIDGETS;
  // Chosen before the calendar became a widget: it stays, after Next.
  if (!w.includes('calendar') && !getPref('widgets_v2', false)) { const i = w.indexOf('next'); return [...w.slice(0, i + 1), 'calendar', ...w.slice(i + 1)]; }
  return w;
}
export function desktopSize(id) {
  const v = (getPref('widget_sizes', {}) || {})[id];
  return v === 'compact' || v === 'full' ? v : id === 'lists' ? 'compact' : 'full';
}
// What a phone shows with the same layout as the desktop.
export const clonedList = () => desktopOrder().map((id) => ({ id, size: desktopSize(id) }));

// The phone's own list, or null if it never had one.
export function ownList() {
  const l = getPref('widget_phone', null);
  return Array.isArray(l) ? l.filter((i) => i && typeof i.id === 'string' && (i.size === 'compact' || i.size === 'full')) : null;
}
export const saveOwn = (list) => setPref('widget_phone', list.map(({ id, size }) => ({ id, size })));

// Switch. The first time to Its own, the phone's list starts as a copy of what it shows now. In use at once (both prefs are
// set before anything is sent); resolves to whether it was kept.
export function setPhoneMode(mode) {
  const copy = mode === 'own' && !ownList() ? saveOwn(clonedList()) : Promise.resolve(true);
  const set = setPref('phone_layout', mode === 'own' ? 'own' : 'same');
  return Promise.all([copy, set]).then((r) => r.every(Boolean));
}
