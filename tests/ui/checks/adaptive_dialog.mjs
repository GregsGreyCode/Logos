// Logos's one dialog (Home Assistant review rec. 8; Greg, 28 Nov): a sheet from the bottom on a phone, at
// 870 px wide or less, or 500 px tall or less; in the middle of a wide screen. Escape, a tap outside and
// (as a sheet) a swipe down close it; Tab stays inside it; the focus goes back to what opened it.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}`;

// "Restart the agent" asks first ("Restart …?"): a confirm on top of the agent's settings.
async function openAsk(page) {
  const btn = page.locator('button', { hasText: /^(Save and restart|Restart) the agent$/ });
  await btn.evaluate((el) => { el.dataset.opener = '1'; });
  await btn.click(); await page.waitForTimeout(500);
  return page.locator('.ask-panel');
}
const opener = (page) => page.evaluate(() => document.activeElement?.dataset.opener === '1');
const gone = async (page) => await page.locator('.ask-layer').count() === 0;

const SIZES = [
  ['phone', { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }, 'sheet'],
  ['tablet', { viewport: { width: 820, height: 1180 }, isMobile: true, hasTouch: true }, 'sheet'],
  ['phone-landscape', { viewport: { width: 844, height: 390 }, isMobile: true, hasTouch: true }, 'sheet'],
  ['short-laptop', { viewport: { width: 1280, height: 480 } }, 'sheet'],
  ['desktop', { viewport: { width: 1440, height: 900 } }, 'centred'],
];
for (const [label, opts, want] of SIZES) {
  const ctx = await browser.newContext(opts);
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto(`${BASE}/#/agents/greg/settings`); await page.waitForTimeout(1800);
  const { width: W, height: H } = opts.viewport;
  const panel = await openAsk(page);
  ok(await panel.isVisible() && /Restart/.test(await panel.innerText()), `${label}: the confirm opens on top of the page`);
  ok(page.url().endsWith('#/agents/greg/settings'), `${label}: without leaving the page`);
  const b = await panel.boundingBox();
  const grip = await page.locator('.ask-panel .dialog-grip').isVisible();
  if (want === 'sheet') {
    ok(Math.abs(b.y + b.height - H) <= 1.5, `${label}: a sheet, anchored to the bottom (bottom ${Math.round(b.y + b.height)} of ${H})`);
    ok(b.y > H * 0.25 || H < 500, `${label}: only as tall as it needs (top ${Math.round(b.y)})`);
    ok(grip, `${label}: with a handle to pull it down`);
    const r = await panel.evaluate((el) => getComputedStyle(el).borderTopLeftRadius + ' ' + getComputedStyle(el).borderBottomLeftRadius);
    ok(/^2\dpx 0px$/.test(r), `${label}: round at the top, square at the bottom (${r})`);
  } else {
    ok(Math.abs(b.x + b.width / 2 - W / 2) <= 2 && Math.abs(b.y + b.height / 2 - H / 2) <= 2,
      `${label}: centred (${Math.round(b.x + b.width / 2)},${Math.round(b.y + b.height / 2)} of ${W / 2},${H / 2})`);
    ok(!grip && b.width <= 480, `${label}: a dialog, no handle (${Math.round(b.width)} px wide)`);
  }
  ok(await page.evaluate(() => document.activeElement?.closest('.ask-panel') != null), `${label}: the focus moves into it`);
  if (label === 'phone' || label === 'desktop') await page.screenshot({ path: `${process.argv[2]}/adaptive_dialog_${label}.png` });
  // Tab stays inside it, both ways.
  let inside = true;
  for (let i = 0; i < 6; i++) { await page.keyboard.press('Tab'); inside = inside && await page.evaluate(() => !!document.activeElement?.closest('.ask-panel')); }
  for (let i = 0; i < 6; i++) { await page.keyboard.press('Shift+Tab'); inside = inside && await page.evaluate(() => !!document.activeElement?.closest('.ask-panel')); }
  ok(inside, `${label}: Tab and Shift+Tab stay inside it`);
  // Escape closes it, and the focus goes back to the button.
  await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  ok(await gone(page) && await opener(page), `${label}: Escape closes it, back on the button`);
  // A tap outside closes it too.
  await openAsk(page);
  const pb = await page.locator('.ask-panel').boundingBox();
  if (opts.hasTouch) await page.touchscreen.tap(W / 2, Math.max(4, pb.y / 2)); else await page.mouse.click(W / 2, Math.max(4, pb.y / 2));
  await page.waitForTimeout(200);
  ok(await gone(page) && await opener(page), `${label}: a tap outside closes it, back on the button`);
  // Cancel says no: nothing is restarted.
  if (label === 'phone') {
    const reqs = []; page.on('request', (r) => { if (r.method() === 'POST') reqs.push(new URL(r.url()).pathname); });
    await openAsk(page);
    await page.locator('.ask-panel button', { hasText: 'Cancel' }).tap(); await page.waitForTimeout(300);
    ok(await gone(page) && !reqs.some((p) => p.endsWith('/settings/apply')), `${label}: Cancel closes it and nothing restarts`);
  }
  if (want === 'sheet' && opts.hasTouch) {
    const cdp = await ctx.newCDPSession(page);
    const swipe = async (from, to) => {
      const pts = (y) => [{ x: W / 2, y, id: 1 }];
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: pts(from) });
      for (let i = 1; i <= 6; i++) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: pts(from + (to - from) * i / 6) }); await page.waitForTimeout(16); }
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await page.waitForTimeout(400);
    };
    // A short pull springs back; a long one closes it.
    await openAsk(page);
    let g = await page.locator('.ask-panel .dialog-grip').boundingBox();
    await swipe(g.y + g.height / 2, g.y + g.height / 2 + 20);
    const back = await page.locator('.ask-panel').boundingBox();
    ok(!await gone(page) && Math.abs(back.y + back.height - H) <= 1.5, `${label}: a short pull springs back`);
    g = await page.locator('.ask-panel .dialog-grip').boundingBox();
    await swipe(g.y + g.height / 2, Math.min(H - 2, g.y + 200));
    ok(await gone(page) && await opener(page), `${label}: a swipe down closes it, back on the button`);
  }
  ok(!errs.length, `${label}: no page errors ${errs.join(' | ')}`);
  await ctx.close();
}

