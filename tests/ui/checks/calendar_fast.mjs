// The Calendar feels fast, and its key sits under it (Greg, 27 Nov). With a slow server (1.2 s an answer):
// a first visit shows a light placeholder, not an empty screen; what this device kept draws at once; an old
// kept copy is replaced in place when the fresh one comes, keeping the picked day and an open event; the
// month either side is fetched ahead, so Later is instant. The key (Yours / Household / Béré) is under the
// month grid and above the day's list on a phone, under the grid on a desktop.
import { createRequire } from 'module';
import fs from 'fs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2];
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}/`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const box = (page, sel) => page.locator(sel).first().boundingBox();

// ── The key's place ──
{
  const ctx = await browser.newContext({ viewport: { width: 360, height: 800 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  await page.goto(BASE + '#/calendar/month'); await page.waitForTimeout(1500);
  const [g, k, s] = [await box(page, '.cal-month'), await box(page, '.cal-legend'), await box(page, '.cal-side')];
  ok(g && k && s && k.y >= g.y + g.height - 1 && s.y >= k.y + k.height - 1, `phone: grid, then the key, then the day's list (${Math.round(g.y + g.height)} ≤ ${Math.round(k.y)}, ${Math.round(k.y + k.height)} ≤ ${Math.round(s.y)})`);
  await page.screenshot({ path: `${OUT}/calendar-fast-phone-key.png`, fullPage: true });
  await page.locator('.cal-views button', { hasText: 'Day' }).tap(); await page.waitForTimeout(600);
  const [d, k2] = [await box(page, '.cal-dayview'), await box(page, '.cal-legend')];
  ok(d && k2 && k2.y >= d.y + d.height - 1, 'phone Day: the key under the day');
  await page.locator('.cal-chip', { hasText: 'Household' }).tap(); await page.waitForTimeout(150);
  ok((await page.locator('.cal-chip', { hasText: 'Household' }).getAttribute('aria-pressed')) === 'false', 'the key still switches a calendar off');
  await page.locator('.cal-chip', { hasText: 'Household' }).tap();
  await ctx.close();

  const desk = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const dp = await desk.newPage();
  await dp.goto(BASE + '#/calendar/month'); await dp.waitForTimeout(1500);
  const [G, K, S] = [await box(dp, '.cal-month'), await box(dp, '.cal-legend'), await box(dp, '.cal-side')];
  ok(G && K && S && K.y >= G.y + G.height - 1 && K.x < S.x && Math.abs(K.x - G.x) < 2, 'desktop: the key under the grid, the day\'s list beside');
  await dp.screenshot({ path: `${OUT}/calendar-fast-desk-key.png` });
  await desk.close();
}

// ── Fast ──
{
  const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  const calls = []; page.on('request', (r) => { const u = new URL(r.url()); if (u.pathname === '/api/v2/calendar') calls.push(u.search); });
  fs.writeFileSync(OUT + '/slow', '1');
  try {
    // First visit, straight to the Calendar: nothing kept, so a placeholder (Home would have fetched it ahead).
    await page.goto(BASE + '#/calendar/month');
    await page.locator('.cal-body .loading').waitFor({ timeout: 4000 }).catch(() => {});
    ok(await page.locator('.cal-body .loading').count() === 1 && await page.locator('.cal-month').count() === 0, 'a first visit: a placeholder while it loads');
    await page.screenshot({ path: `${OUT}/calendar-fast-wait.png` });
    await page.waitForTimeout(2000);
    ok(await page.locator('.cal-month').isVisible() && await page.locator('.cal-body .loading').count() === 0, 'then the month');
    await page.waitForTimeout(2800);                     // the months either side, fetched ahead (slowly)
    const n = calls.length;
    await page.locator('.cal-step[aria-label="Next month"]').tap(); await page.waitForTimeout(150);
    ok(await page.locator('.cal-month').isVisible() && await page.locator('.cal-body .loading').count() === 0, 'Later: the next month at once, fetched ahead');
    ok(!calls.slice(n).includes(calls[1]) && !calls.slice(n).includes(calls[2]), 'without asking again');
    await page.locator('.cal-step[aria-label="Previous month"]').tap(); await page.waitForTimeout(150);
    ok(await page.locator('.cal-month').isVisible(), 'Earlier: back at once');

    // An old kept copy: drawn at once, then the fresh one in place, the picked day and the open event kept.
    await page.goto(BASE + '#/home'); await page.waitForTimeout(1800);
    await page.evaluate(() => {
      for (const k of Object.keys(localStorage)) {
        if (!k.includes('/api/v2/calendar')) continue;
        const v = JSON.parse(localStorage.getItem(k));
        v.t -= 10 * 60000;                                // ten minutes old
        if (v.v && v.v.events) v.v.events = v.v.events.filter((e) => !e.id.includes('dentist'));   // and missing one event
        localStorage.setItem(k, JSON.stringify(v));
      }
    });
    await page.reload(); await page.locator('.home-widgets').waitFor({ timeout: 15000 });   // a new page: no memory copy, the device's kept one
    const before = calls.length;
    await page.evaluate(() => { location.hash = '#/calendar/month'; });
    await page.waitForTimeout(250);
    ok(await page.locator('.cal-month').isVisible() && await page.locator('.cal-body .loading').count() === 0, 'a kept copy draws at once');
    const pick = await page.evaluate(() => { const d = new Date(); d.setDate(d.getDate() + 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; });
    if (await page.locator(`.cal-day[data-day="${pick}"]:not(.other)`).count() === 0) await page.locator('.cal-step[aria-label="Next month"]').tap();   // the last day of a month
    await page.locator(`.cal-day[data-day="${pick}"]`).tap(); await page.waitForTimeout(100);
    ok(!(await page.locator('.cal-side').innerText()).includes('Dentist'), '(the old copy, without the newest event)');
    await page.locator('.cal-side .cal-event').first().tap(); await page.waitForTimeout(100);
    ok(await page.locator('.cal-panel').isVisible(), 'an event opened while the fresh copy comes');
    await page.waitForTimeout(2200);
    ok(calls.length > before, 'the fresh copy was asked for');
    ok(await page.locator('.cal-panel').isVisible(), 'the open event stays open through the update');
    await page.keyboard.press('Escape'); await page.waitForTimeout(100);
    ok((await page.locator('.cal-side').innerText()).includes('Dentist'), 'the fresh copy drawn in place');
    ok(await page.locator(`.cal-day.picked[data-day="${pick}"]`).count() === 1, 'the picked day kept');
  } finally { fs.unlinkSync(OUT + '/slow'); }
  ok(errs.length === 0, `no page errors ${errs.join(' | ')}`);
  await ctx.close();
}
await browser.close();
