// Small helpers shared by every screen.
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

// fetch with the session cookie; refreshes an expired session once, else goes to the login page.
export async function api(path, opts = {}, retried = false) {
  const headers = { ...(opts.headers || {}) };
  // opts.body is sent as JSON; opts.raw (a Blob, with its own Content-Type header) is sent as it is.
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = csrf();
  const r = await fetch(path, { ...opts, headers, credentials: 'same-origin',
    body: opts.raw !== undefined ? opts.raw : (opts.body !== undefined ? JSON.stringify(opts.body) : undefined) });
  if (r.status === 401) {
    if (!retried) {
      const again = await fetch('/auth/refresh', { method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': csrf() } });
      if (again.ok) return api(path, opts, true);
    }
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

export function when(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (days === 0) return `Today ${time}`;
  if (days === 1) return `Yesterday ${time}`;
  if (days < 7) return d.toLocaleDateString([], { weekday: 'long' });
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' });
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
  const parts = text.split(/```[^\n]*\n?/);
  parts.forEach((part, i) => {
    if (i % 2) { nodes.push(h('pre', {}, h('code', {}, part.replace(/\n$/, '')))); return; }
    for (const block of part.split(/\n{2,}/)) {
      const lines = block.split('\n').filter((l) => l.trim());
      if (!lines.length) continue;
      if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
        nodes.push(h('ul', {}, lines.map((l) => h('li', {}, inline(l.replace(/^\s*[-*]\s+/, ''))))));
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
