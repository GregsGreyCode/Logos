// Settings › Household words (28 Nov): names speech-to-text gets wrong, and what it writes instead.
// Logos puts them right in what the house speaker hears and in voice notes, and tells the agents
// (gateway/household_words_api.py). People's and agents' own names join by themselves; anyone in the
// household can add what speech-to-text writes for them, or another word.
import { h, api, getJSON, loading, emptyState } from './util.js';

const PATH = '/api/v2/household-words';
const FROM = { person: 'A person here', agent: 'An agent’s name' };

const splitHeard = (s) => s.split(',').map((x) => x.trim()).filter(Boolean);

export function householdWordsPage(body) {
  let alive = true, words = null, editing = null;        // editing: an index, 'new', or null
  const msg = h('p', { class: 'when', role: 'status' });
  const list = h('div', { class: 'rows hw-list' }, loading('rows', 3));
  const addBtn = h('button', { type: 'button', class: 'btn quiet hw-add', onclick: () => { editing = 'new'; draw(); } }, 'Add a word');
  body.replaceChildren(h('section', { 'aria-label': 'Household words' },
    h('p', { class: 'when' }, 'Names speech-to-text gets wrong, and what it writes instead. Logos puts them right in what the speaker hears and in voice notes, and tells the agents. People’s and agents’ names are here already.'),
    list, addBtn, msg));

  async function save(next, done) {
    msg.textContent = 'Saving…';
    const r = await api(PATH, { method: 'PUT', body: { words: next.map(({ word, heard_as, note }) => ({ word, heard_as, note: note || '' })) } }).catch(() => null);
    const out = r ? await r.json().catch(() => ({})) : {};
    if (!r || !r.ok) { msg.textContent = out.message || 'That did not save just now.'; return false; }
    words = out.words || [];
    editing = null;
    msg.textContent = done || 'Saved. Speech-to-text uses it within a minute.';
    draw();
    return true;
  }

  function form(e, i) {
    const isNew = i === 'new';
    const id = (k) => `hw-${k}-${isNew ? 'new' : i}`;
    const word = h('input', { id: id('word'), type: 'text', value: e.word || '', maxlength: 40, autocomplete: 'off', placeholder: 'As it is written', readonly: !!e.source || null });
    const heard = h('input', { id: id('heard'), type: 'text', value: (e.heard_as || []).join(', '), autocomplete: 'off', spellcheck: 'false', placeholder: 'What it writes instead, with commas' });
    const note = h('input', { id: id('note'), type: 'text', value: e.note || '', maxlength: 120, autocomplete: 'off', placeholder: 'Optional: who or what it is' });
    const f = h('form', { class: 'settings-form hw-form', 'aria-label': isNew ? 'Add a word' : `Edit ${e.word}`, onsubmit: async (ev) => {
      ev.preventDefault();
      const w = word.value.trim();
      if (!w) { msg.textContent = 'Write the word first.'; word.focus(); return; }
      const entry = { word: w, heard_as: splitHeard(heard.value), note: note.value.trim(), source: e.source };
      const next = words.slice();
      const clash = next.findIndex((x, j) => j !== i && x.word.toLowerCase() === w.toLowerCase());
      if (clash >= 0 && isNew) { msg.textContent = `${next[clash].word} is on the list already: edit it there.`; return; }
      if (isNew) next.unshift(entry); else next[i] = entry;
      await save(next);
    } },
    h('label', { for: id('word'), class: 'field-label' }, 'Word'), word,
    h('label', { for: id('heard'), class: 'field-label' }, 'Speech-to-text writes'), heard,
    h('p', { class: 'when' }, 'Only what it gets wrong: a word you also mean (like “rose”) would be changed every time.'),
    h('label', { for: id('note'), class: 'field-label' }, 'Note'), note,
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Save'),
      h('button', { type: 'button', class: 'btn quiet', onclick: () => { editing = null; msg.textContent = ''; draw(); } }, 'Cancel')));
    setTimeout(() => (isNew || e.source ? (isNew ? word : heard) : heard).focus(), 0);
    return f;
  }

  function line(e, i) {
    if (editing === i) return form(e, i);
    const heard = (e.heard_as || []).length ? `Written as ${e.heard_as.join(', ')}` : 'Nothing it gets wrong yet';
    return h('div', { class: 'row-item hw-row', 'data-word': e.word },
      h('span', { class: 'row-main' }, h('strong', {}, e.word),
        h('span', { class: 'when block' }, [heard, e.note, e.source && FROM[e.source]].filter(Boolean).join(' · '))),
      h('span', { class: 'hw-actions' },
        h('button', { type: 'button', class: 'btn small quiet', 'aria-label': `Edit ${e.word}`, onclick: () => { editing = i; msg.textContent = ''; draw(); } }, 'Edit'),
        // A person's or an agent's own name stays; Edit can clear what was added to it.
        !e.source ? h('button', { type: 'button', class: 'btn small quiet danger', 'aria-label': `Remove ${e.word}`, onclick: async (ev) => {
          ev.target.disabled = true;
          if (!await save(words.filter((_, j) => j !== i), `${e.word} removed.`)) ev.target.disabled = false;
        } }, 'Remove') : null));
  }

  function draw() {
    if (!words || !alive) return;
    addBtn.hidden = editing === 'new';
    if (!words.length && editing !== 'new') {
      list.replaceChildren(emptyState({ icon: 'chat', heading: 'No household words yet', text: 'Add a name speech-to-text gets wrong.' }));
      return;
    }
    list.replaceChildren(...(editing === 'new' ? [form({ heard_as: [] }, 'new')] : []), ...words.map(line));
  }

  getJSON(PATH).then((d) => { words = d.words || []; draw(); })
    .catch(() => list.replaceChildren(h('p', { class: 'none' }, 'The words could not be loaded just now.')));
  return () => { alive = false; };
}

// "3 words": the row's value in Settings.
export async function householdWordsSummary() {
  const d = await getJSON(PATH);
  const n = (d.words || []).length;
  return n ? `${n} word${n === 1 ? '' : 's'}` : 'None yet';
}
