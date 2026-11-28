// People screen: who can sign in, who can use which agent, and your own account. An admin also adds
// people by invitation (F38), opens each person's page (what was set up, who can see what), removes
// and restores them, and sees the list of admin actions. The server checks every one of these again.
import { h, api, getJSON, loading, icon, ICONS, subpage } from './util.js';
import { sitesSummary } from './settings.js';

export const slug = (name) => name.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().trim()
  .split(/\s+/)[0].replace(/[^a-z0-9-]+/g, '-').replace(/^[^a-z]+|-+$/g, '').slice(0, 31);

const day = (t) => new Date(t * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' });
const when = (t) => new Date(t * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const STATE = {
  setting_up: 'Setting up…', needs_attention: 'Setup needs a look', invited: 'Invited', invite_expired: 'Invitation expired',
  removing: 'Removing…', removed: 'Removed',
};
const ACTIONS = {
  person_invited: 'invited', invite_renewed: 'sent a new invitation link for', invite_cancelled: 'cancelled the invitation of',
  invite_accepted: 'accepted the invitation', person_setup: 'ran the setup again for', person_removed: 'removed',
  person_restored: 'restored', person_deleted: 'deleted for good (30 days after removal)', create_user: 'added an account for',
  update_user: 'changed the account of', admin_set_password: 'set a new password for', setup_wipe: 'removed all accounts',
};

// Everyone in one row style (28 Nov): icon, name, a status line under it, and a chevron when it opens a page.
function personRow(href, name, value, attention = false, glyph = 'user') {
  return h(href ? 'a' : 'div', { href, class: `set-row person-link two-line${href ? '' : ' no-link'}` },
    h('span', { class: 'set-icon' }, icon(ICONS[glyph])),
    h('span', { class: 'set-text' }, h('span', { class: 'set-name' }, name),
      h('span', { class: `set-value${attention ? ' attention' : ''}` }, value || '')),
    href ? h('span', { class: 'set-chevron' }, icon(ICONS.chevron)) : null);
}

// The link to hand over, shown once (the server keeps only its hash).
function linkBox(token, expires) {
  const url = `${location.origin}/invite#${token}`;
  const input = h('input', { type: 'text', readonly: true, value: url, class: 'invite-link', 'aria-label': 'Invitation link',
    onfocus: (e) => e.target.select() });
  const note = h('span', { class: 'when' });
  return h('div', { class: 'invite-box', role: 'status' },
    h('p', {}, h('strong', {}, 'Send them this link privately'), ' (a message to them alone). It works once, until ', day(expires),
      '. It is shown only now: if it is lost, make a new one from their page.'),
    input,
    h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn', onclick: async () => {
      try { await navigator.clipboard.writeText(url); note.textContent = 'Copied.'; } catch (e) { input.select(); note.textContent = 'Select it and copy.'; }
    } }, 'Copy the link'), note));
}

