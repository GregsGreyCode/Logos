// Routines (Greg, 22 Nov; it replaces the agent's About page): what an agent does on its own, in plain
// words. The built-in ones (morning briefing, weekly digest, what changed) are switched and timed here,
// kept by Logos through every deploy; the ones you asked your agent for can be paused or deleted (to
// add or change one, ask the agent); the rest work behind the scenes. Its model and reach fold away.
import { modelName, h, api, getJSON, none, loading, icon, ICONS, shortName, when, ask, reveal, focusIfKeyboard } from './util.js';
import * as mx from './mx.js';
import { agentBar } from './agents-void.js';

// Logos's routine jobs (gateway/agent_runtime.py ROUTINE_JOBS): shown as the Built in cards, not as rows.
const ROUTINE_JOBS = new Set(['morning-briefing', 'morning-briefing-check', 'morning-briefing-weekend',
  'morning-briefing-weekend-check', 'weekly-digest', 'changes-daily', 'changes-week', 'changes-check']);
const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

function runTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  if (d.getTime() <= Date.now()) return when(d.getTime() / 1000);
  // Ahead: "10:00" today, "tomorrow 10:00", a weekday this week, then the date.
  const days = Math.round((new Date(d.toDateString()) - new Date(new Date().toDateString())) / 86400000);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (days === 0) return time;
  if (days === 1) return `tomorrow ${time}`;
  if (days < 7) return `${d.toLocaleDateString([], { weekday: 'short' })} ${time}`;
  return `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${time}`;
}
// A job someone asked for is named by the agent ("daily-question"): shown as words.
const named = (j) => (j.managed ? j.label : (j.label || j.name).replace(/[-_]+/g, ' ').replace(/^./, (c) => c.toUpperCase()));

// "0 * * * *" and friends, in words, for the handful of shapes Logos uses; hermes already words people's own.
function schedule(s) {
  if (/^once at /.test(s)) return `Once, ${runTime(s.slice(8).replace(' ', 'T'))}`;
  const m = s.match(/^(\S+) (\S+) \* \* (\S+)$/);
  if (!m) return s.charAt(0).toUpperCase() + s.slice(1);
  const [, min, hour, dow] = m;
  if (min === '*' && hour === '*') return 'Every minute';
  if (/^\*\/\d+$/.test(min) && hour === '*') return `Every ${min.slice(2)} minutes`;
  if (hour === '*') return 'Hourly';
  const at = `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`;
  const named = { '1-5': 'Weekdays', '0,6': 'Weekends', '6,0': 'Weekends' };
  return dow === '*' ? `Daily ${at}` : `${named[dow] || `${DAYS[Number(dow) % 7]}s`} ${at}`;
}

