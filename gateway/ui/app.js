// Logos dashboard shell: sign-in handling, theme, and a tiny hash router.
// No build step: plain ES modules served from /ui/.
import { chatView } from './chat.js';
import { homeView } from './home.js';
import { brainView } from './brain.js';
import { agentsView } from './agents.js';
import { peopleView } from './people.js';
import { modelsView } from './models.js';
import { updatesView } from './updates.js';
import { advisorView } from './advisor.js';
import { settingsView } from './settings.js';
import { adoptPrefs } from './prefs.js';
import { noticeboardView, setBadge } from './noticeboard.js';
import { h, api, getJSON, working, labelTables } from './util.js';
import { adopt } from './appearance.js';

const views = { home: homeView, chat: chatView, brain: brainView, noticeboard: noticeboardView, agents: agentsView,
  models: modelsView, people: peopleView, updates: updatesView, advisor: advisorView, settings: settingsView };
const root = document.getElementById('view');
let cleanup = null;

const NAMES = { home: 'Home', chat: 'Chat', brain: 'Brain', noticeboard: 'Noticeboard', agents: 'Agents', models: 'Models',
  people: 'People', updates: 'Updates', advisor: 'Claude Advisor', settings: 'Settings' };
const IN_MORE = ['agents', 'advisor', 'models', 'people', 'settings'];

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
// Leaving the screen (a link in the panel, the Back button) closes an open panel.
function dropSheet() {
  if (!openSheet) return;
  openSheet.sheet.hidden = true; scrim.hidden = true;
  openSheet.opener.setAttribute('aria-expanded', 'false');
  openSheet = null;
}
for (const a of document.querySelectorAll('.sheet a')) a.addEventListener('click', dropSheet);

let visit = 0;
async function route() {
  // Updates now lives in Settings (ST1); old links still work.
  if (location.hash.startsWith('#/updates')) { history.replaceState(null, '', '#/settings/updates'); }
  const [, name = 'home', ...rest] = location.hash.split('/');
  const mine = ++visit;
  dropSheet();
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
// Screens redraw their tables as data arrives; label the cells each time (used by the phone layout).
new MutationObserver(() => labelTables(root)).observe(root, { childList: true, subtree: true });


for (const b of document.querySelectorAll('.logout-btn')) b.addEventListener('click', async () => {
  await api('/auth/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
});

state.me = await getJSON('/api/v2/me');
adopt(state.me);
adoptPrefs(state.me);
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
// The logo cycles while any of your agents is working, in Element X or on a scheduled job too.
// Checked every 10 seconds while this page is on screen.
const activity = async () => {
  if (document.visibilityState !== 'visible') return;
  try {
    const { working: names } = await getJSON('/api/v2/activity');
    working('agents', names.length > 0);
    const label = names.length ? `Working: ${names.join(', ')}` : '';
    for (const el of document.querySelectorAll('.logo')) { el.title = label; }
  } catch (e) { /* the dashboard restarting: try again next time */ }
};
activity();
setInterval(activity, 10000);
document.addEventListener('visibilitychange', activity);
window.addEventListener('hashchange', route);
route();
