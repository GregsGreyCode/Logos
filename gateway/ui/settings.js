// Settings (ST1): one screen for everything you set for yourself. Sections on the left (a row of
// tabs on a phone): Appearance, Your account, Chat, Matrix device, and Updates for whoever
// manages Logos. Each section is its own address (#/settings/chat), so it can be linked to.
import { h, api, getJSON } from './util.js';
import { lookControls, SWATCHES, setMine, myColours, setAgentColour, agentColour, setMotion } from './appearance.js';
import { getPref, setPref } from './prefs.js';
import { updatesView } from './updates.js';

const SECTIONS = [
  { id: 'appearance', name: 'Appearance' },
  { id: 'account', name: 'Your account' },
  { id: 'chat', name: 'Chat' },
  { id: 'matrix', name: 'Matrix device' },
  { id: 'updates', name: 'Updates', admin: true },
];

const heading = (text) => h('h2', { class: 'label' }, text);

const savedNote = () => h('p', { class: 'when appearance-note', role: 'status' });
const say = async (note, p) => { note.textContent = (await p) ? 'Saved: it follows you to your other devices.' : 'Used here, but could not be saved just now.'; };

// One row of colours: six tested swatches, any colour, and (for your own colours) back to the look's.
function colourRow(name, help, list, value, onPick, resettable) {
  const swatches = h('div', { class: 'swatches', role: 'radiogroup', 'aria-label': name });
  const paint = (v) => {
    swatches.replaceChildren(
      ...list.map((c) => h('button', { type: 'button', class: 'swatch-btn', role: 'radio', 'aria-checked': String(c === v),
        'aria-label': c, title: c, style: `background:${c}`, onclick: () => { paint(c); onPick(c); } })),
      h('label', { class: 'swatch-pick' }, 'Any colour',
        h('input', { type: 'color', value: v || list[0], 'aria-label': `${name}: any colour`,
          onchange: (e) => { paint(e.target.value); onPick(e.target.value); } })),
      ...(resettable && v ? [h('button', { type: 'button', class: 'swatch-reset', onclick: () => { paint(''); onPick(''); } }, 'The look’s own')] : []));
  };
  paint(value);
  return h('div', { class: 'colour-row' }, h('div', { class: 'who' }, h('strong', {}, name), h('span', { class: 'when' }, help)), swatches);
}

function appearance() {
  const { looksBox, modeBox, note } = lookControls();
  const cNote = savedNote(), mNote = savedNote();
  const mine = myColours();
  const pickMine = (key) => (c) => { setMine({ [key]: c }); say(cNote, setPref(key, c)); };
  const agentsBox = h('div', { class: 'settings-choices' }, h('p', { class: 'none' }, 'Loading…'));
  // Agent colours: the same for everyone. You set those you may use (your own, the household's).
  getJSON('/api/v2/agents').then((d) => {
    const fixed = (d.others || []).map((a) => h('div', { class: 'colour-row' },
      h('div', { class: 'who' }, h('strong', {}, a.display_name), h('span', { class: 'when' }, 'Its owner chooses this one')),
      h('div', { class: 'swatches' }, agentColour(a.name)
        ? h('span', { class: 'swatch-fixed', style: `background:${agentColour(a.name)}` })
        : h('span', { class: `dot ${a.name}`, style: 'width:24px;height:24px' }))));
    agentsBox.replaceChildren(...(d.agents || []).map((a) => colourRow(a.display_name,
      a.shared ? 'Shared: either of you can change it' : 'Everyone sees this colour', SWATCHES.agent, agentColour(a.name),
      (c) => { setAgentColour(a.name, c); say(cNote, api(`/api/v2/agents/${a.name}/colour`, { method: 'PUT', body: { colour: c } }).then((r) => r.ok)); },
      false)), ...fixed);
  }).catch(() => agentsBox.replaceChildren(h('p', { class: 'none' }, 'The agents could not be listed just now.')));
  const radio = (group, value, label, help, current, onPick) => h('label', { class: 'appearance-mode choice' },
    h('input', { type: 'radio', name: group, value, checked: current === value, onchange: () => onPick(value) }),
    h('span', {}, h('strong', {}, label), h('span', { class: 'when block' }, help)));
  const motion = getPref('motion', 'calm'), names = getPref('names', 'hover');
  const pickMotion = (v) => { setMotion(v, null); say(mNote, setPref('motion', v)); };
  const pickNames = (v) => { setMotion(null, v); say(mNote, setPref('names', v)); };
  return [
    h('section', { 'aria-label': 'Look' }, heading('Look'), looksBox),
    h('section', { 'aria-label': 'Light or dark' }, heading('Light or dark'), modeBox, note),
    h('section', { 'aria-label': 'Your colours' }, heading('Your colours'),
      colourRow('Primary and glow', 'Buttons, links, the glow', SWATCHES.primary, mine.primary, pickMine('primary'), true),
      colourRow('Attention', 'Anything waiting on you, and the Noticeboard badge', SWATCHES.attention, mine.attention, pickMine('attention'), true),
      h('p', { class: 'when' }, 'Logos keeps text readable whatever you pick, so a colour may be shown a shade lighter or darker.')),
    h('section', { 'aria-label': 'Agent colours' }, heading('Agent colours'), agentsBox, cNote),
    h('section', { 'aria-label': 'Motion' }, heading('Motion'),
      h('div', { class: 'settings-choices' },
        radio('motion', 'full', 'Full', 'The agents wander, breathe and cycle colour while they think.', motion, pickMotion),
        radio('motion', 'calm', 'Calm', 'Slow breathing and a slow drift; thinking still cycles colour.', motion, pickMotion),
        radio('motion', 'still', 'Still', 'Nothing moves: colour and glow show each state.', motion, pickMotion))),
    h('section', { 'aria-label': 'Agent names' }, heading('Agent names on their marks'),
      h('div', { class: 'settings-choices' },
        radio('names', 'hover', 'When you point at one', 'On a phone they are always shown.', names, pickNames),
        radio('names', 'always', 'Always', 'Each mark has its name and mood under it.', names, pickNames)), mNote),
  ];
}

