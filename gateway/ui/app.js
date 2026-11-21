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
import { h, api, getJSON, working, labelTables, icon, ICONS } from './util.js';
import { adopt } from './appearance.js';

const views = { home: homeView, chat: chatView, brain: brainView, noticeboard: noticeboardView, agents: agentsView,
  models: modelsView, people: peopleView, updates: updatesView, advisor: advisorView, settings: settingsView };
const root = document.getElementById('view');
let slideNext = null;               // 'left' or 'right': the next screen slides in (swipeTabs, below)
let cleanup = null;

const NAMES = { home: 'Home', chat: 'Chat', brain: 'Brain', noticeboard: 'Activity', agents: 'Agents', models: 'Models',
  people: 'People', updates: 'Updates', advisor: 'Claude Advisor', settings: 'Settings' };
const IN_MORE = ['noticeboard', 'advisor', 'models', 'people', 'settings'];

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
// The tab bar sits at the bottom of what is actually visible. Some phone browsers place "bottom: 0" as if
// their address bar were already hidden, so on a short page (Home) the bar sat under the browser's own
// toolbar until you scrolled. The visual viewport says where the visible bottom is. When the on-screen
// keyboard is open the bar steps aside instead of riding on top of the keyboard.
const vv = window.visualViewport;
function placeTabbar() {
  if (!vv) return;
  const hidden = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
  const keyboard = hidden > 150;
  document.documentElement.style.setProperty('--below-visible', `${keyboard ? 0 : hidden}px`);
  document.documentElement.classList.toggle('keyboard-open', keyboard);
}
if (vv) { vv.addEventListener('resize', placeTabbar); vv.addEventListener('scroll', placeTabbar); placeTabbar(); }

// Each row of the phone's panels gets its icon, then its words.
for (const row of document.querySelectorAll('.sheet-row[data-icon]')) row.prepend(icon(ICONS[row.dataset.icon]));
for (const link of document.querySelectorAll('.nav-links a[data-icon]')) link.prepend(icon(ICONS[link.dataset.icon]));

let visit = 0;
async function route() {
  // Updates now lives in Settings (ST1); old links still work.
  if (location.hash.startsWith('#/updates')) { history.replaceState(null, '', '#/settings/updates'); }
  const [, name = 'home', ...rest] = location.hash.split('/');
  const mine = ++visit;
  dropSheet();
  if (cleanup) { cleanup(); cleanup = null; }
  for (const a of document.querySelectorAll('.nav-links a, .tabbar a, .sheet a')) {
    const on = a.dataset.view === name || (a.closest('.tabbar') && (a.dataset.also || '').split(' ').includes(name));
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (IN_MORE.includes(name)) moreBtn.setAttribute('aria-current', 'page'); else moreBtn.removeAttribute('aria-current');
  // The top bar names the screen only when the tab bar does not: Home, Chat, Brain and Notices are
  // already lit up at the bottom; the screens under More are not (Greg, 21 Nov).
  const tabbed = ['home', 'chat', 'brain', 'agents'].includes(name);
  // Inside one agent (its chat, brain or about): on a phone its own bar replaces the Logos bar.
  document.documentElement.classList.toggle('in-agent', (name === 'chat' && !!rest[0]) || name === 'brain' || (name === 'agents' && !!rest[0]));
  document.getElementById('screen-name').textContent = tabbed ? '' : (NAMES[name] || '');
  document.documentElement.classList.toggle('screen-tabbed', tabbed);
  document.title = NAMES[name] ? `${NAMES[name]} · Logos` : 'Logos';
  if (!views[name]) return soon(name);
  // Each visit draws into its own box. A screen that is still loading when the person moves
  // on finishes into a box that is no longer on the page, instead of over the new screen.
  const box = document.createElement('div');
  box.className = name === 'chat' ? 'view-box view-fade' : 'view-box';   // chat screens fade in (smoother switching)
  if (slideNext) { box.classList.remove('view-fade'); box.classList.add(`slide-in-${slideNext}`); slideNext = null; }
  root.replaceChildren(box);
  window.scrollTo(0, 0);                 // every screen starts at its top
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
for (const el of document.querySelectorAll('.nav-foot .who')) { el.textContent = shown; el.title = `Signed in as ${shown}`; }
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

// Phone (Greg, 21 Nov): swipe between Home and Agents, the first two tabs. On Home, swipe left to the
// agents; on the agents, swipe right back Home. The page follows the finger, then the next slides in.
// Inside an agent the swipe right goes back to all agents (chat-mx), so it is not caught here.
const SWIPE_TABS = ['#/home', '#/chat'];
function swipeTabs(el) {
  let start = null;
  const here = () => {
    const [, name = 'home', ...rest] = location.hash.split('/');
    if (name === 'home') return 0;
    if (name === 'chat' && !rest[0]) return 1;
    return -1;
  };
  const phone = () => window.matchMedia('(max-width: 599px)').matches;
  el.addEventListener('touchstart', (e) => {
    start = null;
    if (!phone() || e.touches.length !== 1 || here() < 0 || document.querySelector('.sheet:not([hidden])')) return;
    if (e.target.closest('textarea, input, select, [contenteditable], .mx-chips, .table-box, .md-table, pre, .tabs')) return;
    start = { x: e.touches[0].clientX, y: e.touches[0].clientY, at: here() };
  }, { passive: true });
  el.addEventListener('touchmove', (e) => {
    if (!start || e.touches.length !== 1) return;
    const dx = e.touches[0].clientX - start.x, dy = Math.abs(e.touches[0].clientY - start.y);
    if (dy > 40 && dy > Math.abs(dx)) { start = null; el.style.transform = ''; el.style.opacity = ''; return; }
    const next = start.at + (dx < 0 ? 1 : -1);
    if (next < 0 || next >= SWIPE_TABS.length || dy > Math.abs(dx) / 2) return;
    el.style.transition = 'none';
    el.style.transform = `translateX(${dx * 0.6}px)`;
    el.style.opacity = String(1 - Math.min(Math.abs(dx) / 500, 0.4));
    if (Math.abs(dx) > 90) {
      start = null;
      slideNext = dx < 0 ? 'right' : 'left';           // the next screen comes in from where the finger went
      el.style.transition = ''; el.style.transform = ''; el.style.opacity = '';
      location.hash = SWIPE_TABS[next];
    }
  }, { passive: true });
  el.addEventListener('touchend', () => {
    start = null;
    el.style.transition = 'transform .2s ease, opacity .2s ease'; el.style.transform = ''; el.style.opacity = '';
  }, { passive: true });
}
swipeTabs(root);
route();
