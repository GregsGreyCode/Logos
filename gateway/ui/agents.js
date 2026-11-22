// Routines (Greg, 22 Nov; it replaces the agent's About page): what an agent does on its own, in plain
// words. The built-in ones (morning briefing, weekly digest, what changed) are switched and timed here,
// kept by Logos through every deploy; the ones you asked your agent for can be paused or deleted (to
// add or change one, ask the agent); the rest work behind the scenes. Its model and reach fold away.
import { modelName, h, api, getJSON, none, loading, icon, ICONS, shortName, when, ask, reveal } from './util.js';
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

  // One built-in routine: a switch, its times, and what it does. A change is saved at once.
  function routineCard(key, r, title, what, fields, save) {
    const sw = h('input', { type: 'checkbox', checked: r.enabled ? '' : null, 'aria-label': `${title}: on or off` });
    const body = h('div', { class: 'routine-fields', hidden: !r.enabled }, ...fields);
    const status = h('span', { class: 'when routine-status' });
    const commit = async () => {
      status.textContent = 'Saving…';
      for (const el of card.querySelectorAll('input, select')) el.disabled = true;
      const r2 = await api(`/api/v2/agents/${agent.name}/routines`, { method: 'PUT', body: { [key]: save(sw.checked) } }).catch(() => null);
      const d = r2 ? await r2.json().catch(() => ({})) : {};
      status.textContent = !r2 ? 'Not saved: Logos did not answer.' : r2.ok ? 'Saved' : (d.message || 'Not saved.');
      for (const el of card.querySelectorAll('input, select')) el.disabled = false;
      setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2500);
    };
    sw.addEventListener('change', () => { body.hidden = !sw.checked; commit(); });
    for (const el of fields.flatMap((f) => [...f.querySelectorAll('input, select')])) el.addEventListener('change', commit);
    const card = h('section', { class: 'card routine', 'aria-label': title },
      h('div', { class: 'routine-head' }, h('label', { class: 'switch' }, sw, h('span', { class: 'switch-track', 'aria-hidden': 'true' })),
        h('span', { class: 'routine-title' }, h('strong', {}, title), h('span', { class: 'when' }, what)), status),
      body);
    return card;
  }
  const timeField = (label, value) => h('label', { class: 'routine-field' }, h('span', { class: 'when' }, label),
    h('input', { type: 'time', value, step: '300' }));
  const dayField = (value) => h('label', { class: 'routine-field' }, h('span', { class: 'when' }, 'Day'),
    h('select', {}, DAYS.map((d, i) => h('option', { value: String(i), selected: i === value ? '' : null }, d))));
  const val = (f) => f.querySelector('input, select').value;

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
    const built = [];
    if (R.briefing) {
      const wd = timeField('Weekdays', R.briefing.weekdays), we = timeField('Weekends', R.briefing.weekends);
      built.push(routineCard('briefing', R.briefing, 'Morning briefing', 'The day ahead: your calendar, the weather and anything waiting.',
        [wd, we], (on) => ({ enabled: on, weekdays: val(wd), weekends: val(we) })));
    }
    if (R.digest) {
      const day = dayField(R.digest.day), t = timeField('Time', R.digest.time);
      built.push(routineCard('digest', R.digest, 'Weekly digest', 'What it learned and what is still open, once a week.',
        [day, t], (on) => ({ enabled: on, day: Number(val(day)), time: val(t) })));
    }
    if (R.changes) {
      const t = timeField('Time', R.changes.time);
      built.push(routineCard('changes', R.changes, 'What changed', 'The day\'s changes to the agents and home servers, in the group chat; the week\'s on Sundays.',
        [t], (on) => ({ enabled: on, time: val(t) })));
    }

    const jobs = st.jobs || [];
    const yours = jobs.filter((j) => !j.managed);
    const behind = jobs.filter((j) => j.managed && !ROUTINE_JOBS.has(j.name));
    const act = (j, action, label) => h('button', { type: 'button', class: `btn ${action === 'remove' ? 'quiet danger-text' : 'quiet'} small`, onclick: async (e) => {
      if (action === 'remove' && !await ask({ title: `Delete "${named(j)}"?`, text: `${agent.display_name} will stop doing it. You can always ask again.`, yes: 'Delete', danger: true })) return;
      e.target.disabled = true;
      const r = await api(`/api/v2/agents/${agent.name}/jobs/${j.id}/${action}`, { method: 'POST' }).catch(() => null);
      if (r && r.ok) load(); else { e.target.disabled = false; say('That did not work just now. Try again in a moment.'); }
    } }, label);
    const yourRow = (j) => h('div', { class: `row-item routine-row${j.active ? '' : ' paused'}` },
      h('span', { class: 'row-main' }, named(j)),
      h('span', { class: 'row-meta' }, schedule(j.schedule),
        j.active ? (j.next_run ? ` · next: ${runTime(j.next_run)}` : '') : ' · paused'),
      h('span', { class: 'row-actions' }, j.active ? act(j, 'pause', 'Pause') : act(j, 'resume', 'Resume'), act(j, 'remove', 'Delete')));
    const behindRow = (j) => h('div', { class: 'row-item' },
      h('span', { class: 'row-main' }, j.label),
      h('span', { class: 'row-meta' }, schedule(j.schedule), ' · ',
        j.last_run ? h('span', { class: j.last_ok ? 'ok' : 'bad' }, `${j.last_ok ? '✓' : '✕ failed'} ${runTime(j.last_run)}`) : 'not yet run'));

    const m = st.model;
    const kv = (k, v, cls) => h('div', { class: 'row-item' }, h('span', { class: `row-meta ${cls || ''}` }, k), h('span', { class: 'row-main' }, v));
    detail.replaceChildren(
      h('p', { class: 'routines-intro' }, `What ${agent.display_name} does on its own. To add something, just ask it, for example `,
        h('em', {}, '"remind me every Friday at 6 to put the bins out"'), '.'),
      h('h2', { class: 'label' }, 'Built in'),
      built.length ? h('div', { class: 'routine-cards' }, ...built) : none('None for this agent.'),
      h('p', { class: 'when' }, 'Built-in routines only ever write to you; they never message other agents.'),
      h('h2', { class: 'label' }, 'You asked for'),
      st.jobs == null ? h('div', { class: 'notice' }, `${agent.display_name} did not answer, so these could not be listed.`)
        : yours.length ? h('div', { class: 'rows' }, ...yours.map(yourRow))
          : none(`Nothing yet. Ask ${agent.display_name} to remind you of something, or to check in every morning.`),
      note,
      h('details', { class: 'routines-fold' }, h('summary', {}, `Behind the scenes (${behind.length})`),
        h('p', { class: 'when' }, 'How the agent keeps itself going. Nothing to set here.'),
        h('div', { class: 'rows' }, ...behind.map(behindRow))),
      h('details', { class: 'routines-fold' }, h('summary', {}, `About ${agent.display_name}`),
        h('div', { class: 'rows' },
          kv('Model', `${m.local ? 'Local' : 'Hosted'} · ${modelName(m.name)}${m.up === false ? ' · not answering' : ''}`),
          kv('Can reach', st.reach.allowed.join(', '), 'ok'),
          kv('Cannot reach', st.reach.blocked.join(', '), 'bad'))));
    reveal(detail);
  }

  root.replaceChildren(h('div', { class: 'page agents-page' }, agent ? agentBar(agent, 'about') : null,
    h('h1', { class: 'agents-title' }, agent ? `${agent.display_name}: routines` : 'Routines'),
    data.agents.length ? cards : none('No agents are set up yet.'), detail));
  detail.replaceChildren(loading('cards', 3));
  await load();
  return () => { alive = false; };
}