// Motion: still when the person asks for less.
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, reducedMotion: 'reduce' });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/#/agents/greg/settings`); await page.waitForTimeout(1800);
  const panel = await openAsk(page);
  ok(await panel.evaluate((el) => getComputedStyle(el).animationName) === 'none', 'reduced motion: the sheet does not slide in');
  await ctx.close();
}

// The other small tasks on the same dialog: the voice picker, a new routine, adding an event.
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  const atBottom = async (sel) => { const b = await page.locator(sel).boundingBox(); return b && Math.abs(b.y + b.height - 844) <= 1.5; };
  await page.goto(`${BASE}/#/agents/greg`); await page.waitForTimeout(1800);
  await page.locator('button', { hasText: '+ New routine' }).tap(); await page.waitForTimeout(400);
  ok(await atBottom('.ask-panel[aria-label="New routine"]') && await page.locator('.ask-panel[aria-label="New routine"] .dialog-grip').isVisible(), 'New routine: a sheet from the bottom');
  await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  ok(await gone(page), 'New routine: Escape closes it');
  await page.goto(`${BASE}/#/calendar`); await page.waitForTimeout(1800);
  const add = page.locator('.cal-add, button[aria-label^="Add an event"]').first();
  if (await add.count()) {
    await add.tap(); await page.waitForTimeout(400);
    ok(await atBottom('.cal-panel'), 'Add an event: a sheet from the bottom');
    await page.keyboard.press('Escape'); await page.waitForTimeout(200);
    ok(await gone(page), 'Add an event: Escape closes it');
  } else ok(false, 'Add an event: its button is there');
  ok(!errs.length, `no page errors ${errs.join(' | ')}`);
  await ctx.close();
}
await browser.close();
