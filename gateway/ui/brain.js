// Brain screen: browse what an agent has written down, add to it by asking, and edit pages by hand.
import { h, api, getJSON, when, markdown, agentLabel } from './util.js';

const FOLDERS = [['projects', 'Projects'], ['people', 'People'], ['topics', 'Topics'], ['daily', 'Daily'],
  ['inbox', 'Inbox'], ['', 'General']];
const NEW_IN = [['projects', 'Projects'], ['people', 'People'], ['topics', 'Topics']];

// Read the reply text out of the /chat event stream.
async function askAgent(agent, message, onText) {
  const r = await api('/chat', { method: 'POST', body: {
    agent_id: agent.id, session_id: `brain-${Math.random().toString(36).slice(2, 10)}`, message } });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).message || `HTTP ${r.status}`);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '', text = '', failed = null;
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
      if (d.type === 'token') text += d.content;
      else if (d.type === 'message') text = d.content;
      else if (d.type === 'error') failed = d.error_title || d.content || 'Something went wrong';
      onText(text.trim());
    }
  }
  if (failed) throw new Error(failed);
  return text.trim();
}

export async function brainView(root, [agentArg, ...pathParts]) {
  let alive = true;
  let agents = [];
  try { agents = (await getJSON('/api/v2/agents')).agents; } catch (e) { /* shown below */ }
  if (!agents.length) {
    root.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, 'No brain to show'), h('p', {}, 'No agent is set up for your account.')));
    return () => {};
  }
  const agent = agents.find((a) => a.name === agentArg) || agents[0];
  const startEditing = location.hash.endsWith('?edit');
  let current = pathParts.length ? decodeURIComponent(pathParts.join('/')).replace(/\?edit$/, '') : '';
  const base = `/api/v2/agents/${agent.name}/brain`;
  const link = (path) => `#/brain/${agent.name}/${path.split('/').map(encodeURIComponent).join('/')}`;
  // On a phone the brain is edited in Obsidian, on the copy Syncthing keeps there.
  const obsidian = (path) => `obsidian://open?vault=${encodeURIComponent(agent.obsidian_vault || `brain-${agent.name}`)}`
    + `&file=${encodeURIComponent(path.replace(/\.md$/, ''))}`;
  const conflictsBox = h('div', { class: 'conflicts', hidden: true });

  const tree = h('nav', { class: 'tree', 'aria-label': 'Pages' });
  const article = h('article', { class: 'doc' });
  const changes = h('div', { class: 'changes' });
  const results = h('div', { class: 'changes', hidden: true });
  const search = h('input', { id: 'q', type: 'search', placeholder: 'Search pages and notes' });
  let pages = [];

  // ── page list, with an inline "new page" form ──────────────────────────
  function newPageForm() {
    const title = h('input', { id: 'np-title', type: 'text', placeholder: 'Title', maxlength: '80' });
    const folder = h('select', { id: 'np-folder' }, NEW_IN.map(([v, l]) => h('option', { value: v }, l)));
    const note = h('div', { class: 'result when' });
    const form = h('form', { class: 'new-page', hidden: true, onsubmit: async (e) => {
      e.preventDefault();
      if (!title.value.trim()) { note.textContent = 'Give the page a title.'; title.focus(); return; }
      const r = await api(`${base}/page`, { method: 'POST', body: { folder: folder.value, title: title.value } });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { note.textContent = out.message || 'Could not create the page.'; return; }
      location.hash = `${link(out.path)}?edit`;
    } },
    h('label', { for: 'np-title', class: 'field-label' }, 'New page title'), title,
    h('label', { for: 'np-folder', class: 'field-label' }, 'Goes under'), folder,
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn small-btn' }, 'Create'),
      h('button', { type: 'button', class: 'btn quiet small-btn', onclick: () => { form.hidden = true; opener.hidden = false; } }, 'Cancel')),
    note);
    const opener = h('button', { type: 'button', class: 'btn quiet wide-only', onclick: () => { form.hidden = false; opener.hidden = true; title.focus(); } }, 'New page');
    return [opener, form];
  }

  function showTree() {
    const item = (p) => h('a', { href: link(p.path), 'aria-current': p.path === current ? 'page' : null }, p.title);
    const groups = FOLDERS.flatMap(([folder, label]) => {
      const mine = pages.filter((p) => p.folder === folder);
      return mine.length ? [h('h2', { class: 'label' }, label), ...mine.map(item)] : [];
    });
    const other = pages.filter((p) => !FOLDERS.some(([f]) => f === p.folder));
    tree.replaceChildren(...newPageForm(), ...groups,
      ...(other.length ? [h('h2', { class: 'label' }, 'Other'), ...other.map(item)] : []));
  }

  async function refresh() {
    const d = await getJSON(base);
    if (!alive) return;
    pages = d.pages;
    showTree();
    // Two versions of a page (changed on a phone and by the agent before they synced).
    const conflicts = d.conflicts || [];
    conflictsBox.hidden = !conflicts.length;
    conflictsBox.replaceChildren(...(conflicts.length ? [
      h('strong', {}, conflicts.length === 1 ? 'A page has two versions' : `${conflicts.length} pages have two versions`),
      h('p', { class: 'when' }, 'It was changed on a phone and here before the two synced, so Syncthing kept both. Open both, keep what you want in the page, then delete the copy (in Obsidian, or ask the agent).'),
      h('ul', {}, conflicts.map((c) => h('li', {}, h('a', { href: link(c.of) }, c.title), ': ',
        h('a', { href: obsidian(c.path), class: 'phone-only' }, 'open the copy in Obsidian'),
        h('span', { class: 'wide-only' }, 'the copy is ', h('code', {}, c.path))))),
    ] : []));
    changes.replaceChildren(h('h2', { class: 'label' }, 'Changed this week'),
      ...(d.changes.length ? d.changes.map((c) => h('a', { href: link(c.path), class: 'hit' }, c.title, h('span', { class: 'when' }, when(c.when))))
        : [h('span', { class: 'when' }, 'Nothing changed this week.')]),
      h('p', { class: 'when' }, 'Tidied nightly. Every change is kept in history.'));
  }

  // ── add or change by asking the agent ──────────────────────────────────
  function askBox(page) {
    const id = 'ask-brain';
    const input = h('textarea', { id, rows: '2', placeholder: `For example: add that the table is booked for 7:30, or note Sam's new phone number` });
    const out = h('div', { class: 'result' });
    const go = h('button', { type: 'submit', class: 'btn' }, 'Add to brain');
    return h('form', { class: 'ask', onsubmit: async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      go.disabled = true;
      out.textContent = `${agent.display_name} is filing it…`;
      const where = page ? `I am looking at the brain page "${page.path}". Put it there if it belongs there, otherwise wherever it fits. ` : '';
      try {
        const reply = await askAgent(agent,
          `Please file this in my brain now. ${where}Then tell me in one or two lines exactly which page you changed and what you wrote. Do not ask me questions.\n\n${text}`,
          (t) => { if (t) out.replaceChildren(...markdown(t)); });
        out.replaceChildren(...markdown(reply || 'Done.'));
        input.value = '';
        await refresh();
        if (page) await showPage(pages.find((p) => p.path === page.path) || page, true);
      } catch (err) {
        out.textContent = `Could not do that: ${err.message}`;
      }
      go.disabled = false;
    } },
    h('label', { for: id, class: 'field-label' }, `Tell ${agent.display_name} what to add or change`),
    h('div', { class: 'ask-row' }, input, go), out);
  }

  // ── read a page ────────────────────────────────────────────────────────
  async function showPage(page, keepAsk = false) {
    current = page.path;
    const ask = keepAsk ? article.querySelector('.ask') : null;
    if (!keepAsk) article.replaceChildren(h('p', { class: 'when' }, 'Loading…'));
    let d;
    try {
      d = await getJSON(`${base}/page?path=${encodeURIComponent(page.path)}`);
    } catch (e) {
      article.replaceChildren(h('div', { class: 'notice' }, 'Could not open this page.'));
      return;
    }
    if (!alive) return;
    const body = d.text.replace(/^# .*\n?/, '');
    article.replaceChildren(
      h('div', { class: 'doc-head' }, h('h1', {}, d.title),
        h('span', { class: 'when' }, d.path.replace(/\.md$/, '').replace(/\//g, ' / ')),
        h('button', { type: 'button', class: 'btn quiet small wide-only', onclick: () => edit(page, d) }, 'Edit'),
        h('a', { class: 'btn quiet small phone-only obsidian-link', href: obsidian(d.path) }, 'Open in Obsidian')),
      ask || askBox(page),
      h('div', { class: 'doc-body' }, ...(body.trim() ? markdown(body) : [h('p', { class: 'when' }, 'This page is empty.')])),
      h('div', { class: 'doc-foot when' }, `Last changed ${when(d.modified).toLowerCase()}. Your edits win: ${agent.display_name} keeps what you write.`));
    showTree();
  }

  // ── edit a page by hand ────────────────────────────────────────────────
  function edit(page, d) {
    const area = h('textarea', { id: 'page-text', class: 'editor', spellcheck: 'true' });
    area.value = d.text;
    const preview = h('div', { class: 'preview', 'aria-label': 'Preview' });
    const note = h('div', { class: 'result when' });
    const paint = () => preview.replaceChildren(...markdown(area.value));
    area.addEventListener('input', paint);
    paint();
    let beforeTidy = null;

    // Wrap or prefix the selected lines, the way a formatting button would.
    const selection = () => [area.selectionStart, area.selectionEnd];
    const replace = (start, end, text, selStart = start, selEnd = start + text.length) => {
      area.setRangeText(text, start, end, 'preserve');
      area.setSelectionRange(selStart, selEnd);
      area.focus();
      paint();
    };
    const wrap = (mark) => () => {
      const [s, e] = selection();
      const picked = area.value.slice(s, e) || 'text';
      replace(s, e, `${mark}${picked}${mark}`, s + mark.length, s + mark.length + picked.length);
    };
    const prefix = (mark) => () => {
      const [s, e] = selection();
      const from = area.value.lastIndexOf('\n', s - 1) + 1;
      const to = area.value.indexOf('\n', e) === -1 ? area.value.length : area.value.indexOf('\n', e);
      const lines = area.value.slice(from, to).split('\n')
        .map((l) => (l.trim() ? mark + l.replace(/^(#{1,6}\s+|[-*]\s+(\[[ x]\]\s+)?)/, '') : l));
      replace(from, to, lines.join('\n'));
    };
    const tool = (label, title, fn) => h('button', { type: 'button', class: 'tool', title, 'aria-label': title, onclick: fn }, label);

    const undo = h('button', { type: 'button', class: 'tool', hidden: true, onclick: () => {
      if (beforeTidy == null) return;
      area.value = beforeTidy; beforeTidy = null; undo.hidden = true; note.textContent = 'Tidy-up undone.'; paint();
    } }, 'Undo tidy');
    const tidy = h('button', { type: 'button', class: 'tool accent', title: 'Let the model fix headings, lists and spacing. It may not change what the notes say.' }, 'Tidy formatting');
    tidy.addEventListener('click', async () => {
      let [s, e] = selection();
      if (s === e) { s = 0; e = area.value.length; }
      const part = area.value.slice(s, e);
      tidy.disabled = true;
      note.textContent = s === 0 && e === area.value.length ? 'Tidying the whole page…' : 'Tidying the selected text…';
      const r = await api(`${base}/reformat`, { method: 'POST', body: { text: part } });
      const out = await r.json().catch(() => ({}));
      tidy.disabled = false;
      if (!r.ok) { note.textContent = out.message || 'Could not tidy that.'; return; }
      beforeTidy = area.value;
      replace(s, e, out.text);
      undo.hidden = false;
      note.textContent = 'Tidied. Check it reads right, then Save. Nothing is saved until you do.';
    });

    const save = h('button', { type: 'button', class: 'btn' }, 'Save');
    save.addEventListener('click', async () => {
      save.disabled = true;
      note.textContent = 'Saving…';
      const r = await api(`${base}/page`, { method: 'PUT', body: { path: d.path, text: area.value, modified: d.modified } });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) {
        save.disabled = false;
        note.textContent = out.message || 'Could not save.';
        return;
      }
      await refresh();
      showPage(pages.find((p) => p.path === d.path) || page);
    });

    article.replaceChildren(
      h('div', { class: 'doc-head' }, h('h1', {}, `Editing: ${d.title}`)),
      h('div', { class: 'toolbar', role: 'toolbar', 'aria-label': 'Formatting' },
        tool('B', 'Bold', wrap('**')), tool('H', 'Heading', prefix('## ')), tool('List', 'Bullet list', prefix('- ')),
        tool('To-do', 'To-do item', prefix('- [ ] ')), tool('Code', 'Code', wrap('`')), tidy, undo),
      h('div', { class: 'edit-split' },
        h('div', {}, h('label', { for: 'page-text', class: 'label' }, 'Markdown'), area),
        h('div', {}, h('div', { class: 'label' }, 'How it will look'), preview)),
      h('div', { class: 'row' }, save,
        h('button', { type: 'button', class: 'btn quiet', onclick: () => showPage(page) }, 'Cancel')),
      note,
      h('p', { class: 'when' }, 'Select text and press a button to format it. "Tidy formatting" works on the selection, or the whole page if nothing is selected. If the agent changes this page while you edit, your save is refused so nothing is lost.'));
    area.focus();
  }

  // ── search ─────────────────────────────────────────────────────────────
  let timer = null;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = search.value.trim();
      if (q.length < 2) { changes.hidden = false; results.hidden = true; return; }
      try {
        const { hits } = await getJSON(`${base}/search?q=${encodeURIComponent(q)}`);
        if (!alive) return;
        const title = (x) => (pages.find((p) => p.path === x.path) || x).title;
        results.replaceChildren(h('h2', { class: 'label' }, `Found in ${hits.length} page${hits.length === 1 ? '' : 's'}`),
          ...hits.map((x) => h('a', { href: link(x.path), class: 'hit' }, title(x), h('span', { class: 'when' }, x.snippet))));
        changes.hidden = true; results.hidden = false;
      } catch (e) { /* keep what is shown */ }
    }, 250);
  });

  const tabs = agents.length > 1 ? h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Whose brain' },
    agents.map((a) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(a.name === agent.name),
      onclick: () => { location.hash = `#/brain/${a.name}`; } }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }),
      ...agentLabel(a.display_name)))) : null;

  root.replaceChildren(h('div', { class: 'brain' },
    h('header', { class: 'chat-head' }, h('h1', {}, 'Brain'), tabs,
      h('label', { for: 'q', class: 'sr' }, 'Search the brain'), search),
    conflictsBox,
    h('div', { class: 'brain-body' }, tree, article,
      h('aside', { class: 'side', 'aria-label': 'Recent changes' }, changes, results))));

  try {
    await refresh();
    if (!alive) return () => {};
    const page = pages.find((p) => p.path === current)
      || pages.find((p) => p.folder === 'projects') || pages.find((p) => p.path === 'README.md') || pages[0];
    if (page && startEditing && page.path === current) {
      current = page.path;
      showTree();
      edit(page, await getJSON(`${base}/page?path=${encodeURIComponent(page.path)}`));
    } else if (page) await showPage(page);
    else article.replaceChildren(askBox(null), h('p', { class: 'when' }, 'This brain has no pages yet.'));
  } catch (e) {
    article.replaceChildren(h('div', { class: 'notice' }, 'This brain could not be read.'));
  }
  return () => { alive = false; clearTimeout(timer); };
}
