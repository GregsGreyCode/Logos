// Notifications: Refresh by hand, beside Mark all read (Greg, 23 Nov).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  let asked = 0;
  await page.route('**/api/v2/notifications', async (r) => { asked += 1; await r.continue(); });
  await page.goto('http://127.0.0.1:8779/#/noticeboard'); await page.waitForTimeout(2500);
  const btn = page.locator('.feed-refresh');
  ok(await btn.isVisible() && await page.locator('.feed-allread').count() === 1, `${n}: Refresh is there, beside Mark all read`);
  ok((await btn.getAttribute('aria-label')) === 'Refresh', `${n}: and named for a screen reader`);
  const before = asked;
  await (mob ? btn.tap() : btn.click());
  ok(await btn.evaluate((e) => e.classList.contains('spinning')), `${n}: it spins while it asks`);
  await page.waitForTimeout(1200);
  ok(asked > before && !(await btn.evaluate((e) => e.classList.contains('spinning'))), `${n}: it asked again, and stopped`);
  const box = await btn.boundingBox();
  ok(box && box.x >= 0 && box.x + box.width <= w && box.width >= 32, `${n}: on the screen, big enough to tap (${Math.round(box.width)}px)`);
  await page.screenshot({ path: `${OUT}/notif-refresh-${n}.png`, clip: { x: 0, y: 0, width: w, height: 160 } });
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