export async function peopleView(root, [who], state) {
  if (who) return personView(root, who, state);
  let alive = true;
  // On a phone People is reached from Settings: its back arrow goes there (28 Nov).
  if (!matchMedia('(min-width: 820px)').matches) subpage({ title: 'People', back: '#/settings', backLabel: 'Settings' });
  const everyone = h('section', { class: 'set-group people-list', 'aria-label': 'People' }, loading('rows', 2));
  const pending = h('div', { class: 'stack' });
  const sites = h('div', { class: 'people-sites' });
  const admin = h('div', { class: 'stack' });
  const log = h('div', { class: 'stack' });
  const addBox = h('div', { class: 'people-add' });

  async function load() {
    let d;
    try { d = await getJSON('/api/v2/people'); } catch (e) {
      everyone.replaceChildren(h('div', { class: 'notice' }, 'Could not load people.'));
      return;
    }
    if (!alive) return;
    const page = (p) => (d.is_admin || p.is_you ? `#/people/${encodeURIComponent(p.username)}` : null);
    const line = (p) => [`@${p.username}`, p.is_you ? 'you' : p.signed_in_before ? '' : 'not signed in yet', p.is_admin ? 'manages Logos' : '',
      STATE[p.state] || '', (() => { const uses = d.agents.filter((a) => p.agents && p.agents[a.name]).map((a) => a.display_name); return uses.length ? `uses ${uses.join(', ')}` : 'no agents'; })()]
      .filter(Boolean).join(' · ');
    everyone.replaceChildren(h('h2', { class: 'set-group-title' }, 'People'),
      ...d.people.map((p) => personRow(page(p), p.display_name, line(p), !!STATE[p.state])));
    if (!d.is_admin) return;
    const invited = d.invited || [], removed = d.removed || [];
    pending.replaceChildren(...[
      invited.length ? h('section', { class: 'set-group', 'aria-label': 'Invited' }, h('h2', { class: 'set-group-title' }, 'Invited'),
        ...invited.map((p) => personRow(`#/people/${encodeURIComponent(p.username)}`, p.display_name,
          p.state === 'invited' ? `Until ${day(p.invite_expires)}` : 'Link expired', p.state !== 'invited'))) : null,
      removed.length ? h('section', { class: 'set-group', 'aria-label': 'Removed' }, h('h2', { class: 'set-group-title' }, 'Removed'),
        ...removed.map((p) => personRow(`#/people/${encodeURIComponent(p.username)}`, p.display_name,
          p.state === 'removing' ? 'Removing…' : (p.delete_after ? `Restorable until ${day(p.delete_after)}` : 'Removed')))) : null,
    ].filter(Boolean));
    if (!addBox.children.length) {
      const form = addPerson(d.offers);
      form.hidden = true;
      const open = h('button', { type: 'button', class: 'btn people-add-btn', 'aria-expanded': 'false', onclick: () => {
        form.hidden = !form.hidden; open.setAttribute('aria-expanded', String(!form.hidden));
        open.textContent = form.hidden ? 'Add a person' : 'Close the form';
        if (!form.hidden) form.querySelector('#np-name').focus();
      } }, 'Add a person');
      addBox.replaceChildren(open, form);
    }
    if (!admin.children.length) admin.replaceChildren(startAgain());
    if (!log.children.length) log.replaceChildren(auditCard());
  }

  // E6: what the agents may reach beyond Logos, as one row (28 Nov): its own page in Settings has the list.
  sitesSummary().then((v) => {
    if (!alive || !v) return;
    sites.replaceChildren(h('section', { class: 'set-group', 'aria-label': 'Sites' },
      personRow('#/settings/sites', 'Sites', `${v} · what the agents may reach beyond Logos`, false, 'globe')));
  }).catch(() => {});

  function field(id, label, type, extra = {}) {
    const input = h('input', { id, type, ...extra });
    return [h('label', { for: id, class: 'field-label' }, label), input];
  }

  function tick(id, label, on, hint) {
    const box = h('input', { type: 'checkbox', id, checked: on ? true : null });
    return [h('label', { class: 'tick', for: id }, box, h('span', {}, label, hint ? h('span', { class: 'when block' }, hint) : null)), box];
  }

  function addPerson(offers) {
    const o = offers || {};
    const [nl, name] = field('np-name', 'Their name', 'text', { autocomplete: 'off', maxlength: '60' });
    const [ul, user] = field('np-user', 'Username (also their agent’s name)', 'text', { autocomplete: 'off', maxlength: '31',
      autocapitalize: 'none', spellcheck: 'false', pattern: '[a-z][a-z0-9-]{1,30}' });
    const [el, email] = field('np-email', 'Email (optional)', 'email', { autocomplete: 'off' });
    let typed = false;
    name.addEventListener('input', () => { if (!typed) user.value = slug(name.value); });
    user.addEventListener('input', () => { typed = true; });
    const picks = [];
    const add = (id, label, on, hint) => { const [row, box] = tick(id, label, on, hint); picks.push([id, box]); return row; };
    const days = h('select', { id: 'np-days', 'aria-label': 'The link works for' },
      [1, 3, 7, 14].map((n) => h('option', { value: String(n), selected: n === 7 ? true : null }, `${n} day${n > 1 ? 's' : ''}`)));
    const note = h('div', { class: 'result when', role: 'status' });
    const out = h('div');
    return h('form', { class: 'card form', 'aria-label': 'Add a person', onsubmit: async (e) => {
      e.preventDefault();
      out.replaceChildren();
      if (!name.value.trim() || !user.value) { note.textContent = 'Give their name and a username.'; return; }
      const body = { display_name: name.value.trim(), username: user.value.trim(), email: email.value.trim(), days: Number(days.value) };
      for (const [id, box] of picks) body[id.replace('np-', '')] = box.checked;
      note.textContent = 'Adding…';
      const r = await api('/api/v2/people', { method: 'POST', body });
      const res = await r.json().catch(() => ({}));
      if (!r.ok) { note.textContent = res.message || 'Could not add them.'; return; }
      note.textContent = `${body.display_name} is invited. Their agent is set up when they open the link and choose a password.`;
      out.replaceChildren(linkBox(res.token, res.expires_at));
      name.value = ''; user.value = ''; email.value = ''; typed = false;
      load();
    } }, h('h2', {}, 'Add a person'),
    h('p', { class: 'when' }, 'They get a link to choose their own password. Their own agent is made for them, and only they can see it.'),
    nl, name, ul, user, el, email,
    h('div', { class: 'ticks', role: 'group', 'aria-label': 'Also set up' }, h('span', { class: 'field-label' }, 'Also set up'),
      o.chat ? add('np-chat', 'Chat in Element X', true, `@${user.value || 'name'}:${o.matrix_server || ''}, with the same password`) : null,
      o.calendar ? add('np-calendar', 'Their own calendar', true, 'Also Household; the others see it read only') : null,
      o.household ? add('np-household', 'Join the household agent', true, 'It answers them and sees their busy times') : null,
      add('np-email_read', 'Their agent may read their email', true, 'Once they connect it in Settings › Email; read only')),
    h('label', { for: 'np-days', class: 'field-label' }, 'The link works for'), days,
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Invite')), note, out);
  }

  function auditCard() {
    const list = h('div', { class: 'rows' }, loading('rows', 2));
    let all = [];
    const more = h('button', { type: 'button', class: 'btn ghost', hidden: true, onclick: () => { draw(all); more.hidden = true; } }, 'Show all');
    const draw = (rows) => list.replaceChildren(...(rows.length ? rows.map((a) => h('div', { class: 'row-item audit-row' },
      h('span', {}, h('strong', {}, a.by || 'Logos'), ` ${ACTIONS[a.action] || a.action}${a.about && a.action !== 'invite_accepted' ? ` ${a.about}` : ''}`),
      h('span', { class: 'when' }, when(a.at)))) : [h('p', { class: 'none' }, 'Nothing yet.')]));
    getJSON('/api/v2/people/audit').then((d) => {
      all = d.actions || [];
      draw(all.slice(0, 8));
      more.hidden = all.length <= 8;
    }).catch(() => list.replaceChildren(h('p', { class: 'none' }, 'Could not load them.')));
    return h('section', { class: 'card', 'aria-label': 'Admin actions' }, h('h2', {}, 'Admin actions'),
      h('p', { class: 'when' }, 'Who added, changed or removed whom, newest first.'), list, more);
  }

  function startAgain() {
    const note = h('div', { class: 'result when' });
    return h('section', { class: 'card', 'aria-label': 'Start again' },
      h('h2', {}, 'Remove all accounts and start again'),
      h('p', { class: 'when' }, 'Agents and their brains are not touched. Afterwards nobody can sign in until someone opens a new setup link, which the server prints with: logos gateway setup-link'),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn danger', onclick: async (e) => {
        if (e.target.dataset.armed !== '1') { e.target.dataset.armed = '1'; e.target.textContent = 'Press again to remove all accounts'; return; }
        const r = await api('/api/setup/wipe', { method: 'POST', body: { confirm: 'WIPE' } });
        if (r.ok) location.href = '/login'; else note.textContent = 'Could not do that.';
      } }, 'Start again')), note);
  }

  root.replaceChildren(h('div', { class: 'page people-page' },
    h('header', {}, h('h1', {}, 'People', h('span', { class: 'beta-tag' }, 'Beta')),
      h('p', { class: 'when lead' }, 'Who can sign in, and which agents each one uses.')),
    addBox, everyone, pending, sites,
    h('div', { class: 'two-col' }, log, admin)));
  await load();
  return () => { alive = false; };
}

