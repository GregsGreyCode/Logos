// Models screen: is everything that answers up, who answers each agent right now, and what
// the paid fallbacks have cost this month. All figures come from the model gateway's records.
import { h, getJSON, api, none, loading } from './util.js';

const NS = 'http://www.w3.org/2000/svg';
const svg = (tag, attrs = {}, ...kids) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  el.append(...kids.flat().filter((k) => k != null));
  return el;
};

const usd = (n) => (n > 0 && n < 0.01 ? '< $0.01' : `$${Number(n || 0).toFixed(2)}`);
const count = (n) => Number(n || 0).toLocaleString();
const dayLabel = (iso) => new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { day: 'numeric', month: 'short' });

function ago(iso) {
  if (!iso) return '';
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 2) return 'just now';
  if (mins < 60) return `${mins} minutes ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} hours ago`;
  return `${Math.round(mins / 1440)} days ago`;
}

// Clean axis steps: 0 .. top in four steps of 1, 2, 2.5 or 5 times a power of ten.
function axis(max, money) {
  if (max <= 0) return { top: money ? 0.01 : 4, ticks: money ? [0, 0.01] : [0, 2, 4] };
  const raw = max / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= raw);
  const whole = money ? step : Math.max(1, Math.ceil(step));
  const ticks = [];
  for (let v = 0; v < max + whole - 1e-12 && ticks.length < 6; v += whole) ticks.push(v);
  return { top: ticks[ticks.length - 1], ticks };
}

// A column chart of one value per day. `series` is [{ key, label, cls }]; more than one stacks.
// Hovering or focusing a day shows its figures; the same numbers are in the table underneath.
function columns(days, series, { money = false, label }) {
  const W = 520, H = 220, L = 44, R = 8, T = 12, B = 26;
  const totals = days.map((d) => series.reduce((s, x) => s + (d[x.key] || 0), 0));
  const { top, ticks } = axis(Math.max(...totals, 0), money);
  const slot = (W - L - R) / days.length;
  const bar = Math.min(24, Math.max(4, slot - 4));
  const y = (v) => T + (H - T - B) * (1 - v / top);
  const fmt = (v) => (money ? `$${v < 1 ? v.toFixed(2) : v.toFixed(v % 1 ? 1 : 0)}` : count(v));
  const tip = h('div', { class: 'chart-tip', role: 'status', hidden: true });
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': label, class: 'chart' });

  for (const t of ticks) {
    root.append(svg('line', { x1: L, x2: W - R, y1: y(t), y2: y(t), class: t === 0 ? 'axis' : 'grid' }),
      svg('text', { x: L - 8, y: y(t) + 4, 'text-anchor': 'end', class: 'tick' }, fmt(t)));
  }
  days.forEach((d, i) => {
    const cx = L + slot * i + slot / 2;
    if (i % 7 === 1 || i === days.length - 1) {
      root.append(svg('text', { x: cx, y: H - 8, 'text-anchor': i === days.length - 1 ? 'end' : 'middle', class: 'tick' },
        dayLabel(d.date)));
    }
    let base = 0;
    const present = series.filter((s) => (d[s.key] || 0) > 0);
    present.forEach((s, n) => {
      const v = d[s.key];
      const y0 = y(base), y1 = y(base + v);
      const gap = n > 0 ? 2 : 0;                      // surface gap between stacked segments
      const height = Math.max(1, y0 - y1 - gap);
      const topMost = n === present.length - 1;
      const r = topMost ? Math.min(4, height) : 0;    // rounded data end, square at the baseline
      const x = cx - bar / 2, yb = y0 - gap, yt = yb - height;
      root.append(svg('path', { class: `mark ${s.cls}`,
        d: `M${x},${yb} V${yt + r} Q${x},${yt} ${x + r},${yt} H${x + bar - r} Q${x + bar},${yt} ${x + bar},${yt + r} V${yb} Z` }));
      base += v;
    });
    // A hit target the full height of the plot: easier than aiming at a short bar.
    const hit = svg('rect', { x: L + slot * i, y: T, width: slot, height: H - T - B, class: 'hit', tabindex: 0,
      'aria-label': `${dayLabel(d.date)}: ${series.map((s) => `${s.label} ${fmt(d[s.key] || 0)}`).join(', ')}` });
    const show = () => {
      tip.replaceChildren(h('strong', {}, dayLabel(d.date)),
        ...series.map((s) => h('span', {}, series.length > 1 ? h('i', { class: `swatch ${s.cls}` }) : null,
          `${series.length > 1 ? `${s.label}: ` : ''}${fmt(d[s.key] || 0)}`)));
      tip.hidden = false;
      tip.style.left = `${Math.min(88, Math.max(12, (cx / W) * 100))}%`;
    };
    const hide = () => { tip.hidden = true; };
    hit.addEventListener('pointerenter', show); hit.addEventListener('pointerleave', hide);
    hit.addEventListener('focus', show); hit.addEventListener('blur', hide);
    root.append(hit);
  });
  const legend = series.length > 1 ? h('div', { class: 'legend' },
    series.map((s) => h('span', {}, h('i', { class: `swatch ${s.cls}` }), s.label))) : null;
  return h('div', { class: 'chart-box' }, legend, h('div', { class: 'chart-wrap' }, root, tip));
}

