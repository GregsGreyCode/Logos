// Advisor screen (story A1): the questions your agent wants to ask Claude, each waiting for you.
// You see the exact text, pick the model, and press Send; nothing leaves the house before that.
import { h, getJSON, api, when, loading, none } from './util.js';

const usd = (n) => `$${Number(n || 0).toFixed(n && n < 0.1 ? 3 : 2)}`;
const MODELS = [['haiku', 'Haiku 5.5', 'quick facts, rewording'], ['sonnet', 'Sonnet 5.5', 'writing, planning, research'],
  ['opus', 'Opus 5.5', 'coding, long documents, hard questions']];

function pendingCard(p, refresh) {
  const pick = h('select', { 'aria-label': 'Model' }, MODELS.map(([v, n, what]) =>
    h('option', { value: v, selected: v === p.model }, `${n}: ${what}`)));
  const status = h('p', { class: 'when', role: 'status' });
  const act = (action) => async (ev) => {
    ev.target.disabled = true;
    const r = await api(`/api/v2/advisor/${p.id}/${action}`, { method: 'POST', body: { model: pick.value } });
    const body = await r.json().catch(() => ({}));
    status.textContent = r.ok ? (action === 'send' ? 'Sent to Claude. The answer goes back to your agent.' : 'Declined. Nothing was sent.')
      : (body.error || 'Could not do that.');
    setTimeout(refresh, 1500);
  };
  return h('div', { class: 'card' },
    h('p', { class: 'when' }, `${p.agent} asks (${p.kind}) · ${when(p.created)} · at most ${usd(p.estimate_max_usd)}`),
    h('blockquote', { class: 'advisor-q' }, p.question),
    h('p', { class: 'when' }, 'This exact text is what Claude will see, with nothing added except "You are helping someone at home."'),
    h('div', { class: 'actions' }, pick,
      h('button', { type: 'button', class: 'btn', onclick: act('send') }, 'Send'),
      h('button', { type: 'button', class: 'btn ghost', onclick: act('decline') }, 'Decline')),
    status);
}

async function post(path, body) {
  const r = await api(path, { method: 'POST', body });
  return { ok: r.ok, body: await r.json().catch(() => ({})) };
}

// A3: ask the admin for more; the admin sees everyone's budget and answers.
function budgetBox(d, refresh) {
  const out = [];
  const msg = h('p', { class: 'when', role: 'status' });
  if ((d.budget_requests || []).length) {
    out.push(h('p', { class: 'when' }, `You asked Greg for more: "${d.budget_requests[0].reason}". Waiting for his answer.`));
  } else if (d.person !== d.admin) {
    const reason = h('input', { type: 'text', maxlength: 300, placeholder: 'What is it for?', 'aria-label': 'Reason' });
    out.push(h('form', { class: 'gpu-form', onsubmit: async (ev) => {
      ev.preventDefault();
      const r = await post('/api/v2/advisor-budget/ask', { reason: reason.value });
      msg.textContent = r.ok ? 'Greg has been asked.' : (r.body.error || 'Could not ask.');
      setTimeout(refresh, 1200);
    } }, reason, h('button', { type: 'submit', class: 'btn ghost' }, 'Ask Greg for more'), msg));
  }
  if (d.everyone) {
    out.push(h('h2', { class: 'label' }, 'Everyone\'s budget'));
    for (const [person, info] of Object.entries(d.everyone)) {
      const name = NAMES[person] || person;               // their name, not their login (Greg, 21 Nov)
      const b = info.budget;
      const act = (action, dollars) => async () => {
        const r = await post(`/api/v2/advisor-budget/${person}/${action}`, { dollars });
        msg.textContent = r.ok ? `${name}: ${r.body.change}.` : (r.body.error || 'Could not change it.');
        setTimeout(refresh, 1200);
      };
      out.push(h('div', { class: 'card budget-row' },
        h('div', { class: 'budget-who' }, h('strong', {}, name), h('span', { class: 'when' }, `${usd(b.spent_usd)} of ${usd(b.limit_usd)} used`),
          ...(info.requests || []).map((q) => h('span', { class: 'when' }, `Asks for more: "${q.reason}"`))),
        h('div', { class: 'actions' },
          h('button', { type: 'button', class: 'btn ghost', onclick: act('raise', 10) }, 'Raise $10'),
          h('button', { type: 'button', class: 'btn ghost', onclick: act('reset', 0) }, 'Reset'),
          (info.requests || []).length ? h('button', { type: 'button', class: 'btn ghost', onclick: act('no', 0) }, 'Decline') : null)));
    }
    out.push(msg);
  }
  return h('div', {}, out);
}

// People's names for the budgets (the advisor knows their logins).
let NAMES = {};
export async function advisorView(root) {
  let alive = true;
  const names = getJSON('/api/v2/people').then((d) => { NAMES = Object.fromEntries((d.people || []).map((p) => [p.username, p.display_name])); }).catch(() => {});
  let timer = null;
  root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Claude Advisor'), loading()));
  async function paint() {
    let d;
    try { d = await getJSON('/api/v2/advisor'); } catch (e) {
      root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Claude Advisor'),
        h('div', { class: 'empty' }, h('h1', {}, e.status === 503 ? 'Not running yet' : 'Could not load it'),
          h('p', {}, e.status === 503 ? 'The Claude Advisor starts with Logos once it is set up on the server.' : 'Try again in a moment.'))));
      return;
    }
    if (!alive) return;
    const b = d.budget;
    const monthName = new Date(`${b.month}-01T12:00:00`).toLocaleDateString([], { month: 'long' });
    const parts = [h('header', {}, h('h1', {}, 'Claude Advisor'),
      h('p', { class: 'when lead' }, "Your agent's questions for Claude wait here for your yes."),
      h('p', { class: 'advisor-left' }, h('strong', {}, `${usd(b.left_usd)} left`), ` · ${usd(b.spent_usd)} of ${usd(b.limit_usd)} used in ${monthName}`))];
    if (!d.key) parts.push(h('div', { class: 'notice' }, 'No Anthropic key is set up yet, so nothing can be sent.'));
    parts.push(budgetBox(d, paint));
    if (d.pending.length) {
      parts.push(h('h2', { class: 'label' }, `Waiting for you (${d.pending.length})`));
      parts.push(...d.pending.map((p) => pendingCard(p, paint)));
    } else parts.push(none('Nothing is waiting for your yes.'));
    if (d.history.length) {
      parts.push(h('h2', { class: 'label' }, 'Log'));
      parts.push(...d.history.map((r) => h('details', { class: 'card' },
        h('summary', {}, `${when(r.created)} · ${r.status}${r.model ? ` · ${r.model}` : ''}${r.cost ? ` · ${usd(r.cost)}` : ''}`),
        h('p', { class: 'when' }, 'Question'), h('blockquote', { class: 'advisor-q' }, r.question),
        r.answer ? [h('p', { class: 'when' }, 'Answer'), h('div', { class: 'advisor-a' }, r.answer)] : null,
        r.note ? h('p', { class: 'when' }, r.note) : null)));
    }
    root.replaceChildren(h('div', { class: 'page' }, parts));
  }
  await names;
  await paint();
  timer = setInterval(() => { if (document.visibilityState === 'visible') paint(); }, 15000);
  return () => { alive = false; clearInterval(timer); };
}
