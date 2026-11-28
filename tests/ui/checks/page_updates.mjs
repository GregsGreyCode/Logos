// A page left open runs old code (23 Nov: a phone still had the chat from before the fixes). It asks which Logos
// the server runs when it comes back to the screen, and starts over if that is not the one it loaded.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });

async function run({ typed = '', newer = true }) {
  const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  let commit = 'abc1234', loads = 0;
  page.on('load', () => { loads += 1; });
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.version = { commit, date: 1795392000 }; await r.fulfill({ json: me }); });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2000);
  if (typed) await page.evaluate((t) => { const x = document.createElement('textarea'); x.value = t; document.body.append(x); }, typed);
  const before = loads;
  if (newer) commit = 'def5678';
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForTimeout(2000);
  const out = { reloaded: loads > before, errs };
  await page.close();
  return out;
}
let r = await run({});
ok(r.reloaded, 'a newer Logos on the server: the page starts over when it comes back');
r = await run({ newer: false });
ok(!r.reloaded, 'the same Logos: nothing happens');
r = await run({ typed: 'half a message' });
ok(!r.reloaded, 'a newer Logos but something is being typed: it waits');
ok(r.errs.length === 0, `no page errors ${r.errs.join('|')}`);
await browser.close();
