// A Matrix sync that stopped by itself is noticed (23 Nov: after a dashboard restart Greg's phone
// caught up once and never synced again). The real mx.js watchdog, with a stand-in client and clock.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });

async function setup(typed = '') {
  const page = await browser.newPage({ viewport: { width: 412, height: 915 } });
  await page.clock.install();
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(500);
  await page.evaluate(async (typed) => {
    // '?real': past the import map, which hands the screens the fake Matrix client.
    const mx = await import('/ui/mx.js?real');
    window.__w = { retries: 0, reloads: 0, handlers: [] };
    const c = { on: (ev, f) => window.__w.handlers.push(f), retryImmediately: () => { window.__w.retries += 1; } };
    window.__sync = (state) => window.__w.handlers.forEach((f) => f(state));
    if (typed) document.body.append(Object.assign(document.createElement('textarea'), { value: typed }));
    mx.watch(c, { ClientEvent: { Sync: 'sync' } });
  }, typed);
  // a reload, stood in for: the page must not actually go away mid-check
  await page.evaluate(() => { window.addEventListener('beforeunload', () => { window.__w.reloads += 1; }); });
  return page;
}
const w = (page) => page.evaluate(() => window.__w);

let page = await setup();
for (let i = 0; i < 6; i++) { await page.clock.runFor(15000); await page.evaluate(() => window.__sync('SYNCING')); }
ok((await w(page)).retries === 0, 'a sync that keeps finishing is left alone');
await page.clock.runFor(76000 + 15000);
ok((await w(page)).retries === 1, 'stalled for 75 s: the library is asked to retry now');
await page.evaluate(() => window.__sync('SYNCING'));
await page.clock.runFor(60000);
ok((await w(page)).retries === 1, 'and once it syncs again nothing more is done');
await page.close();

page = await setup();
let loads = 0; page.on('load', () => { loads += 1; });
await page.clock.runFor(75000 * 2 + 45000);
await page.waitForTimeout(1500);
ok(loads >= 1, 'still stuck after the retry: the page reloads, and syncs from scratch');
await page.close();

page = await setup('half a message');
await page.clock.runFor(75000 * 2 + 45000);
ok((await w(page)).retries === 1 && page.url().includes('#/home'), 'still stuck while typing: no reload that would lose it');
await page.close();
await browser.close();