// P4 on a phone: the whole status as one line of dots; it opens to the details.
function statusLine(d) {
  const server = d.status.model_server;
  const down = d.status.services.filter((s) => !s.up), all = d.status.services.length;
  const item = (up, text) => h('span', { class: 'status-item' }, h('span', { class: `status ${up ? 'up' : 'down'}` }), text);
  const items = [
    server ? item(server.up, server.up ? 'Model server' : 'Model server off') : null,
    ...down.map((s) => item(false, `${s.name} down`)),
    down.length ? h('span', { class: 'status-item when' }, `${all - down.length} of ${all} services up`) : item(true, `All ${all} services up`)];
  return h('details', { class: 'status-line phone-only' },
    h('summary', {}, items, h('span', { class: 'status-more' }, 'Details')),
    h('p', { class: 'when' }, d.status.services.map((s) => `${s.name}: ${s.up ? 'up' : `down (${s.what})`}`).join(' · ')));
}

function statusTile(title, up, lines) {
  return h('div', { class: 'card tile' },
    h('span', { class: `status ${up ? 'up' : 'down'}` }, up ? 'Up' : 'Down'),
    h('h2', {}, title), lines.map((l) => h('p', { class: 'when' }, l)));
}

function meter(used, limit) {
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  return h('div', { class: 'meter', role: 'img', 'aria-label': `${usd(used)} of ${usd(limit)} used` },
    h('div', { class: `meter-fill ${pct >= 100 ? 'full' : ''}`, style: `width:${pct}%` }));
}

// ── The local model (LM Studio): what is loaded, checks, the queue, speed, app-only settings ──

const yes = (v) => (v === true ? 'On' : v === false ? 'Off' : '—');
const post = async (action, body) => {
  const r = await api(`/api/v2/models/local/${action}`, { method: 'POST', body });
  return { ok: r.ok, body: await r.json().catch(() => ({})) };
};

// The GPU this server has: shown, and editable by an admin (it drives the checks and the advice).
function gpuLine(srv, canChange, repaint) {
  const hw = srv.hardware || {};
  const text = hw.vram_gb ? `GPU: ${hw.gpu || 'unnamed'} · ${hw.vram_gb} GB` : 'GPU: not told yet';
  if (!canChange) return h('p', { class: 'when' }, text);
  const gpu = h('input', { type: 'text', value: hw.gpu || '', placeholder: 'e.g. RTX 4080 Super', 'aria-label': 'GPU name', maxlength: 80 });
  const vram = h('input', { type: 'number', value: hw.vram_gb || '', min: 0, max: 1024, step: 1, placeholder: 'GB', 'aria-label': 'GPU memory in GB' });
  const msg = h('span', { class: 'when', role: 'status' });
  const form = h('form', { class: 'gpu-form', hidden: !!hw.vram_gb, onsubmit: async (ev) => {
    ev.preventDefault();
    const r = await post('hardware', { server: srv.server, gpu: gpu.value, vram_gb: Number(vram.value || 0) });
    msg.textContent = r.ok ? 'Saved.' : (r.body.error || 'Could not save.');
    if (r.ok) repaint();
  } }, gpu, vram, h('button', { type: 'submit', class: 'btn ghost' }, 'Save'), msg,
  h('span', { class: 'when' }, hw.vram_gb ? '' : 'Tell Logos how much GPU memory this PC has, and the checks and advice below follow from it.'));
  const edit = h('button', { type: 'button', class: 'linkish', hidden: !hw.vram_gb, onclick: () => { form.hidden = false; edit.hidden = true; } }, 'Change');
  return h('div', {}, h('p', { class: 'when' }, text, ' ', edit), form);
}

