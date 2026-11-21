// Agents screen: is each agent up, what it does on its own, what it runs on, what it can reach.
import { modelName, h, api, getJSON, none, loading, icon, ICONS, shortName } from './util.js';
import { agentBar } from './agents-void.js';

function runTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const sameDay = d.toDateString() === new Date().toDateString();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return sameDay ? `Today ${time}` : `${d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })} ${time}`;
}

// "0 * * * *" and friends, in words, for the handful of shapes Logos uses.
function schedule(s) {
  if (/^once at /.test(s)) return runTime(s.slice(8).replace(' ', 'T'));
  const m = s.match(/^(\S+) (\S+) \* \* (\S+)$/);
  if (!m) return s;
  const [, min, hour, dow] = m;
  if (min === '*' && hour === '*') return 'Every minute';
  if (/^\*\/\d+$/.test(min) && hour === '*') return `Every ${min.slice(2)} minutes`;
  if (hour === '*') return 'Hourly';
  const at = `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`;
  const days = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays'];
  const named = { '1-5': 'Weekdays', '0,6': 'Weekends', '6,0': 'Weekends' };
  return dow === '*' ? `Nightly ${at}` : `${named[dow] || days[Number(dow) % 7] || dow} ${at}`;
}

export async function agentsView(root, [agentArg]) {
  let alive = true;
  let data = { agents: [], others: [] };
  root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Agents'), loading()));
  try { data = await getJSON('/api/v2/agents'); } catch (e) { /* shown below */ }
  const agent = data.agents.find((a) => a.name === agentArg) || data.agents[0];
  const detail = h('div', {});

  const cards = h('div', { class: 'cards agent-cards' },
    data.agents.map((a) => h('a', { href: `#/agents/${a.name}`, class: `card agent-card ${agent && a.name === agent.name ? 'picked' : ''}` },
      h('div', { class: 'card-head' }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), h('strong', {}, a.display_name)),
      h('span', { class: `status ${a.running ? 'up' : ''}` }, a.running ? 'Running' : 'Not running'),
      a.shared ? h('span', { class: 'when' }, 'Shared with the household') : null)),
    data.others.map((a) => h('div', { class: 'card agent-card other' },
      h('div', { class: 'card-head' }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), h('strong', {}, a.display_name),
        h('span', { class: 'chip-lock', title: 'Private to someone else: you can see that it is up, nothing more', 'aria-label': 'private' }, icon(ICONS.lock))),
      h('span', { class: 'status' }, a.running ? 'Running' : 'Not running'))));

  // P3/P4 on a phone: the agents as a row of chips (one dot each; others' dashed and private), with
  // the chosen one's state on a quiet line under them. The desktop keeps the cards.
  const chips = h('div', { class: 'phone-only' }, h('div', { class: 'agent-chips', role: 'list' },
    data.agents.map((a) => h('a', { href: `#/agents/${a.name}`, role: 'listitem', class: 'agent-chip',
      'aria-current': agent && a.name === agent.name ? 'true' : null },
      h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), shortName(a.display_name))),
    data.others.map((a) => h('span', { role: 'listitem', class: 'agent-chip other',
      title: 'Private to someone else: you can see that it is up, nothing more' },
      h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), shortName(a.display_name), h('span', { class: 'chip-lock', 'aria-label': 'private' }, icon(ICONS.lock))))),
    agent ? h('p', { class: 'when agent-state' }, h('span', { class: `status ${agent.running ? 'up' : 'down'}` },
      agent.running ? 'Running' : 'Not running'), agent.shared ? ' · shared with the household' : '') : null);

  async function load() {
    if (!agent) return;
    detail.replaceChildren(loading());
    let st;
    try { st = await getJSON(`/api/v2/agents/${agent.name}/status`); } catch (e) {
      detail.replaceChildren(h('div', { class: 'notice' }, 'Could not load this agent.'));
      return;
    }
    if (!alive) return;
    const rows = (st.jobs || []).map((j) => h('tr', {},
      h('td', {}, j.label), h('td', { class: 'when' }, schedule(j.schedule)),
      h('td', {}, j.managed
        ? (j.last_run ? h('span', { class: j.last_ok ? 'ok' : 'bad', title: j.last_ok ? 'Ran fine' : 'Failed' }, `${j.last_ok ? '✓' : '✕ failed'} ${runTime(j.last_run).replace(/^Today /, '')}`) : h('span', { class: 'when' }, 'Not yet run'))
        : h('button', { type: 'button', class: 'btn quiet small-btn', onclick: async (e) => {
          e.target.disabled = true;
          const r = await api(`/api/v2/agents/${agent.name}/jobs/${j.id}/cancel`, { method: 'POST' });
          if (r.ok) load(); else { e.target.disabled = false; e.target.textContent = 'Could not cancel'; }
        } }, 'Cancel'))));
    // A phone: each job is one line with a quiet second line, no repeated labels.
    const jobRows = (st.jobs || []).map((j) => h('div', { class: 'row-item' },
      h('span', { class: 'row-main' }, j.label),
      h('span', { class: 'row-meta' }, schedule(j.schedule), ' · ', j.managed
        ? (j.last_run ? h('span', { class: j.last_ok ? 'ok' : 'bad', title: j.last_ok ? 'Ran fine' : 'Failed' }, `${j.last_ok ? '✓' : '✕ failed'} ${runTime(j.last_run).replace(/^Today /, '')}`) : 'not yet run')
        : h('button', { type: 'button', class: 'linkish', onclick: async (e) => {
          e.target.disabled = true;
          const r = await api(`/api/v2/agents/${agent.name}/jobs/${j.id}/cancel`, { method: 'POST' });
          if (r.ok) load(); else { e.target.disabled = false; e.target.textContent = 'Could not cancel'; }
        } }, 'Cancel'))));
    const m = st.model;
    const kv = (k, v) => h('div', { class: 'kv' }, h('span', { class: 'when' }, k), h('span', {}, v));
    const setupRow = (label, value, tone) => h('div', { class: 'row-item' },
      h('span', { class: `row-meta ${tone || ''}` }, label), h('span', { class: 'row-main' }, value));
    const phone = h('div', { class: 'phone-only' }, h('div', { class: 'phone-stack' },
      h('section', { 'aria-label': 'Scheduled jobs' }, h('h2', { class: 'label' }, 'Scheduled'),
        st.jobs == null ? h('div', { class: 'notice' }, 'The agent did not answer, so its jobs could not be listed.')
          // The first few, the rest a tap away (Greg, 21 Nov: twelve jobs were a wall on a phone).
          : h('div', { class: 'rows' }, jobRows.length > 5
            ? [...jobRows.slice(0, 4), h('details', { class: 'more-jobs' }, h('summary', {}, `${jobRows.length - 4} more`), ...jobRows.slice(4))]
            : jobRows)),
      h('section', { 'aria-label': 'Setup' }, h('h2', { class: 'label' }, 'Setup'),
        h('div', { class: 'rows' },
          setupRow('Model', `${m.local ? 'Local' : 'Hosted'} · ${modelName(m.name)}${m.up === false ? ' · not answering' : ''}`),
          setupRow('Can reach', st.reach.allowed.join(', '), 'ok'),
          setupRow('Cannot reach', st.reach.blocked.join(', '), 'bad')))));
    detail.replaceChildren(phone, h('div', { class: 'wide-only two-col-wrap' }, h('div', { class: 'two-col' },
      h('section', { class: 'card', 'aria-label': 'Scheduled jobs' },
        h('h2', {}, `What ${agent.display_name} does on its own`),
        st.jobs == null ? h('div', { class: 'notice' }, 'The agent did not answer, so its jobs could not be listed.')
          : h('div', { class: 'table-inline' }, h('table', { class: 'plain' },
            h('thead', {}, h('tr', {}, ['Job', 'When', 'Last run'].map((t) => h('th', { scope: 'col' }, t)))),
            h('tbody', {}, rows)))),
      h('div', { class: 'stack' },
        h('section', { class: 'card', 'aria-label': 'Model' },
          h('h2', {}, 'Model'),
          kv('Model', `${m.local ? 'Local' : 'Hosted'} · ${modelName(m.name)}`),
          m.up === false ? h('div', { class: 'kv' }, h('span', { class: 'bad strong' }, 'Model server'),
            h('span', {}, 'Not answering')) : null,
          // The brain stays local: agents use only the local model. Paid providers and their
          // limit show only if one were ever attached again.
          m.paid_providers.length ? kv('Paid providers', m.paid_providers.join(', ')) : null,
          m.paid_providers.length && st.budget.monthly_usd != null
            ? kv('Monthly limit on paid models', `$${st.budget.monthly_usd.toFixed(2)}`) : null,
          m.paid_providers.length ? h('p', { class: 'when' }, 'At the limit, paid calls are refused until next month.') : null),
        h('section', { class: 'card', 'aria-label': 'What it can reach' },
          h('h2', {}, 'What it can reach'),
          h('div', { class: 'kv' }, h('span', { class: 'ok strong' }, 'Allowed'), h('span', {}, st.reach.allowed.join(', '))),
          h('div', { class: 'kv' }, h('span', { class: 'bad strong' }, 'Blocked'), h('span', {}, st.reach.blocked.join(', '))))))));
  }

  root.replaceChildren(h('div', { class: 'page agents-page' }, agent ? agentBar(agent, 'about') : null, h('h1', { class: 'agents-title' }, 'Agents'),
    data.agents.length || data.others.length ? [h('div', { class: 'wide-only' }, cards), h('div', { class: 'agents-chips' }, chips)] : none('No agents are set up yet.'), detail));
  await load();
  return () => { alive = false; };
}
