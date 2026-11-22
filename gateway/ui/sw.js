// Logos service worker (F37): shows phone notifications pushed by the dashboard (gateway/push.py)
// and opens the chat when one is tapped. It caches nothing: the app always loads fresh.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (e) => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (err) { m = {}; }
  const title = m.title || 'Logos';
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