export async function agentsView(root, [agentArg]) {
  let alive = true;
  let data = { agents: [], others: [] };
  root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Routines'), loading('rows', 4)));
  try { data = await getJSON('/api/v2/agents'); } catch (e) { /* shown below */ }
  const agent = data.agents.find((a) => a.name === agentArg) || data.agents[0];
  const detail = h('div', { class: 'routines' });

  // Which agent (a wide screen): your agents as cards. On a phone the agent's own bar is the header.
  const cards = h('div', { class: 'cards agent-cards wide-only' },
    data.agents.map((a) => h('a', { href: `#/agents/${a.name}`, class: `card agent-card ${agent && a.name === agent.name ? 'picked' : ''}` },
      h('div', { class: 'card-head' }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), h('strong', {}, a.display_name)),
      h('span', { class: `status ${a.running ? 'up' : ''}` }, a.running ? 'Running' : 'Not running'),
      a.shared ? h('span', { class: 'when' }, 'Shared with the household') : null)));

  const note = h('p', { class: 'when', role: 'status' });
  const say = (t) => { note.textContent = t; };

  // One routine (Greg, 23 Nov: one list; whether it is built in or asked for only changes how it is
  // changed): a switch, its name and when, and a tap opens what can be done with it.
  const opened = new Set();                 // cards left open across a reload
  function routineCard({ key, title, summary, on, toggle, body }) {
    const sw = h('input', { type: 'checkbox', checked: on ? '' : null, 'aria-label': `${title}: on or off` });
    const sum = h('span', { class: 'when routine-sum' }, summary);
    const status = h('span', { class: 'when routine-status' });
    const more = h('div', { class: 'routine-more', hidden: !opened.has(key) }, ...body);
    const head = h('button', { type: 'button', class: 'routine-open', 'aria-expanded': String(opened.has(key)) },
      h('span', { class: 'routine-title' }, h('strong', {}, title), sum),
      h('span', { class: 'routine-chev', 'aria-hidden': 'true' }, '›'));
    const card = h('section', { class: `card routine${on ? '' : ' off'}`, 'aria-label': title },
      h('div', { class: 'routine-head' }, h('label', { class: 'switch' }, sw, h('span', { class: 'switch-track', 'aria-hidden': 'true' })), head, status),
      more);
    head.addEventListener('click', () => {
      const open = more.hidden;
      more.hidden = !open; head.setAttribute('aria-expanded', String(open));
      if (open) opened.add(key); else opened.delete(key);
    });
    const busy = async (fn) => {
      status.textContent = 'Saving…';
      for (const el of card.querySelectorAll('input, select, button')) el.disabled = true;
      const msg = await fn().catch(() => 'Not saved: Logos did not answer.');
      status.textContent = msg || 'Saved';
      for (const el of card.querySelectorAll('input, select, button')) el.disabled = false;
      setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2500);
    };
    sw.addEventListener('change', () => { card.classList.toggle('off', !sw.checked); busy(() => toggle(sw.checked)); });
    return { card, busy, setSummary: (t) => { sum.textContent = t; } };
  }
  const putRoutine = async (key, value) => {
    const r = await api(`/api/v2/agents/${agent.name}/routines`, { method: 'PUT', body: { [key]: value } });
    const d = await r.json().catch(() => ({}));
    return r.ok ? '' : (d.message || 'Not saved.');
  };
  const timeField = (label, value) => h('label', { class: 'routine-field' }, h('span', { class: 'when' }, label),
    h('input', { type: 'time', value, step: '300', onclick: (e) => { try { e.target.showPicker?.(); } catch (err) { /* opens by itself */ } } }));
  const dayField = (value) => h('label', { class: 'routine-field' }, h('span', { class: 'when' }, 'Day'),
    h('select', {}, DAYS.map((d, i) => h('option', { value: String(i), selected: i === value ? '' : null }, d))));
  const val = (f) => f.querySelector('input, select').value;

  // + New routine (Greg, 22 Nov): say or type what and when; the agent sets it up (it understands "every
  // other Friday at 6" better than a form would), in its chat, and the new one appears below.
  function newRoutine(had) {
    const text = h('textarea', { id: 'nr-text', rows: '3', placeholder: 'e.g. Every Friday at 18:00, remind me to put the bins out' });
    const msg = h('p', { class: 'when', role: 'status' });
    const go = h('button', { type: 'submit', class: 'btn' }, 'Set it up');
    let rec = null;
    const mic = h('button', { type: 'button', class: 'btn quiet nr-mic', 'aria-label': 'Say it instead', title: 'Say it instead', onclick: async () => {
      if (rec) { rec.stop(); return; }
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { msg.textContent = 'This browser cannot record audio.'; return; }
      let stream;
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) { msg.textContent = 'The microphone is blocked for this site.'; return; }
      const chunks = []; rec = new MediaRecorder(stream);
      rec.addEventListener('dataavailable', (e) => { if (e.data.size) chunks.push(e.data); });
      rec.addEventListener('stop', async () => {
        stream.getTracks().forEach((t) => t.stop()); rec = null; mic.classList.remove('recording'); mic.textContent = '🎤';
        msg.textContent = 'Turning it into words…';
        const blob = new Blob(chunks, { type: chunks[0]?.type || 'audio/webm' });
        const r = await api(`/api/v2/agents/${agent.name}/voice`, { method: 'POST', raw: blob, headers: { 'Content-Type': blob.type } }).catch(() => null);
        const d = r ? await r.json().catch(() => ({})) : {};
        if (d.text) { text.value = (text.value.trim() ? `${text.value.trim()} ` : '') + d.text; msg.textContent = 'Check it, then set it up.'; }
        else msg.textContent = 'No words were heard. Try again, or type it.';
      });
      rec.start(); mic.classList.add('recording'); mic.textContent = '■'; msg.textContent = 'Listening… tap ■ to stop.';
    } }, '🎤');
    const layer = h('div', { class: 'ask-layer' });
    const close = () => { if (rec) rec.stop(); layer.remove(); };
    layer.append(h('div', { class: 'ask-scrim', onclick: close }),
      h('form', { class: 'ask-panel', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'New routine', onsubmit: async (e) => {
        e.preventDefault();
        const what = text.value.trim();
        if (!what) { msg.textContent = 'Say or type what it should do, and when.'; return; }
        go.disabled = true; msg.textContent = 'Sending…';
        try {
          const c = await mx.get();
          const { pickRoom } = await import('./chat-mx.js');
          const room = pickRoom(c, agent.matrix_id);
          if (!room) throw new Error('no chat with the agent yet');
          await c.sendMessage(room.roomId, { msgtype: 'm.text', body: `Please set up a routine for me (a scheduled job): ${what}` });
        } catch (err) { msg.textContent = `Not sent: ${err.message}. Connect this device to your chats in Settings › Devices.`; go.disabled = false; return; }
        msg.textContent = `${agent.display_name} is setting it up… This can take a minute or two; its answer is in your chat too.`;
        // Watch for it: the list grows when the job exists (up to five minutes).
        for (let i = 0; i < 60 && layer.isConnected; i++) {
          await new Promise((ok) => setTimeout(ok, 5000));
          const st = await getJSON(`/api/v2/agents/${agent.name}/status`).catch(() => null);
          if (st && (st.jobs || []).filter((j) => !j.managed).length > had) { msg.textContent = 'Set up.'; await load(); setTimeout(close, 900); return; }
        }
        if (layer.isConnected) { msg.textContent = `Nothing new yet: see what ${agent.display_name} said in your chat.`; go.disabled = false; }
      } },
      h('h2', {}, 'New routine'), h('p', {}, `What should ${agent.display_name} do, and when?`),
      h('label', { for: 'nr-text', class: 'sr' }, 'What and when'), h('div', { class: 'nr-row' }, text, mic), msg,
      h('div', { class: 'row' }, go, h('button', { type: 'button', class: 'btn quiet', onclick: close }, 'Cancel'))));
    document.body.append(layer);
    focusIfKeyboard(text);
  }

  async function load() {
    if (!agent) return;
    let st, rt;
    try {
      [st, rt] = await Promise.all([getJSON(`/api/v2/agents/${agent.name}/status`), getJSON(`/api/v2/agents/${agent.name}/routines`)]);
    } catch (e) {
      detail.replaceChildren(h('div', { class: 'notice' }, 'Could not load this agent\'s routines just now.'));
      return;
    }
    if (!alive) return;
    const R = rt.routines || {};
    const cards = [];
    // A built-in routine: its times in the card; a change is saved at once.
    const builtIn = (key, title, fields, value, summary, cfg) => {
      const c = routineCard({ key, title, summary: summary(), on: cfg.enabled, body: [h('div', { class: 'routine-fields' }, ...fields)],
        toggle: (on) => { cfg.enabled = on; return putRoutine(key, value()); } });
      for (const el of fields.flatMap((f) => [...f.querySelectorAll('input, select')])) {
        el.addEventListener('change', () => { c.setSummary(summary()); c.busy(() => putRoutine(key, value())); });
      }
      cards.push(c.card);
    };
    if (R.briefing) {
      const wd = timeField('Weekdays', R.briefing.weekdays), we = timeField('Weekends', R.briefing.weekends);
      builtIn('briefing', 'Morning briefing', [wd, we], () => ({ enabled: R.briefing.enabled, weekdays: val(wd), weekends: val(we) }),
        () => `Weekdays ${val(wd)}, weekends ${val(we)}`, R.briefing);
    }
    if (R.digest) {
      const day = dayField(R.digest.day), t = timeField('Time', R.digest.time);
      builtIn('digest', 'Weekly digest', [day, t], () => ({ enabled: R.digest.enabled, day: Number(val(day)), time: val(t) }),
        () => `${DAYS[Number(val(day))]}s ${val(t)}`, R.digest);
    }
    if (R.changes) {
      const t = timeField('Time', R.changes.time);
      builtIn('changes', 'What changed', [t], () => ({ enabled: R.changes.enabled, time: val(t) }), () => `Daily ${val(t)} in the group chat`, R.changes);
    }
    // One asked for: on/off pauses it; to change it, ask the agent; or delete it.
    const jobs = st.jobs || [];
    const yours = jobs.filter((j) => !j.managed);
    const behind = jobs.filter((j) => j.managed && !ROUTINE_JOBS.has(j.name));
    const jobAction = async (j, action) => {
      const r = await api(`/api/v2/agents/${agent.name}/jobs/${j.id}/${action}`, { method: 'POST' });
      return r.ok ? '' : 'That did not work just now.';
    };
    for (const j of yours) {
      const del = h('button', { type: 'button', class: 'btn quiet small danger-text', onclick: async () => {
        if (!await ask({ title: `Delete "${named(j)}"?`, text: `${agent.display_name} will stop doing it. You can always ask again.`, yes: 'Delete', danger: true })) return;
        const msg = await jobAction(j, 'remove').catch(() => 'That did not work just now.');
        if (msg) say(msg); else load();
      } }, 'Delete');
      const c = routineCard({ key: `job:${j.id}`, title: named(j),
        summary: `${schedule(j.schedule)}${j.active ? (j.next_run ? ` · next ${runTime(j.next_run)}` : '') : ' · paused'}`,
        on: j.active, toggle: async (on) => { const m = await jobAction(j, on ? 'resume' : 'pause'); if (!m) setTimeout(load, 300); return m; },
        body: [h('p', { class: 'when' }, `You asked ${agent.display_name} for this. To change it, ask it, for example "move this to 9am".`),
          h('div', { class: 'row' }, del)] });
      cards.push(c.card);
    }
    const behindRow = (j) => h('div', { class: 'row-item' },
      h('span', { class: 'row-main' }, j.label),
      h('span', { class: 'row-meta' }, schedule(j.schedule), ' · ',
        j.last_run ? h('span', { class: j.last_ok ? 'ok' : 'bad' }, `${j.last_ok ? '✓' : '✕ failed'} ${runTime(j.last_run)}`) : 'not yet run'));

    const m = st.model;
    const kv = (k, v, cls) => h('div', { class: 'row-item' }, h('span', { class: `row-meta ${cls || ''}` }, k), h('span', { class: 'row-main' }, v));
    detail.replaceChildren(...[                  // (a null would print as the word "null")
      h('div', { class: 'routines-top' },
        h('p', { class: 'routines-intro' }, `What ${agent.display_name} does on its own.`),
        h('button', { type: 'button', class: 'btn', onclick: () => newRoutine(yours.length) }, '+ New routine')),
      st.jobs == null ? h('div', { class: 'notice' }, `${agent.display_name} did not answer, so the ones you asked for could not be listed.`) : null,
      cards.length ? h('div', { class: 'routine-cards' }, ...cards)
        : none(`Nothing yet. Tap + New routine, or ask ${agent.display_name} to remind you of something.`),
      note,
      h('details', { class: 'routines-fold' }, h('summary', {}, `Behind the scenes (${behind.length})`),
        h('p', { class: 'when' }, 'How the agent keeps itself going. Nothing to set here.'),
        h('div', { class: 'rows' }, ...behind.map(behindRow))),
      h('details', { class: 'routines-fold' }, h('summary', {}, `About ${agent.display_name}`),
        h('div', { class: 'rows' },
          kv('Model', `${m.local ? 'Local' : 'Hosted'} · ${modelName(m.name)}${m.up === false ? ' · not answering' : ''}`),
          kv('Can reach', st.reach.allowed.join(', '), 'ok'),
          kv('Cannot reach', st.reach.blocked.join(', '), 'bad'))),
    ].filter(Boolean));
    reveal(detail);
  }

  root.replaceChildren(h('div', { class: 'page agents-page' }, agent ? agentBar(agent, 'about') : null,
    h('h1', { class: 'agents-title' }, agent ? `${agent.display_name}: routines` : 'Routines'),
    data.agents.length ? cards : none('No agents are set up yet.'), detail));
  detail.replaceChildren(loading('cards', 3));
  await load();
  return () => { alive = false; };
}
