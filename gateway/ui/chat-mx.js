// Chat as your Matrix room (story M2): the conversation is your encrypted Element room with the agent,
// read and written by this browser as one of your Matrix devices (mx.js, story M1). The same messages
// appear here and in Element X, live. Under each of the agent's replies, "Thinking" folds out its
// reasoning and tool calls (from the agent's own record, /api/v2/agents/<name>/steps).
// Old web conversations stay readable for 30 days under Archive (Greg, 21 Nov).
// M3: each further conversation is a thread in the room. "New conversation" posts a notice as its title
// (agents ignore notices) and the first message inside the thread, so the agent answers there, as its own
// conversation. Its title is that first message; threads quiet for 30 days leave the list (Greg, 21 Nov).
// On a phone the conversations are chips above the chat (Greg chose K2).
import { h, getJSON, markdown, when, none, loading, agentLabel } from './util.js';
import * as mx from './mx.js';
import { stepsView } from './chat.js';
import { takeAsk } from './home.js';

const ARCHIVE_DAYS = 30;
const THREAD_DAYS = 30;
const TITLE_MARK = '💬 ';
const titleOf = (ev) => (ev && ev.isDecryptionFailure && ev.isDecryptionFailure()
  ? 'An earlier conversation (cannot be read in this browser)'
  : String(ev?.getContent()?.body || 'Conversation').replace(/^💬\s*/, '').slice(0, 80));

function pickRoom(c, agentId) {
  const rooms = c.getRooms().filter((r) => r.getMyMembership() === 'join'
    && ['join', 'invite'].includes(r.getMember(agentId)?.membership));
  // The direct room with the agent first (two members), else the busiest room it shares with you.
  rooms.sort((a, b) => (a.getJoinedMemberCount() === 2 ? 0 : 1) - (b.getJoinedMemberCount() === 2 ? 0 : 1)
    || b.getLastActiveTimestamp() - a.getLastActiveTimestamp());
  return rooms[0] || null;
}