function serverBlock(srv, canChange, repaint) {
  const parts = [h('h2', { class: 'label' }, `Local model · ${srv.server}`), gpuLine(srv, canChange, repaint)];
  if (!srv.up) {
    parts.push(h('div', { class: 'notice' }, srv.error || 'LM Studio is closed or that PC is busy.'));
    return h('section', { 'aria-label': `Local model on ${srv.server}` }, parts);
  }
  const cards = [];
  for (const m of srv.loaded) {
    const ours = srv.want[m.key];
    for (const i of m.instances) {
      const rows = [
        ['Window', `${count(i.context_length)} tokens`, ours ? `agents use up to ${count(ours.uses)} each; Logos loads ${count(ours.load)}` : ''],
        ['Parallel slots', i.parallel ?? '—', 'they share the one window'],
        ['Flash attention', yes(i.flash_attention), ''],
        ['KV cache on GPU', yes(i.offload_kv_cache_to_gpu), ''],
        ['Experts', i.num_experts ?? '—', ''],
        ['File', `${m.size_gb} GB · ${m.quantization}`, m.params],
      ];
      const unload = canChange && !ours ? h('button', { type: 'button', class: 'btn ghost', onclick: async (ev) => {
        if (!confirm(`Unload ${m.key}? No agent uses it.`)) return;
        ev.target.disabled = true;
        const r = await post('unload', { server: srv.server, instance: i.id });
        ev.target.textContent = r.ok ? 'Unloaded' : 'Could not unload';
      } }, 'Unload') : null;
      cards.push(h('div', { class: 'card' },
        h('span', { class: `status ${ours ? 'up' : 'down'}` }, ours ? `Used by ${ours.agents.join(', ')}` : 'Not used by any agent'),
        h('h2', {}, m.name),
        h('dl', { class: 'facts' }, rows.flatMap(([k, v, note]) => [h('dt', {}, k),
          h('dd', {}, String(v), note ? h('span', { class: 'when' }, ` · ${note}`) : null)])),
        unload));
    }
  }
  if (!srv.loaded.length) cards.push(h('div', { class: 'card' }, h('h2', {}, 'Nothing loaded'),
    h('p', { class: 'when' }, 'The first message loads the agents\' model, which takes about a minute.')));
  parts.push(h('div', { class: 'cards' }, cards));
  parts.push(h('ul', { class: 'checks' }, (srv.checks || []).map((c) => h('li', { class: `check ${c.level}` },
    h('span', { class: 'check-mark', 'aria-hidden': 'true' }, c.level === 'warn' ? '!' : c.level === 'ok' ? '✓' : 'i'),
    h('span', { class: 'visually-hidden' }, c.level === 'warn' ? 'Problem: ' : c.level === 'ok' ? 'Fine: ' : 'Note: '), c.text))));
  if (canChange && Object.keys(srv.want).length) {
    const status = h('p', { class: 'when', role: 'status' });
    parts.push(h('div', { class: 'actions' },
      h('button', { type: 'button', class: 'btn', onclick: async (ev) => {
        if (!confirm('Reload the agents\' model with Logos\'s settings? The agents cannot answer for about a minute.')) return;
        ev.target.disabled = true; status.textContent = 'Reloading… this takes about a minute.';
        const r = await post('reload', { server: srv.server });
        status.textContent = r.ok ? `Reloaded with a ${count(r.body.config?.context_length)}-token window.` : (r.body.error || 'Could not reload.');
        ev.target.disabled = false;
      } }, 'Reload with Logos settings'), status));
  }
  parts.push(h('details', { class: 'card' }, h('summary', {}, 'Settings only the LM Studio app can change'),
    h('p', { class: 'when' }, 'LM Studio does not let Logos set or read these. On that PC: My Models, the gear next to the model.'),
    h('table', { class: 'plain' }, h('thead', {}, h('tr', {}, ['Setting', 'Set it to', 'Why'].map((t) => h('th', {}, t)))),
      h('tbody', {}, (srv.advice || []).map((a) => h('tr', {}, h('td', {}, a.setting), h('td', {}, h('strong', {}, a.value)), h('td', {}, a.why)))))));
  return h('section', { 'aria-label': `Local model on ${srv.server}` }, parts);
}

