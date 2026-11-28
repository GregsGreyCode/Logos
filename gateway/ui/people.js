// People screen: who can sign in, who can use which agent, and your own account.
import { h, api, getJSON, loading } from './util.js';

const slug = (name) => name.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
  .replace(/[^a-z0-9_]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);

export async function peopleView(root, _args, state) {
  let alive = true;
  const table = h('div', { class: 'table-box wide-only' }, loading());
  const phoneList = h('div', { class: 'rows phone-only' }, loading());
  const admin = h('div', { class: 'stack' });

  async function load() {
    let d;
    try { d = await getJSON('/api/v2/people'); } catch (e) {
      table.replaceChildren(h('div', { class: 'notice' }, 'Could not load people.'));
      return;
    }
    if (!alive) return;
    // A phone (P3): each person on one row with chips for the agents they can use; the desktop keeps the table.
    phoneList.replaceChildren(...d.people.map((p) => h('div', { class: 'row-item person-row' },
      h('span', { class: 'row-main' }, h('strong', {}, p.display_name), ' ',
        h('span', { class: 'when' }, `@${p.username}${p.is_you ? ' · you' : p.signed_in_before ? '' : ' · not signed in yet'}`)),
      h('span', { class: 'chip-row' },
        d.agents.filter((a) => p.agents[a.name]).map((a) => h('span', { class: 'chip-pill' }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), a.display_name)),
        p.is_admin ? h('span', { class: 'chip-pill', title: 'Manages Logos' }, 'Admin') : null,
        d.agents.some((a) => p.agents[a.name]) ? null : h('span', { class: 'when' }, 'No agents')))));
    table.replaceChildren(h('table', {},
      h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Person'),
        d.agents.map((a) => h('th', { scope: 'col' }, a.display_name)), h('th', { scope: 'col' }, 'Can manage Logos'))),
      h('tbody', {}, d.people.map((p) => h('tr', {},
        h('td', {}, h('strong', {}, p.display_name),
          h('span', { class: 'when block' }, p.is_you ? 'You' : p.signed_in_before ? `Signs in as ${p.username}` : `Has not signed in yet · ${p.username}`)),
        d.agents.map((a) => h('td', { class: p.agents[a.name] ? 'ok' : 'when', title: p.agents[a.name] ? 'Can chat with it and read its brain' : 'No access' }, p.agents[a.name] ? '✓' : '·')),
        h('td', {}, p.is_admin ? 'Yes' : 'No'))))));
    if (d.is_admin && !admin.children.length) admin.replaceChildren(addPerson(), startAgain());
    if (d.is_admin) drawSites();
  }

  // E6: the sites beyond Logos each agent may reach, as its people allowed them (docs/network-approvals.md).
  const sites = h('section', { class: 'card network-sites', 'aria-label': 'Sites the agents may reach', hidden: true });
  async function drawSites() {
    let n;
    try { n = await getJSON('/api/v2/network'); } catch (e) { return; }
    if (!alive) return;
    const site = (e) => (Number(e.port) === 443 ? e.host : `${e.host}:${e.port}`);
    const until = (e) => new Date(e.until * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    const note = h('div', { class: 'result when' });
    const rows = (n.agents || []).map((a) => {
      const line = (e, kind) => h('li', { class: 'site-row' }, h('span', { class: 'site-name' }, site(e)),
        h('span', { class: 'when' }, kind === 'always' ? ` · by ${e.by}` : kind === 'once' ? ` · until ${until(e)}` : ` · denied until ${until(e)}`),
        kind === 'deny' ? null : h('button', { type: 'button', class: 'btn small quiet', onclick: async (ev) => {
          ev.target.disabled = true;
          const r = await api(`/api/v2/network-sites/${encodeURIComponent(a.name)}/remove`, { method: 'POST', body: { host: e.host, port: e.port } });
          if (r.ok) drawSites(); else { ev.target.disabled = false; note.textContent = 'Could not remove it just now.'; }
        } }, 'Remove'));
      const all = [...a.always.map((e) => line(e, 'always')), ...a.once.map((e) => line(e, 'once')), ...a.deny.map((e) => line(e, 'deny'))];
      return h('div', { class: 'site-agent' }, h('strong', {}, a.display_name),
        all.length ? h('ul', { class: 'site-list' }, ...all) : h('p', { class: 'when' }, 'Only Logos itself.'));
    });
    sites.replaceChildren(h('h2', {}, 'Sites the agents may reach'),
      h('p', { class: 'when' }, 'Besides Logos itself. An agent asks its people when a site is refused; they choose Allow once, Always allow or Deny.'),
      ...rows, note);
    sites.hidden = !rows.length;
  }

  function field(id, label, type, extra = {}) {
    const input = h('input', { id, type, ...extra });
    return [h('label', { for: id, class: 'field-label' }, label), input];
  }

  function addPerson() {
    const [nl, name] = field('np-name', 'Name', 'text');
    const [el, email] = field('np-email', 'Email', 'email');
    const [pl, pass] = field('np-pass', 'Password for their first sign-in', 'password', { autocomplete: 'new-password' });
    const note = h('div', { class: 'result when' });
    return h('form', { class: 'card form', 'aria-label': 'Add a person', onsubmit: async (e) => {
      e.preventDefault();
      const username = slug(name.value);
      if (!username || !email.value || pass.value.length < 8) { note.textContent = 'A name, an email and a password of at least 8 characters are needed.'; return; }
      const r = await api('/users', { method: 'POST', body: {
        email: email.value.trim(), username, password: pass.value, display_name: name.value.trim(), role: 'user' } });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { note.textContent = out.message || out.error || 'Could not add them.'; return; }
      note.textContent = `Added. They sign in as "${username}". They see an agent once its definition lists "${username}" as an owner.`;
      name.value = ''; email.value = ''; pass.value = '';
      load();
    } }, h('h2', {}, 'Add a person'), nl, name, el, email, pl, pass,
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Add')), note);
  }

  function startAgain() {
    const note = h('div', { class: 'result when' });
    return h('section', { class: 'card', 'aria-label': 'Start again' },
      h('h2', {}, 'Remove all accounts and start again'),
      h('p', { class: 'when' }, 'Agents and their brains are not touched. Afterwards nobody can sign in until someone opens a new setup link, which the server prints with: logos gateway setup-link'),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn danger', onclick: async (e) => {
        if (e.target.dataset.armed !== '1') { e.target.dataset.armed = '1'; e.target.textContent = 'Press again to remove all accounts'; return; }
        const r = await api('/api/setup/wipe', { method: 'POST' });
        if (r.ok) location.href = '/login'; else note.textContent = 'Could not do that.';
      } }, 'Start again')), note);
  }

  root.replaceChildren(h('div', { class: 'page' },
    h('header', {}, h('h1', {}, 'People', h('span', { class: 'beta-tag' }, 'Beta')),
      h('p', { class: 'when lead' }, 'Who can use which agent.')),
    table, phoneList, sites,
    h('div', { class: 'two-col' }, admin)));
  await load();
  return () => { alive = false; };
}
