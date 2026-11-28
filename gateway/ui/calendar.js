// Calendar (Béré's request, 27 Nov; a full app, Greg 27 Nov): the household's calendars at a glance. Month, week,
// day and a list, each calendar in its own colour with a switch to hide it, today marked; tap a day for its events,
// an event for all of it. Only the calendars the signed-in person may read, read as Home reads them (one call per
// range shown, kept five minutes on this device). Adding, moving and cancelling go through the person's own agent,
// with its rights: their own calendar and Household, never someone else's, never a repeating event. Without such an
// agent, "Ask your agent" puts the words in its chat instead.
//   #/calendar                         this month
//   #/calendar/<month|week|day|list>/<YYYY-MM-DD>[/<event id>]
import { h, api, getJSON, none, loading, icon, ICONS, reveal, focusIfKeyboard, ask } from './util.js';
import { peek, keep, forget, forgetWhere } from './cache.js';
import { draftFor } from './home.js';

const MIN = 60000;
const VIEWS = [['month', 'Month'], ['week', 'Week'], ['day', 'Day'], ['list', 'List']];
const LIST_DAYS = 28;                     // the list: four weeks from the day shown
const HIDDEN_KEY = 'logos-cal-hidden';    // calendars switched off, on this device
const VIEW_KEY = 'logos-cal-view';
const FALLBACK = ['#8b5cf6', '#0891b2', '#e11d48', '#ca8a04', '#64748b'];

// ── dates, always local: "2026-11-27" ──────────────────────────────────────────────────────────
const pad = (n) => String(n).padStart(2, '0');
export const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
export const parse = (s) => { const [y, m, d] = String(s).slice(0, 10).split('-').map(Number); return new Date(y, (m || 1) - 1, d || 1); };
const add = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const monday = (d) => add(d, -((d.getDay() + 6) % 7));
const validDay = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || '') && !Number.isNaN(parse(s).getTime());
const dayWords = (d, opts = { weekday: 'long', day: 'numeric', month: 'long' }) => d.toLocaleDateString([], opts);
const timeOf = (s) => String(s || '').slice(11, 16);

// The first and last day a view shows (both included).
export function rangeOf(view, day) {
  const d = parse(day);
  if (view === 'month') {
    const first = new Date(d.getFullYear(), d.getMonth(), 1), last = new Date(d.getFullYear(), d.getMonth() + 1, 0);
    return [monday(first), add(monday(last), 6)];
  }
  if (view === 'week') return [monday(d), add(monday(d), 6)];
  if (view === 'day') return [d, d];
  return [d, add(d, LIST_DAYS - 1)];
}
const urlOf = ([a, b]) => `/api/v2/calendar?start=${iso(a)}&end=${iso(b)}`;
// What is fetched is always whole months as the month view shows them (Greg, 28 Nov: "takes a long time to open from day
// to month"): a week and a day are inside their month's, so switching views never waits; a list's four weeks take one
// month or two. Home's Month widget asks for the same, so the two share what is kept.
const monthOf = (d) => new Date(d.getFullYear(), d.getMonth(), 1);
const monthsFor = (view, day) => {
  const b = rangeOf(view, day)[1];
  const out = [monthOf(parse(day))];
  if (view === 'list' && monthOf(b).getTime() !== out[0].getTime()) out.push(monthOf(b));
  return out.map((m) => urlOf(rangeOf('month', iso(m))));
};
// Several months' answers as one: events once each (a repeating one once per start), busy times once each.
function merged(list) {
  if (list.length === 1) return list[0];
  const bad = list.find((d) => !d || d.error);
  if (bad) return bad;
  const seen = new Set(), key = (e) => `${e.id || e.title}|${e.start}`;
  const events = [], busy = [];
  for (const d of list) {
    for (const e of d.events || []) if (!seen.has(key(e))) { seen.add(key(e)); events.push(e); }
    for (const b of d.busy || []) { const k = `busy|${b.who}|${b.busy_from}`; if (!seen.has(k)) { seen.add(k); busy.push(b); } }
  }
  return { ...list[0], start: list[0].start, end: list[list.length - 1].end, events, busy };
}

// The days an event covers: an all-day one ends the day before its end; a timed one ending at midnight, the day before.
export function daysOf(e) {
  const start = parse(e.start);
  let end = e.end ? parse(e.end) : start;
  if (e.all_day && e.end) end = add(end, -1);
  else if (e.end && timeOf(e.end) === '00:00' && iso(end) > iso(start)) end = add(end, -1);
  if (end < start) end = start;
  const out = [];
  for (let d = start, i = 0; d <= end && i < 62; d = add(d, 1), i++) out.push(iso(d));
  return out;
}

// Each calendar's colour: the person's own (or their agent's) colour, Household's, else one of a few spare ones.
function paletteFor(d, me) {
  const out = {};
  (d.calendars || []).forEach((c, i) => {
    const cls = String(c.key === 'personal' ? me : c.key).replace(/[^a-z0-9_-]/gi, '');
    out[c.key] = { cls, colour: `var(--agent-${cls}, var(--${cls}, ${FALLBACK[i % FALLBACK.length]}))`,
      name: c.key === 'personal' ? 'Yours' : c.name };
  });
  return out;
}

