// Chat screen: pick an agent, talk to it, see what it did, reopen earlier conversations.
import { h, api, getJSON, when, markdown, working, agentLabel, icon } from './util.js';

const newId = () => Math.random().toString(36).slice(2, 12);

// Per-browser view settings: show the agent's reasoning and tool calls; list scheduled jobs too.
const pref = (key) => { try { return localStorage.getItem(`logos-${key}`) === '1'; } catch (e) { return false; } };
const setPref = (key, on) => { try { localStorage.setItem(`logos-${key}`, on ? '1' : '0'); } catch (e) { /* private window */ } };
const WHERE = { web: 'web', matrix: 'Matrix', telegram: 'Telegram', scheduled: 'Scheduled job' };
const READ_ONLY_NOTE = {
  matrix: 'This conversation happened in Element X. Continue it there, or start a new one here.',
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
  let showThinking = pref('show-thinking');
  let showJobs = pref('show-jobs');
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
    ? h('div', { class: 'model-line', role: 'status', hidden: true }, 'Local model off: a backup model replies')
    : h('div', { class: 'model-line down', role: 'status', hidden: true },
      'Model server off: no replies until LM Studio is on.');
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
  const emptyState = () => log.replaceChildren(h('div', { class: 'empty' },
    h('p', {}, `Say something to ${agent.display_name}. It remembers what matters in its brain, across web and Matrix.`)));

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
    input.focus();
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
      }, c.title, h('span', { class: 'when' }, `${when(c.last_active)} · ${WHERE[c.where] || c.where}`))));
      if (!shown.length) convs.replaceChildren(h('span', { class: 'when' }, 'Nothing yet.'));
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
        if (showThinking && m.role === 'assistant') { const s = stepsView(m.steps); if (s) log.append(s); }
        if (m.text) bubble(m.role, m.text);
        if (!showThinking) chips(m.tools.map((t) => `Used ${t}`));
      }
      if (readOnly) log.append(h('div', { class: 'notice' }, READ_ONLY_NOTE[c.where] || 'This conversation can only be read here.'));
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
    if (unsent && !input.value.trim()) { input.value = text; input.dispatchEvent(new Event('input')); }
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
      onclick: () => { location.hash = `#/chat/${a.name}`; } }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }),
      ...agentLabel(a.display_name)))) : null;

  // Phone: earlier conversations and the view switches open as a panel from the bottom.
  const side = h('aside', { class: 'side', id: 'chat-side', 'aria-label': 'Conversations' });
  const sideScrim = h('div', { class: 'chat-scrim', hidden: true });
  const convBtn = h('button', { type: 'button', class: 'conv-btn', 'aria-label': 'Conversations and view settings',
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
      setPref('show-thinking', showThinking);
      if (lastConversations.some((x) => x.chat_id === chatId || x.id === chatId)) await openConversation(lastConversations);
    } });
  const jobsBox = h('input', { type: 'checkbox', id: 'show-jobs', checked: showJobs ? '' : null,
    onchange: () => { showJobs = jobsBox.checked; setPref('show-jobs', showJobs); loadConversations(); } });

  root.replaceChildren(
    h('section', { class: 'chat', 'aria-label': 'Conversation' },
      h('header', { class: 'chat-head' },
        h('h1', { class: tabs ? null : 'agent-solo' }, agent.display_name), tabs,
        h('span', { class: `status running ${agent.running ? 'up' : ''}` }, agent.running ? 'Running' : 'Not running'),
        h('span', { class: 'status scope' }, agent.shared ? 'Shared with the household' : 'Private to you'),
        convBtn),
      modelStatus, log, micNote, form),
    side, sideScrim);
  side.append(
    h('button', { type: 'button', class: 'btn', onclick: () => { closeSide(false); location.hash = `#/chat/${agent.name}/${newId()}`; } }, 'New conversation'),
    h('label', { class: 'toggle', for: 'show-thinking',
      title: "Show the agent's reasoning and each tool it used, with what went in and what came back" }, thinkingBox, ' Show thinking'),
    h('label', { class: 'toggle', for: 'show-jobs' }, jobsBox, ' Show scheduled jobs'),
    h('h2', { class: 'label' }, 'Earlier conversations'), convs);

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
  return () => { alive = false; stopRecording(); working('chat', false); document.removeEventListener('keydown', onKey); };
}
