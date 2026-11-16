// Chat screen: pick an agent, talk to it, see what it did, reopen earlier conversations.
import { h, api, getJSON, when, markdown } from './util.js';

const newId = () => Math.random().toString(36).slice(2, 12);

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

export async function chatView(root, [agentArg, chatArg], state) {
  let alive = true;
  let agents = [];
  try { agents = (await getJSON('/api/v2/agents')).agents; } catch (e) { /* shown below */ }
  if (!agents.length) {
    root.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, 'No agents yet'),
      h('p', {}, 'No agent is set up for your account. On the server, an agent is created with scripts/logos-agent up <name>.')));
    return () => {};
  }
  const agent = agents.find((a) => a.name === agentArg) || agents[0];
  let chatId = chatArg || newId();
  let readOnly = false;
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
  const emptyState = () => log.replaceChildren(h('div', { class: 'empty' },
    h('p', {}, `Say something to ${agent.display_name}. It remembers what matters in its brain, across web and Telegram.`)));

  let waiting = false;           // true while waiting for a reply the stream did not deliver
  function setBusy(busy) {
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
    input.focus();
  }

  async function loadConversations() {
    try {
      const { conversations } = await getJSON(`/api/v2/agents/${agent.name}/conversations`);
      if (!alive) return;
      convs.replaceChildren(...conversations.map((c) => h('button', {
        type: 'button', 'aria-current': String(c.chat_id === chatId || c.id === chatId),
        onclick: () => { location.hash = `#/chat/${agent.name}/${c.chat_id || c.id}`; },
      }, c.title, h('span', { class: 'when' }, `${when(c.last_active)} · ${c.where === 'web' ? 'web' : 'Telegram'}`))));
      if (!conversations.length) convs.replaceChildren(h('span', { class: 'when' }, 'Nothing yet.'));
      return conversations;
    } catch (e) {
      convs.replaceChildren(h('span', { class: 'when' }, 'Could not load earlier conversations.'));
      return [];
    }
  }

  async function openConversation(conversations) {
    const c = conversations.find((x) => x.chat_id === chatId || x.id === chatId);
    if (!c) { emptyState(); return; }
    readOnly = c.where !== 'web';
    try {
      const { messages } = await getJSON(`/api/v2/agents/${agent.name}/conversations/${encodeURIComponent(c.id)}`);
      if (!alive) return;
      log.replaceChildren();
      for (const m of messages) {
        if (m.text) bubble(m.role, m.text);
        chips(m.tools.map((t) => `Used ${t}`));
      }
      if (readOnly) log.append(h('div', { class: 'notice' }, 'This conversation happened on Telegram. Continue it there, or start a new one here.'));
      scroll();
    } catch (e) {
      log.replaceChildren(h('div', { class: 'msg error' }, 'Could not load this conversation.'));
    }
    setBusy(false);
  }

  async function sendMessage(text) {
    if (log.querySelector('.empty')) log.replaceChildren();
    bubble('user', text);
    const reply = h('div', { class: 'msg assistant' }, h('span', { class: 'when' }, 'Thinking…'));
    log.append(reply);
    const used = h('div', { class: 'chips' });
    log.append(used);
    scroll();
    setBusy(true);
    let body = '', failed = null, lost = false, finished = false;
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
          else if (d.type === 'token') { body += d.content; reply.replaceChildren(...markdown(body.replace(/^\s+/, ''))); }
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
          } else if (d.type === 'error') failed = d.error_title || d.content || 'Something went wrong';
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
    if (failed) {
      if (!body) reply.remove();
      log.append(h('div', { class: 'msg error' }, failed));
    } else if (!body) {
      reply.replaceChildren(h('span', { class: 'when' }, '(no reply)'));
    }
    running = null;
    if (!alive) return;
    setBusy(false);
    input.focus();
    scroll();
    if (location.hash !== `#/chat/${agent.name}/${chatId}`) history.replaceState(null, '', `#/chat/${agent.name}/${chatId}`);
    loadConversations();
  }

  // Microphone: record, turn the recording into text at the voice service, and put the words
  // in the message box. Nothing is sent to the agent until the person presses Send.
  const micLabel = (text) => mic.replaceChildren(h('span', { class: 'mic-dot', 'aria-hidden': 'true' }), text);
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
    input.focus();
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
      onclick: () => { location.hash = `#/chat/${a.name}`; } }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), a.display_name))) : null;

  root.replaceChildren(
    h('section', { class: 'chat', 'aria-label': 'Conversation' },
      h('header', { class: 'chat-head' },
        h('h1', {}, agent.display_name), tabs,
        h('span', { class: `status ${agent.running ? 'up' : ''}` }, agent.running ? 'Running' : 'Not running'),
        h('span', { class: 'status' }, agent.shared ? 'Shared with the household' : 'Private to you')),
      log, micNote, form),
    h('aside', { class: 'side', 'aria-label': 'Conversations' },
      h('button', { type: 'button', class: 'btn quiet', onclick: () => { location.hash = `#/chat/${agent.name}/${newId()}`; } }, 'New conversation'),
      h('h2', { class: 'label' }, 'Earlier conversations'), convs));

  emptyState();
  if (!agent.running) log.replaceChildren(h('div', { class: 'notice' }, `${agent.display_name} is not running. Start it on the server with: scripts/logos-agent up ${agent.name}`));
  setBusy(false);
  const conversations = await loadConversations();
  if (chatArg && conversations) await openConversation(conversations);
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
  input.focus();
  return () => { alive = false; stopRecording(); };
}