// Events of a range by day, with busy times (what some agents see of others) as "Busy", in time order.
function byDay(d, hidden) {
  const map = new Map();
  const nameKey = Object.fromEntries((d.calendars || []).map((c) => [c.name, c.key]));
  const all = [...(d.events || []).map((e) => ({ ...e })),
    ...(d.busy || []).map((b, i) => ({ id: `busy-${i}`, title: 'Busy', busy: true, start: b.busy_from, end: b.busy_until,
      calendar: nameKey[b.who] || b.who, who: b.who, read_only: true }))];
  for (const e of all) {
    if (hidden.has(e.calendar)) continue;
    for (const day of daysOf(e)) { if (!map.has(day)) map.set(day, []); map.get(day).push(e); }
  }
  for (const list of map.values()) list.sort((a, b) => (b.all_day ? 1 : 0) - (a.all_day ? 1 : 0) || String(a.start).localeCompare(String(b.start)) || String(a.title).localeCompare(String(b.title)));
  return map;
}

function loadHidden() {
  try { return new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || '[]')); } catch (e) { return new Set(); }
}
function saveHidden(set) { try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...set])); } catch (e) { /* this visit only */ } }

// "10:30–11:30", "All day", or for a timed event over several days "from 22:00".
function whenShort(e, day) {
  if (e.all_day) return 'All day';
  const s = String(e.start).slice(0, 10), en = String(e.end || e.start).slice(0, 10);
  if (day && s !== day) return en === day ? `until ${timeOf(e.end)}` : 'All day';
  if (en !== s) return `from ${timeOf(e.start)}`;
  return e.end && timeOf(e.end) !== timeOf(e.start) ? `${timeOf(e.start)}–${timeOf(e.end)}` : timeOf(e.start);
}
function whenLong(e) {
  const s = parse(e.start), days = daysOf(e), last = parse(days[days.length - 1]);
  if (e.all_day) return days.length > 1 ? `${dayWords(s)} to ${dayWords(last)}, all day` : `${dayWords(s)}, all day`;
  if (days.length > 1) return `${dayWords(s)} ${timeOf(e.start)} to ${dayWords(parse(e.end))} ${timeOf(e.end)}`;
  return `${dayWords(s)}, ${whenShort(e)}`;
}
// A location that is a web address shows as a short link, never the whole address (as words only inside
// an event's own button, where a link cannot go: the event in full has it).
function placeOf(loc, plain = false) {
  if (!loc) return null;
  const url = String(loc).match(/https?:\/\/\S+/);
  if (!url) return h('span', {}, loc);
  const rest = String(loc).replace(url[0], '').replace(/[\s,·-]+$/, '').trim();
  const word = /maps|goo\.gl/.test(url[0]) ? 'Map' : 'Link';
  if (plain) return h('span', {}, rest || word);
  return h('span', {}, rest ? `${rest} · ` : '', h('a', { href: url[0], target: '_blank', rel: 'noopener' }, word));
}

// Each range asked for once at a time: what the screen shows and what it fetches ahead share one call.
const inflight = new Map();
function fetchRange(url) {
  if (!inflight.has(url)) {
    inflight.set(url, getJSON(url).then((d) => { if (d && !d.error) keep(url, d); return d; })
      .finally(() => inflight.delete(url)));
  }
  return inflight.get(url);
}

// A month small: its days as links (to that day, or the list from it), dots for what is on, today and the picked day marked.
function miniMonth(d, map, pal, { today, picked = '', view = 'day' }) {
  const [a, b] = rangeOf('month', iso(d));
  const cells = [];
  for (let x = a; x <= b; x = add(x, 1)) {
    const key = iso(x), evs = map.get(key) || [];
    cells.push(h('a', { class: `cal-mini-day${x.getMonth() !== d.getMonth() ? ' other' : ''}${key === today ? ' today' : ''}${key === picked ? ' picked' : ''}`,
      href: `#/calendar/${view === 'list' ? 'list' : 'day'}/${key}`,
      'aria-label': `${dayWords(x)}: ${evs.length || 'nothing'}${evs.length ? ` event${evs.length > 1 ? 's' : ''}` : ' on'}`, 'aria-current': key === today ? 'date' : null },
    h('span', { class: 'cal-num' }, String(x.getDate())),
    h('span', { class: 'cal-dots', 'aria-hidden': 'true' }, evs.slice(0, 3).map((e) => h('span', { class: 'dot', style: `background:${(pal[e.calendar] || {}).colour || 'var(--muted)'}` })))));
  }
  const heads = Array.from({ length: 7 }, (_, i) => h('span', { class: 'cal-dow', 'aria-hidden': 'true' }, add(a, i).toLocaleDateString([], { weekday: 'narrow' })));
  return [h('a', { class: 'cal-mini-title', href: `#/calendar/month/${iso(d)}` }, d.toLocaleDateString([], { month: 'long', year: 'numeric' })),
    h('div', { class: 'cal-mini' }, heads, cells)];
}

