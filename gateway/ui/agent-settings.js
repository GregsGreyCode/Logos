// Agent settings (story E6): an agent's personality, model and chat settings, for the people it belongs
// to (and whoever manages Logos). Reached from the agent's menu (Chat | Brain | Routines | Settings) and
// from its Routines page: #/agents/<name>/settings. The choices are kept on this machine, outside the
// public repo (gateway/agent_settings.py); "Save and restart" recreates the agent's sandbox from them,
// the same as scripts/logos-agent up. See docs/agent-settings.md.
import { h, api, getJSON, loading, when, ask, modelName } from './util.js';
import { agentBar } from './agents-void.js';

const n = (x) => Number(x || 0).toLocaleString();

// Lines removed and added between two texts (a longest common subsequence: souls are a few hundred lines).
export function lineDiff(a, b) {
  const x = a.replace(/\n+$/, '').split('\n'), y = b.replace(/\n+$/, '').split('\n');
  const L = Array.from({ length: x.length + 1 }, () => new Uint16Array(y.length + 1));
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < x.length || j < y.length) {
    if (i < x.length && j < y.length && x[i] === y[j]) { out.push([' ', x[i]]); i++; j++; }
    else if (j < y.length && (i === x.length || L[i][j + 1] >= L[i + 1][j])) { out.push(['+', y[j]]); j++; }
    else { out.push(['-', x[i]]); i++; }
  }
  return out;
}

function diffView(before, after) {
  const rows = lineDiff(before, after);
  if (!rows.some(([k]) => k !== ' ')) return h('p', { class: 'when' }, 'No changes.');
  // Unchanged lines far from a change fold into "…", so a one-line edit shows as a few lines.
  const near = rows.map((_, i) => rows.slice(Math.max(0, i - 2), i + 3).some(([k]) => k !== ' '));
  const out = [];
  rows.forEach(([k, t], i) => {
    if (k === ' ' && !near[i]) { if (out.length && out[out.length - 1] !== '…') out.push('…'); return; }
    out.push(h('div', { class: `diff-line${k === '+' ? ' add' : k === '-' ? ' del' : ''}` },
      h('span', { class: 'diff-mark', 'aria-label': k === '+' ? 'added' : k === '-' ? 'removed' : null }, k === ' ' ? '' : k), t || ' '));
  });
  return h('div', { class: 'diff', role: 'region', 'aria-label': 'What changes' },
    ...out.map((r) => (r === '…' ? h('div', { class: 'diff-line skip' }, '…') : r)));
}

