// Settings (ST1): one screen for everything you set for yourself. Sections on the left (a row of
// tabs on a phone): Appearance, Your account, Chat, Matrix device, and Updates for whoever
// manages Logos. Each section is its own address (#/settings/devices…), so it can be linked to.
import { h, api, getJSON, when, loading, revealable, ask } from './util.js';
import * as mx from './mx.js';
import { lookControls, SWATCHES, setMine, myColours, setAgentColour, agentColour, setMotion, setAnimations } from './appearance.js';
import { getPref, setPref } from './prefs.js';
import { updatesView } from './updates.js';
import * as push from './push.js';

const SECTIONS = [
  { id: 'appearance', name: 'Look', full: 'Appearance' },
  { id: 'account', name: 'Account', full: 'Your account' },
  { id: 'matrix', name: 'Devices', full: 'This device and your chats' },
  { id: 'notifications', name: 'Phone alerts', full: 'Notifications on this phone' },
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
      h('label', { class: 'swatch-pick', title: 'Any colour' }, h('span', { class: 'btn-words' }, 'Any colour'),
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
    // Agents you cannot recolour: one line of their dots, set by their owners.
    const others = d.others || [];
    const fixed = others.length ? [h('div', { class: 'colour-row' },
      h('div', { class: 'who' }, h('strong', {}, 'Other agents'), h('span', { class: 'when' }, 'Set by their owners')),
      h('div', { class: 'swatches fixed-list' }, others.map((a) => h('span', { class: 'fixed-agent', title: a.display_name },
        agentColour(a.name) ? h('span', { class: 'swatch-fixed', style: `background:${agentColour(a.name)}` })
          : h('span', { class: `dot ${a.name}`, style: 'width:20px;height:20px' }), a.display_name.replace(/\s+agent$/i, '')))))] : [];
    agentsBox.replaceChildren(...(d.agents || []).map((a) => colourRow(a.display_name,
      a.shared ? 'Shared: either of you can change it' : 'Everyone sees this colour', SWATCHES.agent, agentColour(a.name),
      (c) => { setAgentColour(a.name, c); say(cNote, api(`/api/v2/agents/${a.name}/colour`, { method: 'PUT', body: { colour: c } }).then((r) => r.ok)); },
      false)), ...fixed);
  }).catch(() => agentsBox.replaceChildren(h('p', { class: 'none' }, 'The agents could not be listed just now.')));
  const motion = getPref('motion', 'calm'), names = getPref('names', 'hover');
  const pickMotion = (v) => { setMotion(v, null); say(mNote, setPref('motion', v)); };
  const pickNames = (v) => { setMotion(null, v); say(mNote, setPref('names', v)); };
  const anim = getPref('animations', 'on');
  const aNote = savedNote();
  const pickAnim = (v) => { setAnimations(v); say(aNote, setPref('animations', v)); };
  const MOTION_SAYS = { full: 'The agents wander around the Agents screen, breathe, and cycle their colour while they think.',
    calm: 'The agents drift slowly and breathe gently.', still: 'The agents stay where they are and do not move.' };
  const ANIM_SAYS = { on: 'Screens slide in when you swipe, content fades in, and loading shows soft placeholders.',
    off: 'Screens change at once, with nothing sliding, fading or shimmering.' };
  const motionSays = h('p', { class: 'when' }, MOTION_SAYS[motion] || '');
  const animSays = h('p', { class: 'when' }, ANIM_SAYS[anim] || '');
  return [
    h('section', { 'aria-label': 'Look' }, heading('Look'), looksBox),
    h('section', { 'aria-label': 'Light or dark' }, heading('Light or dark'), modeBox, note),
    // Colours are set once: folded away behind one line (Greg, 21 Nov).
    h('details', { class: 'colours-more' }, h('summary', {}, h('span', {}, 'Colours'), h('span', { class: 'when' }, 'Yours and the agents\'')),
    h('section', { 'aria-label': 'Your colours' }, heading('Your colours'),
      colourRow('Primary and glow', 'Buttons, links, the glow', SWATCHES.primary, mine.primary, pickMine('primary'), true),
      colourRow('Attention', 'Anything waiting on you, and its badge', SWATCHES.attention, mine.attention, pickMine('attention'), true),
      h('p', { class: 'when' }, 'Colours may be adjusted slightly to stay readable.')),
    h('section', { 'aria-label': 'Agent colours' }, heading('Agent colours'), agentsBox, cNote)),
    // Two separate choices (Greg, 22 Nov): how much the agents move, and whether the screens animate.
    h('section', { 'aria-label': 'Agent movement' }, heading('Agent movement'),
      h('div', { class: 'appearance-modes', role: 'radiogroup', 'aria-label': 'Agent movement' },
        [['full', 'Full'], ['calm', 'Calm'], ['still', 'Still']]
          .map(([v, label]) => h('label', { class: 'appearance-mode', title: MOTION_SAYS[v] },
            h('input', { type: 'radio', name: 'motion', value: v, checked: motion === v,
              onchange: () => { motionSays.textContent = MOTION_SAYS[v]; pickMotion(v); } }), label))),
      motionSays),
    h('section', { 'aria-label': 'Animations' }, heading('Animations'),
      h('div', { class: 'appearance-modes', role: 'radiogroup', 'aria-label': 'Animations' },
        [['on', 'On'], ['off', 'Off']]
          .map(([v, label]) => h('label', { class: 'appearance-mode', title: ANIM_SAYS[v] },
            h('input', { type: 'radio', name: 'animations', value: v, checked: anim === v,
              onchange: () => { animSays.textContent = ANIM_SAYS[v]; pickAnim(v); } }), label))),
      animSays, aNote),
    h('section', { 'aria-label': 'Agent names', class: 'wide-only' }, heading('Agent names on their marks'),
      h('div', { class: 'appearance-modes', role: 'radiogroup', 'aria-label': 'Agent names' },
        [['hover', 'On hover', 'Shown when you point at a mark (always shown on a phone)'], ['always', 'Always', 'Each mark has its name and mood under it']]
          .map(([v, label, tip]) => h('label', { class: 'appearance-mode', title: tip },
            h('input', { type: 'radio', name: 'names', value: v, checked: names === v, onchange: () => pickNames(v) }), label))), mNote),
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
  return [heading('Your account'), h('form', { class: 'settings-form form', 'aria-label': 'Your account', onsubmit: async (e) => {
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
  h('p', { class: 'when' }, `Username: ${state.me.username} (it never changes)`),
  nl, name, cl, revealable(cur), pl, revealable(next),
  h('p', { class: 'when' }, 'Leave blank to keep your password.'),
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
    h('section', { 'aria-label': 'Conversations panel', class: 'wide-only' }, heading('Chat: conversations panel (desktop)'),
      h('div', { class: 'settings-choices', role: 'radiogroup', 'aria-label': 'Conversations panel' },
        choice('slide', 'Slides away', 'Shows for a moment when Chat opens, then tucks into a tab on the right.'),
        choice('open', 'Stays open', 'Always beside the conversation; hide it with its own button.'))),
    h('section', { 'aria-label': 'In each conversation' }, heading('Chat: in each conversation'),
      h('div', { class: 'settings-choices' },
        // "Show scheduled jobs" is gone (21 Nov): scheduled messages arrive in the chat itself now.
        toggle('thinking', 'Show thinking', "The agent's reasoning and each tool it used, under its reply.")),
      note),
  ];
}

// M1: connect this browser as one of your Matrix devices, unlock it with the recovery key, and see
// or sign out your devices. Two steps (Greg, 21 Nov): password, then recovery key.
function matrix(state, redraw) {
  const box = h('div', { class: 'settings-body-inner' }, loading());
  const me = state.me.matrix_id;
  if (!me) return [h('p', { class: 'none' }, 'Chat is not set up on this Logos.')];
  const steps = (n) => h('div', { class: 'mx-steps', role: 'list' }, ['Connect', 'Unlock', 'Done'].map((t, i) =>
    h('span', { role: 'listitem', class: `mx-step${i === n ? ' now' : ''}${i < n ? ' done' : ''}` }, `${i < n ? '✓ ' : ''}${t}`)));
  const field = (id, label, input) => [h('label', { for: id, class: 'field-label' }, label), input];
  const note = () => h('p', { class: 'when', role: 'status' });

  function stepConnect() {
    const pass = h('input', { id: 'mx-pass', type: 'password', autocomplete: 'current-password' });
    const msg = note();
    const go = h('button', { type: 'submit', class: 'btn' }, 'Connect');
    box.replaceChildren(steps(0), h('form', { class: 'settings-form', onsubmit: async (e) => {
      e.preventDefault();
      go.disabled = true; msg.textContent = 'Connecting… the first time this browser fetches about 2 MB.';
      try { await mx.connect(me, pass.value); pass.value = ''; show(); }
      catch (err) { msg.textContent = err.httpStatus === 403 ? 'That password was not accepted.' : `Could not connect: ${err.message}`; go.disabled = false; }
    } },
    h('p', {}, 'Connect this device to your chats: the same encrypted messages as on your other devices.'),
    ...field('mx-user', 'Your chat account', h('input', { id: 'mx-user', type: 'text', value: me, readonly: true })),
    ...field('mx-pass', 'Chat password (used once, never kept)', revealable(pass)),
    h('div', { class: 'row' }, go), msg));
  }

  function stepUnlock() {
    const key = h('input', { id: 'mx-key', type: 'text', autocomplete: 'off', spellcheck: 'false', class: 'mono',
      placeholder: 'EsTc 7Q2b Wd9P …' });
    const msg = note();
    const go = h('button', { type: 'submit', class: 'btn' }, 'Unlock');
    box.replaceChildren(steps(1), h('form', { class: 'settings-form', onsubmit: async (e) => {
      e.preventDefault();
      go.disabled = true; msg.textContent = 'Unlocking, then verifying this browser… this can take up to half a minute.';
      try {
        const out = await mx.unlock(key.value); key.value = '';
        if (out.verified) show(); else { msg.textContent = 'The key was accepted but this device is not confirmed yet. Try again, or confirm it from another of your devices that is already connected.'; go.disabled = false; }
      } catch (err) { msg.textContent = /key|decode|checksum|parity/i.test(err.message) ? 'That recovery key was not accepted. Check it and try again.' : `Could not unlock: ${err.message}`; go.disabled = false; }
    } },
    h('p', {}, 'Enter your recovery key: the long code saved when your chats were set up (Greg has a copy if you cannot find yours). It proves this device is yours and unlocks your earlier messages. It is used once and not kept.'),
    ...field('mx-key', 'Recovery key', key),
    h('div', { class: 'row' }, go), msg,
    h('p', { class: 'when' }, 'Or confirm it from another of your devices that is already connected: it shows up there as a new device.')));
  }

  // Done (Greg, 22 Nov: the person need not know the plumbing): one line saying all is well; the
  // devices folded away, with the old ones marked and signed out together.
  async function stepDone() {
    const list = h('div', { class: 'rows' }, loading());
    const msg = note();
    const fold = h('details', { class: 'mx-devices' }, h('summary', {}, 'Your devices'), list);
    box.replaceChildren(
      h('div', { class: 'mx-ok', title: `This browser is a verified device of ${me}.` }, '✓ This device is connected and secure'),
      fold, msg);
    const devs = (await mx.devices()).sort((a, b) => Number(b.here) - Number(a.here) || b.lastSeen - a.lastSeen);
    const plain = (n) => String(n || 'A device').replace(/^Logos \((.*)\)$/, 'Logos · $1');
    // Old: not used for three days, not verified (agents ignore it anyway), or an older copy of a device
    // that is listed again (a browser connected twice).
    const week = Date.now() - 3 * 86400000;
    const old = (d, i) => !d.here && (!d.verified || !d.lastSeen || d.lastSeen < week
      || devs.slice(0, i).some((x) => plain(x.name) === plain(d.name)));
    const olds = devs.filter(old);
    fold.querySelector('summary').textContent = `Your devices (${devs.length})${olds.length ? ` · ${olds.length} not used lately` : ''}`;
    const signOut = async (targets, button) => {
      const pw = await ask({ title: targets.length === 1 ? `Sign out "${plain(targets[0].name)}"?` : `Sign out ${targets.length} old devices?`,
        text: targets.length === 1 ? 'It will need to sign in again to read your chats.' : targets.map((d) => plain(d.name)).join(', '),
        yes: 'Sign out', danger: true, password: 'Your chat password' });
      if (!pw) return;
      button.disabled = true;
      let done = 0;
      for (const d of targets) {
        try { await mx.signOut(d.id, pw); done += 1; }
        catch (err) { msg.textContent = `Could not sign out "${plain(d.name)}": ${err.message}`; break; }
      }
      if (done) msg.textContent = `Signed out ${done} device${done === 1 ? '' : 's'}.`;
      stepDone();
    };
    list.replaceChildren(...devs.map((d, i) => h('div', { class: 'row-item mx-device' },
      h('span', { class: 'row-main' }, d.here ? `${plain(d.name)} · this device` : plain(d.name)),
      h('span', { class: `row-meta ${old(d, i) ? 'warn' : 'ok'}` },
        [d.here ? 'in use now' : d.lastSeen ? `last used ${when(d.lastSeen / 1000)}` : 'never used',
          !d.verified ? 'not trusted: agents ignore it' : '', old(d, i) && d.verified ? 'not used lately' : ''].filter(Boolean).join(' · ')),
      d.here ? null : h('button', { type: 'button', class: 'linkish mx-signout', onclick: (e) => signOut([d], e.target) }, 'Sign out'))),
      ...(olds.length ? [h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn quiet', onclick: (e) => signOut(olds, e.target) },
        `Sign out ${olds.length} old device${olds.length === 1 ? '' : 's'}`))] : []),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn danger', onclick: async () => {
        if (!await ask({ title: 'Disconnect this device?', text: 'It signs out of your chats here and forgets its keys. You can connect it again with your password and recovery key.', yes: 'Disconnect', danger: true })) return;
        await mx.disconnect(); show();
      } }, 'Disconnect this device')));
    if (olds.length) fold.open = true;
  }

  async function show() {
    if (!mx.saved()) return stepConnect();
    box.replaceChildren(loading());
    try { (await mx.isVerified()) ? stepDone() : stepUnlock(); }
    catch (e) { box.replaceChildren(h('div', { class: 'notice' }, `This device could not connect to your chats: ${e.message}`),
      h('button', { type: 'button', class: 'btn quiet', onclick: async () => { await mx.disconnect(); show(); } }, 'Start again')); }
  }
  show();
  return [h('section', { 'aria-label': 'This device' }, heading('This device'), box)];
}

