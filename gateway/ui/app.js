// Logos dashboard shell: sign-in handling, theme, and a tiny hash router.
// No build step: plain ES modules served from /ui/.
import { chatView } from './chat.js';
import { brainView } from './brain.js';
import { agentsView } from './agents.js';
import { peopleView } from './people.js';
import { modelsView } from './models.js';
import { updatesView } from './updates.js';
import { noticeboardView, setBadge } from './noticeboard.js';
import { h, api, getJSON } from './util.js';

const views = { chat: chatView, brain: brainView, noticeboard: noticeboardView, agents: agentsView,
  models: modelsView, people: peopleView, updates: updatesView };
const root = document.getElementById('view');
let cleanup = null;

const NAMES = { chat: 'Chat', brain: 'Brain', noticeboard: 'Noticeboard', agents: 'Agents', models: 'Models',
  people: 'People', updates: 'Updates' };
const IN_MORE = ['models', 'people', 'updates'];

function soon(name) {
  root.replaceChildren(h('div', { class: 'empty' },
    h('h1', {}, name[0].toUpperCase() + name.slice(1)),
    h('p', {}, 'This screen is not built yet.')));
}

// ── panels (phone): More and the account menu ────────────────────────────────
const scrim = document.getElementById('scrim');
let openSheet = null;            // { sheet, opener }
function closeSheet() {
  if (!openSheet) return;
  const { sheet, opener } = openSheet;
  openSheet = null;
  sheet.hidden = true; scrim.hidden = true;
  opener.setAttribute('aria-expanded', 'false');
  opener.focus();
}
function toggleSheet(opener, sheet) {
  if (openSheet) { const same = openSheet.sheet === sheet; closeSheet(); if (same) return; }
  sheet.hidden = false; scrim.hidden = false;
  opener.setAttribute('aria-expanded', 'true');
  openSheet = { sheet, opener };
  const first = sheet.querySelector('a:not([hidden]), button:not([hidden])');
  if (first) first.focus();
}
scrim.addEventListener('click', closeSheet);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && openSheet) closeSheet(); });
const moreBtn = document.getElementById('more-btn');
const acctBtn = document.getElementById('acct-btn');
moreBtn.addEventListener('click', () => toggleSheet(moreBtn, document.getElementById('more-sheet')));
acctBtn.addEventListener('click', () => toggleSheet(acctBtn, document.getElementById('acct-sheet')));
for (const a of document.querySelectorAll('.sheet a')) a.addEventListener('click', () => {
  if (openSheet) { openSheet.sheet.hidden = true; scrim.hidden = true; openSheet.opener.setAttribute('aria-expanded', 'false'); openSheet = null; }
});

let visit = 0;
async function route() {
  const [, name = 'chat', ...rest] = location.hash.split('/');
  const mine = ++visit;
  if (cleanup) { cleanup(); cleanup = null; }
  for (const a of document.querySelectorAll('.nav-links a, .tabbar a, .sheet a')) {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (IN_MORE.includes(name)) moreBtn.setAttribute('aria-current', 'page'); else moreBtn.removeAttribute('aria-current');
  document.getElementById('screen-name').textContent = NAMES[name] || '';
  document.title = NAMES[name] ? `${NAMES[name]} · Logos` : 'Logos';
  if (!views[name]) return soon(name);
  // Each visit draws into its own box. A screen that is still loading when the person moves
  // on finishes into a box that is no longer on the page, instead of over the new screen.
  const box = document.createElement('div');
  box.className = 'view-box';
  root.replaceChildren(box);
  const done = await views[name](box, rest, state);
  if (mine === visit) cleanup = done; else if (done) done();
}

const state = { me: null };

function paintTheme() {
  const light = document.documentElement.dataset.theme === 'light';
  for (const b of document.querySelectorAll('.theme-btn')) b.textContent = light ? 'Switch to dark' : 'Switch to light';
  // The phone's own bar follows the theme too (Add to Home screen, Android's address bar).
  for (const m of document.querySelectorAll('meta[name="theme-color"]')) m.content = light ? '#ffffff' : '#171a21';
}

for (const b of document.querySelectorAll('.theme-btn')) b.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('logos-theme', next); } catch (e) { /* private window: not remembered */ }
  paintTheme();
});
for (const b of document.querySelectorAll('.logout-btn')) b.addEventListener('click', async () => {
  await api('/auth/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
});

paintTheme();
state.me = await getJSON('/api/v2/me');
const shown = state.me.display_name || state.me.username;
if (state.me.role === 'admin') for (const el of document.querySelectorAll('.admin-only')) el.hidden = false;
for (const el of document.querySelectorAll('.nav-foot .who')) el.textContent = `Signed in as ${shown}`;
for (const el of document.querySelectorAll('.sheet-who .who')) el.textContent = shown;
for (const el of document.querySelectorAll('.sheet-who .role')) el.textContent = state.me.role === 'admin' ? 'Manages Logos' : 'Member';
for (const el of document.querySelectorAll('.avatar')) el.textContent = (shown || '?').trim().charAt(0).toUpperCase();
acctBtn.setAttribute('aria-label', `Your account (${shown})`);
// Keep the "waiting on you" count in the menu current on every screen.
const badge = () => getJSON('/api/v2/noticeboard').then((d) => setBadge(d.waiting.length)).catch(() => {});
badge();
setInterval(badge, 60000);
window.addEventListener('hashchange', route);
route();
