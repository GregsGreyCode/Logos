// Chat screen: pick an agent, talk to it, see what it did, reopen earlier conversations.
import { h, api, getJSON, when, markdown, working, agentLabel, icon, none, focusIfKeyboard } from './util.js';
import { getPref, setPref } from './prefs.js';
import * as mx from './mx.js';
import { matrixChat } from './chat-mx.js';
import { voidView } from './agents-void.js';
import { takeAsk } from './home.js';

const newId = () => Math.random().toString(36).slice(2, 12);

// Per-browser view settings: show the agent's reasoning and tool calls; list scheduled jobs too.
const WHERE = { web: 'web', matrix: 'Matrix', telegram: 'Telegram', scheduled: 'Scheduled job' };
const READ_ONLY_NOTE = {
  matrix: 'This conversation carries on in your chat with the agent.',
  telegram: 'This conversation happened on Telegram, which is no longer used. Start a new one here.',
  scheduled: 'This is a scheduled job the agent ran on its own. It is shown to see what it did.',
};

// The three-line "list" icon, drawn as SVG (it needs the SVG namespace, not h()).
function listIcon() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', 'M4 6h16M4 12h16M4 18h10');
  svg.append(path);
  return svg;
}

// A tool's result as a person reads it: tools answer in JSON, often one text field ("content",
// "output") holding a whole file or command output with its newlines escaped. Show that text.
export function readable(result) {
  if (result == null) return '(no result recorded)';
  try {
    const v = JSON.parse(result);
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const key = ['content', 'output', 'stdout', 'text', 'result'].find((k) => typeof v[k] === 'string');
      if (key) {
        const rest = Object.keys(v).filter((k) => k !== key && v[k] !== null && v[k] !== '' && v[k] !== 0);
        return v[key] + (rest.length ? `\n\n${JSON.stringify(Object.fromEntries(rest.map((k) => [k, v[k]])))}` : '');
      }
      return JSON.stringify(v, null, 2);
    }
  } catch (e) { /* not JSON (or cut short): show as it is */ }
  return result;
}

// One model call's reasoning and tool calls, folded away under a one-line summary.
export function stepsView(steps) {
  if (!steps || !steps.length) return null;
  const tools = steps.flatMap((s) => s.calls.map((c) => c.name));
  const thought = steps.some((s) => s.reasoning);
  const summary = [thought ? 'Thinking' : null, tools.length ? `${tools.length} tool call${tools.length > 1 ? 's' : ''}: ${[...new Set(tools)].join(', ')}` : null]
    .filter(Boolean).join(' · ');
  return h('details', { class: 'steps' }, h('summary', {}, summary),
    steps.map((s) => h('div', { class: 'step' },
      s.reasoning ? h('p', { class: 'reasoning' }, s.reasoning) : null,
      s.calls.map((c) => h('div', { class: 'call' },
        h('div', { class: 'call-name' }, c.name),
        c.args ? h('pre', { class: 'call-args' }, c.args) : null,
        h('pre', { class: 'call-result' }, readable(c.result)))))));
}

// A message whose reply has not been seen yet, kept per agent in this browser. If the
// connection drops or the page is reloaded mid-reply, the agent carries on regardless, and
// this is how the chat knows to wait for the reply and fetch it from the agent's history.
const WAIT_FOR_REPLY_MS = 15 * 60 * 1000;
const pendingKey = (agent) => `logos-pending-${agent}`;
function getPending(agent) {
  try {
    const p = JSON.parse(localStorage.getItem(pendingKey(agent)) || 'null');
    return p && Date.now() - p.at < WAIT_FOR_REPLY_MS ? p : null;
  } catch (e) { return null; }
}
function setPending(agent, value) {
  try {
    if (value) localStorage.setItem(pendingKey(agent), JSON.stringify(value));
    else localStorage.removeItem(pendingKey(agent));
  } catch (e) { /* private window: recovery after a reload is unavailable */ }
}

// True once the history holds the agent's reply to `text`: an assistant message with
// words in it, after the last time the person said `text`, as the newest message.
export function replyArrived(messages, text) {
  let asked = -1;
  messages.forEach((m, i) => { if (m.role === 'user' && (m.text || '').trim() === text) asked = i; });
  if (asked < 0 || asked === messages.length - 1) return false;
  const last = messages[messages.length - 1];
  return last.role === 'assistant' && Boolean((last.text || '').trim());
}

