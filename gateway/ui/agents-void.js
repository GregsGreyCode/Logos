// The floating agents (story M4): each agent you can talk to is its Logos mark, in its colour (ST2),
// drifting in the chat's empty space. A mark shows the agent's state: asleep, thinking (typing, or your
// message waiting for it), waiting on you (a Noticeboard request from it), or off (the AI computer is off,
// or the agent is not running). Click a mark to talk to that agent. In a conversation the marks sit in the
// header: the current one with its name, the others small, one click to switch. Names show when you point
// at a mark, or always (Settings; always on a phone). Motion follows Settings: Full, Calm or Still.
import { h, getJSON, icon, ICONS } from './util.js';
import * as mx from './mx.js';

// Moods (Greg, 21 Nov): unplugged when the AI computer (LM Studio) is off; asleep when it is on but the
// agent's model is not loaded; waking while it loads (words that suit the Motion setting, rotating);
// awake when loaded; thinking while it replies; waiting when it needs you; off when the agent is stopped.
export const MOODS = { unplugged: 'Unplugged', asleep: 'Asleep', waking: 'Waking up', awake: 'Awake',
  thinking: 'Thinking…', waiting: 'Waiting on you', off: 'Not running' };
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
  // The name rides inside the drifting part, so it moves and tilts with its mark (Greg, 21 Nov).
  h('span', { class: 'mark-fx', 'aria-hidden': 'true' }, h('span', { class: 'mark-fy' },
    h('span', { class: 'mark-glow' }, h('span', { class: 'mark-glyph' })),
    h('span', { class: 'mark-label' }, h('strong', {}, agent.display_name), h('span', {}, moodWords(mood))))));
}

