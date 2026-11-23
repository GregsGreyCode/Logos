// Small helpers shared by every screen.

// "Greg's agent" -> "Greg's", "Household agent" -> "Household": for a phone's narrow switches.
export const shortName = (name) => String(name || '').replace(/\s+agent$/i, '') || name;

// The full name for wide screens and the short one for phones; CSS shows one of the two.
export function agentLabel(name) {
  return [h('span', { class: 'name-full' }, name), h('span', { class: 'name-short', 'aria-hidden': 'true' }, shortName(name))];
}

// SVG icons (h() makes HTML elements, which cannot draw SVG).
export function icon(paths) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'icon');
  for (const d of paths) {
    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

// A few shared icon shapes (24-unit stroke paths), for buttons whose words are too long on a phone.
export const ICONS = {
  clip: ['M21 11.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l9.2-9.2a3.7 3.7 0 0 1 5.2 5.2l-9.2 9.2a1.8 1.8 0 0 1-2.6-2.6l8.5-8.5'],
  file: ['M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z', 'M14 3v5h5', 'M9 13h6M9 17h6'],
  send: ['M5 12h14', 'M13 6l6 6-6 6'],
  external: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5'],
  lock: ['M6 11h12v9H6z', 'M8 11V8a4 4 0 0 1 8 0v3'],
  eye: ['M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z'],
  eyeOff: ['M3 3l18 18', 'M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.2', 'M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6'],
  mic: ['M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z', 'M5 11a7 7 0 0 0 14 0', 'M12 18v3'],
  home: ['M4 11l8-6 8 6v8H4z', 'M10 19v-5h4v5'],
  chat: ['M4 5h16v11H9l-5 4z'],
  doc: ['M5 4h10l4 4v12H5z', 'M9 12h6M9 16h6'],
  bell: ['M6 9a6 6 0 0 1 12 0c0 6 2 7 2 7H4s2-1 2-7', 'M10 20a2 2 0 0 0 4 0'],
  clock: ['M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18', 'M12 7v5l3 2'],
  check: ['M5 12l5 5 9-10'],
  agents: ['M12 3l8 4.5v9L12 21l-8-4.5v-9z', 'M12 12l8-4.5', 'M12 12v9', 'M12 12L4 7.5'],
  shield: ['M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z', 'M8.5 12l2.5 2.5 4.5-5'],
  chip: ['M7 7h10v10H7z', 'M10 3v4', 'M14 3v4', 'M10 17v4', 'M14 17v4', 'M3 10h4', 'M3 14h4', 'M17 10h4', 'M17 14h4'],
  people: ['M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M2 21v-1a6 6 0 0 1 12 0v1', 'M16 3.5a4 4 0 0 1 0 7', 'M22 21v-1a6 6 0 0 0-4-5.6'],
  gear: ['M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z'],
  logout: ['M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4', 'M16 17l5-5-5-5', 'M21 12H9'],
};

// On a phone a table becomes a list of small blocks; each cell is labelled with its column.
export function labelTables(root) {
  for (const table of root.querySelectorAll('table')) {
    const heads = [...table.querySelectorAll('thead th')].map((th) => th.textContent.trim());
    for (const row of table.querySelectorAll('tbody tr')) {
      [...row.children].forEach((td, i) => { if (heads[i] && !td.dataset.label) td.dataset.label = heads[i]; });
    }
  }
}

// The Logos mark is grey at rest and cycles through colours while an agent is working.
const busy = new Set();
export function working(who, on) {
  if (on) busy.add(who); else busy.delete(who);
  for (const el of document.querySelectorAll('.logo')) el.classList.toggle('busy', busy.size > 0);
}
// The logo says whether the AI computer is on (Greg, 21 Nov): in colour when it is, grey when it is off,
// cycling while an agent works. up: true, false, or null (no agent uses a local model: shown as on).
export function aiState(up, offSince = null) {
  const off = up === false;
  document.documentElement.classList.toggle('ai-off', off);
  const at = offSince ? new Date(offSince * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  const words = off ? `AI computer off${at ? ` since ${at}` : ''} · replies when it's back` : 'AI computer on';
  for (const el of document.querySelectorAll('.logo')) {
    el.dataset.ai = words;
    if (!el.classList.contains('busy')) el.title = words;
  }
  for (const a of document.querySelectorAll('.logo-link')) a.title = `Logos · Home · ${words}`;
}
// "Nothing here" is always the same quiet line: no box, no table, no logo.
export const none = (text) => h('p', { class: 'none' }, text);
// Loading (Greg, 22 Nov): the shape of what is coming, softly shimmering, instead of "Loading…": rows (a
// list), cards, bubbles (a chat), tiles or a card. It fades in only if the wait is noticeable, so a quick
// answer never flashes it. Screen readers hear "Loading".
const W = [72, 54, 86, 64, 78, 48];
const SHAPES = {
  rows: (i) => h('div', { class: 'sk-row' }, h('span', { class: 'sk sk-dot' }),
    h('span', { class: 'sk-lines' }, h('span', { class: 'sk sk-line', style: `width:${W[i % 6]}%` }), h('span', { class: 'sk sk-line thin', style: `width:${W[(i + 3) % 6] - 18}%` }))),
  cards: (i) => h('div', { class: 'sk sk-card' }, h('span', { class: 'sk-in', style: `width:${W[i % 6] - 20}%` }), h('span', { class: 'sk-in thin', style: `width:${W[(i + 2) % 6]}%` })),
  bubbles: (i) => h('div', { class: `sk sk-bubble${i % 2 ? ' me' : ''}`, style: `width:${W[i % 6]}%;height:${i % 3 ? 44 : 68}px` }),
  tiles: () => h('div', { class: 'sk sk-tile' }),
  card: () => h('div', { class: 'sk sk-card tall' }),
};
export const loading = (shape = 'rows', n = 3) => h('div', { class: `loading sk-${shape}`, role: 'status', 'aria-label': 'Loading' },
  ...Array.from({ length: n }, (_, i) => (SHAPES[shape] || SHAPES.rows)(i)));

// The first real content after a wait fades in, rather than appearing all at once.
export function reveal(el) {
  if (!el) return;
  // (Not "reveal": that class is the password box with its eye, display: flex; it laid a fading list out
  // in a row for half a second, Greg 23 Nov.)
  el.classList.remove('fade-up');
  void el.offsetWidth;                         // restart the animation
  el.classList.add('fade-up');
  setTimeout(() => el.classList.remove('fade-up'), 600);
}

// Put the cursor in a text box only where there is a real keyboard: on a phone, focusing it pops the
// on-screen keyboard up before the person has chosen to type.
export function focusIfKeyboard(el) {
  if (el && window.matchMedia('(hover: hover) and (pointer: fine)').matches) el.focus();
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null) el.append(c.nodeType ? c : document.createTextNode(c));
  return el;
}

function csrf() {
  const m = document.cookie.match(/(?:^|; )csrf_token=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

// One refresh at a time (F34). The dashboard asks for several things at once, so when the 15-minute
// access token runs out they all get 401 together. Each used to call /auth/refresh with the same
// refresh token: the first rotated it, the others were refused and sent the page to /login, which
// cancelled the first one's reply, so its new cookie was never stored and the person had to sign in
// again (seen on Greg's phone on 21 Nov). Now every 401 waits for the same refresh.
let refreshing = null;
export function refreshSession() {
  if (!refreshing) {
    refreshing = fetch('/auth/refresh', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf() } })
      .then((r) => r.ok).catch(() => false)
      .finally(() => { setTimeout(() => { refreshing = null; }, 2000); });
  }
  return refreshing;
}

// fetch with the session cookie; refreshes an expired session once, else goes to the login page.
export async function api(path, opts = {}, retried = false) {
  const headers = { ...(opts.headers || {}) };
  // opts.body is sent as JSON; opts.raw (a Blob, with its own Content-Type header) is sent as it is.
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = csrf();
  const r = await fetch(path, { ...opts, headers, credentials: 'same-origin',
    body: opts.raw !== undefined ? opts.raw : (opts.body !== undefined ? JSON.stringify(opts.body) : undefined) });
  if (r.status === 401) {
    // A refresh that finished just before this 401 arrived also counts: try once more either way.
    if (!retried) { await refreshSession(); return api(path, opts, true); }
    location.href = '/login';
    throw new Error('signed out');
  }
  return r;
}

export async function getJSON(path) {
  const r = await api(path);
  if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
  return r.json();
}

// When something happened, the same everywhere: "19:54" today, "Yesterday 18:02", then the weekday,
// then the date.
export function when(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (days === 0) return time;            // today: the time alone, everywhere (Greg, 22 Nov)
  if (days === 1) return `Yesterday ${time}`;
  if (days < 7) return d.toLocaleDateString([], { weekday: 'long' });
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

// A daily note's title is its date: shown as "Notes of 21 Nov" everywhere (Brain, Notifications).
export const noteTitle = (t) => (/^\d{4}-\d{2}-\d{2}$/.test(String(t))
  ? `Notes of ${new Date(`${t}T12:00:00`).toLocaleDateString([], { day: 'numeric', month: 'short' })}` : t);

// Asking before something that cannot be undone, or for a password, in Logos's own sheet (Greg, 22 Nov:
// not the browser's grey boxes, whose password prompt showed what was typed). Resolves to true (or the
// password typed) when confirmed, null when cancelled.
export function ask({ title, text = '', yes = 'OK', danger = false, password = '' }) {
  return new Promise((done) => {
    const pw = password ? h('input', { type: 'password', id: 'ask-pw', autocomplete: 'current-password' }) : null;
    const msg = h('p', { class: 'when', role: 'status' });
    const layer = h('div', { class: 'ask-layer' });
    const close = (v) => { layer.remove(); document.removeEventListener('keydown', key); done(v); };
    const key = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', key);
    layer.append(h('div', { class: 'ask-scrim', onclick: () => close(null) }),
      h('form', { class: 'ask-panel', role: 'dialog', 'aria-modal': 'true', 'aria-label': title, onsubmit: (e) => {
        e.preventDefault();
        if (pw && !pw.value) { msg.textContent = 'Enter the password to go on.'; return; }
        close(pw ? pw.value : true);
      } },
      h('h2', {}, title), text ? h('p', {}, text) : null,
      pw ? h('label', { for: 'ask-pw', class: 'field-label' }, password) : null, pw ? revealable(pw) : null, msg,
      h('div', { class: 'row' }, h('button', { type: 'submit', class: `btn${danger ? ' danger' : ''}` }, yes),
        h('button', { type: 'button', class: 'btn quiet', onclick: () => close(null) }, 'Cancel'))));
    document.body.append(layer);
    if (pw) focusIfKeyboard(pw);
  });
}

// A small, safe Markdown subset: everything is escaped by building DOM nodes, never innerHTML.
function inline(text) {
  const out = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[0][0] === '`' ? h('code', {}, m[0].slice(1, -1)) : h('strong', {}, m[0].slice(2, -2)));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function markdown(text) {
  const nodes = [];
  // HTML comments are notes for programs (the lists copy says which Home Assistant list is which), and
  // Obsidian hides them too.
  text = String(text).replace(/<!--[\s\S]*?-->\n?/g, '');
  const parts = text.split(/```[^\n]*\n?/);
  parts.forEach((part, i) => {
    if (i % 2) { nodes.push(h('pre', {}, h('code', {}, part.replace(/\n$/, '')))); return; }
    for (const block of part.split(/\n{2,}/)) {
      const lines = block.split('\n').filter((l) => l.trim());
      if (!lines.length) continue;
      // A table: rows of |cells|, the second row the |---| line under the header.
      if (lines.length >= 2 && lines.every((l) => /^\s*\|.*\|\s*$/.test(l)) && /^[\s|:-]+$/.test(lines[1])) {
        const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        nodes.push(h('div', { class: 'md-table' }, h('table', {},
          h('thead', {}, h('tr', {}, cells(lines[0]).map((c) => h('th', {}, inline(c))))),
          h('tbody', {}, lines.slice(2).map((l) => h('tr', {}, cells(l).map((c) => h('td', {}, inline(c)))))))));
      } else if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
        // Bullets; "- [ ] thing" and "- [x] thing" are to-dos.
        nodes.push(h('ul', {}, lines.map((l) => {
          const item = l.replace(/^\s*[-*]\s+/, '');
          const todo = item.match(/^\[( |x|X)\]\s+(.*)$/);
          if (!todo) return h('li', {}, inline(item));
          const done = todo[1] !== ' ';
          return h('li', { class: `todo${done ? ' done' : ''}` },
            h('span', { class: 'box', role: 'img', 'aria-label': done ? 'Done' : 'To do' }, done ? '\u2611' : '\u2610'), ' ', inline(todo[2]));
        })));
      } else if (lines.every((l) => /^\s*\d+[.)]\s+/.test(l))) {
        nodes.push(h('ol', {}, lines.map((l) => h('li', {}, inline(l.replace(/^\s*\d+[.)]\s+/, ''))))));
      } else {
        // Headings become real headings; other lines of a block stay one paragraph.
        let p = null;
        for (const l of lines) {
          const hm = l.match(/^(#{1,6})\s+(.*)$/);
          if (hm) { p = null; nodes.push(h(hm[1].length === 1 ? 'h2' : hm[1].length === 2 ? 'h3' : 'h4', {}, inline(hm[2]))); continue; }
          if (!p) { p = h('p', {}); nodes.push(p); } else p.append(h('br'));
          p.append(...inline(l));
        }
      }
    }
  });
  return nodes;
}

// A model id as people say it: "qwen/qwen3.6-35b-a3b" -> "Qwen 3.6 35B A3B". The id stays in the tooltip.
export function modelName(id) {
  if (!id) return '';
  const base = String(id).split('/').pop().replace(/\.gguf$/i, '');
  const UPPER = new Set(['gpt', 'oss', 'it', 'moe', 'vl', 'gguf', 'mlx']);
  return base.split(/[- ]+/).filter(Boolean).map((t, i) => {
    if (i === 0) { const m = t.match(/^([a-z]+)(\d[\d.]*)$/i); if (m) return `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}`; }
    if (UPPER.has(t.toLowerCase())) return t.toUpperCase();
    if (/^[a-z]?\d+(\.\d+)?[bkm]$/i.test(t) || /^q\d/i.test(t)) return t.toUpperCase();
    return t[0].toUpperCase() + t.slice(1);
  }).join(' ');
}

// A password box with a show/hide button beside it.
export function revealable(input) {
  const btn = h('button', { type: 'button', class: 'reveal-btn', 'aria-label': 'Show password', title: 'Show password',
    onclick: () => {
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password'); btn.title = btn.getAttribute('aria-label');
      btn.replaceChildren(icon(show ? ICONS.eyeOff : ICONS.eye));
    } }, icon(ICONS.eye));
  return h('div', { class: 'reveal' }, input, btn);
}
