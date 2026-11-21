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
const dayWord = (ts) => {
  const d = new Date(ts * 1000), today = new Date();
  const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  return days === 0 ? 'Today' : days === 1 ? 'Yesterday' : d.toLocaleDateString([], { weekday: 'long' });
};
const KIND_LABEL = { chat: 'Message', brain: 'Brain', ai: 'AI computer', agents: 'Between the agents', advisor: 'Claude Advisor',
  budget: 'Claude Advisor', conflict: 'Brain', agent_down: 'Agent', sync: 'Sync' };
function feedRow(i, TYPES) {
  const what = i.kind === 'brain' ? `${i.who}'s brain` : i.kind === 'chat' ? i.who : KIND_LABEL[i.kind] || '';
  const type = i.type || 'update';
  // Its type first, in its own colour: Needs you, Problem, Message or Update (Greg, 21 Nov).
  return h(i.link ? 'a' : 'div', { class: `feed-item type-${type} kind-${i.kind}${i.fresh ? ' fresh' : ''}${i.kind === 'ai' ? (i.up ? ' up' : ' down') : ''}`, href: i.link || null },
    h('span', { class: 'feed-dot', 'aria-hidden': 'true' }),
    h('span', { class: 'feed-body' },
      h('span', { class: 'feed-top' },
        h('span', { class: 'feed-tags' }, h('span', { class: `feed-type type-${type}` }, TYPES[type] || type), what && what !== TYPES[type] ? h('span', { class: 'feed-what' }, what) : null),
        h('span', { class: 'when' }, i.kind === 'agent_down' || i.kind === 'sync' ? 'Now' : i.dayOnly ? dayWord(i.ts) : when(i.ts))),
      h('strong', { class: 'feed-title' }, i.title),
      i.kind === 'sync' ? h('span', { class: 'feed-text' }, `Last synced ${i.last_seen ? when(i.last_seen).toLowerCase() : 'never'} · `
        + `${i.missing} change${i.missing === 1 ? '' : 's'} waiting. ${i.text}`) : i.text ? h('span', { class: 'feed-text' }, i.text) : null,
      i.outcome ? h('span', { class: 'feed-extra' }, h('span', { class: `pill ${i.tone}` }, i.outcome)) : null),
    i.fresh ? h('span', { class: 'visually-hidden' }, ' (new)') : null);
}

export async function noticeboardView(root) {
  const { feed, markSeen, TYPES, urgent } = await import('./notify.js');
  const row = (i) => feedRow(i, TYPES);
  let alive = true, timer = null, marked = false, seenAtOpen = null;
  const doing = {};             // request id -> message shown while the agent acts on an acceptance
  const waitingBox = h('div', { class: 'cards' });
  const newBox = h('div', { class: 'feed' }, loading());
  const oldBox = h('div', { class: 'feed' });
  // Needs you: requests to answer, then everything else to do or put right, before what just happened.
  const needsBox = h('div', { class: 'feed needs' });
  const needsLabel = h('h2', { class: 'label' }, 'Needs you');
  const waitSec = h('section', { 'aria-label': 'Needs you', hidden: true }, needsLabel, waitingBox, needsBox);
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
      const needs = items.filter(urgent), rest = items.filter((i) => !urgent(i));
      const toDo = waiting.length + needs.length;
      waitSec.hidden = !toDo;
      needsLabel.textContent = toDo ? `Needs you · ${toDo}` : 'Needs you';
      waitingBox.replaceChildren(...waiting.map(card));
      needsBox.replaceChildren(...needs.map(row));
      const fresh = rest.filter((i) => i.fresh), old = rest.filter((i) => !i.fresh).slice(0, 30);
      newBox.replaceChildren(...(fresh.length ? fresh.map(row) : [none(toDo ? 'Nothing else new.' : "You're all caught up.")]));
      oldSec.hidden = !old.length;
      oldBox.replaceChildren(...old.map(row));
      // Seen once you have opened it: the dots stay for this visit, the count on the bell goes.
      if (!marked) { marked = true; await markSeen(); }
      setBadge(toDo + items.filter((i) => i.unread).length);
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
