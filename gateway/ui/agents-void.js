// The floating agents (story M4): each agent you can talk to is its Logos mark, in its colour (ST2),
// drifting in the chat's empty space. A mark shows the agent's state: asleep, thinking (typing, or your
// message waiting for it), waiting on you (a Noticeboard request from it), or off (the AI computer is off,
// or the agent is not running). Click a mark to talk to that agent. In a conversation the marks sit in the
// header: the current one with its name, the others small, one click to switch. Names show when you point
// at a mark, or always (Settings; always on a phone). Motion follows Settings: Full, Calm or Still.
import { createRecorder } from './voice.js';
import { handOver } from './home.js';
import { h, getJSON, icon, ICONS, when, loading, reveal, focusIfKeyboard } from './util.js';
import * as mx from './mx.js';

// Moods (Greg, 21 Nov): unplugged when the AI computer (LM Studio) is off; asleep when it is on but the
// agent's model is not loaded; waking while it loads (words that suit the Motion setting, rotating);
// awake when loaded; thinking while it replies; waiting when it needs you; off when the agent is stopped.
// These are the words for an agent's state everywhere (UI review, 28 Nov: Routines said "Running" where the Agents screen said
// "Asleep" for the same agent): "Running" only told that its sandbox was up, which an asleep or awake agent always is.
export const MOODS = { unplugged: 'Unplugged', asleep: 'Asleep', waking: 'Waking up', awake: 'Awake',
  thinking: 'Thinking…', waiting: 'Waiting on you', off: 'Not running' };
// The mood the agent list alone tells (no Matrix or Noticeboard): off, unplugged, awake or asleep.
export const baseMood = (a) => (a.running === false ? 'off' : a.model_up === false ? 'unplugged' : a.model_loaded === true ? 'awake' : 'asleep');
const WAKING = {
  calm: ['Meditating', 'Stretching', 'Yawning', 'Brewing tea', 'Opening one eye', 'Breathing in'],
  full: ['Bouncing off the walls', 'Doing star jumps', 'Revving up', 'Cartwheeling', 'Chugging espresso', 'Warming up'],
  still: ['Waking up'],
};
let wakeTurn = 0;
const wakingWord = () => { const list = WAKING[document.documentElement.dataset.motion] || WAKING.calm; return list[wakeTurn % list.length]; };
// While anything is waking, its words turn over every 2.5 s.
setInterval(() => {
  const els = document.querySelectorAll('.agent-mark.mood-waking .mark-label > span');
  if (!els.length) return;
  wakeTurn += 1;
  for (const el of els) el.textContent = wakingWord();
}, 2500);
const moodWords = (mood) => (mood === 'waking' ? wakingWord() : MOODS[mood]);

// One agent's mark: the Logos glyph in the agent's colour, with its name and mood under it.
export function mark(agent, { size = 'big', mood = 'asleep', href = null, current = false } = {}) {
  const tag = href ? 'a' : 'span';
  return h(tag, { class: `agent-mark ${size} mood-${mood}${current ? ' current' : ''}`, href: href || null,
    style: `--mark: var(--agent-${agent.name}, var(--${agent.name}, var(--accent)))`, 'data-agent': agent.name,
    'aria-label': `${agent.display_name}: ${MOODS[mood]}${href ? '. Talk to it' : ''}`, title: `${agent.display_name} · ${MOODS[mood]}` },
  // The name rides inside the drifting part, so it moves with its mark (Greg, 21 Nov).
  // The microphone is added into .mark-top, inside the drifting part, so it stays on the logo as it moves (Greg, 23 Nov).
  h('span', { class: 'mark-fx' }, h('span', { class: 'mark-fy' },
    h('span', { class: 'mark-top' }, h('span', { class: 'mark-glow', 'aria-hidden': 'true' }, h('span', { class: 'mark-glyph' }))),
    h('span', { class: 'mark-label', 'aria-hidden': 'true' }, h('strong', {}, agent.display_name), h('span', {}, moodWords(mood))))));
}

// A group's picture: the people icon in a circle of its first agent's colour. The same in the Chats list, the chat
// switcher and the group's own header (Greg, 28 Nov: the group's header had no picture).
export const groupAvatar = (agent) => h('span', { class: 'void-avatar group', 'aria-hidden': 'true',
  style: `--mark: var(--agent-${agent?.name}, var(--accent))` }, icon(ICONS.people));

