// Home (S6.11): the first screen. What needs you, today's calendar, what the agents did, and an
// ask box that hands the question to Chat.
import { h, api, getJSON, when, none, loading, aiState } from './util.js';
import { getPref, setPref } from './prefs.js';
import { requestCard } from './noticeboard.js';
import { warmAgents } from './chat.js';
import { unreadFromAgents } from './notify.js';

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
  // Greg, 22 Nov: the figures (agents working, AI speed, queue) left Home; they are for whoever runs
  // Logos (Models), not for the day. In their place, what helps Béré as much as Greg: what is next,
  // and the messages from your agents and groups you have not read.
  const nextUp = h('div', { class: 'home-next', hidden: true });
  const msgBox = h('div', { class: 'home-msgs' });
  const msgSec = h('section', { class: 'home-messages', 'aria-label': 'Messages', hidden: true },
    h('h2', { class: 'label' }, 'Messages'), msgBox);
  async function showMessages() {
    let items = [];
    try {
      const agents = (await getJSON('/api/v2/agents')).agents || [];
      items = (await unreadFromAgents(agents)).sort((a, b) => b.ts - a.ts);
    } catch (e) { return; }
    if (!alive) return;
    // One row per chat (its newest unread message), at most three.
    const seen = new Set();
    const rows = items.filter((i) => !seen.has(i.link) && seen.add(i.link)).slice(0, 3);
    msgSec.hidden = !rows.length;
    msgBox.replaceChildren(...rows.map((i) => h('a', { class: 'home-msg', href: i.link },
      h('span', { class: 'home-msg-head' }, h('strong', {}, i.title), h('span', { class: 'when' }, new Date(i.ts * 1000).toDateString() === new Date().toDateString()
        ? new Date(i.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : when(i.ts))),
      h('span', { class: 'home-msg-text' }, i.text))));
  }
  // "Next · 15:00 Call Maman · in 40 min": the next timed event today, while it is still to come.
  function showNext(today) {
    const now = new Date();
    const next = (today && !today.error ? today.events : []).filter((e) => !e.all_day && String(e.start).startsWith(today.date))
      .map((e) => ({ e, at: new Date(String(e.start).replace(' ', 'T')) })).filter((x) => x.at > now)
      .sort((a, b) => a.at - b.at)[0];
    nextUp.hidden = !next;
    if (!next) return;
    const mins = Math.round((next.at - now) / 60000);
    const inWords = mins < 60 ? `in ${mins} min` : mins < 120 ? 'in an hour' : `in ${Math.round(mins / 60)} hours`;
    nextUp.replaceChildren(h('span', { class: 'label' }, 'Next'), h('strong', {}, `${timeOf(next.e)} ${next.e.title}`), h('span', { class: 'when' }, inWords));
  }

  let data = null;

  // The household's shared lists (Greg, 21 Nov), from the copy the household agent keeps in its brain
  // (lists.md, refreshed every 15 minutes): each list, how many things are on it, and the first few.
  // The Home Assistant app's To-do screen (its homeassistant://navigate links), through Android's intent so
  // Chrome opens the app; without the app it falls back to the copy in Logos.
  // Each card opens its own list (?entity_id=todo.…, from the copy's hidden note), else the To-do screen.
  const haTodo = (id) => `intent://navigate/todo${id ? `?entity_id=${encodeURIComponent(id)}` : ''}#Intent;scheme=homeassistant;`
    + 'package=io.homeassistant.companion.android;'
    + `S.browser_fallback_url=${encodeURIComponent(`${location.origin}/#/brain/household/lists.md`)};end`;
  const listsBox = h('div', { class: 'home-lists-row' });
  const listsSec = h('section', { class: 'home-lists', 'aria-label': 'Lists', hidden: true },
    h('h2', { class: 'label' }, 'Lists'), listsBox);
  async function showLists() {
    let page;
    try { page = await getJSON('/api/v2/agents/household/brain/page?path=lists.md'); } catch (e) { return; }
    if (!alive || !page || !page.text) return;
    const lists = [];
    for (const line of String(page.text).split('\n')) {
      const head = line.match(/^##\s+(.+)/);
      if (head) { lists.push({ name: head[1].trim(), items: [], id: '' }); continue; }
      const id = line.match(/^<!--\s*(todo\.[\w-]+)\s*-->/);
      if (id && lists.length) { lists[lists.length - 1].id = id[1]; continue; }
      const item = line.match(/^- \[ \]\s+(.+)/);
      if (item && lists.length) lists[lists.length - 1].items.push(item[1].replace(/\s*\(due [^)]*\)\s*$/, '').trim());
    }
    const shown = lists.filter((l) => l.items.length);
    listsSec.hidden = !lists.length;
    // On a phone a list opens in the Home Assistant app, where the lists live (Greg, 21 Nov); a desktop has
    // no app, so there it opens the copy in Logos.
    const phone = window.matchMedia('(max-width: 599px)').matches;
    listsBox.replaceChildren(...(shown.length ? shown.map((l) => h('a', { class: 'home-list-card', href: phone ? haTodo(l.id) : '#/brain/household/lists.md',
      title: phone ? `Open ${l.name} in Home Assistant` : 'Open the lists' },
      h('span', { class: 'home-list-head' }, h('strong', {}, l.name), h('span', { class: 'home-list-count' }, String(l.items.length))),
      h('span', { class: 'home-list-items' }, l.items.slice(0, 3).join(' · ') + (l.items.length > 3 ? ` · +${l.items.length - 3}` : ''))))
      : [none('Every list is empty.')]));
  }

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
    // One line an event (Greg, 22 Nov): the time, its colour dot, the title, then whose and where at the
    // end. Today's that are over are dimmed.
    const now = new Date();
    const over = (e) => !e.all_day && String(e.start).startsWith(day) && new Date(String(e.end || e.start).replace(' ', 'T')) < now;
    const row = (e) => h('div', { class: `home-row home-line${over(e) ? ' over' : ''}` },
      h('span', { class: 'home-time' }, timeOf(e)),
      h('span', { class: `dot ${e.calendar === 'personal' ? data.me : e.calendar}`, 'aria-hidden': 'true' }),
      h('span', { class: 'home-title' }, e.title),
      h('span', { class: 'home-meta' }, whose(e), place(e.location)));
    // One list, Today then Tomorrow (Greg, 22 Nov: no longer side by side, which left half the width
    // empty whenever one day had more on than the other).
    const part = (label, list, empty) => [h('h3', { class: 'sub-label' }, label), ...(list.length ? list.map(row) : [none(empty)])];
    todayBox.replaceChildren(h('div', { class: 'home-list home-agenda' },
      ...part('Today', todays, 'Nothing today.'), ...part('Tomorrow', later.slice(0, 6), 'Nothing yet.')));
  }


  // ── Your choice of widgets (Greg, 22 Nov): the calendar always; the rest as each person picks ─────
  const WIDGETS = [['next', 'Next', 'Your next event today and how long until it'], ['weather', 'Weather', 'Today and tomorrow where you choose'],
    ['messages', 'Messages', 'Unread from your agents and groups'], ['coming', 'Coming up', 'Birthdays and events in the next two weeks'],
    ['lights', 'Lights', 'Which rooms are lit, and All off'], ['lists', 'Lists', 'The household\'s shared lists']];
  const DEFAULT_WIDGETS = ['next', 'messages', 'lists'];
  const chosen = () => { const w = getPref('widgets', null); return Array.isArray(w) ? w : DEFAULT_WIDGETS; };
  const on = (id) => chosen().includes(id);
  function applyWidgets() {
    if (!on('next')) nextUp.hidden = true;
    if (!on('messages')) msgSec.hidden = true;
    if (!on('lists')) listsSec.hidden = true;
    weatherSec.hidden = !on('weather');
    lightsSec.hidden = !on('lights');
    comingSec.hidden = !on('coming');
  }

  // Weather: one card, now and today, then tomorrow (Open-Meteo, through Logos; only the place's rounded position goes out).
  const SKY = (c) => (c === 0 ? ['☀️', 'Clear'] : c <= 2 ? ['⛅', 'Partly cloudy'] : c === 3 ? ['☁️', 'Cloudy'] : c <= 48 ? ['🌫️', 'Fog']
    : c <= 57 ? ['🌦️', 'Drizzle'] : c <= 67 ? ['🌧️', 'Rain'] : c <= 77 ? ['🌨️', 'Snow'] : c <= 82 ? ['🌦️', 'Showers']
      : c <= 86 ? ['🌨️', 'Snow showers'] : ['⛈️', 'Thunder']);
  const deg = (t) => (t === null || t === undefined ? '–' : `${Math.round(t)}°`);
  const weatherBox = h('div', { class: 'home-weather' });
  const weatherSec = h('section', { class: 'home-weather-sec', 'aria-label': 'Weather', hidden: true }, weatherBox);
  async function showWeather() {
    const w = await getJSON('/api/v2/weather').catch(() => ({ error: 'weather_unavailable' }));
    if (!alive) return;
    if (w.error === 'no_place') { weatherBox.replaceChildren(h('p', { class: 'when' }, 'Weather: choose your town below (Choose what Home shows).')); return; }
    if (w.error) { weatherBox.replaceChildren(h('p', { class: 'when' }, `The weather could not be read just now${w.place ? ` for ${w.place}` : ''}.`)); return; }
    const [icon, words] = SKY(w.now.code);
    const [tIcon, tWords] = SKY(w.tomorrow.code);
    weatherBox.replaceChildren(
      h('span', { class: 'home-weather-now' }, h('span', { class: 'home-weather-icon', 'aria-hidden': 'true' }, icon), h('strong', {}, deg(w.now.temp))),
      h('span', { class: 'home-weather-text' },
        h('span', {}, `${words} · today ${deg(w.today.high)} / ${deg(w.today.low)}${w.today.rain ? ` · ${w.today.rain}% rain` : ''}`),
        h('span', { class: 'when' }, `Tomorrow ${tIcon} ${tWords.toLowerCase()} ${deg(w.tomorrow.high)} / ${deg(w.tomorrow.low)}${w.tomorrow.rain ? ` · ${w.tomorrow.rain}% rain` : ''} · ${w.place.split(',')[0]}`)));
  }

  // Lights: each room, lit or not; a tap switches it (on if all off, else off), and All off.
  const lightsBox = h('div', { class: 'home-lights' });
  const lightsNote = h('p', { class: 'when', role: 'status' });
  const lightsSec = h('section', { class: 'home-lights-sec', 'aria-label': 'Lights', hidden: true },
    h('div', { class: 'home-sec-head' }, h('h2', { class: 'label' }, 'Lights'),
      h('button', { type: 'button', class: 'linkish', onclick: () => switchLights(['everything'], false) }, 'All off')), lightsBox, lightsNote);
  async function switchLights(targets, value) {
    lightsNote.textContent = '';
    for (const b of lightsSec.querySelectorAll('button')) b.disabled = true;
    const r = await api('/api/v2/home/lights', { method: 'POST', body: { targets, on: value } }).catch(() => null);
    const d = r ? await r.json().catch(() => ({})) : {};
    if (!r || !r.ok) lightsNote.textContent = d.message || 'The lights did not answer just now.';
    drawLights(d.rooms ? d : await getJSON('/api/v2/home/lights').catch(() => ({})));
  }
  function drawLights(d) {
    if (!d || d.error || !d.rooms) { lightsBox.replaceChildren(h('p', { class: 'when' }, d && d.error === 'no_house' ? 'No agent here can work the lights.' : 'The lights could not be read just now.')); return; }
    lightsBox.replaceChildren(...d.rooms.map((r) => h('button', { type: 'button', class: `home-light${r.on ? ' lit' : ''}`, 'aria-pressed': String(!!r.on),
      title: r.on ? `Turn ${r.room} off` : `Turn ${r.room} on`, onclick: () => switchLights([r.room], !r.on) },
    h('span', { class: 'home-light-name' }, r.room), h('span', { class: 'when' }, r.on ? `${r.on} of ${r.of} on` : 'off'))));
  }
  async function showLights() { const d = await getJSON('/api/v2/home/lights').catch(() => ({ error: 'x' })); if (alive) drawLights(d); }

  // Coming up: the next two weeks after tomorrow, one line each (all-day ones, like birthdays, too).
  const comingBox = h('div', { class: 'home-list home-agenda' });
  const comingSec = h('section', { class: 'home-coming', 'aria-label': 'Coming up', hidden: true }, h('h2', { class: 'label' }, 'Coming up'), comingBox);
  async function showComing() {
    const d = await getJSON('/api/v2/home/coming').catch(() => ({ error: 'x' }));
    if (!alive) return;
    if (d.error) { comingBox.replaceChildren(h('p', { class: 'when' }, d.error === 'no_calendar' ? 'No calendar yet.' : 'The calendar could not be read just now.')); return; }
    const events = (d.events || []).slice().sort((a, b) => String(a.start).localeCompare(String(b.start))).slice(0, 8);
    const day = (e) => new Date(`${String(e.start).slice(0, 10)}T12:00:00`).toLocaleDateString([], { weekday: 'short', day: 'numeric' });
    const whose = (e) => (e.calendar === 'personal' ? 'Yours' : String((d.calendars || {})[e.calendar] || e.calendar).replace(/\s*\(.*\)\s*$/, ''));
    comingBox.replaceChildren(...(events.length ? events.map((e) => h('div', { class: 'home-row home-line' },
      h('span', { class: 'home-time' }, day(e)),
      h('span', { class: `dot ${e.calendar === 'personal' ? data.me : e.calendar}`, 'aria-hidden': 'true' }),
      h('span', { class: 'home-title' }, `${e.all_day ? '' : `${String(e.start).slice(11, 16)} `}${e.title}`),
      h('span', { class: 'home-meta' }, whose(e)))) : [none('Nothing in the next two weeks.')]));
  }

  // "Choose what Home shows": tick the widgets; Weather asks for a town (found by name, kept with your login).
  const choosePanel = h('section', { class: 'home-choose', 'aria-label': 'Choose what Home shows', hidden: true });
  const chooseLink = h('button', { type: 'button', class: 'linkish home-choose-link', onclick: () => {
    if (!choosePanel.hidden) { choosePanel.hidden = true; return; }
    drawChoose(); choosePanel.hidden = false; choosePanel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  } }, 'Choose what Home shows');
  function drawChoose() {
    const ticks = WIDGETS.map(([id, label, help]) => h('label', { class: 'ng-pick' },
      h('input', { type: 'checkbox', checked: on(id) ? '' : null, onchange: async (e) => {
        const next = WIDGETS.map(([w]) => w).filter((w) => (w === id ? e.target.checked : on(w)));
        await setPref('widgets', next);
        applyWidgets();
        if (e.target.checked) { if (id === 'messages') showMessages(); if (id === 'lists') showLists(); if (id === 'weather') showWeather();
          if (id === 'lights') showLights(); if (id === 'coming') showComing(); if (id === 'next') showNext(data.today); }
      } }), h('span', {}, label, h('small', { class: 'when' }, ` ${help}`))));
    const place = getPref('weather_place', null);
    const q = h('input', { type: 'search', id: 'weather-q', placeholder: 'Town or city', autocomplete: 'off', value: place ? place.name.split(',')[0] : '' });
    const found = h('div', { class: 'ng-picks' });
    const find = async () => {
      found.replaceChildren(h('p', { class: 'when' }, 'Looking…'));
      const r = await getJSON(`/api/v2/weather/places?q=${encodeURIComponent(q.value.trim())}`).catch(() => ({ places: [] }));
      found.replaceChildren(...((r.places || []).length ? r.places.map((p) => h('button', { type: 'button', class: 'btn quiet small', onclick: async () => {
        await setPref('weather_place', p); drawChoose(); showWeather();
      } }, p.name)) : [h('p', { class: 'when' }, 'No place by that name.')]));
    };
    choosePanel.replaceChildren(h('h2', { class: 'label' }, 'Choose what Home shows'), ...ticks,
      h('label', { for: 'weather-q', class: 'field-label' }, `Weather for${place ? `: ${place.name}` : ''}`),
      h('form', { class: 'row', onsubmit: (e) => { e.preventDefault(); find(); } }, q, h('button', { type: 'submit', class: 'btn quiet small' }, 'Find')),
      found, h('p', { class: 'when' }, 'Only the town\'s position (to about a kilometre) is sent to the weather service.'));
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
    showNext(data.today);
    applyWidgets();
    // "Since yesterday" lives in Notifications now (Greg, 21 Nov).
    waitingSec.hidden = !nb.waiting.length;
    if (on('messages')) showMessages();
    if (on('lists')) showLists();
    if (on('weather')) showWeather();
    if (on('lights')) showLights();
    if (on('coming')) showComing();
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
    waitingSec,
    weatherSec,
    h('section', { 'aria-label': 'Calendar', class: 'home-cal' }, h('h2', { class: 'visually-hidden' }, 'Calendar'), nextUp, todayBox),
    msgSec,
    comingSec,
    lightsSec,
    listsSec,
    chooseLink, choosePanel,
    askForm));                                   // at the bottom (Greg, 21 Nov)                                   // under Today and Tomorrow (Greg, 21 Nov)

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