let agentsKept = null, othersKept = [];
// Home warms these in the background (Greg, 21 Nov), so the swipe to Agents draws at once: the agent list,
// and this browser's Matrix connection when it has one.
export function warmAgents() {
  if (!agentsKept) getJSON('/api/v2/agents').then((d) => { agentsKept = d.agents; othersKept = d.others || []; }).catch(() => {});
  if (mx.saved()) mx.get().catch(() => {});
}

export async function chatView(root, [agentArg, chatArg, threadArg], state) {
  let alive = true;
  // The agent list is kept from the last visit, so switching between chats draws at once; a fresh
  // copy is fetched in the background for next time (Greg, 21 Nov: switching lagged on a phone).
  let agents = agentsKept;
  const fresh = getJSON('/api/v2/agents').then((d) => { agentsKept = d.agents; othersKept = d.others || []; return d.agents; }).catch(() => null);
  if (!agents) agents = (await fresh) || [];
  if (!agents.length) {
    root.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, 'No agents yet'),
      h('p', {}, 'No agent is set up for your account yet. Ask Greg to set one up.')));
    return () => {};
  }
  // M4: with this browser connected, Chat opens on the floating agents; pick one to talk to it.
  if (!agentArg && mx.saved() && agents.some((a) => a.matrix_id)) {
    // Everyone's agents float here (Greg, 21 Nov); those you cannot talk to are dimmed, with a padlock.
    // Only the agents you can talk to (Greg, 22 Nov): other people's private agents are not shown.
    return voidView(root, agents.filter((a) => a.matrix_id), []);
  }
  // F37: a notification opens #/chat/room/<room id>; find whose chat or which group that room is.
  if (agentArg === 'room' && chatArg) {
    const roomId = decodeURIComponent(chatArg);
    const c = mx.saved() ? await mx.get().catch(() => null) : null;
    const room = c && c.getRoom(roomId);
    const all = [...agents, ...(othersKept || [])].filter((a) => a.matrix_id);
    const inRoom = room ? agents.filter((a) => a.matrix_id && ['join', 'invite'].includes(room.getMember(a.matrix_id)?.membership)) : [];
    const people = room ? room.getMembers().filter((m) => m.membership === 'join' && !all.some((a) => a.matrix_id === m.userId)) : [];
    const to = !inRoom.length ? '#/chat' : people.length > 1
      ? `#/chat/${inRoom[0].name}/group/${encodeURIComponent(roomId)}` : `#/chat/${inRoom[0].name}`;
    location.replace(to);
    return () => {};
  }
  const agent = agents.find((a) => a.name === agentArg) || agents[0];
  let chatId = chatArg || newId();
  let readOnly = false;
  // M2: a connected browser chats in the Matrix room; an old web chat opened from the Archive is
  // read-only (the conversation goes on in the room). Until the switch-over (M5), a browser that is
  // not connected keeps the web chat, with a line saying how to connect it.
  const connected = !!(agent.matrix_id && mx.saved());
  // M3: #/chat/<agent>/thread/<root event id> is one conversation (a thread); /thread/new starts one.
  const threadId = chatArg === 'thread' ? decodeURIComponent(threadArg || 'new') : null;
  // F36: #/chat/<agent>/group/<room id> is a group room that agent is in (the Household Group).
  const groupId = chatArg === 'group' && threadArg ? decodeURIComponent(threadArg) : null;
  const matrixMode = connected && (!chatArg || !!threadId || !!groupId);
  // M5 (switch-over): chat with an agent on Matrix happens only in its room. Old web chats stay readable.
  const archived = !!agent.matrix_id && !!chatArg && !threadId && !groupId;
  const connectNote = null;
  if (agent.matrix_id && !connected && !chatArg) {
    const old = h('div', { class: 'convs' });
    root.replaceChildren(h('div', { class: 'page' },
      h('header', {}, h('h1', {}, 'Chat')),
      h('div', { class: 'connect-card' },
        h('h2', {}, 'Connect this browser to chat here'),
        h('p', {}, 'Connect this device to your chats once, with your chat password and recovery key.'),
        h('a', { href: '#/settings/matrix', class: 'btn' }, 'Settings › Chats & devices')),
      h('section', { 'aria-label': 'Old web chats' }, h('h2', { class: 'label' }, 'Old web chats (read only)'), old)));
    getJSON(`/api/v2/agents/${agent.name}/conversations`).then(({ conversations }) => {
      const since = Date.now() / 1000 - 30 * 86400;
      const list = conversations.filter((x) => x.where === 'web' && (x.last_active || 0) >= since);
      // The day once, as a heading over its chats, not on every row (21 Nov).
      const rows = []; let day = null;
      for (const x of list) {
        const d = when(x.last_active).replace(/\s*\d{1,2}:\d{2}.*$/, '').replace(/,$/, '') || when(x.last_active);
        if (d !== day) { day = d; rows.push(h('h3', { class: 'sub-label' }, d)); }
        rows.push(h('a', { href: `#/chat/${agent.name}/${x.chat_id}`, class: 'conv-link' }, h('span', { class: 'conv-title' }, x.title)));
      }
      old.replaceChildren(...(rows.length ? rows : [none('None in the last 30 days.')]));
    }).catch(() => old.replaceChildren(none('Could not list them just now.')));
    return () => {};
  }
  let showThinking = getPref('thinking', false);
  let showJobs = getPref('jobs', false);
  let lastConversations = [];
  let running = null;            // { taskId } while a reply is streaming

  const log = h('div', { class: 'log', role: 'log', 'aria-live': 'polite' });
  const input = h('textarea', { id: 'msg', rows: '1', placeholder: `Message ${agent.display_name}` });
  const send = h('button', { type: 'submit', class: 'btn' }, 'Send');
  const stop = h('button', { type: 'button', class: 'btn quiet', hidden: true }, 'Stop');
  const mic = h('button', { type: 'button', class: 'btn quiet mic', 'aria-pressed': 'false',
    hidden: !agent.voice, title: 'Speak your message' });
  const micNote = h('div', { class: 'mic-note', role: 'status', hidden: true });
  let recording = null;          // { recorder, stream, chunks, timer } while the microphone is on
  const convs = h('div', { class: 'convs' });

  // Shown while the local model server is not answering (LM Studio closed, or that machine off).
  // With a paid fallback the agent still answers while the local model is off.
  const modelStatus = agent.has_backup
    ? h('div', { class: 'model-line', role: 'status', hidden: true }, 'The AI computer is off: a backup model is replying')
    : h('div', { class: 'model-line down', role: 'status', hidden: true },
      'The AI computer is off: no replies until it\'s back on.');
  const modelOff = (off) => { modelStatus.hidden = !off; };
  modelOff(agent.model_up === false);
  const scroll = () => { log.scrollTop = log.scrollHeight; };
  const bubble = (role, text) => {
    const el = h('div', { class: `msg ${role}` });
    if (role === 'assistant') el.append(...markdown(text)); else el.textContent = text;
    log.append(el);
    return el;
  };
  const chips = (items) => {
    if (!items.length) return null;
    const el = h('div', { class: 'chips' }, items.map((t) => h('span', { class: 'chip' }, t)));
    log.append(el);
    return el;
  };
  // An empty conversation: no words, just the Logos mark breathing slowly, as if the agent is asleep.
  // It fades back when the first message goes, and a small mark fades in beside the agent's reply.
  const emptyState = () => { mark.remove(); log.replaceChildren(h('div', { class: 'empty sleeping' },
    h('img', { class: 'sleep-mark', src: '/static/logo.svg', alt: '' }),
    h('p', { class: 'visually-hidden' }, `Say something to ${agent.display_name}. It remembers what matters in its brain, across web and Matrix.`))); };

  // The small mark beside the agent's newest reply: it cycles its colours while the agent thinks and
  // settles when the reply is done. One mark, which moves to each new reply.
  const mark = h('img', { class: 'reply-mark', src: '/static/logo.svg', alt: '' });
  let marked = null;
  function placeMark(el, busy) {
    marked = el || null;
    if (!el) { mark.remove(); return; }
    if (mark.parentElement !== log) { log.append(mark); mark.classList.remove('in'); void mark.offsetWidth; mark.classList.add('in'); }
    mark.classList.toggle('busy', !!busy);
    mark.style.top = `${el.offsetTop + 4}px`;
    mark.style.left = `${el.offsetLeft - 30}px`;
  }
  new ResizeObserver(() => { if (marked && marked.isConnected) placeMark(marked, mark.classList.contains('busy')); }).observe(log);

  let waiting = false;           // true while waiting for a reply the stream did not deliver
  function setBusy(busy) {
    working('chat', busy);
    send.disabled = busy || waiting || readOnly || !agent.running;
    input.disabled = readOnly || !agent.running;
    mic.disabled = readOnly || !agent.running;
    stop.hidden = !busy || waiting;
  }

  // The stream was lost (or the page reloaded) while the agent was still working.
  // Look in the agent's own history until the reply is there, then show the conversation.
  async function waitForReply(text) {
    waiting = true;
    setBusy(true);
    const note = h('div', { class: 'notice' }, `The connection dropped, but ${agent.display_name} is still working. The reply will appear here.`);
    log.append(note);
    scroll();
    const deadline = Date.now() + WAIT_FOR_REPLY_MS;
    let seen = -1, found = false;
    while (alive && Date.now() < deadline && !found) {
      await new Promise((r) => setTimeout(r, 4000));
      if (!alive) return;
      try {
        const conversations = await loadConversations();
        const c = (conversations || []).find((x) => x.chat_id === chatId || x.id === chatId);
        if (!c) continue;
        const { messages } = await getJSON(`/api/v2/agents/${agent.name}/conversations/${encodeURIComponent(c.id)}`);
        // Two looks in a row with the same length: the agent has finished, not paused mid-task.
        if (replyArrived(messages, text) && messages.length === seen) { found = true; await openConversation(conversations); }
        seen = messages.length;
      } catch (e) { /* the connection may still be down; keep trying */ }
    }
    if (!alive) return;
    waiting = false;
    setPending(agent.name, null);
    if (!found) note.textContent = `No reply was found. Check "Earlier conversations" in a moment, or send the message again.`;
    setBusy(false);
    focusIfKeyboard(input);
  }

  async function loadConversations() {
    try {
      const { conversations } = await getJSON(`/api/v2/agents/${agent.name}/conversations`);
      if (!alive) return;
      lastConversations = conversations;
      const shown = conversations.filter((c) => showJobs || c.where !== 'scheduled' || c.id === chatId);
      convs.replaceChildren(...shown.map((c) => h('button', {
        type: 'button', 'aria-current': String(c.chat_id === chatId || c.id === chatId),
        onclick: () => { closeSide(false); location.hash = `#/chat/${agent.name}/${c.chat_id || c.id}`; },
        title: `${c.title} · ${WHERE[c.where] || c.where}`,
      }, h('span', { class: 'conv-title' }, c.title), h('span', { class: 'when' }, when(c.last_active)))));
      if (!shown.length) convs.replaceChildren(none('Nothing yet.'));
      return conversations;
    } catch (e) {
      convs.replaceChildren(h('span', { class: 'when' }, 'Could not load earlier conversations.'));
      return [];
    }
  }

  async function openConversation(conversations) {
    const c = conversations.find((x) => x.chat_id === chatId || x.id === chatId);
    if (!c) { emptyState(); return; }
    readOnly = c.where !== 'web' || archived;
    try {
      const { messages } = await getJSON(`/api/v2/agents/${agent.name}/conversations/${encodeURIComponent(c.id)}`);
      if (!alive) return;
      log.replaceChildren();
      let lastReply = null;
      for (const m of messages) {
        if (showThinking && m.role === 'assistant') { const s = stepsView(m.steps); if (s) log.append(s); }
        if (m.text) { const b = bubble(m.role, m.text); if (m.role === 'assistant') lastReply = b; }
        if (!showThinking) chips(m.tools.map((t) => `Used ${t}`));
      }
      placeMark(lastReply, false);
      if (readOnly) log.append(h('div', { class: 'notice' }, archived && c.where === 'web'
        ? h('span', {}, 'An old web chat, kept for reference. ', h('a', { href: `#/chat/${agent.name}` }, 'Continue in the main conversation'), '.')
        : c.where === 'matrix' ? h('span', {}, `This conversation carries on in your chat with ${agent.display_name}. `,
          h('a', { href: `#/chat/${agent.name}`, class: 'btn small' }, 'Open the chat'))
          : (READ_ONLY_NOTE[c.where] || 'This conversation can only be read here.')));
      scroll();
    } catch (e) {
      log.replaceChildren(h('div', { class: 'msg error' }, 'Could not load this conversation.'));
    }
    setBusy(false);
  }

  // Story D2: a message the model could not take (LM Studio offline) waits here and is sent by
  // itself when LM Studio is back, as the agents do in Element X. It waits only while this page
  // is open; "Cancel" puts the words back in the box instead.
  async function sendWhenBack(text, reply, used) {
    reply.remove(); used.remove();
    const mine = log.lastElementChild;
    const cancel = h('button', { type: 'button', class: 'btn small' }, 'Cancel');
    const note = h('div', { class: 'msg waiting', role: 'status' },
      h('span', {}, 'The AI computer is off. This message will be sent when it\'s back on (keep this page open). '), cancel);
    log.append(note);
    setBusy(false);
    scroll();
    let cancelled = false;
    cancel.addEventListener('click', () => {
      cancelled = true;
      note.remove();
      if (mine && mine.classList.contains('user')) mine.remove();
      if (!input.value.trim()) { input.value = text; input.dispatchEvent(new Event('input')); }
    });
    while (alive && !cancelled) {
      await new Promise((ok) => setTimeout(ok, 30000));
      if (!alive || cancelled) return;
      const r = await api(`/api/v2/model?agent=${encodeURIComponent(agent.name)}`).catch(() => null);
      const s = r && r.ok ? await r.json().catch(() => ({})) : {};
      if (s.model_up === true) {
        note.remove();
        if (mine && mine.classList.contains('user')) mine.remove();
        modelOff(false);
        return sendMessage(text);
      }
    }
  }

  async function sendMessage(text) {
    const sleeping = log.querySelector('.empty.sleeping');
    if (sleeping) {                       // the sleeping mark fades back, then the conversation starts
      sleeping.classList.add('waking');
      await new Promise((ok) => setTimeout(ok, window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 450));
    }
    if (log.querySelector('.empty')) log.replaceChildren();
    bubble('user', text);
    const reply = h('div', { class: 'msg assistant' }, h('span', { class: 'when' }, 'Thinking…'));
    log.append(reply);
    placeMark(reply, true);
    const used = h('div', { class: 'chips' });
    log.append(used);
    scroll();
    setBusy(true);
    let body = '', failed = null, lost = false, finished = false, unsent = false;
    const tools = {};
    setPending(agent.name, { chatId, text, at: Date.now() });
    try {
      const r = await api('/chat', { method: 'POST', body: { agent_id: agent.id, session_id: chatId, message: text } });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        failed = e.message || e.error || `HTTP ${r.status}`;
        throw new Error(failed);
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data:')) continue;
          let d;
          try { d = JSON.parse(line.slice(5)); } catch (e) { continue; }
          if (d.type === 'task_started') running = { taskId: d.task_id };
          else if (d.type === 'status' && !body) reply.replaceChildren(h('span', { class: 'when' }, d.content));
          else if (d.type === 'token') { if (!agent.has_backup) modelOff(false); body += d.content; reply.replaceChildren(...markdown(body.replace(/^\s+/, ''))); }
          else if (d.type === 'message') { body = d.content; reply.replaceChildren(...markdown(body)); }
          else if (d.type === 'tool_start') {
            tools[d.call_id] = h('span', { class: 'chip', title: d.preview || '' }, `Using ${d.tool}…`);
            used.append(tools[d.call_id]);
          } else if (d.type === 'tool_end' && tools[d.call_id]) {
            const name = tools[d.call_id].textContent.replace(/^Using /, '').replace(/…$/, '');
            tools[d.call_id].textContent = d.success ? `Used ${name}` : `${name} failed`;
            if (!d.success) tools[d.call_id].classList.add('failed');
          } else if (d.type === 'done') {
            finished = true;
            used.append(h('span', {}, d.stopped ? 'Stopped' : `${d.elapsed_s} s`));
          } else if (d.type === 'error') {
            failed = d.error_title || d.content || 'Something went wrong';
            // Never handed to the agent: give the words back so they need not be typed again.
            if (d.error_type === 'model_off') { unsent = true; modelOff(true); }
          }
          scroll();
        }
      }
      // A stream that ends without "done" or an error was cut off on the way here.
      if (!finished && !failed) lost = true;
    } catch (e) {
      if (!failed) lost = true;    // the request or the stream broke; the agent may still be working
    }
    running = null;
    if (lost && !failed) {
      if (!alive) return;
      reply.remove(); used.remove();
      if (location.hash !== `#/chat/${agent.name}/${chatId}`) history.replaceState(null, '', `#/chat/${agent.name}/${chatId}`);
      return waitForReply(text);
    }
    setPending(agent.name, null);
    if (unsent) return sendWhenBack(text, reply, used);
    if (failed) {
      if (!body) reply.remove();
      log.append(h('div', { class: 'msg error' }, failed));
    } else if (!body) {
      reply.replaceChildren(h('span', { class: 'when' }, '(no reply)'));
    }
    running = null;
    if (!alive) return;
    placeMark(reply.isConnected && body ? reply : marked, false);
    setBusy(false);
    focusIfKeyboard(input);
    scroll();
    if (location.hash !== `#/chat/${agent.name}/${chatId}`) history.replaceState(null, '', `#/chat/${agent.name}/${chatId}`);
    const conversations = await loadConversations();
    // With thinking shown, redraw from the agent's history so this reply's steps appear too.
    if (showThinking && !failed && alive && conversations) await openConversation(conversations);
  }

  // Microphone: record, turn the recording into text at the voice service, and put the words
  // in the message box. Nothing is sent to the agent until the person presses Send.
  const MIC = ['M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z', 'M5 11a7 7 0 0 0 14 0', 'M12 18v3'];
  const micLabel = (text) => mic.replaceChildren(h('span', { class: 'mic-dot', 'aria-hidden': 'true' }),
    icon(MIC), h('span', { class: 'mic-label' }, text));
  const say = (text) => { micNote.textContent = text || ''; micNote.hidden = !text; };
  micLabel('Speak');
  function stopRecording() {
    if (!recording) return;
    clearInterval(recording.timer);
    if (recording.recorder.state !== 'inactive') recording.recorder.stop();
  }
  async function toWords(blob) {
    mic.disabled = true;
    micLabel('Working…');
    try {
      const r = await api(`/api/v2/agents/${agent.name}/voice`, { method: 'POST', raw: blob,
        headers: { 'Content-Type': blob.type || 'audio/webm' } });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.message || 'That did not work. Try again.');
      if (!alive) return;
      if (!d.text) say('No words were heard in that recording.');
      else {
        input.value = (input.value.trim() ? input.value.trimEnd() + ' ' : '') + d.text;
        input.dispatchEvent(new Event('input'));
        say('');
      }
    } catch (e) {
      say(e.message);
    }
    if (!alive) return;
    micLabel('Speak');
    setBusy(Boolean(running));
    focusIfKeyboard(input);
  }
  mic.addEventListener('click', async () => {
    if (recording) return stopRecording();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      return say(window.isSecureContext ? 'This browser cannot record audio.'
        : 'The microphone only works on the https address of this dashboard.');
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) {
      return say('The microphone is blocked. Allow it for this site in the browser, then try again.');
    }
    const recorder = new MediaRecorder(stream);
    const chunks = [];
    const started = Date.now();
    recorder.addEventListener('dataavailable', (e) => { if (e.data.size) chunks.push(e.data); });
    recorder.addEventListener('stop', () => {
      stream.getTracks().forEach((t) => t.stop());
      recording = null;
      mic.setAttribute('aria-pressed', 'false');
      if (alive && chunks.length) toWords(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }));
      else micLabel('Speak');
    });
    recorder.start();
    mic.setAttribute('aria-pressed', 'true');
    say('');
    const tick = () => {
      const s = Math.floor((Date.now() - started) / 1000);
      micLabel(`Stop ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`);
      if (s >= 300) stopRecording();          // five minutes is plenty for one message
    };
    tick();
    recording = { recorder, stream, chunks, timer: setInterval(tick, 500) };
  });

  const form = h('form', { class: 'composer', onsubmit: (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || running || send.disabled) return;
    input.value = '';
    input.style.height = 'auto';
    sendMessage(text);
  } }, h('label', { for: 'msg', class: 'sr' }, 'Message'), input, mic, stop, send);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 200)}px`; });
  stop.addEventListener('click', () => {
    if (running && running.taskId) api(`/chat/${encodeURIComponent(running.taskId)}/cancel`, { method: 'POST' }).catch(() => {});
  });

  const tabs = agents.length > 1 ? h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Agent' },
    agents.map((a) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(a.name === agent.name),
      onclick: () => { location.hash = `#/chat/${a.name}`; } }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }),
      ...agentLabel(a.display_name)))) : null;

  // Phone: earlier conversations and the view switches open as a panel from the bottom.
  const side = h('aside', { class: 'side', id: 'chat-side', 'aria-label': 'Chats' });
  // Desktop: the conversations panel starts open; a small button tucks it away and a tab on the right
  // edge brings it back. Remembered on this device. (On a phone it is the Conversations panel instead.)
  // A labelled button in the header (Greg, 23 Nov: the thin tab at the screen's edge went unnoticed).
  // Chats toggles the panel and stays where it is, lit while the panel is open (Greg, 23 Nov: it vanished
  // when the panel opened).
  const reopen = h('button', { type: 'button', class: 'btn quiet small side-reopen', title: 'Show or hide your chats', 'aria-label': 'Show or hide your chats',
    'aria-controls': 'chat-side', 'aria-pressed': 'true', onclick: () => tuck(!side.classList.contains('tucked')) }, listIcon());   // "Chats", not Conversations (Greg, 23 Nov)
  // It shows for a moment when Chat opens, then slides away to its tab, so you know where it is.
  // Reopen it and it stays (remembered) until you hide it again.
  const wide = () => window.matchMedia('(min-width: 600px)').matches;
  const still = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function slideAway(focusTab) {
    if (side.classList.contains('tucked')) return;
    const done = () => { side.classList.remove('sliding'); side.style.marginRight = ''; side.classList.add('tucked');
      reopen.classList.add('shown'); reopen.setAttribute('aria-pressed', 'false'); if (focusTab) reopen.focus(); };
    if (!wide() || still()) return done();
    side.style.marginRight = `-${side.offsetWidth}px`;
    side.classList.add('sliding');
    setTimeout(done, 480);
  }
  const tuck = (hide) => {
    setPref('panel', hide ? 'slide' : 'open');
    if (hide) return slideAway(true);
    side.classList.remove('tucked'); reopen.classList.remove('shown'); reopen.setAttribute('aria-pressed', 'true');
  };
  if (getPref('panel', 'slide') !== 'open') setTimeout(() => { if (alive && side.isConnected && !side.contains(document.activeElement)) slideAway(false); }, 1200);
  const sideScrim = h('div', { class: 'chat-scrim', hidden: true });
  const convBtn = h('button', { type: 'button', class: 'conv-btn', 'aria-label': 'Chats and view settings',
    'aria-controls': 'chat-side', 'aria-expanded': 'false' }, listIcon());
  const closeSide = (refocus = true) => {
    if (!side.classList.contains('open')) return;
    side.classList.remove('open'); sideScrim.hidden = true;
    convBtn.setAttribute('aria-expanded', 'false');
    if (refocus) convBtn.focus();
  };
  convBtn.addEventListener('click', () => {
    if (side.classList.contains('open')) return closeSide();
    side.classList.add('open'); sideScrim.hidden = false;
    convBtn.setAttribute('aria-expanded', 'true');
    const first = side.querySelector('button'); if (first) first.focus();
  });
  sideScrim.addEventListener('click', () => closeSide());
  const onKey = (e) => { if (e.key === 'Escape') closeSide(); };
  document.addEventListener('keydown', onKey);

  const thinkingBox = h('input', { type: 'checkbox', id: 'show-thinking', checked: showThinking,
    onchange: async () => {
      showThinking = thinkingBox.checked;
      setPref('thinking', showThinking);
      if (lastConversations.some((x) => x.chat_id === chatId || x.id === chatId)) await openConversation(lastConversations);
    } });
  const jobsBox = h('input', { type: 'checkbox', id: 'show-jobs', checked: showJobs ? '' : null,
    onchange: () => { showJobs = jobsBox.checked; setPref('jobs', showJobs); loadConversations(); } });
  // P5: on a desktop these live in a small View menu in the header (the panel keeps them on a phone).
  const mirror = (box, label) => {
    const m = h('input', { type: 'checkbox', checked: box.checked ? '' : null,
      onchange: () => { box.checked = m.checked; box.dispatchEvent(new Event('change')); } });
    box.addEventListener('change', () => { m.checked = box.checked; });
    return h('label', { class: 'view-item' }, m, label);
  };
  const panelPick = (value, label) => h('label', { class: 'view-item' },
    h('input', { type: 'radio', name: 'panel-mode', checked: getPref('panel', 'slide') === value ? '' : null, onchange: () => {
      setPref('panel', value);
      if (value === 'open' && side.classList.contains('tucked')) { side.classList.remove('tucked'); reopen.classList.remove('shown'); }
    } }), label);
  const viewMenu = h('details', { class: 'view-menu wide-only' },
    h('summary', { class: 'btn quiet small' }, 'View'),
    h('div', { class: 'view-pop', role: 'group', 'aria-label': 'View options' },
      mirror(thinkingBox, 'Show thinking'), mirror(jobsBox, 'Show scheduled jobs'),
      h('p', { class: 'label' }, 'Chats panel'),
      panelPick('slide', 'Slides away'), panelPick('open', 'Stays open')));
  document.addEventListener('click', (e) => { if (viewMenu.open && !viewMenu.contains(e.target)) viewMenu.open = false; });

  // M2: with this browser connected as your Matrix device, chat is your Element room itself.
  if (matrixMode) {
    // No View menu (Greg, 23 Nov: it made no sense here): whether the panel stays open is in Settings › Look,
    // and Chats in the header shows or hides it.
    const sideTop = h('div', { class: 'side-top' });
    const done = await matrixChat(root, agent, agents, state, { side, sideScrim, reopen, convBtn, sideTop, viewMenu: null, others: othersKept || [] }, threadId, groupId);
    return () => { alive = false; done(); document.removeEventListener('keydown', onKey); };
  }

  root.replaceChildren(
    h('section', { class: 'chat', 'aria-label': 'Conversation' },
      h('header', { class: 'chat-head' },
        h('h1', { class: tabs ? null : 'agent-solo' }, agent.display_name), tabs,
        h('span', { class: `status running ${agent.running ? 'up' : ''}` }, agent.running ? 'Running' : 'Not running'),
        agent.shared ? h('span', { class: 'status scope' }, 'Shared with the household') : null,
        reopen, viewMenu, convBtn),
      connectNote, modelStatus, log, micNote, form),
    side, sideScrim);
  side.append(
    h('div', { class: 'side-top' }, h('button', { type: 'button', class: 'side-hide', 'aria-label': 'Hide conversations',
      title: 'Hide conversations', 'aria-controls': 'chat-side', onclick: () => tuck(true) }, icon(['M9 6l6 6-6 6']))),
    h('button', { type: 'button', class: 'btn', onclick: () => { closeSide(false); location.hash = `#/chat/${agent.name}/${newId()}`; } }, 'New conversation'),
    h('label', { class: 'toggle phone-toggle', for: 'show-thinking',
      title: "Show the agent's reasoning and each tool it used, with what went in and what came back" }, thinkingBox, ' Show thinking'),
    h('label', { class: 'toggle phone-toggle', for: 'show-jobs' }, jobsBox, ' Show scheduled jobs'),
    h('h2', { class: 'label' }, 'Earlier chats'), convs);

  emptyState();
  if (!agent.running) log.replaceChildren(h('div', { class: 'notice' }, `${agent.display_name} is not running right now, so it can't answer. Greg can start it again.`));
  setBusy(false);
  const conversations = await loadConversations();
  if (chatArg && conversations) await openConversation(conversations);
  // A question asked on Home: send it now, in this new conversation.
  const asked = !chatArg || !conversations.some((x) => x.chat_id === chatId) ? takeAsk(agent.name) : null;
  if (asked && agent.running) { sendMessage(asked); return () => { alive = false; stopRecording(); working('chat', false); document.removeEventListener('keydown', onKey); }; }
  // A reply was still on its way when this page was last open: show the message and wait for it.
  const pending = getPending(agent.name);
  if (pending && pending.chatId === chatId && !readOnly) {
    const asked = [...log.querySelectorAll('.msg.user')].some((el) => el.textContent.trim() === pending.text);
    const answered = asked && log.lastElementChild && log.lastElementChild.classList.contains('assistant');
    if (answered) setPending(agent.name, null);
    else {
      if (!asked) { if (log.querySelector('.empty')) log.replaceChildren(); bubble('user', pending.text); }
      waitForReply(pending.text);
    }
  }
  focusIfKeyboard(input);
  return () => { alive = false; stopRecording(); working('chat', false); document.removeEventListener('keydown', onKey); };
}