// ── one person's page ─────────────────────────────────────────────────────────────────────────

async function personView(root, username, state) {
  let alive = true, timer = null;
  const body = h('div', { class: 'stack' }, loading('card', 2));
  const title = h('h1', {}, '');
  const sub = h('p', { class: 'when lead' }, '');
  const isAdmin = state.me && state.me.role === 'admin';
  const card = (label, ...children) => h('section', { class: 'card', 'aria-label': label }, h('h2', {}, label), ...children);
  const act = async (path, opts, note, after) => {
    note.textContent = 'Working…';
    const r = await api(path, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { note.textContent = d.message || 'That did not work.'; return null; }
    note.textContent = '';
    if (after) after(d);
    load();
    return d;
  };

  function steps(list, p) {
    const mark = { ok: '✓', failed: '!', skipped: '–', running: '…', waiting: '·' };
    const note = h('div', { class: 'result when', role: 'status' });
    const failed = list.some((s) => s.state === 'failed');
    return card('What was set up',
      h('div', { class: 'rows' }, list.map((s) => h('div', { class: `check step-${s.state}${s.state === 'failed' ? ' warn' : s.state === 'ok' ? ' ok' : ''}` },
        h('span', { class: 'check-mark', 'aria-hidden': 'true' }, mark[s.state] || '·'),
        h('span', {}, h('strong', {}, s.label), s.detail ? h('span', { class: 'when block' }, s.detail) : null)))),
      failed && isAdmin && p.state !== 'setting_up' ? h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn quiet', onclick: () =>
        act(`/api/v2/people/${encodeURIComponent(username)}/setup`, { method: 'POST', body: {} }, note) }, 'Run the setup again')) : null,
      note);
  }

  function seeing(v) {
    const parts = [];
    parts.push(card('Agents they use', v.uses.length ? h('div', { class: 'rows' }, v.uses.map((u) => h('div', { class: 'row-item' },
      h('strong', {}, u.display_name),
      h('span', { class: 'when' }, u.shared_with.length ? `Shared with ${u.shared_with.join(', ')}` : 'Theirs alone')))) : h('p', { class: 'none' }, 'None yet.')));
    if (v.reach.length) parts.push(card('What their agent can reach', h('ul', { class: 'plain-list' }, v.reach.map((r) => h('li', {}, r)))));
    if (v.seen_by.length) parts.push(card('Who can see what is theirs', h('div', { class: 'rows' }, v.seen_by.map((s) => h('div', { class: 'row-item' },
      h('strong', {}, s.what), h('span', {}, s.who.join(' · ')), s.note ? h('span', { class: 'when' }, s.note) : null)))));
    return parts;
  }

  function invite(p) {
    const note = h('div', { class: 'result when', role: 'status' });
    const out = h('div');
    const expired = p.state === 'invite_expired';
    return card('Invitation',
      h('p', {}, expired ? 'Their link has expired. Make a new one and send it to them.' : `Their link works until ${day(p.invite.expires_at)}. They have not opened it yet.`),
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn', onclick: () => act(`/api/v2/people/${encodeURIComponent(username)}/invite`,
          { method: 'POST', body: { days: 7 } }, note, (d) => out.replaceChildren(linkBox(d.token, d.expires_at))) }, 'Make a new link'),
        h('button', { type: 'button', class: 'btn danger', onclick: (e) => {
          if (e.target.dataset.armed !== '1') { e.target.dataset.armed = '1'; e.target.textContent = 'Press again to cancel it'; return; }
          act(`/api/v2/people/${encodeURIComponent(username)}/invite`, { method: 'DELETE' }, note, () => { location.hash = '#/people'; });
        } }, 'Cancel the invitation')),
      note, out);
  }

  function remove(p) {
    const note = h('div', { class: 'result when', role: 'status' });
    if (!p.removable) return card('Remove', h('p', { class: 'when' }, p.not_removable_because || 'They cannot be removed from here.'));
    const confirm = h('input', { type: 'text', id: 'rm-confirm', autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false' });
    return h('section', { class: 'card form danger-zone', 'aria-label': 'Remove' }, h('h2', {}, `Remove ${p.display_name}`),
      h('p', { class: 'when' }, 'They can no longer sign in, and their agent stops. Its brain, memories and chats are kept for 30 days and can be ',
        'restored from this page; after that they are deleted for good, with their calendar and chat accounts. Their email login is deleted now.'),
      h('label', { for: 'rm-confirm', class: 'field-label' }, `Type “${p.username}” to confirm`), confirm,
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn danger', onclick: () => {
        if (confirm.value.trim() !== p.username) { note.textContent = `Type “${p.username}” first.`; return; }
        act(`/api/v2/people/${encodeURIComponent(username)}/remove`, { method: 'POST', body: { confirm: confirm.value.trim() } }, note);
      } }, 'Remove them')), note);
  }

  function removed(p) {
    const note = h('div', { class: 'result when', role: 'status' });
    const r = p.removed;
    return card('Removed',
      h('p', {}, `Removed ${when(r.removed_at)}${r.by ? ` by ${r.by}` : ''}. ${r.agent ? 'Their agent’s brain, memories and chats are' : 'Their account is'} kept until ${day(r.delete_after)}, then deleted for good.`),
      (r.notes || []).length ? h('ul', { class: 'plain-list when' }, r.notes.map((n) => h('li', {}, n))) : null,
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn', onclick: () =>
        act(`/api/v2/people/${encodeURIComponent(username)}/restore`, { method: 'POST', body: {} }, note) }, `Restore ${p.display_name}`)),
      note);
  }

  async function load() {
    let p;
    try { p = await getJSON(`/api/v2/people/${encodeURIComponent(username)}`); } catch (e) {
      body.replaceChildren(h('div', { class: 'notice' }, e.status === 404 ? 'There is nobody by that name.' : 'Could not load them.'));
      return;
    }
    if (!alive) return;
    title.textContent = p.display_name;
    const name = document.getElementById('screen-name');
    if (name) name.textContent = p.display_name;
    sub.textContent = [`Signs in as ${p.username}`, p.is_admin ? 'Manages Logos' : 'Member',
      p.last_login ? `last signed in ${when(p.last_login / 1000)}` : 'has not signed in yet', p.email || ''].filter(Boolean).join(' · ');
    const parts = [];
    if (STATE[p.state]) parts.push(h('p', { class: 'notice person-state' }, STATE[p.state]));
    if (p.invite) parts.push(invite(p));
    if ((p.steps || []).length && !p.removed) parts.push(steps(p.steps, p));
    if (p.visibility) parts.push(...seeing(p.visibility));
    if (p.removed) parts.push(removed(p));
    else if (isAdmin && 'removable' in p && p.state !== 'removing') parts.push(remove(p));
    body.replaceChildren(...parts);
    clearTimeout(timer);
    if (['setting_up', 'removing'].includes(p.state) || p.busy) timer = setTimeout(() => { if (alive) load(); }, 3000);
  }

  const head = subpage({ title: '', back: '#/people', backLabel: 'People' });
  head.replaceChild(title, head.querySelector('h1'));
  root.replaceChildren(h('div', { class: 'page person-page' }, head, sub, body));
  await load();
  return () => { alive = false; clearTimeout(timer); };
}