function aheadOf(d, span = 2) {
  for (let n = -span; n <= span; n++) {
    const url = urlOf(rangeOf('month', iso(new Date(d.getFullYear(), d.getMonth() + n, 1))));
    const k = peek(url);
    if (!k || Date.now() - k.t >= 5 * MIN) fetchRange(url).catch(() => { /* asked again when shown */ });
  }
}
// This month and the ones either side, fetched quietly soon after the app opens, so the Calendar opens at once.
export function warmCalendar() { aheadOf(new Date(), 1); }

export async function calendarView(root, rest, state) {
  let alive = true;
  const me = (state.me && state.me.username) || '';
  const today = iso(new Date());
  let [view, day, openId] = rest;
  let pickedView = null;
  try { pickedView = localStorage.getItem(VIEW_KEY); } catch (e) { /* none */ }
  if (!VIEWS.some(([k]) => k === view)) view = VIEWS.some(([k]) => k === pickedView) ? pickedView : 'month';
  if (!validDay(day)) day = today;
  openId = openId ? decodeURIComponent(openId) : '';
  let selected = day;                   // the day whose events show beside (or under) the month
  let data = null, asked = 0;
  const hidden = loadHidden();

  // The period shown is the heading, and a button: it opens the date picker (Greg, 28 Nov: one control fewer in the bar).
  const titleText = h('span', { class: 'cal-title-text', 'aria-live': 'polite' });
  const prev = h('button', { type: 'button', class: 'btn quiet small cal-step', 'aria-label': 'Earlier' }, icon(['M15 6l-6 6 6 6']));
  const next = h('button', { type: 'button', class: 'btn quiet small cal-step', 'aria-label': 'Later' }, icon(ICONS.chevron));
  const todayBtn = h('button', { type: 'button', class: 'btn quiet small cal-today' }, 'Today');
  const goTo = h('input', { type: 'date', class: 'cal-goto', 'aria-label': 'Go to a date', tabindex: '-1', value: day });
  const titleBtn = h('button', { type: 'button', class: 'cal-title-btn', title: 'Go to a date', 'aria-haspopup': 'dialog' }, titleText,
    icon(['M6 9l6 6 6-6']));
  titleBtn.addEventListener('click', () => { try { goTo.showPicker(); } catch (e) { goTo.focus(); goTo.click(); } });
  const title = h('h2', { class: 'cal-title' }, titleBtn, goTo);
  const tabs = h('div', { class: 'tabs cal-views', role: 'tablist', 'aria-label': 'Show' },
    VIEWS.map(([k, label]) => h('button', { type: 'button', role: 'tab', 'data-view': k, 'aria-selected': String(k === view) }, label)));
  const addBtn = h('button', { type: 'button', class: 'btn small cal-add' }, icon(ICONS.plus), 'Add');
  const legend = h('div', { class: 'cal-legend', role: 'group', 'aria-label': 'Calendars shown' });
  const body = h('div', { class: 'cal-body' }, loading('rows', 5));
  const note = h('p', { class: 'when cal-note', role: 'status' });
  const page = h('div', { class: 'page calendar' },
    h('header', { class: 'page-band' }, h('h1', {}, 'Calendar')),
    h('div', { class: 'cal-bar' },
      h('div', { class: 'cal-nav' }, prev, todayBtn, next, title),
      h('div', { class: 'cal-tools' }, tabs, addBtn)),
    note, body);
  root.replaceChildren(page);

  const range = () => rangeOf(view, day);
  const keepAddress = () => {
    const want = `#/calendar/${view}/${day}`;
    if (location.hash !== want) history.replaceState(null, '', want);
    try { sessionStorage.setItem('logos.last.calendar', want); } catch (e) { /* fine */ }
  };

  function drawTitle() {
    const [a, b] = range(), d = parse(day);
    titleText.textContent = view === 'month' ? d.toLocaleDateString([], { month: 'long', year: 'numeric' })
      : view === 'day' ? dayWords(d, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
        : `${a.toLocaleDateString([], { day: 'numeric', month: 'short' })} – ${b.toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' })}`;
    for (const t of tabs.children) t.setAttribute('aria-selected', String(t.dataset.view === view));
    goTo.value = day;
    todayBtn.disabled = iso(range()[0]) <= today && today <= iso(range()[1]) && (view !== 'day' || day === today);
  }

  function drawLegend(pal) {
    const cals = (data && data.calendars) || [];
    legend.hidden = !cals.length;
    legend.replaceChildren(...cals.map((c) => {
      const p = pal[c.key];
      const b = h('button', { type: 'button', class: 'cal-chip', 'aria-pressed': String(!hidden.has(c.key)), style: `--cal:${p.colour}`,
        title: hidden.has(c.key) ? `Show ${p.name}` : `Hide ${p.name}` },
      h('span', { class: `dot ${p.cls}`, 'aria-hidden': 'true' }), p.name, c.busy_only ? h('span', { class: 'when' }, ' (busy times)') : null);
      b.addEventListener('click', () => {
        if (hidden.has(c.key)) hidden.delete(c.key); else hidden.add(c.key);
        saveHidden(hidden); draw();
      });
      return b;
    }));
  }

  // One event: its time, colour and title, whose, and (in the Day view, where there is room: Greg, 27 Nov) where and
  // its notes. Every word shows: titles wrap, nothing is cut short.
  const eventRow = (e, pal, dayIso, full = false) => {
    const p = pal[e.calendar] || { cls: '', colour: 'var(--muted)', name: e.who || e.calendar };
    return h('button', { type: 'button', class: `cal-event${full ? ' full' : ''}${e.all_day ? ' all-day' : ''}${e.busy ? ' busy' : ''}`, style: `--cal:${p.colour}`,
      'data-id': e.id || '', onclick: (ev) => { ev.stopPropagation(); details(e, ev.currentTarget); } },
    h('span', { class: 'cal-ev-time' }, whenShort(e, dayIso)),
    h('span', { class: `dot ${p.cls}`, 'aria-hidden': 'true' }),
    h('span', { class: 'cal-ev-main' },
      h('span', { class: 'cal-ev-title' }, e.busy ? `Busy · ${e.who}` : e.title || '(no title)'),
      h('span', { class: 'cal-ev-who' }, p.name),
      full && e.location ? h('span', { class: 'cal-ev-where' }, placeOf(e.location, true)) : null,
      full && e.notes ? h('span', { class: 'cal-ev-notes' }, e.notes) : null));
  };
  const dayList = (events, pal, dayIso, empty = 'Nothing on.', full = false) => (events && events.length
    ? h('div', { class: 'cal-list' }, events.map((e) => eventRow(e, pal, dayIso, full))) : none(empty));

  function drawMonth(map, pal) {
    const [a, b] = range(), month = parse(day).getMonth();
    const heads = Array.from({ length: 7 }, (_, i) => h('span', { class: 'cal-dow', 'aria-hidden': 'true' },
      add(a, i).toLocaleDateString([], { weekday: 'short' })));
    const cells = [];
    for (let d = a; d <= b; d = add(d, 1)) {
      const key = iso(d), evs = map.get(key) || [];
      const cell = h('button', { type: 'button', role: 'gridcell', class: `cal-day${d.getMonth() !== month ? ' other' : ''}${key === today ? ' today' : ''}${key === selected ? ' picked' : ''}`,
        'data-day': key, 'aria-label': `${dayWords(d)}: ${evs.length ? `${evs.length} event${evs.length > 1 ? 's' : ''}` : 'nothing on'}`,
        'aria-current': key === today ? 'date' : null, 'aria-selected': String(key === selected) },
      h('span', { class: 'cal-num' }, String(d.getDate())),
      h('span', { class: 'cal-dots', 'aria-hidden': 'true' }, evs.slice(0, 4).map((e) => h('span', { class: `dot ${(pal[e.calendar] || {}).cls || ''}`, style: `background:${(pal[e.calendar] || {}).colour || 'var(--muted)'}` }))),
      h('span', { class: 'cal-chips', 'aria-hidden': 'true' }, evs.slice(0, 3).map((e) => h('span', { class: `cal-pill${e.all_day ? ' all-day' : ''}`, style: `--cal:${(pal[e.calendar] || {}).colour || 'var(--muted)'}` },
        // (a space after the time, so a long title can go to the next line instead of out of the cell)
        e.all_day || !(timeOf(e.start) && String(e.start).slice(0, 10) === key) ? '' : [h('span', { class: 'cal-ev-time' }, timeOf(e.start)), ' '], e.busy ? 'Busy' : e.title)),
      evs.length > 3 ? h('span', { class: 'cal-more' }, `+${evs.length - 3} more`) : null));
      cell.addEventListener('click', () => { selected = key; if (d.getMonth() !== month) { day = key; load(); } else draw(); });
      cell.addEventListener('dblclick', () => { day = key; view = 'day'; load(); });
      cells.push(cell);
    }
    const grid = h('div', { class: 'cal-month', role: 'grid', 'aria-label': titleText.textContent }, heads, cells);
    const sd = parse(selected);
    const side = h('section', { class: 'cal-side card', 'aria-label': dayWords(sd) },
      h('div', { class: 'cal-side-head' }, h('h3', {}, selected === today ? `Today · ${dayWords(sd, { day: 'numeric', month: 'long' })}` : dayWords(sd)),
        h('a', { class: 'linkish small', href: `#/calendar/day/${selected}` }, 'Open day')),
      dayList(map.get(selected), pal, selected));
    swipe(grid);
    // The key under the month, above the day's list (Greg, 27 Nov).
    return h('div', { class: 'cal-month-wrap' }, h('div', { class: 'cal-month-main' }, grid, legend), side);
  }

  function drawDays(days, map, pal, { skipEmpty = false, cls = 'cal-week' } = {}) {
    const out = [];
    for (const d of days) {
      const key = iso(d), evs = map.get(key) || [];
      if (skipEmpty && !evs.length) continue;
      out.push(h('section', { class: `cal-col${key === today ? ' today' : ''}`, 'data-day': key, 'aria-label': dayWords(d) },
        h('a', { class: 'cal-col-head', href: `#/calendar/day/${key}` },
          h('span', { class: 'cal-col-dow' }, key === today ? 'Today' : d.toLocaleDateString([], { weekday: 'short' })),
          h('span', { class: 'cal-col-num' }, d.toLocaleDateString([], { day: 'numeric', month: cls === 'cal-agenda' ? 'long' : undefined }))),
        dayList(evs, pal, key, cls === 'cal-week' ? '—' : 'Nothing on.')));
    }
    if (!out.length) out.push(none(`Nothing in the ${LIST_DAYS} days from ${dayWords(parse(day), { day: 'numeric', month: 'long' })}.`));
    const box = h('div', { class: cls }, out);
    if (cls === 'cal-week') swipe(box);
    return box;
  }

  function draw() {
    if (!alive) return;
    drawTitle();
    if (!data) return;
    if (data.error) {
      legend.hidden = true;
      body.replaceChildren(h('div', { class: 'card cal-trouble' },
        h('p', {}, data.error === 'no_calendar' ? 'There is no calendar for you yet.' : 'The calendar could not be read just now.'),
        data.error === 'no_calendar' ? h('p', { class: 'when' }, 'Whoever manages Logos can give you one (docs/calendar.md).')
          : h('button', { type: 'button', class: 'btn quiet small', onclick: () => load(true) }, 'Try again')));
      return;
    }
    const pal = paletteFor(data, me), map = byDay(data, hidden);
    drawLegend(pal);
    const [a, b] = range();
    const days = []; for (let d = a; d <= b; d = add(d, 1)) days.push(d);
    // A redraw keeps the place: what had the focus has it again.
    const had = document.activeElement && body.contains(document.activeElement)
      ? (document.activeElement.dataset.day ? `[data-day="${document.activeElement.dataset.day}"]` : document.activeElement.dataset.id ? `[data-id="${CSS.escape(document.activeElement.dataset.id)}"]` : '') : '';
    // The key goes under the calendar (Greg, 27 Nov): under the month grid, else under the days.
    const keyed = (el) => h('div', { class: 'cal-keyed' }, el, legend);
    // Day and List on a wide screen (Greg, 28 Nov: "the placement of everything isn't very good"): the words in a
    // readable column, and beside them the month to jump about in, with the key under it.
    const aside = (el) => h('div', { class: 'cal-split' }, el,
      h('aside', { class: 'cal-aside', 'aria-label': 'Month' }, h('div', { class: 'cal-aside-month card' }, miniMonth(parse(day), map, pal, { today, picked: day, view })), legend));
    body.dataset.drawn = `${view}/${day}`;
    body.replaceChildren(view === 'month' ? drawMonth(map, pal)
      : view === 'week' ? keyed(drawDays(days, map, pal))
        : view === 'day' ? aside(h('div', { class: 'cal-dayview card' }, dayList(map.get(day), pal, day, 'Nothing on this day.', true)))
          : aside(drawDays(days, map, pal, { skipEmpty: true, cls: 'cal-agenda' })));
    addBtn.hidden = false;
    if (had) { const el = body.querySelector(had); if (el) el.focus({ preventScroll: true }); }
  }

  // Fast (Greg, 27 Nov): what this device kept draws at once; a fresh copy (when the kept one is older than five
  // minutes, or there is none) replaces it in place only if something changed; with nothing kept, a light
  // placeholder. Once drawn, the range before and after are fetched quietly, so Earlier and Later are instant.
  async function load(fresh = false) {
    keepAddress();
    const mine = ++asked, urls = monthsFor(view, day);
    if (fresh) urls.forEach(forget);
    if (view !== 'month') selected = day;
    else if (selected < iso(range()[0]) || selected > iso(range()[1]) || parse(selected).getMonth() !== parse(day).getMonth()) selected = day;
    drawTitle();
    const kept = urls.map(peek);
    const whole = kept.every(Boolean);
    if (whole) { data = merged(kept.map((k) => k.v)); draw(); }
    else if (!fresh || !data) {
      data = null;
      body.dataset.drawn = '';
      body.replaceChildren(h('div', { class: 'cal-wait' }, loading(view === 'month' ? 'card' : 'rows', view === 'month' ? 1 : 4)));
    }
    if (!whole || kept.some((k) => Date.now() - k.t >= 5 * MIN)) {
      let d;
      try { d = merged(await Promise.all(urls.map(fetchRange))); } catch (e) { d = whole ? null : { error: 'calendar_unavailable' }; }
      if (!alive || mine !== asked) return;
      if (d && !(whole && JSON.stringify(d) === JSON.stringify(data))) {
        const wasWaiting = !data;
        data = d; draw();
        if (wasWaiting) reveal(body.firstElementChild);
      }
    }
    if (!alive || mine !== asked) return;
    if (openId && data) {        // opened at one event (from Home)
      const btn = body.querySelector(`[data-id="${CSS.escape(openId)}"]`);
      const e = [...(data.events || [])].find((x) => x.id === openId);
      openId = '';
      if (e) details(e, btn);
    }
    if (data && !data.error) ahead();
  }
  // Two months either side of the one shown, if this device has no fresh copy of them: Earlier and Later, even
  // pressed quickly, and a day stepped over a month's end, are drawn from what is here.
  function ahead() { aheadOf(parse(day)); }

  const stepped = (n) => {
    const d = parse(day);
    return iso(view === 'month' ? new Date(d.getFullYear(), d.getMonth() + n, 1) : add(d, n * (view === 'week' ? 7 : view === 'day' ? 1 : LIST_DAYS)));
  };
  const step = (n) => {
    day = stepped(n);
    if (view === 'month') selected = parse(today).getMonth() === parse(day).getMonth() && parse(today).getFullYear() === parse(day).getFullYear() ? today : day;
    load();
  };
  prev.addEventListener('click', () => step(-1));
  next.addEventListener('click', () => step(1));
  todayBtn.addEventListener('click', () => { day = today; selected = today; load(); });
  goTo.addEventListener('change', () => { if (validDay(goTo.value)) { day = goTo.value; selected = day; load(); } });
  for (const t of tabs.children) t.addEventListener('click', () => {
    view = t.dataset.view;
    if (view !== 'month') day = selected || day;
    try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* fine */ }
    load();
  });
  addBtn.addEventListener('click', () => editor(null, view === 'month' ? selected : day));
  // On a phone, a swipe across the month or week moves to the next or the last one.
  function swipe(el) {
    let s = null;
    el.addEventListener('touchstart', (e) => { s = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null; }, { passive: true });
    el.addEventListener('touchend', (e) => {
      if (!s) return;
      const t = e.changedTouches[0], dx = t.clientX - s.x, dy = t.clientY - s.y; s = null;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
    }, { passive: true });
  }
  const onKey = (e) => {
    if (e.target.closest('input, textarea, select, .ask-layer')) return;
    if (e.key === 'ArrowLeft' && e.altKey === false && !e.ctrlKey && !e.metaKey && document.activeElement === document.body) step(-1);
    if (e.key === 'ArrowRight' && !e.ctrlKey && !e.metaKey && document.activeElement === document.body) step(1);
  };
  document.addEventListener('keydown', onKey);

  const canChange = (e) => !!(data && (data.can_change || []).includes(e.calendar) && !e.read_only && !e.busy && !e.repeats);
  async function changed(what) {
    forgetWhere('/api/v2/calendar?'); forget('/api/v2/home/coming');
    note.textContent = what;
    await load(true);
  }
  async function send(action, payload) {
    const r = await api(`/api/v2/calendar/${action}`, { method: 'POST', body: payload }).catch(() => null);
    const d = r ? await r.json().catch(() => ({})) : {};
    return r && r.ok && !d.error ? { ok: true, d } : { ok: false, message: d.message || 'The calendar could not be reached just now.' };
  }

  // ── One event in full ──────────────────────────────────────────────────────────────────────
  function layer(label, opener) {
    const box = h('div', { class: 'ask-layer cal-layer' });
    const close = () => { box.remove(); document.removeEventListener('keydown', key); if (opener && opener.isConnected) opener.focus(); };
    const key = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', key);
    const panel = h('div', { class: 'ask-panel cal-panel', role: 'dialog', 'aria-modal': 'true', 'aria-label': label });
    box.append(h('div', { class: 'ask-scrim', onclick: close }), panel);
    document.body.append(box);
    return { panel, close };
  }
  function details(e, opener) {
    const pal = paletteFor(data, me), p = pal[e.calendar] || { cls: '', colour: 'var(--muted)', name: e.who || e.calendar };
    const { panel, close } = layer(e.title || 'Event', opener);
    const why = e.busy ? `Only the time ${e.who} is busy is shared.`
      : e.repeats ? 'A repeating event: change it on your phone.'
        : e.read_only || !['personal', 'household'].includes(e.calendar) ? `${p.name}'s calendar: you can see it, not change it.`
          : !canChange(e) ? 'To change it, ask your agent.' : '';
    const msg = h('p', { class: 'when', role: 'status' });
    const btns = [];
    if (canChange(e)) {
      btns.push(h('button', { type: 'button', class: 'btn', onclick: () => { close(); editor(e); } }, 'Change'));
      btns.push(h('button', { type: 'button', class: 'btn danger', onclick: async () => {
        if (!(await ask({ title: 'Cancel this event?', text: `“${e.title}” comes off ${p.name === 'Yours' ? 'your calendar' : p.name} for everyone.`, yes: 'Cancel event', danger: true }))) return;
        const r = await send('cancel', { id: e.id });
        if (!r.ok) { msg.textContent = r.message; return; }
        close(); changed(`“${e.title}” is cancelled.`);
      } }, 'Cancel event'));
    } else if (!e.busy && data.agent && !(data.can_change || []).length) {
      btns.push(h('a', { class: 'btn quiet', href: `#/chat/${data.agent}`, onclick: () => draftFor(data.agent, `About “${e.title}” on ${dayWords(parse(e.start))}: `) }, 'Ask your agent'));
    }
    btns.push(h('button', { type: 'button', class: 'btn quiet', onclick: close }, 'Close'));
    panel.append(
      h('div', { class: 'cal-detail-head', style: `--cal:${p.colour}` }, h('span', { class: `dot ${p.cls}`, 'aria-hidden': 'true' }), h('h2', {}, e.busy ? 'Busy' : e.title || '(no title)')),
      h('dl', { class: 'cal-facts' },
        h('dt', {}, 'When'), h('dd', {}, whenLong(e)),
        h('dt', {}, 'Calendar'), h('dd', {}, p.name),
        e.location ? [h('dt', {}, 'Where'), h('dd', {}, placeOf(e.location))] : null,
        e.repeats ? [h('dt', {}, 'Repeats'), h('dd', {}, 'Yes')] : null,
        e.notes ? [h('dt', {}, 'Notes'), h('dd', { class: 'cal-notes' }, e.notes)] : null),
      why ? h('p', { class: 'when' }, why) : null, msg,
      h('div', { class: 'row' }, btns));
    focusIfKeyboard(panel.querySelector('button'));
  }

  // ── Add or change one ──────────────────────────────────────────────────────────────────────
  function editor(e, onDay) {
    if (!data || data.error) return;
    const can = data.can_change || [];
    if (!can.length) {                         // no agent of their own with their calendar: the words go to its chat
      if (!data.agent) { note.textContent = 'There is no agent to add it for you.'; return; }
      draftFor(data.agent, `Add an event on ${dayWords(parse(onDay || day))}: `);
      location.hash = `#/chat/${data.agent}`;
      return;
    }
    const { panel, close } = layer(e ? 'Change event' : 'Add an event', addBtn);
    const allDay0 = e ? !!e.all_day : false;
    const startDay = e ? String(e.start).slice(0, 10) : onDay || day;
    const endDay = e ? (e.all_day ? iso(add(parse(e.end || e.start), e.end ? -1 : 0)) : String(e.end || e.start).slice(0, 10)) : startDay;
    const f = {
      title: h('input', { type: 'text', id: 'cal-f-title', maxlength: '200', required: true, value: e ? e.title : '', autocomplete: 'off' }),
      calendar: h('select', { id: 'cal-f-cal', disabled: e ? true : null },
        can.map((k) => h('option', { value: k, selected: e ? e.calendar === k : k === 'personal' ? true : null }, k === 'personal' ? 'Yours' : 'Household'))),
      allDay: h('input', { type: 'checkbox', id: 'cal-f-allday', checked: allDay0 ? true : null, disabled: e ? true : null }),
      date: h('input', { type: 'date', id: 'cal-f-date', required: true, value: startDay }),
      from: h('input', { type: 'time', id: 'cal-f-from', value: e && !e.all_day ? timeOf(e.start) : '09:00' }),
      to: h('input', { type: 'time', id: 'cal-f-to', value: e && !e.all_day ? timeOf(e.end) : '10:00' }),
      endDate: h('input', { type: 'date', id: 'cal-f-enddate', value: endDay }),
      location: h('input', { type: 'text', id: 'cal-f-where', maxlength: '300', value: e ? e.location || '' : '', autocomplete: 'off' }),
      notes: h('textarea', { id: 'cal-f-notes', rows: '3', maxlength: '1000' }, e ? e.notes || '' : ''),
    };
    const times = h('div', { class: 'cal-form-row' },
      h('label', { class: 'field-label', for: 'cal-f-from' }, 'From', f.from), h('label', { class: 'field-label', for: 'cal-f-to' }, 'To', f.to));
    const ends = h('label', { class: 'field-label', for: 'cal-f-enddate' }, 'Last day', f.endDate);
    const sync = () => { times.hidden = f.allDay.checked; ends.hidden = !f.allDay.checked; };
    f.allDay.addEventListener('change', sync); sync();
    const msg = h('p', { class: 'when', role: 'status' });
    const save = h('button', { type: 'submit', class: 'btn' }, e ? 'Save' : 'Add');
    const form = h('form', { class: 'cal-form', onsubmit: async (ev) => {
      ev.preventDefault();
      const all = f.allDay.checked;
      if (!f.title.value.trim()) { msg.textContent = 'Give it a title.'; return; }
      if (!validDay(f.date.value)) { msg.textContent = 'Pick a day.'; return; }
      const payload = { title: f.title.value.trim(), location: f.location.value.trim(), notes: f.notes.value.trim(),
        start: all ? f.date.value : `${f.date.value} ${f.from.value || '09:00'}`,
        end: all ? (validDay(f.endDate.value) && f.endDate.value > f.date.value ? f.endDate.value : f.date.value)
          : `${f.to.value && f.to.value <= (f.from.value || '') ? iso(add(parse(f.date.value), 1)) : f.date.value} ${f.to.value || f.from.value}` };
      if (!all && !f.to.value) delete payload.end;
      if (e) payload.id = e.id; else payload.calendar = f.calendar.value;
      save.disabled = true; msg.textContent = e ? 'Saving…' : 'Adding…';
      const r = await send(e ? 'update' : 'add', payload);
      save.disabled = false;
      if (!r.ok) { msg.textContent = r.message; return; }
      close();
      day = f.date.value; selected = day;
      changed(e ? `“${payload.title}” is changed.` : `“${payload.title}” is in ${f.calendar.value === 'household' ? 'Household' : 'your calendar'}.`);
    } },
    h('h2', {}, e ? 'Change event' : 'Add an event'),
    h('label', { class: 'field-label', for: 'cal-f-title' }, 'What', f.title),
    h('div', { class: 'cal-form-row' },
      h('label', { class: 'field-label', for: 'cal-f-cal' }, 'Calendar', f.calendar),
      h('label', { class: 'field-label', for: 'cal-f-date' }, 'Day', f.date)),
    h('label', { class: 'cal-check', for: 'cal-f-allday' }, f.allDay, 'All day'),
    times, ends,
    h('label', { class: 'field-label', for: 'cal-f-where' }, 'Where (optional)', f.location),
    h('label', { class: 'field-label', for: 'cal-f-notes' }, 'Notes (optional)', f.notes),
    e ? h('p', { class: 'when' }, 'Its calendar, and whether it is all day, stay as they are.') : null,
    msg, h('div', { class: 'row' }, save, h('button', { type: 'button', class: 'btn quiet', onclick: close }, 'Close')));
    panel.append(form);
    focusIfKeyboard(f.title);
  }

  state.refresh = () => load(true);
  await load();
  return () => { alive = false; document.removeEventListener('keydown', onKey); for (const l of document.querySelectorAll('.cal-layer')) l.remove(); };
}

