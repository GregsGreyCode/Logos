// A dead connection under the chat's long poll is cut, not waited on for ~110 s (Béré, 23 Nov: moving between
// Wi-Fi and mobile data, her message sat on "sending…"). The real fetchFn from mx.js, with a fetch that never answers.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 412, height: 915 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.clock.install();
await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(500);
await page.evaluate(async () => {
  const mx = await import('/ui/mx.js?real');
  window.__mx2 = mx;
  window.__calls = [];
  // a fetch that never answers on its own, but gives up when told to
  window.fetch = (url, opts = {}) => new Promise((resolve, reject) => {
    window.__calls.push(String(url));
    if (opts.signal) opts.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
  });
  window.__settled = {};
  window.__go = (name, url) => { window.__settled[name] = 'waiting'; mx.fetchFn(url, {}).then(() => { window.__settled[name] = 'answered'; }, (e) => { window.__settled[name] = e.name; }); };
});
const state = (n) => page.evaluate((k) => window.__settled[k], n);

await page.evaluate(() => window.__go('poll', '/_matrix/client/v3/sync?filter=f&timeout=30000&since=41999'));
await page.clock.runFor(45000);
ok(await state('poll') === 'waiting', 'a long poll is given its 30 s and some grace');
await page.clock.runFor(6000);
ok(await state('poll') === 'AbortError', 'then it is cut (20 s after it should have answered), so the library reconnects');

await page.evaluate(() => window.__go('poll2', '/_matrix/client/v3/sync?filter=f&timeout=30000&since=42010'));
await page.clock.runFor(5000);
await page.evaluate(() => window.__mx2.dropSyncs());
ok(await state('poll2') === 'AbortError', 'dropSyncs (a network change, or an alert the chat does not show) cuts it at once');

await page.evaluate(() => window.__go('first', '/_matrix/client/v3/sync?filter=f&timeout=0'));
await page.clock.runFor(120000);
ok(await state('first') === 'waiting', 'the very first sync on a new device (no "since") is never cut: it can be large');
await page.evaluate(() => window.__go('send', '/_matrix/client/v3/rooms/!r/send/m.room.message/1'));
await page.clock.runFor(120000);
await page.evaluate(() => window.__mx2.dropSyncs());
ok(await state('send') === 'waiting', 'nothing but the long poll is ever cut (a send, a photo upload)');
await page.evaluate(() => window.__go('poll3', '/_matrix/client/v3/sync?filter=f&timeout=30000&since=42020'));
await page.evaluate(() => window.dispatchEvent(new Event('online')));
ok(await state('poll3') === 'AbortError', 'the phone coming back online cuts a poll that may be on the old connection');
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
