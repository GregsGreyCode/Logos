// Noticeboard screen: what the agents asked each other, and the requests waiting on you.
import { h, icon, ICONS, api, getJSON, when, none, loading, reveal } from './util.js';
import { peek, keep } from './cache.js';

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
  // What waits on you plus your agents' messages you have not read (notify.js countNew): the number alone, in the side menu
  // as in the tab bar (Greg, 23 Nov: "1 new" wrapped into a lump on the desktop). New updates (brain changes and the like)
  // are not counted, so the badge says in its tooltip and label what it counts (UI review, 28 Nov: it read as "unread").
  const c = typeof n === 'number' ? { total: n } : (n || { total: 0 });
  const total = c.total ?? ((c.needs || 0) + (c.messages || 0));
  const parts = [c.needs ? `${c.needs} need${c.needs === 1 ? 's' : ''} you` : '',
    c.messages ? `${c.messages} unread message${c.messages === 1 ? '' : 's'}` : ''].filter(Boolean);
  const words = parts.length ? parts.join(', ') : `${total} to look at`;
  for (const el of document.querySelectorAll('.nb-badge')) {
    el.textContent = total ? (total > 99 ? '99+' : String(total)) : '';
    el.setAttribute('aria-label', total ? words : '');
    el.title = total ? words : '';
    el.hidden = !total;
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
  el.append(result);                 // (append writes a null as the word "null": only what is there)
  if (soon) el.append(h('div', { class: 'when' }, `Expires ${when(m.expires_at).toLowerCase()}`));
  return el;
}

