// Edit Home's place (Greg, 28 Nov: "the edit pen should maybe be in a better position"): a pencil and the word Edit at the
// right of Home's title band, as the other screens keep their actions there; on a phone a slim row at the top of Home (the
// date is in the top bar; Greg, 23 Nov: not in the top bar itself). While arranging, Done in the same place, with Tidy up
// and Reset layout beside it on a desktop; on a phone the bar at the bottom is one big Done as well.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, mob, name] of [[390, 844, true, 'phone'], [412, 915, true, 'phone412'], [900, 1000, false, 'tablet'], [1440, 900, false, 'desk'], [1920, 1080, false, 'wide']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob, deviceScaleFactor: 2 });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.route('**/api/v2/me', async (route) => { const r = await route.fetch(); const d = await r.json(); d.prefs.tips_seen = ['home', 'agents', 'chat', 'notifications', 'routines']; await route.fulfill({ json: d }); });
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2200);
  const btn = page.locator('.home-edit-link');
  ok(await btn.count() === 1 && await page.locator('.topbar .home-edit-link').count() === 0, `${name}: one Edit, not in the top bar`);
  ok(/Edit/.test(await btn.innerText()) && await btn.locator('.icon').count() === 1, `${name}: a pencil and the word Edit`);
  const b = await btn.boundingBox();
  const firstWidget = await page.$$eval('.home-widgets > .home-widget', (els) => Math.min(...els.filter((e) => e.offsetParent).map((e) => e.getBoundingClientRect().top)));
  const right = await page.$$eval('.home-widgets > .home-widget', (els) => Math.max(...els.filter((e) => e.offsetParent).map((e) => e.getBoundingClientRect().right)));
  ok(b && b.y >= 0 && b.y + b.height <= firstWidget + 2 && Math.abs(b.x + b.width - right) <= 4, `${name}: at the top right of Home, above the widgets, lined up with their right edge (${Math.round(b.x + b.width)} / ${Math.round(right)})`);
  if (!mob) ok(b.y < 60, `${name}: in the title band`);
  else {
    const date = await page.locator('.home-head .home-date').boundingBox();
    ok(!date || date.width <= 2, `${name}: the date is not shown twice (it is in the top bar)`);
    ok(b.y < 150 && b.height >= 32, `${name}: in view without scrolling, big enough to tap (${Math.round(b.height)} px)`);
  }
  await page.screenshot({ path: `${process.argv[2]}/pencil-${name}.png` });
  await btn.click(); await page.waitForTimeout(500);
  ok((await btn.innerText()) === 'Done' && await page.locator('.home-widgets.editing').count() === 1, `${name}: it starts arranging and says Done`);
  const d = await btn.boundingBox();
  ok(Math.abs(d.x + d.width - (b.x + b.width)) <= 2 && Math.abs(d.y + d.height / 2 - (b.y + b.height / 2)) <= 2, `${name}: Done is in the same place as Edit`);
  const tidy = page.locator('.home-tidy'), reset = page.locator('.home-reset');
  if (w >= 1100) {
    const t = await tidy.boundingBox(), r = await reset.boundingBox();
    ok(await tidy.isVisible() && await reset.isVisible() && t.x < d.x && r.x < d.x && Math.abs(t.y + t.height / 2 - (d.y + d.height / 2)) < 6,
      `${name}: Tidy up and Reset layout beside Done, in the band`);
    ok(await page.locator('.home-tray .home-tidy').count() === 0, `${name}: and no longer in the Add tray`);
  } else ok(!(await tidy.isVisible()) && !(await reset.isVisible()), `${name}: no Tidy up or Reset layout without the desktop grid`);
  if (mob) {
    // The bar at the bottom of the screen is one big Done too, always in view (Greg, 23 Nov)
    const done = page.locator('.home-ask .ask-done');
    const bd = await done.boundingBox();
    ok(await done.isVisible() && bd.y + bd.height <= hgt && bd.y > hgt * 0.6 && bd.width > w - 60, `${name}: the bar at the bottom is one big Done (${Math.round(bd.y)} of ${hgt})`);
    ok(!(await page.locator('.home-ask textarea').isVisible()), `${name}: while arranging, the ask box is out of the way`);
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await page.waitForTimeout(300);
    ok(await done.isVisible() && (await done.boundingBox()).y + bd.height <= hgt, `${name}: and still in view at the bottom of the page`);
    await page.screenshot({ path: `${process.argv[2]}/pencil-${name}-done-bottom.png` });
    await done.click(); await page.waitForTimeout(300);
    ok(await page.locator('.home-widgets.editing').count() === 0 && await page.locator('.home-ask textarea').isVisible(), `${name}: the bottom Done ends it, and the ask box is back`);
    await page.evaluate(() => window.scrollTo(0, 0)); await btn.click(); await page.waitForTimeout(300);
  } else {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); await page.waitForTimeout(300);
    const s = await btn.boundingBox();
    ok(s && s.y >= 0 && s.y + s.height <= hgt, `${name}: Done stays in view as you scroll (${Math.round(s.y)})`);
    await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(200);
  }
  await page.screenshot({ path: `${process.argv[2]}/pencil-${name}-done.png` });
  await btn.click(); await page.waitForTimeout(300);
  ok(await page.locator('.home-widgets.editing').count() === 0 && /Edit/.test(await btn.innerText()), `${name}: Done ends it, and Edit is back`);
  ok(errs.length === 0, `${name}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
