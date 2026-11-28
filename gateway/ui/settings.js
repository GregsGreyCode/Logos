// Settings (ST1): one screen for everything you set for yourself. Sections on the left (a row of
// tabs on a phone): Appearance, Your account, Chat, Matrix device, and Updates for whoever
// manages Logos. Each section is its own address (#/settings/devices…), so it can be linked to.
import { h, api, getJSON, when, loading, revealable, ask, icon, ICONS, emptyState, subpage, sheet, adminName, isAdmin } from './util.js';
import { moods, MOODS } from './agents-void.js';
import * as mx from './mx.js';
import { lookControls, SWATCHES, setMine, myColours, setAgentColour, agentColour, setMotion, setAnimations, LOOKS, current as currentLook } from './appearance.js';
import { getPref, setPref } from './prefs.js';
import { reset as resetTips } from './tips.js';
import { updatesView } from './updates.js';
import * as push from './push.js';
import { createRecorder } from './voice.js';
import { householdWordsPage, householdWordsSummary } from './household-words.js';

// F45 (Greg, 23 Nov: "more inspired by Element X's settings"): a list, not a row of tabs. Grouped,
// each row with its icon and what it is set to now; on a phone a row opens its page (with a back
// arrow), on a wider screen the list stays on the left beside the page.
// One name each (28 Nov): the row's name is its page's title.
const SECTIONS = [
  { id: 'appearance', name: 'Look', icon: 'palette', group: 'You' },
  { id: 'notifications', name: 'Phone alerts', icon: 'bell', group: 'You' },
  { id: 'email', name: 'Email', icon: 'mail', group: 'You' },
  { id: 'speaker', name: 'Speaker', icon: 'speaker', group: 'You' },
  { id: 'voice', name: 'Your voice', icon: 'mic', group: 'You' },
  { id: 'words', name: 'Household words', icon: 'chat', group: 'You' },     // names speech-to-text gets wrong (28 Nov)
  { id: 'tips', name: 'Tips', icon: 'bulb', group: 'You' },
  { id: 'matrix', name: 'Chats & devices', icon: 'shield', group: 'This device' },
  { id: 'updates', name: 'Updates', icon: 'update', group: 'Logos', admin: true },
  { id: 'metrics', name: 'Metrics', icon: 'chart', group: 'Logos', admin: true },
  { id: 'account', name: 'Your account', icon: 'user', group: null },
  // In the Agents group (drawn once the agents are known), and linked from People.
  { id: 'sites', name: 'Sites', icon: 'globe', group: null },
];
// Screens of their own, listed here too so a phone reaches them from Settings (admin only).
const ELSEWHERE = [
  { href: '#/people', name: 'People', icon: 'people' },
  { href: '#/models', name: 'Models', icon: 'chip' },
  { href: '#/advisor', name: 'Claude Advisor', icon: 'advisor' },
];
const WIDE = '(min-width: 820px)';

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
  const motion = getPref('motion', 'calm'), names = getPref('names', 'always');
  const pickMotion = (v) => { setMotion(v, null); say(mNote, setPref('motion', v)); };
  const pickNames = (v) => { setMotion(null, v); say(mNote, setPref('names', v)); };
  const anim = getPref('animations', 'on');
  const aNote = savedNote();
  const tNote = savedNote();
  const pickAnim = (v) => { setAnimations(v); say(aNote, setPref('animations', v)); };
  const MOTION_SAYS = { full: 'The agents wander around the Agents screen, breathe, and cycle their colour while they think.',
    calm: 'The agents drift slowly and breathe gently.', still: 'The agents stay where they are and do not move.' };
  const ANIM_SAYS = { on: 'Screens slide in when you swipe, content fades in, and loading shows soft placeholders.',
    off: 'Screens change at once, with nothing sliding, fading or shimmering.' };
  const motionSays = h('p', { class: 'when' }, MOTION_SAYS[motion] || '');
  const animSays = h('p', { class: 'when' }, ANIM_SAYS[anim] || '');
  return [
    h('section', { 'aria-label': 'Look' }, looksBox),
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

function tips() {
  const note = savedNote();
  // F41: the first-visit tips, once more (each screen shows its tips again next time it opens).
  return [h('section', { 'aria-label': 'Tips' },
    h('p', { class: 'when' }, 'Each screen shows a few tips the first time you open it.'),
    h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn quiet', onclick: () => say(note, resetTips()) }, 'Show the tips again')), note)];
}

// Prometheus (F42, Greg 23 Nov): off until switched on here. The token is shown once; only its hash is kept.
function metricsPage() {
  const box = h('div', { class: 'settings-body-inner' }, loading());
  const msg = h('p', { class: 'when', role: 'status' });
  const put = async (body) => {
    const r = await api('/api/v2/metrics', { method: 'PUT', body }).catch(() => null);
    return r && r.ok ? r.json() : null;
  };
  const config = (token) => `scrape_configs:
  - job_name: logos
    metrics_path: /metrics
    scheme: ${location.protocol.replace(':', '')}
    authorization:
      credentials: ${token}
    static_configs:
      - targets: ['${location.host}']`;
  const copy = (text, what) => navigator.clipboard ? h('button', { type: 'button', class: 'btn quiet small', onclick: async () => {
    try { await navigator.clipboard.writeText(text); msg.textContent = `${what} copied.`; } catch (e) { msg.textContent = 'Could not copy: select it instead.'; }
  } }, `Copy ${what}`) : '';
  function showOff() {
    box.replaceChildren(emptyState({ icon: 'chart', heading: 'Metrics are off',
      text: 'Turn them on to let Prometheus or Grafana at home read how your agents are doing: which are running, how many messages each sent today, and whether the AI computer answers.',
      action: { label: 'Turn on', admin: true, ask: 'to turn them on', onclick: async () => {
        msg.textContent = 'Turning it on…';
        const d = await put({ enabled: true });
        if (!d) { msg.textContent = 'Could not turn it on just now.'; return; }
        msg.textContent = ''; showOn(d.token);
      } } }), msg, about());
  }
  function about() {
    return h('details', { class: 'colours-more' }, h('summary', {}, h('span', {}, 'What it shares'), h('span', { class: 'when' }, 'Numbers only')),
      h('p', { class: 'when' }, 'Agent names, counts, times, and 0 or 1 for "is it running". Never a message, a person, a room or a key. Prometheus must send the token below; without it the address does not exist.'),
      h('p', { class: 'when mono' }, 'logos_agent_up · logos_agent_messages_today · logos_agent_messages_total · logos_agent_last_message_timestamp_seconds · logos_model_up · logos_widget_needs · logos_info'));
  }
  function showOn(token) {
    const parts = [h('p', {}, token ? 'On. Copy this now: it is shown once, and Logos keeps only a scrambled copy.' : 'On. The token was shown when you turned it on; Logos keeps only a scrambled copy of it.')];
    if (token) {
      parts.push(h('p', { class: 'mono w-token' }, token), copy(token, 'token'),
        h('p', { class: 'when' }, 'For Prometheus (prometheus.yml):'), h('pre', { class: 'w-config' }, config(token)), copy(config(token), 'config'));
    }
    parts.push(h('p', { class: 'when' }, 'Lost it? Make a new token: the old one stops working.'),
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn quiet', onclick: async () => {
          if (!await ask({ title: 'Make a new token?', text: 'Whatever uses the old token stops getting numbers until you give it the new one.', yes: 'New token' })) return;
          const d = await put({ enabled: true, rotate: true });
          if (d) showOn(d.token); else msg.textContent = 'Could not make a new token just now.';
        } }, 'New token'),
        h('button', { type: 'button', class: 'btn quiet danger', onclick: async () => {
          if (!await ask({ title: 'Turn metrics off?', text: 'The address stops existing and the token is forgotten.', yes: 'Turn off', danger: true })) return;
          if (await put({ enabled: false })) showOff(); else msg.textContent = 'Could not turn it off just now.';
        } }, 'Turn off')), msg, about());
    box.replaceChildren(...parts);
  }
  getJSON('/api/v2/metrics').then((d) => (d.enabled ? showOn(null) : showOff()))
    .catch(() => box.replaceChildren(emptyState({ icon: 'lock', heading: 'Only whoever manages Logos can set this', action: { admin: true } })));
  return [box];
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
    h('section', { 'aria-label': 'Chats panel', class: 'wide-only' }, heading('Chat: chats panel (desktop)'),
      h('div', { class: 'settings-choices', role: 'radiogroup', 'aria-label': 'Chats panel' },
        choice('slide', 'Slides away', 'Shows for a moment when a chat opens, then tucks away; the Chats button brings it back.'),
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
  if (!me) return [emptyState({ icon: 'chat', heading: 'Chat is not set up on this Logos', text: 'Chats come from Element X, through the chat server Logos sets up.', action: { admin: true, ask: 'to set it up', label: 'See People', href: '#/people' } })];
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
    const disconnect = h('button', { type: 'button', class: 'btn danger mx-disconnect', onclick: async () => {
      if (!await ask({ title: 'Disconnect this device?', text: 'It signs out of your chats here and forgets its keys. You can connect it again with your password and recovery key.', yes: 'Disconnect', danger: true })) return;
      await mx.disconnect(); show();
    } }, 'Disconnect this device');
    const thisCard = h('section', { class: 'mx-card', 'aria-label': 'This device' },
      h('div', { class: 'mx-card-top' },
        h('span', { class: 'mx-ok-dot', 'aria-hidden': 'true' }, '✓'),
        h('div', { class: 'mx-card-text' }, h('strong', {}, 'Connected and secure'),
          h('span', { class: 'when' }, `This browser is a verified device of ${me}.`))),
      h('div', { class: 'mx-card-actions' }, disconnect,
        h('span', { class: 'when' }, 'Signs out of your chats here and forgets its keys. You can connect it again later.')));
    const head = h('div', { class: 'mx-list-head' }, h('div', { class: 'mx-card-text' }, h('h3', { class: 'mx-h' }, 'Your devices'), h('span', { class: 'when mx-count' }, '')));
    const fold = h('section', { class: 'mx-devices', 'aria-label': 'Your devices' }, head, list);
    box.replaceChildren(thisCard, fold, msg);
    const devs = (await mx.devices()).sort((a, b) => Number(b.here) - Number(a.here) || b.lastSeen - a.lastSeen);
    const plain = (n) => String(n || 'A device').replace(/^Logos \((.*)\)$/, 'Logos · $1');
    // Old: not used for three days, not verified (agents ignore it anyway), or an older copy of a device
    // that is listed again (a browser connected twice).
    const week = Date.now() - 3 * 86400000;
    const old = (d, i) => !d.here && (!d.verified || !d.lastSeen || d.lastSeen < week
      || devs.slice(0, i).some((x) => plain(x.name) === plain(d.name)));
    const olds = devs.filter(old);
    head.querySelector('.mx-count').textContent = `${devs.length} device${devs.length === 1 ? '' : 's'}${olds.length ? ` · ${olds.length} not used lately` : ''}`;
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
    list.replaceChildren(...devs.map((d, i) => h('div', { class: `row-item mx-device${d.here ? ' here' : ''}` },
      h('span', { class: 'row-main' }, plain(d.name), d.here ? h('span', { class: 'mx-here' }, 'This device') : null),
      h('span', { class: `row-meta ${old(d, i) ? 'warn' : 'ok'}` },
        [d.here ? 'in use now' : d.lastSeen ? `last used ${when(d.lastSeen / 1000)}` : 'never used',
          !d.verified ? 'not trusted: agents ignore it' : '', old(d, i) && d.verified ? 'not used lately' : ''].filter(Boolean).join(' · ')),
      d.here ? null : h('button', { type: 'button', class: 'btn quiet small mx-signout', onclick: (e) => signOut([d], e.target) }, 'Sign out'))));
    if (olds.length) head.append(h('button', { type: 'button', class: 'btn quiet small mx-clean', onclick: (e) => signOut(olds, e.target) },
      `Sign out ${olds.length} old device${olds.length === 1 ? '' : 's'}`));
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
    if (!state.me.matrix_id) { box.replaceChildren(emptyState({ icon: 'bell', heading: 'Chat is not set up on this Logos', text: 'Phone alerts come from your chats.', action: { admin: true, ask: 'to set it up', label: 'See People', href: '#/people' } })); return; }
    if (!mx.saved()) {
      box.replaceChildren(emptyState({ icon: 'bell', heading: 'Connect this device to your chats first', text: 'Phone alerts come from them.',
        action: { label: 'Chats & devices', href: '#/settings/matrix' } }));
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
      on: 'On for this phone. Your agents\' messages, and the people in your groups, arrive as notifications.',
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
  // What an alert shows (Greg, 23 Nov): who wrote it, or nothing but "Logos · New message" (nothing about who
  // or which chat on the lock screen). Messages are end-to-end encrypted: an alert never has their words.
  const choice = getPref('alerts', 'who');
  const cNote = savedNote();
  const SAYS = { who: 'Shows who wrote, or the group, and for an agent\'s reply its first words: "Household agent · The green bin goes out tonight". A person\'s message says only "New message".',
    private: 'Shows only "Logos · New message", with no name or group on your lock screen.' };
  const says = h('p', { class: 'when' }, SAYS[choice]);
  const privacy = h('section', { 'aria-label': 'What an alert shows' }, heading('What an alert shows'),
    h('div', { class: 'appearance-modes', role: 'radiogroup', 'aria-label': 'What an alert shows' },
      [['who', 'Who wrote it'], ['private', 'Nothing']].map(([v, label]) => h('label', { class: 'appearance-mode', title: SAYS[v] },
        h('input', { type: 'radio', name: 'alerts', value: v, checked: choice === v,
          onchange: () => { says.textContent = SAYS[v]; say(cNote, setPref('alerts', v)); } }), label))),
    says, cNote);
  return [h('section', { 'aria-label': 'Phone alerts' }, box), privacy];
}

// F40 Email (Greg, 23 Nov): connect your own mailbox here, no server commands. Gmail with an app password
// (links straight to Google's pages); Outlook.com with Microsoft's sign-in code, through the one Logos app
// that whoever manages Logos adds once (walked through below). Only your own mailboxes; read only.
// F43 (27 Nov): several mailboxes, Gmail and Outlook mixed: one card each, and "Add another mailbox".
function email(body) {
  let alive = true, timer = null, adding = false;
  const openPrivate = new Set();            // the mailboxes whose private senders are unfolded
  const box = h('div', { class: 'mail-box' }, loading('rows', 2));
  const msg = h('p', { class: 'when', role: 'status' });
  body.replaceChildren(h('section', { 'aria-label': 'Email' },
    h('p', { class: 'when' }, 'Your agent can read your email to tell you what needs a reply and find things for you. '
      + 'It can never send, delete or move mail, and no other agent can see it.'), box, msg));
  const step = (n, ...kids) => h('li', { class: 'mail-step' }, h('span', { class: 'mail-n', 'aria-hidden': 'true' }, String(n)), h('div', {}, ...kids));
  const ext = (href, text) => h('a', { href, target: '_blank', rel: 'noopener' }, text);
  const errOf = async (r) => { const d = await r.json().catch(() => ({})); return d.message || d.error || 'That did not work.'; };

  async function load(keepOpen = false) {
    clearTimeout(timer);
    if (!keepOpen) openPrivate.clear();
    let d;
    try { d = await getJSON('/api/v2/mail'); } catch (e) { box.replaceChildren(h('p', { class: 'none' }, 'Email settings could not be loaded just now.')); return; }
    if (!alive) return;
    if (!d.agent) { box.replaceChildren(emptyState({ icon: 'mail', heading: 'Your agent does not read email yet', text: 'It is set up for each person, with their own agent.', action: { admin: true, ask: 'to turn it on', label: 'Set it up', href: '#/people' } })); return; }
    if (d.signin && d.signin.state === 'waiting') return showCode(d);
    const boxes = d.mailboxes || [];
    if (boxes.length && !adding) return connected(d, boxes);
    choose(d, boxes.length > 0);
  }
  const back = () => { adding = false; load(); };

  // The service's ISO time, as everywhere else: today the time alone, else the day and time.
  const checked = (iso) => { const t = Date.parse(iso); return Number.isFinite(t) ? when(t / 1000).toLowerCase() : String(iso); };
  function card(m, many) {
    const hl = m.health;
    const state = !hl ? 'Starting to read it…' : hl.error ? `The last refresh failed: ${hl.error}` : hl.synced ? `Up to date, checked ${checked(hl.synced)}` : 'Copying your recent mail…';
    const sender = h('input', { type: 'text', placeholder: 'mybank.example', 'aria-label': `A sender to keep private in ${m.address || 'this mailbox'}`,
      autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false' });
    const exclude = async (s, remove = false) => {
      const r = await api('/api/v2/mail/exclude', { method: 'POST', body: { box: m.id, sender: s, remove } });
      msg.textContent = r.ok ? '' : await errOf(r); if (r.ok) load(true);
    };
    // The mailbox and everything about it in one card (Greg, 23 Nov: the senders list below it looked
    // bizarre): who it is and how it is doing, private senders folded away, and Disconnect at its foot.
    // Private senders are kept per mailbox.
    const n = m.exclude.length;
    const priv = h('details', { class: 'mail-private', open: openPrivate.has(m.id) },
      h('summary', {}, h('span', {}, 'Private senders'), h('span', { class: 'when' }, n ? `${n} kept out` : 'None')),
      h('p', { class: 'when' }, `Your agent never sees email from these${many ? ' in this mailbox' : ''}: a bank or a doctor, say. A whole domain or one address.`),
      n ? h('div', { class: 'mail-chips' }, m.exclude.map((s) => h('span', { class: 'mail-chip' }, s,
        h('button', { type: 'button', 'aria-label': `Let your agent read mail from ${s} again`, onclick: () => exclude(s, true) }, '✕')))) : '',
      h('form', { class: 'mail-add', onsubmit: (e) => { e.preventDefault(); if (sender.value.trim()) exclude(sender.value.trim()); } },
        sender, h('button', { type: 'submit', class: 'btn quiet small' }, 'Add')));
    priv.addEventListener('toggle', () => { if (priv.open) openPrivate.add(m.id); else openPrivate.delete(m.id); });
    const name = m.address || 'Your mailbox';
    return h('div', { class: 'card mail-card', 'data-box': m.id },
      h('div', { class: 'mail-who' }, h('span', { class: `status ${hl && hl.error ? 'down' : 'up'}`, 'aria-hidden': 'true' }),
        h('div', {}, h('strong', {}, name), h('p', { class: 'when' }, `${m.kind === 'gmail' ? 'Gmail' : 'Outlook.com'} · ${state}`))),
      priv,
      h('div', { class: 'mail-foot' }, h('button', { type: 'button', class: 'linkish danger-text', 'aria-label': `Disconnect ${name}`, onclick: async () => {
        if (!await ask({ title: many ? `Disconnect ${name}?` : 'Disconnect your email?',
          text: 'Your agent stops reading it, and Logos forgets the login. Its copy of this mail is removed.', yes: 'Disconnect', danger: true })) return;
        const r = await api(`/api/v2/mail?box=${encodeURIComponent(m.id)}`, { method: 'DELETE' }); msg.textContent = r.ok ? 'Disconnected.' : await errOf(r); load();
      } }, 'Disconnect')));
  }

  function connected(d, boxes) {
    const many = boxes.length > 1;
    box.replaceChildren(h('div', { class: 'mail-list' }, ...boxes.map((m) => card(m, many))),
      h('button', { type: 'button', class: 'btn quiet mail-more', onclick: () => { adding = true; msg.textContent = ''; choose(d, true); } },
        '+ Add another mailbox'));
  }

  function choose(d, more = false) {
    const pick = (kind) => (kind === 'gmail' ? gmail() : outlook(d));
    box.replaceChildren(h('p', {}, more ? 'Which email is the other mailbox?' : 'Which email do you use?'),
      h('div', { class: 'row mail-choose' },
        h('button', { type: 'button', class: 'btn quiet', onclick: () => pick('gmail') }, 'Gmail'),
        h('button', { type: 'button', class: 'btn quiet', onclick: () => pick('outlook') }, 'Outlook.com / Hotmail')),
      more ? h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn quiet', onclick: back }, 'Back')) : '');
  }

  function gmail() {
    const addr = h('input', { id: 'mail-addr', type: 'email', autocomplete: 'email', placeholder: 'you@gmail.com' });
    const pw = h('input', { id: 'mail-pw', type: 'password', autocomplete: 'off', placeholder: 'abcd efgh ijkl mnop' });
    const go = h('button', { type: 'submit', class: 'btn' }, 'Connect');
    box.replaceChildren(h('ol', { class: 'plain mail-steps' },
      step(1, h('strong', {}, 'Turn on 2-Step Verification'), h('p', { class: 'when' }, 'Google only allows app passwords with it on. ', ext('https://myaccount.google.com/signinoptions/twosv', 'Open Google\'s page'))),
      step(2, h('strong', {}, 'Make an app password'), h('p', { class: 'when' }, 'Name it "Logos" and Google shows 16 letters. ', ext('https://myaccount.google.com/apppasswords', 'Open App passwords'))),
      step(3, h('strong', {}, 'Paste it here'),
        h('form', { class: 'form', onsubmit: async (e) => {
          e.preventDefault(); go.disabled = true; msg.textContent = 'Checking with Gmail…';
          const r = await api('/api/v2/mail/gmail', { method: 'POST', body: { address: addr.value.trim(), app_password: pw.value } });
          go.disabled = false;
          if (!r.ok) { msg.textContent = await errOf(r); return; }
          const out = await r.json(); pw.value = ''; adding = false;
          msg.textContent = `Connected: ${out.inbox} emails in your Inbox. Your agent starts reading in a few minutes.`; load();
        } }, h('label', { for: 'mail-addr', class: 'field-label' }, 'Gmail address'), addr,
        h('label', { for: 'mail-pw', class: 'field-label' }, 'App password'), revealable(pw),
        h('div', { class: 'row' }, go, h('button', { type: 'button', class: 'btn quiet', onclick: () => load() }, 'Back'))))),
    h('p', { class: 'when' }, 'Removing the app password in your Google account cuts Logos off at once.'));
  }

  function outlook(d) {
    if (!d.outlook_ready && !d.is_admin) {
      box.replaceChildren(emptyState({ icon: 'mail', heading: 'Outlook is not set up on this Logos yet', text: 'It is added once for everyone, then you can connect here.', action: { admin: true, ask: 'to add it' } }),
        h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn quiet', onclick: () => load() }, 'Back')));
      return;
    }
    if (!d.outlook_ready) return outlookApp();
    const go = h('button', { type: 'button', class: 'btn', onclick: async () => {
      go.disabled = true; msg.textContent = 'Asking Microsoft for a code…';
      const r = await api('/api/v2/mail/outlook', { method: 'POST' });
      go.disabled = false;
      if (!r.ok) { msg.textContent = await errOf(r); return; }
      msg.textContent = ''; load();
    } }, 'Get a sign-in code');
    box.replaceChildren(h('p', {}, 'You sign in on Microsoft\'s own page with a short code: Logos never sees your password.'),
      h('div', { class: 'row' }, go, h('button', { type: 'button', class: 'btn quiet', onclick: () => load() }, 'Back')),
      // Whoever manages Logos can put in a different app (a wrong ID pasted, Greg 23 Nov).
      d.is_admin ? h('p', { class: 'when' }, h('button', { type: 'button', class: 'linkish', onclick: () => { msg.textContent = ''; outlookApp(); } }, 'Change the Logos app')) : '');
  }

  function showCode(d) {
    const s = d.signin;
    box.replaceChildren(h('ol', { class: 'plain mail-steps' },
      step(1, h('strong', {}, 'Open Microsoft\'s sign-in page'), h('p', {}, ext(s.url, s.url.replace(/^https:\/\//, '')))),
      step(2, h('strong', {}, 'Type this code'), h('p', { class: 'mail-code' }, s.code),
        navigator.clipboard ? h('button', { type: 'button', class: 'btn quiet small', onclick: async () => { try { await navigator.clipboard.writeText(s.code); msg.textContent = 'Code copied.'; } catch (e) { /* type it */ } } }, 'Copy the code') : ''),
      step(3, h('strong', {}, 'Sign in and allow "Logos mail"'), h('p', { class: 'when' }, 'It asks to read your mail only. This page carries on by itself.'))),
    h('p', { class: 'when' }, 'Waiting for you to sign in…'));
    timer = setTimeout(async () => {
      const r = await getJSON('/api/v2/mail').catch(() => null);
      if (!alive || !r) return;
      if (r.signin && r.signin.state === 'failed') { msg.textContent = r.signin.error; outlook(r); return; }
      if (r.signin && r.signin.state === 'connected') { msg.textContent = 'Connected. Your agent starts reading in a few minutes.'; adding = false; }
      load();
    }, 3000);
  }

  // Once per Logos, by whoever manages it: the one app registration every Outlook.com user signs in through.
  function outlookApp() {
    const id = h('input', { id: 'mail-app', type: 'text', autocomplete: 'off', placeholder: '1a2b3c4d-…', spellcheck: 'false' });
    box.replaceChildren(h('p', {}, 'One-time setup for this Logos (about 5 minutes). After this, everyone connects Outlook with a code.'),
      h('ol', { class: 'plain mail-steps' },
        step(1, h('strong', {}, 'Register the app'), h('p', { class: 'when' }, 'At ', ext('https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade', 'Entra › App registrations'),
          ', sign in with your Microsoft account, then New registration: name "Logos mail", account type "Personal Microsoft accounts only", no redirect URI. Register.')),
        step(2, h('strong', {}, 'Allow sign-in with a code'), h('p', { class: 'when' }, 'Authentication › "Allow public client flows": Yes › Save.')),
        step(3, h('strong', {}, 'Read-only permission'), h('p', { class: 'when' }, 'API permissions › Add a permission › Microsoft Graph › Delegated: Mail.Read and offline_access (User.Read is there already). Nothing that sends or deletes.')),
        step(4, h('strong', {}, 'Paste the Application (client) ID'), h('p', { class: 'when' }, 'From the app\'s Overview page.'),
          h('form', { class: 'row', onsubmit: async (e) => {
            e.preventDefault();
            const r = await api('/api/v2/mail/outlook-app', { method: 'PUT', body: { client_id: id.value.trim() } });
            msg.textContent = r.ok ? 'Saved: Outlook can be connected now.' : await errOf(r);
            if (r.ok) outlook({ outlook_ready: true, is_admin: true });
          } }, id, h('button', { type: 'submit', class: 'btn' }, 'Save')))),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn quiet', onclick: () => load() }, 'Back')));
  }

  load();
  return () => { alive = false; clearTimeout(timer); };
}

// The house speaker (Voice V1.2; reworked 28 Nov: settings, not a status page). For each agent the person may
// use: the languages it speaks there, each on or off (one at least: a language that is off is answered in
// the agent's main one), and each language's voice, shown with ▶ and "Change", which opens the full list
// grouped by language in a sheet (phone) or a dialog (desktop). How the speaker is doing is one small line.
const LANG_NAMES = { en: 'English', fr: 'French' };
function speaker(body) {
  let alive = true, playing = null, data = null;
  const player = new Audio();
  const msg = h('p', { class: 'when', role: 'status' });
  const top = h('div', { class: 'speaker-top' });
  const box = h('div', { class: 'settings-body-inner speaker-agents' }, loading('rows', 3));
  body.replaceChildren(h('section', { 'aria-label': 'Speaker' }, top,
    h('p', { class: 'when' }, 'Each agent answers the house speaker in the languages switched on here, in the voice chosen for each. A change applies from its next answer.'),
    box, msg));
  const urls = {};
  const stop = () => { player.pause(); if (playing) { playing.replaceChildren(icon(ICONS.play)); playing.setAttribute('aria-label', playing.dataset.label); } playing = null; };
  player.addEventListener('ended', stop);
  async function play(btn, voice, note = msg) {
    if (playing === btn) { stop(); return; }
    stop(); note.textContent = '';
    btn.disabled = true;
    try {
      if (!urls[voice]) {
        const r = await api(`/api/v2/speaker/sample?voice=${encodeURIComponent(voice)}`);
        if (!r.ok) { const d = await r.json().catch(() => ({})); note.textContent = d.message || 'The sample could not be played just now.'; return; }
        urls[voice] = URL.createObjectURL(await r.blob());
      }
      if (!alive) return;
      player.src = urls[voice]; await player.play();
      playing = btn; btn.replaceChildren(icon(ICONS.stop)); btn.setAttribute('aria-label', 'Stop the sample');
    } catch (e) { note.textContent = 'The sample could not be played just now.'; } finally { btn.disabled = false; }
  }
  const playBtn = (v, note) => {
    const label = `Play a sample of ${v.name}`;
    const btn = h('button', { type: 'button', class: 'icon-btn voice-play', 'aria-label': label, 'data-label': label, title: 'Play a sample',
      onclick: () => play(btn, v.id, note) }, icon(ICONS.play));
    return btn;
  };
  const keyOf = (lang) => (lang === 'fr' ? ['voice_fr', 'default_fr'] : ['voice', 'default']);
  // The full list for one language, as radio buttons, each with its sample.
  function choiceList(a, voices, lang, note, after) {
    const [key, def] = keyOf(lang), usual = a[def];
    return h('div', { class: 'settings-choices voice-list', role: 'radiogroup', 'aria-label': `${a.display_name}: ${lang === 'fr' ? 'French voice' : 'voice'}` },
      voices.map((v) => h('div', { class: 'voice-row' },
        h('label', { class: 'appearance-mode choice' },
          h('input', { type: 'radio', name: `voice-${lang}-${a.name}`, value: v.id, checked: v.id === a[key], onchange: async () => {
            const r = await api(`/api/v2/agents/${a.name}/speaker-voice`, { method: 'PUT', body: { [key]: v.id === usual ? '' : v.id } }).catch(() => null);
            note.textContent = r && r.ok ? `Saved: ${a.display_name} speaks${lang === 'fr' ? ' French' : ''} as ${v.name} from its next answer.` : 'That could not be saved just now.';
            if (r && r.ok) { a[key] = v.id; after(); }
          } }),
          h('span', {}, h('strong', {}, v.name), h('span', { class: 'when' }, `${v.accent} ${v.who}${v.id === usual ? ' · its usual voice' : ''}`))),
        playBtn(v, note))));
  }
  function picker(a, first, redraw) {
    const note = h('p', { class: 'when appearance-note', role: 'status' });
    const groups = (data.installed || []).map((l) => {
      const voices = (data.voices || []).filter((v) => (v.lang || 'en') === l.code);
      return h('section', { class: 'pick-group', 'aria-label': l.name, 'data-lang': l.code }, h('h3', { class: 'label' }, l.name),
        choiceList(a, voices, l.code, note, redraw));
    });
    sheet({ title: `${a.display_name}'s voice`, body: [note, ...groups], onClose: stop });
    const at = document.querySelector(`.pick-group[data-lang="${first}"]`);
    if (at && first !== 'en') at.scrollIntoView({ block: 'start' });
  }
  async function setLanguages(a, on, note) {
    const off = (data.installed || []).map((l) => l.code).filter((c) => !on.includes(c));
    const r = await api(`/api/v2/agents/${a.name}/speaker-languages`, { method: 'PUT', body: { off } }).catch(() => null);
    if (r && r.ok) { a.languages = (await r.json()).languages; note.textContent = 'Saved: from its next answer.'; return true; }
    const d = r ? await r.json().catch(() => ({})) : {};
    note.textContent = d.message || 'That could not be saved just now.';
    return false;
  }
  function addLanguage(a) {
    const more = data.more || [];
    const admin = isAdmin();
    sheet({ title: 'Add a language', body: [
      h('p', { class: 'when' }, `${a.display_name} can speak every language listed on its card: switch one on there. `
        + 'The speech to text understands these too, but Logos has no voice for them yet:'),
      h('div', { class: 'rows' }, more.map((l) => h('div', { class: 'row-item lang-more' }, h('strong', {}, l.name),
        h('span', { class: 'when' }, 'Needs a voice installed first')))),
      admin ? h('p', { class: 'when' }, 'To add one: a Piper voice for it in the speaker voices image (infra/tts/Dockerfile) and gateway/tts.py, then rebuild the image. It then shows here with its own switch.')
        : h('p', { class: 'empty-ask' }, `Ask ${adminName()} to add a voice for one.`),
    ] });
  }
  function agentCard(a) {
    const card = h('section', { class: 'card speaker-agent', 'aria-label': `${a.display_name}'s voice` });
    const note = h('p', { class: 'when appearance-note', role: 'status' });
    function draw() {
      const on = a.languages || ['en'];
      const status = [a.speaks ? 'Answers the speaker' : 'Not on the speaker yet: this is how it will sound',
        a.shared ? 'shared: either of you can change it' : '', a.last_used ? `last answered ${when(a.last_used).toLowerCase()}` : ''].filter(Boolean).join(' · ');
      const main = on[0];
      const rows = (data.installed || []).map((l) => {
        const [key] = keyOf(l.code);
        const v = (data.voices || []).find((x) => x.id === a[key]) || { id: a[key], name: a[key], accent: '', who: '' };
        const isOn = on.includes(l.code);
        const sw = h('input', { type: 'checkbox', id: `lang-${a.name}-${l.code}`, checked: isOn ? '' : null,
          disabled: isOn && on.length === 1 ? '' : null, 'aria-label': `${a.display_name} speaks ${l.name}`, onchange: async (e) => {
            const want = e.target.checked ? [...on, l.code] : on.filter((c) => c !== l.code);
            e.target.disabled = true;
            if (!await setLanguages(a, want, note)) e.target.checked = !e.target.checked;
            draw();
          } });
        return h('div', { class: `speaker-lang${isOn ? '' : ' off'}`, 'data-lang': l.code },
          h('label', { class: 'switch' }, sw, h('span', { class: 'switch-track', 'aria-hidden': 'true' })),
          h('div', { class: 'speaker-lang-text' }, h('strong', {}, l.name),
            h('span', { class: 'when' }, isOn ? v.name : `Off: answered in ${LANG_NAMES[main] || main} instead`)),
          isOn ? playBtn(v, note) : null,
          isOn ? h('button', { type: 'button', class: 'btn quiet small voice-change', 'aria-label': `Change ${a.display_name}'s ${l.name} voice`,
            onclick: () => picker(a, l.code, draw) }, 'Change') : null);
      });
      card.replaceChildren(h('div', { class: 'speaker-agent-head' }, h('h2', {}, a.display_name), h('p', { class: 'when speaker-status' }, status)),
        h('h3', { class: 'label' }, 'Languages'), h('div', { class: 'speaker-langs' }, ...rows),
        h('button', { type: 'button', class: 'linkish speaker-add', onclick: () => addLanguage(a) }, '+ Add a language'), note);
    }
    draw();
    return card;
  }
  function serviceLine(d) {
    if (d.on) return h('p', { class: 'when speaker-service' }, h('span', { class: 'status up', 'aria-hidden': 'true' }), ' Speaker voices on');
    return emptyState({ icon: 'speaker', heading: d.problem ? 'The speaker voices are not working' : 'The speaker voices are off',
      text: d.problem || 'Home Assistant reads answers in its own voice until they are switched on. What you choose here is kept for then.',
      action: { label: 'How to turn them on', admin: true, ask: 'to switch them on', onclick: (e) => {
        e.target.replaceWith(h('p', { class: 'when' }, 'On the Logos machine, put TTS_BIND=<its address on the home network> in ~/.config/logos/speaker, then restart the household agent (docs/voice.md).'));
      } } });
  }
  getJSON('/api/v2/speaker').then((d) => {
    if (!alive) return;
    data = { installed: [{ code: 'en', name: 'English' }, { code: 'fr', name: 'French' }], more: [], ...d };
    const agents = d.agents || [];
    top.replaceChildren(serviceLine(data));
    if (!agents.length) { box.replaceChildren(emptyState({ icon: 'agents', heading: 'You have no agents yet', action: { admin: true, ask: 'for one', label: 'People', href: '#/people' } })); return; }
    box.replaceChildren(...agents.map((a) => agentCard({ languages: ['en', 'fr'], ...a })));
  }).catch(() => box.replaceChildren(h('p', { class: 'none' }, 'The voices could not be loaded just now.')));
  return () => { alive = false; stop(); Object.values(urls).forEach((u) => URL.revokeObjectURL(u)); };
}

// Sites the agents may reach (E6; its own page since 28 Nov, Greg: "weird to see without paging, as that's
// going to become a lot over time"). By agent, folded; a search; ten rows each, then "Show more". Edit
// takes a site away, lifts a Deny early, or adds a site by hand (the server checks it as it checks an answer).
const SITES_PAGE = 10;
function sitesPage(body) {
  let alive = true, editing = false, data = null, query = '';
  const shown = {};                    // agent -> how many rows are unfolded
  const openAgents = new Set();
  const msg = h('p', { class: 'when', role: 'status' });
  const list = h('div', { class: 'sites-agents' }, loading('rows', 3));
  const search = h('input', { type: 'search', placeholder: 'Search sites', 'aria-label': 'Search sites', autocomplete: 'off',
    oninput: (e) => { query = e.target.value.trim().toLowerCase(); draw(); } });
  const edit = h('button', { type: 'button', class: 'btn quiet small sites-edit', 'aria-pressed': 'false', onclick: () => {
    editing = !editing; edit.textContent = editing ? 'Done' : 'Edit'; edit.setAttribute('aria-pressed', String(editing)); draw();
  } }, 'Edit');
  body.replaceChildren(h('section', { 'aria-label': 'Sites' },
    h('p', { class: 'when' }, 'What the agents may reach beyond Logos itself. An agent asks its people when a site is refused; they choose Allow once, Always allow or Deny.'),
    h('div', { class: 'sites-tools' }, h('label', { class: 'set-search' }, icon(ICONS.search), search), edit), list, msg));
  const site = (e) => (Number(e.port) === 443 ? e.host : `${e.host}:${e.port}`);
  const until = (e) => new Date(e.until * 1000).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  const post = async (path, body2) => {
    const r = await api(path, { method: 'POST', body: body2 }).catch(() => null);
    const out = r ? await r.json().catch(() => ({})) : {};
    msg.textContent = r && r.ok ? '' : (out.message || 'That did not work just now.');
    if (r && r.ok) load();
    return r && r.ok;
  };
  function agentBlock(a) {
    const rows = [...a.always.map((e) => ['always', e]), ...a.once.map((e) => ['once', e]), ...a.deny.map((e) => ['deny', e])]
      .filter(([, e]) => !query || site(e).toLowerCase().includes(query));
    const n = shown[a.name] || SITES_PAGE;
    const WHAT = { always: 'Always allowed', once: 'Allowed once', deny: 'Denied' };
    const line = ([kind, e]) => h('div', { class: `row-item site-row site-${kind}` },
      h('span', { class: 'row-main' }, h('span', { class: 'site-name' }, site(e)),
        h('span', { class: 'when block' }, kind === 'always' ? `${WHAT[kind]}${e.by ? ` · by ${e.by}` : ''}` : `${WHAT[kind]} · until ${until(e)}`)),
      editing && kind !== 'deny' ? h('button', { type: 'button', class: 'btn small quiet danger', 'aria-label': `Remove ${site(e)} from ${a.display_name}`, onclick: async (ev) => {
        ev.target.disabled = true;
        if (!await post(`/api/v2/network-sites/${encodeURIComponent(a.name)}/remove`, { host: e.host, port: e.port })) ev.target.disabled = false;
      } }, 'Remove') : null,
      editing && kind === 'deny' && a.yours ? h('button', { type: 'button', class: 'btn small quiet', 'aria-label': `Lift the deny on ${site(e)} for ${a.display_name}`, onclick: async (ev) => {
        ev.target.disabled = true;
        if (!await post(`/api/v2/network-sites/${encodeURIComponent(a.name)}/undeny`, { host: e.host, port: e.port })) ev.target.disabled = false;
      } }, 'Lift') : null);
    const add = editing && a.yours ? (() => {
      const input = h('input', { type: 'text', placeholder: 'example.org', 'aria-label': `A site ${a.display_name} may always reach`,
        autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false' });
      return h('form', { class: 'mail-add site-add', onsubmit: async (e) => {
        e.preventDefault();
        const [host, port] = input.value.trim().replace(/^https?:\/\//, '').replace(/\/.*$/, '').split(':');
        if (host) await post(`/api/v2/network-sites/${encodeURIComponent(a.name)}/add`, { host, port: Number(port) || 443 });
      } }, input, h('button', { type: 'submit', class: 'btn quiet small' }, 'Add'));
    })() : null;
    const counts = `${a.always.length + a.once.length} allowed${a.deny.length ? `, ${a.deny.length} denied` : ''}`;
    const d = h('details', { class: 'colours-more site-agent', 'data-agent': a.name, open: openAgents.has(a.name) || !!query },
      h('summary', {}, h('span', {}, a.display_name), h('span', { class: 'when' }, counts)),
      rows.length ? h('div', { class: 'rows' }, rows.slice(0, n).map(line))
        : h('p', { class: 'none' }, query ? 'No site matches.' : 'Only Logos itself.'),
      rows.length > n ? h('button', { type: 'button', class: 'btn ghost small sites-more', onclick: () => { shown[a.name] = n + SITES_PAGE * 3; draw(); } },
        `Show ${Math.min(rows.length - n, SITES_PAGE * 3)} more`) : null,
      add);
    d.addEventListener('toggle', () => { if (d.open) openAgents.add(a.name); else openAgents.delete(a.name); });
    return d;
  }
  function draw() {
    if (!data) return;
    const agents = data.agents || [];
    if (!agents.length) { list.replaceChildren(emptyState({ icon: 'globe', heading: 'No agents of yours ask for sites' })); return; }
    list.replaceChildren(...agents.map(agentBlock));
  }
  async function load() {
    try { data = await getJSON('/api/v2/network'); } catch (e) { list.replaceChildren(h('p', { class: 'none' }, 'The sites could not be loaded just now.')); return; }
    if (!alive) return;
    if (!openAgents.size && (data.agents || []).length === 1) openAgents.add(data.agents[0].name);
    draw();
  }
  load();
  return () => { alive = false; };
}

// "7 allowed, 2 denied": the summary on the Sites row (Settings › Agents, and People).
export async function sitesSummary() {
  const n = await getJSON('/api/v2/network');
  const agents = n.agents || [];
  if (!agents.length) return null;
  const allowed = agents.reduce((t, a) => t + (a.always || []).length + (a.once || []).length, 0);
  const denied = agents.reduce((t, a) => t + (a.deny || []).length, 0);
  return `${allowed} allowed${denied ? `, ${denied} denied` : ''}`;
}

// Your voice (Voice VO.2): about 25 seconds of reading (the whole text; the recorder stops by itself at 45 s) makes a voice print, so the house speaker can tell who
// is speaking (Home answers for you; your own agent answers only you). Then test it (you, and someone else), check it
// against what the speaker itself heard, or delete it. Logos keeps numbers describing the voice, never the recording.
function yourVoice(body) {
  let alive = true, mode = 'enrol', add = false, rulesOf = null;
  const status = h('p', { class: 'when', role: 'status' });
  const intro = h('div', {}, loading('rows', 2));
  const printBox = h('div', { class: 'settings-body-inner voiceprint-now' });
  const testBox = h('section', { 'aria-label': 'Test it', hidden: true });
  const speakerBox = h('section', { 'aria-label': 'At the house speaker', hidden: true });
  const deleteBox = h('section', { 'aria-label': 'Delete your voice print', hidden: true });
  const peopleBox = h('section', { 'aria-label': 'Everyone\'s voice', hidden: true });
  const stateNote = h('p', { class: 'when', role: 'status' });
  const testNote = h('p', { class: 'when appearance-note', role: 'status' });
  const heardList = h('div', { class: 'settings-choices voiceprint-heard' });
  const prompt = h('blockquote', { class: 'voiceprint-prompt' });
  const what = h('p', { class: 'when' });
  const rec = createRecorder({ agent: '', raw: true, maxSeconds: 45, say: (t) => { status.replaceChildren(t || ''); },
    onWords: () => {}, onAudio: (blob) => upload(blob) });
  const label = (t) => { rec.mic.setAttribute('aria-label', t); rec.mic.title = t; };
  body.replaceChildren(
    h('section', { 'aria-label': 'Your voice' }, intro, printBox),
    h('section', { 'aria-label': 'Record' }, heading('Record'), what, prompt,
      h('form', { class: 'voiceprint-form', onsubmit: (e) => e.preventDefault() }, rec.mic, rec.bar), status),
    testBox, speakerBox, peopleBox, deleteBox);
  const quality = (q) => (q ? `I heard ${Number(q.speech || 0).toFixed(1)} s of speech${q.clipped ? ', too loud' : ''}, ${Number(q.snr || 0) >= 15 ? 'clean' : 'a little noisy'} (${Math.round(q.snr || 0)} dB).` : '');
  function showPrint(d) {
    const s = d.summary;
    testBox.hidden = speakerBox.hidden = deleteBox.hidden = !s;
    if (!s) {
      printBox.replaceChildren(h('p', { class: 'none' }, 'No voice print yet.'));
      what.textContent = `Tap the microphone and read all of this aloud (about ${d.seconds || 25} seconds; it stops by itself at 45), then tap ■. A quiet room is best.`;
      add = false; label('Record my voice');
      return;
    }
    const own = s.own ? `Your own samples score ${s.own[0].toFixed(2)} to ${s.own[1].toFixed(2)}.` : '';
    const others = s.others ? ` ${s.others} other voices score at most ${Number(s.others_top).toFixed(2)}.` : '';
    const pr = s.progress || { labelled: 0, window: 0, hits: 0, held: 0, missing: [] };
    const rules = rulesOf || { rate: 0.99, window: 50, samples: 50, days: 3 };
    const times = (n) => `${n} time${n === 1 ? '' : 's'}`;
    const lastN = pr.window ? `it would have recognised you ${pr.hits} of the last ${times(pr.window)}` : 'it has not tested itself yet';
    const speakerNeeds = s.speaker_threshold != null ? ` At the speaker it needs ${Number(s.speaker_threshold).toFixed(2)}.` : '';
    // VO.3 (Greg, 28 Nov): it learns first, from the speaker itself, and only recognises you once it is sure.
    const active = s.state === 'active';
    const reset = h('button', { type: 'button', class: 'btn quiet', onclick: async () => {
      if (!await ask({ title: 'Start learning again?', text: 'The speaker stops recognising you until it has tested itself again. What it has learned is kept.', yes: 'Start again' })) return;
      const r = await api('/api/v2/voiceprint/state', { method: 'POST', body: { state: 'learning' } }).catch(() => null);
      const got = r ? await r.json().catch(() => ({})) : {};
      stateNote.textContent = r && r.ok ? 'It is learning your voice again.' : 'That did not work just now.';
      if (r && r.ok) showPrint({ summary: got.summary });
    } }, 'Start learning again');
    printBox.replaceChildren(
      active
        ? h('div', { class: 'voiceprint-state' },
          h('p', {}, h('strong', {}, 'Recognising you. '), `The house speaker knows your voice: ${lastN} at the speaker.`),
          h('p', { class: 'when' }, 'Your own agent answers only you there, and Home knows it is you who asked. If it gets worse, it goes back to learning and tells you.'))
        : h('div', { class: 'voiceprint-state' },
          h('p', {}, h('strong', {}, 'Learning your voice: '),
            `${pr.labelled} recording${pr.labelled === 1 ? '' : 's'} from the speaker, ${lastN}. It switches on at ${Math.round(rules.rate * 100)}%.`),
          h('p', { class: 'when' }, `Just use the speaker as usual: every time you call your own agent by its name it learns a little more, from different distances and days. `
            + 'Until it switches on, your agent answers anyone who says its name, and nothing private is said out loud. It tells you when it switches on.')),
      h('details', { class: 'voiceprint-details' }, h('summary', {}, 'Details'),
        h('p', { class: 'when' }, `${s.samples} samples: ${s.samples - s.speaker} from your recording, ${s.speaker} from the speaker (${pr.labelled} when you called your agent, on ${pr.days || 0} day${pr.days === 1 ? '' : 's'}).`
          + `${pr.held ? ` ${pr.held} did not sound like you and were held back.` : ''}`),
        active ? '' : h('p', { class: 'when' }, `It switches on with ${rules.samples} recordings from the speaker on ${rules.days} different days, `
          + `recognising you in ${Math.round(rules.rate * 100)}% of the last ${rules.window}, and never taking anyone else for you.`
          + `${pr.missing && pr.missing.length ? ` Still needed: ${pr.missing.join('; ')}.` : ''}`),
        h('p', { class: 'when' }, `${own}${others} It needs ${s.threshold.toFixed(2)} for a recording here${s.tested_others ? ` (raised by ${s.tested_others} other voice${s.tested_others === 1 ? '' : 's'} you let it hear)` : ''}.${speakerNeeds}`)),
      active || pr.window ? h('div', { class: 'row' }, reset) : '', stateNote);
    what.textContent = 'Recording again adds to your voice print: a different room or distance helps.';
    add = true; label('Record more of my voice');
  }
  async function load() {
    const d = await getJSON('/api/v2/voiceprint').catch(() => null);
    if (!alive) return;
    if (!d) { intro.replaceChildren(h('p', { class: 'none' }, 'Your voice could not be loaded just now.')); return; }
    rulesOf = d.rules || rulesOf;
    if (d.admin) people();
    intro.replaceChildren(
      h('p', { class: 'when' }, 'Logos learns to tell who is speaking at the house speaker from their voice. Once it is sure it is you, Home answers for you ("what\'s on today?" is your day), and your own agent answers only you.'),
      h('p', { class: 'when' }, 'Logos keeps numbers that describe your voice (a voice print), never the recording. Only you can see, change or delete yours. It is a strong hint, not a lock: a recording of your voice could fool it.'),
      ...(d.on ? [] : [h('p', { class: 'none' }, 'Speech to text for the house speaker is not switched on on this Logos yet.')]));
    prompt.textContent = d.prompt || '';
    showPrint(d);
  }
  async function upload(blob) {
    const m = mode;
    const out = m === 'enrol' ? status : testNote;
    out.textContent = 'Working out your voice…';
    rec.mic.disabled = true;
    const q = m === 'enrol' ? `step=enrol${add ? '&add=1' : ''}` : `step=${m}`;
    try {
      const r = await api(`/api/v2/voiceprint?${q}`, { method: 'POST', raw: blob, headers: { 'Content-Type': blob.type || 'audio/webm' } });
      const d = await r.json().catch(() => ({}));
      if (!alive) return;
      if (!r.ok) { out.textContent = d.message || 'That did not work. Try again.'; return; }
      if (!d.ok) { out.textContent = `${quality(d.quality)} ${d.problem || ''}`; return; }
      if (m === 'enrol') { status.textContent = `${quality(d.quality)} Saved.`; showPrint({ summary: d.summary }); return; }
      const need = `needs ${d.threshold.toFixed(2)}`;
      testNote.textContent = m === 'me'
        ? `You scored ${d.score.toFixed(2)} (${need}): ${d.recognised ? 'recognised.' : 'not recognised. Try again, or record more of your voice.'}`
        : `They scored ${d.score.toFixed(2)} (${need}): ${d.recognised ? 'wrongly taken for you. It is now stricter: test yourself again.' : 'refused, as it should be.'}`;
      showPrint({ summary: d.summary });
    } catch (e) { out.textContent = 'That did not work. Try again.'; } finally { rec.mic.disabled = false; mode = 'enrol'; }
  }
  // Testing uses the same microphone: the next recording is scored instead of added.
  const testWith = (m, text) => { mode = m; testNote.textContent = text; rec.start(); };
  testBox.replaceChildren(heading('Test it'),
    h('p', { class: 'when' }, 'Say anything for five seconds or so, then tap ■.'),
    h('div', { class: 'row' },
      h('button', { type: 'button', class: 'btn quiet', onclick: () => testWith('me', 'Listening for you…') }, 'Test: me'),
      h('button', { type: 'button', class: 'btn quiet', onclick: () => testWith('other', 'Hand it over: someone else, say anything…') }, 'Test: someone else')),
    testNote);
  const clock = (at) => new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  async function heard() {
    heardList.replaceChildren(loading('rows', 1));
    const d = await getJSON('/api/v2/voiceprint/heard').catch(() => ({ heard: [] }));
    if (!alive) return;
    const rows = d.heard || [];
    if (!rows.length) { heardList.replaceChildren(h('p', { class: 'none' }, d.problem || 'Nothing heard at the speaker in the last 30 minutes.')); return; }
    heardList.replaceChildren(...rows.map((r) => {
      const note = h('span', { class: 'when' });
      const what = r.added ? 'added to your voice' : r.held ? `held back: ${r.held}` : r.recognised ? 'recognised as you' : 'not recognised';
      const fix = (claim) => h('button', { type: 'button', class: 'btn quiet', onclick: async (ev) => {
        ev.target.disabled = true;
        const res = await api(`/api/v2/voiceprint/heard/${r.id}`, { method: claim ? 'POST' : 'DELETE' }).catch(() => null);
        const got = res ? await res.json().catch(() => ({})) : {};
        note.textContent = res && res.ok ? (claim ? ' Added: it will know you better at the speaker.' : ' Taken out of your voice.') : ` ${got.message || 'That did not work.'}`;
        if (res && res.ok) showPrint({ summary: got.summary });
      } }, claim ? 'That was me' : 'Not me');
      return h('div', { class: 'voice-row voiceprint-heard-row' },
        h('span', {}, h('strong', {}, clock(r.at || (Date.now() / 1000 - r.ago))), h('span', { class: 'when' },
          ` · ${Number(r.speech || 0).toFixed(1)} s · scored ${r.score.toFixed(2)} · ${what}`), note),
        r.added ? fix(false) : fix(true));
    }));
  }
  speakerBox.replaceChildren(heading('At the house speaker'),
    h('p', { class: 'when' }, 'What the speaker heard in the last 30 minutes, newest first. To help it learn sooner: "That was me" adds one, "Not me" takes out one that was not you.'),
    h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn quiet', onclick: heard }, 'Check what it heard')), heardList);
  async function people() {
    const d = await getJSON('/api/v2/voiceprint/people').catch(() => null);
    if (!alive || !d || !(d.people || []).length) return;
    peopleBox.hidden = false;
    peopleBox.replaceChildren(heading('Everyone\'s voice'), h('div', { class: 'settings-choices' }, ...d.people.map((p) =>
      h('div', { class: 'voice-row voiceprint-person' }, h('span', {}, h('strong', {}, p.display_name), h('span', { class: 'when' },
        ` · ${p.state === 'active' ? 'recognising' : 'learning'} · ${p.labelled} from the speaker · ${p.window ? `${p.hits} of the last ${p.window}` : 'not tested yet'}`))))));
  }
  deleteBox.replaceChildren(heading('Delete'),
    h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn danger', onclick: async () => {
      if (!await ask({ title: 'Delete your voice print?', text: 'The house speaker will no longer recognise you. You can record it again at any time.', yes: 'Delete', danger: true })) return;
      const r = await api('/api/v2/voiceprint', { method: 'DELETE' }).catch(() => null);
      status.textContent = r && r.ok ? 'Your voice print was deleted.' : 'It could not be deleted just now.';
      if (r && r.ok) { testNote.textContent = ''; heardList.replaceChildren(); showPrint({ summary: null }); }
    } }, 'Delete my voice print')));
  load();
  return () => { alive = false; rec.destroy(); };
}

// What each row says it is set to now, and whether something there is wrong: {value, problem}. A promise
// where it has to be asked for. "Not set up yet" is not a problem; broken is (28 Nov).
function statusOf(id, state) {
  const v = (value, problem = null) => ({ value, problem });
  if (id === 'appearance') {
    const { look, mode } = currentLook();
    const name = (LOOKS.find((l) => l.id === look) || {}).name || look;
    return v(`${{ glass: 'Glass', hud: 'HUD', warm: 'Warm' }[look] || name} · ${{ dark: 'Dark', light: 'Light', auto: 'Auto' }[mode] || mode}`);
  }
  if (id === 'matrix') return v(state.me.matrix_id ? (mx.saved() ? 'Connected' : 'Not connected') : '');
  if (id === 'notifications') return push.status().then((st) => (st === 'on'
    ? v(getPref('alerts', 'who') === 'private' ? 'On · private' : 'On')
    : st === 'blocked' ? v('Blocked', 'Phone alerts are blocked in this browser') : v(st === 'unsupported' ? '' : 'Off')));
  if (id === 'email') return getJSON('/api/v2/mail').then((d) => {
    const b = d.mailboxes || [];
    const failed = b.filter((m) => m.health && m.health.error);
    const value = b.length > 1 ? `${b.length} mailboxes` : b.length ? ({ gmail: 'Gmail', outlook: 'Outlook' }[b[0].kind] || 'Connected') : 'Not connected';
    return v(failed.length ? 'Not refreshing' : value, failed.length ? `Email: ${failed[0].address || 'a mailbox'} is not refreshing` : null);
  });
  if (id === 'metrics') return getJSON('/api/v2/metrics').then((d) => v(d.enabled ? 'On' : 'Off'));
  if (id === 'speaker') return getJSON('/api/v2/speaker').then((d) => {
    const a = (d.agents || []).find((x) => x.speaks) || (d.agents || [])[0];
    const name = a ? ((d.voices || []).find((x) => x.id === a.voice) || {}).name || '' : '';
    return d.problem ? v('Not working', 'The speaker voices are not working') : v(name);
  });
  if (id === 'voice') return getJSON('/api/v2/voiceprint').then((d) => v(!d.enrolled ? 'Not set up' : (d.summary || {}).state === 'active' ? 'Recognising' : 'Learning'));
  if (id === 'words') return householdWordsSummary().then((t) => v(t));
  if (id === 'updates') return getJSON('/api/v2/updates?check=1').then((d) => {
    const n = ((d.plan || {}).commits || []).length;
    return n ? v(`${n} to install`, `${n} update${n === 1 ? '' : 's'} waiting to install`) : v('Up to date');
  });
  return v('');
}

function row({ href, name, glyph, value, current, cls = '', dot = null }) {
  const val = h('span', { class: 'set-value' });
  Promise.resolve(value).then((s) => {
    const text = typeof s === 'string' ? s : (s || {}).value || '';
    val.textContent = text;
    if (/to install|Not connected|Off|Blocked|Not working|Not refreshing|Not running|Unplugged/.test(text)) val.classList.add('attention');
  }).catch(() => {});
  return h('a', { href, class: `set-row${cls}`, 'aria-current': current ? 'page' : null },
    h('span', { class: 'set-icon' }, dot || icon(ICONS[glyph])), h('span', { class: 'set-name' }, name), val,
    h('span', { class: 'set-chevron' }, icon(ICONS.chevron)));
}

// "Needs attention" (28 Nov, after Home Assistant's repairs card): only when something is wrong. Each line
// opens its page; Dismiss hides that line for a day.
const DISMISSED = 'logos.settings.dismissed';
const DAY = 86400000;
function dismissed() {
  try { return JSON.parse(localStorage.getItem(DISMISSED) || '{}'); } catch (e) { return {}; }
}
function dismiss(key) {
  const d = dismissed();
  d[key] = Date.now();
  for (const k of Object.keys(d)) if (Date.now() - d[k] > DAY) delete d[k];
  try { localStorage.setItem(DISMISSED, JSON.stringify(d)); } catch (e) { /* shown again next time */ }
}
function attentionCard(problems) {
  const card = h('section', { class: 'card set-attention', 'aria-label': 'Needs attention', hidden: true });
  const items = [];
  const draw = () => {
    const gone = dismissed();
    const live = items.filter((p) => !(gone[p.key] && Date.now() - gone[p.key] < DAY));
    card.hidden = !live.length;
    card.replaceChildren(h('h2', { class: 'set-attention-title' }, icon(ICONS.alert), 'Needs attention'),
      ...live.map((p) => h('div', { class: 'set-attention-row' },
        h('a', { href: p.href, class: 'set-attention-link' }, h('span', {}, p.text), icon(ICONS.chevron)),
        h('button', { type: 'button', class: 'linkish set-dismiss', 'aria-label': `Dismiss "${p.text}" for a day`,
          onclick: () => { dismiss(p.key); draw(); } }, 'Dismiss'))));
  };
  for (const p of problems) {
    Promise.resolve(p).then((got) => {
      for (const one of [got].flat()) if (one && one.text) items.push(one);
      draw();
    }).catch(() => {});
  }
  return card;
}

// The Agents group: one row per agent you may use, in the words the Agents screen uses for its state.
function agentsGroup(agentsP, sitesP, id = null) {
  const sec = h('section', { class: 'set-group', 'aria-label': 'Agents' }, h('h2', { class: 'set-group-title' }, 'Agents'), loading('rows', 2));
  Promise.all([agentsP, sitesP.catch(() => null)]).then(async ([d, sites]) => {
    const agents = (d && d.agents) || [];
    if (!agents.length && !sites) { sec.remove(); return; }
    const mood = await moods(agents).catch(() => ({}));
    sec.replaceChildren(sec.firstChild, ...agents.map((a) => row({ href: `#/agents/${encodeURIComponent(a.name)}/settings`, name: a.display_name,
      dot: h('span', { class: `dot ${a.name}`, style: `background: var(--agent-${a.name}, var(--${a.name}, var(--accent)))` }),
      value: MOODS[mood[a.name]] || (a.running === false ? MOODS.off : a.model_up === false ? MOODS.unplugged : MOODS.asleep), cls: ' set-agent' })),
    ...(sites ? [row({ href: '#/settings/sites', name: 'Sites', glyph: 'globe', value: sites, cls: ' set-sites', current: id === 'sites' })] : []));
    filterIndex(sec.closest('.set-index'));
  }).catch(() => sec.remove());
  return sec;
}

// The search at the top of the list: rows whose name or value has the words; empty groups fold away.
let lastQuery = '';
function filterIndex(nav) {
  if (!nav) return;
  const q = lastQuery.trim().toLowerCase();
  let any = false;
  for (const r of nav.querySelectorAll('.set-row, .set-profile')) {
    const hit = !q || r.textContent.toLowerCase().includes(q);
    r.hidden = !hit;
    any = any || hit;
  }
  for (const g of nav.querySelectorAll('.set-group')) {
    g.hidden = !!q && ![...g.querySelectorAll('.set-row')].some((r) => !r.hidden);
  }
  for (const el of nav.querySelectorAll('.set-version')) el.hidden = !!q;
  const none = nav.querySelector('.set-nomatch');
  if (none) none.hidden = !q || any;
}

function index(state, id, extra = {}) {
  const isAdmin = state.me.role === 'admin';
  const shown = (g) => SECTIONS.filter((s) => s.group === g && (!s.admin || isAdmin));
  const shownName = state.me.display_name || state.me.username;
  const group = (title, rows) => (rows.length ? h('section', { class: 'set-group', 'aria-label': title },
    h('h2', { class: 'set-group-title' }, title), ...rows) : '');
  const st = extra.status || {};
  const sectionRow = (s) => row({ href: `#/settings/${s.id}`, name: s.name, glyph: s.icon, value: st[s.id], current: s.id === id });
  const search = h('input', { type: 'search', placeholder: 'Search settings', 'aria-label': 'Search settings', autocomplete: 'off', value: lastQuery,
    oninput: (e) => { lastQuery = e.target.value; filterIndex(nav); },
    onkeydown: (e) => {
      if (e.key === 'Escape') { e.target.value = ''; lastQuery = ''; filterIndex(nav); }
      if (e.key === 'Enter') { const first = [...nav.querySelectorAll('.set-row')].find((r) => !r.hidden && r.href); if (first) first.click(); }
    } });
  const nav = h('nav', { class: 'set-index', 'aria-label': 'Settings' },
    h('label', { class: 'set-search' }, icon(ICONS.search), search,
      h('kbd', { class: 'set-kbd wide-only', 'aria-hidden': 'true' }, /Mac/.test(navigator.platform) ? '⌘K' : 'Ctrl K')),
    extra.attention || '',
    h('a', { href: '#/settings/account', class: 'set-profile', 'aria-current': id === 'account' ? 'page' : null },
      h('span', { class: 'set-avatar', 'aria-hidden': 'true' }, (shownName || '?').trim().charAt(0).toUpperCase()),
      h('span', { class: 'set-who' }, h('strong', {}, shownName),
        h('span', { class: 'when' }, `${state.me.username} · ${isAdmin ? 'Manages Logos' : 'Member'}`)),
      h('span', { class: 'set-chevron' }, icon(ICONS.chevron))),
    group('You', shown('You').map(sectionRow)),
    extra.agents || '',
    group('This device', shown('This device').map(sectionRow)),
    isAdmin ? group('Logos', [...shown('Logos').map(sectionRow),
      ...ELSEWHERE.map((e) => row({ href: e.href, name: e.name, glyph: e.icon, cls: ' set-elsewhere' }))]) : '',
    h('p', { class: 'none set-nomatch', hidden: true }, 'No setting matches.'),
    h('section', { class: 'set-group set-end' },
      h('button', { type: 'button', class: 'set-row set-danger', onclick: () => { const b = document.querySelector('.logout-btn'); if (b) b.click(); } },
        h('span', { class: 'set-icon' }, icon(ICONS.logout)), h('span', { class: 'set-name' }, 'Sign out'))),
    versionLine(state));
  filterIndex(nav);
  return nav;
}

// The end of the list, as in Element X: which Logos this is (the commit, and when it was made).
function versionLine(state) {
  const v = state.me.version || {};
  if (!v.commit) return '';
  const day = v.date ? new Date(v.date * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }) : '';
  return h('p', { class: 'set-version' }, `Logos ${v.commit}${day ? ` · ${day}` : ''}`);
}

// Everything the list and its "Needs attention" card ask for, asked once.
function gather(state) {
  const isAdmin = state.me.role === 'admin';
  const status = {};
  for (const s of SECTIONS) if (!s.admin || isAdmin) status[s.id] = Promise.resolve(statusOf(s.id, state)).catch(() => ({ value: '' }));
  const agentsP = getJSON('/api/v2/agents').catch(() => null);
  const sitesP = sitesSummary();
  const fromRow = (id, href) => status[id] && status[id].then((s) => (s && s.problem ? { key: id, text: s.problem, href } : null));
  const problems = [
    fromRow('updates', '#/settings/updates'), fromRow('notifications', '#/settings/notifications'),
    fromRow('email', '#/settings/email'), fromRow('speaker', '#/settings/speaker'),
    // The AI computer off, and an agent that is not running.
    agentsP.then((d) => {
      const all = (d && d.agents) || [];
      const out = [];
      if (all.some((a) => a.model_up === false)) out.push({ key: 'model', text: 'The AI computer is off: the agents reply when it is back', href: isAdmin ? '#/models' : '#/chat' });
      for (const a of all.filter((x) => x.running === false)) out.push({ key: `agent-${a.name}`, text: `${a.display_name} is not running`, href: `#/agents/${encodeURIComponent(a.name)}/settings` });
      return out;
    }),
  ].filter(Boolean);
  return { status, agentsP, sitesP, problems };
}

export async function settingsView(root, [section], state) {
  const isAdmin = state.me.role === 'admin';
  const known = SECTIONS.filter((s) => !s.admin || isAdmin);
  const wide = matchMedia(WIDE).matches;
  // A phone opens on the list; a wider screen opens on Look beside it. Old links still land
  // (#/settings/chat went under Look on 22 Nov).
  const id = known.some((s) => s.id === section) ? section : (section === 'chat' ? 'appearance' : (wide ? 'appearance' : null));
  const page = h('div', { class: `page settings${id ? '' : ' at-index'}` });
  const body = h('div', { class: 'settings-body' });
  let done = null;
  const sec = SECTIONS.find((s) => s.id === id);
  const listParts = () => {
    const g = gather(state);
    const attention = attentionCard(g.problems);
    return { status: g.status, attention, agents: agentsGroup(g.agentsP, g.sitesP, id) };
  };
  if (!wide && id) {
    // One header (28 Nov, as Home Assistant's sub-pages): the back arrow and the page's name in the top bar.
    page.append(subpage({ title: sec.name, back: '#/settings', backLabel: 'Settings' }), body);
  } else if (!wide) {
    page.append(index(state, null, listParts()));
  } else {
    // A desktop: Settings › the page, over the list and the page beside it.
    page.append(h('header', { class: 'set-crumbs' }, h('h1', {}, h('span', { class: 'set-crumb-root' }, 'Settings'),
      h('span', { class: 'set-crumb-sep', 'aria-hidden': 'true' }, ' › '), h('span', { class: 'set-crumb-page' }, sec.name))),
    h('div', { class: 'settings-layout' }, index(state, id, listParts()), body));
  }
  root.replaceChildren(page);
  try {
    if (sessionStorage.getItem('logos.settings.search')) {
      sessionStorage.removeItem('logos.settings.search');
      const box = page.querySelector('.set-search input'); if (box) box.focus();
    }
  } catch (e) { /* no memory: no focus */ }
  if (id === 'appearance') body.replaceChildren(...appearance(), ...chat());
  else if (id === 'account') body.replaceChildren(...account(state));
  else if (id === 'matrix') body.replaceChildren(...matrix(state));
  else if (id === 'notifications') body.replaceChildren(...notifications(state));
  else if (id === 'tips') body.replaceChildren(...tips());
  else if (id === 'metrics') body.replaceChildren(...metricsPage());
  else if (id === 'email') done = email(body);
  else if (id === 'speaker') done = speaker(body);
  else if (id === 'sites') done = sitesPage(body);
  else if (id === 'voice') done = yourVoice(body);
  else if (id === 'words') done = householdWordsPage(body);
  else if (id === 'updates') done = await updatesView(body, [], state, true);
  // Turning a foldable or resizing across the line: the other layout.
  const mq = matchMedia(WIDE);
  const flip = () => { if (root.contains(page)) settingsView(root, [section], state); };
  mq.addEventListener('change', flip);
  return () => { mq.removeEventListener('change', flip); if (done) done(); };
}
