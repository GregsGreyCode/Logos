// Homelab widgets on Home (F42; design: https://claude.ai/artifact/DJ7eLVTkisqY2p7rWggQKs). The server
// (gateway/widgets.py) says which kinds there are and fetches their numbers; this draws them, in the
// same five states for every kind, and asks for their setup. A new kind needs only its DRAW entry.
import { swr, forget } from './cache.js';
import { h, api, when } from './util.js';
import { agentColour } from './appearance.js';

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
    ? h('div', { class: `w-card${(n.disk || 0) >= 95 || (n.mem || 0) >= 97 ? ' alert' : ''}` },
      h('div', { class: 'w-head' }, h('strong', {}, n.name), h('span', { class: 'when' }, uptime(n.uptime))),
      h('div', { class: 'w-meters' },
        bar('CPU', n.cpu),
        bar('Memory', n.mem, n.mem_total_gb ? [`${Math.round(n.mem_gb)}`, ` of ${n.mem_total_gb} GB`] : null),
        bar('Disk', n.disk)))
    : h('div', { class: 'w-card off alert' }, h('div', { class: 'w-head' }, h('strong', {}, n.name), h('span', { class: 'when bad' }, 'offline')))));
}

function proxmox(d) {
  return [h('div', { class: 'w-grid2' }, ...(d.items || []).map((g) => h('div', { class: `w-card w-guest${g.running ? '' : ' off'}` },
    h('div', { class: 'w-head' }, h('strong', {}, `${g.id} ${g.name}`), h('span', { class: `w-dot${g.running ? '' : ' idle'}`, 'aria-label': g.running ? 'running' : 'stopped' })),
    h('span', { class: 'when' }, g.running ? `CPU ${g.cpu}% · ${g.mem_gb} GB` : `stopped${g.kind === 'container' ? ' · container' : ''}`))))];
}

function backups(d) {
  return [h('div', { class: 'w-rows' }, ...(d.items || []).map((b) => h('div', { class: `w-row${b.ok ? '' : ' down'}` },
    h('span', { class: 'w-dot', 'aria-hidden': 'true' }), h('span', { class: 'w-name' }, b.name),
    h('span', { class: 'w-meta' }, `${b.ran ? when(b.ran) : ''}${b.detail ? `${b.ran ? ' · ' : ''}${b.detail}` : ''}`))))];
}

function adblock(d) {
  const r = 15, c = 2 * Math.PI * r, part = (c * (d.percent || 0)) / 100;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 36 36'); svg.setAttribute('class', 'w-ring'); svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${d.percent}% blocked`);
  for (const [cls, dash] of [['w-ring-bg', ''], ['w-ring-fg', `${part} ${c}`]]) {
    const circle = document.createElementNS(ns, 'circle');
    for (const [k, v] of Object.entries({ cx: 18, cy: 18, r, class: cls, transform: 'rotate(-90 18 18)' })) circle.setAttribute(k, v);
    if (dash) circle.setAttribute('stroke-dasharray', dash);
    svg.append(circle);
  }
  return [h('div', { class: 'w-card w-adblock-card' }, svg, h('div', { class: 'w-adblock-words' },
    h('span', { class: 'w-num' }, `${d.percent}%`),
    h('span', { class: 'when' }, `${(d.blocked || 0).toLocaleString()} of ${(d.total || 0).toLocaleString()} blocked today`),
    (d.top || []).length ? h('span', { class: 'when' }, `Top: ${d.top.join(', ')}`) : ''))];
}

