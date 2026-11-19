// Home (S6.11): the first screen. What needs you, today's calendar, what the agents did, and an
// ask box that hands the question to Chat.
import { h, getJSON, when } from './util.js';
import { requestCard, setBadge } from './noticeboard.js';

const DAY = { weekday: 'long', day: 'numeric', month: 'long' };
const ASK_KEY = 'logos-ask';           // Chat picks the question up from here and sends it

// "2026-11-19 10:30" -> "10:30"; an all-day event shows "All day".
const timeOf = (e) => (e.all_day ? 'All day' : String(e.start || '').slice(11, 16));

export async function homeView(root) {
  let alive = true, timer = null;
  const doing = {};
  const waitingBox = h('div', { class: 'stack home-waiting' });
  const todayBox = h('div', { class: 'home-list' });
  const doneBox = h('div', { class: 'stack' });
  const status = h('div', { class: 'when home-status' });
  const waitingLabel = h('h2', { class: 'label' }, 'Waiting on you');

  let data = null;
  const obsidian = (path) => `obsidian://open?vault=${encodeURIComponent(data.agent.obsidian_vault)}`
    + `&file=${encodeURIComponent(path.replace(/\.md$/, ''))}`;
  const brainLink = (path) => `#/brain/${data.agent.name}/${path.split('/').map(encodeURIComponent).join('/')}`;
  const isPhone = () => window.matchMedia('(max-width: 720px)').matches;

  function showToday(today) {
    if (!today || today.error) {
      todayBox.replaceChildren(h('p', { class: 'when' }, today && today.error === 'no_calendar'
        ? 'Your agent has no calendar yet.' : 'The calendar could not be read just now.'));
      return;
    }
    const day = today.date;
    const rows = [...today.events.map((e) => ({ ...e, kind: 'event' })),
      ...today.busy.map((b) => ({ start: b.busy_from, end: b.busy_until, title: 'Busy', who: b.who, kind: 'busy' }))]
      .sort((a, b) => String(a.start).localeCompare(String(b.start)));
    const todays = rows.filter((e) => String(e.start).startsWith(day));
    const later = rows.filter((e) => !String(e.start).startsWith(day));
    const row = (e, label) => h('div', { class: 'home-row' },
      h('span', { class: 'home-time' }, label ?? timeOf(e)),
      h('span', { class: `dot ${e.calendar === 'personal' ? data.me : e.calendar}`, 'aria-hidden': 'true' }),
      h('span', { class: 'home-title' }, e.title, e.location ? h('span', { class: 'when' }, ` · ${e.location}`) : null),
      h('span', { class: 'when' }, e.kind === 'busy' ? e.who
        : e.calendar === 'personal' ? 'Yours' : (today.calendars || {})[e.calendar] || e.calendar));
    todayBox.replaceChildren(
      ...(todays.length ? todays.map((e) => row(e)) : [h('p', { class: 'when' }, 'Nothing in the calendar today.')]),
      ...later.slice(0, 4).map((e) => row(e, 'Tomorrow')));
  }

  function showDone(log) {
    const since = Date.now() / 1000 - 24 * 3600;
    const items = [];
    for (const c of data.brain_changes || []) {
      items.push({ when: c.when, line: [h('a', { href: isPhone() ? obsidian(c.path) : brainLink(c.path) }, c.title), ' changed in the brain'],
        who: data.agent.display_name });
    }
    for (const m of (log || []).filter((x) => (x.ts || 0) >= since && x.status !== 'open')) {
      items.push({ when: m.ts, line: [`${m.from_name} → ${m.to_name}: `, h('span', {}, m.text)],
        who: m.status === 'answered' && m.answer ? `Answered: ${m.answer}` : m.status });
    }
    items.sort((a, b) => b.when - a.when);
    doneBox.replaceChildren(...(items.length ? items.slice(0, 8).map((i) => h('div', { class: 'home-done' },
      h('span', {}, ...i.line), h('span', { class: 'when' }, `${i.who} · ${when(i.when)}`)))
      : [h('p', { class: 'when' }, 'Nothing new since yesterday.')]));
  }

  async function load() {
    let nb = { waiting: [], log: [] };
    try {
      [data, nb] = await Promise.all([getJSON('/api/v2/home'), getJSON('/api/v2/noticeboard').catch(() => nb)]);
    } catch (e) {
      if (alive) status.textContent = 'Home could not be loaded just now.';
      return;
    }
    if (!alive) return;
    if (!data.agent) {
      root.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, 'No agent yet'),
        h('p', {}, 'No agent is set up for your account.')));
      return;
    }
    const offSince = data.model_off_since
      ? new Date(data.model_off_since * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    status.replaceChildren(h('span', { class: `status ${data.model_up === false ? 'down' : 'up'}` },
      data.model_up === false
        ? `LM Studio offline${offSince ? ` since ${offSince}` : ''}: the agents will answer what's waiting when it's back`
        : 'Model server on'));
    setBadge(nb.waiting.length);
    waitingLabel.textContent = nb.waiting.length ? `Waiting on you · ${nb.waiting.length}` : 'Waiting on you';
    waitingBox.replaceChildren(...(nb.waiting.length ? nb.waiting.map((m) => requestCard(m, doing, load))
      : [h('p', { class: 'when' }, 'Nothing is waiting on you.')]));
    showToday(data.today);
    showDone(nb.log);
    ask.placeholder = `Ask ${data.agent.display_name}…`;
  }

  // The ask box: the question goes to Chat, which sends it in a new conversation.
  const ask = h('input', { id: 'home-ask', type: 'text', placeholder: 'Ask your agent…', autocomplete: 'off' });
  const askForm = h('form', { class: 'home-ask', onsubmit: (e) => {
    e.preventDefault();
    const text = ask.value.trim();
    if (!text || !data || !data.agent) return;
    try { sessionStorage.setItem(ASK_KEY, JSON.stringify({ agent: data.agent.name, text })); } catch (err) { /* sent below anyway */ }
    location.hash = `#/chat/${data.agent.name}/${Math.random().toString(36).slice(2, 12)}`;
  } }, h('label', { for: 'home-ask', class: 'sr' }, 'Ask your agent'), ask,
    h('button', { type: 'submit', class: 'btn' }, 'Ask'));

  root.replaceChildren(h('div', { class: 'page home' },
    h('header', { class: 'home-head' },
      h('h1', { class: 'home-date' }, new Date().toLocaleDateString([], DAY)), status),
    askForm,
    h('section', { 'aria-label': 'Waiting on you' }, waitingLabel, waitingBox),
    h('section', { 'aria-label': 'Today' }, h('h2', { class: 'label' }, 'Today'), todayBox),
    h('section', { 'aria-label': 'What happened' }, h('h2', { class: 'label' }, 'Since yesterday'), doneBox)));

  await load();
  timer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, 60000);
  return () => { alive = false; clearInterval(timer); };
}

// Chat calls this when it opens: a question asked on Home, still to be sent.
export function takeAsk(agent) {
  try {
    const v = JSON.parse(sessionStorage.getItem(ASK_KEY) || 'null');
    if (v && v.agent === agent && v.text) { sessionStorage.removeItem(ASK_KEY); return v.text; }
  } catch (e) { /* nothing waiting */ }
  return null;
}
