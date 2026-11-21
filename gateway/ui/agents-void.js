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

// The void: every agent floating, nothing else, until you pick one.
export async function voidView(root, agents, others = []) {
  let alive = true;
  const field = h('div', { class: 'void-field' });
  // Spots spread evenly for however many agents there are, centred (on a wide screen two agents sat
  // off to the left). Yours first, then the private ones.
  const all = [...agents.map((a) => ({ a, mine: true })), ...others.map((a) => ({ a, mine: false }))];
  const spot = (i, n) => {
    const x = n === 1 ? 50 : 18 + (64 * i) / (n - 1);
    const y = [44, 34, 54, 40, 50, 36][i % 6];
    return [x, y];
  };
  const draw = (m) => field.replaceChildren(...all.map(({ a, mine }, i) => {
    const el = mine ? mark(a, { size: 'big', mood: m[a.name] || 'asleep', href: `#/chat/${a.name}` })
      : mark(a, { size: 'big', mood: a.running === false ? 'off' : 'asleep' });
    if (!mine) {
      el.classList.add('private');
      el.title = `${a.display_name} · private to its owner`;
      el.setAttribute('aria-label', `${a.display_name}: private to its owner`);
      el.querySelector('.mark-label span')?.replaceChildren(icon(ICONS.lock), ' Private');
    }
    el.classList.add(`drift-${i % 3}`);
    const [x, y] = spot(i, all.length);
    el.style.setProperty('--x', `${x}%`);
    el.style.setProperty('--y', `${y}%`);
    return el;
  }));
  root.replaceChildren(h('section', { class: 'chat void', 'aria-label': 'Your agents' },
    h('h1', { class: 'visually-hidden' }, 'Chat'),
    h('p', { class: 'void-ask' }, 'Who do you want to talk to?'), field));
  draw(Object.fromEntries(agents.map((a) => [a.name, 'asleep'])));
  const tick = async () => { const m = await moods(agents); if (alive) draw(m); };
  await tick();
  const t = setInterval(tick, 8000);
  return () => { alive = false; clearInterval(t); };
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

// Phone (Greg, 21 Nov): inside an agent, one bar: ‹ back to all agents, its name, and Chat | Brain | About.
export function agentBar(agent, active) {
  const tab = (key, label, href) => h('a', { href, class: `agent-tab${key === active ? ' on' : ''}`, 'aria-current': key === active ? 'page' : null }, label);
  return h('nav', { class: 'agent-bar', 'aria-label': agent.display_name },
    h('a', { href: '#/chat', class: 'agent-back', 'aria-label': 'All agents', title: 'All agents' }, '‹'),
    h('strong', { class: 'agent-bar-name' }, agent.display_name),
    h('span', { class: 'agent-tabs' },
      tab('chat', 'Chat', `#/chat/${agent.name}`),
      // On a phone Brain opens the agent's vault straight in Obsidian (Greg, 21 Nov); a desktop has no
      // Obsidian, so there it is the reader in Logos (the desktop never shows this bar).
      h('a', { href: `obsidian://open?vault=${encodeURIComponent(agent.obsidian_vault || `brain-${agent.name}`)}&file=README.md`, class: 'agent-tab',
        title: 'Open this brain in Obsidian' }, 'Brain', h('span', { class: 'agent-tab-out', 'aria-hidden': 'true' }, ' ↗')),
      tab('about', 'About', `#/agents/${agent.name}`)));
}
