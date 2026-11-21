// The floating agents (story M4): each agent you can talk to is its Logos mark, in its colour (ST2),
// drifting in the chat's empty space. A mark shows the agent's state: asleep, thinking (typing, or your
// message waiting for it), waiting on you (a Noticeboard request from it), or off (the AI computer is off,
// or the agent is not running). Click a mark to talk to that agent. In a conversation the marks sit in the
// header: the current one with its name, the others small, one click to switch. Names show when you point
// at a mark, or always (Settings; always on a phone). Motion follows Settings: Full, Calm or Still.
import { h, getJSON } from './util.js';
import * as mx from './mx.js';

export const MOODS = { asleep: 'Asleep', thinking: 'Thinking…', waiting: 'Waiting on you', off: 'Off' };

// One agent's mark: the Logos glyph in the agent's colour, with its name and mood under it.
export function mark(agent, { size = 'big', mood = 'asleep', href = null, current = false } = {}) {
  const tag = href ? 'a' : 'span';
  return h(tag, { class: `agent-mark ${size} mood-${mood}${current ? ' current' : ''}`, href: href || null,
    style: `--mark: var(--agent-${agent.name}, var(--${agent.name}, var(--accent)))`, 'data-agent': agent.name,
    'aria-label': `${agent.display_name}: ${MOODS[mood]}${href ? '. Talk to it' : ''}`, title: `${agent.display_name} · ${MOODS[mood]}` },
  h('span', { class: 'mark-fx', 'aria-hidden': 'true' }, h('span', { class: 'mark-fy' },
    h('span', { class: 'mark-glow' }, h('span', { class: 'mark-glyph' })))),
  h('span', { class: 'mark-label', 'aria-hidden': 'true' }, h('strong', {}, agent.display_name), h('span', {}, MOODS[mood])));
}

// Each agent's state, from its Matrix room (this browser), the Noticeboard and the agent list.
export async function moods(agents) {
  const out = Object.fromEntries(agents.map((a) => [a.name, a.running === false || a.model_up === false ? 'off' : 'asleep']));
  try {
    const nb = await getJSON('/api/v2/noticeboard');
    for (const m of nb.waiting || []) if (out[m.from] && out[m.from] !== 'off') out[m.from] = 'waiting';
  } catch (e) { /* the Noticeboard is a nicety here */ }
  try {
    const c = mx.saved() ? await mx.get() : null;
    if (c) {
      const me = c.getUserId();
      for (const a of agents) {
        if (!a.matrix_id || out[a.name] === 'off') continue;
        const rooms = c.getRooms().filter((r) => r.getMyMembership() === 'join' && r.getMember(a.matrix_id)?.membership === 'join');
        for (const r of rooms) {
          const typing = r.getMember(a.matrix_id)?.typing;
          const evs = r.getLiveTimeline().getEvents().filter((e) => e.getType() === 'm.room.message' && e.getContent().msgtype !== 'm.notice');
          const last = evs[evs.length - 1];
          const unanswered = last && last.getSender() === me && Date.now() - last.getTs() < 15 * 60000;
          if (typing || unanswered) out[a.name] = 'thinking';
        }
      }
    }
  } catch (e) { /* Matrix not ready: the marks stay as they are */ }
  return out;
}

// The void: every agent floating, nothing else, until you pick one.
export async function voidView(root, agents) {
  let alive = true;
  const field = h('div', { class: 'void-field' });
  const draw = (m) => field.replaceChildren(...agents.map((a, i) => {
    const el = mark(a, { size: 'big', mood: m[a.name] || 'asleep', href: `#/chat/${a.name}` });
    el.classList.add(`drift-${i % 3}`);
    el.style.setProperty('--x', `${[22, 52, 76, 36, 64][i % 5]}%`);
    el.style.setProperty('--y', `${[46, 28, 52, 70, 74][i % 5]}%`);
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