// Each agent's state, from its Matrix room (this browser), the Noticeboard and the agent list.
export async function moods(agents) {
  // Fresh AI-computer state each time (the server checks LM Studio at most every 20 s).
  try {
    const live = Object.fromEntries(((await getJSON('/api/v2/agents')).agents || []).map((a) => [a.name, a]));
    agents = agents.map((a) => (live[a.name] ? { ...a, running: live[a.name].running, model_up: live[a.name].model_up, model_loaded: live[a.name].model_loaded } : a));
  } catch (e) { /* keep what we had */ }
  const out = Object.fromEntries(agents.map((a) => [a.name,
    a.running === false ? 'off' : a.model_up === false ? 'unplugged' : a.model_loaded === true ? 'awake' : 'asleep']));
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

// A voice note recorded on the void, handed to the chat it opens (taken once, like Home's Ask).
let handed = null;
export function takeVoice(agent) {
  if (!handed || handed.agent !== agent) return null;
  const blob = handed.blob; handed = null; return blob;
}

// Where the person put each agent (this device), by dragging it (Greg, 21 Nov): { phone|wide: { name: [x%, y%] } }.
// A phone and a wide screen are different shapes, so each keeps its own places.
const SPOTS_KEY = 'logos.void.spots';
const savedSpots = () => { try { const v = JSON.parse(localStorage.getItem(SPOTS_KEY) || '{}'); return v && typeof v === 'object' ? v : {}; } catch (e) { return {}; } };
const saveSpots = (v) => { try { localStorage.setItem(SPOTS_KEY, JSON.stringify(v)); } catch (e) { /* private window */ } };

// The void: every agent floating, nothing else, until you pick one.
// Press and hold a mark (half a second) to lift it, then put it anywhere (onto another: they swap). Keep holding
// still for a second and it records: let go and the voice note goes to that agent, in its chat (Greg, 21 Nov).
export async function voidView(root, agents, others = []) {
  let alive = true;
  const field = h('div', { class: 'void-field' });
  const ask = h('p', { class: 'void-ask' }, 'Who do you want to talk to?');
  // How it works, said once under the question (Greg, 21 Nov).
  const how = h('p', { class: 'void-how' }, 'Tap to chat · hold to move', h('br'), 'Hold longer for a voice note: swipe up to send');
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
  const draw = (m) => {
    lastMoods = m;
    if (busy) { redrawWanted = true; return; }               // never redraw under a finger
    field.replaceChildren(...all.map(({ a, mine }, i) => {
      // Not a real link (Greg, 21 Nov): a long press on one made Android offer to copy or save it. A tap
      // still opens the chat (the click handler below), and Enter does too.
      const el = mark(a, { size: 'big', mood: mine ? (m[a.name] || 'asleep') : (a.running === false ? 'off' : 'asleep') });
      if (mine) { el.dataset.href = `#/chat/${a.name}`; el.setAttribute('role', 'link'); el.tabIndex = 0; }
      if (!mine) {
        el.classList.add('private');
        el.title = `${a.display_name} · private to its owner`;
        el.setAttribute('aria-label', `${a.display_name}: private to its owner`);
        el.querySelector('.mark-label span')?.replaceChildren(icon(ICONS.lock), ' Private');
      } else {
        el.setAttribute('aria-label', `${el.getAttribute('aria-label')}. Talk to it. Hold to move it, or keep holding to send a voice note`);
      }
      el.classList.add(`drift-${i % 3}`);
      const [x, y] = placeOf(a.name, i);
      el.style.setProperty('--x', `${x}%`);
      el.style.setProperty('--y', `${y}%`);
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
  const LIFT_MS = 500, RECORD_MS = 1000, SLOP = 10;
  const label = (el) => el.querySelector('.mark-label > span');
  const tell = (t) => { ask.textContent = t; };
  const resetAsk = () => { ask.textContent = 'Who do you want to talk to?'; };
  function finish() {
    if (!g) return;
    clearTimeout(g.t1); clearTimeout(g.t2);
    g.el.classList.remove('lifted', 'dragging', 'recording', 'will-send', 'will-cancel');
    g.el.querySelector('.rec-guide')?.remove();
    if (!g.drag) g.el.style.translate = '';
    for (const el of field.querySelectorAll('.drop-target')) el.classList.remove('drop-target');
    window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); window.removeEventListener('pointercancel', onUp);
    document.documentElement.classList.remove('holding-mark');
    g = null; busy = false;
    if (redrawWanted) { redrawWanted = false; const before = rects(); draw(lastMoods); glide(before); }
  }
  async function record() {
    if (!g || g.drag || !g.mine) return;
    const mine = g;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      tell(window.isSecureContext ? 'This browser cannot record audio.' : 'The microphone only works on the https address.'); return;
    }
    mine.el.classList.add('recording'); navigator.vibrate?.(20);
    label(mine.el)?.replaceChildren('Listening…');
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) {
      tell('The microphone is blocked. Allow it for this site, then hold again.'); if (g === mine) finish(); return;
    }
    if (g !== mine) { stream.getTracks().forEach((t) => t.stop()); tell('Microphone ready: hold an agent again to record.'); return; }
    const recorder = new MediaRecorder(stream); const chunks = []; const started = Date.now();
    recorder.addEventListener('dataavailable', (e) => { if (e.data.size) chunks.push(e.data); });
    recorder.addEventListener('stop', () => {
      stream.getTracks().forEach((t) => t.stop()); clearInterval(mine.tick);
      const still = document.documentElement.dataset.motion === 'still' || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const play = (cls, ms) => { if (still) return; mine.el.classList.add(cls); setTimeout(() => mine.el.classList.remove(cls), ms); };
      if (!mine.send) {
        play('cancelling', 450);                                 // a small shake: nothing went
        tell(mine.choice === 'cancel' ? 'Cancelled: nothing was sent.' : 'Not sent: swipe up to send it.'); return;
      }
      if (!chunks.length || Date.now() - started < 600) { tell('Hold a little longer to record.'); return; }
      handed = { agent: mine.name, blob: new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }) };
      tell('Sending…');
      play('sending', 600);                                      // it flies up and away, then its chat opens
      setTimeout(() => { location.hash = `#/chat/${mine.name}`; }, still ? 0 : 320);
    });
    recorder.start();
    mine.rec = recorder;
    // Swipe up to send, down to cancel (Greg, 21 Nov); the label says what letting go will do.
    mine.el.append(h('span', { class: 'rec-guide', 'aria-hidden': 'true' },
      h('span', { class: 'rec-up' }, '↑ Send'), h('span', { class: 'rec-down' }, '↓ Cancel')));
    const tick = () => { const s = Math.floor((Date.now() - started) / 1000);
      const time = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      label(mine.el)?.replaceChildren(mine.choice === 'send' ? `Let go to send · ${time}` : mine.choice === 'cancel' ? 'Let go to cancel' : `Recording ${time}`);
      tell('Swipe up to send · down to cancel');
      if (s >= 300) { mine.choice = 'send'; onUp({ type: 'pointerup' }); } };
    tick(); mine.tick = setInterval(tick, 500);
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
    if (g.rec || g.el.classList.contains('recording')) {
      // Recording: up sends, down cancels; the mark follows the finger up and down.
      const y = Math.max(-140, Math.min(140, dy));
      g.el.style.translate = `0px ${Math.round(y * 0.6)}px`;
      g.choice = dy < -70 ? 'send' : dy > 70 ? 'cancel' : null;
      g.el.classList.toggle('will-send', g.choice === 'send');
      g.el.classList.toggle('will-cancel', g.choice === 'cancel');
      if (g.choice !== g.shown) { g.shown = g.choice; if (g.choice) navigator.vibrate?.(8); }
      return;
    }
    if (!g.drag && Math.hypot(dx, dy) > SLOP) { g.drag = true; clearTimeout(g.t2); g.el.classList.add('dragging'); label(g.el)?.replaceChildren('Move me'); }
    if (!g.drag) return;
    g.el.style.translate = `${dx}px ${dy}px`;
    const t = nearest(e.clientX, e.clientY);
    for (const el of field.querySelectorAll('.drop-target')) if (el !== t) el.classList.remove('drop-target');
    t?.classList.add('drop-target');
  }
  function onUp(e) {
    if (!g || (e.pointerId !== undefined && e.pointerId !== g.id)) return;
    const was = g;
    if (was.lifted || was.drag || was.el.classList.contains('recording')) swallowClick = true;
    if (was.rec) { was.send = e.type === 'pointerup' && was.choice === 'send'; was.rec.stop(); finish(); return; }
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
    const el = e.target.closest('.agent-mark');
    if (!el || g || (e.pointerType === 'mouse' && e.button !== 0)) return;
    swallowClick = false;
    g = { el, name: el.dataset.agent, mine: !!el.dataset.href, id: e.pointerId, x0: e.clientX, y0: e.clientY, lifted: false, drag: false, rec: null };
    busy = true;
    g.t1 = setTimeout(() => {
      if (!g) return;
      g.lifted = true; g.el.classList.add('lifted'); navigator.vibrate?.(10);
      document.documentElement.classList.add('holding-mark');
      tell(g.mine ? 'Move it, or keep holding to record a voice note' : 'Move it');
    }, LIFT_MS);
    g.t2 = setTimeout(record, RECORD_MS);
    window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', onUp); window.addEventListener('pointercancel', onUp);
  });
  // A hold is not a tap: no link opened, no long-press menu.
  field.addEventListener('click', (e) => { if (swallowClick) { e.preventDefault(); e.stopPropagation(); swallowClick = false; } }, true);
  field.addEventListener('click', (e) => { const el = e.target.closest('.agent-mark[data-href]'); if (el) location.hash = el.dataset.href; });
  field.addEventListener('keydown', (e) => { const el = e.target.closest('.agent-mark[data-href]'); if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); location.hash = el.dataset.href; } });
  field.addEventListener('contextmenu', (e) => { if (e.target.closest('.agent-mark')) e.preventDefault(); });
  field.addEventListener('dragstart', (e) => e.preventDefault());

  root.replaceChildren(h('section', { class: 'chat void', 'aria-label': 'Your agents' },
    h('h1', { class: 'visually-hidden' }, 'Chat'), ask, how, field));
  draw(lastMoods);
  const tick = async () => { const m = await moods(agents); if (alive) draw(m); };
  await tick();
  const t = setInterval(tick, 8000);
  return () => { alive = false; clearInterval(t); if (g) { if (g.rec) { g.send = false; g.rec.stop(); } finish(); } };
}

