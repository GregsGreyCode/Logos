// Noticeboard screen: what the agents asked each other, and the requests waiting on you.
import { h, api, getJSON, when, none, loading } from './util.js';

const KIND = { fyi: 'FYI', question: 'Question', shared_change: 'Shared change', reminder: 'Reminder' };
const ASKS = { question: 'asks a question', shared_change: 'proposes a shared change', reminder: 'asks for a reminder' };

export function outcome(m) {
  if (m.kind === 'fyi') return ['Noted', 'plain'];
  if (m.status === 'open') return [m.to_mine ? 'Waiting on you' : `Waiting on ${m.to_name}`, m.to_mine ? 'wait' : 'plain'];
  if (m.status === 'expired') return ['Expired, no answer', 'bad'];
  const text = m.answer ? `: ${m.answer}` : '';
  return [`${m.status === 'accepted' ? 'Accepted' : 'Declined'}${text}`, m.status === 'accepted' ? 'good' : 'plain'];
}

export function setBadge(n) {
  // What waits on you plus what is new (notify.js). The side menu says "3 new"; the tab bar, the number.
  for (const el of document.querySelectorAll('.nb-badge')) {
    const small = el.classList.contains('tab-badge');
    el.textContent = n ? (small ? String(n) : `${n} new`) : '';
    if (small) el.setAttribute('aria-label', n ? `${n} new` : '');
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
        `${when(m.ts)}${m.urgent ? ' · urgent' : ''}`, { title: `${ASKS[m.kind]} of ${m.to_name} · request ${m.id}` })),
    h('div', { class: 'card-text' }, m.text));
  if (doing[m.id]) {
    el.append(h('div', { class: 'result' }, doing[m.id]));
    return el;
  }
  if (m.kind === 'question') {
    const id = `a${m.id}`;
    const input = h('input', { id, type: 'text', placeholder: 'Your answer' });
    el.append(h('label', { for: id, class: 'sr' }, 'Your answer'), input,
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
  // When it expires only matters once it is close (within a day).
  const soon = m.expires_at && (new Date(m.expires_at).getTime() - Date.now()) < 86400000;
  el.append(result, soon ? h('div', { class: 'when' }, `Expires ${when(m.expires_at).toLowerCase()}`) : null);
  return el;
}

// The Notifications tab (Greg, 21 Nov): what waits on you, then one feed of what happened (notify.js),
// newest first: New since you last looked, then Earlier (a week). Opening it marks everything seen.
const KIND_LABEL = { chat: 'Message', brain: 'Brain', ai: 'AI computer', agents: 'Between the agents' };
function feedRow(i) {
  const size = i.kind === 'brain' ? [i.added ? `+${i.added}` : '', i.removed ? `−${i.removed}` : ''].filter(Boolean).join(' ') : '';
  const what = i.kind === 'brain' ? `${i.who}'s brain` : KIND_LABEL[i.kind] || '';
  return h(i.link ? 'a' : 'div', { class: `feed-item kind-${i.kind}${i.fresh ? ' fresh' : ''}${i.kind === 'ai' ? (i.up ? ' up' : ' down') : ''}`, href: i.link || null },
    h('span', { class: 'feed-dot', 'aria-hidden': 'true' }),
    h('span', { class: 'feed-body' },
      h('span', { class: 'feed-top' }, h('span', { class: 'feed-what' }, what), h('span', { class: 'when' }, when(i.ts))),
      h('strong', { class: 'feed-title' }, i.title),
      i.text ? h('span', { class: 'feed-text' }, i.text) : null,
      size || i.outcome ? h('span', { class: 'feed-extra' }, size ? h('span', { class: 'when' }, size) : null,
        i.outcome ? h('span', { class: `pill ${i.tone}` }, i.outcome) : null) : null),
    i.fresh ? h('span', { class: 'visually-hidden' }, ' (new)') : null);
}

export async function noticeboardView(root) {
  const { feed, markSeen } = await import('./notify.js');
  let alive = true, timer = null, marked = false, seenAtOpen = null;
  const doing = {};             // request id -> message shown while the agent acts on an acceptance
  const waitingBox = h('div', { class: 'cards' });
  const newBox = h('div', { class: 'feed' }, loading());
  const oldBox = h('div', { class: 'feed' });
  const waitSec = h('section', { 'aria-label': 'Waiting on you', hidden: true }, h('h2', { class: 'label' }, 'Waiting on you'), waitingBox);
  const newSec = h('section', { 'aria-label': 'New' }, h('h2', { class: 'label' }, 'New'), newBox);
  const oldSec = h('section', { 'aria-label': 'Earlier', hidden: true }, h('h2', { class: 'label' }, 'Earlier this week'), oldBox);
  const error = h('div', { class: 'notice', hidden: true });
  const card = (m) => requestCard(m, doing, () => load());

  async function load() {
    try {
      const { items: raw, waiting, seen } = await feed();
      if (!alive) return;
      // New means new since you opened this page: it stays so while you are here, even once marked seen.
      if (seenAtOpen === null) seenAtOpen = seen;
      const items = raw.map((i) => ({ ...i, fresh: i.unread || i.ts > seenAtOpen }));
      error.hidden = true;
      for (const id of Object.keys(doing)) if (!waiting.some((w) => String(w.id) === id)) delete doing[id];
      waitSec.hidden = !waiting.length;
      waitingBox.replaceChildren(...waiting.map(card));
      const fresh = items.filter((i) => i.fresh), old = items.filter((i) => !i.fresh).slice(0, 30);
      newBox.replaceChildren(...(fresh.length ? fresh.map(feedRow) : [none(waiting.length ? 'Nothing else new.' : "You're all caught up.")]));
      oldSec.hidden = !old.length;
      oldBox.replaceChildren(...old.map(feedRow));
      // Seen once you have opened it: the dots stay for this visit, the count on the bell goes.
      if (!marked) { marked = true; await markSeen(); }
      setBadge(waiting.length + items.filter((i) => i.unread).length);
    } catch (e) {
      error.hidden = false;
      error.textContent = 'Could not load the notifications just now.';
    }
  }

  root.replaceChildren(h('div', { class: 'page notifications' },
    h('header', {}, h('h1', {}, 'Notifications')),
    error, waitSec, newSec, oldSec));
  await load();
  timer = setInterval(load, 15000);
  return () => { alive = false; clearInterval(timer); };
}
