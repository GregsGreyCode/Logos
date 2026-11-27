// People screen: who can sign in, who can use which agent, and your own account. An admin also adds
// people by invitation (F38), opens each person's page (what was set up, who can see what), removes
// and restores them, and sees the list of admin actions. The server checks every one of these again.
import { h, api, getJSON, loading, icon, ICONS } from './util.js';

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

function personRow(href, name, value, attention = false) {
  return h('a', { href, class: 'set-row person-link' },
    h('span', { class: 'set-icon' }, icon(ICONS.user)), h('span', { class: 'set-name' }, name),
    h('span', { class: `set-value${attention ? ' attention' : ''}` }, value || ''),
    h('span', { class: 'set-chevron' }, icon(ICONS.chevron)));
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
  const table = h('div', { class: 'table-box wide-only' }, loading());
  const phoneList = h('div', { class: 'rows phone-only' }, loading());
  const pending = h('div', { class: 'stack' });
  const admin = h('div', { class: 'stack' });
  const log = h('div', { class: 'stack' });

  async function load() {
    let d;
    try { d = await getJSON('/api/v2/people'); } catch (e) {
      table.replaceChildren(h('div', { class: 'notice' }, 'Could not load people.'));
      phoneList.replaceChildren();
      return;
    }
    if (!alive) return;
    const page = (p) => (d.is_admin || p.is_you ? `#/people/${encodeURIComponent(p.username)}` : null);
    const nameOf = (p) => (page(p) ? h('a', { href: page(p), class: 'person-name' }, p.display_name) : p.display_name);
    const stateOf = (p) => (STATE[p.state] ? h('span', { class: 'chip-pill attn' }, STATE[p.state]) : null);
    // A phone (P3): each person on one row with chips for the agents they can use; the desktop keeps the table.
    phoneList.replaceChildren(...d.people.map((p) => h('div', { class: 'row-item person-row' },
      h('span', { class: 'row-main' }, h('strong', {}, nameOf(p)), ' ',
        h('span', { class: 'when' }, `@${p.username}${p.is_you ? ' · you' : p.signed_in_before ? '' : ' · not signed in yet'}`)),
      h('span', { class: 'chip-row' },
        d.agents.filter((a) => p.agents[a.name]).map((a) => h('span', { class: 'chip-pill' }, h('span', { class: `dot ${a.name}`, 'aria-hidden': 'true' }), a.display_name)),
        p.is_admin ? h('span', { class: 'chip-pill', title: 'Manages Logos' }, 'Admin') : null, stateOf(p),
        d.agents.some((a) => p.agents[a.name]) ? null : h('span', { class: 'when' }, 'No agents')))));
    table.replaceChildren(h('table', {},
      h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Person'),
        d.agents.map((a) => h('th', { scope: 'col' }, a.display_name)), h('th', { scope: 'col' }, 'Can manage Logos'))),
      h('tbody', {}, d.people.map((p) => h('tr', {},
        h('td', {}, h('strong', {}, nameOf(p)), ' ', stateOf(p),
          h('span', { class: 'when block' }, p.is_you ? 'You' : p.signed_in_before ? `Signs in as ${p.username}` : `Has not signed in yet · ${p.username}`)),
        d.agents.map((a) => h('td', { class: p.agents[a.name] ? 'ok' : 'when', title: p.agents[a.name] ? 'Can chat with it and read its brain' : 'No access' }, p.agents[a.name] ? '✓' : '·')),
        h('td', {}, p.is_admin ? 'Yes' : 'No'))))));
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
    if (!admin.children.length) admin.replaceChildren(addPerson(d.offers), startAgain());
    if (!log.children.length) log.replaceChildren(auditCard());
  }

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

  root.replaceChildren(h('div', { class: 'page' },
    h('header', {}, h('h1', {}, 'People', h('span', { class: 'beta-tag' }, 'Beta')),
      h('p', { class: 'when lead' }, 'Who can use which agent.')),
    table, phoneList, pending,
    h('div', { class: 'two-col' }, admin, log)));
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

  root.replaceChildren(h('div', { class: 'page person-page' },
    h('header', { class: 'set-head' }, h('a', { href: '#/people', class: 'set-back' }, icon(ICONS.back), h('span', {}, 'People'))),
    h('header', {}, title, sub), body));
  await load();
  return () => { alive = false; clearTimeout(timer); };
}