// Each agent's state, from its Matrix room (this browser), the Noticeboard and the agent list.
export async function moods(agents) {
  // Fresh AI-computer state each time (the server checks LM Studio at most every 20 s).
  try {
    const live = Object.fromEntries(((await getJSON('/api/v2/agents')).agents || []).map((a) => [a.name, a]));
    agents = agents.map((a) => (live[a.name] ? { ...a, running: live[a.name].running, model_up: live[a.name].model_up, model_loaded: live[a.name].model_loaded } : a));
  } catch (e) { /* keep what we had */ }
  const out = Object.fromEntries(agents.map((a) => [a.name, baseMood(a)]));
  try {
    const nb = await getJSON('/api/v2/noticeboard');
    for (const m of nb.waiting || []) if (out[m.from] && !['off', 'unplugged'].includes(out[m.from])) out[m.from] = 'waiting';
  } catch (e) { /* the Noticeboard is a nicety here */ }
  try {
    const c = mx.saved() ? await mx.get() : null;
    if (c) {
      const me = c.getUserId();
      for (const a of agents) {
        if (!a.matrix_id || ['off', 'unplugged'].includes(out[a.name])) continue;
        const rooms = c.getRooms().filter((r) => r.getMyMembership() === 'join' && r.getMember(a.matrix_id)?.membership === 'join');
        for (const r of rooms) {
          const typing = r.getMember(a.matrix_id)?.typing;
          const evs = r.getLiveTimeline().getEvents().filter((e) => e.getType() === 'm.room.message' && e.getContent().msgtype !== 'm.notice');
          const last = evs[evs.length - 1];
          const unanswered = last && last.getSender() === me && Date.now() - last.getTs() < 15 * 60000;
          // Your message waits and the model is not loaded yet: it is loading (waking); else thinking.
          if (typing) out[a.name] = 'thinking';
          else if (unanswered) out[a.name] = out[a.name] === 'asleep' ? 'waking' : 'thinking';
        }
      }
    }
  } catch (e) { /* Matrix not ready: the marks stay as they are */ }
  return out;
}

// Where the person put each agent (this device), by dragging it (Greg, 21 Nov): { phone|wide: { name: [x%, y%] } }.
// A phone and a wide screen are different shapes, so each keeps its own places.
const SPOTS_KEY = 'logos.void.spots';
const savedSpots = () => { try { const v = JSON.parse(localStorage.getItem(SPOTS_KEY) || '{}'); return v && typeof v === 'object' ? v : {}; } catch (e) { return {}; } };
const saveSpots = (v) => { try { localStorage.setItem(SPOTS_KEY, JSON.stringify(v)); } catch (e) { /* private window */ } };

