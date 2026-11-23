import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const out = process.argv[2];
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
for (const [label, vp, mobile] of [['phone', { width: 412, height: 915 }, true], ['desktop', { width: 1300, height: 900 }, false]]) {
  const ctx = await browser.newContext({ viewport: vp, isMobile: mobile, hasTouch: mobile, deviceScaleFactor: 2 });
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push((e.stack || e.message).split('\n').slice(0, 2).join(' < ')));
  const puts = []; page.on('request', (r) => { if (r.method() === 'PUT') puts.push(r.postData()); });
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2200);
  const order = () => page.$$eval('.home-widgets > .home-widget:not(.off)', (els) => els.map((e) => e.dataset.widget));
  console.log(label, 'start order:', (await order()).join(','));
  if (mobile) {           // long press on a widget starts Edit
    const b = await page.locator('.home-widget[data-widget="calendar"]').boundingBox();
    const cdp = await ctx.newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: b.x + 40, y: b.y + 40 }] });
    await page.waitForTimeout(700);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(200);
    ok(await page.locator('.home-widgets.editing').count() === 1, `${label}: long press starts Edit`);
  } else {
    await page.locator('.home-edit-link').click();
    ok(await page.locator('.home-widgets.editing').count() === 1, `${label}: Edit Home starts Edit`);
  }
  await page.screenshot({ path: `${out}/e-${label}-edit.png` });
  // drag Lights onto Weather (the first)
  const hnd = page.locator('.home-widget[data-widget="lights"] .widget-handle');
  await hnd.scrollIntoViewIfNeeded();
  const hb = await hnd.boundingBox();
  await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2); await page.mouse.down();
  for (let i = 0; i < 40; i++) {
    const tb = await page.locator('.home-widget[data-widget="weather"]').boundingBox();
    await page.mouse.move(tb.x + tb.width / 2, tb.y + tb.height / 2, { steps: 3 });
    if ((await order())[0] === 'lights') break;
    await page.waitForTimeout(30);
  }
  await page.mouse.up(); await page.waitForTimeout(300);
  ok((await order())[0] === 'lights', `${label}: Lights dragged to the top (${(await order()).join(',')})`);
  ok(puts.some((p) => p.includes('"widgets":["lights"')), `${label}: new order saved`);
  // remove Coming up, then add it back from the tray
  await page.locator('.home-widget[data-widget="coming"] .widget-remove').click(); await page.waitForTimeout(300);
  ok(!(await order()).includes('coming') && await page.locator('.home-add', { hasText: 'Coming up' }).count() === 1, `${label}: ✕ takes it off, Add offers it`);
  await page.locator('.home-add', { hasText: 'Coming up' }).click(); await page.waitForTimeout(400);
  ok((await order()).at(-1) === 'coming', `${label}: Add puts it back at the end`);
  // Done: the bar at the bottom on a phone, the button in the title band on a wide screen
  const bottom = page.locator('.home-ask .ask-done');
  if (await bottom.isVisible()) await bottom.click(); else await page.locator('.home-edit-link').click();
  await page.waitForTimeout(200);
  ok(await page.locator('.home-widgets.editing').count() === 0 && await page.locator('.widget-bar').count() === 0, `${label}: Done leaves Edit`);
  await page.screenshot({ path: `${out}/e-${label}-done.png` });
  ok(errs.length === 0, `${label}: no page errors ${errs.join(' | ')}`);
  await ctx.close();
}
await browser.close();
