// While arranging Home the whole widget picks up, not only its handle (Greg, 28 Nov: "we should be able to grab it from
// almost anywhere in its boundary"). Desktop: dragged from the middle of a widget it moves; a plain click on it moves
// nothing; each control inside a widget (Compact/Full, Places, the places editor's box, ✕, the resize corner) does its own
// job and never starts a drag; a drag that ends over a control presses nothing; the hand cursor; the keyboard still moves
// it. Phone: a swipe over a widget still scrolls the page; a long press picks it up and it can be dragged; a press on a
// control does not pick it up.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const URL_HOME = `http://127.0.0.1:${process.env.PORT || 8779}/#/home`;
const TIPS = ['home', 'agents', 'chat', 'notifications', 'routines'];
const browser = await chromium.launch({ executablePath: process.env.CHROME });

// ── desktop ──
{
  let saved = null, order = null;
  const page = await browser.newPage({ viewport: { width: 1440, height: 1400 } });   // tall: every widget in view, no scrolling
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.route('**/api/v2/me/prefs', async (route) => { const b = JSON.parse(route.request().postData() || '{}'); if (b.widget_grid) saved = b.widget_grid; if (b.widgets) order = b.widgets; await route.fulfill({ json: { ok: true } }); });
  await page.route('**/api/v2/me', async (route) => { const r = await route.fetch(); const d = await r.json(); d.prefs.tips_seen = TIPS; await route.fulfill({ json: d }); });
  await page.goto(URL_HOME); await page.waitForTimeout(2500);
  await page.locator('.home-edit-link').click(); await page.waitForTimeout(600);
  const W = (id) => page.locator(`.home-widget[data-widget="${id}"]`);
  const place = (id) => W(id).evaluate((e) => `${e.style.gridColumn} | ${e.style.gridRow}`);
  const count = () => page.locator('.home-widgets > .home-widget:not(.off)').count();
  const moving = () => page.evaluate(() => !!document.querySelector('.home-widget.dragging, .home-widget.lifted') || document.documentElement.classList.contains('widget-moving'));

  // the look: a hand over the widget's body, a pointer over its controls
  const lb = await W('lists').boundingBox();
  await page.mouse.move(lb.x + lb.width / 2, lb.y + lb.height * 0.65); await page.waitForTimeout(150);
  ok(await page.evaluate(([x, y]) => getComputedStyle(document.elementFromPoint(x, y)).cursor, [lb.x + lb.width / 2, lb.y + lb.height * 0.65]) === 'grab', 'desktop: a hand (grab) over the middle of a widget');
  ok(await W('lists').evaluate((e) => getComputedStyle(e).outlineStyle) === 'solid', 'desktop: the widget under the mouse has a solid outline (dashed otherwise)');
  ok(await W('weather').evaluate((e) => getComputedStyle(e).outlineStyle) === 'dashed', 'desktop: the others dashed');
  ok(await W('lists').locator('.widget-size button').first().evaluate((e) => getComputedStyle(e).cursor) === 'pointer', 'desktop: a pointer over its controls');

  // a plain click in the middle moves nothing
  let before = await place('lists');
  await page.mouse.click(lb.x + lb.width / 2, lb.y + lb.height * 0.65); await page.waitForTimeout(300);
  ok(await place('lists') === before && !(await moving()), 'desktop: a plain click on a widget moves nothing');

  // dragged from the middle (not the handle) to the other column
  const toRight = lb.x < 700;
  await page.mouse.move(lb.x + lb.width / 2, lb.y + lb.height * 0.65); await page.mouse.down();
  await page.mouse.move(lb.x + lb.width / 2 + (toRight ? 30 : -30), lb.y + lb.height * 0.6, { steps: 3 });
  ok(await moving(), 'desktop: pressing in the middle of a widget and moving picks it up');
  ok(await page.evaluate(([x, y]) => getComputedStyle(document.elementFromPoint(x, y)).cursor, [toRight ? 1100 : 400, 300]) === 'grabbing', 'desktop: a closed hand while it is held');
  await page.mouse.move(lb.x + lb.width / 2 + (toRight ? 590 : -590), 200, { steps: 12 });   // held by its middle: its left edge lands one column-half over await page.waitForTimeout(200);
  ok(await page.locator('.guide-layer .grid-ghost').count() === 1, 'desktop: with its ghost outline where it will land');
  await page.mouse.up(); await page.waitForTimeout(600);
  const la = await W('lists').boundingBox();
  ok(Math.abs(la.x - lb.x) > 300 && saved && saved.lists && (toRight ? saved.lists.x >= 6 : saved.lists.x < 6), `desktop: dragged from its middle, it moves to the other column (x ${Math.round(lb.x)} to ${Math.round(la.x)}) and is kept`);
  ok(!(await moving()), 'desktop: and is let go');

  // each control does its own job and never starts a drag
  const pressMove = async (loc, what) => {
    const p0 = {}; for (const id of ['weather', 'lists', 'coming', 'calendar']) p0[id] = await place(id);
    const b = await loc.boundingBox();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2); await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2 + 60, b.y + b.height / 2 + 40, { steps: 5 }); await page.waitForTimeout(100);
    const held = await moving();
    await page.mouse.up(); await page.waitForTimeout(300);
    const same = await (async () => { for (const id of Object.keys(p0)) if (what !== 'resize' && await place(id) !== p0[id]) return false; return true; })();
    ok(!held && same, `desktop: pressing ${what} and moving does not pick the widget up`);
  };
  await pressMove(W('weather').locator('.widget-size button').first(), 'Compact/Full');
  await pressMove(W('weather').locator('.widget-remove'), '✕');
  await pressMove(W('weather').locator('.widget-bar button', { hasText: 'Places' }), 'Places');
  ok(await count() >= 6, 'desktop: …and pressing ✕ then moving off it took nothing off');
  // a click on a control does its job
  const sizeNow = await W('coming').evaluate((e) => e.classList.contains('size-compact'));
  before = await place('coming');
  await W('coming').locator('.widget-size button', { hasText: sizeNow ? 'Full' : 'Compact' }).click(); await page.waitForTimeout(400);
  ok(await W('coming').evaluate((e) => e.classList.contains('size-compact')) !== sizeNow && !(await moving()), 'desktop: a click on Compact/Full switches the size, no drag');
  await W('weather').locator('.widget-bar button', { hasText: 'Places' }).click(); await page.waitForTimeout(400);
  const box = W('weather').locator('.home-places-edit input').first();
  ok(await box.isVisible(), 'desktop: a click on Places opens its editor');
  await pressMove(box, 'the places search box');
  await box.click(); await page.keyboard.type('Lon');
  ok(await box.inputValue() === 'Lon', 'desktop: and the box takes typing');
  await W('weather').locator('.widget-bar button', { hasText: 'Places' }).click(); await page.waitForTimeout(300);
  // the resize corner resizes, it does not move
  const cb = await W('coming').boundingBox();
  const grip = page.locator('.grip-layer .widget-grip[data-widget="coming"]');
  const gb = await grip.boundingBox();
  await page.mouse.move(gb.x + gb.width / 2, gb.y + gb.height / 2); await page.mouse.down();
  await page.mouse.move(gb.x + 10, gb.y + 120, { steps: 6 }); await page.waitForTimeout(100);
  const heldByGrip = await page.evaluate(() => !!document.querySelector('.home-widget.dragging, .home-widget.lifted'));
  await page.mouse.up(); await page.waitForTimeout(400);
  const ca = await W('coming').boundingBox();
  ok(!heldByGrip && Math.abs(ca.x - cb.x) < 2 && Math.abs(ca.y - cb.y) < 2 && ca.height > cb.height + 40, `desktop: the resize corner resizes (height ${Math.round(cb.height)} to ${Math.round(ca.height)}) and does not move it`);

  // a drag that ends over another widget's ✕ takes nothing off
  const n0 = await count();
  const mb = await W('messages').boundingBox(), xb = await W('weather').locator('.widget-remove').boundingBox();
  await page.mouse.move(mb.x + mb.width / 2, mb.y + mb.height * 0.7); await page.mouse.down();
  await page.mouse.move(xb.x + xb.width / 2, xb.y + xb.height / 2, { steps: 12 }); await page.mouse.up(); await page.waitForTimeout(500);
  ok(await count() === n0 && await W('weather').isVisible(), 'desktop: a drag let go over another widget\'s ✕ presses nothing');

  // the keyboard still moves it, on its handle
  const k0 = saved.lists;
  await W('lists').locator('.widget-handle').focus();
  await page.keyboard.press(k0.x > 0 ? 'ArrowLeft' : 'ArrowRight'); await page.waitForTimeout(300);
  ok(saved.lists.x === k0.x + (k0.x > 0 ? -1 : 1), `desktop: arrows on the handle still move it (x ${k0.x} to ${saved.lists.x})`);
  const k1 = saved.lists;
  await page.keyboard.press(k1.x + k1.w < 12 ? 'Alt+ArrowRight' : 'Alt+ArrowLeft'); await page.waitForTimeout(300);
  ok(saved.lists.w !== k1.w, `desktop: Alt+arrows resize it (w ${k1.w} to ${saved.lists.w})`);
  await page.screenshot({ path: `${process.argv[2]}/drag-anywhere-desk.png` });
  ok(errs.length === 0, `desktop: no page errors ${errs.join('|')}`);
  await page.close();
}