const hm = (s) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}` : `${Math.floor(s / 60)} min`);
function playing(d) {
  if (!(d.items || []).length) return [h('p', { class: 'when' }, 'Nothing is playing.')];
  return (d.items || []).map((p) => h('div', { class: `w-card w-play${p.paused ? ' paused' : ''}` },
    h('span', { class: 'w-poster', 'aria-hidden': 'true' }),
    h('div', { class: 'w-play-words' }, h('strong', {}, p.title),
      h('span', { class: 'when' }, [p.where, p.who].filter(Boolean).join(' · ') + (p.paused ? ' · paused' : '')),
      p.percent !== null && p.percent !== undefined ? h('div', { class: 'w-bar' }, h('span', { style: `width:${p.percent}%` })) : '',
      p.length ? h('span', { class: 'when' }, `${hm(p.position)} of ${hm(p.length)}`) : '')));
}

// Sonarr's "01:04:30" (or "1.02:00:00" for a day and more) as "1 h 04", "4 min", "1 day".
function left(t) {
  const m = String(t || '').match(/^(?:(\d+)\.)?(\d+):(\d+):(\d+)$/);
  if (!m) return '';
  const [d, hh, mm] = [+(m[1] || 0), +m[2], +m[3]];
  return d ? `${d} day${d === 1 ? '' : 's'}` : hh ? `${hh} h ${String(mm).padStart(2, '0')}` : `${Math.max(1, mm)} min`;
}

function downloads(d) {
  const items = d.items || [];
  return [h('div', { class: `w-card${items.some((i) => i.stalled) ? ' alert' : ''}` }, ...(items.length ? items.slice(0, 6).map((i) => h('div', { class: `w-dl${i.stalled ? ' stalled' : ''}` },
    h('div', { class: 'w-head' }, h('span', { class: 'w-name' }, i.title),
      h('span', { class: 'w-meta' }, i.stalled ? 'stalled' : i.waiting ? 'waiting' : `${i.percent}%${left(i.left) ? ` · ${left(i.left)} left` : ''}`)),
    i.stalled ? '' : h('div', { class: 'w-bar' }, h('span', { style: `width:${i.percent}%` }))))
    : [h('p', { class: 'when' }, 'Nothing downloading.')])),
  d.upcoming ? h('p', { class: 'when' }, `Next up this week: ${d.upcoming} episode${d.upcoming === 1 ? '' : 's'}.`) : ''];
}

function bookmarks(d) {
  const initials = (n) => n.split(/\s+/).filter(Boolean).map((x) => x[0]).join('').slice(0, 2) || '·';
  return [h('div', { class: 'w-marks' }, ...(d.items || []).map((b) => h('a', { class: 'w-mark', href: b.url, target: '_blank', rel: 'noopener' },
    h('span', { class: 'w-mark-tile', 'aria-hidden': 'true' }, initials(b.name)), h('span', { class: 'w-mark-name' }, b.name))))];
}

const figure = (label, value, unit, sub) => h('div', { class: 'w-card w-fig' }, h('span', { class: 'when' }, label),
  h('span', { class: 'w-num' }, value ?? '–', unit && value !== null && value !== undefined ? h('small', {}, ` ${unit}`) : ''),
  sub ? h('span', { class: 'when' }, sub) : '');

function energy(d) {
  const out = [h('div', { class: 'w-grid2' },
    figure('Using now', d.power_w, 'W'),
    figure('Today', d.energy_kwh, 'kWh', d.cost !== null && d.cost !== undefined ? `about £${d.cost.toFixed(2)}` : ''))];
  if (d.ups) {
    const b = d.ups.battery;
    out.push(h('div', { class: `w-card w-ups${d.ups.on_battery ? ' alert' : ''}` },
      h('div', { class: 'w-head' }, h('strong', {}, 'UPS'), h('span', { class: `w-pill${d.ups.on_battery ? ' bad' : ''}` }, d.ups.on_battery ? 'On battery' : 'On mains')),
      b !== null && b !== undefined ? h('div', { class: `w-bar ${level(100 - b)}` }, h('span', { style: `width:${b}%` })) : '',
      h('span', { class: 'when' }, [b !== null && b !== undefined ? `Battery ${Math.round(b)}%` : '', d.ups.minutes !== null && d.ups.minutes !== undefined ? `about ${d.ups.minutes} min on battery` : '']
        .filter(Boolean).join(' · '))));
  }
  return out;
}

function network(d) {
  return [h('div', { class: 'w-grid3' }, figure('Down', d.down, 'Mbit/s'), figure('Up', d.up, 'Mbit/s'), figure('Ping', d.ping, 'ms')),
    d.tested ? h('p', { class: 'when' }, `Tested ${String(d.tested).slice(11, 16) || d.tested}`) : ''];
}

function cameras(d) {
  const t = Math.floor(Date.now() / 60000);           // a new picture each minute, not on every redraw
  return [h('div', { class: 'w-grid2' }, ...(d.items || []).map((c) => {
    const words = `${c.name.replace(/_/g, ' ')} · ${c.label && c.at ? `${c.label} ${when(c.at)}` : 'quiet'}`;
    return h('a', { class: 'w-cam', href: '#/home', 'aria-label': words, onclick: (e) => e.preventDefault() },
      h('img', { src: `/api/v2/widgets/cameras/snapshot/${encodeURIComponent(c.name)}?t=${t}`, alt: '', loading: 'lazy' }),
      h('span', { class: 'w-cam-label' }, words));
  })), h('p', { class: 'when' }, 'Snapshots, not live video, through Logos.')];
}

function agentsToday(d) {
  const rows = d.items || [];
  if (!rows.length) return [h('p', { class: 'when' }, 'No agents yet.')];
  const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const words = (r) => (!r.ready ? 'Not running'
    : r.messages ? `${r.messages} message${r.messages === 1 ? '' : 's'} today · last ${hhmm(r.last)}`
      : r.last ? `Quiet today · last ${when(r.last)}` : 'Quiet today');
  return [h('div', { class: 'w-card w-agents' }, ...rows.map((r) => {
    const colour = agentColour(r.agent);
    return h('div', { class: `w-agent${r.ready ? '' : ' idle'}` },
      h('span', { class: 'w-mark', style: colour ? `background:color-mix(in srgb, ${colour} 22%, transparent);color:${colour}` : '', 'aria-hidden': 'true' },
        (r.name || '?').trim().charAt(0).toUpperCase()),
      h('div', { class: 'w-agent-words' }, h('strong', {}, r.name), h('span', { class: 'when' }, words(r))));
  })), h('p', { class: 'when' }, 'Counts only: never what was said.')];
}

const DRAW = { agents: agentsToday, services, servers, proxmox, backups, adblock, playing, downloads, bookmarks, energy, network, cameras };

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
    if (f.kind === 'choice') {
      const name = `w-${kind.id}-${f.key}`;
      const group = h('div', { class: 'appearance-modes', role: 'radiogroup', 'aria-label': f.label },
        ...(f.options || []).map(([v, label], i) => h('label', { class: 'appearance-mode' },
          h('input', { type: 'radio', name, value: v, checked: f.value ? f.value === v : i === 0 }), label)));
      inputs[f.key] = () => (group.querySelector('input:checked') || {}).value || '';
      return h('div', { class: 'w-field' }, h('span', { class: 'field-label' }, f.label), group);
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
    kind.uses ? h('p', { class: 'when' }, `This is the ${kind.uses[0].toUpperCase()}${kind.uses.slice(1)} setup: changing it here changes it there too.`) : '',
    ...fields,
    h('p', { class: 'when' }, 'Kept on the Logos server only: never in a browser, never with an agent.'),
    msg,
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Save'), test,
      h('button', { type: 'button', class: 'btn quiet', onclick: close }, 'Cancel'), remove)));
  document.body.append(layer);
  layer.querySelector('input, textarea')?.focus();
}
