// Chat as your Matrix room (story M2): the conversation is your encrypted Element room with the agent,
// read and written by this browser as one of your Matrix devices (mx.js, story M1). The same messages
// appear here and in Element X, live. Under each of the agent's replies, "Thinking" folds out its
// reasoning and tool calls (from the agent's own record, /api/v2/agents/<name>/steps).
// Old web conversations stay readable for 30 days under Archive (Greg, 21 Nov).
// M3: each further conversation is a thread in the room. "New conversation" posts a notice as its title
// (agents ignore notices) and the first message inside the thread, so the agent answers there, as its own
// conversation. Its title is that first message; threads quiet for 30 days leave the list (Greg, 21 Nov).
// On a phone the conversations are chips above the chat (Greg chose K2).
import { h, api, getJSON, markdown, when, none, loading, focusIfKeyboard, icon, ICONS } from './util.js';
import * as mx from './mx.js';
import { stepsView } from './chat.js';
import { takeAsk } from './home.js';
import { markBar, agentBar, takeVoice } from './agents-void.js';
import { getPref } from './prefs.js';

const ARCHIVE_DAYS = 30;
const THREAD_DAYS = 30;
const TITLE_MARK = '💬 ';
// hermes opens a "Hermes — <job>" thread for a scheduled message (a briefing, the daily question).
// Those show by the job's plain name, and in the chat as the message itself (Greg, 21 Nov).
const CRON_ROOT = /^Hermes\s+[—–-]\s+(\S+)\s*$/;
const JOB_TITLES = { 'morning-briefing': 'Morning briefing', 'morning-briefing-weekend': 'Morning briefing',
  'daily-question': 'Daily question', 'weekly-digest': 'Weekly digest', 'changes-daily': 'What changed',
  'changes-week': 'What changed this week', 'noticeboard-check': 'Waiting on you', 'noticeboard-urgent': 'Urgent' };
export const jobTitle = (job) => JOB_TITLES[job] || (job.charAt(0).toUpperCase() + job.slice(1)).replace(/[-_]+/g, ' ');
const cronJob = (ev) => (ev && ev.getType && ev.getType() === 'm.room.message' && CRON_ROOT.exec(String(ev.getContent()?.body || ''))?.[1]) || null;
const titleOf = (ev) => (ev && ev.isDecryptionFailure && ev.isDecryptionFailure()
  ? 'Locked conversation'
  : cronJob(ev) ? jobTitle(cronJob(ev))
    : String(ev?.getContent()?.body || 'Conversation').replace(/^💬\s*/, '').slice(0, 80));

function pickRoom(c, agentId) {
  const rooms = c.getRooms().filter((r) => r.getMyMembership() === 'join'
    && ['join', 'invite'].includes(r.getMember(agentId)?.membership));
  // The direct room with the agent first (two members), else the busiest room it shares with you.
  rooms.sort((a, b) => (a.getJoinedMemberCount() === 2 ? 0 : 1) - (b.getJoinedMemberCount() === 2 ? 0 : 1)
    || b.getLastActiveTimestamp() - a.getLastActiveTimestamp());
  return rooms[0] || null;
}

// Swipe right (starting in the left part of the screen) or pinch two fingers together: back.
// Touch only; a mouse never triggers it. Returns the function that removes the listeners.
export function backGestures(el, back) {
  if (!el) return () => {};
  let start = null, pinch = null;
  const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  const onStart = (e) => {
    if (e.touches.length === 2) { pinch = dist(e.touches); start = null; return; }
    const t = e.touches[0];
    start = e.touches.length === 1 && t.clientX < window.innerWidth * 0.4 && !e.target.closest('textarea, input, .mx-chips')
      ? { x: t.clientX, y: t.clientY } : null;
  };
  const onMove = (e) => {
    if (pinch && e.touches.length === 2 && dist(e.touches) < pinch * 0.6) { pinch = null; back(); return; }
    if (!start || e.touches.length !== 1) return;
    const dx = e.touches[0].clientX - start.x, dy = Math.abs(e.touches[0].clientY - start.y);
    if (dy > 40) { start = null; return; }
    if (dx > 0 && dy < dx / 2) { el.style.transition = 'none'; el.style.transform = `translateX(${dx * 0.8}px)`; el.style.opacity = String(1 - Math.min(dx / 400, 0.5)); }
    if (dx > 120 && dy < dx / 2) { start = null; back(); }
  };
  const settle = () => { el.style.transition = 'transform .2s ease, opacity .2s ease'; el.style.transform = ''; el.style.opacity = ''; };
  const onEnd = (e) => { if (e.touches.length < 2) pinch = null; if (!e.touches.length) { start = null; settle(); } };
  el.addEventListener('touchstart', onStart, { passive: true });
  el.addEventListener('touchmove', onMove, { passive: true });
  el.addEventListener('touchend', onEnd, { passive: true });
  return () => { el.removeEventListener('touchstart', onStart); el.removeEventListener('touchmove', onMove); el.removeEventListener('touchend', onEnd); };
}

