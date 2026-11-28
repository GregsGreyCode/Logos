// Logos dashboard shell: sign-in handling, theme, and a tiny hash router.
// No build step: plain ES modules served from /ui/.
import { chatView } from './chat.js';
import { homeView } from './home.js';
import { brainView } from './brain.js';
import { calendarView, warmCalendar } from './calendar.js';
import { listsView } from './lists.js';
import { agentsView } from './agents.js';
import { peopleView } from './people.js';
import { modelsView } from './models.js';
import { updatesView } from './updates.js';
import { advisorView } from './advisor.js';
import { settingsView } from './settings.js';
import { adoptPrefs } from './prefs.js';
import * as tips from './tips.js';
import { clearAll as clearCache, owner as cacheOwner } from './cache.js';
import { noticeboardView, setBadge } from './noticeboard.js';
import { h, api, getJSON, working, aiState, labelTables, icon, ICONS, wouldLose, setViewer, leaveSubpage } from './util.js';
import { adopt } from './appearance.js';
import * as mx from './mx.js';
import * as push from './push.js';
import * as reach from './reach.js';

const views = { home: homeView, chat: chatView, brain: brainView, calendar: calendarView, lists: listsView, noticeboard: noticeboardView, agents: agentsView,
  models: modelsView, people: peopleView, updates: updatesView, advisor: advisorView, settings: settingsView };
const root = document.getElementById('view');
let slideNext = null;               // 'left' or 'right': the next screen slides in (swipeTabs, below)
let cleanup = null;

const NAMES = { home: 'Home', chat: 'Agents', brain: 'Brain', calendar: 'Calendar', lists: 'Lists', noticeboard: 'Notifications', agents: 'Routines', models: 'Models',
  people: 'People', updates: 'Updates', advisor: 'Claude Advisor', settings: 'Settings' };
// The screens in the account panel: the initial, top right, lights up on them.
// Lists (28 Nov) are in the panel on a phone, whose tab bar is full; Home's Lists widget opens them too.
const IN_ACCOUNT = ['advisor', 'models', 'people', 'settings', 'lists'];   // the Calendar is a phone tab (27 Nov), not in the panel

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
// Ctrl/Cmd+K (28 Nov): the search at the top of Settings, from anywhere on a desktop but a chat.
document.addEventListener('keydown', (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 'k' || e.altKey || e.shiftKey) return;
  if (document.querySelector('.agent-bar.has-hotkey')) return;      // in a chat Ctrl+K switches agent (agents-void.js agentBar)
  e.preventDefault();
  const box = document.querySelector('.set-search input');
  if (box) { box.focus(); box.select(); return; }
  try { sessionStorage.setItem('logos.settings.search', '1'); } catch (err) { /* opens without focus */ }
  location.hash = '#/settings';
});
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
  // A chat's ?beside=… (the second chat beside it on a wide screen, Greg 28 Nov) is not part of the path.
  const [, name = 'home', ...rest] = location.hash.split('?')[0].split('/');
  const mine = ++visit;
  try { sessionStorage.setItem(`logos.last.${name}`, location.hash); } catch (e) { /* private window: no memory */ }
  dropSheet();
  tips.hide();
  leaveSubpage();                        // a sub-page's back arrow leaves the top bar with it
  if (cleanup) { cleanup(); cleanup = null; }
  for (const a of document.querySelectorAll('.nav-links a, .tabbar a, .sheet a')) {
    const on = a.dataset.view === name || (a.closest('.tabbar') && (a.dataset.also || '').split(' ').includes(name));
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (IN_ACCOUNT.includes(name)) acctBtn.setAttribute('aria-current', 'page'); else acctBtn.removeAttribute('aria-current');
  glow.place();
  // The top bar names the screen only when the tab bar does not: Home, Chat, Brain and Notices are
  // already lit up at the bottom; the screens under More are not (Greg, 21 Nov).
  const tabbed = ['home', 'chat', 'brain', 'agents', 'noticeboard', 'calendar'].includes(name);
  // Inside one agent (its chat, brain or about): on a phone its own bar replaces the Logos bar.
  document.documentElement.classList.toggle('in-agent', (name === 'chat' && !!rest[0]) || name === 'brain' || (name === 'agents' && !!rest[0]));
  // The top bar's middle (Greg, 21-23 Nov): Home's date, and on the other two tabs their name, centred,
  // between the logo and your initial. Inside an agent its own bar takes over.
  // Every other screen too (Greg, 23 Nov: "Settings" sat further in, behind a divider, than the others).
  const inAgent = (name === 'chat' && !!rest[0]) || name === 'brain' || (name === 'agents' && !!rest[0]);
  // (the full weekday again: the pencil left the top bar, Greg 23 Nov; the month short on a narrow phone)
  const centred = name === 'home' ? new Date().toLocaleDateString([], { weekday: 'long', day: 'numeric', month: innerWidth < 390 ? 'short' : 'long' })
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
  state.refresh = null;                  // a screen says here how to refresh itself (pull down); else it is drawn again
  const done = await views[name](box, rest, state);
  if (mine === visit) progress(false);
  if (mine === visit) tips.show(tips.keyFor(name, rest), () => mine === visit);    // F41: first visit only
  if (mine === visit) cleanup = done; else if (done) done();
}

