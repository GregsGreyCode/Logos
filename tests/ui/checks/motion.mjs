import { createRequire } from 'module';
import fs from 'fs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const out = process.argv[2];
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push((e.stack || e.message).split('\n').slice(0, 2).join(' < ')));
fs.writeFileSync(out + '/slow', '1');
await page.goto('http://127.0.0.1:8779/#/home');
await page.waitForTimeout(1500);   // app.js waits for /api/v2/me (slow) then Home starts loading
await page.waitForTimeout(500);
ok(await page.locator('.loading .sk').count() > 4, `placeholders while Home loads (${await page.locator('.loading .sk').count()})`);
ok(await page.locator('.top-progress.on').count() === 1, 'the progress line runs');
await page.screenshot({ path: out + '/m-skeleton.png' });
await page.waitForTimeout(3500);
ok(await page.locator('.home-widgets .loading').count() <= 1, 'placeholders replaced by content');
ok(await page.locator('.top-progress.on:not(.done)').count() === 0, 'the progress line finished');
fs.unlinkSync(out + '/slow');
const glowX = () => page.evaluate(() => { const g = document.querySelector('.tab-glow'), a = document.querySelector('.tabbar a[aria-current="page"]'); return [Math.round(new DOMMatrix(getComputedStyle(g).transform).m41), a.offsetLeft]; });
await page.waitForTimeout(500);
const [gx, ax] = await glowX(); ok(Math.abs(gx - ax) < 2, `tab highlight under Home (${gx} vs ${ax})`);
const cdp = await ctx.newCDPSession(page);
async function swipe(dx, steps, stepMs) {
  const y = 400, x0 = 300;
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: x0, y }] });
  for (let i = 1; i <= steps; i++) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x0 + dx * i / steps, y }] }); await page.waitForTimeout(stepMs); }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(700);
  return page.evaluate(() => location.hash);
}
ok(await swipe(-60, 12, 40) === '#/home', 'a short slow drag springs back');
ok(await page.evaluate(() => document.querySelector('#view').style.transform) === '', 'and the screen is back in place');
ok(await swipe(-170, 8, 16) === '#/chat', 'a long drag goes to Agents');
await page.waitForTimeout(600);
const [gx2, ax2] = await glowX(); ok(Math.abs(gx2 - ax2) < 2, 'the highlight moved to Agents');
ok(await swipe(-70, 4, 8) === '#/noticeboard', 'a quick flick goes to Notifications');
ok(await swipe(-200, 8, 16) === '#/noticeboard', 'nothing past the last tab (it stretches and comes back)');
ok(await swipe(220, 8, 16) === '#/chat', 'swipe right goes back');
ok(errs.length === 0, `no page errors ${errs.join(' | ')}`);
await browser.close();
