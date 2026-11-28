// The server out of reach (Greg, 23 Nov: Tailscale off on his phone, and the app failed quietly).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const page = await ctx.newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(1500);
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload(); await page.waitForTimeout(1500);              // now under the worker: the app is kept
  ok(await page.locator('.reach-bar').count() === 0, `${n}: no line while the server answers`);

  // Open, then out of reach: a request fails and the line comes up.
  await ctx.setOffline(true);
  await page.evaluate(() => import('/ui/util.js').then((u) => u.api('/api/v2/activity')).catch(() => {}));
  await page.waitForTimeout(1000);
  ok(await page.locator('.reach-bar').count() === 0, `${n}: not at once (a restart of a few seconds does not flash it)`);
  await page.waitForSelector('.reach-bar', { timeout: 5000 }).catch(() => {});
  ok(/Can.t reach Logos/.test(await page.locator('.reach-bar').textContent().catch(() => '')), `${n}: says it cannot reach Logos`);
  const box = await page.locator('.reach-bar').boundingBox();
  ok(box && box.x >= 15 && box.x + box.width <= w - 15, `${n}: fits the screen`);
  await page.screenshot({ path: `${OUT}/out-of-reach-${n}.png` });
  await page.click('.reach-again'); await page.waitForTimeout(500);
  ok(await page.locator('.reach-bar').count() === 1, `${n}: Try again while still out of reach keeps it`);

  // Back: the phone says it is online again, and the line goes by itself.
  await ctx.setOffline(false);
  await page.waitForTimeout(800);
  ok(await page.locator('.reach-bar').count() === 0, `${n}: gone once the server answers`);

  // Opening the app while out of reach: the kept copy opens and says why.
  await ctx.setOffline(true);
  await page.reload().catch(() => {}); await page.waitForTimeout(500);
  await page.waitForSelector('.reach-bar', { timeout: 8000 }).catch(() => {});
  ok(await page.locator('.topbar, .nav-links').count() > 0, `${n}: the app still opens`);
  ok(await page.locator('.reach-bar').count() === 1, `${n}: and says it cannot reach Logos`);
  await ctx.setOffline(false);
  await page.waitForTimeout(3000);
  ok(await page.locator('.reach-bar').count() === 0 && await page.locator('.home-date, main .view-box *').count() > 0, `${n}: and starts properly once back`);
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await ctx.close();
}
await browser.close();