// The void: every agent floating, nothing else, until you pick one.
// Press and hold a mark (half a second) to lift it, then put it anywhere (onto another: they swap). To speak to an
// agent, tap the small microphone on its mark, then ■ to send (Greg, 23 Nov: it was a long hold with a swipe, easy to get wrong).
export async function voidView(root, agents, others = []) {
  let alive = true;
  const field = h('div', { class: 'void-field' });
  // No question or how-to above the agents (Greg, 22 Nov: that belongs in onboarding). The line is
  // there only while it says something: recording, sending, or what went wrong.
  const ask = h('p', { class: 'void-ask', role: 'status', hidden: true });
  // Speaking to an agent (Greg, 23 Nov): a small microphone on its mark. Tap to start, tap ■ to send: its words go to its chat as
  // a voice message. (It was a long hold with a swipe up to send and down to cancel: too easy to get wrong.)
  const dock = h('div', { class: 'void-rec-dock' });
  const recorders = {};
  const sayAsk = (t) => { if (typeof t === 'string') tell(t); else { ask.replaceChildren(t); ask.hidden = false; } };
  const markOf = (name) => field.querySelector(`.agent-mark[data-agent="${name}"]`);
  const recording = () => Object.values(recorders).some((r) => r.recording || r.busy);
  function recorderFor(a) {
    if (recorders[a.name]) return recorders[a.name];
    const r = createRecorder({ agent: a.name, say: sayAsk,
      onWords: (text, meta) => { handOver(a.name, text, meta); location.hash = `#/chat/${a.name}`; },
      onRecording: (on) => {
        const el = markOf(a.name);
        if (el) { el.classList.toggle('recording', on); if (on) label(el)?.replaceChildren('Listening…'); }
        if (!on) draw(lastMoods);
      } });
    dock.append(r.bar);
    return (recorders[a.name] = r);
  }
  // Spots spread evenly for however many agents there are, centred (on a wide screen two agents sat
  // off to the left). Yours first, then the private ones, unless the person has moved them.
  const all = [...agents.map((a) => ({ a, mine: true })), ...others.map((a) => ({ a, mine: false }))];
  const layout = () => (window.matchMedia('(max-width: 599px)').matches ? 'phone' : 'wide');
  const spot = (i, n) => {
    if (layout() === 'phone') {                               // two columns, rows down the screen
      const rows = Math.ceil(n / 2), row = Math.floor(i / 2), last = i === n - 1 && n % 2 === 1;
      return [last ? 50 : (i % 2 ? 72 : 28), rows === 1 ? 40 : 16 + (58 * row) / Math.max(rows - 1, 1)];
    }
    const x = n === 1 ? 50 : 18 + (64 * i) / (n - 1);
    const y = [44, 34, 54, 40, 50, 36][i % 6];
    return [x, y];
  };
  const spots = savedSpots();
  const placeOf = (name, i) => (spots[layout()] || {})[name] || spot(i, all.length);
  let lastMoods = Object.fromEntries(agents.map((a) => [a.name, 'asleep']));
  let busy = false, redrawWanted = false;
  // Each mark is made once and then updated in place (Greg, 22 Nov: the marks jumped every few seconds
  // when they were remade for a new mood, their drift starting again from the beginning). Only a change
  // in which agents there are makes them anew.
  const moodOf = (a, mine, m) => (mine ? (m[a.name] || 'asleep') : (a.running === false ? 'off' : 'asleep'));
  const fill = (el, a, mine, i, m) => {
    const mood = moodOf(a, mine, m);
    el.className = el.className.replace(/\bmood-[\w-]+/, `mood-${mood}`);
    if (mine) {
      el.title = `${a.display_name} · ${MOODS[mood]}`;
      el.setAttribute('aria-label', `${a.display_name}: ${MOODS[mood]}. Talk to it. Hold to move it`);
      const words = el.querySelector('.mark-label > span');
      if (words && words.textContent !== moodWords(mood)) words.textContent = moodWords(mood);
    }
    const [x, y] = placeOf(a.name, i);
    if (el.style.getPropertyValue('--x') !== `${x}%`) el.style.setProperty('--x', `${x}%`);
    if (el.style.getPropertyValue('--y') !== `${y}%`) el.style.setProperty('--y', `${y}%`);
  };
  const draw = (m) => {
    lastMoods = m;
    if (busy) { redrawWanted = true; return; }               // never redraw under a finger
    const have = [...field.querySelectorAll(':scope > .agent-mark')];
    if (have.length === all.length && have.every((el, i) => el.dataset.agent === all[i].a.name)) {
      all.forEach(({ a, mine }, i) => fill(have[i], a, mine, i, m));
      return;
    }
    field.replaceChildren(...all.map(({ a, mine }, i) => {
      // Not a real link (Greg, 21 Nov): a long press on one made Android offer to copy or save it. A tap
      // still opens the chat (the click handler below), and Enter does too.
      const el = mark(a, { size: 'big', mood: moodOf(a, mine, m) });
      if (mine) { el.dataset.href = `#/chat/${a.name}`; el.setAttribute('role', 'link'); el.tabIndex = 0; }
      if (mine && a.voice) {
        el.querySelector('.mark-top').append(h('button', { type: 'button', class: 'mark-mic', 'aria-label': `Record a voice message for ${a.display_name}`, title: 'Record a voice message',
          onclick: (e) => { e.preventDefault(); e.stopPropagation(); if (!recording()) recorderFor(a).start(); } }, icon(ICONS.mic)));
      }
      if (!mine) {
        el.classList.add('private');
        el.title = `${a.display_name} · private to its owner`;
        el.setAttribute('aria-label', `${a.display_name}: private to its owner`);
        el.querySelector('.mark-label span')?.replaceChildren(icon(ICONS.lock), ' Private');
      }
      el.classList.add(`drift-${i % 3}`);
      fill(el, a, mine, i, m);
      el.draggable = false;
      return el;
    }));
  };

  // Smooth moves: each mark glides from where it was to where it now is (FLIP, on `translate`, which
  // leaves the marks' own transform alone).
  const rects = () => Object.fromEntries([...field.querySelectorAll('.agent-mark')].map((el) => [el.dataset.agent, el.getBoundingClientRect()]));
  function glide(before) {
    const still = document.documentElement.dataset.motion === 'still' || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (still) return;
    for (const el of field.querySelectorAll('.agent-mark')) {
      const was = before[el.dataset.agent]; if (!was) continue;
      const now = el.getBoundingClientRect();
      const dx = was.left - now.left, dy = was.top - now.top;
      if (!dx && !dy) continue;
      el.style.transition = 'none'; el.style.translate = `${dx}px ${dy}px`;
      requestAnimationFrame(() => requestAnimationFrame(() => {
        el.style.transition = 'translate .45s cubic-bezier(.2, .8, .2, 1)'; el.style.translate = '0px 0px';
        el.addEventListener('transitionend', () => { el.style.transition = ''; el.style.translate = ''; }, { once: true });
      }));
    }
  }

  // ── press, hold, move, record ────────────────────────────────────────────────────────────────
  let g = null, swallowClick = false;
  const LIFT_MS = 500, SLOP = 10;
  const label = (el) => el.querySelector('.mark-label > span');
  const tell = (t) => { ask.textContent = t; ask.hidden = !t; };
  const resetAsk = () => { ask.textContent = ''; ask.hidden = true; };
  // An outcome ("Cancelled", "Not sent…") shows for a few seconds, then the question comes back
  // (Greg, 21 Nov: "Not sent" stayed there for good).
  const flash = (t) => { tell(t); setTimeout(() => { if (ask.textContent === t) resetAsk(); }, 4000); };
  function finish() {
    if (!g) return;
    clearTimeout(g.t1);
    g.el.classList.remove('lifted', 'dragging', 'recording', 'will-send', 'will-cancel');
    if (!g.drag) g.el.style.translate = '';
    for (const el of field.querySelectorAll('.drop-target')) el.classList.remove('drop-target');
    window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); window.removeEventListener('pointercancel', onUp);
    document.documentElement.classList.remove('holding-mark');
    g = null; busy = false;
    if (redrawWanted) { redrawWanted = false; const before = rects(); draw(lastMoods); glide(before); }
  }
  function nearest(x, y) {
    let best = null, bestD = Infinity;
    for (const el of field.querySelectorAll('.agent-mark')) {
      if (el === g.el) continue;
      const r = el.getBoundingClientRect(); const d = Math.hypot(r.left + r.width / 2 - x, r.top + r.height / 2 - y);
      if (d < bestD) { best = el; bestD = d; }
    }
    return bestD < 70 ? best : null;                          // right on another: a swap; else a new place
  }
  function onMove(e) {
    if (!g || e.pointerId !== g.id) return;
    const dx = e.clientX - g.x0, dy = e.clientY - g.y0;
    if (!g.lifted) { if (Math.hypot(dx, dy) > SLOP) finish(); return; }     // a swipe or a scroll, not a hold
    if (!g.drag && Math.hypot(dx, dy) > SLOP) { g.drag = true; g.el.classList.add('dragging'); label(g.el)?.replaceChildren('Move me'); }
    if (!g.drag) return;
    g.el.style.translate = `${dx}px ${dy}px`;
    const t = nearest(e.clientX, e.clientY);
    for (const el of field.querySelectorAll('.drop-target')) if (el !== t) el.classList.remove('drop-target');
    t?.classList.add('drop-target');
  }
  function onUp(e) {
    if (!g || (e.pointerId !== undefined && e.pointerId !== g.id)) return;
    const was = g;
    if (was.lifted || was.drag) swallowClick = true;
    if (was.drag) {
      const target = field.querySelector('.drop-target');
      const before = rects();
      const r = was.el.getBoundingClientRect(); before[was.name] = r;        // it glides home from where it was let go
      was.el.style.translate = '';
      const i = all.findIndex((x) => x.a.name === was.name);
      const here = spots[layout()] = { ...(spots[layout()] || {}) };
      if (target) {                                           // onto another: they swap places
        const j = all.findIndex((x) => x.a.name === target.dataset.agent);
        const a = placeOf(was.name, i), b = placeOf(target.dataset.agent, j);
        here[was.name] = b; here[target.dataset.agent] = a;
      } else {                                                // anywhere else: it stays where it was let go
        const f = field.getBoundingClientRect();
        const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;       // the mark is placed by its centre
        here[was.name] = [Math.round(clamp(((cx - f.left) / f.width) * 100, 14, 86) * 10) / 10,
          Math.round(clamp(((cy - f.top) / f.height) * 100, 12, 88) * 10) / 10];
      }
      saveSpots(spots);
      g.lifted = false; finish();
      draw(lastMoods); glide(before);
      resetAsk();
      return;
    }
    finish(); resetAsk();
  }
  field.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.mark-mic')) return;                // the microphone is a button, not a handle
    const el = e.target.closest('.agent-mark');
    if (!el || g || (e.pointerType === 'mouse' && e.button !== 0)) return;
    swallowClick = false;
    g = { el, name: el.dataset.agent, mine: !!el.dataset.href, id: e.pointerId, x0: e.clientX, y0: e.clientY, lifted: false, drag: false };
    busy = true;
    g.t1 = setTimeout(() => {
      if (!g) return;
      g.lifted = true; g.el.classList.add('lifted'); navigator.vibrate?.(10);
      document.documentElement.classList.add('holding-mark');
      tell('Move it');
    }, LIFT_MS);
    window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', onUp); window.addEventListener('pointercancel', onUp);
  });
  // A hold is not a tap: no link opened, no long-press menu.
  field.addEventListener('click', (e) => { if (swallowClick) { e.preventDefault(); e.stopPropagation(); swallowClick = false; } }, true);
  field.addEventListener('click', (e) => { const el = e.target.closest('.agent-mark[data-href]'); if (el) location.hash = el.dataset.href; });
  field.addEventListener('keydown', (e) => { if (e.target.closest('.mark-mic')) return; const el = e.target.closest('.agent-mark[data-href]'); if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); location.hash = el.dataset.href; } });
  field.addEventListener('contextmenu', (e) => { if (e.target.closest('.agent-mark')) e.preventDefault(); });
  field.addEventListener('dragstart', (e) => e.preventDefault());

  // Conversations (Greg, 22 Nov): under the agents, each agent's chat and each group (F36), newest first,
  // with its last message and an unread count, so more groups and agents only add rows.
  const convBox = h('nav', { class: 'void-groups', 'aria-label': 'Chats' }, loading('cards', 2));
  let convShown = false;
  // The chats list for more rooms and agents (Greg, 23 Nov): each with its picture, filters, a search once
  // there are many, and the first eight with "Show more". Kept across the 15-second refresh.
  const convView = { filter: 'all', query: '', all: false };
  root.replaceChildren(h('section', { class: 'chat void', 'aria-label': 'Your agents' },
    h('header', { class: 'page-band void-band' }, h('h1', {}, 'Agents')), ask, field, convBox, dock));
  const short = (ms) => when(ms / 1000);
  // F36 part 2 (Greg, 22 Nov): start a group from here, and answer an invitation to one.
  // A sheet over the screen (Greg, 22 Nov: inside the page it squeezed the agents to a sliver on a phone).
  const form = h('div', { class: 'void-newgroup', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'New group' });
  const scrim = h('div', { class: 'void-scrim', onclick: () => closeForm() });
  const sheet = h('div', { class: 'void-sheet', hidden: true }, scrim, form);
  document.body.append(sheet);
  const onKey = (e) => { if (e.key === 'Escape' && !sheet.hidden) closeForm(); };
  document.addEventListener('keydown', onKey);
  function closeForm() { sheet.hidden = true; document.documentElement.classList.remove('sheet-open'); }
  async function openForm() {
    const c = await mx.get();
    const me = c.getUserId();
    const server = me.split(':').slice(1).join(':');
    const people = ((await getJSON('/api/v2/people').catch(() => ({ people: [] }))).people || []).filter((p) => !p.is_you);
    const name = h('input', { id: 'ng-name', type: 'text', placeholder: 'e.g. Holiday planning', maxlength: '60', autocomplete: 'off' });
    const tick = (id, label, sub, checked) => h('label', { class: 'ng-pick' },
      h('input', { type: 'checkbox', value: id, checked: checked ? '' : null }), h('span', {}, label, sub ? h('small', { class: 'when' }, ` ${sub}`) : null));
    const peopleBox = h('div', { class: 'ng-picks' }, ...people.map((p) => tick(`@${p.username}:${server}`, p.display_name, '', false)));
    const agentBox = h('div', { class: 'ng-picks' }, ...agents.filter((a) => a.matrix_id).map((a) =>
      tick(a.matrix_id, a.display_name, a.shared ? '' : '· answers only you', !!a.shared)));
    const msg = h('p', { class: 'when', role: 'status' });
    const go = h('button', { type: 'submit', class: 'btn' }, 'Create group');
    const picked = (box) => [...box.querySelectorAll('input:checked')].map((i) => i.value);
    form.replaceChildren(h('form', { class: 'settings-form', onsubmit: async (e) => {
      e.preventDefault();
      const ppl = picked(peopleBox), bots = picked(agentBox), title = name.value.trim();
      if (!title) { msg.textContent = 'Give the group a name.'; name.focus(); return; }
      if (!bots.length) { msg.textContent = 'Pick at least one agent: a group is people and agents together.'; return; }
      go.disabled = true; msg.textContent = 'Creating the group…';
      try {
        const r = await c.createRoom({ name: title, preset: 'private_chat', invite: [...ppl, ...bots],
          initial_state: [{ type: 'm.room.encryption', state_key: '', content: { algorithm: 'm.megolm.v1.aes-sha2' } }] });
        for (let i = 0; i < 50 && !c.getRoom(r.room_id); i++) await new Promise((ok) => setTimeout(ok, 200));
        const agent = agents.find((a) => a.matrix_id === bots[0]);
        location.hash = `#/chat/${agent.name}/group/${encodeURIComponent(r.room_id)}`;
      } catch (err) { msg.textContent = `The group could not be created: ${err.message}`; go.disabled = false; }
    } },
    h('h2', { class: 'label' }, 'New group'),
    h('label', { for: 'ng-name', class: 'field-label' }, 'Name'), name,
    people.length ? h('p', { class: 'field-label' }, 'People') : null, people.length ? peopleBox : null,
    h('p', { class: 'field-label' }, 'Agents'), agentBox,
    h('p', { class: 'when' }, 'Agents answer in a group when someone asks them with the @ button. Everyone invited gets an invitation to accept.'),
    h('div', { class: 'row' }, go, h('button', { type: 'button', class: 'btn quiet', onclick: () => closeForm() }, 'Cancel')), msg));
    sheet.hidden = false;
    document.documentElement.classList.add('sheet-open');
    form.scrollTop = 0;
    focusIfKeyboard(name);              // a phone keeps its keyboard down until the box is tapped
  }
  const newBtn = h('button', { type: 'button', class: 'linkish void-new', onclick: () => openForm() }, '+ New group');

  const showConvs = async () => {
    try {
      const [{ conversationsOf }, c] = await Promise.all([import('./chat-mx.js'), mx.get()]);
      const list = conversationsOf(c, agents);
      // Invitations to groups waiting for you: Join or Decline (agents accept theirs by themselves).
      const invites = c.getRooms().filter((r) => r.getMyMembership() === 'invite');
      if (!alive) return;
      const inviter = (r) => { const m = r.getMember(c.getUserId()); const by = m?.events?.member?.getSender?.(); return by ? (r.getMember(by)?.name || by) : ''; };
      if (!convShown) { convShown = true; reveal(convBox); }
      const FILTERS = [['all', 'All'], ['agents', 'Agents'], ['groups', 'Groups'], ['unread', 'Unread']];
      const counts = { all: list.length, agents: list.filter((r) => r.kind === 'agent').length,
        groups: list.filter((r) => r.kind === 'group').length, unread: list.filter((r) => r.unread).length };
      const q = convView.query.trim().toLowerCase();
      const matching = list.filter((r) => (convView.filter === 'all' || (convView.filter === 'agents' && r.kind === 'agent')
        || (convView.filter === 'groups' && r.kind === 'group') || (convView.filter === 'unread' && r.unread))
        && (!q || `${r.name} ${r.text || ''}`.toLowerCase().includes(q)));
      const SHOWN = 8;
      const visible = convView.all || q ? matching : matching.slice(0, SHOWN);
      const chips = list.length > 3 ? h('div', { class: 'void-filters', role: 'tablist', 'aria-label': 'Show' },
        FILTERS.filter(([k]) => k === 'all' || counts[k]).map(([k, label]) => h('button', { type: 'button', role: 'tab', class: `chip${convView.filter === k ? ' on' : ''}`,
          'aria-selected': String(convView.filter === k), onclick: () => { convView.filter = k; convView.all = false; showConvs(); } },
        label, k !== 'all' ? h('span', { class: 'void-filter-n' }, String(counts[k])) : null))) : null;
      let search = null;
      if (list.length > 6) {
        search = h('input', { type: 'search', class: 'void-search', placeholder: 'Find a chat…', 'aria-label': 'Find a chat', autocomplete: 'off', value: convView.query });
        search.addEventListener('input', () => { convView.query = search.value; const at = search.selectionStart; showConvs().then(() => { const s2 = convBox.querySelector('.void-search'); if (s2) { s2.focus(); s2.setSelectionRange(at, at); } }); });
      }
      const picture = (r) => (r.kind === 'agent' ? mark(r.agent, { size: 'tiny' })
        : groupAvatar(r.agent));
      convBox.replaceChildren(h('div', { class: 'void-convs-head' }, h('h2', { class: 'label' }, 'Chats'), newBtn),
        ...(chips || search ? [h('div', { class: 'void-tools' }, chips, search)] : []),
        ...invites.map((r) => h('div', { class: 'void-group invite' },
          h('span', { class: 'void-group-head' }, h('span', { class: 'void-group-name' }, r.name || 'A group'), h('span', { class: 'void-unread' }, 'Invited')),
          h('span', { class: 'void-group-last' }, inviter(r) ? `${inviter(r)} invited you` : 'You are invited'),
          h('span', { class: 'row void-invite-actions' },
            h('button', { type: 'button', class: 'btn small', onclick: async (e) => { e.target.disabled = true; try { await c.joinRoom(r.roomId); } catch (err) { e.target.disabled = false; } setTimeout(showConvs, 800); } }, 'Join'),
            h('button', { type: 'button', class: 'btn quiet small', onclick: async (e) => { e.target.disabled = true; try { await c.leave(r.roomId); } catch (err) { e.target.disabled = false; } setTimeout(showConvs, 800); } }, 'Decline')))),
        ...visible.map((r) => h('a', { href: r.link, class: `void-group with-pic${r.unread ? ' unread' : ''}`, 'data-kind': r.kind },
          h('span', { class: 'void-pic' }, picture(r), r.unread ? h('span', { class: 'void-dot', 'aria-hidden': 'true' }) : null),
          h('span', { class: 'void-group-body' },
            h('span', { class: 'void-group-head' }, h('span', { class: 'void-group-name' }, r.name),
              h('span', { class: 'when' }, r.ts ? short(r.ts) : ''),
              r.unread ? h('span', { class: 'void-unread', 'aria-label': `${r.unread} unread` }, String(r.unread)) : null),
            h('span', { class: 'void-group-last' }, r.text ? `${r.kind === 'group' && r.who ? `${r.who}: ` : r.who === 'You' ? 'You: ' : ''}${r.text}` : 'Nothing said yet')))),
        ...(!matching.length && list.length ? [h('p', { class: 'none' }, q ? 'No chat matches.' : 'Nothing here.')] : []),
        ...(visible.length < matching.length ? [h('button', { type: 'button', class: 'linkish void-more', onclick: () => { convView.all = true; showConvs(); } },
          `Show ${matching.length - visible.length} more`)] : []));
      convBox.hidden = false;
    } catch (e) { convBox.hidden = true; /* no Matrix in this browser: no list to show */ }
  };
  showConvs();
  const convTimer = setInterval(() => { if (document.visibilityState === 'visible') showConvs(); }, 15000);
  draw(lastMoods);
  const tick = async () => { const m = await moods(agents); if (alive) draw(m); };
  await tick();
  const t = setInterval(tick, 8000);
  return () => { alive = false; clearInterval(t); clearInterval(convTimer); sheet.remove(); document.removeEventListener('keydown', onKey); for (const r of Object.values(recorders)) r.destroy();
    document.documentElement.classList.remove('sheet-open'); if (g) finish(); };
}



