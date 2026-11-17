// Updates screen (admins): what is new on v2 for this machine, and "Update now".
// The update runs as its own job on the server (scripts/logos-deploy), so it carries on
// while it restarts this dashboard; this screen keeps asking how it is going.
import { h, api, getJSON } from './util.js';

const STATE_TEXT = {
  running: ['Updating…', ''],
  done: ['Last update finished', 'up'],
  problems: ['Last update finished with problems', 'down'],
  failed: ['Last update failed', 'down'],
};

const time = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '');

export async function updatesView(root, _args, state) {
  if (state.me.role !== 'admin') {
    root.replaceChildren(h('div', { class: 'empty' }, h('h1', {}, 'Updates'),
      h('p', {}, 'Only someone who manages Logos can update it.')));
    return null;
  }
  let alive = true;
  let timer = null;
  let wasRunning = false;
  const planBox = h('section', { class: 'card', 'aria-label': 'Available update' });
  const lastBox = h('section', { class: 'card', 'aria-label': 'Last update' });
  const note = h('div', { class: 'result when', role: 'status' });

  function renderPlan(d) {
    const p = d.plan;
    if (d.running) {
      planBox.replaceChildren(h('h2', {}, 'Updating now'),
        h('p', { class: 'when' }, 'You can leave this screen open. The dashboard restarts during the update, so it may go quiet for a few seconds.'));
      return;
    }
    if (!p) { planBox.replaceChildren(h('h2', {}, 'Checking for updates…')); return; }
    const recheck = h('button', { type: 'button', class: 'btn quiet', onclick: () => refresh(true, true) }, 'Check again');
    if (p.refused) {
      planBox.replaceChildren(h('h2', {}, 'Cannot update this machine'),
        h('p', { class: 'notice', style: 'white-space: pre-wrap' }, p.refused), h('div', { class: 'row' }, recheck));
      return;
    }
    if (!p.commits.length) {
      planBox.replaceChildren(h('h2', {}, 'Up to date'),
        h('p', { class: 'when' }, `Running ${p.before.slice(0, 7)}, the latest on v2.`), h('div', { class: 'row' }, recheck));
      return;
    }
    const go = h('button', { type: 'button', class: 'btn', onclick: async () => {
      go.disabled = true; note.textContent = 'Starting…';
      const r = await api('/api/v2/updates', { method: 'POST' });
      const out = await r.json().catch(() => ({}));
      note.textContent = r.ok ? '' : (out.error || 'Could not start the update.');
      if (!r.ok) go.disabled = false;
      refresh(false);
    } }, 'Update now');
    planBox.replaceChildren(
      h('h2', {}, `${p.commits.length} change${p.commits.length === 1 ? '' : 's'} to install`),
      h('ul', { class: 'plain' }, p.commits.map((c) => h('li', {}, h('code', {}, c.sha), ' ', c.subject))),
      h('h3', {}, 'What will happen'),
      h('ul', {}, p.steps.map((s) => h('li', {}, s))),
      h('p', { class: 'when' }, `Agents running here: ${p.running.join(', ') || 'none'}. Brains, memory and Telegram connections are kept.`),
      h('div', { class: 'row' }, go, recheck), note);
  }

  function renderLast(d) {
    const s = d.status;
    if (!s && !d.output.length) { lastBox.hidden = true; return; }
    lastBox.hidden = false;
    const [label, cls] = STATE_TEXT[d.running ? 'running' : s?.state] || ['Last update', ''];
    lastBox.replaceChildren(
      h('div', { class: 'card-head' }, h('h2', {}, label), cls ? h('span', { class: `status ${cls}` }, '') : null),
      s ? h('p', { class: 'when' },
        [s.before && s.after ? `${s.before.slice(0, 7)} → ${s.after.slice(0, 7)}` : '',
          s.started ? `started ${time(s.started)}` : '', s.finished ? `finished ${time(s.finished)}` : '']
          .filter(Boolean).join(' · ')) : null,
      s?.error ? h('p', { class: 'notice', style: 'white-space: pre-wrap' }, s.error) : null,
      d.output.length ? h('details', { open: d.running || (s && s.state !== 'done') },
        h('summary', {}, 'Output'), h('pre', { class: 'log-output' }, d.output.join('\n'))) : null);
  }

  async function refresh(check = true, fresh = false) {
    clearTimeout(timer);
    let d;
    try {
      d = await getJSON(`/api/v2/updates${check ? '?check=1' : ''}${fresh ? '&fresh=1' : ''}`);
    } catch (e) {
      // The dashboard restarting mid-update: try again shortly.
      if (alive) timer = setTimeout(() => refresh(false), 2000);
      return;
    }
    if (!alive) return;
    renderPlan(d);
    renderLast(d);
    if (d.running) { wasRunning = true; timer = setTimeout(() => refresh(false), 2000); }
    else if (wasRunning) { wasRunning = false; refresh(true, true); }   // finished: show what is left
  }

  root.replaceChildren(h('div', { class: 'page' },
    h('header', {}, h('h1', {}, 'Updates'),
      h('p', { class: 'when lead' }, 'Install the latest Logos from the v2 branch on this machine. Only the parts that changed are restarted.')),
    planBox, lastBox));
  lastBox.hidden = true;
  await refresh(true);
  return () => { alive = false; clearTimeout(timer); };
}
