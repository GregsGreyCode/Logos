// Homelab widgets on Home (F42; design: https://claude.ai/artifact/DJ7eLVTkisqY2p7rWggQKs). The server
// (gateway/widgets.py) says which kinds there are and fetches their numbers; this draws them, in the
// same five states for every kind, and asks for their setup. A new kind needs only its DRAW entry.
import { swr, forget } from './cache.js';
import { h, api, when } from './util.js';

const SECOND = 1000;

// ── drawing each kind ────────────────────────────────────────────────────────────────────────

const ago = (ts) => {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 60 ? `${m} min` : m < 48 * 60 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} days`;
};
const ms = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${n} ms`);
const level = (pct) => (pct === null || pct === undefined ? '' : pct >= 95 ? 'bad' : pct >= 85 ? 'warn' : 'fine');
const uptime = (s) => (s >= 2 * 86400 ? `up ${Math.floor(s / 86400)} days` : s >= 3600 ? `up ${Math.floor(s / 3600)} h` : s ? 'just started' : '');

function services(d, expand) {
  const SHOWN = 6;
  const items = d.items || [];
  const row = (i) => h('a', { class: `w-row${i.up ? (i.slow ? ' slow' : '') : ' down'}`, href: i.url, target: '_blank', rel: 'noopener' },
    h('span', { class: 'w-dot', 'aria-hidden': 'true' }),
    h('span', { class: 'w-name' }, i.name),
    h('span', { class: 'w-meta' }, !i.up ? `${i.detail || 'down'}${i.since ? ` · ${ago(i.since)}` : ''}`
      : i.slow ? `slow · ${ms(i.ms)}` : ms(i.ms)));
  const list = h('div', { class: 'w-rows' }, ...items.slice(0, SHOWN).map(row));
  if (items.length <= SHOWN) return [list];
  const more = h('button', { type: 'button', class: 'linkish small' }, `All ${items.length}`);
  more.addEventListener('click', () => { list.replaceChildren(...items.map(row)); more.remove(); expand(); });
  return [list, h('p', { class: 'when' }, `${SHOWN} of ${items.length} shown · problems first · `, more)];
}

function servers(d) {
  // words: [the number, its smaller unit], e.g. ['41', ' of 64 GB'] (the design's Memory reading).
  const bar = (label, pct, words) => h('div', { class: `w-meter ${level(pct)}` },
    h('span', { class: 'when' }, label),
    h('span', { class: 'w-num' }, ...(words ? [words[0], h('small', {}, words[1])] : [pct === null || pct === undefined ? '–' : `${pct}%`])),
    h('div', { class: 'w-bar', role: 'img', 'aria-label': `${label} ${pct ?? '–'}%` }, h('span', { style: `width:${Math.min(100, pct || 0)}%` })));
  return (d.items || []).map((n) => (n.online
    ? h('div', { class: 'w-card' },
      h('div', { class: 'w-head' }, h('strong', {}, n.name), h('span', { class: 'when' }, uptime(n.uptime))),
      h('div', { class: 'w-meters' },
        bar('CPU', n.cpu),
        bar('Memory', n.mem, n.mem_total_gb ? [`${Math.round(n.mem_gb)}`, ` of ${n.mem_total_gb} GB`] : null),
        bar('Disk', n.disk)))
    : h('div', { class: 'w-card off' }, h('div', { class: 'w-head' }, h('strong', {}, n.name), h('span', { class: 'when bad' }, 'offline')))));
}

const DRAW = { services, servers };

// ── one widget on Home ───────────────────────────────────────────────────────────────────────

// kind: from GET /api/v2/widgets. setUp: opens the setup sheet (null when this person cannot).
export function widget(kind, { onChange } = {}) {
  const summary = h('span', { class: 'when w-summary' });
  const body = h('div', { class: 'w-body' }, h('div', { class: 'w-skeleton', 'aria-hidden': 'true' }, h('span'), h('span'), h('span')));
  const el = h('section', { class: `home-widget-kind w-${kind.id}`, 'aria-label': kind.name },
    h('div', { class: 'w-top' }, h('h2', { class: 'label' }, kind.name), summary), body);
  const url = `/api/v2/widgets/${kind.id}`;

  function setupCard() {
    summary.textContent = '';
    el.classList.remove('needs', 'stale');
    body.replaceChildren(h('div', { class: 'w-card setup' },
      h('p', {}, kind.about || `${kind.name} is not set up yet.`),
      kind.can_set_up ? h('button', { type: 'button', class: 'btn quiet', onclick: () => setupSheet(kind, () => { forget(url); show(); onChange?.(); }) }, 'Set it up')
        : h('p', { class: 'when' }, 'Whoever manages Logos can set it up.')));
  }

  function draw(d) {
    if (!d || d.state === 'setup') { setupCard(); return; }
    el.classList.toggle('needs', d.state === 'needs');
    el.classList.toggle('stale', !!d.stale || d.state === 'unreachable');
    if (d.state === 'unreachable') {
      summary.textContent = '';
      body.replaceChildren(h('div', { class: 'w-card' }, h('p', {}, d.error || 'It could not be asked just now.'),
        h('p', { class: 'when' }, 'Trying again in a minute.'),
        kind.can_set_up ? h('button', { type: 'button', class: 'linkish small', onclick: () => setupSheet(kind, () => { forget(url); show(); }) }, 'Check its setup') : ''));
      return;
    }
    summary.textContent = d.stale ? `as of ${when(d.at)}` : d.summary || '';
    const drawn = (DRAW[kind.id] || (() => []))(d, () => {});
    body.replaceChildren(...drawn,
      d.stale ? h('p', { class: 'when w-stale' }, `${d.error} · trying again`) : '');
  }

  // The last copy at once, then fresh numbers (kept as long as the kind refreshes).
  const show = () => swr(url, (kind.refresh || 60) * SECOND, draw, { state: 'unreachable' });
  if (!kind.set_up) setupCard();
  return { el, show };
}

