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
import * as tips from './tips.js';
import { noticeboardView, setBadge } from './noticeboard.js';
import { h, api, getJSON, working, aiState, labelTables, icon, ICONS } from './util.js';
import { adopt } from './appearance.js';
import * as mx from './mx.js';
import * as push from './push.js';

const views = { home: homeView, chat: chatView, brain: brainView, noticeboard: noticeboardView, agents: agentsView,
  models: modelsView, people: peopleView, updates: updatesView, advisor: advisorView, settings: settingsView };
const root = document.getElementById('view');
let slideNext = null;               // 'left' or 'right': the next screen slides in (swipeTabs, below)
let cleanup = null;

const NAMES = { home: 'Home', chat: 'Chat', brain: 'Brain', noticeboard: 'Notifications', agents: 'Agents', models: 'Models',
  people: 'People', updates: 'Updates', advisor: 'Claude Advisor', settings: 'Settings' };
// The screens in the account panel: the initial, top right, lights up on them.
const IN_ACCOUNT = ['advisor', 'models', 'people', 'settings'];

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
const acctBtn = document.getElementById('acct-btn');
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
// A thin line along the top while a screen loads (Greg, 22 Nov): only if it takes a moment, so a quick
// screen shows nothing; it runs ahead, then fills and fades when the screen is in.
const bar = document.body.appendChild(Object.assign(document.createElement('div'), { className: 'top-progress', ariaHidden: 'true' }));
let barTimer = null;
function progress(on) {
  clearTimeout(barTimer);
  if (on) { bar.className = 'top-progress'; barTimer = setTimeout(() => { bar.className = 'top-progress on'; }, 180); return; }
  if (bar.classList.contains('on')) { bar.className = 'top-progress on done'; barTimer = setTimeout(() => { bar.className = 'top-progress'; }, 450); }
}

// The tab bar's highlight glides to the tab you are on (and follows a swipe).
const glow = (() => {
  const tabbar = document.querySelector('.tabbar');
  if (!tabbar) return { place() {}, follow() {} };
  const el = tabbar.appendChild(Object.assign(document.createElement('span'), { className: 'tab-glow', ariaHidden: 'true' }));
  const tabs = () => [...tabbar.querySelectorAll('a')];
  const at = (i) => { const t = tabs()[i]; return t ? { x: t.offsetLeft, w: t.offsetWidth } : null; };
  const set = (x, w, instant) => { el.style.transition = instant ? 'none' : ''; el.style.width = `${w}px`; el.style.transform = `translateX(${x}px)`; };
  return {
    place() {
      const i = tabs().findIndex((a) => a.getAttribute('aria-current') === 'page');
      el.hidden = i < 0;
      const p = at(i); if (p) set(p.x, p.w);
    },
    follow(i, frac) {                     // frac: -1..1 of the way to the next tab, as the finger moves
      const p = at(i), q = at(i + Math.sign(frac));
      if (!p || !q || !frac) return;
      set(p.x + (q.x - p.x) * Math.abs(frac), p.w, true);
    },
  };
})();
window.addEventListener('resize', () => glow.place());

