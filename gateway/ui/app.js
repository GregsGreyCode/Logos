// Logos dashboard shell: sign-in handling, theme, and a tiny hash router.
// No build step: plain ES modules served from /ui/.
import { chatView } from './chat.js';
import { brainView } from './brain.js';
import { agentsView } from './agents.js';
import { peopleView } from './people.js';
import { modelsView } from './models.js';
import { noticeboardView, setBadge } from './noticeboard.js';
import { h, api, getJSON } from './util.js';

const views = { chat: chatView, brain: brainView, noticeboard: noticeboardView, agents: agentsView,
  models: modelsView, people: peopleView };
const root = document.getElementById('view');
let cleanup = null;

function soon(name) {
  root.replaceChildren(h('div', { class: 'empty' },
    h('h1', {}, name[0].toUpperCase() + name.slice(1)),
    h('p', {}, 'This screen is designed but not built yet. The old dashboard still has what existed before.')));
}

let visit = 0;
async function route() {
  const [, name = 'chat', ...rest] = location.hash.split('/');
  const mine = ++visit;
  if (cleanup) { cleanup(); cleanup = null; }
  for (const a of document.querySelectorAll('.nav-links a')) {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
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
  document.getElementById('theme').textContent = light ? 'Switch to dark' : 'Switch to light';
}

document.getElementById('theme').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('logos-theme', next); } catch (e) { /* private window: not remembered */ }
  paintTheme();
});
document.getElementById('logout').addEventListener('click', async () => {
  await api('/auth/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
});

paintTheme();
state.me = await getJSON('/api/v2/me');
document.getElementById('who').textContent = `Signed in as ${state.me.display_name || state.me.username}`;
// Keep the "waiting on you" count in the menu current on every screen.
const badge = () => getJSON('/api/v2/noticeboard').then((d) => setBadge(d.waiting.length)).catch(() => {});
badge();
setInterval(badge, 60000);
window.addEventListener('hashchange', route);
route();
