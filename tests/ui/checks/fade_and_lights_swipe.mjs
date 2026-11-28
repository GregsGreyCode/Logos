import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 });
const page = await ctx.newPage();
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2500);
// 1. Notifications never lay their days out side by side while fading in
await page.evaluate(() => { location.hash = '#/noticeboard'; });
let side = false;
for (let i = 0; i < 14; i++) { await page.waitForTimeout(60); side = side || await page.evaluate(() => { const s = [...document.querySelectorAll('.feed-days > section')].map((x) => x.getBoundingClientRect()); return s.length > 1 && Math.abs(s[0].left - s[1].left) > 5; }); }
ok(!side, 'the days stay one under the other while they fade in');
// 2. A slide that starts in the gap between light pills does not change tab
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2200);
await page.evaluate(() => { const s = document.querySelector('.home-lights-sec'); window.scrollBy(0, s.getBoundingClientRect().top - 120); }); await page.waitForTimeout(400);
const a = await page.locator('.home-light').nth(0).boundingBox(), b = await page.locator('.home-light').nth(1).boundingBox();
const x = (a.x + a.width + b.x) / 2, y = a.y + a.height / 2;
const cdp = await ctx.newCDPSession(page);
await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
for (let i = 1; i <= 10; i++) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x - 25 * i, y }] }); await page.waitForTimeout(16); }
await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await page.waitForTimeout(600);
ok(await page.evaluate(() => location.hash) === '#/home', 'a slide starting between the pills stays on Home');
// 3. A swipe elsewhere on Home still changes tab
await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(300);
await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: 300, y: 300 }] });
for (let i = 1; i <= 8; i++) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: 300 - 25 * i, y: 300 }] }); await page.waitForTimeout(16); }
await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await page.waitForTimeout(700);
ok(await page.evaluate(() => location.hash) === '#/chat', 'a swipe on the rest of Home still goes to Agents');
await browser.close();
