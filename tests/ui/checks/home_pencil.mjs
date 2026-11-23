import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, mob, name] of [[412, 915, true, 'phone'], [1440, 900, false, 'desk']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob, deviceScaleFactor: 2 });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2200);
  const btn = page.locator('.home-edit-link');
  if (mob) {
    ok(await page.locator('.topbar .home-edit-link').count() === 0, 'phone: no pencil in the top bar');
    const lastWidget = await page.$$eval('.home-widgets > .home-widget:not([hidden])', (els) => Math.max(...els.map((e) => e.getBoundingClientRect().bottom + scrollY)));
    const b = await btn.evaluate((e) => e.getBoundingClientRect().top + scrollY);
    ok(b > lastWidget, 'phone: the pencil is at the end of Home');
    await btn.scrollIntoViewIfNeeded(); await page.screenshot({ path: `${process.argv[2]}/pencil-phone.png` });
  } else {
    const b = await btn.boundingBox();
    ok(b && b.x > w * 0.7 && b.y < 60, `desk: the pencil in the title band (${Math.round(b.x)},${Math.round(b.y)})`);
  }
  await btn.click(); await page.waitForTimeout(400);
  ok((await btn.innerText()) === 'Done' && await page.locator('.home-widgets.editing').count() === 1, `${name}: it starts arranging and says Done`);
  if (name === 'phone') {
    // Done is a block of its own under the Add chips, in the page: not floating over them (Greg, 23 Nov)
    await btn.scrollIntoViewIfNeeded(); await page.waitForTimeout(300);
    const d = await btn.boundingBox(), t = await page.locator('.home-tray').boundingBox();
    ok(await btn.evaluate((e) => getComputedStyle(e).position === 'static'), 'phone: Done is not floating');
    ok(d.y >= t.y + t.height, `phone: Done is below the Add chips (${Math.round(d.y)} under ${Math.round(t.y + t.height)})`);
    ok(d.width > w - 60, `phone: and spans the width (${Math.round(d.width)}px)`);
    ok(d.y >= 0 && d.y + d.height <= hgt, 'phone: and can be reached');
  } else {
    await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(300);
    const d = await btn.boundingBox();
    ok(d && d.y >= 0 && d.y + d.height <= hgt, `${name}: Done is in view (${Math.round(d.y)})`);
  }
  await page.screenshot({ path: `${process.argv[2]}/pencil-${name}-done.png` });
  await btn.click(); await page.waitForTimeout(300);
  ok(await page.locator('.home-widgets.editing').count() === 0, `${name}: Done ends it`);
  ok(errs.length === 0, `${name}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
