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
const MODE_NAMES = { dark: 'Dark', light: 'Light', auto: 'Auto' };   // Auto: follows the device
const root = document.documentElement;
const prefersLight = window.matchMedia('(prefers-color-scheme: light)');

// ── ST2: your colours, kept readable ───────────────────────────────────────────────────────────
// Whatever is picked, text made from it passes WCAG AA (4.5:1) against the ground of the look in use:
// the colour is nudged lighter or darker until it does, so it can look a shade off what was picked.
export const SWATCHES = {
  primary: ['#5562e6', '#2f9e8f', '#d0508a', '#8b5cf6', '#e0743a', '#3b82c4'],
  attention: ['#ffc466', '#ff8a6b', '#f472b6', '#facc15', '#5be3a6', '#7dd3fc'],
  agent: ['#7c8cff', '#ffb066', '#5be3a6', '#f472b6', '#c084fc', '#7dd3fc'],
};
let mine = { primary: '', attention: '' };
let agentColours = {};
const hexRgb = (hex) => { const n = parseInt(hex.slice(1), 16); return [n >> 16, (n >> 8) & 255, n & 255]; };
const rgbHex = (c) => '#' + c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')).join('');
const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const mix = (c, to, t) => c.map((v, i) => v + (to[i] - v) * t);
const rgba = (c, a) => `rgba(${c.map(Math.round).join(', ')}, ${a})`;
function readable(c, bg, need = 4.5) {
  const to = lum(bg) < 0.4 ? [255, 255, 255] : [0, 0, 0];
  for (let t = 0; t <= 1; t += 0.05) { const m = mix(c, to, t); if (ratio(m, bg) >= need) return m; }
  return to;
}
function onIt(c) {                       // a filled colour and the text that goes on it
  const white = [255, 255, 255], dark = [11, 13, 18];
  if (ratio(c, white) >= 4.5) return [c, white];
  if (ratio(c, dark) >= 4.5) return [c, dark];
  return [readable(c, white), white];
}
function groundRgb() {
  const probe = document.createElement('span');
  probe.style.color = getComputedStyle(root).getPropertyValue('--ground').trim() || '#0f1115';
  document.body.append(probe);
  const m = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
  probe.remove();
  return m;
}
const OWN = ['--accent', '--accent-ink', '--accent-soft', '--accent-text', '--glow-1', '--warn-ink', '--warn-bg', '--attn', '--attn-ink'];
function paintColours() {
  if (!document.body) return;
  for (const p of OWN) root.style.removeProperty(p);           // the look's own first, to measure its ground
  const g = groundRgb(), dark = lum(g) < 0.4;
  if (mine.primary) {
    const c = hexRgb(mine.primary), [fill, ink] = onIt(c);
    root.style.setProperty('--accent', rgbHex(fill));
    root.style.setProperty('--accent-ink', rgbHex(ink));
    root.style.setProperty('--accent-soft', rgba(c, dark ? 0.16 : 0.1));
    root.style.setProperty('--accent-text', rgbHex(readable(c, g)));
    root.style.setProperty('--glow-1', rgba(c, dark ? 0.34 : 0.18));
  }
  if (mine.attention) {
    const c = hexRgb(mine.attention), [fill, ink] = onIt(c);
    root.style.setProperty('--warn-ink', rgbHex(readable(c, g)));
    root.style.setProperty('--warn-bg', rgba(c, dark ? 0.14 : 0.18));
    root.style.setProperty('--attn', rgbHex(fill));
    root.style.setProperty('--attn-ink', rgbHex(ink));
  }
  // Each agent's colour, the same for everyone: its dot now, its floating mark later.
  let sheet = document.getElementById('agent-colours');
  if (!sheet) { sheet = document.createElement('style'); sheet.id = 'agent-colours'; document.head.append(sheet); }
  sheet.textContent = Object.entries(agentColours).filter(([n, c]) => /^[a-z0-9_-]+$/.test(n) && /^#[0-9a-f]{6}$/i.test(c))
    .map(([n, c]) => `:root { --agent-${n}: ${c}; } .dot.${n} { background: ${c}; }`).join('\n');
}
export function setMine(colours) { mine = { ...mine, ...colours }; paintColours(); }
export function setAgentColour(name, colour) { agentColours = { ...agentColours, [name]: colour }; paintColours(); }
export const myColours = () => ({ ...mine });
export const agentColour = (name) => agentColours[name] || '';
export function setMotion(motion, names) {
  if (motion) root.dataset.motion = motion;
  if (names) root.dataset.names = names;
}

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
  paintColours();
  paintBars();
}
prefersLight.addEventListener('change', () => { if (root.dataset.mode === 'auto') apply(current()); });

async function save(choice) {
  apply(choice);
  const r = await api('/api/v2/me/look', { method: 'PUT', body: current() }).catch(() => null);
  return !!(r && r.ok);
}

// The look and light-or-dark controls, shown in Settings > Appearance.
export function lookControls() {
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
    // A look with one mode (Terminal HUD is dark only) hides the whole Light-or-dark section.
    modeBox.classList.toggle('single', def.modes.length === 1);
  };
  const pick = async (choice) => {
    apply(choice);
    paintModes();
    for (const i of looksBox.querySelectorAll('input')) i.checked = i.value === current().look;
    note.textContent = (await save(choice)) ? 'Saved: it follows you to your other devices.' : 'Used here, but could not be saved just now.';
  };
  const looksBox = h('div', { class: 'appearance-looks', role: 'radiogroup', 'aria-label': 'Look' },
    LOOKS.map((l) => h('label', { class: `look-option look-${l.id}` },
      h('input', { type: 'radio', name: 'look', value: l.id, checked: l.id === current().look,
        onchange: () => pick({ look: l.id, mode: l.modes.includes(current().mode) ? current().mode : l.modes[0] }) }),
      h('span', { class: 'look-swatch', 'aria-hidden': 'true' }, l.swatch.map((c) => h('span', { style: `background:${c}` }))),
      h('span', { class: 'look-text' }, h('strong', {}, l.name), h('span', { class: 'when' }, l.feel)))));
  paintModes();
  return { looksBox, modeBox, note };
}

// After sign-in: the look saved with the login wins over this device's memory of it.
export function adopt(me) {
  const p = (me && me.prefs) || {};
  mine = { primary: p.primary || '', attention: p.attention || '' };
  agentColours = { ...((me && me.agent_colours) || {}) };
  setMotion(p.motion || 'calm', p.names || 'hover');
  if (me && me.look) apply(me.look);
  else { paintColours(); paintBars(); }
  if (me && ['greg', 'bere'].includes(me.username)) root.dataset.person = me.username;
}
