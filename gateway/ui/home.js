// Home (S6.11): the first screen. What needs you, today's calendar, what the agents did, and an
// ask box that hands the question to Chat.
import { h, getJSON, when, none, loading, aiState } from './util.js';
import { requestCard } from './noticeboard.js';
import { warmAgents } from './chat.js';

const DAY = { weekday: 'long', day: 'numeric', month: 'long' };
const ASK_KEY = 'logos-ask';           // Chat picks the question up from here and sends it
const SEEN_KEY = 'logos-home-last';    // the last Home seen on this device, drawn at once while the new one loads

// "2026-11-19 10:30" -> "10:30"; an all-day event shows "All day".
const timeOf = (e) => (e.all_day ? 'All day' : String(e.start || '').slice(11, 16));

export async function homeView(root) {
  let alive = true, timer = null;
  const doing = {};
  const waitingBox = h('div', { class: 'stack home-waiting' }, loading());
  const todayBox = h('div', { class: 'home-days' });
  const statusWords = h('p', { class: 'when home-status', hidden: true, id: 'home-status-words' });
  const waitingLabel = h('h2', { class: 'label' }, 'Waiting on you');
  const waitingSec = h('section', { 'aria-label': 'Waiting on you' }, waitingLabel, waitingBox);
  // Terminal HUD (L6): a live strip of the same figures the Models screen shows. Hidden in the other looks.
  const strip = h('div', { class: 'hud-strip', 'aria-label': 'Live figures' });
  const cell = (label, value, unit, live) => h('div', { class: 'hud-cell' }, h('span', {}, label),
    h('strong', { class: live ? 'live' : null }, value, unit ? h('small', {}, ` ${unit}`) : null));
  async function showStrip() {
    if (document.documentElement.dataset.look !== 'hud') return;
    const [local, act] = await Promise.all([getJSON('/api/v2/models/local').catch(() => null),
      getJSON('/api/v2/activity').catch(() => null)]);
    if (!alive) return;
    const q = local && local.queue, sp = local && local.speed && (local.speed.alone || {});
    strip.replaceChildren(
      cell('Working', act ? String(act.working.length) : '–', act && act.working.length ? 'agents' : '', act && act.working.length),
      cell('Model', sp && sp.median_tokens_per_s ? String(Math.round(sp.median_tokens_per_s)) : '–', 'tok/s'),
      cell('Queue', q ? `${q.running}/${q.waiting}` : '–', 'run/wait'));
  }

  let data = null;

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
    const whose = (e) => (e.kind === 'busy' ? e.who
      : e.calendar === 'personal' ? 'Yours' : String((today.calendars || {})[e.calendar] || e.calendar).replace(/\s*\(.*\)\s*$/, ''));
    // A location that is a web address (a Maps link) shows as a short link, never the whole address:
    // one long unbreakable address made the whole page wider than a phone (Greg, 21 Nov).
    const place = (loc) => {
      if (!loc) return null;
      const url = String(loc).match(/https?:\/\/\S+/);
      if (!url) return ` · ${loc}`;
      const label = /maps|goo\.gl/.test(url[0]) ? 'Map' : 'Link';
      const rest = String(loc).replace(url[0], '').replace(/[\s,·-]+$/, '').trim();
      return h('span', {}, ` · ${rest ? `${rest} · ` : ''}`, h('a', { href: url[0], target: '_blank', rel: 'noopener' }, label));
    };
    const row = (e) => h('div', { class: 'home-row' },
      h('span', { class: 'home-time' }, timeOf(e)),
      h('span', { class: `dot ${e.calendar === 'personal' ? data.me : e.calendar}`, 'aria-hidden': 'true' }),
      h('span', { class: 'home-title' }, h('span', {}, e.title),
        h('span', { class: 'home-meta' }, whose(e), place(e.location))));
    // Today and Tomorrow side by side, on a phone too (Greg, 21 Nov): each its own column.
    const col = (label, list, empty) => h('div', { class: 'home-list home-day' },
      h('h3', { class: 'sub-label' }, label), ...(list.length ? list.map(row) : [none(empty)]));
    todayBox.replaceChildren(col('Today', todays, 'Nothing today.'), col('Tomorrow', later.slice(0, 4), 'Nothing yet.'));
  }


  async function load() {
    let nb = { waiting: [], log: [] };
    try {
      [data, nb] = await Promise.all([getJSON('/api/v2/home'), getJSON('/api/v2/noticeboard').catch(() => nb)]);
    } catch (e) {
      if (alive) { statusWords.textContent = 'Home could not be loaded just now.'; statusWords.hidden = false; }
      return;
    }
    if (!alive) return;
    try { localStorage.setItem(SEEN_KEY, JSON.stringify({ day: new Date().toDateString(), data, nb })); } catch (e) { /* full or private */ }
    paint(nb);
  }

  // The last Home seen today on this device, drawn at once; load() replaces it a moment later.
  function fromLast() {
    try {
      const last = JSON.parse(localStorage.getItem(SEEN_KEY) || 'null');
      if (!last || last.day !== new Date().toDateString() || !last.data || !last.data.agent) return;
      data = last.data;
      paint(last.nb || { waiting: [], log: [] });
    } catch (e) { /* nothing kept */ }
  }

  function paint(nb) {
    if (!data.agent) {
      root.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, 'No agent yet'),
        h('p', {}, 'No agent is set up for your account.')));
      return;
    }
    // Whether the AI computer is on shows on the logo now, on every screen (Greg, 21 Nov).
    aiState(data.model_up, data.model_off_since);
    waitingLabel.textContent = nb.waiting.length ? `Waiting on you · ${nb.waiting.length}` : 'Waiting on you';
    waitingBox.replaceChildren(...(nb.waiting.length ? nb.waiting.map((m) => requestCard(m, doing, load))
      : [none('Nothing is waiting on you.')]));
    showToday(data.today);
    // "Since yesterday" lives in Notifications now (Greg, 21 Nov).
    waitingSec.hidden = !nb.waiting.length;
    showStrip();
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
      h('div', { class: 'home-date-row' }, h('h1', { class: 'home-date' }, new Date().toLocaleDateString([], DAY))), statusWords),
    strip,
    waitingSec,
    h('section', { 'aria-label': 'Calendar' }, h('h2', { class: 'visually-hidden' }, 'Calendar'), todayBox),
    askForm));                                   // under Today and Tomorrow (Greg, 21 Nov)

  fromLast();
  await load();
  setTimeout(() => { if (alive) warmAgents(); }, 300);   // the Agents tab, ready before the swipe
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
