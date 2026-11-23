// F41 First-visit tips (Greg, 23 Nov): the first time someone opens a screen, a small card or two on
// what can be done there. "Got it" (or Skip) remembers it with their login, so it never shows again on
// any device; Settings › Look › Tips shows them all again.
import { h } from './util.js';
import { getPref, setPref } from './prefs.js';

// Each step: a title, a line, and optionally what it is about on the page (outlined while it shows).
export const TIPS = {
  home: [
    { title: 'Your day at a glance', text: 'Weather, your calendar, messages and lists. Tap anything to open it. Swipe left or right to move between Home, Agents and Notifications.' },
    { title: 'Make it yours', text: 'Edit Home lets you drag the widgets into your order, remove them, or add more, like Lights.', at: '.home-edit-link' },
    { title: 'Ask your agent', text: 'Type a question here and it opens in a new chat with your agent.', at: '.home-ask' },
  ],
  agents: [
    { title: 'Your agents', text: 'Each mark is an agent. Tap one to open its conversations.', at: '.agent-mark' },
    { title: 'Hold to talk', text: 'Press and hold an agent: it lifts, and you can move it. Keep holding still and it records. Let go and your voice note goes to that agent.' },
    { title: 'Groups', text: '+ New group starts a chat with other people and the household agent.', at: '.void-new' },
  ],
  chat: [
    { title: 'Type or speak', text: 'Write a message, or hold the send button to record a voice message.', at: '.send-btn' },
    { title: 'React, copy, attach', text: 'Press and hold a message to react with an emoji or copy it. The 📎 sends a photo or a file.' },
  ],
  notifications: [
    { title: 'What happened', text: 'Messages, agent updates and changes, grouped by day. New ones are marked until you tap them: a tap opens it in full and marks it read.' },
    { title: 'All at once', text: 'Mark all read clears everything new in one go.', at: '.feed-allread' },
  ],
  routines: [
    { title: 'Routines', text: 'Things your agent does on its own, on a schedule: briefings, digests, reminders. Switch any of them off here.' },
    { title: 'Make your own', text: 'Tap + New routine and say or type what and when, like "every Friday at 5, remind me to put the bins out".' },
  ],
};

// Which tips belong to a screen (the router's name and the rest of the address).
export function keyFor(name, rest = []) {
  if (name === 'home') return 'home';
  if (name === 'chat') return rest[0] ? 'chat' : 'agents';
  if (name === 'noticeboard') return 'notifications';
  if (name === 'agents' && rest[0]) return 'routines';
  return null;
}

const seen = () => { const v = getPref('tips_seen', []); return Array.isArray(v) ? v : []; };
let card = null;

export function hide() {
  if (!card) return;
  card.remove(); card = null;
  for (const el of document.querySelectorAll('.tip-target')) el.classList.remove('tip-target');
}

// Shown once the screen has drawn; a screen left before then shows nothing.
export function show(key, stillHere = () => true) {
  hide();
  if (!key || !TIPS[key] || seen().includes(key)) return;
  const steps = TIPS[key];
  let i = 0;
  const done = (all) => { hide(); setPref('tips_seen', all ? Object.keys(TIPS) : [...new Set([...seen(), key])]); };
  const title = h('strong', { class: 'tip-title' });
  const text = h('p', { class: 'tip-text' });
  const dots = h('span', { class: 'tip-dots', 'aria-hidden': 'true' });
  const next = h('button', { type: 'button', class: 'btn small' });
  const skip = h('button', { type: 'button', class: 'linkish tip-skip' }, 'Skip tips');
  card = h('aside', { class: 'tip-card', role: 'dialog', 'aria-label': 'Tip', 'aria-live': 'polite' },
    title, text, h('div', { class: 'tip-row' }, skip, dots, next));
  const draw = () => {
    const s = steps[i];
    title.textContent = s.title; text.textContent = s.text;
    next.textContent = i < steps.length - 1 ? 'Next' : 'Got it';
    dots.replaceChildren(...steps.map((_, j) => h('span', { class: j === i ? 'on' : '' })));
    skip.style.visibility = i > 0 ? 'hidden' : '';
    for (const el of document.querySelectorAll('.tip-target')) el.classList.remove('tip-target');
    card.classList.remove('tip-top');
    const at = s.at && document.querySelector(s.at);
    if (!at) return;
    at.classList.add('tip-target');
    at.scrollIntoView({ block: 'center' });
    // What the tip is about must not be under the card: then the card goes to the top instead.
    const r = at.getBoundingClientRect(), c = card.getBoundingClientRect();
    if (r.bottom > c.top - 8 && r.top < c.bottom) card.classList.add('tip-top');
  };
  next.addEventListener('click', () => { if (i < steps.length - 1) { i += 1; draw(); } else done(false); });
  skip.addEventListener('click', () => done(true));
  setTimeout(() => {                       // after the screen has drawn and settled
    if (!card || !stillHere()) { hide(); return; }
    document.body.append(card); draw();
  }, 700);
}

export function reset() { return setPref('tips_seen', []); }
