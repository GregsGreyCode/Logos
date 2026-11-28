// Models screen: is everything that answers up, who answers each agent right now, and what
// the paid fallbacks have cost this month. All figures come from the model gateway's records.
import { h, getJSON, api, none, loading, modelName, ask } from './util.js';
const PROVIDER_NAMES = { anthropic: 'Claude Advisor (Anthropic)', openrouter: 'OpenRouter', openai: 'OpenAI' };

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

// llama-swap's live numbers (model_server_metrics.live): the GPU, speed, the prompt cache, the queue.
const pct = (r) => `${Math.round(r * 100)} %`;
function liveRows(lv) {
  if (!lv) return [];
  const rows = [];
  const g = lv.gpu;
  if (g && (g.memory_total_bytes || g.temperature_celsius != null)) {
    const gb = (b) => (b / 2 ** 30).toFixed(1);
    rows.push(['GPU now', [g.memory_total_bytes ? `${gb(g.memory_used_bytes || 0)} of ${gb(g.memory_total_bytes)} GB` : null,
      g.temperature_celsius != null ? `${Math.round(g.temperature_celsius)} °C` : null,
      g.util_percent != null ? `${Math.round(g.util_percent)} % busy` : null].filter(Boolean).join(' · '), g.name || '']);
  }
  if (lv.metrics_on === false) {
    rows.push(['Speed', 'Not reported', 'add --metrics to its cmd in config.yaml']);
  } else if (lv.metrics_on) {
    const n = (v) => (v == null ? '—' : count(Math.round(v)));
    rows.push(['Speed', `reads ${n(lv.prompt_tokens_per_s)} · writes ${n(lv.generation_tokens_per_s)} tokens/s`, 'averages since the model started']);
    if (lv.prompt_cache_hit_ratio != null) rows.push(['Prompt cache', `${pct(lv.prompt_cache_hit_ratio)} reused`, 'share of prompt tokens it did not have to read again']);
    if (lv.kv_cache_usage_ratio != null) rows.push(['KV cache in use', pct(lv.kv_cache_usage_ratio), '']);
    rows.push(['Working on', `${lv.requests_processing} now · ${lv.requests_deferred} waiting`, 'on the model PC']);
  }
  return rows;
}

