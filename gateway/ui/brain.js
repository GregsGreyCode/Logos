// Brain screen: browse what an agent has written down, and correct a page by hand.
import { h, api, getJSON, when, markdown } from './util.js';

const FOLDERS = [['projects', 'Projects'], ['people', 'People'], ['topics', 'Topics'], ['daily', 'Daily'],
  ['inbox', 'Inbox'], ['', 'General']];

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
  const wanted = pathParts.length ? decodeURIComponent(pathParts.join('/')).replace(/\?edit$/, '') : '';
  const base = `/api/v2/agents/${agent.name}/brain`;
  const link = (path) => `#/brain/${agent.name}/${path.split('/').map(encodeURIComponent).join('/')}`;

  const tree = h('nav', { class: 'tree', 'aria-label': 'Pages' });
  const article = h('article', { class: 'doc' });
  const changes = h('div', { class: 'changes' });
  const search = h('input', { id: 'q', type: 'search', placeholder: 'Search pages and notes' });

  async function newPage() {
    const title = (prompt('Title of the new page') || '').trim();
    if (!title) return;
    const folder = (prompt('Where does it go? Type projects, people or topics', 'projects') || '').trim().toLowerCase();
    const r = await api(`${base}/page`, { method: 'POST', body: { folder, title } });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) { alert(out.message || 'Could not create the page.'); return; }
    location.hash = link(out.path) + '?edit';
  }

  function showTree(pages, current) {
    tree.replaceChildren(...FOLDERS.flatMap(([folder, label]) => {
      const mine = pages.filter((p) => p.folder === folder);
      return mine.length ? [h('h2', { class: 'label' }, label),
        ...mine.map((p) => h('a', { href: link(p.path), 'aria-current': p.path === current ? 'page' : null }, p.title))] : [];
    }));
    tree.prepend(h('button', { type: 'button', class: 'btn quiet', onclick: newPage }, 'New page'));
    const other = pages.filter((p) => !FOLDERS.some(([f]) => f === p.folder));
    if (other.length) tree.append(h('h2', { class: 'label' }, 'Other'),
      ...other.map((p) => h('a', { href: link(p.path), 'aria-current': p.path === current ? 'page' : null }, p.title)));
  }

  async function showPage(page) {
    article.replaceChildren(h('p', { class: 'when' }, 'Loading…'));
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
        h('button', { type: 'button', class: 'btn quiet small', onclick: () => edit(page, d) }, 'Edit')),
      ...(body.trim() ? markdown(body) : [h('p', { class: 'when' }, 'This page is empty.')]),
      h('div', { class: 'doc-foot when' }, `Last changed ${when(d.modified).toLowerCase()}. Your edits win: ${agent.display_name} keeps what you write.`));
  }

  function edit(page, d) {
    const area = h('textarea', { id: 'page-text', class: 'editor', spellcheck: 'true' });
    area.value = d.text;
    const note = h('div', { class: 'result when' });
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
      showPage({ ...page, modified: out.modified });
    });
    const preview = h('div', { class: 'preview', 'aria-label': 'Preview' });
    const paint = () => preview.replaceChildren(...markdown(area.value));
    area.addEventListener('input', paint);
    paint();
    article.replaceChildren(
      h('div', { class: 'doc-head' }, h('h1', {}, `Editing: ${d.title}`)),
      h('div', { class: 'edit-split' },
        h('div', {}, h('label', { for: 'page-text', class: 'label' }, 'Markdown'), area),
        h('div', {}, h('div', { class: 'label' }, 'How it will look'), preview)),
      h('div', { class: 'row' }, save,
        h('button', { type: 'button', class: 'btn quiet', onclick: () => showPage(page) }, 'Cancel')),
      note,
      h('p', { class: 'when' }, 'Plain Markdown. The first line starting with "# " is the title. If the agent changes this page while you edit, your save is refused so nothing is lost.'));
    area.focus();
  }

  let timer = null;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = search.value.trim();
      if (q.length < 2) { changes.hidden = false; results.hidden = true; return; }
      try {
        const { hits } = await getJSON(`${base}/search?q=${encodeURIComponent(q)}`);
        if (!alive) return;
        results.replaceChildren(h('h2', { class: 'label' }, `Found in ${hits.length} page${hits.length === 1 ? '' : 's'}`),
          ...hits.map((x) => h('a', { href: link(x.path), class: 'hit' }, x.title, h('span', { class: 'when' }, x.snippet))));
        changes.hidden = true; results.hidden = false;
      } catch (e) { /* keep what is shown */ }
    }, 250);
  });
  const results = h('div', { class: 'changes', hidden: true });

  const tabs = agents.length > 1 ? h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Whose brain' },
    agents.map((a) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(a.name === agent.name),
      onclick: () => { location.hash = `#/brain/${a.name}`; } }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), a.display_name))) : null;

  root.replaceChildren(h('div', { class: 'brain' },
    h('header', { class: 'chat-head' }, h('h1', {}, 'Brain'), tabs,
      h('label', { for: 'q', class: 'sr' }, 'Search the brain'), search),
    h('div', { class: 'brain-body' }, tree, article,
      h('aside', { class: 'side', 'aria-label': 'Recent changes' }, changes, results))));

  try {
    const d = await getJSON(base);
    if (!alive) return () => {};
    const page = d.pages.find((p) => p.path === wanted)
      || d.pages.find((p) => p.folder === 'projects') || d.pages.find((p) => p.path === 'README.md') || d.pages[0];
    showTree(d.pages, page && page.path);
    changes.replaceChildren(h('h2', { class: 'label' }, 'Changed this week'),
      ...(d.changes.length ? d.changes.map((c) => h('a', { href: link(c.path), class: 'hit' }, c.title, h('span', { class: 'when' }, when(c.when))))
        : [h('span', { class: 'when' }, 'Nothing changed this week.')]),
      h('p', { class: 'when' }, 'Tidied nightly. Every change is kept in history.'));
    if (page && startEditing && page.path === wanted) {
      edit(page, await getJSON(`${base}/page?path=${encodeURIComponent(page.path)}`));
    } else if (page) await showPage(page); else article.replaceChildren(h('p', { class: 'when' }, 'This brain has no pages yet.'));
  } catch (e) {
    article.replaceChildren(h('div', { class: 'notice' }, 'This brain could not be read.'));
  }
  return () => { alive = false; clearTimeout(timer); };
}
