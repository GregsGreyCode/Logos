// Chat as your Matrix room (story M2): the conversation is your encrypted Element room with the agent,
// read and written by this browser as one of your Matrix devices (mx.js, story M1). The same messages
// appear here and in Element X, live. Under each of the agent's replies, "Thinking" folds out its
// reasoning and tool calls (from the agent's own record, /api/v2/agents/<name>/steps).
// Old web conversations stay readable for 30 days under Archive (Greg, 21 Nov).
// M3: each further conversation is a thread in the room. "New conversation" posts a notice as its title
// (agents ignore notices) and the first message inside the thread, so the agent answers there, as its own
// conversation. Its title is that first message; threads quiet for 30 days leave the list (Greg, 21 Nov).
// On a phone the conversations are chips above the chat (Greg chose K2).
import { reveal, h, api, getJSON, markdown, when, none, loading, focusIfKeyboard, icon, ICONS } from './util.js';
import * as mx from './mx.js';
import { clearRoom } from './push.js';
import { voiceNote, imageThumb, fileCard, prepareFile, sendPrepared, fileSize } from './media.js';
import { stepsView } from './chat.js';
import { takeAsk } from './home.js';
import { createRecorder } from './voice.js';
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

export function pickRoom(c, agentId) {
  const rooms = c.getRooms().filter((r) => r.getMyMembership() === 'join'
    && ['join', 'invite'].includes(r.getMember(agentId)?.membership));
  // The direct room with the agent first (two members), else the busiest room it shares with you.
  rooms.sort((a, b) => (a.getJoinedMemberCount() === 2 ? 0 : 1) - (b.getJoinedMemberCount() === 2 ? 0 : 1)
    || b.getLastActiveTimestamp() - a.getLastActiveTimestamp());
  return rooms[0] || null;
}