export async function agentSettingsView(root, name) {
  let alive = true, ready = false, running = false;
  let poll = null;
  root.replaceChildren(h('div', { class: 'page agents-page' }, h('h1', {}, 'Agent settings'), loading('cards', 3)));
  let v, agents = [];
  try {
    [v, agents] = await Promise.all([getJSON(`/api/v2/agents/${name}/settings`),
      getJSON('/api/v2/agents').then((d) => d.agents || []).catch(() => [])]);
  } catch (e) {
    root.replaceChildren(h('div', { class: 'page' }, h('h1', {}, 'Agent settings'),
      h('div', { class: 'notice' }, e.status === 404 ? 'Only the people this agent belongs to can change it.' : 'The settings could not be loaded just now.')));
    return () => {};
  }
  const agent = agents.find((a) => a.name === name) || { name, display_name: v.display_name };
  document.title = `${v.display_name}: settings · Logos`;

  // ── Personality ───────────────────────────────────────────────────────────────────────────
  const soul = h('textarea', { id: 'soul-text', class: 'soul-text', rows: '14', spellcheck: 'true', maxlength: String(v.soul.limit) });
  soul.value = v.soul.text;
  const count = h('span', { class: 'when soul-count' });
  const preview = h('div', { class: 'soul-preview', hidden: true });
  const badge = h('span', { class: `chip-tag${v.soul.custom ? ' on' : ''}` });
  const history = h('details', { class: 'routines-fold soul-history' });
  const soulNote = h('p', { class: 'when', role: 'status' });
  const recount = () => {
    count.textContent = `${n(soul.value.length)} of ${n(v.soul.limit)} characters`;
    count.classList.toggle('bad', soul.value.length > v.soul.limit);
    if (!preview.hidden) preview.replaceChildren(diffView(v.soul.text, soul.value));
    dirty();
  };
  soul.addEventListener('input', recount);
  const showPreview = () => {
    preview.hidden = !preview.hidden;
    previewBtn.textContent = preview.hidden ? 'Preview changes' : 'Hide preview';
    if (!preview.hidden) preview.replaceChildren(diffView(v.soul.text, soul.value));
  };
  const previewBtn = h('button', { type: 'button', class: 'btn quiet', onclick: showPreview }, 'Preview changes');
  const resetBtn = h('button', { type: 'button', class: 'btn quiet danger-text', onclick: async () => {
    if (!await ask({ title: 'Back to the default personality?', text: 'The one in use now is kept in the history, so you can restore it.', yes: 'Reset' })) return;
    await save({ soul_reset: true }, soulNote, 'Back to the default.');
  } }, 'Reset to default');

  function drawSoul() {
    badge.textContent = v.soul.custom ? 'Your own version' : 'The default';
    badge.classList.toggle('on', v.soul.custom);
    resetBtn.hidden = !v.soul.custom;
    const rows = v.soul.history || [];
    history.replaceChildren(h('summary', {}, `Earlier versions (${rows.length})`),
      rows.length ? h('div', { class: 'rows' }, ...rows.map((r, i) => {
        const shown = h('div', { class: 'soul-old', hidden: true });
        const what = { edited: 'Edited', restored: 'Restored', default: 'The default' }[r.what] || r.what;
        return h('div', { class: 'row-item soul-version' },
          h('span', { class: 'row-main' }, `${what}${i === 0 && v.soul.custom ? ' · in use' : ''}`),
          h('span', { class: 'row-meta' }, [when(r.ts), r.by ? `by ${r.by}` : '', `${n(r.chars)} characters`].filter(Boolean).join(' · ')),
          h('div', { class: 'row' },
            h('button', { type: 'button', class: 'btn quiet small', onclick: async (e) => {
              if (!shown.hidden) { shown.hidden = true; e.target.textContent = 'Compare with the text above'; return; }
              const d = await getJSON(`/api/v2/agents/${name}/settings/soul/${r.id}`).catch(() => null);
              shown.replaceChildren(d ? diffView(soul.value, d.text) : h('p', { class: 'when' }, 'That version could not be loaded.'));
              shown.hidden = false; e.target.textContent = 'Hide';
            } }, 'Compare with the text above'),
            h('button', { type: 'button', class: 'btn quiet small', onclick: async () => {
              if (!await ask({ title: 'Restore this version?', text: 'It replaces the text above. The one in use now stays in the history.', yes: 'Restore' })) return;
              await save({ soul_restore: r.id }, soulNote, 'Restored.');
            } }, 'Restore')),
          shown);
      })) : h('p', { class: 'when' }, 'None yet: each saved version is kept here (the last 20).'));
  }

  const personality = h('section', { class: 'card settings-card', 'aria-labelledby': 'set-soul' },
    h('div', { class: 'card-head' }, h('h2', { id: 'set-soul' }, 'Personality'), badge),
    h('p', { class: 'when' }, `How ${v.display_name} talks and what it should keep in mind. The rules every agent follows (its brain, honesty, the tools it has) are added by Logos and stay the same.`),
    h('label', { for: 'soul-text', class: 'sr' }, 'Personality and instructions'), soul,
    h('div', { class: 'row soul-tools' }, count, previewBtn, resetBtn), preview, soulNote, history);

  // ── Model ─────────────────────────────────────────────────────────────────────────────────
  const m = v.model;
  const pick = h('select', { id: 'model-pick' });
  const windowPick = h('select', { id: 'model-window' });
  const modelNote = h('p', { class: 'when', role: 'status' });
  const choices = m.choices.length ? m.choices : [{ id: m.default, loaded: null, vision: m.vision, max_context: 0 }];
  for (const c of choices) {
    const tags = [c.loaded ? 'loaded' : c.loaded === false ? 'not loaded' : '', c.vision ? 'sees photos' : '',
      c.max_context ? `up to ${n(c.max_context)} tokens` : ''].filter(Boolean).join(' · ');
    pick.append(h('option', { value: c.id, selected: c.id === m.default ? '' : null }, `${modelName(c.id)}${c.id === m.definition.default ? ' (default)' : ''}${tags ? ` — ${tags}` : ''}`));
  }
  const fillWindows = () => {
    const c = choices.find((x) => x.id === pick.value) || {};
    const sizes = [...new Set([...m.context_choices, m.context_length])].sort((a, b) => a - b)
      .filter((s) => !c.max_context || s <= c.max_context);
    const keep = Number(windowPick.value) || m.context_length;
    windowPick.replaceChildren(...sizes.map((s) => h('option', { value: String(s), selected: s === (sizes.includes(keep) ? keep : sizes[sizes.length - 1]) ? '' : null },
      `${n(s)} tokens${s === m.definition.context_length ? ' (default)' : ''}`)));
    dirty();
  };
  pick.addEventListener('change', fillWindows);
  windowPick.addEventListener('change', dirty);
  fillWindows();
  const canModel = m.local && m.server_up && !m.locked;
  pick.disabled = windowPick.disabled = !canModel;
  const model = h('section', { class: 'card settings-card', 'aria-labelledby': 'set-model' },
    h('div', { class: 'card-head' }, h('h2', { id: 'set-model' }, 'Model'), m.custom ? h('span', { class: 'chip-tag on' }, 'Changed here') : null),
    h('p', { class: 'when' }, 'The model on the AI computer that writes its answers, and how much of a conversation it can hold at once.'),
    m.locked ? h('div', { class: 'notice' }, m.locked)
      : !m.local ? h('div', { class: 'notice' }, 'This agent does not use the local model server, so its model is not changed here.')
        : !m.server_up ? h('div', { class: 'notice' }, 'The AI computer is not answering, so the models cannot be listed or changed now.') : null,
    h('label', { class: 'routine-field' }, h('span', { class: 'when' }, 'Model'), pick),
    h('label', { class: 'routine-field' }, h('span', { class: 'when' }, 'Context window'), windowPick),
    h('p', { class: 'when' }, 'Bigger remembers more of a long chat but needs more graphics memory and is slower. Its rules and memory take about 8,000 tokens.'),
    modelNote);

  // ── Chat ──────────────────────────────────────────────────────────────────────────────────
  const c = v.chat;
  const field = (id, label, input, hint, locked) => h('div', { class: 'set-field' },
    h('label', { for: id, class: 'field-label' }, label), input, locked ? h('p', { class: 'when' }, locked) : hint ? h('p', { class: 'when' }, hint) : null);
  const dn = h('input', { type: 'text', id: 'chat-name', maxlength: '40', value: c.display_name.value, disabled: c.display_name.locked ? '' : null });
  dn.addEventListener('input', dirty);
  let echo = null, spk = null;
  const chatRows = [field('chat-name', 'Name', dn, `Shown in Logos and as its name in chats. Default: ${c.display_name.default}.`, c.display_name.locked)];
  if (c.voice_echo) {
    echo = h('input', { type: 'checkbox', id: 'chat-echo', checked: c.voice_echo.value ? '' : null, disabled: c.voice_echo.locked ? '' : null });
    echo.addEventListener('change', dirty);
    chatRows.push(h('div', { class: 'set-field set-switch' },
      h('label', { class: 'switch' }, echo, h('span', { class: 'switch-track', 'aria-hidden': 'true' })),
      h('label', { for: 'chat-echo' }, h('strong', {}, 'Show what it heard'), h('span', { class: 'when' }, ' The words of each voice note, before the answer, so you can tell it heard you right.')),
      c.voice_echo.locked ? h('p', { class: 'when' }, c.voice_echo.locked) : null));
  }
  if (c.speaker_name) {
    spk = h('input', { type: 'text', id: 'chat-speaker', maxlength: '20', value: c.speaker_name.value, disabled: c.speaker_name.locked ? '' : null });
    spk.addEventListener('input', dirty);
    chatRows.push(field('chat-speaker', 'Name at the house speaker', spk,
      `The speaker wakes to "${(c.speaker_name.wake || [])[0] || `Hey ${c.speaker_name.value}`}". One or two words; "Hey" is added.`, c.speaker_name.locked));
  }
  chatRows.push(h('a', { class: 'set-row set-link', href: '#/settings/speaker' }, h('span', { class: 'set-name' }, 'Speaker voice'),
    h('span', { class: 'set-value' }, 'In Settings'), h('span', { class: 'set-chevron', 'aria-hidden': 'true' }, '›')));
  const chat = h('section', { class: 'card settings-card', 'aria-labelledby': 'set-chat' }, h('h2', { id: 'set-chat' }, 'Chat'), ...chatRows);

  // ── Save, and restart ─────────────────────────────────────────────────────────────────────
  const status = h('p', { class: 'apply-status', role: 'status' });
  const bar = h('div', { class: 'apply-bar', hidden: true }, h('span', { class: 'apply-fill' }));
  const lines = h('pre', { class: 'apply-lines' });
  const linesBox = h('details', { class: 'routines-fold', hidden: true }, h('summary', {}, 'What it did'), lines);
  const saveBtn = h('button', { type: 'button', class: 'btn quiet', onclick: () => saveAll(false) }, 'Save');
  const goBtn = h('button', { type: 'button', class: 'btn', onclick: () => saveAll(true) }, 'Save and restart the agent');
  const apply = h('section', { class: 'card settings-card apply-card', 'aria-labelledby': 'set-apply' },
    h('h2', { id: 'set-apply' }, 'Apply'),
    h('p', { class: 'when' }, `Changes are used once ${v.display_name} restarts. That takes about a minute, and it does not reply meanwhile; `
      + 'if it is writing a reply, Logos waits for it to finish first. Its memory, brain and chats are kept.'),
    status, bar, h('div', { class: 'row' }, goBtn, saveBtn), linesBox);

  // What changed on the page since it was loaded or saved.
  function changes() {
    const out = {};
    const vm = v.model, vc = v.chat;
    if (soul.value !== v.soul.text) out.soul = soul.value;
    if (canModel && (pick.value !== vm.default || Number(windowPick.value) !== vm.context_length)) out.model = { default: pick.value, context_length: Number(windowPick.value) };
    if (!vc.display_name.locked && dn.value.trim() !== vc.display_name.value) out.display_name = dn.value;
    if (echo && !vc.voice_echo.locked && echo.checked !== vc.voice_echo.value) out.voice_echo = echo.checked;
    if (spk && !vc.speaker_name.locked && spk.value.trim() !== vc.speaker_name.value) out.speaker_name = spk.value;
    return out;
  }
  function dirty() {
    if (!ready) return;
    const k = Object.keys(changes()).length;
    saveBtn.disabled = running || !k;
    goBtn.disabled = running;
    goBtn.textContent = k ? 'Save and restart the agent' : 'Restart the agent';
    if (!running) status.textContent = k ? `${k === 1 ? 'One change' : `${k} changes`} not saved yet.` : v.pending ? 'Saved, but not in use until the agent restarts.' : '';
    status.classList.toggle('attention', !running && (k > 0 || v.pending));
  }

  async function put(body, note) {
    let r = await api(`/api/v2/agents/${name}/settings`, { method: 'PUT', body }).catch(() => null);
    let d = r ? await r.json().catch(() => ({})) : {};
    if (r && r.status === 409 && d.error === 'confirm') {
      const text = (d.warnings || []).map((w) => w.text).join('\n\n');
      if (!await ask({ title: 'Change the model?', text, yes: 'Change it' })) { note.textContent = 'Not changed.'; return false; }
      r = await api(`/api/v2/agents/${name}/settings`, { method: 'PUT', body: { ...body, confirm: true } }).catch(() => null);
      d = r ? await r.json().catch(() => ({})) : {};
    }
    if (!r || !r.ok) { note.textContent = d.message || 'Not saved: Logos did not answer.'; return false; }
    v = { ...v, ...d };
    soul.value = v.soul.text;
    drawSoul(); recount();
    const warn = (d.warnings || []).filter((w) => !w.confirm).map((w) => w.text);
    modelNote.textContent = warn.join(' ');
    return true;
  }
  async function save(body, note, done) {
    if (await put(body, note)) { note.textContent = done; dirty(); }
  }
  async function saveAll(restart) {
    const body = changes();
    if (Object.keys(body).length) {
      status.textContent = 'Saving…';
      if (!await put(body, status)) return;
      status.textContent = 'Saved.';
    }
    if (!restart) { dirty(); return; }
    if (!await ask({ title: `Restart ${v.display_name}?`, text: 'It does not reply for about a minute. If it is writing a reply now, Logos waits for it to finish.', yes: 'Restart' })) { dirty(); return; }
    const r = await api(`/api/v2/agents/${name}/settings/apply`, { method: 'POST' }).catch(() => null);
    const d = r ? await r.json().catch(() => ({})) : {};
    if (!r || r.status !== 202) { status.textContent = d.message || 'It could not be restarted just now.'; return; }
    follow(d);
  }

  // Progress: about a minute; the bar fills over 75 seconds and waits at the end.
  function follow(job) {
    running = ['waiting', 'restarting'].includes(job.state);
    bar.hidden = !running && job.state !== 'done';
    bar.classList.toggle('waiting', job.state === 'waiting');
    bar.firstChild.style.width = job.state === 'done' ? '100%' : job.state === 'restarting' ? `${Math.min(95, 5 + (job.seconds || 0) * 1.2)}%` : '5%';
    if (job.state !== 'idle') {
      status.textContent = `${job.message || ''}${running ? ` (${job.seconds || 0} s)` : ''}`;
      status.classList.toggle('bad', job.state === 'failed');
      status.classList.toggle('good', job.state === 'done');
    }
    lines.textContent = (job.lines || []).join('\n');
    linesBox.hidden = !(job.lines || []).length;
    for (const el of root.querySelectorAll('.settings-card textarea, .settings-card input, .settings-card select, .settings-card button')) {
      if (!el.closest('.soul-history') && !el.closest('.apply-card')) el.disabled = running || (el.dataset.off === '1');
    }
    saveBtn.disabled = goBtn.disabled = running;
    if (running && alive) poll = setTimeout(async () => {
      const next = await getJSON(`/api/v2/agents/${name}/settings/apply`).catch(() => job);
      if (!alive) return;
      if (next.state === 'done') v.pending = false;
      follow(next);
    }, 2000);
    else if (job.state === 'done' || job.state === 'failed') { running = false; pick.disabled = windowPick.disabled = !canModel; dirty(); status.textContent = job.message || ''; }
  }

  for (const el of [pick, windowPick, dn, echo, spk].filter(Boolean)) if (el.disabled) el.dataset.off = '1';
  ready = true;
  drawSoul(); recount();
  const title = h('h1', { class: 'agents-title visually-hidden' }, `${v.display_name}: settings`);   // the agent bar says it (UI review, 28 Nov)
  root.replaceChildren(h('div', { class: 'page agents-page agent-settings' }, agentBar(agent, 'settings', agents), title,
    h('p', { class: 'routines-intro' }, v.shared ? 'Shared: everyone it belongs to can change these.' : `What ${v.display_name} is like, and how it answers.`),
    h('div', { class: 'settings-cards' }, personality, model, chat, apply)));
  if (v.apply && v.apply.state !== 'idle') follow(v.apply);
  return () => { alive = false; clearTimeout(poll); };
}
