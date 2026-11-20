// Chat as your Matrix room (story M2): the conversation is your encrypted Element room with the agent,
// read and written by this browser as one of your Matrix devices (mx.js, story M1). The same messages
// appear here and in Element X, live. Under each of the agent's replies, "Thinking" folds out its
// reasoning and tool calls (from the agent's own record, /api/v2/agents/<name>/steps).
// Old web conversations stay readable for 30 days under Archive (Greg, 21 Nov).
import { h, getJSON, markdown, when, none, loading, agentLabel } from './util.js';
import * as mx from './mx.js';
import { stepsView } from './chat.js';
import { takeAsk } from './home.js';

const ARCHIVE_DAYS = 30;

function pickRoom(c, agentId) {
  const rooms = c.getRooms().filter((r) => r.getMyMembership() === 'join'
    && ['join', 'invite'].includes(r.getMember(agentId)?.membership));
  // The direct room with the agent first (two members), else the busiest room it shares with you.
  rooms.sort((a, b) => (a.getJoinedMemberCount() === 2 ? 0 : 1) - (b.getJoinedMemberCount() === 2 ? 0 : 1)
    || b.getLastActiveTimestamp() - a.getLastActiveTimestamp());
  return rooms[0] || null;
}

export async function matrixChat(root, agent, agents, state, sideBits) {
  let alive = true;
  const off = [];
  const log = h('div', { class: 'log', role: 'log', 'aria-live': 'polite' }, loading());
  const input = h('textarea', { id: 'chat-input', rows: '1', placeholder: `Message ${agent.display_name} (also in Element X)` });
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
        h('h1', { class: tabs ? null : 'agent-solo' }, agent.display_name), tabs,
        h('span', { class: 'mx-same', title: 'The same conversation as your Element X room' }, 'Same as Element X'),
        sideBits.viewMenu || null, sideBits.convBtn || null),
      modelLine, log, thinkingRow, note, form),
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

  function bubbleFor(ev, edits, reactions) {
    const mine = ev.getSender() === me;
    const content = edits.get(ev.getId()) || ev.getContent();
    const kind = content.msgtype;
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
      !mine && kind === 'm.text' && content.body ? thinking(String(content.body)) : null);
  }

  function draw(stick) {
    if (!room || !alive) return;
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    const events = room.getLiveTimeline().getEvents();
    const edits = new Map(), reactions = new Map();
    for (const ev of events) {
      const rel = ev.getContent()['m.relates_to'] || ev.getWireContent()?.['m.relates_to'];
      if (ev.getType() === 'm.room.message' && ev.getContent()['m.new_content'] && rel?.rel_type === 'm.replace') edits.set(rel.event_id, ev.getContent()['m.new_content']);
      if (ev.getType() === 'm.reaction' && rel?.rel_type === 'm.annotation') {
        const map = reactions.get(rel.event_id) || new Map(); map.set(rel.key, (map.get(rel.key) || 0) + 1); reactions.set(rel.event_id, map);
      }
    }
    const shown = events.filter((ev) => (ev.getType() === 'm.room.message' || (ev.isEncrypted() && ev.isDecryptionFailure()))
      && !(ev.getContent()['m.relates_to']?.rel_type === 'm.replace') && !ev.isRedacted() && !ev.threadRootId);
    const older = h('button', { type: 'button', class: 'linkish mx-older', onclick: async (e) => {
      e.target.textContent = 'Loading…'; await c.scrollback(room, 30); draw(false);
    } }, 'Earlier messages');
    log.replaceChildren(older, ...(shown.length ? shown.map((ev) => bubbleFor(ev, edits, reactions)) : [none('Nothing said here yet.')]));
    // Still waiting for the agent when the newest message is yours.
    const last = shown[shown.length - 1];
    thinkingRow.hidden = !(last && last.getSender() === me) && !typing;
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
    try { await c.sendMessage(room.roomId, { msgtype: 'm.text', body: text }); }
    catch (e) { note.textContent = `Not sent: ${e.message}. It is kept above; send it again.`; input.value = text; }
    draw(true);
  }

  // Panel: the main conversation (threads arrive with M3), then old web chats for 30 days.
  const archive = h('div', { class: 'convs' });
  sideBits.side.replaceChildren(sideBits.sideTop || '',
    h('h2', { class: 'label' }, 'Conversations'),
    h('div', { class: 'convs' }, h('button', { type: 'button', 'aria-current': 'true' },
      h('span', { class: 'conv-title' }, 'Main conversation'), h('span', { class: 'when' }, 'now'))),
    h('h2', { class: 'label' }, 'Archive (old web chats)'), archive,
    ...(sideBits.toggles || []));
  getJSON(`/api/v2/agents/${agent.name}/conversations`).then(({ conversations }) => {
    const since = Date.now() / 1000 - ARCHIVE_DAYS * 86400;
    const old = conversations.filter((x) => x.where === 'web' && (x.last_active || 0) >= since);
    archive.replaceChildren(...(old.length ? old.map((x) => h('a', { href: `#/chat/${agent.name}/${x.chat_id}`, class: 'conv-link' },
      h('span', { class: 'conv-title' }, x.title), h('span', { class: 'when' }, when(x.last_active))))
      : [none(`None in the last ${ARCHIVE_DAYS} days.`)]));
  }).catch(() => archive.replaceChildren(none('Could not list them just now.')));

  draw(true);
  const asked = takeAsk(agent.name);           // a question asked on Home
  if (asked) sendText(asked);
  input.focus();
  return () => { alive = false; clearTimeout(timer); for (const f of off) f(); };
}