// F36 (Greg, 22 Nov): a group is a Matrix room you share with at least one agent and someone else
// (the Household Group). Each opens like an agent's chat, at #/chat/<agent>/group/<room id>.
export function groupsOf(c, agents) {
  const ids = new Set(agents.map((a) => a.matrix_id).filter(Boolean));
  const me = c.getUserId();
  const out = [];
  for (const room of c.getRooms()) {
    if (room.getMyMembership() !== 'join') continue;
    const members = room.getMembers().filter((mb) => ['join', 'invite'].includes(mb.membership));
    const bots = members.filter((mb) => ids.has(mb.userId));
    const people = members.filter((mb) => !ids.has(mb.userId) && mb.userId !== me);
    if (!bots.length || !people.length) continue;
    const agent = agents.find((a) => a.matrix_id === bots[0].userId);
    out.push({ room, agent, name: room.name || 'Group', link: groupLink(agent, room.roomId),
      people: people.map((mb) => mb.name), agents: bots.map((mb) => mb.userId) });
  }
  return out.sort((a, b) => b.room.getLastActiveTimestamp() - a.room.getLastActiveTimestamp());
}
// The Agents screen's list (Greg, 22 Nov): each agent's own chat and each group, newest first, with its
// last message and how many of the others' messages you have not read.
export function conversationsOf(c, agents) {
  const me = c.getUserId();
  const rows = [];
  const summary = (room) => {
    // Unread: the others' messages after your read marker and after your own last message. The marker
    // is found in the whole timeline, where it usually sits on a reaction (the agent's ✅), not a
    // message; looked for among the messages only, it was never found and the count never cleared.
    const all = room.getLiveTimeline().getEvents();
    const readId = room.getEventReadUpTo?.(me);
    let seenUpTo = -1;
    all.forEach((e, i) => { if (e.getId() === readId || e.getSender() === me) seenUpTo = i; });
    const isMsg = (e) => e.getType() === 'm.room.message' && !e.isRedacted?.()
      && !(e.getContent()['m.relates_to']?.rel_type === 'm.replace') && e.getContent().msgtype !== 'm.notice'
      && !CRON_ROOT.test(String(e.getContent().body || ''));
    const msgs = all.map((e, i) => [e, i]).filter(([e]) => isMsg(e));
    const last = msgs.length ? msgs[msgs.length - 1][0] : null;
    const unread = msgs.filter(([e, i]) => i > seenUpTo && e.getSender() !== me).length;
    const who = last ? (last.getSender() === me ? 'You' : room.getMember(last.getSender())?.name || '') : '';
    const text = last ? (last.isDecryptionFailure?.() ? 'Locked message' : String(last.getContent().body || '').replace(/\s+/g, ' ').replace(/[*_`#>]/g, '')) : '';
    return { ts: last ? last.getTs() : 0, text, who, unread };
  };
  for (const a of agents.filter((x) => x.matrix_id)) {
    const room = pickRoom(c, a.matrix_id);
    if (room && room.getJoinedMemberCount() <= 2) rows.push({ kind: 'agent', agent: a, name: a.display_name, link: `#/chat/${a.name}`, ...summary(room) });
  }
  for (const g of groupsOf(c, agents)) rows.push({ kind: 'group', agent: g.agent, name: g.name, link: g.link, ...summary(g.room) });
  // A chat with a reply you have not seen comes first, the newest reply on top; then the rest by their last message
  // (Greg, 23 Nov: "most recently responded to and not checked").
  return rows.sort((x, y) => (!!y.unread - !!x.unread) || y.ts - x.ts);
}
export const groupLink = (agent, roomId) => `#/chat/${agent.name}/group/${encodeURIComponent(roomId)}`;
const escapeHtml = (t) => String(t).replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));

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

export async function matrixChat(root, agent, agents, state, sideBits, threadId = null, groupId = null) {
  const group = groupId || null;
  if (group) threadId = null;               // a group is one conversation (hermes: one session per room)
  const isNew = threadId === 'new';
  let alive = true;
  const off = [];
  const log = h('div', { class: 'log', role: 'log', 'aria-live': 'polite' }, loading('bubbles', 6));
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
  const main = group ? groupLink(agent, group) : `#/chat/${agent.name}`;
  const crumb = threadId ? h('a', { href: main, class: 'mx-crumb' }, 'Main conversation ›') : null;
  const convTitle = h('span', { class: 'mx-conv-title' }, isNew ? 'New conversation' : '');
  const chipsRow = h('div', { class: 'mx-chips phone-only', role: 'tablist', 'aria-label': 'Chats' });
  // Send (Greg, 23 Nov): the arrow when there is something to send; with nothing in the box and the agent working,
  // it is Stop (■), which asks the agent to stop; otherwise it rests. The microphone is a button of its own,
  // tap to start and tap to finish (voice.js), no longer a hold.
  const send = h('button', { type: 'submit', class: 'btn send-btn idle', 'aria-label': 'Send', title: 'Send', disabled: true },
    h('span', { class: 'btn-words' }, 'Send'), h('span', { class: 'send-ico' }, icon(ICONS.send)), h('span', { class: 'stop-ico' }, icon(ICONS.stop)));
  const note = h('p', { class: 'when mx-note', role: 'status' });
  const thinkingMore = h('span', { class: 'mx-thinking-more' });
  const thinkingRow = h('div', { class: 'mx-thinking', hidden: true },
    // One sentence that wraps as text (Greg, 23 Nov: as three flex items it split into columns on a phone).
    h('span', { class: 'mx-thinking-mark', 'aria-hidden': 'true' }),
    h('span', { class: 'mx-thinking-text' }, `${agent.display_name} is thinking`, h('span', { class: 'dots', 'aria-hidden': 'true' }, '…'), ' ', thinkingMore));
  const modelLine = h('div', { class: 'model-line down', role: 'status', hidden: agent.model_up !== false },
    'AI computer off · your message will queue');
  // 📎 (Greg, 22 Nov; several at once 23 Nov): photos and files wait in a strip above the box, each with
  // ✕, while you type the message that goes with them; Send sends them all, then the message, so the agent
  // answers them together (the logos-media-batch plugin makes them one turn). The phone's picker offers
  // the camera too.
  const MAX_FILES = 10;
  const picker = h('input', { type: 'file', multiple: true, class: 'visually-hidden', tabindex: '-1', 'aria-hidden': 'true',
    accept: 'image/*,video/*,audio/*,application/pdf,.doc,.docx,.xls,.xlsx,.txt,.csv,.md' });
  const attach = h('button', { type: 'button', class: 'mx-attach', 'aria-label': 'Add photos or files', title: 'Add photos or files',
    onclick: () => picker.click() }, icon(ICONS.clip));
  const pending = h('div', { class: 'mx-pending', hidden: true });
  let files = [];
  function drawPending() {
    pending.hidden = !files.length;
    pending.replaceChildren(...files.map((f, i) => h('span', { class: 'mx-pending-item', title: f.name },
      f.type.startsWith('image/') ? h('img', { class: 'mx-pending-img', alt: f.name, src: f._url || (f._url = URL.createObjectURL(f)) })
        : h('span', { class: 'mx-pending-file' }, icon(ICONS.file), h('span', {}, f.name)),
      h('button', { type: 'button', class: 'mx-pending-x', 'aria-label': `Leave out ${f.name}`, onclick: () => { files.splice(i, 1); drawPending(); idle(); } }, '✕'))),
    files.length < MAX_FILES ? h('button', { type: 'button', class: 'mx-pending-add', 'aria-label': 'Add more', onclick: () => picker.click() }, '+') : '',
    h('span', { class: 'when mx-pending-hint' }, `${files.length} ${files.length === 1 ? 'file' : 'files'} · add a message, then send`));
  }
  picker.addEventListener('change', () => {
    const picked = [...(picker.files || [])];
    picker.value = '';
    const big = picked.filter((f) => f.size > 45 * 1024 * 1024);
    const all = [...files, ...picked.filter((f) => f.size <= 45 * 1024 * 1024)];
    files = all.slice(0, MAX_FILES);
    note.textContent = big.length ? `${big.map((f) => f.name).join(', ')}: too big to send (45 MB at most).`
      : all.length > MAX_FILES ? `${MAX_FILES} at most in one go: the rest were left out.` : '';
    drawPending(); idle(); input.focus();
  });
  // Every file uploaded first, then the messages back to back: the photos, then the text.
  async function sendFiles(text) {
    const list = files.slice();
    files = []; drawPending();
    note.textContent = list.length === 1 ? `Sending ${list[0].name}…` : `Sending ${list.length} files…`;
    try {
      const extra = group ? (() => { const g = groupMessage(text || ''); return { 'm.mentions': g['m.mentions'] || {} }; })() : {};
      const contents = await Promise.all(list.map((f) => prepareFile(c, room.roomId, f)));
      const where = threadId && !isNew ? threadId : null;
      for (const content of contents) await sendPrepared(c, room.roomId, content, { threadId: where, extra });
      note.textContent = '';
      for (const f of list) if (f._url) URL.revokeObjectURL(f._url);
      if (text && text.trim()) await sendText(text);
    } catch (err) { note.textContent = `Not sent: ${err.message}. They are kept above; send again.`; files = list; drawPending(); idle(); }
  }
  const say = (t) => { note.replaceChildren(typeof t === 'string' ? document.createTextNode(t) : t); };
  const voice = createRecorder({ agent: agent.name, say,
    onWords: (text) => { input.value = (input.value.trim() ? input.value.trimEnd() + ' ' : '') + text; grow(); syncSend(); input.focus(); },
    onBusy: (b) => form.classList.toggle('transcribing', b) });
  const submit = () => {
    if (send.dataset.mode === 'stop') { stopAgent(); return; }
    return files.length ? sendFiles(input.value) : sendText(input.value);
  };
  const form = h('form', { class: 'composer', onsubmit: (e) => { e.preventDefault(); submit(); } },
    h('label', { for: 'chat-input', class: 'sr' }, 'Message'), attach, picker, input, voice.mic, send, voice.bar);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } });
  // Send, Stop or resting, from what is in the box and whether the agent is working (thinkingRow shows it).
  function syncSend() {
    const has = !!input.value.trim() || files.length > 0;
    const mode = has ? 'send' : (!thinkingRow.hidden ? 'stop' : 'idle');
    send.dataset.mode = mode;
    send.classList.toggle('idle', mode === 'idle'); send.classList.toggle('stop', mode === 'stop');
    const words = mode === 'stop' ? `Stop ${agent.display_name}` : 'Send';
    send.setAttribute('aria-label', words); send.title = words;
    send.disabled = mode === 'idle';
  }
  const idle = syncSend;                                   // its old name, used where files are added or removed
  input.addEventListener('input', syncSend);
  // Stop: hermes stops the running answer for /stop whatever its settings (it is sent as a message).
  async function stopAgent() {
    send.disabled = true;
    await sendText('/stop');
    say(`Asked ${agent.display_name} to stop.`);
    setTimeout(syncSend, 1200);
  }
  syncSend();
  off.push(() => voice.destroy());

  // In a group: ‹ back, the group's name and who is in it; and above the box, the agents to ask. Agents
  // answer in a group only when asked (an @mention), so the message says whom it is for.
  const groupTitle = h('span', { class: 'mark-name' }, 'Group');
  const groupWho = h('span', { class: 'when group-who' });
  const groupHead = h('div', { class: 'mark-bar group-bar', role: 'navigation', 'aria-label': 'Group' },
    h('a', { href: '#/chat', class: 'mark-back', 'aria-label': 'All agents and groups', title: 'All agents and groups' }, '‹'),
    h('span', { class: 'group-title' }, groupTitle, groupWho));
  const askRow = h('div', { class: 'mx-ask', hidden: !group });
  let asking = new Set();                    // the agents your next message in the group is for
  const askKey = `logos.group.ask.${group}`;

  root.replaceChildren(
    h('section', { class: 'chat', 'aria-label': 'Conversation' },
      h('header', { class: 'chat-head' },
        // M4: the agents' marks replace the agent tabs; inside a thread, its title; in a group, its name.
        group ? groupHead : threadId ? null : markBar(agents.filter((a) => a.matrix_id), agent),
        threadId || group ? null : agentBar(agent, 'chat', agents.filter((a) => a.matrix_id)),
        crumb, threadId ? h('h1', { class: 'mx-thread-title' }, convTitle) : h('h1', { class: 'visually-hidden' }, agent.display_name),
        sideBits.reopen || null, sideBits.viewMenu || null, sideBits.convBtn || null),
      group ? null : chipsRow, log, thinkingRow, note, modelLine, group ? askRow : null, pending, form),
    sideBits.side, sideBits.sideScrim || null);

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
    log.replaceChildren(h('div', { class: 'notice' }, `This device could not connect to your chats (${e.message}). `,
      h('a', { href: '#/settings/matrix' }, 'Check Settings › Chats & devices'), '.'));
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
    // 'sent' is the library's word for "the server has it, its echo has not come back yet" (Greg, 23 Nov: photos said
    // "sending…" while the agent was already working on them). Working on it: "received". Otherwise the tick.
    if (ev.status === 'sent') return !thinkingRow.hidden ? 'received' : 'sent';
    if (ev.status) return ev.status === 'not_sent' ? 'not sent · tap or hold to retry' : 'sending…';
    const key = ev.getSenderKey && ev.getSenderKey();
    if (!key || key === ownKey) return 'sent';
    const name = deviceNames.get(key);
    if (!name && !looking) { looking = true; readNames().then(() => { looking = false; redraw(); }); }
    return name ? `from ${name}` : 'from another of your devices';
  };

  room = group ? c.getRoom(group) : pickRoom(c, agent.matrix_id);
  const allAgents = [...agents, ...(sideBits.others || [])].filter((a) => a.matrix_id);
  const agentIds = new Set(allAgents.map((a) => a.matrix_id));
  const inRoom = () => allAgents.filter((a) => ['join', 'invite'].includes(room?.getMember(a.matrix_id)?.membership));
  const nameOf = (id) => room?.getMember(id)?.name || allAgents.find((a) => a.matrix_id === id)?.display_name || id;
  if (group && (!room || room.getMyMembership() !== 'join')) {
    log.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, 'Not in this group'),
      h('p', {}, 'You are not in this group any more, or it has not reached this browser yet.')));
    return () => { alive = false; for (const f of off) f(); };
  }
  if (group) {
    groupTitle.textContent = room.name || 'Group';
    const people = room.getMembers().filter((mb) => mb.membership === 'join' && !agentIds.has(mb.userId)).map((mb) => (mb.userId === me ? 'you' : mb.name));
    groupWho.textContent = [...people, ...inRoom().map((a) => a.display_name)].join(', ');
    input.placeholder = `Message ${room.name || 'the group'}`;
    try { asking = new Set(JSON.parse(localStorage.getItem(askKey) || '[]')); } catch (e) { asking = new Set(); }
    const drawAsk = () => {
      for (const id of [...asking]) if (!inRoom().some((a) => a.matrix_id === id)) asking.delete(id);
      askRow.replaceChildren(...inRoom().map((a) => h('button', {
        type: 'button', class: `chip mx-ask-chip${asking.has(a.matrix_id) ? ' on' : ''}`, 'aria-pressed': String(asking.has(a.matrix_id)),
        title: asking.has(a.matrix_id) ? `Your message goes to ${a.display_name} too` : `Only people read it; tap to ask ${a.display_name}`,
        onclick: () => {
          if (asking.has(a.matrix_id)) asking.delete(a.matrix_id); else asking.add(a.matrix_id);
          try { localStorage.setItem(askKey, JSON.stringify([...asking])); } catch (e) { /* remembered for this visit only */ }
          drawAsk();
        } }, asking.has(a.matrix_id) ? `✓ ${a.display_name} will answer` : `@ ${a.display_name}`)));
    };
    drawAsk();
  }
  if (!room) {
    log.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, `No room with ${agent.display_name} yet`),
      h('p', {}, 'Start the conversation here.'),
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
    const sentHere = where === 'sent';
    const words = [showTime ? when(ev.getTs() / 1000) : '', sentHere ? '' : where, edited ? 'edited' : ''].filter(Boolean).join(' · ');
    if (!words && !sentHere) return null;
    return h('span', { class: 'when mx-meta', title: sentHere ? `Sent${seen ? ` · seen by ${agent.display_name}` : ''}` : null },
      words, sentHere ? h('span', { class: `mx-tick${seen ? ' seen' : ''}`, 'aria-label': seen ? 'seen' : 'sent' }, icon(ICONS.check)) : null);
  }
  const ACK = new Set(['✅', '✔️', '👀']);
  const unfolded = new Set();              // long posts opened with Show all (kept across redraws)

  let latestShown = null;
  // The questions in a reply: its sentences that end in "?" (markdown stripped), the last three.
  function questionsIn(text) {
    const plain = String(text).replace(/```[\s\S]*?```/g, ' ').replace(/[*_`#>|]/g, '').replace(/\[(.*?)\]\([^)]*\)/g, '$1');
    const found = plain.split(/\n+/).flatMap((line) => line.match(/[^.!?]*\?/g) || []).map((q) => q.replace(/^[\s\-•\d.)]+/, '').trim())
      .filter((q) => q.length > 8 && q.length < 300);
    return [...new Set(found)].slice(-3);
  }
  function bubbleFor(ev, edits, reactions, inMain, showTime = true, showName = false) {
    const mine = ev.getSender() === me;
    const fromAgent = agentIds.has(ev.getSender());
    const content = edits.get(ev.getId()) || ev.getContent();
    const kind = content.msgtype;
    const thread = inMain ? replyMap.get(ev.getId()) : null;
    const job = cronJob(ev);
    if (job && (!inMain || group)) return h('p', { class: 'when mx-thread-start' }, `${jobTitle(job)} · ${when(ev.getTs() / 1000)}`);
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
        : h('p', { class: 'when mx-thread-start' }, `Started ${when(ev.getTs() / 1000)}`);
    }
    let body;
    if (ev.isDecryptionFailure && ev.isDecryptionFailure()) body = [h('em', { class: 'when' }, 'Could not decrypt this message (sent before this browser was connected, and not in your key backup).')];
    // Voice notes, images and files open here (Greg, 22 Nov), decrypted in this browser (media.js).
    else if (kind === 'm.audio') body = [voiceNote(c, content)];
    else if (kind === 'm.image') body = [imageThumb(c, content)];
    else if (kind === 'm.file' || kind === 'm.video') body = [fileCard(c, content, kind)];
    else body = mine ? [String(content.body || '')] : markdown(String(content.body || ''));
    const rx = [...(reactions.get(ev.getId()) || [])];
    const seen = mine && rx.some(([k, r]) => ACK.has(k) && r.n > (r.mine ? 1 : 0));
    const chips = rx.filter(([k, r]) => !(mine && ACK.has(k) && !r.mine));
    const meta = metaFor(ev, mine, edits.has(ev.getId()), showTime, seen);
    // "Thinking ›" under a reply only with Settings > Show thinking on (Greg, 21 Nov: off by default).
    const think = ev.getSender() === agent.matrix_id && kind === 'm.text' && content.body && getPref('thinking', false) ? thinking(String(content.body)) : null;
    const canReact = ev.getId() && !ev.getId().startsWith('~') && !(ev.isDecryptionFailure && ev.isDecryptionFailure());
    const bubble = h('div', { class: `msg ${mine ? 'user' : 'assistant'}${mine || fromAgent ? '' : ' person'}` }, ...body);
    const text = String(content.body || '');
    // Long replies fold, but not the newest one while it is the last thing said and under 10 minutes old
    // (Greg, 23 Nov: a question at the end of a long answer was hidden while it waited on him).
    const fresh = ev === latestShown && Date.now() - ev.getTs() < 10 * 60000;
    const long = !mine && !fresh && (text.length > 700 || text.split('\n').length > 12);
    const folded = long && !unfolded.has(ev.getId());
    if (folded) bubble.classList.add('folded');
    // A folded message that asks something shows its question under the fold, so it is never missed.
    const asks = folded ? questionsIn(text) : [];
    const turn = h('div', { class: `mx-turn ${mine ? 'me' : 'agent'}${showTime ? '' : ' grouped'}` },
      showName ? h('span', { class: `mx-sender${fromAgent ? ' bot' : ''}` }, nameOf(ev.getSender())) : null,
      bubble,
      long ? h('button', { type: 'button', class: 'linkish mx-fold', onclick: () => {
        if (unfolded.has(ev.getId())) unfolded.delete(ev.getId()); else unfolded.add(ev.getId());
        draw(false);
      } }, unfolded.has(ev.getId()) ? 'Show less' : 'Show all') : null,
      asks.length ? h('div', { class: 'mx-asks', role: 'note' }, h('span', { class: 'mx-asks-label' }, asks.length > 1 ? 'Questions' : 'Question'),
        ...asks.map((q) => h('p', {}, q))) : null,
      chips.length ? h('div', { class: 'mx-reactions' }, chips.map(([k, r]) => h('button', {
        type: 'button', class: `chip mx-react${r.mine ? ' mine' : ''}`, 'aria-pressed': String(!!r.mine),
        title: r.mine ? 'Take your reaction back' : 'React with this too', onclick: () => react(ev, k, r.mine),
      }, `${k}${r.n > 1 ? ` ${r.n}` : ''}`))) : null,
      meta || think ? h('div', { class: 'mx-foot' }, meta, think) : null,
      thread ? h('a', { class: 'mx-thread-link', href: `${main}/thread/${encodeURIComponent(ev.getId())}` },
        `↳ ${thread.count} repl${thread.count === 1 ? 'y' : 'ies'} · open this conversation`) : null);
    if (mine && ev.status === 'not_sent') {
      bubble.classList.add('failed');
      const openFail = () => { failMenuFor = failMenuFor === ev ? null : ev; draw(false); };
      bubble.append(h('button', { type: 'button', class: 'mx-failed', 'aria-label': 'Not sent: retry or delete', title: 'Not sent: retry or delete', onclick: (e) => { e.stopPropagation(); openFail(); } }, '!'));
      longPress(bubble, openFail);
      bubble.addEventListener('click', openFail);
      if (failMenuFor === ev) turn.insertBefore(failMenu(ev), bubble.nextSibling);
    }
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
      if (mineId) { await c.redactEvent(room.roomId, mineId); takenBack.add(mineId); }
      else await c.sendEvent(room.roomId, 'm.reaction', { 'm.relates_to': { rel_type: 'm.annotation', event_id: ev.getId(), key } });
      note.textContent = '';
    } catch (e) { note.textContent = `Reaction not sent: ${e.message}`; }
    draw(false);
  }
  // Which message has its emoji row open; kept here because every new event redraws the chat.
  let pickerFor = null;
  const takenBack = new Set();               // your reactions taken back here, gone from the screen at once
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
    if (group) return n;
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

  let drawnOnce = false;
  // Pinned to the newest message (Greg, 23 Nov: on a desktop a chat opened short of the bottom, as images,
  // folds and the fade-in grew it after the first scroll): while pinned, anything that makes the log taller
  // keeps it at the bottom; scrolling up unpins it, coming back down pins it again.
  let pinned = true;
  const toBottom = () => { if (pinned) log.scrollTop = log.scrollHeight; };
  log.addEventListener('scroll', () => { pinned = log.scrollHeight - log.scrollTop - log.clientHeight < 80; }, { passive: true });
  log.addEventListener('load', toBottom, true);                       // an image or a video arriving
  const logGrows = new MutationObserver(() => requestAnimationFrame(toBottom));
  logGrows.observe(log, { childList: true, subtree: true });
  const logResize = new ResizeObserver(toBottom);
  logResize.observe(log);
  off.push(() => { logGrows.disconnect(); logResize.disconnect(); });
  function draw(stick) {
    if (!room || !alive) return;
    if (!drawnOnce) { drawnOnce = true; reveal(log); }
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    const events = eventsNow();
    replyMap = threadReplies();
    drawLists();
    const edits = new Map(), reactions = new Map();
    // Reactions taken back (Greg, 23 Nov: tapping his own 🔁 sent the take-back 13 times, each accepted,
    // and the chip stayed: in an encrypted room the library does not always mark the reaction as
    // redacted). So every redaction in the room counts, and your own counts at once (takenBack).
    const gone = new Set(takenBack);
    for (const ev of events) if (ev.getType() === 'm.room.redaction') {
      const id = ev.event?.redacts || ev.getContent()?.redacts || ev.getAssociatedId?.();
      if (id) gone.add(id);
    }
    for (const ev of events) {
      const rel = ev.getContent()['m.relates_to'] || ev.getWireContent()?.['m.relates_to'];
      if (ev.getType() === 'm.room.message' && ev.getContent()['m.new_content'] && rel?.rel_type === 'm.replace') edits.set(rel.event_id, ev.getContent()['m.new_content']);
      if (ev.getType() === 'm.reaction' && rel?.rel_type === 'm.annotation' && !ev.isRedacted() && !gone.has(ev.getId())) {
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
      && (!inMain || group || !ev.threadRootId || ev.threadRootId === ev.getId()));
    if (isNew) { log.replaceChildren(none('A new conversation: write its first message below. It becomes its title.')); return; }
    if (threadId) { const t = shown[0]; convTitle.textContent = t ? titleOf(t) : 'Conversation'; }
    const older = h('button', { type: 'button', class: 'linkish mx-older', onclick: async (e) => {
      e.target.textContent = 'Loading…'; await c.scrollback(room, 30); draw(false);
    } }, 'Earlier messages');
    latestShown = shown[shown.length - 1] || null;
    log.replaceChildren(...(threadId ? [] : [older]), ...(shown.length ? shown.map((ev, i) => {
      // The time shows at the end of a run: the next message is from someone else, or 5+ minutes later.
      const next = shown[i + 1];
      const showTime = !next || next.getSender() !== ev.getSender() || next.getTs() - ev.getTs() > 5 * 60000;
      // In a group, who said it: once at the start of each run of someone else's messages.
      const prev = shown[i - 1];
      const showName = !!group && ev.getSender() !== me && (!prev || cronJob(prev) || prev.getSender() !== ev.getSender() || ev.getTs() - prev.getTs() > 5 * 60000);
      return bubbleFor(ev, edits, reactions, inMain, showTime, showName);
    }) : [none('Nothing said here yet.')]));
    // Read: tell Matrix this chat has been read up to its newest message (as Element X does), so it
    // leaves Notifications here and on the phone (Greg, 21 Nov). Only while the page is on screen.
    const newest = [...room.getLiveTimeline().getEvents()].reverse().find((e) => e.getId() && !e.getId().startsWith('~') && e.getSender() !== me);
    if (document.visibilityState === 'visible') clearRoom(room.roomId);   // its phone notification, if any (F37)
    if (newest && newest.getId() !== lastReceipt && document.visibilityState === 'visible') {
      lastReceipt = newest.getId();
      c.sendReadReceipt(newest).catch(() => { lastReceipt = null; });
    }
    // Still waiting for the agent when the newest message is yours.
    const last = shown[shown.length - 1];
    // (a conversation's title notice is not a message waiting for an answer)
    // Waiting for the agent: your newest readable message, under 15 minutes old, that is not a conversation's
    // opening (its title notice, or a message whose thread already has replies).
    const waitingOnAgent = !!(last && last.getSender() === me && last.status !== 'not_sent' && !last.isDecryptionFailure()
      && String(last.getContent().body || '').trim().toLowerCase() !== '/stop'      // a Stop is not a question waiting
      && last.getContent().msgtype !== 'm.notice' && !replyMap.has(last.getId()) && Date.now() - last.getTs() < 15 * 60000
      && (!group || (last.getContent()['m.mentions']?.user_ids || []).some((id) => agentIds.has(id))));
    const wasWorking = !thinkingRow.hidden;
    thinkingRow.hidden = !waitingOnAgent && !typing;
    syncSend();
    if (wasWorking !== !thinkingRow.hidden && shown.some((e) => e.getSender() === me && e.status === 'sent')) setTimeout(() => redraw(), 0);
    askedAt = waitingOnAgent ? last.getTs() : (typing ? askedAt || Date.now() : 0);
    progress();
    if (stick || atBottom) { pinned = true; log.scrollTop = log.scrollHeight; requestAnimationFrame(toBottom); }
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
    if (!room || member.roomId !== room.roomId || !(group ? agentIds.has(member.userId) : member.userId === agent.matrix_id)) return;
    typing = member.typing; thinkingRow.hidden = !typing && thinkingRow.hidden; redraw();
  };
  const onEcho = () => redraw();
  c.on(m.RoomEvent.Timeline, onTimeline); off.push(() => c.off(m.RoomEvent.Timeline, onTimeline));
  c.on(m.MatrixEventEvent.Decrypted, onDecrypted); off.push(() => c.off(m.MatrixEventEvent.Decrypted, onDecrypted));
  c.on(m.RoomMemberEvent.Typing, onTyping); off.push(() => c.off(m.RoomMemberEvent.Typing, onTyping));
  c.on(m.RoomEvent.LocalEchoUpdated, onEcho); off.push(() => c.off(m.RoomEvent.LocalEchoUpdated, onEcho));

  // A group message for the agents picked above: their names first (as Element writes a mention) and
  // m.mentions, which is what hermes goes by. With none picked, only the people read it.
  function groupMessage(text) {
    const to = inRoom().filter((a) => asking.has(a.matrix_id));
    if (!to.length) return { msgtype: 'm.text', body: text, 'm.mentions': {} };
    const names = to.map((a) => a.display_name).join(', ');
    const pills = to.map((a) => `<a href="https://matrix.to/#/${escapeHtml(a.matrix_id)}">${escapeHtml(a.display_name)}</a>`).join(', ');
    return { msgtype: 'm.text', body: `${names}: ${text}`, format: 'org.matrix.custom.html',
      formatted_body: `${pills}: ${escapeHtml(text).replace(/\n/g, '<br>')}`, 'm.mentions': { user_ids: to.map((a) => a.matrix_id) } };
  }

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
      else if (group) await c.sendMessage(room.roomId, groupMessage(text));
      else await c.sendMessage(room.roomId, { msgtype: 'm.text', body: text });
    } catch (e) {
      // The failed message stays in the chat with its ! (retried by itself, or held to retry), as in
      // Element X (Greg, 23 Nov: a line of text and the words back in the box took the screen). Only if
      // it is not there is the text given back.
      if (!failedNow().some((ev) => String(ev.getContent().body || '') === text)) { note.textContent = `Not sent: ${e.message}`; input.value = text; grow(); }
    }
    draw(true);
  }
  // ── messages that did not go: retry and delete ─────────────────────────────────────────────
  function failedNow() {
    if (!room) return [];
    const seen = new Set();
    return [...(room.getPendingEvents?.() || []), ...room.getLiveTimeline().getEvents()]
      .filter((ev) => ev.status === 'not_sent' && !seen.has(ev) && seen.add(ev));
  }
  const tries = new WeakMap();
  async function resend(ev) {
    tries.set(ev, (tries.get(ev) || 0) + 1);
    try { await c.resendEvent(ev, room); } catch (e) { /* still not sent: it keeps its ! */ }
    draw(false);
  }
  // By itself: when the connection comes back, and every 10 s while something waits (5 tries each).
  const retryAll = () => { for (const ev of failedNow()) if ((tries.get(ev) || 0) < 5) resend(ev); };
  const onSync = (state, prev) => { if ((state === 'SYNCING' || state === 'PREPARED') && prev && prev !== state && prev !== 'SYNCING') retryAll(); };
  if (m.ClientEvent?.Sync) { c.on(m.ClientEvent.Sync, onSync); off.push(() => c.off(m.ClientEvent.Sync, onSync)); }
  const retryTimer = setInterval(() => { if (navigator.onLine !== false && failedNow().length) retryAll(); }, 10000);
  off.push(() => clearInterval(retryTimer));
  window.addEventListener('online', retryAll); off.push(() => window.removeEventListener('online', retryAll));
  let failMenuFor = null;
  function failMenu(ev) {
    return h('div', { class: 'mx-picker mx-failmenu', role: 'menu', 'aria-label': 'Not sent' },
      h('button', { type: 'button', role: 'menuitem', onclick: () => { failMenuFor = null; tries.delete(ev); resend(ev); } }, 'Send again'),
      h('button', { type: 'button', role: 'menuitem', class: 'danger-text', onclick: () => { failMenuFor = null; try { c.cancelPendingEvent(ev); } catch (e) { /* gone already */ } draw(false); } }, 'Delete'));
  }

  // Panel: the main conversation (threads arrive with M3), then old web chats for 30 days.
  const archive = h('div', { class: 'convs' });
  if (group) {
    const members = room.getMembers().filter((mb) => ['join', 'invite'].includes(mb.membership));
    sideBits.side.replaceChildren(sideBits.sideTop || '', h('h2', { class: 'label' }, 'In this group'),
      h('div', { class: 'convs' }, members.map((mb) => h('p', { class: 'conv-link' },
        h('span', { class: 'conv-title' }, mb.userId === me ? `${mb.name} (you)` : mb.name),
        h('span', { class: 'when' }, agentIds.has(mb.userId) ? 'agent · answers when asked' : mb.membership === 'invite' ? 'invited' : '')))));
  } else sideBits.side.replaceChildren(sideBits.sideTop || '',
    h('a', { href: `${main}/thread/new`, class: 'btn mx-new' }, 'New conversation'),
    h('h2', { class: 'label' }, 'Chats'), convList,
    h('h2', { class: 'label' }, 'Archive (old web chats)'), archive,
    ...(sideBits.toggles || []));
  if (!group) getJSON(`/api/v2/agents/${agent.name}/conversations`).then(({ conversations }) => {
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
  const asked = group ? null : takeAsk(agent.name);           // a question asked on Home
  if (asked) sendText(asked);
  const spoken = group ? null : takeVoice(agent.name);        // a voice note recorded by holding the agent's mark
  if (spoken) voice.send(spoken, 0, { auto: true }).then((t) => { if (t) sendText(t); });
  voice.resume();                                              // words from a recording made before a reload
  focusIfKeyboard(input);
  return () => { alive = false; clearTimeout(timer); for (const f of off) f(); };
}