// Groups for the agent switcher (Greg, 28 Nov: from Household Group you could switch to an agent but not back):
// it lists the agents first, then your groups. Needs this browser's chat connection; none, no groups.
async function groupRows(agents) {
  try {
    const [{ conversationsOf }, c] = await Promise.all([import('./chat-mx.js'), mx.get()]);
    return c ? conversationsOf(c, agents).filter((r) => r.kind === 'group') : [];
  } catch (e) { return []; }
}
const tick = () => h('span', { class: 'switch-tick', 'aria-hidden': 'true' }, '✓');
function groupItems(rows, here) {
  if (!rows.length) return [];
  return [h('div', { class: 'switch-head', role: 'presentation' }, 'Groups'),
    ...rows.map((r) => h('a', { href: r.link, role: 'option', class: `switch-item${r.link === here ? ' on' : ''}`, 'aria-selected': String(r.link === here), 'data-group': r.link },
      groupAvatar(r.agent), h('span', { class: 'switch-text' }, h('strong', {}, r.name), h('span', { class: 'switch-mood' }, 'Group')),
      r.link === here ? tick() : null))];
}

// The views of an agent, in the pill on the right (Greg, 21 Nov): it names where you are and opens to the others.
const LABELS = { chat: 'Chat', brain: 'Brain', about: 'Routines', settings: 'Settings' };
// Picking another agent keeps you where you are (Greg, 23 Nov): its chat, brain, routines or settings.
const PLACE = { chat: (n) => `#/chat/${n}`, brain: (n) => `#/brain/${n}`, about: (n) => `#/agents/${n}`, settings: (n) => `#/agents/${n}/settings` };
const phone = () => window.matchMedia('(max-width: 599px)').matches;

