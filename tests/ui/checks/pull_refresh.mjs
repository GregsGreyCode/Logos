// Pull down to refresh (Greg, 23 Nov): out from under the top bar, only the screen's data, not in a chat.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
const page = await ctx.newPage();
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
const cdp = await ctx.newCDPSession(page);
const touch = (type, x, y) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' ? [] : [{ x, y }] });
async function drag(dy, { hold = false, x = 206, y0 = 320 } = {}) {
  await touch('touchStart', x, y0);
  for (let i = 1; i <= 10; i++) { await touch('touchMove', x, y0 + (dy * i) / 10); await page.waitForTimeout(16); }
  if (hold) return;
  await touch('touchEnd');
}
const count = { home: 0, feed: 0 };
page.on('request', (r) => { const u = new URL(r.url()).pathname; if (u === '/api/v2/home') count.home += 1; if (u === '/api/v2/notifications') count.feed += 1; });

await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2500);
await page.evaluate(() => { window.__same = 'yes'; });
ok(await page.locator('.feed-refresh').count() === 0, 'no Refresh button');
ok(await page.evaluate(() => getComputedStyle(document.documentElement).overscrollBehaviorY === 'contain'), "Chrome's own pull-to-refresh is off");

// 1. a long pull: the spinner comes out from under the top bar, then Home asks again
let before = count.home;
await drag(240, { hold: true });
const bar = await page.locator('.topbar').boundingBox();
const box = await page.locator('.ptr').boundingBox();
ok(Math.abs(box.y - (bar.y + bar.height)) <= 1, `the spinner starts just under the top bar (${Math.round(box.y)} vs ${Math.round(bar.y + bar.height)})`);
ok(await page.locator('.ptr.on').count() === 1, 'and shows while pulling');
const dot = await page.locator('.ptr-dot').boundingBox();
ok(dot.y >= box.y - 1 && dot.y + dot.height > box.y + 10, 'moved out into view');
ok(/translateY\(\d/.test(await page.evaluate(() => document.getElementById('view').style.transform)), 'the screen moves down with the finger, leaving room for it');
await page.screenshot({ path: `${OUT}/pull-down.png`, clip: { x: 0, y: 0, width: 412, height: 260 } });
await touch('touchEnd'); await page.waitForTimeout(250);
ok(await page.locator('.ptr.spinning').count() === 1, 'let go past the line: it spins');
await page.waitForTimeout(1500);
ok(count.home > before, `Home asked for fresh data (${count.home - before} request)`);
ok(await page.locator('.ptr.on').count() === 0, 'and the spinner tucks away');
ok(await page.evaluate(() => window.__same) === 'yes', 'the app did not reload');
ok(await page.evaluate(() => document.getElementById('view').style.transform) === '', 'and the screen is back in place');

// 2. a short pull does nothing
before = count.home;
await drag(70); await page.waitForTimeout(1200);
ok(count.home === before && await page.locator('.ptr.on').count() === 0, 'a short pull springs back and refreshes nothing');

// 3. not at the top of the page
await page.evaluate(() => { document.body.style.minHeight = '3000px'; window.scrollTo(0, 400); });
await page.waitForTimeout(300);
before = count.home;
await drag(240, { hold: true });
ok(await page.locator('.ptr.on').count() === 0, 'scrolled down the page: no pull');
await touch('touchEnd'); await page.evaluate(() => { document.body.style.minHeight = ''; window.scrollTo(0, 0); });

// 4. Notifications asks again
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/noticeboard`); await page.waitForTimeout(2500);
before = count.feed;
await drag(240); await page.waitForTimeout(1500);
ok(count.feed > before, `Notifications asked again (${count.feed - before})`);

// 5. not inside a chat
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2500);
await drag(240, { hold: true, y0: 400 });
ok(await page.locator('.ptr.on').count() === 0, 'in a chat, pulling down does not start a refresh');
await touch('touchEnd');
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