// In a conversation: the current agent's mark and name, then the others, small, one click to switch.
export function markBar(agents, current) {
  const bar = h('div', { class: 'mark-bar', role: 'navigation', 'aria-label': 'Agents' },
    h('a', { href: '#/chat', class: 'mark-back', 'aria-label': 'All agents', title: 'All agents' }, '‹'),
    mark(current, { size: 'small', current: true, href: '#/chat' }),
    h('a', { href: '#/chat', class: 'mark-name', title: 'All agents' }, current.display_name),
    h('span', { class: 'mark-others' }, agents.filter((a) => a.name !== current.name)
      .map((a) => mark(a, { size: 'tiny', href: `#/chat/${a.name}` }))));
  let alive = true;
  const tick = async () => {
    if (!alive || !bar.isConnected) return;
    const m = await moods(agents);
    for (const el of bar.querySelectorAll('.agent-mark')) {
      const mood = m[el.dataset.agent] || 'asleep';
      el.className = el.className.replace(/mood-\w+/, `mood-${mood}`);
      const a = agents.find((x) => x.name === el.dataset.agent);
      el.setAttribute('aria-label', `${a.display_name}: ${MOODS[mood]}`); el.title = `${a.display_name} · ${MOODS[mood]}`;
    }
  };
  tick();
  const t = setInterval(() => { if (!bar.isConnected) { alive = false; clearInterval(t); } else tick(); }, 8000);
  return bar;
}