// A site an agent's sandbox refused (story E6): Allow once (an hour), Always allow, or Deny (asked again
// in a week at the soonest). Only the agent's own people get the question; the server checks that too.
// Greg, 28 Nov ("I can't gauge if it's safe"): the card says what the site is, who is really asking
// (a chat, a job, or the agent software in the background), whether it is needed, and offers to ask the
// agent why. One site asked by several agents at once is one card; it decides all of yours in it.
const DECIDED = { once: 'Allowed for an hour.', always: 'Always allowed.', deny: 'Denied. It will not ask again for a week.' };
const REC_CLASS = { needed: 'need', not_needed: 'no', ask: 'ask' };
export function whyText(i) {
  const at = new Date(i.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const site = i.port && Number(i.port) !== 443 ? `${i.host}:${i.port}` : i.host;
  return `Your sandbox stopped a connection to ${site} at ${at}. What were you trying to do, and do you need that site? `
    + 'Answer in one or two lines; do not try to reach it again for now.';
}
export function networkCard(i, reload) {
  const result = h('div', { class: 'result when', role: 'status' });
  const about = i.site || { known: false, domain: i.host, about: 'Not a site Logos knows.', rec: 'ask', recommend: 'Unknown: ask the agent', advice: '' };
  const agents = i.agents && i.agents.length ? i.agents : [{ agent: i.agent, who: i.who, rid: i.rid, asker: null }];
  const go = async (decision, btn) => {
    for (const b of el.querySelectorAll('button')) b.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    const r = await api(`/api/v2/network/${encodeURIComponent(i.rid)}/${decision}`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    btn.removeAttribute('aria-busy');
    if (!r.ok) {
      for (const b of el.querySelectorAll('button')) b.disabled = false;
      result.textContent = d.message || d.error || `Could not do that (HTTP ${r.status}).`;
      return;
    }
    result.textContent = d.message ? `${DECIDED[decision]} ${d.message}` : DECIDED[decision];
    setTimeout(reload, 1200);
  };
  const button = (decision, label, cls) => {
    const b = h('button', { type: 'button', class: cls, onclick: () => go(decision, b) }, label);
    return b;
  };
  // "Ask the agent why": the question goes into the agent's chat, which sends it when it opens.
  const askWhy = (a) => h('a', { class: 'btn small quiet net-why', href: `#/chat/${encodeURIComponent(a.agent)}`,
    onclick: async () => { const { handOver } = await import('./home.js'); handOver(a.agent, whyText(i)); } },
  agents.length > 1 ? `Ask ${a.who} why` : 'Ask the agent why');
  const said = (a) => (a.asker ? a.asker.text : 'The agent (what it was doing is not known)');
  // The same answer for every agent (the agent software's own fetch, as on 28 Nov) is said once.
  const same = agents.length > 1 && agents.every((a) => said(a) === said(agents[0]));
  const rows = same ? [{ ...agents[0], who: agents.length === 2 ? 'Both' : `All ${agents.length}` }] : agents;
  const askers = h('ul', { class: 'net-askers', 'aria-label': 'Who is asking' }, ...rows.map((a) => h('li', { class: `net-asker kind-${a.asker ? a.asker.kind : 'agent'}` },
    agents.length > 1 ? h('strong', {}, `${a.who}: `) : null, said(a))));
  const el = h('div', { class: 'card network-ask' },
    h('div', { class: 'card-head' }, h('span', { class: `dot ${i.agent}`, 'aria-hidden': 'true' }),
      h('strong', {}, i.title), h('span', { class: 'when' }, when(i.ts))),
    h('dl', { class: 'net-facts' },
      h('dt', {}, 'The site'),
      h('dd', {}, h('span', { class: 'net-about' }, about.about), ' ', h('span', { class: 'net-domain', title: i.host }, about.domain)),
      h('dt', {}, 'Who is asking'),
      h('dd', {}, askers),
      h('dt', {}, 'Logos says'),
      h('dd', {}, h('span', { class: `net-rec rec-${REC_CLASS[about.rec] || 'ask'}` }, about.recommend),
        about.advice ? h('span', { class: 'net-advice' }, ` ${about.advice}`) : null)),
    h('div', { class: 'row' }, button('once', 'Allow once', about.rec === 'not_needed' ? 'btn quiet' : 'btn'),
      button('always', 'Always allow', 'btn quiet'),
      button('deny', 'Deny', about.rec === 'not_needed' ? 'btn' : 'btn quiet')),
    h('div', { class: 'row net-whys' }, ...agents.map(askWhy)),
    result);
  return el;
}

// An agent that asked to be called something else (Greg, 28 Nov): nothing changes until one of its
// people taps Confirm here (gateway/agent_rename.py); the server checks the name again and that it is theirs.
export function renameCard(i, reload) {
  const result = h('div', { class: 'result when', role: 'status' });
  const go = async (decision, btn) => {
    for (const b of el.querySelectorAll('button')) b.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    const r = await api(`/api/v2/agent-rename/${encodeURIComponent(i.rid)}/${decision}`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    btn.removeAttribute('aria-busy');
    if (!r.ok) {
      for (const b of el.querySelectorAll('button')) b.disabled = false;
      result.textContent = d.message || d.error || `Could not do that (HTTP ${r.status}).`;
      return;
    }
    result.textContent = d.message || (decision === 'confirm' ? 'Renamed.' : 'It keeps its name.');
    setTimeout(reload, 2500);
  };
  const button = (decision, label, cls) => {
    const b = h('button', { type: 'button', class: cls, onclick: () => go(decision, b) }, label);
    return b;
  };
  const el = h('div', { class: 'card network-ask rename-ask' },
    h('div', { class: 'card-head' }, h('span', { class: `dot ${i.agent}`, 'aria-hidden': 'true' }),
      h('strong', {}, i.title), h('span', { class: 'when' }, when(i.ts))),
    h('div', { class: 'card-text' }, i.text),
    h('div', { class: 'row' }, button('confirm', 'Confirm', 'btn'), button('dismiss', 'Not now', 'btn quiet')),
    result);
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
const KIND_LABEL = { network: 'Network', network_done: 'Network', rename: 'Agent', rename_done: 'Agent', widget: 'Home widget', chat: 'Message', brain: 'Brain', ai: 'AI computer', agents: 'Between the agents', advisor: 'Claude Advisor',
  budget: 'Claude Advisor', conflict: 'Brain', agent_down: 'Agent', sync: 'Sync' };
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
function feedRow(i, TYPES, onOpen) {
  // A message's title already says who wrote it, so its second line is just the words.
  const source = i.source || (i.kind === 'brain' ? `${i.who}'s brain` : i.kind === 'chat' ? '' : KIND_LABEL[i.kind] || '');
  const type = i.type || 'update';
  // Only Problem and Message carry a label: an Update needs none, and every "needs" row sits under the Needs you heading,
  // so a "Needs you" chip on it only repeated the heading (UI review, 28 Nov).
  const chip = type === 'problem' || type === 'message' ? h('span', { class: `feed-type type-${type}` }, TYPES[type] || type) : null;
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

export async function noticeboardView(root, rest, state) {
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

  // The last list seen is drawn at once, then the fresh one (Greg, 23 Nov); every 15 s after that.
  async function load() {
    try {
      const f = await feed();
      if (!alive) return;
      keep('feed', { items: f.items, waiting: f.waiting });
      draw(f);
    } catch (e) {
      error.hidden = false;
      error.textContent = 'Could not load the notifications just now.';
    }
  }

  function draw({ items: raw, waiting }) {
    {
      // New stays new until you tap it, or Mark all read (Greg, 22 Nov: opening the tab no longer clears all).
      const items = raw.map((i) => (opened.has(i.id) ? { ...i, fresh: false } : i));
      error.hidden = true;
      for (const id of Object.keys(doing)) if (!waiting.some((w) => String(w.id) === id)) delete doing[id];
      const needs = items.filter(urgent), rest = items.filter((i) => !urgent(i)).slice(0, 40);
      const toDo = waiting.length + needs.length;
      waitSec.hidden = !toDo;
      needsLabel.textContent = toDo ? `Needs you · ${toDo}` : 'Needs you';
      const asks = (i) => i.kind === 'network' || i.kind === 'rename';      // answered right on the card
      waitingBox.replaceChildren(...waiting.map(card), ...needs.filter(asks).map((i) => (i.kind === 'rename' ? renameCard : networkCard)(i, load)));
      needsBox.replaceChildren(...needs.filter((i) => !asks(i)).map(row));
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
      setBadge({ needs: toDo, messages: items.filter((i) => !urgent(i) && i.type === 'message' && i.fresh).length });
    }
  }

  root.replaceChildren(h('div', { class: 'page notifications' },
    // A title like every screen's on a desktop (Greg, 23 Nov), with Mark all read in it; the phone's top bar names it.
    h('header', { class: 'page-band' }, h('h1', {}, 'Notifications'), allRead),
    error, h('div', { class: 'feed-head' }, caught), waitSec, days));
  if (state) state.refresh = load;                    // pulling down (app.js)
  const kept = peek('feed');
  if (kept && kept.v && Array.isArray(kept.v.items)) draw(kept.v);
  await load();
  timer = setInterval(load, 15000);
  return () => { alive = false; clearInterval(timer); };
}
