// Looks (L3): Calm glass, Terminal HUD and Warm & personal, each person's own choice, kept with their
// login (PUT /api/v2/me/look) and remembered on this device so the page opens in it with no flash.
// A look only restyles the same screens: nothing is reachable in one look and not another.
import { h, api } from './util.js';

export const LOOKS = [
  { id: 'glass', name: 'Calm glass', feel: 'Dark, translucent panels with a soft glow', modes: ['dark', 'light', 'auto'],
    swatch: ['#0a0e16', '#5562e6', '#e8ecf4'] },
  { id: 'hud', name: 'Terminal HUD', feel: 'Mission control: monospace, live numbers', modes: ['dark'],
    swatch: ['#05080a', '#7df9c5', '#ffb547'] },
  { id: 'warm', name: 'Warm & personal', feel: 'Cream and rounded, a colour each', modes: ['dark', 'light', 'auto'],
    swatch: ['#f6f1ea', '#2e5bd8', '#e39b2d'] },
];
const MODE_NAMES = { dark: 'Dark', light: 'Light', auto: 'Follow this device' };
const root = document.documentElement;
const prefersLight = window.matchMedia('(prefers-color-scheme: light)');

export function current() {
  return { look: root.dataset.look || 'glass', mode: root.dataset.mode || 'dark' };
}

function paintBars() {
  // The phone's own bar follows the look (Add to Home screen, Android's address bar).
  const bar = getComputedStyle(root).getPropertyValue('--bar').trim() || '#171a21';
  for (const m of document.querySelectorAll('meta[name="theme-color"]')) m.content = bar;
}

export function apply({ look, mode }) {
  const def = LOOKS.find((l) => l.id === look) || LOOKS[0];
  const m = def.modes.includes(mode) ? mode : def.modes[0];
  root.dataset.look = def.id;
  root.dataset.mode = m;
  root.dataset.theme = m === 'auto' ? (prefersLight.matches ? 'light' : 'dark') : m;
  try { localStorage.setItem('logos-look', def.id); localStorage.setItem('logos-mode', m); } catch (e) { /* private window */ }
  paintBars();
}
prefersLight.addEventListener('change', () => { if (root.dataset.mode === 'auto') apply(current()); });

async function save(choice) {
  apply(choice);
  const r = await api('/api/v2/me/look', { method: 'PUT', body: current() }).catch(() => null);
  return !!(r && r.ok);
}

let dialog = null;
export function openAppearance() {
  if (dialog) dialog.remove();
  const note = h('p', { class: 'when appearance-note', role: 'status' });
  const modeBox = h('div', { class: 'appearance-modes', role: 'radiogroup', 'aria-label': 'Light or dark' });
  const paintModes = () => {
    const { look, mode } = current();
    const def = LOOKS.find((l) => l.id === look);
    modeBox.replaceChildren(...Object.keys(MODE_NAMES).map((m) => {
      const allowed = def.modes.includes(m);
      return h('label', { class: `appearance-mode${allowed ? '' : ' off'}` },
        h('input', { type: 'radio', name: 'mode', value: m, checked: m === mode, disabled: !allowed,
          onchange: () => pick({ look, mode: m }) }), MODE_NAMES[m]);
    }));
    if (def.modes.length === 1) modeBox.append(h('p', { class: 'when' }, `${def.name} is dark only.`));
  };
  const pick = async (choice) => {
    paintModes();
    note.textContent = (await save(choice)) ? 'Saved: it follows you to your other devices.' : 'Used here, but could not be saved just now.';
    paintModes();
    for (const i of looksBox.querySelectorAll('input')) i.checked = i.value === current().look;
  };
  const looksBox = h('div', { class: 'appearance-looks', role: 'radiogroup', 'aria-label': 'Look' },
    LOOKS.map((l) => h('label', { class: `look-option look-${l.id}` },
      h('input', { type: 'radio', name: 'look', value: l.id, checked: l.id === current().look,
        onchange: () => pick({ look: l.id, mode: l.modes.includes(current().mode) ? current().mode : l.modes[0] }) }),
      h('span', { class: 'look-swatch', 'aria-hidden': 'true' }, l.swatch.map((c) => h('span', { style: `background:${c}` }))),
      h('span', { class: 'look-text' }, h('strong', {}, l.name), h('span', { class: 'when' }, l.feel)))));
  dialog = h('dialog', { class: 'appearance', 'aria-labelledby': 'appearance-title' },
    h('div', { class: 'appearance-head' },
      h('h2', { id: 'appearance-title' }, 'Appearance'),
      h('button', { type: 'button', class: 'btn quiet small', onclick: () => dialog.close() }, 'Done')),
    h('h3', { class: 'label' }, 'Look'), looksBox,
    h('h3', { class: 'label' }, 'Light or dark'), modeBox, note);
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });   // a tap outside closes it
  document.body.append(dialog);
  paintModes();
  dialog.showModal();
}

// After sign-in: the look saved with the login wins over this device's memory of it.
export function adopt(me) {
  if (me && me.look) apply(me.look);
  else paintBars();
  if (me && ['greg', 'bere'].includes(me.username)) root.dataset.person = me.username;
}
