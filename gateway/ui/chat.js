// Chat screen: pick an agent, talk to it, see what it did, reopen earlier conversations.
import { h, api, getJSON, when, markdown } from './util.js';

const newId = () => Math.random().toString(36).slice(2, 12);

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

  function setBusy(busy) {
    send.disabled = busy || readOnly || !agent.running;
    input.disabled = readOnly || !agent.running;
    stop.hidden = !busy;
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
    let body = '', failed = null;
    const tools = {};
    try {
      const r = await api('/chat', { method: 'POST', body: { agent_id: agent.id, session_id: chatId, message: text } });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        throw new Error(e.message || e.error || `HTTP ${r.status}`);
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
            used.append(h('span', {}, d.stopped ? 'Stopped' : `${d.elapsed_s} s`));
          } else if (d.type === 'error') failed = d.error_title || d.content || 'Something went wrong';
          scroll();
        }
      }
    } catch (e) {
      failed = e.message;
    }
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

  const form = h('form', { class: 'composer', onsubmit: (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || running || send.disabled) return;
    input.value = '';
    input.style.height = 'auto';
    sendMessage(text);
  } }, h('label', { for: 'msg', class: 'sr' }, 'Message'), input, stop, send);
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
      log, form),
    h('aside', { class: 'side', 'aria-label': 'Conversations' },
      h('button', { type: 'button', class: 'btn quiet', onclick: () => { location.hash = `#/chat/${agent.name}/${newId()}`; } }, 'New conversation'),
      h('h2', { class: 'label' }, 'Earlier conversations'), convs));

  emptyState();
  if (!agent.running) log.replaceChildren(h('div', { class: 'notice' }, `${agent.display_name} is not running. Start it on the server with: scripts/logos-agent up ${agent.name}`));
  setBusy(false);
  const conversations = await loadConversations();
  if (chatArg && conversations) await openConversation(conversations);
  input.focus();
  return () => { alive = false; };
}
