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
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = csrf();
  const r = await fetch(path, { ...opts, headers, credentials: 'same-origin',
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
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