// ── phone: a long press picks it up; a swipe scrolls ──
{
  let order = null;
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.route('**/api/v2/me/prefs', async (route) => { const b = JSON.parse(route.request().postData() || '{}'); if (b.widgets) order = b.widgets; await route.fulfill({ json: { ok: true } }); });
  await page.route('**/api/v2/me', async (route) => { const r = await route.fetch(); const d = await r.json(); d.prefs.tips_seen = TIPS; await route.fulfill({ json: d }); });
  await page.goto(URL_HOME); await page.waitForTimeout(2500);
  const cdp = await ctx.newCDPSession(page);
  const touch = (type, x, y) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' ? [] : [{ x, y }] });
  const ids = () => page.$$eval('.home-widgets > .home-widget:not(.off)', (els) => els.map((e) => e.dataset.widget));
  await page.locator('.home-edit-link').tap(); await page.waitForTimeout(600);
  const W = (id) => page.locator(`.home-widget[data-widget="${id}"]`);
  const lifted = () => page.evaluate(() => !!document.querySelector('.home-widget.dragging, .home-widget.lifted'));

  // a quick swipe up over a widget's middle scrolls the page and moves nothing
  const order0 = await ids();
  await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(200);
  let b = await W(order0[1]).boundingBox();
  let x = b.x + b.width / 2, y = Math.min(b.y + b.height / 2, 600);
  await touch('touchStart', x, y);
  for (let i = 1; i <= 8; i++) { await touch('touchMove', x, y - i * 30); await page.waitForTimeout(16); }
  await touch('touchEnd'); await page.waitForTimeout(500);
  ok(await page.evaluate(() => window.scrollY) > 60 && JSON.stringify(await ids()) === JSON.stringify(order0) && !(await lifted()), `phone: a swipe over a widget scrolls the page (to ${await page.evaluate(() => Math.round(window.scrollY))}) and moves nothing`);

  // a long press on a control does not pick the widget up
  await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(300);
  const sw = await W(order0[1]).locator('.widget-size button').first().boundingBox();
  await touch('touchStart', sw.x + sw.width / 2, sw.y + sw.height / 2); await page.waitForTimeout(700);
  const heldOnControl = await lifted();
  await touch('touchEnd'); await page.waitForTimeout(300);
  ok(!heldOnControl, 'phone: a long press on Compact/Full does not pick the widget up');
  const xb = await W(order0[1]).locator('.widget-remove').boundingBox();
  await touch('touchStart', xb.x + xb.width / 2, xb.y + xb.height / 2); await page.waitForTimeout(700);
  const heldOnX = await lifted();
  await touch('touchMove', xb.x + 40, xb.y + 120); await touch('touchEnd'); await page.waitForTimeout(300);
  ok(!heldOnX && (await ids()).length === order0.length, 'phone: nor on ✕, and moving off ✕ takes nothing off');

  // a long press in the middle of the first widget picks it up; dragged down past the second, it goes after it
  await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(300);
  const first = order0[0], second = order0[1];
  b = await W(first).boundingBox();
  x = b.x + b.width / 2; y = b.y + b.height * 0.6;
  await touch('touchStart', x, y); await page.waitForTimeout(600);
  ok(await lifted(), 'phone: a long press in the middle of a widget picks it up');
  const s2 = await W(second).boundingBox();
  const ty = s2.y + s2.height * 0.6, sy0 = await page.evaluate(() => window.scrollY);
  for (let i = 1; i <= 12; i++) { await touch('touchMove', x, y + (ty - y) * i / 12); await page.waitForTimeout(20); }
  const scrolledWhileHeld = Math.abs(await page.evaluate(() => window.scrollY) - sy0);
  await page.screenshot({ path: `${process.argv[2]}/drag-anywhere-phone.png` });
  await touch('touchEnd'); await page.waitForTimeout(600);
  const after = await ids();
  ok(after.indexOf(first) > after.indexOf(second), `phone: long-press-dragged down past ${second}, ${first} goes after it (${after.slice(0, 3).join(', ')})`);
  ok(order && order.indexOf(first) > order.indexOf(second), 'phone: and the new order is kept');
  ok(scrolledWhileHeld < 40, `phone: the page does not scroll away under the finger while it is held (${scrolledWhileHeld} px)`);
  ok(!(await lifted()), 'phone: let go, it is put down');
  // a tap on a control still works
  const now = await W(second).evaluate((e) => e.classList.contains('size-compact'));
  await W(second).locator('.widget-size button', { hasText: now ? 'Full' : 'Compact' }).tap(); await page.waitForTimeout(400);
  ok(await W(second).evaluate((e) => e.classList.contains('size-compact')) !== now, 'phone: a tap on Compact/Full still switches the size');
  ok(errs.length === 0, `phone: no page errors ${errs.join('|')}`);
  await ctx.close();
}
await browser.close();
