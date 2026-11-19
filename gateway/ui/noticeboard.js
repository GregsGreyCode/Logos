// Noticeboard screen: what the agents asked each other, and the requests waiting on you.
import { h, api, getJSON, when } from './util.js';

const KIND = { fyi: 'FYI', question: 'Question', shared_change: 'Shared change', reminder: 'Reminder' };
const ASKS = { question: 'asks a question', shared_change: 'proposes a shared change', reminder: 'asks for a reminder' };

function outcome(m) {
  if (m.kind === 'fyi') return ['Noted', 'plain'];
  if (m.status === 'open') return [m.to_mine ? 'Waiting on you' : `Waiting on ${m.to_name}`, m.to_mine ? 'wait' : 'plain'];
  if (m.status === 'expired') return ['Expired, no answer', 'bad'];
  const text = m.answer ? `: ${m.answer}` : '';
  return [`${m.status === 'accepted' ? 'Accepted' : 'Declined'}${text}`, m.status === 'accepted' ? 'good' : 'plain'];
}

export function setBadge(n) {
  // The side menu says "2 waiting"; the phone's tab bar has room for the number only.
  for (const el of document.querySelectorAll('.nb-badge')) {
    const small = el.classList.contains('tab-badge');
    el.textContent = n ? (small ? String(n) : `${n} waiting`) : '';
    if (small) el.setAttribute('aria-label', n ? `${n} waiting` : '');
    el.hidden = !n;
  }
}

async function decide(m, decision, text, card, doing, reload) {
  for (const b of card.querySelectorAll('button')) b.disabled = true;
  const r = await api(`/api/v2/noticeboard/${m.id}/answer`, { method: 'POST', body: { decision, text } });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    for (const b of card.querySelectorAll('button')) b.disabled = false;
    card.querySelector('.result').textContent = d.message || d.error || `Could not send (HTTP ${r.status}).`;
    return;
  }
  if (d.delegated) doing[m.id] = `${m.to_name} is doing this now and will answer when it is done.`;
  reload();
}

// One request waiting on the person, with its answer buttons. Shared by Noticeboard and Home.
export function requestCard(m, doing, reload) {
  const result = h('div', { class: 'result when' });
  const el = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('span', { class: `dot ${m.from}`, 'aria-hidden': 'true' }),
      h('strong', {}, m.from_name), h('span', { class: 'when' },
        `${ASKS[m.kind]} of ${m.to_name} · request ${m.id} · ${when(m.ts)}${m.urgent ? ' · urgent' : ''}`)),
    h('div', { class: 'card-text' }, m.text));
  if (doing[m.id]) {
    el.append(h('div', { class: 'result' }, doing[m.id]));
    return el;
  }
  if (m.kind === 'question') {
    const id = `a${m.id}`;
    const input = h('input', { id, type: 'text', placeholder: 'Your answer' });
    el.append(h('label', { for: id, class: 'field-label' }, 'Your answer, sent in your words'), input,
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn', onclick: () => {
          if (!input.value.trim()) { result.textContent = 'Type the answer to send, or decline.'; input.focus(); return; }
          decide(m, 'accepted', input.value, el, doing, reload);
        } }, 'Send answer'),
        h('button', { type: 'button', class: 'btn quiet', onclick: () => decide(m, 'declined', input.value, el, doing, reload) }, 'Decline')));
  } else {
    el.append(h('div', { class: 'row' },
      h('button', { type: 'button', class: 'btn', onclick: () => decide(m, 'accepted', '', el, doing, reload) },
        m.kind === 'reminder' ? 'Accept and set reminder' : 'Accept and make the change'),
      h('button', { type: 'button', class: 'btn quiet', onclick: () => decide(m, 'declined', '', el, doing, reload) }, 'Decline')));
  }
  el.append(result, h('div', { class: 'when' },
    `Expires ${when(m.expires_at).toLowerCase()} if nobody decides.`));
  return el;
}

export async function noticeboardView(root) {
  let alive = true, timer = null;
  const doing = {};             // request id -> message shown while the agent acts on an acceptance
  const waitingBox = h('div', { class: 'cards' });
  const tbody = h('tbody');
  const error = h('div', { class: 'notice', hidden: true });

  const card = (m) => requestCard(m, doing, () => load());

  async function load() {
    try {
      const { waiting, log } = await getJSON('/api/v2/noticeboard');
      if (!alive) return;
      error.hidden = true;
      for (const id of Object.keys(doing)) if (!waiting.some((w) => String(w.id) === id)) delete doing[id];
      setBadge(waiting.length);
      waitingBox.replaceChildren(...(waiting.length ? waiting.map(card)
        : [h('div', { class: 'when' }, 'Nothing is waiting on you.')]));
      tbody.replaceChildren(...log.map((m) => {
        const [label, tone] = outcome(m);
        return h('tr', {}, h('td', { class: 'nowrap when' }, when(m.ts)),
          h('td', { class: 'nowrap' }, `${m.from_name} → ${m.to_name}`),
          h('td', { class: 'when nowrap' }, KIND[m.kind] || m.kind), h('td', {}, m.text),
          h('td', {}, h('span', { class: `pill ${tone}` }, label)));
      }));
      if (!log.length) tbody.replaceChildren(h('tr', {}, h('td', { colspan: '5', class: 'when' }, 'No messages yet.')));
    } catch (e) {
      error.hidden = false;
      error.textContent = 'Could not load the noticeboard.';
    }
  }

  root.replaceChildren(h('div', { class: 'page' },
    h('header', {}, h('h1', {}, 'Noticeboard'),
      h('p', { class: 'when lead' }, 'What the agents have asked each other. Another agent can only ask. Nothing is answered or done until you decide.')),
    error,
    h('section', { 'aria-label': 'Waiting on you' }, h('h2', { class: 'label' }, 'Waiting on you'), waitingBox),
    h('section', { 'aria-label': 'Recent messages' }, h('h2', { class: 'label' }, 'Recent messages'),
      h('div', { class: 'table-box' }, h('table', {},
        h('thead', {}, h('tr', {}, ['When', 'From, to', 'Kind', 'Message', 'Outcome'].map((t) => h('th', { scope: 'col' }, t)))),
        tbody)),
      h('p', { class: 'when' }, 'Each agent can send another ten requests a day. Urgent ones reach the person in Element X within a minute.'))));
  await load();
  timer = setInterval(load, 15000);
  return () => { alive = false; clearInterval(timer); };
}
