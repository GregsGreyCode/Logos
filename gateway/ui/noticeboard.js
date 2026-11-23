// Noticeboard screen: what the agents asked each other, and the requests waiting on you.
import { h, api, getJSON, when, none, loading, reveal } from './util.js';

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
  // What waits on you plus what is new (notify.js): the number alone, in the side menu as in the tab bar
  // (Greg, 23 Nov: "1 new" wrapped into a lump on the desktop).
  for (const el of document.querySelectorAll('.nb-badge')) {
    el.textContent = n ? (n > 99 ? '99+' : String(n)) : '';
    el.setAttribute('aria-label', n ? `${n} new` : '');
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

// The Notifications tab (Greg, 21 Nov; tidied 22 Nov): what needs you first, then one feed by day
// (Today, Yesterday, then weekdays), two lines a row. New since you last looked keeps its dot. Opening
// it marks everything seen.
const dayWord = (ts) => {
  const d = new Date(ts * 1000), today = new Date();
  const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  return days === 0 ? 'Today' : days === 1 ? 'Yesterday' : d.toLocaleDateString([], { weekday: 'long' });
};
const KIND_LABEL = { chat: 'Message', brain: 'Brain', ai: 'AI computer', agents: 'Between the agents', advisor: 'Claude Advisor',
  budget: 'Claude Advisor', conflict: 'Brain', agent_down: 'Agent', sync: 'Sync' };
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
function feedRow(i, TYPES, onOpen) {
  // A message's title already says who wrote it, so its second line is just the words.
  const source = i.source || (i.kind === 'brain' ? `${i.who}'s brain` : i.kind === 'chat' ? '' : KIND_LABEL[i.kind] || '');
  const type = i.type || 'update';
  // Only a type that asks something of you shows (Needs you, Problem, Message); an Update needs no label.
  const chip = type !== 'update' ? h('span', { class: `feed-type type-${type}` }, TYPES[type] || type) : null;
  const time = i.kind === 'agent_down' || i.kind === 'sync' ? 'Now' : i.dayOnly ? '' : hhmm(i.ts);
  const detail = i.kind === 'sync' ? `Last synced ${i.last_seen ? when(i.last_seen).toLowerCase() : 'never'} · `
    + `${i.missing} change${i.missing === 1 ? '' : 's'} waiting. ${i.text}` : (i.text || '');
  const second = [i.outcome, source && source !== i.title ? source : '', detail].filter(Boolean).join(' · ');
  // A tap opens it in place (the whole text, and a way to go to it) and marks it read (Greg, 22 Nov).
  const go = i.link ? h('a', { href: i.link, class: 'btn small feed-go' }, i.kind === 'chat' ? 'Open the chat' : i.kind === 'brain' ? 'Open the page'
    : i.kind === 'agents' ? 'Open the noticeboard' : i.kind === 'logos-update' ? 'Update' : 'Open') : null;
  const row = h('div', { class: `feed-item type-${type} kind-${i.kind}${i.fresh ? ' fresh' : ''}${i.kind === 'ai' ? (i.up ? ' up' : ' down') : ''}`,
    role: 'button', tabindex: '0', 'aria-expanded': 'false' },
    h('span', { class: 'feed-dot', 'aria-hidden': 'true' }),
    h('span', { class: 'feed-body' },
      h('span', { class: 'feed-top' }, chip, h('strong', { class: 'feed-title' }, i.title), h('span', { class: 'when' }, time)),
      second ? h('span', { class: 'feed-text' }, second) : null,
      h('span', { class: 'feed-more' }, i.dayOnly || !i.ts ? null : h('span', { class: 'when' }, when(i.ts)), go)),
    i.fresh ? h('span', { class: 'visually-hidden feed-new' }, ' (new)') : null);
  const toggle = (e) => {
    if (e.target.closest('a')) return;                    // the Open button goes on its own
    const open = row.classList.toggle('open');
    row.setAttribute('aria-expanded', String(open));
    if (row.classList.contains('fresh')) { row.classList.remove('fresh'); row.querySelector('.feed-new')?.remove(); onOpen(i); }
  };
  row.addEventListener('click', toggle);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); } });
  return row;
}

export async function noticeboardView(root) {
  const { feed, markRead, markAllRead, countNew, TYPES, urgent } = await import('./notify.js');
  const opened = new Set();                 // read during this visit (kept new-looking until the server agrees)
  const readOne = async (i) => { opened.add(i.id); await markRead(i); countNew().then(setBadge).catch(() => {}); };
  const row = (i) => feedRow(opened.has(i.id) ? { ...i, fresh: false } : i, TYPES, readOne);
  let alive = true, timer = null;
  const doing = {};             // request id -> message shown while the agent acts on an acceptance
  const waitingBox = h('div', { class: 'cards' });
  const needsBox = h('div', { class: 'feed needs' });
  const needsLabel = h('h2', { class: 'label' }, 'Needs you');
  const waitSec = h('section', { 'aria-label': 'Needs you', hidden: true }, needsLabel, waitingBox, needsBox);
  const caught = h('p', { class: 'feed-caught', hidden: true }, '✓ All caught up');
  const allRead = h('button', { type: 'button', class: 'linkish feed-allread', hidden: true, onclick: async () => {
    allRead.disabled = true; await markAllRead(); allRead.disabled = false; load();
  } }, 'Mark all read');
  const days = h('div', { class: 'feed-days' }, loading('rows', 6));
  let shown = false;
  const error = h('div', { class: 'notice', hidden: true });
  const card = (m) => requestCard(m, doing, () => load());

  async function load() {
    try {
      const { items: raw, waiting } = await feed();
      if (!alive) return;
      // New stays new until you tap it, or Mark all read (Greg, 22 Nov: opening the tab no longer clears all).
      const items = raw.map((i) => (opened.has(i.id) ? { ...i, fresh: false } : i));
      error.hidden = true;
      for (const id of Object.keys(doing)) if (!waiting.some((w) => String(w.id) === id)) delete doing[id];
      const needs = items.filter(urgent), rest = items.filter((i) => !urgent(i)).slice(0, 40);
      const toDo = waiting.length + needs.length;
      waitSec.hidden = !toDo;
      needsLabel.textContent = toDo ? `Needs you · ${toDo}` : 'Needs you';
      waitingBox.replaceChildren(...waiting.map(card));
      needsBox.replaceChildren(...needs.map(row));
      caught.hidden = !!toDo || rest.some((i) => i.fresh);
      allRead.hidden = !items.some((i) => i.fresh);
      // By day, newest first.
      const groups = [];
      for (const i of rest) {
        const d = dayWord(i.ts);
        if (!groups.length || groups[groups.length - 1][0] !== d) groups.push([d, []]);
        groups[groups.length - 1][1].push(i);
      }
      if (!shown) { shown = true; reveal(days); }
      days.replaceChildren(...(groups.length ? groups.map(([d, list]) => h('section', { 'aria-label': d },
        h('h2', { class: 'label' }, d), h('div', { class: 'feed' }, ...list.map(row)))) : [none('Nothing this week.')]));
      setBadge(toDo + items.filter((i) => urgent(i) ? false : i.type === 'message' && i.fresh).length);
    } catch (e) {
      error.hidden = false;
      error.textContent = 'Could not load the notifications just now.';
    }
  }

  root.replaceChildren(h('div', { class: 'page notifications' },
    h('header', { class: 'visually-hidden' }, h('h1', {}, 'Notifications')),
    error, h('div', { class: 'feed-head' }, caught, allRead), waitSec, days));
  await load();
  timer = setInterval(load, 15000);
  return () => { alive = false; clearInterval(timer); };
}
