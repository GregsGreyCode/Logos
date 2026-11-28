// Settings (ST1): one screen for everything you set for yourself. Sections on the left (a row of
// tabs on a phone): Appearance, Your account, Chat, Matrix device, and Updates for whoever
// manages Logos. Each section is its own address (#/settings/devices…), so it can be linked to.
import { h, api, getJSON, when, loading, revealable, ask, icon, ICONS } from './util.js';
import * as mx from './mx.js';
import { lookControls, SWATCHES, setMine, myColours, setAgentColour, agentColour, setMotion, setAnimations, LOOKS, current as currentLook } from './appearance.js';
import { getPref, setPref } from './prefs.js';
import { reset as resetTips } from './tips.js';
import { updatesView } from './updates.js';
import * as push from './push.js';
import { createRecorder } from './voice.js';

// F45 (Greg, 23 Nov: "more inspired by Element X's settings"): a list, not a row of tabs. Grouped,
// each row with its icon and what it is set to now; on a phone a row opens its page (with a back
// arrow), on a wider screen the list stays on the left beside the page.
const SECTIONS = [
  { id: 'appearance', name: 'Look', full: 'Look', icon: 'palette', group: 'You' },
  { id: 'notifications', name: 'Phone alerts', full: 'Phone alerts', icon: 'bell', group: 'You' },
  { id: 'email', name: 'Email', full: 'Email', icon: 'mail', group: 'You' },
  { id: 'speaker', name: 'Speaker', full: 'House speaker', icon: 'speaker', group: 'You' },
  { id: 'voice', name: 'Your voice', full: 'Your voice', icon: 'mic', group: 'You' },
  { id: 'tips', name: 'Tips', full: 'Tips', icon: 'bulb', group: 'You' },
  { id: 'matrix', name: 'Chats & devices', full: 'Chats & devices', icon: 'shield', group: 'This device' },
  { id: 'updates', name: 'Updates', full: 'Updates', icon: 'update', group: 'Logos', admin: true },
  { id: 'metrics', name: 'Metrics', full: 'Prometheus metrics', icon: 'chart', group: 'Logos', admin: true },
  { id: 'account', name: 'Your account', full: 'Your account', icon: 'user', group: null },
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

function tips() {
  const note = savedNote();
  // F41: the first-visit tips, once more (each screen shows its tips again next time it opens).
  return [h('section', { 'aria-label': 'Tips' }, heading('Tips'),
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
    box.replaceChildren(h('p', {}, 'Off. Turn it on to let Prometheus or Grafana at home read how your agents are doing: which are running, how many messages each sent today, and whether the AI computer answers.'),
      h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn', onclick: async () => {
        msg.textContent = 'Turning it on…';
        const d = await put({ enabled: true });
        if (!d) { msg.textContent = 'Could not turn it on just now.'; return; }
        msg.textContent = ''; showOn(d.token);
      } }, 'Turn on')), msg, about());
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
    .catch(() => box.replaceChildren(h('p', { class: 'none' }, 'Only whoever manages Logos can set this.')));
  return [heading('Prometheus metrics'), box];
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
    if (!state.me.matrix_id) { box.replaceChildren(h('p', { class: 'none' }, 'Chat is not set up on this Logos.')); return; }
    if (!mx.saved()) {
      box.replaceChildren(h('p', {}, 'Connect this device to your chats first (Settings › Chats & devices): notifications come from them.'),
        h('a', { href: '#/settings/matrix', class: 'btn' }, 'Settings › Chats & devices'));
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
  return [h('section', { 'aria-label': 'Phone alerts' }, heading('Phone alerts'), box), privacy];
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
  body.replaceChildren(h('section', { 'aria-label': 'Email' }, heading('Email'),
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
    if (!d.agent) { box.replaceChildren(h('p', { class: 'none' }, 'Your agent is not set up to read email yet. Ask whoever manages Logos to turn it on.')); return; }
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
      box.replaceChildren(h('p', { class: 'none' }, 'Outlook is not set up on this Logos yet. Whoever manages Logos adds it once, then you can connect here.'),
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

// The house speaker (Voice V1.2): each agent answers in its own voice, chosen here by its people (for the
// household agent, either of you). Every voice has a play button; the sample comes from the speaker's own Piper.
// V3.4: and a French voice for each agent, used when it answers in French (Béré speaks French to the speaker).
function speaker(body) {
  let alive = true, playing = null;
  const player = new Audio();
  const msg = h('p', { class: 'when', role: 'status' });
  const box = h('div', { class: 'settings-body-inner' }, loading('rows', 3));
  body.replaceChildren(h('section', { 'aria-label': 'House speaker' }, heading('House speaker'),
    h('p', { class: 'when' }, 'When an agent answers through the speaker it uses the voice chosen here. The change applies from its next answer.'),
    box, msg));
  const urls = {};
  const stop = () => { player.pause(); if (playing) { playing.replaceChildren(icon(ICONS.play)); playing.setAttribute('aria-label', playing.dataset.label); } playing = null; };
  player.addEventListener('ended', stop);
  async function play(btn, voice) {
    if (playing === btn) { stop(); return; }
    stop(); msg.textContent = '';
    btn.disabled = true;
    try {
      if (!urls[voice]) {
        const r = await api(`/api/v2/speaker/sample?voice=${encodeURIComponent(voice)}`);
        if (!r.ok) { const d = await r.json().catch(() => ({})); msg.textContent = d.message || 'The sample could not be played just now.'; return; }
        urls[voice] = URL.createObjectURL(await r.blob());
      }
      if (!alive) return;
      player.src = urls[voice]; await player.play();
      playing = btn; btn.replaceChildren(icon(ICONS.stop)); btn.setAttribute('aria-label', 'Stop the sample');
    } catch (e) { msg.textContent = 'The sample could not be played just now.'; } finally { btn.disabled = false; }
  }
  function choiceList(a, voices, fr, note) {
    const key = fr ? 'voice_fr' : 'voice', usual = fr ? a.default_fr : a.default;
    return h('div', { class: 'settings-choices voice-list', role: 'radiogroup', 'aria-label': `${a.display_name}: ${fr ? 'French voice' : 'voice'}` },
      voices.map((v) => {
        const label = `Play a sample of ${v.name}`;
        const btn = h('button', { type: 'button', class: 'icon-btn voice-play', 'aria-label': label, 'data-label': label, title: 'Play a sample',
          onclick: () => play(btn, v.id) }, icon(ICONS.play));
        return h('div', { class: 'voice-row' },
          h('label', { class: 'appearance-mode choice' },
            h('input', { type: 'radio', name: `${fr ? 'voice-fr' : 'voice'}-${a.name}`, value: v.id, checked: v.id === a[key], onchange: async () => {
              const r = await api(`/api/v2/agents/${a.name}/speaker-voice`, { method: 'PUT', body: { [key]: v.id === usual ? '' : v.id } }).catch(() => null);
              note.textContent = r && r.ok ? `Saved: ${a.display_name} speaks${fr ? ' French' : ''} as ${v.name} from its next answer.` : 'That could not be saved just now.';
            } }),
            h('span', {}, h('strong', {}, v.name), h('span', { class: 'when' }, `${v.accent} ${v.who}${v.id === usual ? ' · its usual voice' : ''}`))),
          btn);
      }));
  }
  function agentBlock(a, voices) {
    const note = h('p', { class: 'when appearance-note', role: 'status' });
    const english = voices.filter((v) => (v.lang || 'en') === 'en'), french = voices.filter((v) => v.lang === 'fr');
    const list = choiceList(a, english, false, note);
    const frNote = h('p', { class: 'when appearance-note', role: 'status' });
    const frBlock = french.length ? [h('h3', { class: 'label voice-lang' }, 'In French'),
      h('p', { class: 'when' }, 'When it answers in French (to someone who spoke French to it).'),
      choiceList(a, french, true, frNote), frNote] : [];
    const where = a.speaks ? (a.shared ? 'Answers the speaker. Shared: either of you can change it' : 'Answers the speaker')
      : 'Not on the speaker yet: this is the voice it will have';
    const section = h('section', { 'aria-label': `${a.display_name}'s voice` },
      h('h2', { class: 'label' }, a.display_name), h('p', { class: 'when' }, where), list, note, ...frBlock);
    if (a.speaks) return section;
    // An agent not on the speaker yet: folded away behind one line, so Home's list is not lost below it.
    const now = (voices.find((v) => v.id === a.voice) || {}).name || '';
    return h('details', { class: 'colours-more' }, h('summary', {}, h('span', {}, a.display_name), h('span', { class: 'when' }, now)), section);
  }
  getJSON('/api/v2/speaker').then((d) => {
    if (!alive) return;
    const agents = d.agents || [];
    if (!agents.length) { box.replaceChildren(h('p', { class: 'none' }, 'You have no agents yet.')); return; }
    box.replaceChildren(...(d.on ? [] : [h('p', { class: 'none' }, 'The speaker voices are not switched on on this Logos yet. What you choose is kept for when they are.')]),
      ...agents.map((a) => agentBlock(a, d.voices || [])));
  }).catch(() => box.replaceChildren(h('p', { class: 'none' }, 'The voices could not be loaded just now.')));
  return () => { alive = false; stop(); Object.values(urls).forEach((u) => URL.revokeObjectURL(u)); };
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
    h('section', { 'aria-label': 'Your voice' }, heading('Your voice'), intro, printBox),
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

// What each row says it is set to now; a promise where it has to be asked for.
function valueOf(id, state) {
  if (id === 'appearance') {
    const { look, mode } = currentLook();
    const name = (LOOKS.find((l) => l.id === look) || {}).name || look;
    return `${{ glass: 'Glass', hud: 'HUD', warm: 'Warm' }[look] || name} · ${{ dark: 'Dark', light: 'Light', auto: 'Auto' }[mode] || mode}`;
  }
  if (id === 'matrix') return state.me.matrix_id ? (mx.saved() ? 'Connected' : 'Not connected') : '';
  if (id === 'notifications') return push.status().then((st) => (st === 'on'
    ? (getPref('alerts', 'who') === 'private' ? 'On · private' : 'On') : { blocked: 'Blocked', unsupported: '' }[st] ?? 'Off'));
  if (id === 'email') return getJSON('/api/v2/mail').then((d) => {
    const b = d.mailboxes || [];
    return b.length > 1 ? `${b.length} mailboxes` : b.length ? ({ gmail: 'Gmail', outlook: 'Outlook' }[b[0].kind] || 'Connected') : 'Not connected';
  });
  if (id === 'metrics') return getJSON('/api/v2/metrics').then((d) => (d.enabled ? 'On' : 'Off'));
  if (id === 'speaker') return getJSON('/api/v2/speaker').then((d) => {
    const a = (d.agents || []).find((x) => x.speaks) || (d.agents || [])[0];
    return a ? ((d.voices || []).find((v) => v.id === a.voice) || {}).name || '' : '';
  });
  if (id === 'voice') return getJSON('/api/v2/voiceprint').then((d) => (!d.enrolled ? 'Not set up' : (d.summary || {}).state === 'active' ? 'Recognising' : 'Learning'));
  if (id === 'updates') return getJSON('/api/v2/updates?check=1').then((d) => {
    const n = ((d.plan || {}).commits || []).length;
    return n ? `${n} to install` : 'Up to date';
  });
  return '';
}

function row({ href, name, glyph, value, current, cls = '' }) {
  const val = h('span', { class: 'set-value' });
  Promise.resolve(value).then((v) => { val.textContent = v || ''; if (/to install|Not connected|Off|Blocked/.test(v || '')) val.classList.add('attention'); })
    .catch(() => {});
  return h('a', { href, class: `set-row${cls}`, 'aria-current': current ? 'page' : null },
    h('span', { class: 'set-icon' }, icon(ICONS[glyph])), h('span', { class: 'set-name' }, name), val,
    h('span', { class: 'set-chevron' }, icon(ICONS.chevron)));
}

function index(state, id) {
  const isAdmin = state.me.role === 'admin';
  const shown = (g) => SECTIONS.filter((s) => s.group === g && (!s.admin || isAdmin));
  const shownName = state.me.display_name || state.me.username;
  const group = (title, rows) => (rows.length ? h('section', { class: 'set-group', 'aria-label': title },
    h('h2', { class: 'set-group-title' }, title), ...rows) : '');
  const sectionRow = (s) => row({ href: `#/settings/${s.id}`, name: s.name, glyph: s.icon, value: valueOf(s.id, state), current: s.id === id });
  return h('nav', { class: 'set-index', 'aria-label': 'Settings' },
    h('a', { href: '#/settings/account', class: 'set-profile', 'aria-current': id === 'account' ? 'page' : null },
      h('span', { class: 'set-avatar', 'aria-hidden': 'true' }, (shownName || '?').trim().charAt(0).toUpperCase()),
      h('span', { class: 'set-who' }, h('strong', {}, shownName),
        h('span', { class: 'when' }, `${state.me.username} · ${isAdmin ? 'Manages Logos' : 'Member'}`)),
      h('span', { class: 'set-chevron' }, icon(ICONS.chevron))),
    group('You', shown('You').map(sectionRow)),
    group('This device', shown('This device').map(sectionRow)),
    isAdmin ? group('Logos', [...shown('Logos').map(sectionRow),
      ...ELSEWHERE.map((e) => row({ href: e.href, name: e.name, glyph: e.icon, cls: ' set-elsewhere' }))]) : '',
    h('section', { class: 'set-group set-end' },
      h('button', { type: 'button', class: 'set-row set-danger', onclick: () => { const b = document.querySelector('.logout-btn'); if (b) b.click(); } },
        h('span', { class: 'set-icon' }, icon(ICONS.logout)), h('span', { class: 'set-name' }, 'Sign out'))),
    versionLine(state));
}

// The end of the list, as in Element X: which Logos this is (the commit, and when it was made).
function versionLine(state) {
  const v = state.me.version || {};
  if (!v.commit) return '';
  const day = v.date ? new Date(v.date * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }) : '';
  return h('p', { class: 'set-version' }, `Logos ${v.commit}${day ? ` · ${day}` : ''}`);
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
  if (!wide && id) {
    // The top bar names the page (as on every screen); the back link sits above it, Element X style.
    const name = document.getElementById('screen-name');
    if (name) name.textContent = sec.full;
    page.append(h('header', { class: 'set-head' },
      h('a', { href: '#/settings', class: 'set-back' }, icon(ICONS.back), h('span', {}, 'Settings'))), body);
  } else if (!wide) {
    page.append(index(state, null));
  } else {
    page.append(h('header', {}, h('h1', {}, 'Settings')), h('div', { class: 'settings-layout' }, index(state, id), body));
  }
  root.replaceChildren(page);
  if (id === 'appearance') body.replaceChildren(...appearance(), ...chat());
  else if (id === 'account') body.replaceChildren(...account(state));
  else if (id === 'matrix') body.replaceChildren(...matrix(state));
  else if (id === 'notifications') body.replaceChildren(...notifications(state));
  else if (id === 'tips') body.replaceChildren(...tips());
  else if (id === 'metrics') body.replaceChildren(...metricsPage());
  else if (id === 'email') done = email(body);
  else if (id === 'speaker') done = speaker(body);
  else if (id === 'voice') done = yourVoice(body);
  else if (id === 'updates') done = await updatesView(body, [], state, true);
  // Turning a foldable or resizing across the line: the other layout.
  const mq = matchMedia(WIDE);
  const flip = () => { if (root.contains(page)) settingsView(root, [section], state); };
  mq.addEventListener('change', flip);
  return () => { mq.removeEventListener('change', flip); if (done) done(); };
}
