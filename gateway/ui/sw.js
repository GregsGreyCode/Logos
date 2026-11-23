// Logos service worker (F37): shows phone notifications pushed by the dashboard (gateway/push.py)
// and opens the chat when one is tapped.
// It also keeps a copy of the app itself (the page, its code and pictures; never any data from /api or
// Matrix), so that with the server out of reach (Tailscale off, Greg 23 Nov) the app still opens and
// says so, instead of the browser's own error page. The network always comes first: the copy is used
// only when the server does not answer, so the app never runs old code while the server is there.
const SHELL = 'logos-shell-1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k.startsWith('logos-shell-') && k !== SHELL) await caches.delete(k);
  await self.clients.claim();
})()));

const isShell = (url) => url.origin === self.location.origin
  && (url.pathname === '/' || url.pathname.startsWith('/ui/') || url.pathname.startsWith('/static/'));

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (!isShell(url)) return;
  const key = url.pathname === '/' ? '/' : req;          // the page is one copy, whatever its #
  e.respondWith((async () => {
    const net = fetch(req).then(async (r) => {
      if (r.ok && !r.redirected && r.type === 'basic') {
        const c = await caches.open(SHELL);
        await c.put(key, r.clone());
      }
      return r;
    });
    // The page itself: if the server is slow to answer at all (no route to it), use the copy after 4 s.
    const wait = req.mode === 'navigate' ? 4000 : 0;
    try {
      if (!wait) return await net;
      return await Promise.race([net, new Promise((_, no) => setTimeout(() => no(new Error('slow')), wait))]);
    } catch (err) {
      const kept = await caches.match(key);
      if (kept) { e.waitUntil(net.catch(() => {})); return kept; }
      return net;
    }
  })());
});

self.addEventListener('push', (e) => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (err) { m = {}; }
  const title = m.title || 'Logos';
  // An open page is told too (push.js): the chat should be getting that message, and if it is not, it starts over.
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    for (const w of wins) if (w.visibilityState === 'visible') w.postMessage({ type: 'logos-push', room: m.room || '' });
  }).catch(() => {}));
  // One notification per chat: a new message replaces the last one from the same chat (and buzzes).
  e.waitUntil(self.registration.showNotification(title, {
    body: m.body || 'New message',
    tag: m.room || 'logos',
    renotify: true,
    icon: '/static/apple-touch-icon.png',
    // The small status-bar icon: Android draws it from the alpha of a one-colour image; without it Chrome
    // shows a plain bell (Greg, 22 Nov).
    badge: '/static/badge-96.png',
    data: { link: m.link || '#/home', room: m.room || '' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const link = (e.notification.data && e.notification.data.link) || '#/home';
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const open = wins.find((w) => new URL(w.url).origin === self.location.origin);
    if (open) {
      await open.focus();
      open.postMessage({ type: 'logos-open', link });
      return;
    }
    await self.clients.openWindow(`/${link}`);
  })());
});
