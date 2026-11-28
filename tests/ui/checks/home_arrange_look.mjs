import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, mob, name] of [[412, 915, true, 'phone'], [1440, 900, false, 'desk']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob, deviceScaleFactor: mob ? 2 : 1 });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2500);
  const before = await page.$$eval('.home-widget:not([hidden]):not(.off)', (els) => els.map((e) => Math.round(e.getBoundingClientRect().height)));
  await page.locator('.home-edit-link').click(); await page.waitForTimeout(600);
  ok(await page.locator('.home-widgets.editing .home-light').first().isVisible(), `${name}: the lights still show while editing`);
  ok(await page.locator('.home-widgets.editing .home-weather').first().isVisible(), `${name}: so does the weather`);
  await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(200);
  const hd = page.locator('.widget-handle').nth(1); const b = await hd.boundingBox();
  await page.mouse.move(b.x + 10, b.y + 10); await page.mouse.down(); await page.mouse.move(b.x + 30, b.y + 120, { steps: 6 }); await page.waitForTimeout(200);
  await page.screenshot({ path: `${process.argv[2]}/edit-${name}.png` });
  await page.mouse.up(); await page.close();
}
await browser.close();