export async function matrixChat(root, agent, agents, state, sideBits, threadId = null) {
  const isNew = threadId === 'new';
  let alive = true;
  const off = [];
  const log = h('div', { class: 'log', role: 'log', 'aria-live': 'polite' }, loading());
  const input = h('textarea', { id: 'chat-input', rows: '1', placeholder: isNew ? 'Start a new conversation: its first message is its title'
    : threadId ? 'Reply' : `Message ${agent.display_name}` });
  // The box grows with what is in it, up to about half the screen, so a long message or a voice
  // transcript can be read and scrolled before it is sent (Greg, 21 Nov).
  const grow = () => {
    input.style.height = 'auto';
    const max = Math.round(window.innerHeight * (window.innerWidth < 600 ? 0.45 : 0.4));
    const hgt = Math.min(input.scrollHeight + 2, max);
    input.style.height = `${hgt}px`;
    input.style.overflowY = input.scrollHeight + 2 > max ? 'auto' : 'hidden';
    input.classList.toggle('tall', hgt > 60);
  };
  input.addEventListener('input', grow);
  const main = `#/chat/${agent.name}`;
  const crumb = threadId ? h('a', { href: main, class: 'mx-crumb' }, 'Main conversation ›') : null;
  const convTitle = h('span', { class: 'mx-conv-title' }, isNew ? 'New conversation' : '');
  const chipsRow = h('div', { class: 'mx-chips phone-only', role: 'tablist', 'aria-label': 'Conversations' });
  // Send, and hold to record (Greg, 21 Nov): with the box empty the button slowly turns between the arrow
  // and a microphone, so people find out they can hold it down to speak a message.
  const send = h('button', { type: 'submit', class: 'btn send-btn idle', 'aria-label': 'Send, or hold to record a voice message' },
    h('span', { class: 'btn-words' }, 'Send'), h('span', { class: 'send-ico' }, icon(ICONS.send)), h('span', { class: 'mic-ico' }, icon(ICONS.mic)));
  const note = h('p', { class: 'when mx-note', role: 'status' });
  const thinkingMore = h('span', { class: 'mx-thinking-more' });
  const thinkingRow = h('div', { class: 'mx-thinking', hidden: true },
    h('span', { class: 'mx-thinking-mark', 'aria-hidden': 'true' }), `${agent.display_name} is thinking`, h('span', { class: 'dots', 'aria-hidden': 'true' }, '…'),
    thinkingMore);
  const modelLine = h('div', { class: 'model-line down', role: 'status', hidden: agent.model_up !== false },
    'AI computer off · your message will queue');
  const form = h('form', { class: 'composer', onsubmit: (e) => { e.preventDefault(); sendText(input.value); } },
    h('label', { for: 'chat-input', class: 'sr' }, 'Message'), input, send);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(input.value); } });
  const idle = () => send.classList.toggle('idle', !input.value.trim());
  input.addEventListener('input', idle);

  // Hold the button to record; let go to stop. The words land in the box to check, then Send.
  let rec = null, holdTimer = null, held = false;
  const say = (t) => { note.textContent = t; };
  async function startRecording() {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      say(window.isSecureContext ? 'This browser cannot record audio.' : 'The microphone only works on the https address.'); return;
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) {
      say('The microphone is blocked. Allow it for this site in the browser, then try again.'); return;
    }
    if (!held) { stream.getTracks().forEach((t) => t.stop()); return; }      // let go before it started
    const recorder = new MediaRecorder(stream); const chunks = []; const started = Date.now();
    recorder.addEventListener('dataavailable', (e) => { if (e.data.size) chunks.push(e.data); });
    recorder.addEventListener('stop', () => {
      stream.getTracks().forEach((t) => t.stop()); clearInterval(rec?.timer); rec = null; send.classList.remove('recording');
      if (alive && chunks.length && Date.now() - started > 600) toWords(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }));
      else say('Hold a little longer to record.');
    });
    recorder.start(); send.classList.add('recording');
    const tick = () => { const s = Math.floor((Date.now() - started) / 1000);
      say(`Recording ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} · let go to stop`); if (s >= 300) stopRecording(); };
    tick(); rec = { recorder, timer: setInterval(tick, 500) };
  }
  function stopRecording() { if (rec && rec.recorder.state !== 'inactive') rec.recorder.stop(); }
  // autoSend: a voice note recorded by holding the agent's mark (agents-void) goes straight out.
  async function toWords(blob, autoSend = false) {
    say('Turning it into words…'); send.disabled = true;
    try {
      const r = await api(`/api/v2/agents/${agent.name}/voice`, { method: 'POST', raw: blob, headers: { 'Content-Type': blob.type || 'audio/webm' } });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.message || 'That did not work. Try again.');
      if (!d.text) say('No words were heard in that recording.');
      else if (autoSend) { say(''); await sendText(d.text); }
      else { input.value = (input.value.trim() ? input.value.trimEnd() + ' ' : '') + d.text; grow(); idle(); say('Check it, then send.'); }
    } catch (e) { say(e.message); }
    send.disabled = false;
  }
  send.addEventListener('pointerdown', (e) => {
    if (input.value.trim() || send.disabled) return;           // with words in the box it is just Send
    e.preventDefault(); held = true;
    holdTimer = setTimeout(startRecording, 200);
  });
  const letGo = () => {
    if (!held) return; held = false; clearTimeout(holdTimer);
    if (rec) stopRecording(); else if (!input.value.trim()) say('Hold to record a voice message.');
  };
  for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) send.addEventListener(ev, letGo);
  send.addEventListener('contextmenu', (e) => e.preventDefault());
  off.push(() => { held = false; clearTimeout(holdTimer); stopRecording(); });

  root.replaceChildren(
    h('section', { class: 'chat', 'aria-label': 'Conversation' },
      h('header', { class: 'chat-head' },
        // M4: the agents' marks replace the agent tabs; inside a thread, its title.
        threadId ? null : markBar(agents.filter((a) => a.matrix_id), agent),
        threadId ? null : agentBar(agent, 'chat'),
        crumb, threadId ? h('h1', { class: 'mx-thread-title' }, convTitle) : h('h1', { class: 'visually-hidden' }, agent.display_name),
        sideBits.viewMenu || null, sideBits.convBtn || null),
      chipsRow, log, thinkingRow, note, modelLine, form),
    sideBits.side, sideBits.sideScrim || null, sideBits.reopen || null);

  // Phone (Greg, 21 Nov): in a conversation the chat's own header replaces the Logos bar, and you go
  // back to all your agents by swiping right, by pinching (zooming out to the floating agents), or by
  // tapping the agent's mark.
  document.documentElement.classList.add('in-conv');
  off.push(() => document.documentElement.classList.remove('in-conv'));
  off.push(backGestures(root.querySelector('section.chat'), () => { location.hash = '#/chat'; }));
  // Once per browser, on a touch screen: say the gestures exist.
  try {
    if (matchMedia('(pointer: coarse)').matches && !localStorage.getItem('logos.tip.back')) {
      localStorage.setItem('logos.tip.back', '1');
      const tip = h('div', { class: 'gesture-tip', role: 'status' }, 'Tip: swipe right or pinch to see all your agents');
      root.querySelector('section.chat')?.append(tip);
      setTimeout(() => tip.classList.add('gone'), 5000); setTimeout(() => tip.remove(), 5600);
    }
  } catch (e) { /* no storage: no tip */ }

  let c, room;
  try {
    c = await mx.get();
  } catch (e) {
    log.replaceChildren(h('div', { class: 'notice' }, `This browser's Matrix device could not start (${e.message}). `,
      h('a', { href: '#/settings/matrix' }, 'Check Settings › Matrix device'), '.'));
    return () => {};
  }
  if (!alive) return () => {};
  const me = c.getUserId();
  const m = await import('/ui/vendor/matrix/matrix.js');

  // Where each of your messages came from: this browser, or another of your devices (Element X…),
  // told apart by the sending device's key; names of devices new since the page opened are looked up.
  const deviceNames = new Map();
  let ownKey = '';
  try { ownKey = (await c.getCrypto().getOwnDeviceKeys()).curve25519; } catch (e) { /* older crypto */ }
  async function readNames() {
    try {
      const info = await c.getCrypto().getUserDeviceInfo([me], true);
      for (const d of (info.get(me) || new Map()).values()) deviceNames.set(d.getIdentityKey(), d.displayName || d.deviceId);
    } catch (e) { /* names are a nicety */ }
  }
  // Device names are a nicety: wait at most 0.3 s for them, so opening a chat never hangs on it.
  await Promise.race([readNames(), new Promise((ok) => setTimeout(ok, 300))]);
  let looking = false;
  const fromWhere = (ev) => {
    if (ev.status) return ev.status === 'not_sent' ? 'not sent' : 'sending…';
    const key = ev.getSenderKey && ev.getSenderKey();
    if (!key || key === ownKey) return 'sent · also in Element X';
    const name = deviceNames.get(key);
    if (!name && !looking) { looking = true; readNames().then(() => { looking = false; redraw(); }); }
    return name ? `from ${name}` : 'from another of your devices';
  };

  room = pickRoom(c, agent.matrix_id);
  if (!room) {
    log.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, `No room with ${agent.display_name} yet`),
      h('p', {}, 'Start the conversation here; it appears in Element X too.'),
      h('button', { type: 'button', class: 'btn', onclick: async (e) => {
        e.target.disabled = true;
        try {
          const r = await c.createRoom({ preset: 'trusted_private_chat', is_direct: true, invite: [agent.matrix_id],
            initial_state: [{ type: 'm.room.encryption', state_key: '', content: { algorithm: 'm.megolm.v1.aes-sha2' } }] });
          // Mark it as the direct chat with the agent, as Element does, so both show it the same way.
          const direct = c.getAccountData('m.direct')?.getContent() || {};
          await c.setAccountData('m.direct', { ...direct, [agent.matrix_id]: [...(direct[agent.matrix_id] || []), r.room_id] });
          for (let i = 0; i < 50 && !c.getRoom(r.room_id); i++) await new Promise((ok) => setTimeout(ok, 200));
          room = c.getRoom(r.room_id);
          draw(true);
        } catch (err) {
          note.textContent = `Could not start the conversation: ${err.message}`;
          e.target.disabled = false;
        }
      } }, 'Start the conversation')));
  }

  // ── drawing the room ────────────────────────────────────────────────────────────────────────
  const steps = new Map();                 // reply text -> its thinking, fetched when opened
  let replyMap = new Map();                // thread root id -> { count, last }
  let lastReceipt = null;                  // the event this browser last marked as read
  let briefs = new Map();                  // scheduled-message thread root id -> the message itself
  function thinking(text) {
    const box = h('div', { class: 'mx-steps' }, loading());
    const d = h('details', { class: 'mx-think', ontoggle: async () => {
      if (!d.open || steps.has(text)) return;
      try {
        const r = await getJSON(`/api/v2/agents/${agent.name}/steps?text=${encodeURIComponent(text.slice(0, 200))}`);
        steps.set(text, r);
        const v = stepsView(r.steps || []);
        box.replaceChildren(v ? v : none('No thinking was recorded for this reply.'));
        if (v) v.open = true;
      } catch (e) { box.replaceChildren(none('The thinking could not be read just now.')); }
    } }, h('summary', {}, 'Thinking ›'), box);
    return d;
  }

  // Under a message: its time; for yours, a tick when it is in the room (also in Element X, on hover)
  // or where it came from ("from Element X") when another of your devices sent it.
  // One line under a message: its time (only at the end of a run), where it came from, and a tick that
  // turns green once the agent has acknowledged it (its ✅ reaction, no longer shown as a chip).
  function metaFor(ev, mine, edited, showTime = true, seen = false) {
    const where = mine ? fromWhere(ev) : '';
    const sentHere = where === 'sent · also in Element X';
    const words = [showTime ? when(ev.getTs() / 1000) : '', sentHere ? '' : where, edited ? 'edited' : ''].filter(Boolean).join(' · ');
    if (!words && !sentHere) return null;
    return h('span', { class: 'when mx-meta', title: sentHere ? `Sent · also in Element X${seen ? ` · seen by ${agent.display_name}` : ''}` : null },
      words, sentHere ? h('span', { class: `mx-tick${seen ? ' seen' : ''}`, 'aria-label': seen ? 'seen' : 'sent' }, icon(ICONS.check)) : null);
  }
  const ACK = new Set(['✅', '✔️', '👀']);

  function bubbleFor(ev, edits, reactions, inMain, showTime = true) {
    const mine = ev.getSender() === me;
    const content = edits.get(ev.getId()) || ev.getContent();
    const kind = content.msgtype;
    const thread = inMain ? replyMap.get(ev.getId()) : null;
    const job = cronJob(ev);
    if (job && !inMain) return h('p', { class: 'when mx-thread-start' }, `${jobTitle(job)} · ${when(ev.getTs() / 1000)}`);
    if (job && briefs.has(ev.getId())) {
      // The message itself, under a small label; only a conversation that went on links onwards.
      const turn = bubbleFor(briefs.get(ev.getId()), edits, reactions, false, showTime);
      turn.prepend(h('span', { class: 'mx-job' }, jobTitle(job)));
      const more = (thread?.count || 1) - 1;
      if (more > 0) turn.append(h('a', { class: 'mx-thread-link', href: `${main}/thread/${encodeURIComponent(ev.getId())}` },
        `↳ ${more} repl${more === 1 ? 'y' : 'ies'} · open`));
      return turn;
    }
    if (kind === 'm.notice' && String(content.body || '').startsWith(TITLE_MARK.trim())) {
      // A conversation's title (M3): in the main timeline, a link into it; inside it, its heading.
      const n = thread ? thread.count : 0;
      return inMain
        ? h('a', { class: 'mx-thread-link', href: `${main}/thread/${encodeURIComponent(ev.getId())}` },
          `↳ ${titleOf(ev)}`, h('span', { class: 'when' }, ` · ${n} message${n === 1 ? '' : 's'} · open this conversation`))
        : h('p', { class: 'when mx-thread-start' }, `Started ${when(ev.getTs() / 1000)} · in Element X under “${titleOf(ev)}”`);
    }
    let body;
    if (ev.isDecryptionFailure && ev.isDecryptionFailure()) body = [h('em', { class: 'when' }, 'Could not decrypt this message (sent before this browser was connected, and not in your key backup).')];
    else if (kind === 'm.audio') body = [h('em', {}, 'Voice note: play it in Element X.')];
    else if (kind === 'm.image' || kind === 'm.file' || kind === 'm.video') body = [h('em', {}, `${content.body || 'A file'}: open it in Element X.`)];
    else body = mine ? [String(content.body || '')] : markdown(String(content.body || ''));
    const rx = [...(reactions.get(ev.getId()) || [])];
    const seen = mine && rx.some(([k, r]) => ACK.has(k) && r.n > (r.mine ? 1 : 0));
    const chips = rx.filter(([k, r]) => !(mine && ACK.has(k) && !r.mine));
    const meta = metaFor(ev, mine, edits.has(ev.getId()), showTime, seen);
    // "Thinking ›" under a reply only with Settings > Show thinking on (Greg, 21 Nov: off by default).
    const think = !mine && kind === 'm.text' && content.body && getPref('thinking', false) ? thinking(String(content.body)) : null;
    const canReact = ev.getId() && !ev.getId().startsWith('~') && !(ev.isDecryptionFailure && ev.isDecryptionFailure());
    const bubble = h('div', { class: `msg ${mine ? 'user' : 'assistant'}` }, ...body);
    const turn = h('div', { class: `mx-turn ${mine ? 'me' : 'agent'}${showTime ? '' : ' grouped'}` },
      bubble,
      chips.length ? h('div', { class: 'mx-reactions' }, chips.map(([k, r]) => h('button', {
        type: 'button', class: `chip mx-react${r.mine ? ' mine' : ''}`, 'aria-pressed': String(!!r.mine),
        title: r.mine ? 'Take your reaction back' : 'React with this too', onclick: () => react(ev, k, r.mine),
      }, `${k}${r.n > 1 ? ` ${r.n}` : ''}`))) : null,
      meta || think ? h('div', { class: 'mx-foot' }, meta, think) : null,
      thread ? h('a', { class: 'mx-thread-link', href: `${main}/thread/${encodeURIComponent(ev.getId())}` },
        `↳ ${thread.count} repl${thread.count === 1 ? 'y' : 'ies'} · open this conversation`) : null);
    if (canReact) {
      const open = () => { pickerFor = ev.getId(); draw(false); };
      bubble.append(h('button', { type: 'button', class: 'mx-react-open', 'aria-label': 'React to this message', title: 'React', onclick: (e) => { e.stopPropagation(); open(); } }, '☺'));
      longPress(bubble, open);
      if (pickerFor === ev.getId()) turn.insertBefore(pickerOf(ev, new Map(rx.map(([k, r]) => [k, r.mine])), String(content.body || '')), bubble.nextSibling);
    }
    return turn;
  }

  // ── reactions (F35, Greg 22 Nov): a long press on a message (a phone) or its ☺ button (a mouse)
  // opens a row of emoji; a tap sends it as a Matrix reaction, tapping your own again takes it back.
  // Agents only act on reactions to their own approval prompts, so a reaction never starts a reply.
  // The agent hears every reaction at its next turn; 👎 and ❤️ also go into its lessons, and ❓ / 🔁
  // ask it to explain more or try again (logos-reactions, docs/messages.md; /emojis lists them).
  const QUICK = ['👍', '👎', '❤️', '😂', '❓', '🔁'];
  const QUICK_HINT = { '👎': 'Not right: the agent notes it', '❤️': 'Loved it: the agent notes it',
    '❓': 'Ask the agent to explain more', '🔁': 'Ask the agent to try again' };
  async function react(ev, key, mineId) {
    pickerFor = null;
    try {
      if (mineId) await c.redactEvent(room.roomId, mineId);
      else await c.sendEvent(room.roomId, 'm.reaction', { 'm.relates_to': { rel_type: 'm.annotation', event_id: ev.getId(), key } });
      note.textContent = '';
    } catch (e) { note.textContent = `Reaction not sent: ${e.message}`; }
    draw(false);
  }
  // Which message has its emoji row open; kept here because every new event redraws the chat.
  let pickerFor = null;
  function closePicker() { if (pickerFor) { pickerFor = null; draw(false); } }
  function pickerOf(ev, have, text) {
    const row = h('div', { class: 'mx-picker', role: 'menu', 'aria-label': 'React' },
      ...QUICK.map((k) => h('button', { type: 'button', role: 'menuitem', class: have.get(k) ? 'mine' : '', 'aria-label': QUICK_HINT[k] || `React ${k}`, title: QUICK_HINT[k] || '',
        'aria-pressed': String(!!have.get(k)), onclick: () => react(ev, k, have.get(k) || null) }, k)),
      text && navigator.clipboard ? h('button', { type: 'button', role: 'menuitem', class: 'mx-copy', onclick: async () => {
        closePicker();
        try { await navigator.clipboard.writeText(text.trim()); note.textContent = 'Copied.'; } catch { note.textContent = 'Could not copy.'; }
      } }, 'Copy') : null);
    requestAnimationFrame(() => { if (row.isConnected && !row.contains(document.activeElement)) row.querySelector('button')?.focus({ preventScroll: true }); });
    return row;
  }
  const outside = (e) => { if (pickerFor && !e.target.closest('.mx-picker, .mx-react-open')) closePicker(); };
  const escape = (e) => { if (e.key === 'Escape') closePicker(); };
  document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape);
  off.push(() => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); });
  function longPress(el, fire) {
    let t = null, x = 0, y = 0;
    const stop = () => { clearTimeout(t); t = null; };
    el.addEventListener('pointerdown', (e) => {
      if (e.pointerType === 'mouse' || e.target.closest('a, button, summary')) return;
      x = e.clientX; y = e.clientY; stop();
      t = setTimeout(() => { t = null; navigator.vibrate?.(10); fire(); }, 450);
    });
    el.addEventListener('pointermove', (e) => { if (t && Math.hypot(e.clientX - x, e.clientY - y) > 10) stop(); });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach((n) => el.addEventListener(n, stop));
    // On a touch screen the long press is ours: no system menu or word selection on the bubble.
    el.addEventListener('contextmenu', (e) => { if (matchMedia('(pointer: coarse)').matches) e.preventDefault(); });
  }

  // ── the conversations: Main, then threads (newest first, those quiet for 30 days left out) ──────
  function threadsNow() {
    if (!room) return [];
    const since = Date.now() - THREAD_DAYS * 86400000;
    return [...replyMap].filter(([id]) => !room.findEventById(id)?.isDecryptionFailure())
      .filter(([id, r]) => !(briefs.has(id) && r.count <= 1))
      .map(([id, r]) => ({ id, title: titleOf(room.findEventById(id)), last: r.last }))
      .filter((t) => t.last >= since).sort((a, b) => b.last - a.last);
  }
  const convList = h('div', { class: 'convs' });
  function drawLists() {
    const list = threadsNow();
    const link = (href, title, last, on) => h('a', { href, class: 'conv-link', 'aria-current': on ? 'true' : null },
      h('span', { class: 'conv-title' }, title), h('span', { class: 'when' }, last ? when(last / 1000) : ''));
    convList.replaceChildren(link(main, 'Main conversation', 0, !threadId),
      ...list.map((t) => link(`${main}/thread/${encodeURIComponent(t.id)}`, t.title, t.last, t.id === threadId)));
    const chip = (href, label, on, extra) => h('a', { href, class: `mx-chip${on ? ' on' : ''}${extra || ''}`, role: 'tab', 'aria-selected': String(!!on), title: label }, h('span', { class: 'chip-text' }, label));
    // On a phone the conversations panel (☰) opens from the end of this row; the header has the agent bar.
    const panel = sideBits.convBtn ? h('button', { type: 'button', class: 'mx-chip panel', 'aria-label': 'All conversations', title: 'All conversations',
      onclick: () => sideBits.convBtn.click() }, '☰') : null;
    chipsRow.replaceChildren(chip(`${main}/thread/new`, '+', isNew, ' add'), chip(main, 'Main', !threadId),
      ...list.map((t) => chip(`${main}/thread/${encodeURIComponent(t.id)}`, t.title, t.id === threadId)), panel);
  }

  // The events of what is on screen: the main timeline, or one thread (its first message, then the replies).
  function eventsNow() {
    const all = room.getLiveTimeline().getEvents();
    if (!threadId || isNew) return all;
    const rootEv = all.find((e) => e.getId() === threadId) || room.findEventById(threadId);
    return [rootEv, ...all.filter((e) => e.threadRootId === threadId && e.getId() !== threadId)].filter(Boolean);
  }
  // Replies per thread, from each message's thread mark (m.relates_to rel_type m.thread).
  function threadReplies() {
    const n = new Map();
    briefs = new Map();
    for (const e of room.getLiveTimeline().getEvents()) {
      const r = e.threadRootId;
      if (r && r !== e.getId() && e.getType() === 'm.room.message') {
        n.set(r, { count: (n.get(r)?.count || 0) + 1, last: e.getTs() });
        const root = room.findEventById(r);
        if (!briefs.has(r) && cronJob(root) && e.getSender() === root.getSender()) briefs.set(r, e);
      }
    }
    return n;
  }

  function draw(stick) {
    if (!room || !alive) return;
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    const events = eventsNow();
    replyMap = threadReplies();
    drawLists();
    const edits = new Map(), reactions = new Map();
    for (const ev of events) {
      const rel = ev.getContent()['m.relates_to'] || ev.getWireContent()?.['m.relates_to'];
      if (ev.getType() === 'm.room.message' && ev.getContent()['m.new_content'] && rel?.rel_type === 'm.replace') edits.set(rel.event_id, ev.getContent()['m.new_content']);
      if (ev.getType() === 'm.reaction' && rel?.rel_type === 'm.annotation' && !ev.isRedacted()) {
        // Per message: each emoji, how many, and your own reaction's id (so tapping it again takes it back).
        const map = reactions.get(rel.event_id) || new Map();
        const r = map.get(rel.key) || { n: 0, mine: null };
        r.n += 1; if (ev.getSender() === me && ev.getId() && !ev.getId().startsWith('~')) r.mine = ev.getId();
        map.set(rel.key, r); reactions.set(rel.event_id, map);
      }
    }
    const inMain = !threadId;
    const shown = events.filter((ev) => (ev.getType() === 'm.room.message' || (ev.isEncrypted() && ev.isDecryptionFailure()))
      && !(ev.getContent()['m.relates_to']?.rel_type === 'm.replace') && !ev.isRedacted()
      && (!inMain || !ev.threadRootId || ev.threadRootId === ev.getId()));
    if (isNew) { log.replaceChildren(none('A new conversation: write its first message below. It becomes its title.')); return; }
    if (threadId) { const t = shown[0]; convTitle.textContent = t ? titleOf(t) : 'Conversation'; }
    const older = h('button', { type: 'button', class: 'linkish mx-older', onclick: async (e) => {
      e.target.textContent = 'Loading…'; await c.scrollback(room, 30); draw(false);
    } }, 'Earlier messages');
    log.replaceChildren(...(threadId ? [] : [older]), ...(shown.length ? shown.map((ev, i) => {
      // The time shows at the end of a run: the next message is from someone else, or 5+ minutes later.
      const next = shown[i + 1];
      const showTime = !next || next.getSender() !== ev.getSender() || next.getTs() - ev.getTs() > 5 * 60000;
      return bubbleFor(ev, edits, reactions, inMain, showTime);
    }) : [none('Nothing said here yet.')]));
    // Read: tell Matrix this chat has been read up to its newest message (as Element X does), so it
    // leaves Notifications here and on the phone (Greg, 21 Nov). Only while the page is on screen.
    const newest = [...room.getLiveTimeline().getEvents()].reverse().find((e) => e.getId() && !e.getId().startsWith('~') && e.getSender() !== me);
    if (newest && newest.getId() !== lastReceipt && document.visibilityState === 'visible') {
      lastReceipt = newest.getId();
      c.sendReadReceipt(newest).catch(() => { lastReceipt = null; });
    }
    // Still waiting for the agent when the newest message is yours.
    const last = shown[shown.length - 1];
    // (a conversation's title notice is not a message waiting for an answer)
    // Waiting for the agent: your newest readable message, under 15 minutes old, that is not a conversation's
    // opening (its title notice, or a message whose thread already has replies).
    const waitingOnAgent = !!(last && last.getSender() === me && !last.isDecryptionFailure()
      && last.getContent().msgtype !== 'm.notice' && !replyMap.has(last.getId()) && Date.now() - last.getTs() < 15 * 60000);
    thinkingRow.hidden = !waitingOnAgent && !typing;
    askedAt = waitingOnAgent ? last.getTs() : (typing ? askedAt || Date.now() : 0);
    progress();
    if (stick || atBottom) log.scrollTop = log.scrollHeight;
  }

  // ── still thinking (F33, Greg 21 Nov: "something to show it's working and not stuck") ──────────
  // After a minute: how long since your message, and from the model guard how far the reply has got
  // ("about 900 words of thinking so far", "writing the answer"). The guard also cuts thinking that
  // goes round in circles, and hermes then asks again by itself.
  let askedAt = 0, progressTimer = null, lastProgress = 0;
  function progress() {
    clearTimeout(progressTimer);
    if (thinkingRow.hidden || !askedAt) { thinkingMore.textContent = ''; return; }
    const secs = (Date.now() - askedAt) / 1000;
    if (secs < 60) { progressTimer = setTimeout(progress, (60 - secs) * 1000 + 50); return; }
    const mins = Math.floor(secs / 60);
    const base = ` · ${mins} min`;
    if (!thinkingMore.textContent.startsWith(base)) thinkingMore.textContent = base;
    if (Date.now() - lastProgress > 4000) {
      lastProgress = Date.now();
      getJSON('/api/v2/models/progress').then((p) => {
        if (thinkingRow.hidden) return;
        const people = (p.live || []).filter((r) => r.who === 'person');
        let more = '';
        if (p.model_server_off) more = ' · the AI computer is off; the answer comes when it is back';
        else if (people.length === 1 && people[0].answer_chars > 0) more = ' · writing the answer';
        else if (people.length === 1 && people[0].thinking_chars > 0) {
          const words = Math.round(people[0].thinking_chars / 6 / 50) * 50;
          more = words ? ` · about ${words.toLocaleString()} words of thinking so far` : '';
        } else if (people.length > 1) more = ` · ${people.length} replies being written`;
        thinkingMore.textContent = base + more;
      }).catch(() => {});
    }
    progressTimer = setTimeout(progress, 5000);
  }
  off.push(() => clearTimeout(progressTimer));

  // ── live ───────────────────────────────────────────────────────────────────────────────────
  let typing = false, timer = null;
  const redraw = () => { clearTimeout(timer); timer = setTimeout(() => draw(false), 60); };
  const onTimeline = (ev, r) => { if (room && r && r.roomId === room.roomId) redraw(); };
  const onDecrypted = (ev) => { if (room && ev.getRoomId() === room.roomId) redraw(); };
  const onTyping = (ev, member) => {
    if (!room || member.roomId !== room.roomId || member.userId !== agent.matrix_id) return;
    typing = member.typing; thinkingRow.hidden = !typing && thinkingRow.hidden; redraw();
  };
  const onEcho = () => redraw();
  c.on(m.RoomEvent.Timeline, onTimeline); off.push(() => c.off(m.RoomEvent.Timeline, onTimeline));
  c.on(m.MatrixEventEvent.Decrypted, onDecrypted); off.push(() => c.off(m.MatrixEventEvent.Decrypted, onDecrypted));
  c.on(m.RoomMemberEvent.Typing, onTyping); off.push(() => c.off(m.RoomMemberEvent.Typing, onTyping));
  c.on(m.RoomEvent.LocalEchoUpdated, onEcho); off.push(() => c.off(m.RoomEvent.LocalEchoUpdated, onEcho));

  async function sendText(text) {
    text = String(text || '').trim();
    if (!text || !room) return;
    input.value = ''; grow();
    idle();
    note.textContent = '';
    try {
      if (isNew) {
        // The title as a notice (agents ignore notices), then the first message inside its thread.
        const r = await c.sendMessage(room.roomId, { msgtype: 'm.notice', body: TITLE_MARK + text.split('\n')[0].slice(0, 80) });
        await c.sendMessage(room.roomId, r.event_id, { msgtype: 'm.text', body: text });
        location.hash = `${main}/thread/${encodeURIComponent(r.event_id)}`;
        return;
      }
      if (threadId) await c.sendMessage(room.roomId, threadId, { msgtype: 'm.text', body: text });
      else await c.sendMessage(room.roomId, { msgtype: 'm.text', body: text });
    } catch (e) { note.textContent = `Not sent: ${e.message}. It is kept above; send it again.`; input.value = text; grow(); }
    draw(true);
  }

  // Panel: the main conversation (threads arrive with M3), then old web chats for 30 days.
  const archive = h('div', { class: 'convs' });
  sideBits.side.replaceChildren(sideBits.sideTop || '',
    h('a', { href: `${main}/thread/new`, class: 'btn mx-new' }, 'New conversation'),
    h('h2', { class: 'label' }, 'Conversations'), convList,
    h('h2', { class: 'label' }, 'Archive (old web chats)'), archive,
    ...(sideBits.toggles || []));
  getJSON(`/api/v2/agents/${agent.name}/conversations`).then(({ conversations }) => {
    const since = Date.now() / 1000 - ARCHIVE_DAYS * 86400;
    const old = conversations.filter((x) => x.where === 'web' && (x.last_active || 0) >= since);
    archive.replaceChildren(...(old.length ? old.map((x) => h('a', { href: `#/chat/${agent.name}/${x.chat_id}`, class: 'conv-link' },
      h('span', { class: 'conv-title' }, x.title), h('span', { class: 'when' }, when(x.last_active))))
      : [none(`None in the last ${ARCHIVE_DAYS} days.`)]));
  }).catch(() => archive.replaceChildren(none('Could not list them just now.')));

  // Enough history for 30 days of conversations (a few pages back at most).
  if (room) {
    for (let i = 0; i < 4; i++) {
      const first = room.getLiveTimeline().getEvents()[0];
      if (!first || first.getTs() < Date.now() - THREAD_DAYS * 86400000) break;
      try { await c.scrollback(room, 50); } catch (e) { break; }
    }
  }
  draw(true);
  const asked = takeAsk(agent.name);           // a question asked on Home
  if (asked) sendText(asked);
  const spoken = takeVoice(agent.name);        // a voice note recorded by holding the agent's mark
  if (spoken) toWords(spoken, true);
  focusIfKeyboard(input);
  return () => { alive = false; clearTimeout(timer); for (const f of off) f(); };
}