const state = { me: null };
// Screens redraw their tables as data arrives; label the cells each time (used by the phone layout).
new MutationObserver(() => labelTables(root)).observe(root, { childList: true, subtree: true });


for (const b of document.querySelectorAll('.logout-btn')) b.addEventListener('click', async () => {
  clearCache();                                   // nothing of yours stays on a shared computer (Greg, 23 Nov)
  try { localStorage.removeItem('logos-home-last'); } catch (e) { /* fine */ }
  await mx.forget().catch(() => {});
  await api('/auth/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
});

// F37: a tapped notification opens its chat; with notifications on, keep the service worker current.
push.listen();
// The worker also keeps a copy of the app, so it opens and says why when the server is out of reach
// (sw.js), so it runs for everyone, with or without notifications.
if ('serviceWorker' in navigator && window.isSecureContext) push.worker().catch(() => {});

// Out of reach on opening (reach.js shows why): wait, and start over once the server answers.
state.me = await getJSON('/api/v2/me').catch((e) => {
  if (e.status) throw e;
  reach.onBack(() => location.reload());
  return new Promise(() => {});
});
// Back after being out of reach: draw the screen again with fresh data (a chat keeps itself current,
// and redrawing it would lose what is being typed).
reach.onBack(() => { badge(); if (!location.hash.startsWith('#/chat')) route(); });
cacheOwner(state.me.username);
setViewer(state.me);
// The Calendar's months (this one and those either side) quietly, once the first screen has had its turn, so it opens at once.
setTimeout(() => { if (!location.hash.startsWith('#/calendar')) warmCalendar(); }, 400);
mx.bind(state.me.matrix_id);
// An installed web app reopened from the recents list keeps running the code it loaded days ago (23 Nov:
// a phone still had the old chat, without the fixes made since). So when the app comes back to the screen,
// and every ten minutes, it asks which Logos the server runs; if that is not the one it loaded, it starts
// over, unless something would be lost (typing, a photo waiting, a message not sent): then it waits.
const loadedCommit = (state.me.version || {}).commit || '';
let updating = false;
const upToDate = async () => {
  if (updating || !loadedCommit || document.visibilityState !== 'visible') return;
  let now = '';
  try { now = ((await getJSON('/api/v2/me')).version || {}).commit || ''; } catch (e) { return; }
  if (now && now !== loadedCommit && !wouldLose()) { updating = true; location.reload(); }
};
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') upToDate(); });
setInterval(upToDate, 10 * 60000);
// Phone alerts that Chrome quietly swapped out are signed up again (push.js heal).
const healPush = () => push.heal().catch(() => {});
setTimeout(healPush, 3000);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') healPush(); });
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
// The menu remembers where you were in each section (Greg, 23 Nov: going back and forth between menus on a desktop was poor):
// from Brain, Agents takes you back into the chat you left; pressing the section you are already in goes to its start.
document.addEventListener('click', (e) => {
  const a = e.target.closest('.nav-links a[data-view]');
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
  const here = location.hash.split('/')[1] || 'home';
  if (a.dataset.view === here) return;                              // the same section: its start (the link itself)
  let last = null;
  try { last = sessionStorage.getItem(`logos.last.${a.dataset.view}`); } catch (err) { /* none */ }
  if (last && last.startsWith(`#/${a.dataset.view}/`) && last !== a.getAttribute('href')) { e.preventDefault(); location.hash = last; }
});

// Phone (Greg, 21 Nov; 22 Nov: smoother): swipe between Home, Agents and Notifications, the first three tabs.
// (Not on to the Calendar, the fourth: there a swipe moves a month or a week.)
// The screen follows the finger one to one (and only stretches a little past the first and last tab);
// let go past about a third of the width, or with a flick, and it carries on off the screen while the
// next slides in from the side you swiped towards; otherwise it springs back. The tab bar's highlight
// follows the finger. Settings › Animations › Off (or the phone's reduced motion) stops the movement; Agent
// movement is only about the agents' marks. Inside an agent the swipe right goes back to all agents (chat-mx), not caught here.
const SWIPE_TABS = ['#/home', '#/chat', '#/noticeboard'];
function swipeTabs(el) {
  let s = null, leaving = false;
  const here = () => {
    const [, name = 'home', ...rest] = location.hash.split('?')[0].split('/');
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

// Pull down to refresh (Greg, 23 Nov). On a phone, Chrome's own pull-to-refresh reloads the whole app and draws
// its spinner from the very top of the screen, over the top bar; it is switched off in CSS and this replaces it:
// at the top of a screen, pulling down brings a round spinner out from *under* the top bar; let go past
// the line and it spins while that screen asks for fresh data (state.refresh, else the screen is drawn again),
// then tucks away. Not in a chat (pulling down there scrolls back through the messages), not while something
// is being arranged, typed or slid.
const pull = (() => {
  const dot = h('div', { class: 'ptr', 'aria-hidden': 'true' }, h('div', { class: 'ptr-dot' }, icon(ICONS.refresh)));
  document.body.append(dot);
  const TRIGGER = 56, HOLD = 48, MAX = 88;
  const phone = () => window.matchMedia('(max-width: 599px)').matches;
  let s = null, running = false, buzzed = false;
  const show = (px, spin = false, settle = false) => {
    dot.style.setProperty('--pull', `${px}px`);
    dot.style.setProperty('--turn', `${px * 4}deg`);
    dot.classList.toggle('on', px > 0 || spin);
    dot.classList.toggle('spinning', spin);
    dot.classList.toggle('settling', settle);
    // The screen moves down with the finger, so the spinner has its own gap under the top bar.
    root.style.transition = settle ? 'transform .26s cubic-bezier(.2, .9, .3, 1)' : 'none';
    root.style.transform = px > 0 ? `translateY(${px}px)` : '';
    if (!px && settle) setTimeout(() => { root.style.transition = ''; }, 300);
  };
  const skip = (t) => !phone() || running || window.scrollY > 0 || document.documentElement.classList.contains('in-agent')
    || document.documentElement.classList.contains('in-conv') || document.querySelector('.sheet:not([hidden]), .ask-layer, .home-widgets.editing')
    || (t.closest && t.closest('textarea, input, select, [contenteditable], .topbar, .tabbar, .widget-handle, .home-light, .home-lights-sec, .table-box, pre, .mx-chips'));
  document.addEventListener('touchstart', (e) => {
    s = null;
    if (e.touches.length !== 1 || skip(e.target)) return;
    s = { x: e.touches[0].clientX, y: e.touches[0].clientY, lock: null, px: 0 };
    buzzed = false;
  }, { passive: true });
  document.addEventListener('touchmove', (e) => {
    if (!s || e.touches.length !== 1) return;
    const dx = e.touches[0].clientX - s.x, dy = e.touches[0].clientY - s.y;
    if (!s.lock) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      s.lock = dy > 0 && dy > Math.abs(dx) * 1.2 && window.scrollY <= 0 ? 'down' : 'no';
    }
    if (s.lock !== 'down') return;
    s.px = Math.min(MAX, Math.max(0, dy) * 0.55);          // the finger moves further than the spinner (it resists)
    if (!buzzed && s.px >= TRIGGER) { buzzed = true; try { navigator.vibrate && navigator.vibrate(8); } catch (err) { /* fine */ } }
    if (buzzed && s.px < TRIGGER) buzzed = false;
    show(s.px);
  }, { passive: true });
  const end = async () => {
    if (!s) return;
    const { lock, px } = s; s = null;
    if (lock !== 'down') return;
    if (px < TRIGGER) { show(0, false, true); return; }
    running = true;
    show(HOLD, true, true);
    const started = Date.now();
    try { await (state.refresh ? state.refresh() : route()); } catch (err) { /* the screen shows its own trouble */ }
    await new Promise((r) => setTimeout(r, Math.max(0, 700 - (Date.now() - started))));   // long enough to be seen
    show(0, false, true);
    running = false;
  };
  document.addEventListener('touchend', end, { passive: true });
  document.addEventListener('touchcancel', () => { if (s) { s = null; show(0, false, true); } }, { passive: true });
  return { show };
})();
route();
