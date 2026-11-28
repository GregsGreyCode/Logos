// The Lists screen (Greg, 28 Nov): your personal lists (only you see them) and the shared ones (both of you, also in
// the Home Assistant app). Everything goes through your own agent's house key (/api/v2/lists, gateway/home.py), so the
// house service decides exactly as it does for the agent. Add, tick, edit and remove items; make, move and delete lists.
import { h, api, getJSON, loading, emptyState, ask, dialog, none, reveal, focusIfKeyboard, icon, ICONS } from './util.js';

const PATH = '/api/v2/lists';

export async function listsView(root, rest, state) {
  let alive = true, data = null, editing = null, shown = false;      // editing: `${kind}/${list}/${item}`
  const msg = h('p', { class: 'when lists-msg', role: 'status' });
  const newBtn = h('button', { type: 'button', class: 'btn quiet small lists-new', onclick: () => newList() }, icon(ICONS.plus), 'New list');
  const body = h('div', { class: 'lists-body' }, loading('cards', 2));
  root.replaceChildren(h('div', { class: 'page lists-page' },
    h('header', { class: 'page-band' }, h('h1', {}, 'Lists')),
    h('div', { class: 'lists-top' }, msg, newBtn), body));

  async function load() {
    try { data = await getJSON(PATH); } catch (e) { data = { error: 'house_unavailable', lists: [] }; }
    if (alive) draw();
  }

  async function change(action, args, done = '') {
    msg.textContent = '';
    const r = await api(`${PATH}/${action}`, { method: 'POST', body: args }).catch(() => null);
    const out = r ? await r.json().catch(() => ({})) : {};
    if (!r || !r.ok || out.error) {
      msg.textContent = out.message || 'That did not work just now: try again in a moment.';
      await load();
      return null;
    }
    editing = null;
    await load();
    msg.textContent = out.note || done;
    return out;
  }

  const kindOf = (l) => (l.kind === 'personal' ? 'personal' : 'shared');
  const base = (l) => ({ list: l.name, kind: kindOf(l) });

  function newList() {
    const personal = !!(data && data.personal);
    const name = h('input', { id: 'list-new-name', type: 'text', maxlength: 40, autocomplete: 'off', placeholder: 'e.g. Video games' });
    const mine = h('input', { type: 'radio', name: 'list-kind', value: 'personal', checked: true });
    const ours = h('input', { type: 'radio', name: 'list-kind', value: 'shared' });
    const note = h('p', { class: 'when', role: 'status' });
    const d = dialog({ label: 'New list', form: true, cls: 'lists-dialog', onsubmit: async (e) => {
      e.preventDefault();
      const n = name.value.trim();
      if (!n) { note.textContent = 'Write a name first.'; name.focus(); return; }
      const shared = !personal || ours.checked;
      note.textContent = 'Making it…';
      const out = await change('create', { name: n, kind: shared ? 'shared' : 'personal' },
        shared ? `“${n}” is shared now: you both see it.` : `“${n}” is yours: only you see it.`);
      if (out) d.close(); else note.textContent = msg.textContent;
    } },
    h('h2', {}, 'New list'),
    h('label', { for: 'list-new-name', class: 'field-label' }, 'Name'), name,
    personal ? h('fieldset', { class: 'lists-kind' }, h('legend', { class: 'field-label' }, 'Who sees it'),
      h('label', {}, mine, ' Just you'), h('label', {}, ours, ' Both of you (shared)')) : h('p', { class: 'when' }, 'Both of you will see it.'),
    note,
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Make it'),
      h('button', { type: 'button', class: 'btn quiet', onclick: () => d.close() }, 'Cancel')));
    focusIfKeyboard(name);
  }

  async function move(l) {
    const toShared = kindOf(l) === 'personal';
    const yes = await ask(toShared
      ? { title: `Share “${l.name}”?`, text: 'Both of you will see it, here and in the Home Assistant app. Its open items go with it; ticked ones are left out.', yes: 'Share it' }
      : { title: `Make “${l.name}” just yours?`, text: 'Only you will see it. The other person no longer will, and it leaves the Home Assistant app if it can.', yes: 'Make it mine', danger: true });
    if (!yes) return;
    await change('move', { name: l.name, kind: kindOf(l), to: toShared ? 'shared' : 'personal' },
      toShared ? `“${l.name}” is shared now.` : `“${l.name}” is yours now.`);
  }

  async function remove(l) {
    const yes = await ask({ title: `Delete “${l.name}”?`, text: 'The list goes, with everything on it. This cannot be undone.', yes: 'Delete', danger: true });
    if (yes) await change('delete', { name: l.name, kind: 'personal' }, `“${l.name}” deleted.`);
  }

  function itemRow(l, i) {
    const key = `${kindOf(l)}/${l.name}/${i.text}`;
    if (editing === key) {
      const input = h('input', { type: 'text', value: i.text, maxlength: 200, 'aria-label': 'Item', autocomplete: 'off' });
      const f = h('form', { class: 'list-edit', onsubmit: async (e) => {
        e.preventDefault();
        const t = input.value.trim();
        if (!t || t === i.text) { editing = null; draw(); return; }
        await change('change', { ...base(l), item: i.text, new_title: t });
      } }, input,
      h('button', { type: 'submit', class: 'btn small' }, 'Save'),
      h('button', { type: 'button', class: 'btn small quiet', onclick: () => { editing = null; draw(); } }, 'Cancel'));
      setTimeout(() => input.focus(), 0);
      return h('li', { class: 'list-item editing' }, f);
    }
    const box = h('button', { type: 'button', class: 'list-box', role: 'checkbox', 'aria-checked': String(!!i.done),
      'aria-label': i.done ? `Put back ${i.text}` : `Tick off ${i.text}` }, i.done ? icon(ICONS.check) : null);
    const row = h('li', { class: `list-item${i.done ? ' done' : ''}` }, box,
      h('button', { type: 'button', class: 'list-text', title: 'Edit', onclick: () => { editing = key; msg.textContent = ''; draw(); } },
        h('span', {}, i.text),
        i.due || i.details ? h('span', { class: 'when block' }, [i.due && `Due ${i.due}`, i.details].filter(Boolean).join(' · ')) : null),
      h('button', { type: 'button', class: 'icon-btn list-remove', 'aria-label': `Remove ${i.text}`, title: 'Remove',
        onclick: (e) => { e.currentTarget.disabled = true; change('remove', { ...base(l), item: i.text }); } }, icon(ICONS.trash)));
    box.addEventListener('click', () => {
      box.disabled = true; row.classList.add('ticking'); box.setAttribute('aria-checked', String(!i.done));
      change(i.done ? 'untick' : 'tick', { ...base(l), item: i.text }, i.done ? `“${i.text}” is back on.` : '');
    });
    return row;
  }

  function card(l) {
    const open = l.items.filter((i) => !i.done), ticked = l.items.filter((i) => i.done);
    const input = h('input', { type: 'text', maxlength: 200, autocomplete: 'off', placeholder: 'Add an item', 'aria-label': `Add to ${l.name}` });
    const add = h('form', { class: 'list-add', onsubmit: async (e) => {
      e.preventDefault();
      const t = input.value.trim();
      if (!t) return;
      input.disabled = true;
      const out = await change('add', { ...base(l), item: t });
      const again = root.querySelector(`.list-card[data-key="${CSS.escape(`${kindOf(l)}/${l.name}`)}"] .list-add input`);
      if (out && again) focusIfKeyboard(again);
    } }, input, h('button', { type: 'submit', class: 'btn small' }, 'Add'));
    const personal = kindOf(l) === 'personal';
    return h('section', { class: 'card list-card', 'data-key': `${kindOf(l)}/${l.name}`, 'aria-label': l.name },
      h('div', { class: 'list-head' },
        h('h3', {}, l.name), h('span', { class: 'list-count' }, String(open.length)),
        h('span', { class: 'list-actions' },
          data.personal ? h('button', { type: 'button', class: 'btn small quiet list-move', onclick: () => move(l) }, personal ? 'Share' : 'Make mine') : null,
          personal ? h('button', { type: 'button', class: 'btn small quiet danger list-delete', onclick: () => remove(l) }, 'Delete') : null)),
      open.length ? h('ul', { class: 'list-items' }, ...open.map((i) => itemRow(l, i))) : none('Nothing on it.'),
      add,
      ticked.length ? h('details', { class: 'list-done' }, h('summary', {}, `Ticked (${ticked.length})`),
        h('ul', { class: 'list-items' }, ...ticked.map((i) => itemRow(l, i)))) : null);
  }

  function draw() {
    if (!alive || !data) return;
    const lists = Array.isArray(data.lists) ? data.lists : [];
    newBtn.hidden = !!data.error;
    if (data.error && !lists.length) {
      body.replaceChildren(data.error === 'no_lists'
        ? emptyState({ icon: 'check', heading: 'No lists here yet', text: 'Lists need the house service, which works them in Home Assistant.', action: { admin: true, label: 'Settings', href: '#/settings', ask: 'to turn the lists on' } })
        : emptyState({ icon: 'alert', heading: 'The lists could not be loaded', text: data.message || 'The house service did not answer.', action: { label: 'Try again', onclick: () => load() } }));
      return;
    }
    const mine = lists.filter((l) => l.kind === 'personal'), ours = lists.filter((l) => l.kind !== 'personal');
    const yours = h('section', { class: 'lists-sec', 'aria-label': 'Yours' },
      h('h2', { class: 'label' }, 'Yours'), h('p', { class: 'when' }, 'Only you see these.'),
      ...(mine.length ? mine.map(card)
        : [data.personal
          ? emptyState({ icon: 'plus', heading: 'No lists of your own yet', text: 'Games to play, books to read: lists only you see. Make one here, or ask your agent.', action: { label: 'New list', onclick: () => newList() } })
          : emptyState({ icon: 'lock', heading: 'No personal lists', text: 'They come with your own agent, once it works the lists.' })]));
    const shared = h('section', { class: 'lists-sec', 'aria-label': 'Shared' },
      h('h2', { class: 'label' }, 'Shared'), h('p', { class: 'when' }, 'Both of you see these, here and in the Home Assistant app.'),
      ...(data.note ? [h('p', { class: 'when lists-note' }, data.note)] : []),
      ...(ours.length ? ours.map(card) : [emptyState({ icon: 'people', heading: 'No shared lists', text: 'Shopping, jobs around the house: lists you both see.' })]));
    body.replaceChildren(yours, shared);
    if (!shown) { shown = true; reveal(body); }
  }

  if (state) state.refresh = load;                    // pulling down (app.js)
  await load();
  return () => { alive = false; };
}
