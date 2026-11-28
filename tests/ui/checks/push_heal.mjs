// Phone alerts that Chrome swapped out sign up again by themselves (23 Nov: both phones had been
// dropped as "gone" and nothing told Logos about the new subscription). Headless Chrome cannot reach
// a real push service, so the subscription and Logos's push answers are stood in for.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });

async function run({ devices, endpoint, savedKey = 'old-key', savedEp = 'https://push.example/old', permission = 'granted' }) {
  const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  const calls = [];
  await ctx.addInitScript(({ savedKey, savedEp, endpoint, permission }) => {
    try {
      if (savedKey) localStorage.setItem('logos.push.pushkey', savedKey);
      if (savedEp) localStorage.setItem('logos.push.endpoint', savedEp);
    } catch (e) { /* fine */ }
    Object.defineProperty(Notification, 'permission', { get: () => permission });
    const sub = { endpoint, toJSON: () => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } }) };
    PushManager.prototype.getSubscription = async () => sub;
    PushManager.prototype.subscribe = async () => sub;
  }, { savedKey, savedEp, endpoint, permission });
  const page = await ctx.newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.route('**/api/v2/push', (r) => r.fulfill({ json: { public_key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U',
    app_id: 'logos.web', gateway_url: 'http://172.17.0.1:8091/_matrix/push/v1/notify', devices } }));
  await page.route('**/api/v2/push/subscribe', (r) => { calls.push(['subscribe', r.request().postDataJSON().subscription.endpoint]); return r.fulfill({ json: { pushkey: 'new-key' } }); });
  await page.route('**/api/v2/push/unsubscribe', (r) => { calls.push(['unsubscribe', r.request().postDataJSON().pushkey]); return r.fulfill({ json: { removed: true } }); });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(4500);
  const out = await page.evaluate(() => ({ key: localStorage.getItem('logos.push.pushkey'), ep: localStorage.getItem('logos.push.endpoint'),
    pushers: (window.__mx && window.__mx.pushers) || [] }));
  await ctx.close();
  return { calls, errs, ...out };
}

// Logos forgot this phone (it was dropped as gone): it signs up again and the old pusher goes.
let r = await run({ devices: [], endpoint: 'https://push.example/new' });
ok(r.calls.some(([k, e]) => k === 'subscribe' && e === 'https://push.example/new'), 'a forgotten phone signs up again');
ok(r.key === 'new-key' && r.ep === 'https://push.example/new', 'and keeps its new pushkey and subscription');
ok(r.pushers.some((p) => p.pushkey === 'old-key' && p.kind === null) && r.pushers.some((p) => p.pushkey === 'new-key' && p.kind === 'http'),
  'the Matrix account swaps the old pusher for the new one');
ok(r.errs.length === 0, `no page errors ${r.errs.join('|')}`);

// Chrome swapped the subscription while Logos still has the old one: renewed, the old one removed.
r = await run({ devices: [{ pushkey: 'old-key', device: 'Logos on Pixel' }], endpoint: 'https://push.example/swapped' });
ok(r.calls.some(([k]) => k === 'subscribe') && r.calls.some(([k, key]) => k === 'unsubscribe' && key === 'old-key'), 'a swapped subscription is renewed and the old one removed');

// All as it should be: nothing is done.
r = await run({ devices: [{ pushkey: 'old-key', device: 'Logos on Pixel' }], endpoint: 'https://push.example/old' });
ok(r.calls.length === 0 && r.key === 'old-key', 'a phone Logos still knows is left alone');

// Alerts turned off on this phone, or permission taken away: left alone.
r = await run({ devices: [], endpoint: 'https://push.example/new', savedKey: '' , savedEp: '' });
ok(r.calls.length === 0, 'a phone that never turned alerts on is not signed up');
r = await run({ devices: [], endpoint: 'https://push.example/new', permission: 'denied' });
ok(r.calls.length === 0, 'nor one whose permission was taken away');
await browser.close();