// Phone notifications (F37): on or off for this device, a test, and your other devices that have them.
function notifications(state) {
  const box = h('div', { class: 'settings-body-inner' }, loading());
  const msg = h('p', { class: 'when', role: 'status' });
  async function show() {
    if (!state.me.matrix_id) { box.replaceChildren(h('p', { class: 'none' }, 'Chat is not set up on this Logos.')); return; }
    if (!mx.saved()) {
      box.replaceChildren(h('p', {}, 'Connect this device to your chats first (Settings › Devices): notifications come from them.'),
        h('a', { href: '#/settings/matrix', class: 'btn' }, 'Settings › Devices'));
      return;
    }
    const st = await push.status().catch(() => 'off');
    const info = await getJSON('/api/v2/push').catch(() => ({ devices: [] }));
    const busy = (b) => { for (const el of box.querySelectorAll('button')) el.disabled = b; };
    const act = (label, fn, cls = 'btn') => h('button', { type: 'button', class: cls, onclick: async () => {
      busy(true); msg.textContent = '';
      try { await fn(); } catch (e) { msg.textContent = e.message; }
      busy(false); show();
    } }, label);
    const words = {
      on: 'On for this phone. Your agents\' messages, and the people in your groups, arrive as notifications: who wrote, not what (it stays encrypted).',
      off: 'Off for this phone. Turn them on to hear about new messages without opening Logos.',
      blocked: 'Notifications are blocked for this site. Allow them in the browser\'s site settings, then come back.',
      unsupported: 'This browser cannot show notifications. On Android, open Logos from its icon on your home screen (in Chrome).',
    }[st];
    const others = (info.devices || []).filter((d) => d.pushkey !== (localStorage.getItem('logos.push.pushkey') || ''));
    box.replaceChildren(h('p', {}, words),
      h('div', { class: 'row' },
        st === 'on' ? act('Turn off', () => push.turnOff(), 'btn quiet') : st === 'off' ? act('Turn on', () => push.turnOn()) : null,
        st === 'on' ? act('Send a test', async () => { msg.textContent = (await push.test()) ? 'Sent: it should appear in a few seconds.' : 'The test could not be sent.'; }, 'btn quiet') : null),
      ...(others.length ? [h('p', { class: 'when' }, `Also on: ${others.map((d) => d.device || 'another device').join(', ')}.`)] : []),
      msg);
  }
  show();
  return [h('section', { 'aria-label': 'Phone alerts' }, heading('Phone alerts'), box)];
}