export async function matrixChat(root, agent, agents, state, sideBits, threadId = null) {
  const isNew = threadId === 'new';
  let alive = true;
  const off = [];
  const log = h('div', { class: 'log', role: 'log', 'aria-live': 'polite' }, loading());
  const input = h('textarea', { id: 'chat-input', rows: '1', placeholder: isNew ? 'Start a new conversation: its first message is its title'
    : threadId ? 'Reply in this conversation' : `Message ${agent.display_name}` });
  const main = `#/chat/${agent.name}`;
  const crumb = threadId ? h('a', { href: main, class: 'mx-crumb' }, 'Main conversation ›') : null;
  const convTitle = h('span', { class: 'mx-conv-title' }, isNew ? 'New conversation' : '');
  const chipsRow = h('div', { class: 'mx-chips phone-only', role: 'tablist', 'aria-label': 'Conversations' });
  const send = h('button', { type: 'submit', class: 'btn' }, 'Send');
  const note = h('p', { class: 'when mx-note', role: 'status' });
  const thinkingRow = h('div', { class: 'mx-thinking', hidden: true },
    h('span', { class: 'mx-thinking-mark', 'aria-hidden': 'true' }), `${agent.display_name} is thinking`, h('span', { class: 'dots', 'aria-hidden': 'true' }, '…'));
  const tabs = agents.length > 1 ? h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Agent' },
    agents.map((a) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(a.name === agent.name),
      onclick: () => { location.hash = `#/chat/${a.name}`; } }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }),
      ...agentLabel(a.display_name)))) : null;
  const modelLine = h('div', { class: 'model-line down', role: 'status', hidden: agent.model_up !== false },
    'The AI computer is off: your message waits in the room, and the agent answers when it is back on.');
  const form = h('form', { class: 'composer', onsubmit: (e) => { e.preventDefault(); sendText(input.value); } },
    h('label', { for: 'chat-input', class: 'sr' }, 'Message'), input, send);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(input.value); } });

  root.replaceChildren(
    h('section', { class: 'chat', 'aria-label': 'Conversation' },
      h('header', { class: 'chat-head' },
        crumb, h('h1', { class: tabs ? null : 'agent-solo' }, threadId ? convTitle : agent.display_name), threadId ? null : tabs,
        h('span', { class: 'mx-same', title: 'The same conversation as your Element X room' }, 'Same as Element X'),
        sideBits.viewMenu || null, sideBits.convBtn || null),
      chipsRow, modelLine, log, thinkingRow, note, form),
    sideBits.side, sideBits.sideScrim || null, sideBits.reopen || null);

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
  await readNames();
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
    } }, h('summary', {}, 'Thinking'), box);
    return d;
  }

  function bubbleFor(ev, edits, reactions, inMain) {
    const mine = ev.getSender() === me;
    const content = edits.get(ev.getId()) || ev.getContent();
    const kind = content.msgtype;
    const thread = inMain ? replyMap.get(ev.getId()) : null;
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
    const rx = reactions.get(ev.getId());
    return h('div', { class: `mx-turn ${mine ? 'me' : 'agent'}` },
      h('div', { class: `msg ${mine ? 'user' : 'assistant'}` }, ...body),
      rx ? h('div', { class: 'mx-reactions' }, [...rx].map(([k, n]) => h('span', { class: 'chip' }, `${k} ${n > 1 ? n : ''}`))) : null,
      h('span', { class: 'when mx-meta' }, [when(ev.getTs() / 1000), mine ? fromWhere(ev) : '', edits.has(ev.getId()) ? 'edited' : '']
        .filter(Boolean).join(' · ')),
      !mine && kind === 'm.text' && content.body ? thinking(String(content.body)) : null,
      thread ? h('a', { class: 'mx-thread-link', href: `${main}/thread/${encodeURIComponent(ev.getId())}` },
        `↳ ${thread.count} repl${thread.count === 1 ? 'y' : 'ies'} · open this conversation`) : null);
  }

  // ── the conversations: Main, then threads (newest first, those quiet for 30 days left out) ──────
  function threadsNow() {
    if (!room) return [];
    const since = Date.now() - THREAD_DAYS * 86400000;
    return [...replyMap].map(([id, r]) => ({ id, title: titleOf(room.findEventById(id)), last: r.last }))
      .filter((t) => t.last >= since).sort((a, b) => b.last - a.last);
  }
  const convList = h('div', { class: 'convs' });
  function drawLists() {
    const list = threadsNow();
    const link = (href, title, last, on) => h('a', { href, class: 'conv-link', 'aria-current': on ? 'true' : null },
      h('span', { class: 'conv-title' }, title), h('span', { class: 'when' }, last ? when(last / 1000) : ''));
    convList.replaceChildren(link(main, 'Main conversation', 0, !threadId),
      ...list.map((t) => link(`${main}/thread/${encodeURIComponent(t.id)}`, t.title, t.last, t.id === threadId)));
    const chip = (href, label, on, extra) => h('a', { href, class: `mx-chip${on ? ' on' : ''}${extra || ''}`, role: 'tab', 'aria-selected': String(!!on) }, label);
    chipsRow.replaceChildren(chip(`${main}/thread/new`, '+', isNew, ' add'), chip(main, 'Main', !threadId),
      ...list.map((t) => chip(`${main}/thread/${encodeURIComponent(t.id)}`, t.title, t.id === threadId)));
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
    for (const e of room.getLiveTimeline().getEvents()) {
      const r = e.threadRootId;
      if (r && r !== e.getId() && e.getType() === 'm.room.message') n.set(r, { count: (n.get(r)?.count || 0) + 1, last: e.getTs() });
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
      if (ev.getType() === 'm.reaction' && rel?.rel_type === 'm.annotation') {
        const map = reactions.get(rel.event_id) || new Map(); map.set(rel.key, (map.get(rel.key) || 0) + 1); reactions.set(rel.event_id, map);
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
    log.replaceChildren(...(threadId ? [] : [older]), ...(shown.length ? shown.map((ev) => bubbleFor(ev, edits, reactions, inMain)) : [none('Nothing said here yet.')]));
    // Still waiting for the agent when the newest message is yours.
    const last = shown[shown.length - 1];
    // (a conversation's title notice is not a message waiting for an answer)
    thinkingRow.hidden = !(last && last.getSender() === me && last.getContent().msgtype !== 'm.notice') && !typing;
    if (stick || atBottom) log.scrollTop = log.scrollHeight;
  }

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
    input.value = '';
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
    } catch (e) { note.textContent = `Not sent: ${e.message}. It is kept above; send it again.`; input.value = text; }
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
  input.focus();
  return () => { alive = false; clearTimeout(timer); for (const f of off) f(); };
}