// One header for every agent screen (UI review, recommendation 6, Greg, 28 Nov: each screen had its own name switch and
// view pill, and the group's header was another again). As Home Assistant's one top bar: ‹ back to all agents and chats;
// the name with ▾, which lists the agents (with what each is doing), then your groups, the one you are in ticked, with a
// search once there are more than five agents; and on the right the view pill (Chat, Brain, Routines, Settings) where the
// screen has views. The same control in the same place in an agent's chat, its Brain, Routines and Settings, and in a
// group (its picture and who is in it in the name; no views). `tools` are the screen's own buttons, just before the pill.
// In a chat Ctrl+K (⌘K) opens the switcher (Greg, 23 Nov); arrows and Enter move and pick, Escape closes.
// No logo beside the name (Greg, 28 Nov); the group keeps its picture.
// `beside(href)`: on a very wide screen a button opens the same list to pick a chat to open beside this one (Greg, 28 Nov).
// With two chats on screen Ctrl+K goes to the one you are in (hotkeyHere).
export function agentBar(agent, active, agents = [], { group = null, tools = [], hotkey = false, beside = null } = {}) {
  const here = group ? group.link : null;
  const go = PLACE[active] || PLACE.chat;
  const list = h('div', { class: 'switch-list', role: 'listbox', 'aria-label': 'Switch agent or group' });
  const search = agents.length > 5 ? h('input', { type: 'search', class: 'switch-search', placeholder: 'Find an agent…', 'aria-label': 'Find an agent', autocomplete: 'off' }) : null;
  const besideHead = beside ? h('p', { class: 'switch-beside-head', hidden: true }, 'Open beside this chat') : null;
  const pop = h('div', { class: `switch-pop agent-bar-pop${group ? ' group-pop' : ''}`, hidden: true },
    besideHead, search, list, h('a', { href: '#/chat', class: 'switch-all' }, 'All agents and chats'));
  const items = agents.map((a) => {
    const on = !group && a.name === agent.name;
    return h('a', { href: go(a.name), class: `switch-item${on ? ' on' : ''}`, role: 'option', 'aria-selected': String(on), 'data-agent': a.name },
      mark(a, { size: 'tiny', mood: baseMood(a) }), h('span', { class: 'switch-text' }, h('strong', {}, a.display_name), h('span', { class: 'switch-mood' }, MOODS[baseMood(a)])),
      on ? tick() : null);
  });
  list.replaceChildren(...items);
  let groupsShown = false;
  const addGroups = async () => {
    const extra = groupItems(await groupRows(agents), here);
    if (!extra.length || groupsShown) return;
    groupsShown = true; list.append(...extra);
    items.push(...extra.filter((el) => el.matches('a')));
  };
  addGroups();
  const btn = h('button', { type: 'button', class: `switch-btn agent-bar-switch${group ? ' group-switch' : ''}`, 'aria-haspopup': 'listbox', 'aria-expanded': 'false',
    title: hotkey ? 'Switch agent or group (Ctrl+K)' : 'Switch agent or group' },
  group ? groupAvatar(agent) : null,
  group ? h('span', { class: 'group-title' }, group.title, group.who) : h('span', { class: 'mark-name' }, agent.display_name),
  h('span', { class: 'switch-caret', 'aria-hidden': 'true' }, '▾'));
  const shown = () => items.filter((el) => !el.hidden);
  const filter = () => { const q = (search?.value || '').trim().toLowerCase(); for (const el of items) el.hidden = !!q && !el.textContent.toLowerCase().includes(q); };
  // What each agent is doing, fresh each time the list opens.
  const freshen = async () => {
    const m = await moods(agents).catch(() => ({}));
    for (const el of items) {
      if (!el.dataset.agent) continue;
      const md = m[el.dataset.agent] || 'asleep';
      el.querySelector('.switch-mood').textContent = MOODS[md];
      const mk = el.querySelector('.agent-mark'); if (mk) mk.className = mk.className.replace(/mood-\w+/, `mood-${md}`);
    }
  };
  let besideMode = false;
  const open = (forBeside = false) => {
    if (!groupsShown) addGroups();
    besideMode = !!(forBeside && beside);
    pop.classList.toggle('beside-mode', besideMode);
    if (besideHead) besideHead.hidden = !besideMode;
    if (besideBtn) besideBtn.setAttribute('aria-expanded', String(besideMode));
    pop.hidden = false; btn.setAttribute('aria-expanded', 'true'); bar.classList.add('open');
    if (search) { search.value = ''; filter(); search.focus(); } else (items.find((el) => el.classList.contains('on')) || items[0])?.focus();
    freshen();
  };
  const close = (refocus) => {
    if (pop.hidden) return;
    pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); besideBtn?.setAttribute('aria-expanded', 'false'); bar.classList.remove('open');
    if (refocus) (besideMode ? besideBtn : btn).focus();
  };
  btn.addEventListener('click', () => (pop.hidden || besideMode ? open() : close(true)));
  const besideBtn = beside ? h('button', { type: 'button', class: 'bar-beside', 'aria-haspopup': 'listbox', 'aria-expanded': 'false',
    'aria-label': 'Open a chat beside this one', title: 'Open a chat beside this one',
    onclick: () => (!pop.hidden && besideMode ? close(true) : open(true)) }, icon(ICONS.beside)) : null;
  search?.addEventListener('input', filter);
  pop.addEventListener('keydown', (e) => {
    const vis = shown(), i = vis.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); close(true); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); (vis[i + 1] || vis[0])?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (i <= 0 ? (search || vis[vis.length - 1]) : vis[i - 1])?.focus(); }
    else if (e.key === 'Enter' && document.activeElement === search && vis[0]) { e.preventDefault(); vis[0].click(); }
  });
  pop.addEventListener('click', (e) => {
    const a = e.target.closest('a');
    if (!a) return;
    // Picking for the chat beside: it opens there; this chat stays.
    if (besideMode && a.classList.contains('switch-item')) { e.preventDefault(); close(false); beside(a.getAttribute('href')); return; }
    close(false);
  });
  const outside = (e) => { if (!bar.isConnected) { document.removeEventListener('pointerdown', outside, true); return; } if (!who.contains(e.target) && !besideBtn?.contains(e.target)) close(false); };
  document.addEventListener('pointerdown', outside, true);
  if (hotkey) {
    const key = (e) => {
      if (!bar.isConnected) { document.removeEventListener('keydown', key); return; }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && hotkeyHere(bar)) { e.preventDefault(); pop.hidden ? open() : close(true); }
    };
    document.addEventListener('keydown', key);
  }
  const who = h('span', { class: 'agent-bar-who' }, btn, pop);

  // The view pill.
  let menu = null;
  if (LABELS[active]) {
    const item = (k, href, extra = {}) => h('a', { href, class: `agent-tab${k === active ? ' on' : ''}`, role: 'menuitem',
      'aria-current': k === active ? 'page' : null, ...extra }, LABELS[k],
      k === 'brain' && phone() ? h('span', { class: 'agent-tab-out', 'aria-hidden': 'true' }, ' ↗') : null);
    menu = h('details', { class: 'agent-menu' },
      h('summary', { class: 'agent-tab on', 'aria-label': `${LABELS[active]}: switch to Chat, Brain, Routines or Settings` },
        LABELS[active], h('span', { class: 'agent-menu-caret', 'aria-hidden': 'true' }, '▾')),
      h('div', { class: 'agent-menu-list', role: 'menu' },
        item('chat', `#/chat/${agent.name}`),
        // On a phone Brain opens the agent's vault straight in Obsidian (Greg, 21 Nov); a desktop has no Obsidian, so
        // there it is the reader in Logos.
        phone() ? item('brain', `obsidian://open?vault=${encodeURIComponent(agent.obsidian_vault || `brain-${agent.name}`)}&file=README.md`,
          { title: 'Open this brain in Obsidian' }) : item('brain', `#/brain/${agent.name}`),
        item('about', `#/agents/${agent.name}`),
        item('settings', `#/agents/${agent.name}/settings`)));
    menu.addEventListener('click', (e) => { if (e.target.closest('a')) menu.open = false; });
    const shut = (e) => {
      if (!menu.isConnected) { document.removeEventListener('pointerdown', shut, true); return; }
      if (menu.open && !menu.contains(e.target)) menu.open = false;
    };
    document.addEventListener('pointerdown', shut, true);
  }
  const bar = h('nav', { class: `agent-bar one-bar${group ? ' group-bar' : ''}${hotkey ? ' has-hotkey' : ''}`, 'aria-label': group ? 'Group' : agent.display_name },
    h('a', { href: '#/chat', class: 'agent-back', 'aria-label': 'All agents and chats', title: 'All agents and chats' }, '‹'),
    who, ...tools.filter(Boolean), besideBtn, menu);
  return bar;
}
// Two chats side by side (Greg, 28 Nov): each is a .chat-pane; the one you last clicked or typed in has .pane-active.
// Ctrl+K belongs to that one while it is on screen, else to the first on screen. A bar outside any pane always takes it.
function hotkeyHere(bar) {
  const pane = bar.closest('.chat-pane');
  if (!pane) return true;
  const shown = [...document.querySelectorAll('.chat-pane')].filter((p) => p.querySelector('section.chat')?.offsetParent);
  const owner = shown.find((p) => p.classList.contains('pane-active')) || shown[0];
  return owner === pane;
}
