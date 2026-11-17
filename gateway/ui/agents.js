// Agents screen: is each agent up, what it does on its own, what it runs on, what it can reach.
import { h, api, getJSON } from './util.js';

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
  const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  return dow === '*' ? `Nightly, ${at}` : `${days[Number(dow) % 7] || dow}, ${at}`;
}

export async function agentsView(root, [agentArg]) {
  let alive = true;
  let data = { agents: [], others: [] };
  try { data = await getJSON('/api/v2/agents'); } catch (e) { /* shown below */ }
  const agent = data.agents.find((a) => a.name === agentArg) || data.agents[0];
  const detail = h('div', { class: 'two-col' });

  const cards = h('div', { class: 'cards' },
    data.agents.map((a) => h('a', { href: `#/agents/${a.name}`, class: `card agent-card ${agent && a.name === agent.name ? 'picked' : ''}` },
      h('div', { class: 'card-head' }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), h('strong', {}, a.display_name)),
      h('span', { class: `status ${a.running ? 'up' : ''}` }, a.running ? 'Running' : 'Not running'),
      h('span', { class: 'when' }, a.shared ? 'Shared with the household' : 'Private to you'))),
    data.others.map((a) => h('div', { class: 'card agent-card other' },
      h('div', { class: 'card-head' }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), h('strong', {}, a.display_name)),
      h('span', { class: 'status' }, a.running ? 'Running' : 'Not running'),
      h('span', { class: 'when' }, 'Private to someone else. You can see that it is up, nothing more.'))));

  async function load() {
    if (!agent) return;
    detail.replaceChildren(h('p', { class: 'when' }, 'Loading…'));
    let st;
    try { st = await getJSON(`/api/v2/agents/${agent.name}/status`); } catch (e) {
      detail.replaceChildren(h('div', { class: 'notice' }, 'Could not load this agent.'));
      return;
    }
    if (!alive) return;
    const rows = (st.jobs || []).map((j) => h('tr', {},
      h('td', {}, j.label), h('td', { class: 'when' }, schedule(j.schedule)),
      h('td', {}, j.managed
        ? (j.last_run ? h('span', { class: j.last_ok ? 'ok' : 'bad' }, `${runTime(j.last_run)}, ${j.last_ok ? 'fine' : 'failed'}`) : h('span', { class: 'when' }, 'Not yet run'))
        : h('button', { type: 'button', class: 'btn quiet small-btn', onclick: async (e) => {
          e.target.disabled = true;
          const r = await api(`/api/v2/agents/${agent.name}/jobs/${j.id}/cancel`, { method: 'POST' });
          if (r.ok) load(); else { e.target.disabled = false; e.target.textContent = 'Could not cancel'; }
        } }, 'Cancel'))));
    const m = st.model;
    const kv = (k, v) => h('div', { class: 'kv' }, h('span', { class: 'when' }, k), h('span', {}, v));
    detail.replaceChildren(
      h('section', { class: 'card', 'aria-label': 'Scheduled jobs' },
        h('h2', {}, `What ${agent.display_name} does on its own`),
        st.jobs == null ? h('div', { class: 'notice' }, 'The agent did not answer, so its jobs could not be listed.')
          : h('div', { class: 'table-inline' }, h('table', { class: 'plain' },
            h('thead', {}, h('tr', {}, ['Job', 'When', 'Last run'].map((t) => h('th', { scope: 'col' }, t)))),
            h('tbody', {}, rows))),
        h('p', { class: 'when' }, 'Reminders the agent set for you can be cancelled here. The rest is how it keeps its brain in order.')),
      h('div', { class: 'stack' },
        h('section', { class: 'card', 'aria-label': 'Model and spend' },
          h('h2', {}, 'Model and spend'),
          kv('Model', `${m.local ? 'Local' : 'Hosted'} · ${m.name}`),
          m.up === false ? h('div', { class: 'kv' }, h('span', { class: 'bad strong' }, 'Model server'),
            h('span', {}, 'Not answering. The agent cannot reply or run its model jobs until it is back on.')) : null,
          kv('Paid providers', m.paid_providers.length ? m.paid_providers.join(', ') : 'None attached'),
          st.budget.monthly_usd == null ? kv('Budget', 'Not behind the model gateway')
            : kv('Monthly limit on paid models', `$${st.budget.monthly_usd.toFixed(2)}`),
          h('p', { class: 'when' }, m.paid_providers.length
            ? 'At the limit, paid calls are refused until next month. A running total is not shown here yet.'
            : 'Local replies are free, so nothing can be spent. The limit applies once a paid provider is attached.')),
        h('section', { class: 'card', 'aria-label': 'What it can reach' },
          h('h2', {}, 'What it can reach'),
          h('div', { class: 'kv' }, h('span', { class: 'ok strong' }, 'Allowed'), h('span', {}, st.reach.allowed.join(', '))),
          h('div', { class: 'kv' }, h('span', { class: 'bad strong' }, 'Blocked'), h('span', {}, st.reach.blocked.join(', '))),
          h('p', { class: 'when' }, 'Enforced by the sandbox, not by the agent.'))));
  }

  root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Agents'),
    data.agents.length || data.others.length ? cards : h('p', { class: 'when' }, 'No agents are set up yet.'), detail));
  await load();
  return () => { alive = false; };
}