async function route() {
  // Updates now lives in Settings (ST1); old links still work.
  if (location.hash.startsWith('#/updates')) { history.replaceState(null, '', '#/settings/updates'); }
  const [, name = 'home', ...rest] = location.hash.split('/');
  const mine = ++visit;
  dropSheet();
  tips.hide();
  if (cleanup) { cleanup(); cleanup = null; }
  for (const a of document.querySelectorAll('.nav-links a, .tabbar a, .sheet a')) {
    const on = a.dataset.view === name || (a.closest('.tabbar') && (a.dataset.also || '').split(' ').includes(name));
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (IN_ACCOUNT.includes(name)) acctBtn.setAttribute('aria-current', 'page'); else acctBtn.removeAttribute('aria-current');
  glow.place();
  // The top bar names the screen only when the tab bar does not: Home, Chat, Brain and Notices are
  // already lit up at the bottom; the screens under More are not (Greg, 21 Nov).
  const tabbed = ['home', 'chat', 'brain', 'agents', 'noticeboard'].includes(name);
  // Inside one agent (its chat, brain or about): on a phone its own bar replaces the Logos bar.
  document.documentElement.classList.toggle('in-agent', (name === 'chat' && !!rest[0]) || name === 'brain' || (name === 'agents' && !!rest[0]));
  // The top bar's middle (Greg, 21-23 Nov): Home's date, and on the other two tabs their name, centred,
  // between the logo and your initial. Inside an agent its own bar takes over.
  // Every other screen too (Greg, 23 Nov: "Settings" sat further in, behind a divider, than the others).
  const inAgent = (name === 'chat' && !!rest[0]) || name === 'brain' || (name === 'agents' && !!rest[0]);
  const centred = name === 'home' ? new Date().toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' })
    : name === 'chat' && !rest[0] ? 'Agents' : name === 'noticeboard' ? 'Notifications' : !inAgent && NAMES[name] ? NAMES[name] : null;
  document.getElementById('screen-name').textContent = centred !== null ? centred : tabbed ? '' : (NAMES[name] || '');
  document.documentElement.classList.toggle('on-home', name === 'home');
  document.documentElement.classList.toggle('title-centred', centred !== null);
  document.documentElement.classList.toggle('screen-tabbed', tabbed);
  document.title = NAMES[name] ? `${NAMES[name]} · Logos` : 'Logos';
  if (!views[name]) return soon(name);
  // Each visit draws into its own box. A screen that is still loading when the person moves
  // on finishes into a box that is no longer on the page, instead of over the new screen.
  const box = document.createElement('div');
  box.className = name === 'chat' ? 'view-box view-fade' : 'view-box';   // chat screens fade in (smoother switching)
  if (slideNext) {
    // The old screen has just slid out. The new one stays hidden until it has drawn something, then
    // slides in (the box itself is display: contents, so the slide is on the main area).
    const dir = slideNext; slideNext = null;
    box.classList.remove('view-fade');
    root.classList.remove('slide-in-left', 'slide-in-right');
    root.style.transition = 'none'; root.style.transform = ''; root.style.willChange = ''; root.style.opacity = '0';
    const t0 = performance.now();
    const go = () => {
      if (!box.isConnected) return;
      if (!box.childElementCount && performance.now() - t0 < 400) { requestAnimationFrame(go); return; }
      root.style.opacity = '';
      root.classList.add(`slide-in-${dir}`);
      root.addEventListener('animationend', () => root.classList.remove(`slide-in-${dir}`), { once: true });
    };
    requestAnimationFrame(go);
  }
  root.replaceChildren(box);
  window.scrollTo(0, 0);                 // every screen starts at its top
  progress(true);
  const done = await views[name](box, rest, state);
  if (mine === visit) progress(false);
  if (mine === visit) tips.show(tips.keyFor(name, rest), () => mine === visit);    // F41: first visit only
  if (mine === visit) cleanup = done; else if (done) done();
}

const state = { me: null };
// Screens redraw their tables as data arrives; label the cells each time (used by the phone layout).
new MutationObserver(() => labelTables(root)).observe(root, { childList: true, subtree: true });


for (const b of document.querySelectorAll('.logout-btn')) b.addEventListener('click', async () => {
  await mx.forget().catch(() => {});
  await api('/auth/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
});

// F37: a tapped notification opens its chat; with notifications on, keep the service worker current.
push.listen();
try { if (localStorage.getItem('logos.push.pushkey') && push.supported()) push.worker().catch(() => {}); } catch (e) { /* no storage */ }

state.me = await getJSON('/api/v2/me');
mx.bind(state.me.matrix_id);
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
// The bell's number: what waits on you plus what is new (notify.js).
const badge = () => import('./notify.js').then((n) => n.countNew()).then(setBadge).catch(() => {});
badge();
setInterval(badge, 60000);
// The logo cycles while any of your agents is working, in Element X or on a scheduled job too.
// Checked every 10 seconds while this page is on screen.
const activity = async () => {
  if (document.visibilityState !== 'visible') return;
  try {
    const { working: names, model_up: up } = await getJSON('/api/v2/activity');
    working('agents', names.length > 0);
    aiState(up);
    if (names.length) for (const el of document.querySelectorAll('.logo')) el.title = `Working: ${names.join(', ')}`;
  } catch (e) { /* the dashboard restarting: try again next time */ }
};
activity();
setInterval(activity, 10000);
document.addEventListener('visibilitychange', activity);
window.addEventListener('hashchange', route);

// Phone (Greg, 21 Nov; 22 Nov: smoother): swipe between Home, Agents and Notifications, the first three tabs.
// The screen follows the finger one to one (and only stretches a little past the first and last tab);
// let go past about a third of the width, or with a flick, and it carries on off the screen while the
// next slides in from the side you swiped towards; otherwise it springs back. The tab bar's highlight
// follows the finger. Settings › Animations › Off (or the phone's reduced motion) stops the movement; Agent
// movement is only about the agents' marks. Inside an agent the swipe right goes back to all agents (chat-mx), not caught here.
const SWIPE_TABS = ['#/home', '#/chat', '#/noticeboard'];
function swipeTabs(el) {
  let s = null, leaving = false;
  const here = () => {
    const [, name = 'home', ...rest] = location.hash.split('/');
    if (name === 'home') return 0;
    if (name === 'chat' && !rest[0]) return 1;
    if (name === 'noticeboard') return 2;
    return -1;
  };
  const phone = () => window.matchMedia('(max-width: 599px)').matches;
  // Settings › Animations (Greg, 22 Nov: kept apart from the agents' movement), or the phone's reduced motion.
  const motion = () => (window.matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.dataset.anim === 'off' ? 'still' : 'full');
  const reset = (animate) => {
    el.style.transition = animate ? 'transform .38s cubic-bezier(.2, 1.3, .4, 1), opacity .3s ease' : 'none';
    el.style.transform = ''; el.style.opacity = '';
    glow.place();
  };
  el.addEventListener('touchstart', (e) => {
    s = null;
    if (leaving || !phone() || e.touches.length !== 1 || here() < 0 || document.querySelector('.sheet:not([hidden])')) return;
    if (e.target.closest('textarea, input, select, [contenteditable], .mx-chips, .table-box, .md-table, pre, .tabs, .home-widgets.editing, .widget-handle, .home-light, .home-lights-sec')) return;
    const t = e.touches[0];
    s = { x: t.clientX, y: t.clientY, at: here(), dx: 0, lock: null, v: 0, t: e.timeStamp, lx: t.clientX };
  }, { passive: true });
  el.addEventListener('touchmove', (e) => {
    if (!s || e.touches.length !== 1) return;
    // Holding a floating agent (moving it, or recording for it) is not a swipe.
    if (document.documentElement.classList.contains('holding-mark')) { s = null; reset(false); return; }
    const t = e.touches[0];
    const dx = t.clientX - s.x, dy = t.clientY - s.y;
    if (!s.lock) {                                   // decide once: across (a swipe) or down (a scroll)
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
      s.lock = Math.abs(dx) > Math.abs(dy) * 1.2 ? 'x' : 'y';
    }
    if (s.lock !== 'x') return;
    const dt = Math.max(1, e.timeStamp - s.t);
    s.v = 0.8 * ((t.clientX - s.lx) / dt) + 0.2 * s.v;  // px per ms, smoothed
    s.t = e.timeStamp; s.lx = t.clientX; s.dx = dx;
    if (motion() === 'still') return;
    const next = s.at + (dx < 0 ? 1 : -1);
    const edge = next < 0 || next >= SWIPE_TABS.length;
    const w = el.clientWidth || window.innerWidth;
    const x = edge ? dx * 0.25 : dx;                 // a little stretch where there is nothing further
    el.style.transition = 'none'; el.style.willChange = 'transform, opacity';
    el.style.transform = `translateX(${x}px)`;
    el.style.opacity = String(1 - Math.min(Math.abs(x) / (w * 1.6), 0.35));
    if (!edge) glow.follow(s.at, -Math.max(-1, Math.min(1, dx / w)));
  }, { passive: true });
  const end = () => {
    if (!s) return;
    const { dx, v, at, lock } = s; s = null;
    if (lock !== 'x') return;
    const w = el.clientWidth || window.innerWidth;
    const next = at + (dx < 0 ? 1 : -1);
    const far = Math.abs(dx) > w * 0.3, flick = Math.abs(v) > 0.45 && Math.abs(dx) > 30 && Math.sign(v) === Math.sign(dx);
    if (next < 0 || next >= SWIPE_TABS.length || !(far || flick)) { reset(motion() !== 'still'); return; }
    leaving = true;
    slideNext = dx < 0 ? 'right' : 'left';
    const go = () => { leaving = false; location.hash = SWIPE_TABS[next]; };
    if (motion() === 'still') { go(); return; }
    // Carry on the way the finger went, as fast as it was going (a flick is quicker), then the next slides in.
    const ms = Math.max(110, Math.min(220, (w - Math.abs(dx)) / Math.max(Math.abs(v), 1.2)));
    el.style.transition = `transform ${ms}ms cubic-bezier(.3, .7, .4, 1), opacity ${ms}ms ease-out`;
    el.style.transform = `translateX(${dx < 0 ? -w : w}px)`; el.style.opacity = '0';
    setTimeout(go, ms - 10);
  };
  el.addEventListener('touchend', end, { passive: true });
  el.addEventListener('touchcancel', () => { if (s) { s = null; reset(true); } }, { passive: true });
}
swipeTabs(root);
route();