function account(state) {
  const field = (id, label, type, extra = {}) => {
    const input = h('input', { id, type, ...extra });
    return [h('label', { for: id, class: 'field-label' }, label), input];
  };
  const [nl, name] = field('acc-name', 'Name', 'text', { value: state.me.display_name || '', autocomplete: 'name' });
  const [cl, cur] = field('acc-cur', 'Current password', 'password', { autocomplete: 'current-password' });
  const [pl, next] = field('acc-new', 'New password', 'password', { autocomplete: 'new-password', placeholder: 'At least 8 characters' });
  const note = h('p', { class: 'result when', role: 'status' });
  return [h('form', { class: 'settings-form form', 'aria-label': 'Your account', onsubmit: async (e) => {
    e.preventDefault();
    const body = { display_name: name.value.trim() };
    if (next.value) { body.current_password = cur.value; body.new_password = next.value; }
    const r = await api('/users/me', { method: 'PATCH', body });
    const out = await r.json().catch(() => ({}));
    note.textContent = r.ok ? 'Saved.' : (out.message || out.error || 'Could not save.');
    if (r.ok) {
      state.me.display_name = body.display_name; cur.value = ''; next.value = '';
      for (const el of document.querySelectorAll('.who')) el.textContent = body.display_name;
    }
  } },
  h('p', { class: 'when' }, `You sign in as ${state.me.username}. That stays the same, so you keep your agents.`),
  nl, name, cl, cur, pl, next,
  h('p', { class: 'when' }, 'Leave the password boxes empty to change only your name.'),
  h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Save')), note)];
}

function chat() {
  const note = h('p', { class: 'when appearance-note', role: 'status' });
  const saved = async (p) => { note.textContent = (await p) ? 'Saved: it follows you to your other devices.' : 'Used here, but could not be saved just now.'; };
  const panel = getPref('panel', 'slide');
  const choice = (value, label, help) => h('label', { class: 'appearance-mode choice' },
    h('input', { type: 'radio', name: 'panel', value, checked: panel === value, onchange: () => saved(setPref('panel', value)) }),
    h('span', {}, h('strong', {}, label), h('span', { class: 'when block' }, help)));
  const toggle = (key, label, help) => h('label', { class: 'appearance-mode choice' },
    h('input', { type: 'checkbox', checked: getPref(key, false), onchange: (e) => saved(setPref(key, e.target.checked)) }),
    h('span', {}, h('strong', {}, label), h('span', { class: 'when block' }, help)));
  return [
    h('section', { 'aria-label': 'Conversations panel' }, heading('Conversations panel (desktop)'),
      h('div', { class: 'settings-choices', role: 'radiogroup', 'aria-label': 'Conversations panel' },
        choice('slide', 'Slides away', 'Shows for a moment when Chat opens, then tucks into a tab on the right.'),
        choice('open', 'Stays open', 'Always beside the conversation; hide it with its own button.'))),
    h('section', { 'aria-label': 'In each conversation' }, heading('In each conversation'),
      h('div', { class: 'settings-choices' },
        toggle('thinking', 'Show thinking', "The agent's reasoning and each tool it used, under its reply."),
        toggle('jobs', 'Show scheduled jobs', 'Conversations the agent had on its own, like the morning briefing, in the list.')),
      note),
  ];
}

function matrix() {
  return [h('section', { 'aria-label': 'Matrix device' }, heading('This browser and Matrix'),
    h('p', { class: 'none' }, 'Soon, chat here will be your Element conversation itself. You will connect this browser once '
      + 'with your Matrix password and your recovery key, and see or sign out your devices here (story M1).'))];
}

export async function settingsView(root, [section], state) {
  const isAdmin = state.me.role === 'admin';
  const shown = SECTIONS.filter((s) => !s.admin || isAdmin);
  const id = shown.some((s) => s.id === section) ? section : 'appearance';
  const body = h('div', { class: 'settings-body' });
  let done = null;
  const tabs = h('nav', { class: 'settings-tabs', 'aria-label': 'Settings sections' },
    shown.map((s) => h('a', { href: `#/settings/${s.id}`, class: 'settings-tab', 'aria-current': s.id === id ? 'page' : null }, s.name)));
  root.replaceChildren(h('div', { class: 'page settings' },
    h('header', {}, h('h1', {}, 'Settings')),
    h('div', { class: 'settings-layout' }, tabs, body)));
  if (id === 'appearance') body.replaceChildren(...appearance());
  else if (id === 'account') body.replaceChildren(...account(state));
  else if (id === 'chat') body.replaceChildren(...chat());
  else if (id === 'matrix') body.replaceChildren(...matrix());
  else if (id === 'updates') done = await updatesView(body, [], state, true);
  return () => { if (done) done(); };
}