// ── Home's Month widget (Greg, 27 Nov): this month small; a tap opens the Calendar. Full: today's events under it;
// Compact: the month and its dots only. A size change redraws from what is on screen, asking nothing again. ──
export function monthWidget({ compact = () => false } = {}) {
  const box = h('div', { class: 'cal-mini-box' });      // its placeholder only once it is on Home
  const sec = h('section', { class: 'home-month', 'aria-label': 'Month' },
    h('h2', { class: 'label' }, h('a', { class: 'label-link', href: '#/calendar' }, 'Month')), box);
  let me = '', last = null;
  function draw(d) {
    last = d;
    if (!d || d.error) { box.replaceChildren(none(d && d.error === 'no_calendar' ? 'No calendar yet.' : 'The calendar could not be read just now.')); return; }
    const small = compact();
    const now = new Date(), today = iso(now);
    const pal = paletteFor(d, me), map = byDay(d, loadHidden());
    const todays = map.get(today) || [];
    box.replaceChildren(...[
      ...miniMonth(now, map, pal, { today }),
      small ? null : h('h3', { class: 'sub-label' }, 'Today'),
      small ? null : todays.length ? h('div', { class: 'home-list home-agenda' }, todays.slice(0, 4).map((e) => h('a', { class: 'home-row home-line', href: `#/calendar/day/${today}/${encodeURIComponent(e.id || '')}` },
        h('span', { class: 'home-time' }, e.all_day || String(e.start).slice(0, 10) !== today ? 'All day' : timeOf(e.start)),
        h('span', { class: 'dot', style: `background:${(pal[e.calendar] || {}).colour || 'var(--muted)'}`, 'aria-hidden': 'true' }),
        h('span', { class: 'home-title' }, e.busy ? 'Busy' : e.title),
        h('span', { class: 'home-meta' }, (pal[e.calendar] || {}).name || '')))) : none('Nothing today.')].filter(Boolean));
  }
  return {
    sec,
    show(username) {
      me = username || me;
      if (!box.childElementCount) box.replaceChildren(loading('rows', 3));
      const url = urlOf(rangeOf('month', iso(new Date()))), k = peek(url);
      if (k) draw(k.v);
      if (k && Date.now() - k.t < 5 * MIN) return Promise.resolve();
      return fetchRange(url).then((d) => { if (!k || JSON.stringify(d) !== JSON.stringify(k.v)) draw(d); })
        .catch(() => { if (!k) draw({ error: 'calendar_unavailable' }); }); },
    // Drawn again from what it last had (the size switch); asks only if it has nothing yet.
    redraw() { if (last) draw(last); else this.show(); },
  };
}