async function localModel(repaint) {
  let d;
  try { d = await getJSON('/api/v2/models/local'); } catch (e) {
    return h('section', { 'aria-label': 'Local model' }, h('h2', { class: 'label' }, 'Local model'),
      h('div', { class: 'notice' }, 'Could not read the local model servers.'));
  }
  if (!d.servers || !d.servers.length) return null;
  const q = d.queue, s = d.speed;
  const lines = [];
  if (s && s.alone.replies) lines.push(`On its own: ${s.alone.median_tokens_per_s ?? '—'} tokens/s, ${s.alone.median_seconds} s per reply (median of ${s.alone.replies})`);
  if (s && s.shared.replies) lines.push(`Sharing the GPU: ${s.shared.median_tokens_per_s ?? '—'} tokens/s, ${s.shared.median_seconds} s per reply (${s.shared.replies})`);
  if (s && (s.alone.replies || s.shared.replies)) lines.push(`Waited in the queue ${s.queued} times; longest ${Math.round(s.longest_wait_s)} s`);
  const speed = h('section', { 'aria-label': 'Replies this week' }, h('div', { class: 'card' }, h('h2', {}, 'Replies this week'),
    lines.length ? lines.map((l) => h('p', {}, l)) : none('No replies recorded since the model guard last restarted.'),
    q ? h('p', { class: 'when' }, `Now: ${q.running} running, ${q.waiting} waiting (at most ${q.slots} at once)`) : null));
  return h('div', {}, d.servers.map((srv) => serverBlock(srv, d.can_change, repaint)), speed);
}