export async function settingsView(root, [section], state) {
  const isAdmin = state.me.role === 'admin';
  const shown = SECTIONS.filter((s) => !s.admin || isAdmin);
  const id = shown.some((s) => s.id === section) ? section : 'appearance';
  const body = h('div', { class: 'settings-body' });
  let done = null;
  const tabs = h('nav', { class: 'settings-tabs', 'aria-label': 'Settings sections' },
    shown.map((s) => h('a', { href: `#/settings/${s.id}`, class: 'settings-tab', title: s.full || s.name, 'aria-current': s.id === id ? 'page' : null }, s.name)));
  root.replaceChildren(h('div', { class: 'page settings' },
    h('header', {}, h('h1', {}, 'Settings')),
    h('div', { class: 'settings-layout' }, tabs, body)));
  // Chat's two options live under Look now (Greg, 22 Nov: a tab for one checkbox on a phone); old links land there.
  if (id === 'appearance') body.replaceChildren(...appearance(), ...chat());
  else if (id === 'account') body.replaceChildren(...account(state));
  else if (id === 'matrix') body.replaceChildren(...matrix(state));
  else if (id === 'notifications') body.replaceChildren(...notifications(state));
  else if (id === 'updates') done = await updatesView(body, [], state, true);
  return () => { if (done) done(); };
}