// Phone (Greg, 21 Nov): inside an agent, one bar: ‹ back to all agents, its name, and one pill naming
// where you are (Chat, About) that opens to Chat | Brain | About, so the name has the room (Greg, 21 Nov).
const LABELS = { chat: 'Chat', brain: 'Brain', about: 'About' };
export function agentBar(agent, active) {
  const item = (key, href, extra = {}) => h('a', { href, class: `agent-tab${key === active ? ' on' : ''}`, role: 'menuitem',
    'aria-current': key === active ? 'page' : null, ...extra }, LABELS[key],
    key === 'brain' ? h('span', { class: 'agent-tab-out', 'aria-hidden': 'true' }, ' ↗') : null);
  const menu = h('details', { class: 'agent-menu' },
    h('summary', { class: 'agent-tab on', 'aria-label': `${LABELS[active] || 'Chat'}: switch to Chat, Brain or About` },
      LABELS[active] || 'Chat', h('span', { class: 'agent-menu-caret', 'aria-hidden': 'true' }, '▾')),
    h('div', { class: 'agent-menu-list', role: 'menu' },
      item('chat', `#/chat/${agent.name}`),
      // On a phone Brain opens the agent's vault straight in Obsidian (Greg, 21 Nov); a desktop has no
      // Obsidian, so there it is the reader in Logos (the desktop never shows this bar).
      item('brain', `obsidian://open?vault=${encodeURIComponent(agent.obsidian_vault || `brain-${agent.name}`)}&file=README.md`,
        { title: 'Open this brain in Obsidian' }),
      item('about', `#/agents/${agent.name}`)));
  menu.addEventListener('click', (e) => { if (e.target.closest('a')) menu.open = false; });
  const shut = (e) => {
    if (!menu.isConnected) { document.removeEventListener('pointerdown', shut, true); return; }
    if (menu.open && !menu.contains(e.target)) menu.open = false;
  };
  document.addEventListener('pointerdown', shut, true);
  return h('nav', { class: 'agent-bar', 'aria-label': agent.display_name },
    h('a', { href: '#/chat', class: 'agent-back', 'aria-label': 'All agents', title: 'All agents' }, '‹'),
    h('strong', { class: 'agent-bar-name' }, agent.display_name), menu);
}
