// Home (S6.11): the first screen. What needs you, today's calendar, what the agents did, and an
// ask box that hands the question to Chat.
import * as G from './home-grid.js';
import { swr, forget, peek, clearAll } from './cache.js';
import { widget as homelabWidget, BAND, setupSheet } from './widgets.js';
import { createRecorder } from './voice.js';
import * as mx from './mx.js';
import { h, api, getJSON, when, none, loading, aiState, reveal, icon, ICONS } from './util.js';
import { getPref, setPref } from './prefs.js';
import { requestCard } from './noticeboard.js';
import { warmAgents } from './chat.js';
import { unreadFromAgents } from './notify.js';

const DAY = { weekday: 'long', day: 'numeric', month: 'long' };
const ASK_KEY = 'logos-ask';           // Chat picks the question up from here and sends it
const SEEN_KEY = 'logos-home-last';    // the last Home seen on this device, drawn at once while the new one loads

// "2026-11-19 10:30" -> "10:30"; an all-day event shows "All day".
const timeOf = (e) => (e.all_day ? 'All day' : String(e.start || '').slice(11, 16));

export async function homeView(root, rest, state) {
  let alive = true, timer = null, painted = false;
  const doing = {};
  const waitingBox = h('div', { class: 'stack home-waiting' }, loading());
  const todayBox = h('div', { class: 'home-days' }, loading('rows', 4));
  const statusWords = h('p', { class: 'when home-status', hidden: true, id: 'home-status-words' });
  const waitingLabel = h('h2', { class: 'label' }, 'Waiting on you');
  // Hidden until it is known that something waits: usually nothing does, so no placeholder that then vanishes.
  const waitingSec = h('section', { 'aria-label': 'Waiting on you', hidden: true }, waitingLabel, waitingBox);
  // Greg, 22 Nov: the figures (agents working, AI speed, queue) left Home; they are for whoever runs
  // Logos (Models), not for the day. In their place, what helps Béré as much as Greg: what is next,
  // and the messages from your agents and groups you have not read.
  const nextUp = h('div', { class: 'home-next', hidden: true });
  const msgBox = h('div', { class: 'home-msgs' });
  const msgSec = h('section', { class: 'home-messages', 'aria-label': 'Messages', hidden: true },
    h('h2', { class: 'label' }, 'Messages'), msgBox);
  let msgRows = [];
  async function showMessages() {
    let items = [];
    try {
      const agents = (await getJSON('/api/v2/agents')).agents || [];
      items = (await unreadFromAgents(agents)).sort((a, b) => b.ts - a.ts);
    } catch (e) { return; }
    if (!alive) return;
    // One row per chat (its newest unread message), at most three.
    const seen = new Set();
    msgRows = items.filter((i) => !seen.has(i.link) && seen.add(i.link)).slice(0, 3);
    drawMessages();
  }
  // Compact: who and when, without the message's first words.
  function drawMessages() {
    const compact = compactOf('messages');
    msgSec.hidden = !msgRows.length;
    msgBox.replaceChildren(...msgRows.map((i) => h('a', { class: 'home-msg', href: i.link },
      h('span', { class: 'home-msg-head' }, h('strong', {}, i.title), h('span', { class: 'when' }, when(i.ts))),
      compact ? null : h('span', { class: 'home-msg-text' }, i.text))));
  }
  // "Next · 15:00 Call Maman · in 40 min": the next timed event today, while it is still to come. Compact leaves out
  // "Next" and says how long in a word ("40 min").
  function showNext(today) {
    const now = new Date();
    const next = (today && !today.error ? today.events : []).filter((e) => !e.all_day && String(e.start).startsWith(today.date))
      .map((e) => ({ e, at: new Date(String(e.start).replace(' ', 'T')) })).filter((x) => x.at > now)
      .sort((a, b) => a.at - b.at)[0];
    nextUp.hidden = !next;
    if (!next) return;
    const mins = Math.round((next.at - now) / 60000);
    const inWords = mins < 60 ? `in ${mins} min` : mins < 120 ? 'in an hour' : `in ${Math.round(mins / 60)} hours`;
    const compact = compactOf('next');
    nextUp.replaceChildren(compact ? null : h('span', { class: 'label' }, 'Next'), h('strong', {}, `${timeOf(next.e)} ${next.e.title}`),
      h('span', { class: 'when' }, compact ? inWords.replace(/^in /, '').replace('an hour', '1 h').replace(/ hours$/, ' h') : inWords));
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
  const listsNote = h('p', { class: 'when home-lists-note', role: 'status' });
  const listsBox = h('div', { class: 'home-lists-row' }, loading('tiles', 2));
  const listsSec = h('section', { class: 'home-lists', 'aria-label': 'Lists' },
    h('h2', { class: 'label' }, 'Lists'), listsBox, listsNote);
  // The lists as they are now in Home Assistant (Greg, 23 Nov: "more of an image of the to-do list rather than an
  // actual to-do list"): read live, and in Full an item can be ticked off. If Home Assistant cannot be reached, the
  // copy the household agent keeps (refreshed every 15 minutes) is shown instead, to read only.
  const LIVE = '/api/v2/home/lists';
  const showLists = () => swr(LIVE, 60 * 1000, drawLive, { error: 'x' });            // kept 1 minute
  const showListsCopy = () => swr('/api/v2/agents/household/brain/page?path=lists.md', 5 * 60000, drawLists, null);
  function drawLive(d) {
    if (!alive) return;
    if (!d || d.error || !Array.isArray(d.lists)) { showListsCopy(); return; }
    listsLive = true;
    drawListsData(d.lists.map((l) => ({ name: l.name, id: l.id, items: (l.items || []).map((i) => ({ text: i.text, due: i.due || '' })) })));
  }
  let listsData = null, listsLive = false;     // what is drawn, so changing the size or ticking redraws without asking again
  // Every widget comes Compact or Full (Greg, 23 Nov for Lists; 24 Nov: every widget), chosen in Edit, each person's own.
  // Each decides what Compact leaves out. A widget nobody has chosen for stays as it always looked: Lists compact, the rest full.
  const SIZES = [['compact', 'Compact'], ['full', 'Full']];
  const sizeOf = (id) => { const v = (getPref('widget_sizes', {}) || {})[id]; return v === 'compact' || v === 'full' ? v : id === 'lists' ? 'compact' : 'full'; };
  const compactOf = (id) => sizeOf(id) === 'compact';
  function drawLists(page) {              // the copy in the brain: the same lists, to read
    if (!alive || !page || !page.text) return;
    listsLive = false;
    const lists = [];
    for (const line of String(page.text).split('\n')) {
      const head = line.match(/^##\s+(.+)/);
      if (head) { lists.push({ name: head[1].trim(), items: [], id: '' }); continue; }
      const id = line.match(/^<!--\s*(todo\.[\w-]+)\s*-->/);
      if (id && lists.length) { lists[lists.length - 1].id = id[1]; continue; }
      const item = line.match(/^- \[ \]\s+(.+)/);
      if (item && lists.length) {
        const due = item[1].match(/\s*\(due ([^)]*)\)\s*$/);
        lists[lists.length - 1].items.push({ text: item[1].replace(/\s*\(due [^)]*\)\s*$/, '').trim(), due: due ? due[1].trim() : '' });
      }
    }
    drawListsData(lists);
  }
  // Ticking off: the item is struck through at once, then Home Assistant is asked; if it says no, it comes back with
  // the reason. When it worked the item leaves the list, with Undo for a few seconds (the house service puts a ticked
  // item back when it is added again).
  let undoTimer = null;
  async function tickOff(l, i, row, box) {
    row.classList.add('ticking'); box.setAttribute('aria-checked', 'true'); box.disabled = true;
    listsNote.replaceChildren();
    const r = await api('/api/v2/home/lists/tick', { method: 'POST', body: { list: l.name, item: i.text, done: true } }).catch(() => null);
    const d = r ? await r.json().catch(() => ({})) : {};
    if (!r || !r.ok || d.error) {
      row.classList.remove('ticking'); box.setAttribute('aria-checked', 'false'); box.disabled = false;
      listsNote.textContent = d.message ? `Could not tick it off: ${d.message}` : 'Could not tick it off just now: Home Assistant did not answer.';
      return;
    }
    forget(LIVE);
    setTimeout(() => { l.items = l.items.filter((x) => x !== i); if (alive) drawListsData(listsData); }, 400);
    clearTimeout(undoTimer);
    listsNote.replaceChildren(`Ticked off “${i.text}” · `, h('button', { type: 'button', class: 'linkish', onclick: async () => {
      listsNote.textContent = 'Putting it back…';
      const u = await api('/api/v2/home/lists/tick', { method: 'POST', body: { list: l.name, item: i.text, done: false } }).catch(() => null);
      forget(LIVE);
      listsNote.textContent = u && u.ok ? '' : 'Could not put it back just now.';
      showLists();
    } }, 'Undo'));
    undoTimer = setTimeout(() => listsNote.replaceChildren(), 7000);
  }
  function drawListsData(lists) {
    if (!alive) return;
    listsData = lists;
    const shown = lists.filter((l) => l.items.length);
    listsSec.hidden = !lists.length;
    // On a phone a list opens in the Home Assistant app, where the lists live (Greg, 21 Nov); a desktop has
    // no app, so there it opens the copy in Logos.
    const phone = window.matchMedia('(max-width: 599px)').matches;
    const full = sizeOf('lists') === 'full';
    listsBox.classList.toggle('full', full);
    const href = (l) => (phone ? haTodo(l.id) : '#/brain/household/lists.md');
    // Full (Béré, 23 Nov: "wants to see more"): every list as a card with its things one under another and when each
    // is due, up to 12 each, then how many more. Live, each has a box to tick it off.
    const MORE = 12;
    const fullCard = (l) => h('section', { class: 'home-list-card full', 'aria-label': l.name },
      h('a', { class: 'home-list-head', href: href(l), title: phone ? `Open ${l.name} in Home Assistant` : 'Open the lists' },
        h('strong', {}, l.name), h('span', { class: 'home-list-count' }, String(l.items.length))),
      h('ul', { class: 'home-list-all' }, ...l.items.slice(0, MORE).map((i) => {
        const box = listsLive ? h('button', { type: 'button', class: 'home-list-box tick', role: 'checkbox', 'aria-checked': 'false', 'aria-label': `Tick off ${i.text}` })
          : h('span', { class: 'home-list-box', 'aria-hidden': 'true' });
        const row = h('li', {}, box, h('span', { class: 'home-list-text' }, i.text), i.due ? h('span', { class: 'home-list-due' }, i.due) : null);
        if (listsLive) box.addEventListener('click', () => tickOff(l, i, row, box));
        return row;
      })),
      l.items.length > MORE ? h('a', { class: 'home-list-more', href: href(l) }, `+${l.items.length - MORE} more`) : null);
    const tile = (l) => h('a', { class: 'home-list-card', href: href(l), title: phone ? `Open ${l.name} in Home Assistant` : 'Open the lists' },
      h('span', { class: 'home-list-head' }, h('strong', {}, l.name), h('span', { class: 'home-list-count' }, String(l.items.length))),
      h('span', { class: 'home-list-items' }, l.items.slice(0, 3).map((i) => i.text).join(' · ') + (l.items.length > 3 ? ` · +${l.items.length - 3}` : '')));
    listsBox.replaceChildren(...(shown.length ? shown.map(full ? fullCard : tile) : [none('Every list is empty.')]));
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
    // Compact: today only, what is still to come, without whose and where.
    if (compactOf('calendar')) {
      const ahead = todays.filter((e) => !over(e));
      todayBox.replaceChildren(h('div', { class: 'home-list home-agenda compact' }, ...part('Today', ahead,
        todays.length ? 'Nothing more today.' : 'Nothing today.')));
      return;
    }
    todayBox.replaceChildren(h('div', { class: 'home-list home-agenda' },
      ...part('Today', todays, 'Nothing today.'), ...part('Tomorrow', later.slice(0, 6), 'Nothing yet.')));
  }


  // ── Home is yours to arrange (Greg, 22 Nov): widgets in your order, dragged into place in Edit ──
  const WIDGETS = { next: 'Next', weather: 'Weather', calendar: 'Calendar', messages: 'Messages', coming: 'Coming up', lights: 'Lights', lists: 'Lists' };
  // Everyone starts with Greg's own arrangement (23 Nov), then moves things as they like.
  const DEFAULT_WIDGETS = ['weather', 'calendar', 'messages', 'next', 'lists', 'lights', 'coming'];
  const chosen = () => {
    const w = getPref('widgets', null);
    if (!Array.isArray(w)) return DEFAULT_WIDGETS;
    // Chosen before the calendar became a widget: it stays, after Next.
    if (!w.includes('calendar') && !getPref('widgets_v2', false)) { const i = w.indexOf('next'); return [...w.slice(0, i + 1), 'calendar', ...w.slice(i + 1)]; }
    return w.filter((x) => x in WIDGETS);
  };
  const on = (id) => chosen().includes(id);
  // Weather: one card, now and today, then tomorrow (Open-Meteo, through Logos; only the place's rounded position goes out).
  const SKY = (c) => (c === 0 ? ['☀️', 'Clear'] : c <= 2 ? ['⛅', 'Partly cloudy'] : c === 3 ? ['☁️', 'Cloudy'] : c <= 48 ? ['🌫️', 'Fog']
    : c <= 57 ? ['🌦️', 'Drizzle'] : c <= 67 ? ['🌧️', 'Rain'] : c <= 77 ? ['🌨️', 'Snow'] : c <= 82 ? ['🌦️', 'Showers']
      : c <= 86 ? ['🌨️', 'Snow showers'] : ['⛈️', 'Thunder']);
  const deg = (t) => (t === null || t === undefined ? '–' : `${Math.round(t)}°`);
  const weatherBox = h('div', { class: 'home-weather' }, loading('rows', 1));
  const weatherSec = h('section', { class: 'home-weather-sec', 'aria-label': 'Weather' }, weatherBox);
  const MIN = 60000;
  const showWeather = () => swr('/api/v2/weather', 30 * MIN, drawWeather, { error: 'weather_unavailable' });   // kept 30 minutes
  // More than one place (Greg, 24 Nov): a row each, in your order, each with its own now and tomorrow. Full: now, today's
  // high and low and rain, then tomorrow. Compact: now and tomorrow's high, one line a place.
  let weatherData = null;
  const shortName = (n) => String(n || '').split(',')[0];
  function placeRow(p, compact) {
    const name = shortName(p.place);
    if (p.error || !p.now) {
      return h('div', { class: 'home-weather-place failed' }, h('span', { class: 'home-weather-text' },
        h('span', {}, h('strong', {}, name), ' · could not be read just now')));
    }
    const [icon, words] = SKY(p.now.code);
    const [tIcon, tWords] = SKY(p.tomorrow.code);
    const now = h('span', { class: 'home-weather-now' }, h('span', { class: 'home-weather-icon', 'aria-hidden': 'true' }, icon), h('strong', {}, deg(p.now.temp)));
    if (compact) {
      return h('div', { class: 'home-weather-place compact', 'aria-label': `${name}: ${words}, ${deg(p.now.temp)}; tomorrow ${tWords.toLowerCase()}, ${deg(p.tomorrow.high)}` }, now,
        h('span', { class: 'home-weather-name' }, name),
        h('span', { class: 'when home-weather-tomorrow' }, `Tomorrow `, h('span', { 'aria-hidden': 'true' }, tIcon), ` ${deg(p.tomorrow.high)}`));
    }
    return h('div', { class: 'home-weather-place' }, now,
      h('span', { class: 'home-weather-text' },
        h('span', {}, h('strong', { class: 'home-weather-name' }, name), ` · ${words} · today ${deg(p.today.high)} / ${deg(p.today.low)}${p.today.rain ? ` · ${p.today.rain}% rain` : ''}`),
        h('span', { class: 'when' }, `Tomorrow ${tIcon} ${tWords.toLowerCase()} ${deg(p.tomorrow.high)} / ${deg(p.tomorrow.low)}${p.tomorrow.rain ? ` · ${p.tomorrow.rain}% rain` : ''}`)));
  }
  function drawWeather(w) {
    if (!alive) return;
    weatherData = w;
    const places = Array.isArray(w.places) ? w.places : w.error === 'no_place' ? [] : [w];     // an older server: one place
    weatherBox.classList.toggle('many', places.length > 1);
    if (!places.length) { weatherBox.replaceChildren(h('div', { class: 'home-place-first' }, h('p', { class: 'when' }, 'Weather: which town?'), placePicker(addPlace))); return; }
    if (places.length === 1 && w.error && !w.now) { weatherBox.replaceChildren(h('p', { class: 'when' }, `The weather could not be read just now${w.place ? ` for ${w.place}` : ''}.`)); return; }
    weatherBox.replaceChildren(...places.map((p) => placeRow(p, compactOf('weather'))));
  }

  // Lights: each room, lit or not; a tap switches it (on if all off, else off), and All off.
  const lightsBox = h('div', { class: 'home-lights' }, loading('tiles', 4));
  const lightsNote = h('p', { class: 'when', role: 'status' });
  const lightsSec = h('section', { class: 'home-lights-sec', 'aria-label': 'Lights' },
    h('div', { class: 'home-sec-head' }, h('h2', { class: 'label' }, 'Lights'),
      h('button', { type: 'button', class: 'linkish', onclick: () => switchLights(['everything'], false) }, 'All off')), lightsBox, lightsNote);
  async function switchLights(targets, value, brightness) {
    lightsNote.textContent = '';
    for (const b of lightsSec.querySelectorAll('button')) b.disabled = true;
    lightsSec.classList.add('busy');
    const r = await api('/api/v2/home/lights', { method: 'POST', body: { targets, on: value, ...(brightness ? { brightness } : {}) } }).catch(() => null);
    const d = r ? await r.json().catch(() => ({})) : {};
    lightsSec.classList.remove('busy');
    if (!r || !r.ok) lightsNote.textContent = d.message || 'The lights did not answer just now.';
    drawLights(d.rooms ? d : await getJSON('/api/v2/home/lights').catch(() => ({})));
  }
  let lightsData = null;
  // Compact: smaller pills with the room's name only (how bright still shows as the fill).
  function drawLights(d) {
    lightsData = d;
    lightsBox.classList.toggle('compact', compactOf('lights'));
    if (!d || d.error || !d.rooms) { lightsBox.replaceChildren(h('p', { class: 'when' }, d && d.error === 'no_house' ? 'No agent here can work the lights.' : 'The lights could not be read just now.')); return; }
    // Each room is a pill (Greg, 23 Nov): a tap switches it; sliding along it sets how bright, the pill
    // filling as the finger moves (from off too), set when let go. Up and down still scrolls the page.
    lightsBox.replaceChildren(...d.rooms.map((r) => {
      const level = r.on ? (r.brightness || 100) : 0;
      const fill = h('span', { class: 'home-light-fill', 'aria-hidden': 'true' });
      const words = h('span', { class: 'when' }, r.on ? (r.brightness ? `${r.brightness}%` : `${r.on} of ${r.of} on`) : 'off');
      const tile = h('div', { class: `home-light${r.on ? ' lit' : ''}`, role: 'slider', tabindex: '0', 'aria-label': `${r.room} brightness`,
        'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(level), 'aria-valuetext': r.on ? `${level}%` : 'off',
        title: 'Tap to switch, slide to dim or brighten' },
      fill, h('span', { class: 'home-light-name' }, r.room), words);
      const show = (pct) => { fill.style.width = `${pct}%`; tile.classList.toggle('lit', pct > 0); words.textContent = pct ? `${pct}%` : 'off';
        tile.setAttribute('aria-valuenow', String(pct)); tile.setAttribute('aria-valuetext', pct ? `${pct}%` : 'off'); };
      fill.style.width = `${level}%`;
      let g = null;
      tile.addEventListener('pointerdown', (e) => {
        if (e.button > 0) return;
        g = { id: e.pointerId, x: e.clientX, y: e.clientY, sliding: false, pct: level, rect: tile.getBoundingClientRect() };
      });
      tile.addEventListener('pointermove', (e) => {
        if (!g || e.pointerId !== g.id) return;
        const dx = e.clientX - g.x, dy = e.clientY - g.y;
        if (!g.sliding) {
          if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) { g = null; return; }      // a scroll
          if (Math.abs(dx) < 8) return;
          g.sliding = true; tile.classList.add('sliding');
          try { tile.setPointerCapture(e.pointerId); } catch (err) { /* fine */ }
        }
        g.pct = Math.max(0, Math.min(100, Math.round(((e.clientX - g.rect.left) / g.rect.width) * 100)));
        show(g.pct);
      });
      const end = (e) => {
        if (!g || e.pointerId !== g.id) return;
        const was = g; g = null; tile.classList.remove('sliding');
        if (!was.sliding) { switchLights([r.room], !r.on); return; }                     // a tap
        if (was.pct === 0) switchLights([r.room], false);
        else switchLights([r.room], true, Math.max(1, was.pct));
      };
      tile.addEventListener('pointerup', end);
      tile.addEventListener('pointercancel', () => { g = null; tile.classList.remove('sliding'); show(level); });
      tile.addEventListener('keydown', (e) => {                                            // keyboard: arrows, Enter
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); switchLights([r.room], !r.on); return; }
        const step = { ArrowRight: 10, ArrowUp: 10, ArrowLeft: -10, ArrowDown: -10 }[e.key];
        if (!step) return;
        e.preventDefault();
        const pct = Math.max(0, Math.min(100, (Number(tile.getAttribute('aria-valuenow')) || 0) + step));
        show(pct);
        clearTimeout(tile._t); tile._t = setTimeout(() => (pct ? switchLights([r.room], true, pct) : switchLights([r.room], false)), 500);
      });
      return tile;
    }));
  }
  const showLights = () => swr('/api/v2/home/lights', 0, (d) => { if (alive) drawLights(d); }, { error: 'x' });   // kept, but always asked again

  // Coming up: the next two weeks after tomorrow, one line each (all-day ones, like birthdays, too).
  const comingBox = h('div', { class: 'home-list home-agenda' }, loading('rows', 3));
  const comingSec = h('section', { class: 'home-coming', 'aria-label': 'Coming up' }, h('h2', { class: 'label' }, 'Coming up'), comingBox);
  const showComing = () => swr('/api/v2/home/coming', 5 * MIN, drawComing, { error: 'x' });   // kept 5 minutes
  let comingData = null;
  // Compact: the next three, without whose.
  function drawComing(d) {
    if (!alive) return;
    comingData = d;
    if (d.error) { comingBox.replaceChildren(h('p', { class: 'when' }, d.error === 'no_calendar' ? 'No calendar yet.' : 'The calendar could not be read just now.')); return; }
    const compact = compactOf('coming');
    const events = (d.events || []).slice().sort((a, b) => String(a.start).localeCompare(String(b.start))).slice(0, compact ? 3 : 8);
    const day = (e) => new Date(`${String(e.start).slice(0, 10)}T12:00:00`).toLocaleDateString([], { weekday: 'short', day: 'numeric' });
    const whose = (e) => (e.calendar === 'personal' ? 'Yours' : String((d.calendars || {})[e.calendar] || e.calendar).replace(/\s*\(.*\)\s*$/, ''));
    comingBox.replaceChildren(...(events.length ? events.map((e) => h('div', { class: 'home-row home-line' },
      h('span', { class: 'home-time' }, day(e)),
      h('span', { class: `dot ${e.calendar === 'personal' ? data.me : e.calendar}`, 'aria-hidden': 'true' }),
      h('span', { class: 'home-title' }, `${e.all_day ? '' : `${String(e.start).slice(11, 16)} `}${e.title}`),
      compact ? null : h('span', { class: 'home-meta' }, whose(e)))) : [none('Nothing in the next two weeks.')]));
  }

  // Weather's places, chosen in the widget itself: found by name, kept with your login, in the order you put them
  // (Greg, 24 Nov). Someone who chose one town before keeps it, as the first.
  const MAX_PLACES = 8;
  const myPlaces = () => {
    const list = getPref('weather_places', null);
    if (Array.isArray(list)) return list;
    const one = getPref('weather_place', null);
    return one ? [one] : [];
  };
  async function savePlaces(list) {
    await setPref('weather_places', list);
    forget('/api/v2/weather'); showWeather();
  }
  const samePlace = (a, b) => Math.round(a.lat * 100) === Math.round(b.lat * 100) && Math.round(a.lon * 100) === Math.round(b.lon * 100);
  async function addPlace(pl) {
    const list = myPlaces();
    if (list.some((x) => samePlace(x, pl)) || list.length >= MAX_PLACES) return;
    await savePlaces([...list, pl]);
  }
  function placePicker(onPick) {
    const q = h('input', { type: 'search', placeholder: 'Town or city', autocomplete: 'off', 'aria-label': 'Town or city for the weather' });
    const found = h('div', { class: 'home-places' });
    const find = async () => {
      found.replaceChildren(h('p', { class: 'when' }, 'Looking…'));
      const r = await getJSON(`/api/v2/weather/places?q=${encodeURIComponent(q.value.trim())}`).catch(() => ({ places: [] }));
      found.replaceChildren(...((r.places || []).length ? r.places.map((pl) => h('button', { type: 'button', class: 'btn quiet small', onclick: async () => {
        await onPick(pl); q.value = ''; found.replaceChildren();
      } }, pl.name)) : [h('p', { class: 'when' }, 'No place by that name.')]));
    };
    return h('div', { class: 'home-place' },
      h('form', { class: 'row', onsubmit: (e) => { e.preventDefault(); find(); } }, q, h('button', { type: 'submit', class: 'btn quiet small' }, 'Find')),
      found, h('p', { class: 'when' }, 'Only each town\'s position, to about a kilometre, goes to the weather service.'));
  }
  // Edit › Places: your places in order, each moved up or down or taken off, and another found by name.
  function placesEditor() {
    const box = h('div', { class: 'home-place home-places-edit', 'aria-label': 'Weather places' });
    const draw = () => {
      const list = myPlaces();
      // The list is changed at once (setPref keeps it straight away), saved in the background.
      const change = (l) => { savePlaces(l); draw(); };
      const move = (i, by) => { const l = [...list]; const [x] = l.splice(i, 1); l.splice(i + by, 0, x); change(l); };
      const row = (pl, i) => h('li', { class: 'home-place-row' },
        h('span', { class: 'home-place-name' }, pl.name),
        h('button', { type: 'button', class: 'place-btn', 'aria-label': `Move ${shortName(pl.name)} up`, title: 'Up', disabled: i === 0, onclick: () => move(i, -1) }, '↑'),
        h('button', { type: 'button', class: 'place-btn', 'aria-label': `Move ${shortName(pl.name)} down`, title: 'Down', disabled: i === list.length - 1, onclick: () => move(i, 1) }, '↓'),
        h('button', { type: 'button', class: 'place-btn remove', 'aria-label': `Take ${shortName(pl.name)} off`, title: 'Take off', onclick: () => change(list.filter((_, j) => j !== i)) }, '✕'));
      const rows = h('ol', { class: 'home-place-list' }, ...list.map(row));
      box.replaceChildren(h('span', { class: 'label' }, list.length ? `Places · ${list.length}` : 'Places'), rows,
        list.length >= MAX_PLACES ? h('p', { class: 'when' }, `Up to ${MAX_PLACES} places.`) : placePicker(async (pl) => { await addPlace(pl); draw(); }));
    };
    draw();
    return box;
  }

  // ── Edit: drag a widget by its handle (finger or mouse), ✕ takes it off, Add puts one back ──────
  const nextSec = h('section', { class: 'home-next-sec', 'aria-label': 'Next' }, nextUp);
  const calSec = h('section', { 'aria-label': 'Calendar', class: 'home-cal' }, h('h2', { class: 'visually-hidden' }, 'Calendar'), todayBox);
  const SECS = { next: nextSec, weather: weatherSec, calendar: calSec, messages: msgSec, coming: comingSec, lights: lightsSec, lists: listsSec };
  for (const [id, el] of Object.entries(SECS)) { el.dataset.widget = id; el.classList.add('home-widget'); }
  const board = h('div', { class: 'home-widgets' });
  // A wide screen (Greg, 23 Nov): two columns that each pack up on their own, still in your order
  // (left, right, left…), instead of rows that leave a gap under the shorter widget. Each widget spans
  // as many 4 px grid rows as it is tall; repacked whenever one changes size or the order changes.
  const wide = matchMedia('(min-width: 820px)');   // two columns from an unfolded foldable up
  // ── The desktop grid (F49): widgets placed and sized anywhere on 12 columns (home-grid.js) ──
  const grid = (() => {
    const mq = matchMedia('(min-width: 1100px)');
    let layout = { ...(getPref('widget_grid', {}) || {}) };
    const shown = (el) => !el.classList.contains('off') && getComputedStyle(el).display !== 'none';
    const widgets = () => [...board.children].filter((el) => el.dataset.widget && shown(el));
    const api = {
      on: () => mq.matches,
      off() {
        if (!board.classList.contains('free')) return;
        board.classList.remove('free');
        for (const el of board.children) { el.style.gridColumn = ''; el.style.gridRow = ''; el.style.height = ''; el.style.overflow = ''; }
      },
      // Place every shown widget; `movingId` keeps the place it was given while the others make room.
      apply(movingId = null) {
        if (!mq.matches) return api.off();
        board.classList.add('free');
        const els = widgets();
        const width = board.getBoundingClientRect().width || 900;
        const items = els.map((el) => {
          const id = el.dataset.widget;
          // Never smaller than its own minimum for the size it is (a saved place from before, or Compact turned Full)
          const c = layout[id] ? G.atLeast(layout[id], id, sizeOf(id), width) : null;
          const w = c ? c.w : Math.max(G.defaultWidth(window.innerWidth), G.minCols(id, sizeOf(id), width)), fixed = c ? c.h : 0;
          if (!fixed) el.style.height = '';
          return { id, x: c ? c.x : null, y: c ? c.y : 0, w, fixed, h: fixed || G.rowsFor(el.getBoundingClientRect().height) };
        });
        for (const it of G.compact(items, movingId)) {
          const src = items.find((i) => i.id === it.id), el = els.find((e) => e.dataset.widget === it.id);
          layout[it.id] = { x: it.x, y: it.y, w: it.w, h: src.fixed };
          el.style.gridColumn = `${it.x + 1} / span ${it.w}`;
          el.style.gridRow = `${it.y + 1} / span ${src.fixed || it.h}`;
          if (src.fixed) { el.style.height = `${G.pxFor(src.fixed)}px`; el.style.overflow = 'auto'; } else { el.style.overflow = ''; }
        }
        requestAnimationFrame(() => api.syncGrips());
      },
      // The resize corners live in a layer of their own above the board (not inside the widgets: a stretched widget scrolls
      // inside itself and would clip its own corner), each sitting on its widget's bottom-right corner.
      syncGrips() {
        const host = board.parentElement;
        let layer = host.querySelector(':scope > .grip-layer');
        if (!layer) { layer = h('div', { class: 'grip-layer' }); host.append(layer); }
        if (!editing || !mq.matches) { layer.replaceChildren(); return; }
        const hr = host.getBoundingClientRect();
        const want = widgets();
        for (const g of [...layer.children]) if (!want.some((el) => el.dataset.widget === g.dataset.widget)) g.remove();
        for (const el of want) {
          const id = el.dataset.widget;
          let g = layer.querySelector(`[data-widget="${id}"]`);
          if (!g) {
            g = h('button', { type: 'button', class: 'widget-grip', 'data-widget': id, 'aria-label': `Resize ${WIDGETS[id]}`,
              title: 'Drag to resize · double-click: fit its content (Alt+arrows with the handle)' });
            g.addEventListener('pointerdown', (e) => api.resize(SECS[id], e));
            g.addEventListener('dblclick', () => api.fit(SECS[id]));
            layer.append(g);
          }
          const r = el.getBoundingClientRect();
          g.style.left = `${r.right - hr.left - 10}px`; g.style.top = `${r.bottom - hr.top - 10}px`;
        }
      },
      // While a widget is moved or resized (Greg, 24 Nov): a ghost outline where it will land, and guides where its edges line
      // up with another widget's (or the board's). In a layer of their own over the board, like the corners.
      guide(id) {
        const host = board.parentElement;
        let layer = host.querySelector(':scope > .guide-layer');
        if (!layer) { layer = h('div', { class: 'guide-layer', 'aria-hidden': 'true' }); host.append(layer); }
        if (!id) { layer.replaceChildren(); layer.hidden = true; return; }
        layer.hidden = false;
        const hr = host.getBoundingClientRect(), b = board.getBoundingClientRect(), m = G.metrics(b.width);
        const ox = b.left - hr.left, oy = b.top - hr.top;
        const el = SECS[id], prev = el.style.transform;
        el.style.transform = '';
        const slot = el.getBoundingClientRect();                     // where it lands: its place in the grid, before it is moved
        el.style.transform = prev;
        const items = widgets().map((w) => { const c = layout[w.dataset.widget]; return { id: w.dataset.widget, x: c.x, y: c.y, w: c.w, h: c.h || G.rowsFor(w.getBoundingClientRect().height) }; });
        const me = items.find((i) => i.id === id);
        const lines = G.guides(items, me);
        const colX = (e) => ox + (e === 0 ? 0 : e === G.COLS ? b.width : e * m.stepX - G.GAPX / 2);
        const span = Math.max(b.height, slot.bottom - b.top);
        const ghost = h('div', { class: 'grid-ghost', style: `left:${slot.left - hr.left}px;top:${slot.top - hr.top}px;width:${slot.width}px;height:${slot.height}px` });
        const cols = Array.from({ length: G.COLS }, (_, k) => h('div', { class: 'grid-col', style: `left:${ox + k * m.stepX}px;top:${oy}px;width:${m.col}px;height:${span}px` }));
        layer.replaceChildren(...cols, ghost,
          ...lines.cols.map((e) => h('div', { class: 'snap-guide v', 'data-col': String(e), style: `left:${colX(e) - 1}px;top:${oy}px;height:${span}px` })),
          ...lines.rows.map((r) => h('div', { class: 'snap-guide h', 'data-row': String(r), style: `top:${oy + r * G.ROWU - (r ? G.GAPV / 2 : 0) - 1}px;left:${ox}px;width:${b.width}px` })));
      },
      // Tidy up: everything packed neatly, the order kept (home-grid.js tidy).
      // Twice: the widths first (a widget as tall as its content is taller or shorter at its new width), then the places with
      // the heights they have now.
      tidy() {
        const els = widgets(), width = board.getBoundingClientRect().width || 900, track = G.defaultWidth(window.innerWidth);
        const ids = G.readingOrder(layout, els.map((e) => e.dataset.widget));
        const items = () => ids.map((id) => {
          const c = layout[id] || { w: track, h: 0 };
          return { id, w: c.w, min: G.minCols(id, sizeOf(id), width), h: c.h || G.rowsFor(SECS[id].getBoundingClientRect().height) };
        });
        const place = () => { for (const t of G.tidy(items(), track)) layout[t.id] = { ...(layout[t.id] || { h: 0 }), x: t.x, y: t.y, w: t.w }; api.apply(); };
        place();
        requestAnimationFrame(() => { place(); api.persist(); });
      },
      forget(id) { delete layout[id]; },
      reset() { layout = {}; setPref('widget_grid', {}).catch(() => {}); api.apply(); },
      layout: () => layout,
      persist() {
        const ids = widgets().map((e) => e.dataset.widget);
        setPref('widget_grid', G.clean(layout, ids)).catch(() => {});
        const order = G.readingOrder(layout, ids);                         // the phone's order follows what was arranged here
        if (order.join() !== ids.join()) for (const id of order) board.append(SECS[id]);      // moved only when it changed: a moved element loses a double-click
        return save(orderNow());
      },
      move(el, e) {
        e.preventDefault();
        const id = el.dataset.widget, r0 = el.getBoundingClientRect(), grab = { x: e.clientX - r0.left, y: e.clientY - r0.top }, pid = e.pointerId;
        el.classList.add('dragging');
        let last = e;
        const follow = () => {
          el.style.transform = '';
          const r = el.getBoundingClientRect();
          el.style.transform = `translate(${last.clientX - grab.x - r.left}px, ${last.clientY - grab.y - r.top}px)`;
        };
        const move = (ev) => {
          if (ev.pointerId !== pid) return;
          ev.preventDefault(); last = ev;
          const b = board.getBoundingClientRect(), m = G.metrics(b.width), c = layout[id];
          const at = G.cellAt(m, ev.clientX - grab.x - b.left, ev.clientY - grab.y - b.top, c.w);
          layout[id] = { ...c, x: at.x, y: at.y };
          api.apply(id); follow(); api.guide(id);
          if (ev.clientY < 90) window.scrollBy(0, -12); else if (ev.clientY > window.innerHeight - 160) window.scrollBy(0, 12);
        };
        const up = (ev) => {
          if (ev.pointerId !== pid) return;
          window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up);
          el.classList.remove('dragging'); el.style.transform = ''; api.guide(null);
          api.apply(); api.persist();
        };
        window.addEventListener('pointermove', move, { passive: false }); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
      },
      resize(el, e) {
        e.preventDefault(); e.stopPropagation();
        const id = el.dataset.widget, r0 = el.getBoundingClientRect(), pid = e.pointerId;
        el.classList.add('resizing');
        const move = (ev) => {
          if (ev.pointerId !== pid) return;
          ev.preventDefault();
          const b = board.getBoundingClientRect(), m = G.metrics(b.width), c = layout[id];
          const w = Math.max(G.minCols(id, sizeOf(id), b.width), Math.min(G.COLS - c.x, Math.round((ev.clientX - r0.left + G.GAPX) / m.stepX)));
          const h = Math.max(G.minRows(id, sizeOf(id)), G.rowsFor(ev.clientY - r0.top));
          layout[id] = { ...c, w, h };
          api.apply(id); api.guide(id);
        };
        const up = (ev) => {
          if (ev.pointerId !== pid) return;
          window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up);
          el.classList.remove('resizing'); api.guide(null); api.apply(); api.persist();
        };
        window.addEventListener('pointermove', move, { passive: false }); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
      },
      fit(el) { const c = layout[el.dataset.widget]; if (c) { layout[el.dataset.widget] = { ...c, h: 0 }; api.apply(); api.persist(); } },
      // Keyboard: arrows move it a column or a row-step; with Alt they resize it.
      key(el, e) {
        const id = el.dataset.widget, c = layout[id];
        if (!c || !['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return false;
        e.preventDefault();
        const dx = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0, dy = e.key === 'ArrowDown' ? 4 : e.key === 'ArrowUp' ? -4 : 0;
        if (e.altKey) {
          const rows = c.h || G.rowsFor(el.getBoundingClientRect().height);
          layout[id] = { ...c, w: Math.max(G.minCols(id, sizeOf(id), board.getBoundingClientRect().width), Math.min(G.COLS - c.x, c.w + dx)), h: dy ? Math.max(G.minRows(id, sizeOf(id)), rows + dy) : c.h };
        } else layout[id] = { ...c, x: Math.max(0, Math.min(G.COLS - c.w, c.x + dx)), y: Math.max(0, c.y + dy) };
        api.apply(id); api.persist();
        return true;
      },
    };
    mq.addEventListener('change', () => api.apply());
    return api;
  })();
  const ROW = 4, GAP = 22;
  const pack = () => {
    if (grid.on()) { grid.apply(); return; }              // a desktop: the free grid places everything
    grid.off();
    for (const el of board.children) {
      if (!wide.matches || getComputedStyle(el).display === 'none') { el.style.gridRowEnd = ''; continue; }   // packed while editing too
      el.style.gridRowEnd = `span ${Math.max(1, Math.ceil((el.getBoundingClientRect().height + GAP) / ROW))}`;
    }
  };
  const sizes = new ResizeObserver(() => requestAnimationFrame(pack));
  new MutationObserver(() => { for (const el of board.children) sizes.observe(el); requestAnimationFrame(pack); })
    .observe(board, { childList: true, attributes: true, attributeFilter: ['hidden', 'class'], subtree: false });
  wide.addEventListener('change', pack);
  let editing = false;
  const tray = h('div', { class: 'home-tray', hidden: true });
  // A pencil at the top right of Home (Greg, 23 Nov: "Edit Home" as text at the bottom was lame); while
  // arranging it is Done, and stays at the top as you scroll.
  const editBtn = h('button', { type: 'button', class: 'home-edit-link', 'aria-label': 'Arrange Home', title: 'Arrange Home',
    onclick: () => setEditing(!editing) }, icon(ICONS.pencil));
  const SHOW = { next: () => showNext(data && data.today), weather: () => showWeather(), messages: () => showMessages(), coming: () => showComing(),
    lights: () => showLights(), lists: () => showLists(), calendar: () => {} };
  // Drawn again from what is already here, when its size changes: nothing is asked again.
  const REDRAW = { next: () => showNext(data && data.today), weather: () => weatherData && drawWeather(weatherData), calendar: () => data && showToday(data.today),
    messages: () => drawMessages(), coming: () => comingData && drawComing(comingData), lights: () => lightsData && drawLights(lightsData),
    lists: () => listsData && drawListsData(listsData) };
  const markSize = (id) => { const el = SECS[id]; if (el) { el.classList.toggle('size-compact', compactOf(id)); el.classList.toggle('size-full', !compactOf(id)); } };

  function applyWidgets() {
    const order = chosen();
    for (const [id, w] of Object.entries(BANDS)) w.el.hidden = !order.includes(id);        // the sign: on or off
    for (const id of order) if (SECS[id]) board.append(SECS[id]);          // in your order
    for (const [id, el] of Object.entries(SECS)) {
      markSize(id);
      el.classList.toggle('off', !order.includes(id));
      if (!order.includes(id)) board.append(el);                           // kept, out of sight, after the rest
    }
    drawTray();
  }
  async function save(order) {
    await setPref('widgets', order);
    if (!getPref('widgets_v2', false)) await setPref('widgets_v2', true).catch(() => {});
  }
  const orderNow = () => [...[...board.children].filter((el) => !el.classList.contains('off')).map((el) => el.dataset.widget),
    ...Object.keys(BANDS).filter(on)];            // the sign is chosen the same way, though it is not on the board
  // F42: the homelab widgets the server offers (gateway/widgets.py), added to the ones above. One that
  // is not set up is offered only to whoever can set it up.
  const KINDS = {};
  // The Las Vegas sign lives in the title band, between the date and the pencil (wide screens), not on the board.
  const bandSlot = h('div', { class: 'home-band' });
  const BANDS = {};
  function addKinds(list) {
    for (const k of list || []) {
      KINDS[k.id] = k;
      if (SECS[k.id] || BANDS[k.id]) continue;
      const w = homelabWidget(k, { onChange: () => drawTray(), compact: () => compactOf(k.id) });
      if (BAND.has(k.id)) {
        BANDS[k.id] = w; WIDGETS[k.id] = k.name; SHOW[k.id] = w.show;
        w.el.dataset.widget = k.id; w.el.classList.add('home-band-widget');
        bandSlot.append(w.el);
        continue;
      }
      WIDGETS[k.id] = k.name;
      SECS[k.id] = w.el;
      SHOW[k.id] = w.show;
      REDRAW[k.id] = w.redraw;
      w.el.dataset.widget = k.id; w.el.classList.add('home-widget');
    }
  }
  function drawTray() {
    const hidden = Object.keys(WIDGETS).filter((id) => !on(id) && (!KINDS[id] || KINDS[id].set_up || KINDS[id].can_set_up));
    tray.replaceChildren(h('span', { class: 'label' }, hidden.length ? 'Add' : 'Every widget is on Home'),
      grid.on() ? h('span', { class: 'home-grid-tools' },
        h('button', { type: 'button', class: 'btn quiet small home-tidy', title: 'Pack everything neatly, in the same order', onclick: () => grid.tidy() }, 'Tidy up'),
        h('button', { type: 'button', class: 'linkish small home-reset', onclick: () => grid.reset() }, 'Reset layout')) : '',     // not null: replaceChildren would write "null" (it did, on a phone)
      ...hidden.map((id) => h('button', { type: 'button', class: 'chip home-add', onclick: async () => {
        save([...orderNow(), id]); applyWidgets(); decorate(); SHOW[id](); grid.apply();
      } }, `+ ${WIDGETS[id]}`)));
  }
  // Compact or Full, in the widget's bar while arranging; kept with your login, redrawn at once. On a desktop the widget
  // grows to the new size's minimum if it is narrower or shorter than that.
  function sizeSwitch(id) {
    const now = sizeOf(id);
    return h('div', { class: 'widget-size', role: 'group', 'aria-label': `${WIDGETS[id]} size` }, ...SIZES.map(([k, label]) => h('button', {
      type: 'button', class: k === now ? 'on' : '', 'aria-pressed': String(k === now), onclick: () => {
        if (k === sizeOf(id)) return;
        setPref('widget_sizes', { ...(getPref('widget_sizes', {}) || {}), [id]: k });     // kept in the background; the page at once
        markSize(id); REDRAW[id]?.(); decorate();
        if (grid.on()) { grid.apply(id); grid.persist(); }
      } }, label)));
  }
  function decorate() {
    for (const [id, w] of Object.entries(BANDS)) {
      w.el.querySelector(':scope > .widget-bar')?.remove();
      if (!editing || !on(id)) continue;
      const k = KINDS[id];
      w.el.prepend(h('div', { class: 'widget-bar band-bar' },
        k && k.can_set_up ? h('button', { type: 'button', class: 'linkish small', onclick: () => setupSheet(k, () => { forget(`/api/v2/widgets/${id}`); w.show(); }) }, 'Edit') : null,
        h('button', { type: 'button', class: 'widget-remove', 'aria-label': `Take ${WIDGETS[id]} off Home`, title: 'Take off Home', onclick: async () => {
          save(orderNow().filter((x) => x !== id)); applyWidgets(); decorate();      // at once; kept in the background (Greg, 24 Nov: it took a second)
        } }, '✕')));
    }
    for (const [id, el] of Object.entries(SECS)) {
      el.querySelector(':scope > .widget-bar')?.remove();
      if (!editing) continue;
      const handle = h('button', { type: 'button', class: 'widget-handle', 'aria-label': `Move ${WIDGETS[id]}`, title: 'Drag to move' }, '⠿');
      handle.addEventListener('pointerdown', (e) => (grid.on() ? grid.move(el, e) : drag(el, e)));
      handle.addEventListener('keydown', (e) => {                         // keyboard: arrows move it
        if (grid.on()) { if (grid.key(el, e)) handle.focus(); return; }
        if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
        e.preventDefault();
        const sib = e.key === 'ArrowUp' ? el.previousElementSibling : el.nextElementSibling;
        if (sib && !sib.classList.contains('off')) { if (e.key === 'ArrowUp') sib.before(el); else sib.after(el); save(orderNow()); handle.focus(); }
      });
      el.prepend(h('div', { class: 'widget-bar' }, handle, h('span', { class: 'widget-name' }, WIDGETS[id]),
        sizeSwitch(id),
        id === 'weather' ? h('button', { type: 'button', class: 'linkish small', 'aria-expanded': String(!!el.querySelector('.home-places-edit')), onclick: (e) => {
          const open = el.querySelector('.home-places-edit');
          if (open) open.remove(); else el.querySelector('.widget-bar').after(placesEditor());
          e.currentTarget.setAttribute('aria-expanded', String(!open));
        } }, 'Places') : null,
        h('button', { type: 'button', class: 'widget-remove', 'aria-label': `Take ${WIDGETS[id]} off Home`, title: 'Take off Home', onclick: async () => {
          grid.forget(id); save(orderNow().filter((w) => w !== id)); applyWidgets(); decorate(); grid.apply(); grid.persist();   // the page first, the saving after
        } }, '✕')));
    }
  }
  function setEditing(v) {
    editing = v;
    requestAnimationFrame(() => requestAnimationFrame(() => grid.syncGrips()));
    board.classList.toggle('editing', v);
    tray.hidden = !v;
    editBtn.replaceChildren(v ? 'Done' : icon(ICONS.pencil));
    editBtn.setAttribute('aria-label', v ? 'Done arranging' : 'Arrange Home'); editBtn.title = v ? 'Done' : 'Arrange Home';
    editBtn.classList.toggle('done', v);
    askForm.classList.toggle('editing', v);
    if (!v) for (const el of board.querySelectorAll('.home-places-edit')) el.remove();
    decorate(); drawTray();
  }
  // A long press on a widget also starts Edit (as a phone's home screen does).
  let pressT = null;
  board.addEventListener('pointerdown', (e) => {
    if (editing || e.pointerType === 'mouse' || e.target.closest('a, button, input')) return;
    const x = e.clientX, y = e.clientY;
    pressT = setTimeout(() => { navigator.vibrate?.(10); setEditing(true); }, 550);
    const cancel = (ev) => { if (!ev || ev.type !== 'pointermove' || Math.hypot(ev.clientX - x, ev.clientY - y) > 10) { clearTimeout(pressT); off(); } };
    const off = () => { board.removeEventListener('pointermove', cancel); board.removeEventListener('pointerup', cancel); board.removeEventListener('pointercancel', cancel); };
    board.addEventListener('pointermove', cancel); board.addEventListener('pointerup', cancel); board.addEventListener('pointercancel', cancel);
  });

  // Dragging: the widget follows the pointer; it swaps places with the widget it is moved over (a list on a
  // phone, two columns on a wide screen), and the page scrolls when the pointer nears the top or bottom.
  function drag(el, e) {
    e.preventDefault();
    // Listened for on the window: moving the widget in the page would end a pointer capture on its handle.
    const id = e.pointerId;
    const r0 = el.getBoundingClientRect();
    const grab = { x: e.clientX - r0.left, y: e.clientY - r0.top };
    el.classList.add('dragging');
    let last = e;
    const place = () => {
      el.style.transform = '';
      const r = el.getBoundingClientRect();
      el.style.transform = `translate(${last.clientX - grab.x - r.left}px, ${last.clientY - grab.y - r.top}px)`;
    };
    const move = (ev) => {
      if (ev.pointerId !== id) return;
      ev.preventDefault();
      last = ev;
      const over = [...board.children].find((s) => s !== el && !s.classList.contains('off') && (() => {
        const b = s.getBoundingClientRect(); return ev.clientX >= b.left && ev.clientX <= b.right && ev.clientY >= b.top && ev.clientY <= b.bottom;
      })());
      if (over) {
        const kids = [...board.children];
        if (kids.indexOf(over) > kids.indexOf(el)) over.after(el); else over.before(el);
      }
      if (ev.clientY < 90) window.scrollBy(0, -12); else if (ev.clientY > window.innerHeight - 160) window.scrollBy(0, 12);
      place();
    };
    const up = (ev) => {
      if (ev.pointerId !== id) return;
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up);
      el.classList.remove('dragging'); el.style.transform = '';
      save(orderNow());
    };
    window.addEventListener('pointermove', move, { passive: false }); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
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
    const first = !painted;
    paint(nb);
    painted = true;
    // From the cache: it brightens as the fresh data replaces it. From nothing: the content fades in.
    if (board.classList.contains('stale')) board.classList.remove('stale');
    else if (first) reveal(board);
  }

  // The last Home seen today on this device, drawn at once; load() replaces it a moment later.
  function fromLast() {
    try {
      const last = JSON.parse(localStorage.getItem(SEEN_KEY) || 'null');
      if (!last || last.day !== new Date().toDateString() || !last.data || !last.data.agent) return;
      data = last.data;
      paint(last.nb || { waiting: [], log: [] });
      painted = true;
      board.classList.add('stale');            // the last Home seen, quietly, until the fresh one is in
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
    nextSec.hidden = nextUp.hidden;
    applyWidgets();
    if (editing) decorate();
    // "Since yesterday" lives in Notifications now (Greg, 21 Nov).
    waitingSec.hidden = !nb.waiting.length;
    for (const id of chosen()) if (id !== 'next' && SHOW[id]) SHOW[id]();
    ask.placeholder = `Ask ${data.agent.display_name}…`;
    voice.mic.hidden = !data.agent.voice;                     // an agent without voice has no microphone
  }

  // The ask box: the question goes to Chat, which sends it in a new conversation.
  // The ask box (Greg, 23 Nov: it took you somewhere that was not your agent). It is the chat's own composer now: a
  // box that grows, the microphone (tap to start, tap to finish), and Ask, which opens your agent's real chat and
  // sends the question there.
  const ask = h('textarea', { id: 'home-ask', rows: '1', placeholder: 'Ask your agent…', autocomplete: 'off', enterkeyhint: 'send' });
  const askNote = h('p', { class: 'when mx-note home-ask-note', role: 'status' });
  const askGrow = () => { ask.style.height = 'auto'; ask.style.height = `${Math.min(ask.scrollHeight + 2, 132)}px`; };
  const say = (t) => { askNote.replaceChildren(typeof t === 'string' ? document.createTextNode(t) : t); };
  const voice = createRecorder({ agent: () => (data && data.agent ? data.agent.name : ''), say,
    // A recording is sent as soon as its words are ready, as a voice message (see voice.js), to your agent's chat.
    onWords: (text, meta) => { ask.value = text; submitAsk(meta); } });
  const submitAsk = (spoken) => {
    const text = ask.value.trim();
    if (!text || !data || !data.agent) return;
    try { sessionStorage.setItem(ASK_KEY, JSON.stringify({ agent: data.agent.name, text, voice: spoken && spoken.seconds > 0 ? spoken : null })); } catch (err) { /* sent below anyway */ }
    // An agent on Matrix is asked in its own chat (#/chat/<agent>); the old web chat is a read-only archive now.
    location.hash = data.agent.matrix && mx.saved() ? `#/chat/${data.agent.name}` : `#/chat/${data.agent.name}/${Math.random().toString(36).slice(2, 12)}`;
  };
  const askForm = h('form', { class: 'home-ask', onsubmit: (e) => { e.preventDefault(); submitAsk(); } },
    h('label', { for: 'home-ask', class: 'sr' }, 'Ask your agent'), ask, voice.mic,
    h('button', { type: 'submit', class: 'btn ask-go', 'aria-label': 'Ask' }, h('span', { class: 'btn-words' }, 'Ask'), icon(ICONS.send)),
    voice.bar, askNote,
    // While arranging Home you are not asking anything (Greg, 23 Nov): the bar at the bottom of the screen is one big Done,
    // always in view, instead of a button to scroll down to.
    h('button', { type: 'button', class: 'ask-done', onclick: () => setEditing(false) }, 'Done'));
  ask.addEventListener('input', askGrow);
  ask.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitAsk(); } });
  voice.resume();

  root.replaceChildren(h('div', { class: 'page home' },
    h('header', { class: 'home-head' },
      h('div', { class: 'home-date-row' }, h('h1', { class: 'home-date' }, new Date().toLocaleDateString([], DAY)), bandSlot, editBtn), statusWords),
    waitingSec,
    board,
    tray,
    askForm));                                   // the Ask box stays at the bottom (Greg, 21 Nov)
  addKinds((peek('/api/v2/widgets') || {}).v?.widgets);     // known from last time: in place at once
  applyWidgets();
  swr('/api/v2/widgets', 5 * MIN, (d) => {
    if (!alive || !d || d.error) return;
    const fresh = (d.widgets || []).filter((k) => !SECS[k.id]);
    addKinds(d.widgets);
    if (fresh.length) { applyWidgets(); if (editing) decorate(); for (const k of fresh) if (on(k.id)) SHOW[k.id](); }
  }, null);

  // Pulling down (app.js): everything kept is let go, and Home asks again (widgets included).
  if (state) state.refresh = async () => { clearAll(); await load(); };
  fromLast();
  await load();
  setTimeout(() => { if (alive) warmAgents(); }, 300);   // the Agents tab, ready before the swipe
  timer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, 60000);
  // On a phone the pencil sits at the end of Home, under the last widget (Greg, 23 Nov: not in the top
  // bar), and Done floats above the Ask box while arranging; on a wide screen it is in Home's title band.
  // It moves when the window crosses that width.
  const phone = matchMedia('(max-width: 599px)');
  const dateRow = editBtn.parentElement;
  const placeEdit = () => { if (phone.matches) tray.after(editBtn); else dateRow.append(editBtn); };
  placeEdit();
  phone.addEventListener('change', placeEdit);
  return () => { alive = false; clearInterval(timer); phone.removeEventListener('change', placeEdit); editBtn.remove(); voice.destroy(); };
}

// Chat calls this when it opens: a question asked on Home, still to be sent.
// Hand a question, typed or spoken, to the agent's chat, which sends it when it opens (Home's Ask, a recording on the
// Agents screen). voice: {seconds, wave} when it was spoken.
export function handOver(agent, text, voice) {
  try { sessionStorage.setItem(ASK_KEY, JSON.stringify({ agent, text, voice: voice && voice.seconds > 0 ? voice : null })); } catch (e) { /* sent below anyway */ }
}
// The same, with the voice message's length and shape when it was spoken: {text, voice}.
export function takeAskFull(agent) {
  try {
    const v = JSON.parse(sessionStorage.getItem(ASK_KEY) || 'null');
    if (v && v.agent === agent && v.text) { sessionStorage.removeItem(ASK_KEY); return { text: v.text, voice: v.voice || null }; }
  } catch (e) { /* nothing waiting */ }
  return null;
}
export function takeAsk(agent) {
  try {
    const v = JSON.parse(sessionStorage.getItem(ASK_KEY) || 'null');
    if (v && v.agent === agent && v.text) { sessionStorage.removeItem(ASK_KEY); return v.text; }
  } catch (e) { /* nothing waiting */ }
  return null;
}