function serverBlock(srv, canChange, repaint) {
  // P5: the server's address and GPU sit in the model card's own heading, and the checks, the
  // reload button and "Settings only the LM Studio app can change" at its foot.
  const where = h('div', { class: 'card-where' }, h('span', { class: 'when' }, srv.server), gpuLine(srv, canChange, repaint));
  const parts = [h('h2', { class: 'label' }, 'Local model')];
  if (!srv.up) {
    parts.push(where);
    parts.push(h('div', { class: 'notice' }, srv.error || 'The model server is off, or that PC is busy.'));
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
        ...(srv.kind === 'llama-swap' ? [
          ['KV cache', String(i.kv_cache_type || 'f16').toUpperCase(), ''],
          ['Experts on the CPU', i.n_cpu_moe === 'all' ? 'all layers' : i.n_cpu_moe ? `${i.n_cpu_moe} layers` : 'none', 'the rest are on the GPU'],
        ] : [['Experts', i.num_experts ?? '—', '']]),
        ['File', `${m.size_gb} GB · ${m.quantization}`, m.params],
        ...liveRows(m.live),
      ];
      const unload = canChange && !ours ? h('button', { type: 'button', class: 'btn ghost', onclick: async (ev) => {
        if (!await ask({ title: `Unload ${m.key}?`, text: 'No agent uses it.', yes: 'Unload' })) return;
        ev.target.disabled = true;
        const r = await post('unload', { server: srv.server, instance: i.id });
        ev.target.textContent = r.ok ? 'Unloaded' : 'Could not unload';
      } }, 'Unload') : null;
      cards.push(h('div', { class: 'card' },
        h('div', { class: 'card-title' }, h('h2', { title: m.name }, modelName(m.name)),
          h('span', { class: `status ${ours ? 'up' : 'down'}` }, ours ? `Used by ${ours.agents.join(', ')}` : 'Not used by any agent')),
        cards.length ? null : where,
        h('dl', { class: 'facts' }, rows.flatMap(([k, v, note]) => [h('dt', {}, k),
          h('dd', {}, String(v), note ? h('span', { class: 'when' }, ` · ${note}`) : null)])),
        unload));
    }
  }
  if (!srv.loaded.length) cards.push(h('div', { class: 'card' }, h('h2', {}, 'Nothing loaded'), where,
    h('p', { class: 'when' }, 'The first message loads the agents\' model, which takes about a minute.')));
  parts.push(h('div', { class: 'cards' }, cards));
  const foot = h('div', { class: 'card-foot' });
  cards[0].append(foot);
  foot.append(h('ul', { class: 'checks' }, (srv.checks || []).map((c) => h('li', { class: `check ${c.level}` },
    h('span', { class: 'check-mark', 'aria-hidden': 'true' }, c.level === 'warn' ? '!' : c.level === 'ok' ? '✓' : 'i'),
    h('span', { class: 'visually-hidden' }, c.level === 'warn' ? 'Problem: ' : c.level === 'ok' ? 'Fine: ' : 'Note: '), c.text))));
  if (canChange && Object.keys(srv.want).length) {
    const status = h('p', { class: 'when', role: 'status' });
    foot.append(h('div', { class: 'actions' },
      h('button', { type: 'button', class: 'btn', onclick: async (ev) => {
        const swap = srv.kind === 'llama-swap';
        if (!await ask({ title: swap ? 'Restart the agents\' model?' : 'Reload the agents\' model?',
          text: swap ? 'With the settings in config.yaml on that PC. The agents cannot answer for a few seconds.'
            : 'With Logos\'s settings. The agents cannot answer for about a minute.', yes: swap ? 'Restart' : 'Reload' })) return;
        ev.target.disabled = true; status.textContent = swap ? 'Restarting…' : 'Reloading… this takes about a minute.';
        const r = await post('reload', { server: srv.server });
        status.textContent = !r.ok ? (r.body.error || 'Could not reload.')
          : swap ? 'Restarted with config.yaml\'s settings.' : `Reloaded with a ${count(r.body.config?.context_length)}-token window.`;
        ev.target.disabled = false;
      } }, srv.kind === 'llama-swap' ? 'Restart the model' : 'Reload with Logos settings'), status));
  }
  if (srv.kind === 'llama-swap') {
    foot.append(h('p', { class: 'when' }, 'llama-swap runs this model with the settings in config.yaml on that PC (window, GPU layers, experts, KV cache). Change them there; the card above shows what is running.'));
    return h('section', { 'aria-label': `Local model on ${srv.server}` }, parts);
  }
  foot.append(h('details', { class: 'lms-only' }, h('summary', {}, 'Settings only the LM Studio app can change'),
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

// ── Local vs cloud (28 Nov): the electricity the model PC used, against what Claude would have cost ──

// 4.6M, 159K: the same in every browser language (some write "4.6m").
function compact(n) {
  const v = Number(n || 0);
  for (const [d, u] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
    if (Math.abs(v) >= d) return `${(v / d).toFixed(v / d >= 100 ? 0 : 1).replace(/\.0$/, '')}${u}`;
  }
  return String(Math.round(v));
}
function money(n, cur) {
  const v = Number(n || 0);
  const f = (x) => { try { return x.toLocaleString([], { style: 'currency', currency: cur }); } catch (e) { return `${x.toFixed(2)} ${cur}`; } };
  return v !== 0 && Math.abs(v) < 0.005 ? `< ${f(0.01)}` : f(v);
}
const hours = (s) => (s >= 3600 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 60)} min`);
const short = (label) => label.replace(/^Claude /, '');

// What each agent used this month, from the model gateway's own records (the guard cannot tell agents apart).
function agentShares(agents) {
  const rows = (agents || []).map((a) => {
    const local = (a.models || []).filter((m) => m.kind === 'local');
    return { name: a.display_name, in: local.reduce((s, m) => s + (m.prompt_tokens || 0), 0), out: local.reduce((s, m) => s + (m.completion_tokens || 0), 0) };
  }).filter((r) => r.in + r.out > 0);
  const all = rows.reduce((s, r) => s + r.in + r.out, 0);
  if (!all) return null;
  return h('div', { class: 'cost-agents' }, h('h3', {}, 'By agent this month'),
    h('ul', { class: 'plain' }, rows.sort((a, b) => b.in + b.out - a.in - a.out).map((r) => h('li', {},
      h('strong', {}, r.name), ` ${compact(r.in)} in · ${compact(r.out)} out`,
      h('span', { class: 'when' }, ` · ${Math.round(((r.in + r.out) / all) * 100)} %`)))),
    h('p', { class: 'when' }, 'From the model gateway\'s records (the model guard does not know which agent asked).'));
}

function costSettings(s, repaint) {
  const field = (label, attrs) => h('label', {}, h('span', { class: 'when' }, label), h('input', attrs));
  const price = field(`Electricity per kWh (${s.currency})`, { type: 'number', name: 'price', min: 0, max: 10, step: 0.001, value: s.price_per_kwh });
  const cur = field('Currency', { type: 'text', name: 'currency', maxlength: 3, value: s.currency, autocapitalize: 'characters' });
  const fx = field(`${s.currency} per US dollar`, { type: 'number', name: 'usd_rate', min: 0.0001, step: 0.0001, value: s.usd_rate });
  const base = field('GPU idle baseline (watts)', { type: 'number', name: 'baseline', min: 0, max: 2000, step: 1, value: s.baseline_watts ?? '',
    placeholder: s.idle_measured_watts != null ? `measured ${Math.round(s.idle_measured_watts)}` : '0' });
  const msg = h('p', { class: 'when', role: 'status' });
  const form = h('form', { class: 'cost-form', onsubmit: async (ev) => {
    ev.preventDefault();
    const v = (n) => form.querySelector(`[name="${n}"]`).value;
    const r = await api('/api/v2/models/local-cost', { method: 'PUT', body: {
      price_per_kwh: Number(v('price')), currency: v('currency'), usd_rate: Number(v('usd_rate')), baseline_watts: v('baseline') === '' ? null : Number(v('baseline')) } });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) { msg.textContent = b.error || 'Could not save.'; return; }
    msg.textContent = 'Saved.';
    repaint();
  } }, price, cur, fx, base, h('button', { type: 'submit', class: 'btn' }, 'Save'), msg);
  return h('details', { class: 'cost-settings' }, h('summary', {}, 'Prices and settings'), form,
    h('p', { class: 'when' }, 'The idle baseline is what the GPU draws with no model loaded (the PC would be on anyway). Leave it empty to use the median Logos measured, or set 0 to count every watt.'));
}

function localVsCloud(c, agents, state, redraw, refresh) {
  const cur = c.settings.currency;
  const periods = [['today', 'Today'], ['this_month', 'This month'], ['all', c.since ? `Since ${new Date(c.since).toLocaleDateString([], { day: 'numeric', month: 'short' })}` : 'Since tracking began']];
  const p = c[state.period] || c.today;
  const tabs = h('div', { class: 'cost-periods', role: 'group', 'aria-label': 'Period' }, periods.map(([k, label]) => h('button', {
    type: 'button', class: 'btn small', 'aria-pressed': String(state.period === k), onclick: () => { state.period = k; redraw(); } }, label)));
  const t = p.tokens, e = p.energy;
  const cachedPct = t.prompt ? Math.round((t.cached / t.prompt) * 100) : 0;
  const facts = [
    ['Tokens in', compact(t.prompt), p.cache_measured ? `${cachedPct} % reused from the cache` : 'cache not measured: counted as not cached'],
    ['Tokens out', compact(t.completion), ''],
    ['Requests', count(p.requests), p.busy_s ? `${hours(p.busy_s)} answering` : ''],
    ['Electricity', money(p.local.total, cur), p.energy_measured
      ? `${e.answering_kwh.toFixed(2)} kWh answering (${money(p.local.answering, cur)}) · ${e.loaded_kwh.toFixed(2)} kWh kept loaded (${money(p.local.loaded, cur)})`
      : 'no GPU power readings yet'],
  ];
  const verdict = (x) => (x.saved >= 0
    ? h('span', { class: 'ok' }, `saves ${money(x.saved, cur)}`)
    : h('span', { class: 'bad' }, `${money(-x.saved, cur)} cheaper on ${short(x.label)}`));
  const table = h('table', { class: 'plain cost-table' },
    h('thead', {}, h('tr', {}, h('th', {}, 'On Claude'), h('th', { class: 'num' }, 'Would cost'), h('th', { class: 'num' }, 'Against local'))),
    h('tbody', {}, p.cloud.map((x) => h('tr', { 'data-model': x.model }, h('td', {}, short(x.label)),
      h('td', { class: 'num' }, money(x.cost, cur)), h('td', { class: 'num' }, verdict(x))))));
  const proj = c.projected && state.period === 'this_month' ? h('p', { class: 'cost-projected' },
    h('strong', {}, `Projected for ${c.month.split(' ')[0]}: `), `electricity ${money(c.projected.local, cur)}; `,
    c.projected.cloud.map((x) => `${short(x.label)} ${money(x.cost, cur)}`).join(', '),
    h('span', { class: 'when' }, ` · carries on the last ${c.projected.from_days} days`)) : null;
  const haiku = (c.prices.models || []).find((m) => (m.assumed || []).includes('cache_read'));
  const notes = h('ul', { class: 'plain cost-notes when' },
    h('li', {}, 'Local cost is the GPU\'s electricity only, at your price: not the PC\'s other parts or the hardware itself.'),
    h('li', {}, `Claude list prices (${c.prices.as_of}), in ${cur} at ${c.settings.usd_rate} per dollar. Cached prompt tokens at the cache-read price; the one-off cache-write premium is left out.`),
    haiku ? h('li', {}, `${short(haiku.label)}'s cache-read price is not published: assumed 10 % of its input price. Prompts over ${count(haiku.long.over)} tokens use its higher rates.`) : null,
    c.settings.baseline_used ? h('li', {}, `${Math.round(c.settings.baseline_used)} W of GPU idle draw is left out (${c.settings.baseline_from === 'setting' ? 'set below' : 'measured with no model loaded'}).`) : null);
  return h('section', { 'aria-label': 'Local vs cloud' }, h('h2', { class: 'label' }, 'Local vs cloud'),
    h('div', { class: 'card cost-card' }, h('h2', {}, 'What running at home saves'), tabs,
      h('dl', { class: 'facts' }, facts.flatMap(([k, v, note]) => [h('dt', {}, k),
        h('dd', {}, v, note ? h('span', { class: 'when' }, ` · ${note}`) : null)])),
      h('div', { class: 'table-box' }, table), proj, state.period === 'this_month' ? agentShares(agents) : null, notes,
      costSettings(c.settings, refresh)));
}