// ── the setup sheet (whoever manages Logos) ──────────────────────────────────────────────────

export function setupSheet(kind, done) {
  const inputs = {};
  const msg = h('p', { class: 'when', role: 'status' });
  const fields = (kind.fields || []).map((f) => {
    const id = `w-${kind.id}-${f.key}`;
    let input;
    if (f.kind === 'check') {
      input = h('input', { id, type: 'checkbox', checked: !!f.value });
      inputs[f.key] = () => input.checked;
      return h('label', { class: 'appearance-mode choice' }, input,
        h('span', {}, h('strong', {}, f.label), f.help ? h('span', { class: 'when block' }, f.help) : ''));
    }
    input = f.kind === 'lines'
      ? h('textarea', { id, rows: 5, placeholder: f.placeholder || '', spellcheck: 'false' }, f.value || '')
      : h('input', { id, type: f.kind === 'secret' ? 'password' : f.kind === 'url' ? 'url' : 'text', autocomplete: 'off', spellcheck: 'false',
        placeholder: f.kind === 'secret' && f.set ? 'Saved: leave blank to keep it' : (f.placeholder || ''), value: f.kind === 'secret' ? '' : (f.value || '') });
    inputs[f.key] = () => input.value;
    return h('div', { class: 'w-field' }, h('label', { for: id, class: 'field-label' }, f.label), input,
      f.help ? h('p', { class: 'when' }, f.help) : '');
  });
  const values = () => Object.fromEntries(Object.entries(inputs).map(([k, get]) => [k, get()]));
  const layer = h('div', { class: 'ask-layer' });
  const close = () => { layer.remove(); document.removeEventListener('keydown', key); };
  const key = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', key);
  const send = async (method, path) => {
    msg.textContent = method === 'POST' ? 'Trying it…' : 'Trying it, then saving…';
    const r = await api(path, { method, body: { fields: values() } }).catch(() => null);
    const d = r ? await r.json().catch(() => ({})) : {};
    return d;
  };
  const test = h('button', { type: 'button', class: 'btn quiet', onclick: async () => {
    const d = await send('POST', `/api/v2/widgets/${kind.id}/test`);
    msg.textContent = d.ok ? `It works: ${d.summary}.` : (d.error || 'It could not be tried just now.');
  } }, 'Test');
  const remove = kind.set_up ? h('button', { type: 'button', class: 'btn quiet danger', onclick: async () => {
    await api(`/api/v2/widgets/${kind.id}`, { method: 'DELETE' }).catch(() => null);
    kind.set_up = false; close(); done?.();
  } }, 'Remove its setup') : '';
  layer.append(h('div', { class: 'ask-scrim', onclick: close }),
    h('form', { class: 'ask-panel w-setup', role: 'dialog', 'aria-modal': 'true', 'aria-label': `Set up ${kind.name}`, onsubmit: async (e) => {
      e.preventDefault();
      const d = await send('PUT', `/api/v2/widgets/${kind.id}`);
      if (!d.ok) { msg.textContent = d.error || 'It could not be saved just now.'; return; }
      kind.set_up = true; close(); done?.();
    } },
    h('h2', {}, `Set up ${kind.name}`),
    kind.about ? h('p', { class: 'when' }, kind.about) : '',
    ...fields,
    h('p', { class: 'when' }, 'Kept on the Logos server only: never in a browser, never with an agent.'),
    msg,
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Save'), test,
      h('button', { type: 'button', class: 'btn quiet', onclick: close }, 'Cancel'), remove)));
  document.body.append(layer);
  layer.querySelector('input, textarea')?.focus();
}