export async function modelsView(root) {
  let alive = true;
  let timer = null;
  root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Models'), loading()));

  async function paint() {
    let d;
    try { d = await getJSON('/api/v2/models'); } catch (e) {
      root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Models'),
        h('div', { class: 'notice' }, 'Could not load the model status.')));
      return;
    }
    if (!alive) return;
    const server = d.status.model_server;
    const paidOn = d.paid_providers.length > 0;

    const tiles = [];
    if (server) {
      const loaded = server.loaded.map((m) => `${m.model} (${count(m.context)} context)`);
      tiles.push(statusTile('Local model server', server.up, server.up
        ? [loaded.length ? `Loaded: ${loaded.join(', ')}` : 'Nothing loaded: the next message loads the model first',
          server.ready ? 'Ready for the agents' : `The agents use ${server.wanted.join(', ')}`]
        : ['LM Studio is closed or that machine is busy.',
          paidOn ? 'Agents with a paid fallback keep answering.' : 'No paid fallback is switched on, so agents cannot answer.']));
    }
    // Problems get a card each; everything that is up shares one, so "all fine" is one glance.
    const upNow = d.status.services.filter((s) => s.up);
    for (const s of d.status.services.filter((x) => !x.up)) tiles.push(statusTile(s.name, false, [s.what]));
    if (upNow.length) tiles.push(statusTile(`${upNow.length} of ${d.status.services.length} services`, true,
      [upNow.map((s) => s.name).join(', ')]));

    const rows = d.agents.map((a) => {
      const last = a.current
        ? `${a.current.kind === 'local' ? 'Local' : a.current.provider} · ${a.current.model}, ${ago(a.current.last_call)}`
        : 'No calls yet';
      const chain = a.fallbacks.length
        ? a.fallbacks.map((f) => `${f.provider} · ${f.model}${f.spent_out ? ' (limit spent)' : ''}`).join(', then ')
        : 'None';
      const who = [
        h('td', {}, h('strong', {}, a.display_name), a.test ? h('span', { class: 'when' }, ' test agent') : null),
        h('td', {}, h('span', { class: `status ${a.now.kind === 'none' ? 'down' : a.now.kind === 'local' ? 'up' : 'paid'}` }, a.now.label),
          h('div', { class: 'when' }, `Last answered by: ${last}`),
          a.current && a.current.since ? h('div', { class: 'when' }, `On that one since ${ago(a.current.since)}`) : null)];
      // The brain stays local: without a paid provider there is no fallback, spend or projection to show.
      if (!paidOn) return h('tr', {}, who);
      return h('tr', {}, ...who,
        h('td', {}, chain),
        h('td', { class: 'num' }, h('strong', {}, usd(a.month_usd)), h('span', { class: 'when' }, ` of ${usd(a.limit_usd)}`),
          meter(a.month_usd, a.limit_usd), h('div', { class: 'when' }, `${usd(a.left_usd)} left`)),
        h('td', { class: 'num' }, h('span', { class: a.over ? 'bad strong' : '' }, usd(a.projected_usd)),
          a.over ? h('div', { class: 'bad' }, 'Over the limit: paid answers would stop before month end') : null));
    });

    const sections = [
      h('h1', {}, 'Models'),
      d.error ? h('div', { class: 'notice' }, d.error) : null,
      h('section', { 'aria-label': 'Status' }, h('div', { class: 'cards tiles wide-only' }, tiles), statusLine(d)),
      await localModel(() => paint()),
      h('section', { 'aria-label': 'Agents' },
        h('h2', { class: 'label' }, paidOn ? `Who answers, and spend in ${d.month}` : 'Who answers'),
        h('div', { class: 'table-box' }, h('table', { class: paidOn ? 'plain wide' : 'plain' },
          h('thead', {}, h('tr', {}, (paidOn ? ['Agent', 'Answering now', 'Paid fallback', 'Spent this month', 'Projected month end']
            : ['Agent', 'Answering now']).map((t, i) => h('th', { class: i > 2 ? 'num' : '' }, t)))),
          h('tbody', {}, rows))),
        h('p', { class: 'when' }, paidOn
          ? 'The projection continues the last 7 days, hour by hour, since paid answers cluster when the local model is off. Costs are the figures the model gateway recorded.'
          : 'The brain stays local: every agent answers with the model on the home server, so nothing is sent anywhere and nothing is spent.')),
    ];

    if (d.all_agents && d.total) {
      const requestSeries = [{ key: 'local', label: 'Local (free)', cls: 's1' }, { key: 'paid', label: 'Paid', cls: 's2' }];
      const table = h('div', { class: 'table-box', hidden: true }, h('table', { class: 'plain' },
        h('thead', {}, h('tr', {}, ['Day', 'Local requests', 'Paid requests', 'Spend'].map((t, i) => h('th', { class: i ? 'num' : '' }, t)))),
        h('tbody', {}, d.daily.filter((x) => x.local || x.paid).reverse().map((x) => h('tr', {},
          h('td', {}, dayLabel(x.date)), h('td', { class: 'num' }, count(x.local)),
          h('td', { class: 'num' }, count(x.paid)), h('td', { class: 'num' }, usd(x.usd)))))));
      const toggle = h('button', { type: 'button', class: 'linkish', 'aria-expanded': 'false', onclick: () => {
        table.hidden = !table.hidden;
        toggle.setAttribute('aria-expanded', String(!table.hidden));
        toggle.textContent = table.hidden ? 'Show these figures as a table' : 'Hide the table';
      } }, 'Show these figures as a table');
      sections.push(
        h('section', { 'aria-label': 'Totals' },
          h('h2', { class: 'label' }, 'All agents this month'),
          h('div', { class: 'cards tiles' },
            h('div', { class: 'card tile' }, h('p', { class: 'when' }, 'Spent'), h('div', { class: 'figure' }, usd(d.total.month_usd)),
              h('p', { class: 'when' }, `of ${usd(d.total.limit_usd)} in limits`)),
            h('div', { class: 'card tile' }, h('p', { class: 'when' }, 'Projected month end'),
              h('div', { class: 'figure' }, usd(d.total.projected_usd))),
            d.providers.map((p) => h('div', { class: 'card tile' }, h('p', { class: 'when' }, p.kind === 'local' ? 'Local model' : p.provider),
              h('div', { class: 'figure' }, p.kind === 'local' ? 'Free' : usd(p.usd)),
              h('p', { class: 'when' }, `${count(p.requests)} requests`))))),
        h('section', { 'aria-label': 'Over time' },
          h('div', { class: 'cards charts' },
            h('div', { class: 'card' }, h('h2', {}, 'Requests per day'),
              columns(d.daily, requestSeries, { label: 'Requests per day over the last 30 days, local and paid' })),
            h('div', { class: 'card' }, h('h2', {}, 'Paid spend per day'),
              columns(d.daily, [{ key: 'usd', label: 'Spend', cls: 's2' }], { money: true, label: 'Paid spend per day over the last 30 days' }))),
          toggle, table));
    }
    root.replaceChildren(h('div', { class: 'page' }, sections));
  }

  await paint();
  timer = setInterval(() => { if (document.visibilityState === 'visible') paint(); }, 60000);
  return () => { alive = false; clearInterval(timer); };
}
