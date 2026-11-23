import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto('http://127.0.0.1:8779/#/advisor'); await page.waitForTimeout(2000);
  ok(await page.locator('.card .actions select').evaluate((e) => getComputedStyle(e).appearance === 'none'), `${n}: the model picker is styled like the app's`);
  const log = await page.$$eval('.advisor-log > details', (els) => els.length > 1 ? els[1].getBoundingClientRect().top - els[0].getBoundingClientRect().bottom : 0);
  ok(log > 0 && log <= 12, `${n}: the log's entries sit close together (${Math.round(log)}px)`);
  const row = await page.locator('.budget-row', { hasText: 'Wedding' }).evaluate((e) => { const who = e.querySelector('.budget-who').getBoundingClientRect(); return who.width / e.getBoundingClientRect().width; });
  ok(row > (mob ? 0.8 : 0.4), `${n}: a budget's words keep the width (${Math.round(row * 100)}%)`);
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