export async function modelsView(root) {
  let alive = true;
  let timer = null;
  const costState = { period: 'today' };
  root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Models', h('span', { class: 'beta-tag' }, 'Beta')), loading()));

  async function paint() {
    let d;
    try { d = await getJSON('/api/v2/models'); } catch (e) {
      root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Models', h('span', { class: 'beta-tag' }, 'Beta')),
        h('div', { class: 'notice' }, 'Could not load the model status.')));
      return;
    }
    if (!alive) return;
    const server = d.status.model_server;
    const paidOn = d.paid_providers.length > 0;

    const tiles = [];
    if (server) {
      const loaded = server.loaded.map((m) => `${modelName(m.model)} (${count(m.context)} context)`);
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
        ? `${a.current.kind === 'local' ? 'Local' : a.current.provider} · ${modelName(a.current.model)}, ${ago(a.current.last_call)}`
        : 'No calls yet';
      const chain = a.fallbacks.length
        ? a.fallbacks.map((f) => `${f.provider} · ${modelName(f.model)}${f.spent_out ? ' (limit spent)' : ''}`).join(', then ')
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
      h('h1', {}, 'Models', h('span', { class: 'beta-tag' }, 'Beta')),
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

    if (d.all_agents) {                               // whole-household figures: whoever manages Logos
      let c = null;
      try { c = await getJSON('/api/v2/models/local-cost'); } catch (e) { c = null; }
      if (!alive) return;
      if (c && c.today) {
        const slot = h('div', {});
        const draw = () => slot.replaceChildren(localVsCloud(c, d.agents, costState, draw, async () => {
          try { c = await getJSON('/api/v2/models/local-cost'); } catch (e) { /* keep the last figures */ }
          draw();
        }));
        draw();
        sections.push(slot);
      }
    }

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
            // Names people know, not provider ids ("anthropic" is the Claude Advisor's spend, Greg 23 Nov).
            d.providers.map((p) => h('div', { class: 'card tile' }, h('p', { class: 'when' }, p.kind === 'local' ? 'Local model' : PROVIDER_NAMES[p.provider] || p.provider),
              h('div', { class: 'figure' }, p.kind === 'local' ? 'Free' : usd(p.usd)),
              h('p', { class: 'when' }, `${count(p.requests)} requests`))))),
        h('section', { 'aria-label': 'Over time' },
          h('div', { class: 'cards charts' },
            h('div', { class: 'card' }, h('h2', {}, 'Requests per day'),
              columns(d.daily, requestSeries, { label: 'Requests per day over the last 30 days, local and paid' })),
            // An empty chart when nothing was paid says less than one line (Greg, 23 Nov).
            (d.daily || []).some((x) => Number(x.usd) > 0)
              ? h('div', { class: 'card' }, h('h2', {}, 'Paid spend per day'),
                columns(d.daily, [{ key: 'usd', label: 'Spend', cls: 's2' }], { money: true, label: 'Paid spend per day over the last 30 days' }))
              : h('div', { class: 'card' }, h('h2', {}, 'Paid spend per day'), h('p', { class: 'when' }, 'Nothing paid in the last 30 days.'))),
          toggle, table));
    }
    root.replaceChildren(h('div', { class: 'page' }, sections));
  }

  await paint();
  timer = setInterval(() => { if (document.visibilityState === 'visible') paint(); }, 60000);
  return () => { alive = false; clearInterval(timer); };
}
