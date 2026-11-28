// How fast the Calendar answers (Greg, 28 Nov: "still takes a long time to open from day to month"), with the stand-in
// calendar as slow as the real one was that day (1.6 s a range: a file named slow-calendar). Each step is timed from the
// click to the new view drawn with its events (no placeholder, and the events of the range asked for, not the last one's): opening it from Home, Month → Week → Day → Month,
// "Open day", Earlier and Later in Month and in Day (also across a month's end). Every view switch and step is drawn
// from what is on hand, so each must be under 400 ms; opening it from Home too, once Home has been up a moment.
//   The timings are printed (TIME lines) and kept in $OUT/calendar-speed.json.
import { createRequire } from 'module';
import fs from 'fs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2];
const FAST = 400;
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const times = {};

// Click what sel finds, then wait (a frame at a time) until the view is drawn: want's selector there, no placeholder,
// and the title changed when asked to.
async function timed(page, name, sel, want, { titleChanges = false, hasText = null } = {}) {
  const ms = await page.evaluate(async ({ sel, want, titleChanges, hasText }) => {
    const title = () => (document.querySelector('.cal-title') || {}).textContent;
    const was = title();
    const el = [...document.querySelectorAll(sel)].find((e) => e.offsetParent && (!hasText || e.textContent.includes(hasText)));
    if (!el) return -1;
    const t0 = performance.now();
    el.click();
    return new Promise((done) => {
      const tick = () => {
        const body = document.querySelector('.cal-body');     // what it drew is what the address asks for (not the last view's)
        const drawn = document.querySelector(want) && !document.querySelector('.cal-wait') && (!titleChanges || title() !== was)
          && body && body.dataset.drawn && location.hash.endsWith(body.dataset.drawn);
        if (drawn) return done(Math.round(performance.now() - t0));
        if (performance.now() - t0 > 8000) return done(9999);
        requestAnimationFrame(tick);
      };
      tick();
    });
  }, { sel, want, titleChanges, hasText });
  times[name] = ms;
  console.log(`TIME ${name}: ${ms} ms`);
  return ms;
}

for (const [w, hgt, dev, mobile] of [[1440, 900, 'desk', false], [412, 915, 'phone', true]]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mobile, hasTouch: mobile });
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  fs.writeFileSync(`${OUT}/slow-calendar`, '1');
  try {
    await page.goto('http://127.0.0.1:8779/#/home');
    await page.locator('.home-widgets').waitFor(); await page.waitForTimeout(2500);     // Home up a moment
    const open = mobile ? '.tabbar a[data-view="calendar"]' : '.nav-links a[data-view="calendar"]';
    await page.evaluate(() => { try { localStorage.setItem('logos-cal-view', 'month'); } catch (e) { /* fine */ } });
    ok(await timed(page, `${dev} open from Home`, open, '.cal-month .cal-day') < FAST, `${dev}: the Calendar opens from Home in under ${FAST} ms (${times[`${dev} open from Home`]})`);
    await page.waitForTimeout(300);
    for (const [from, to, want] of [['Month', 'Week', '.cal-week'], ['Week', 'Day', '.cal-dayview'], ['Day', 'Month', '.cal-month'], ['Month', 'List', '.cal-agenda'], ['List', 'Month', '.cal-month']]) {
      const t = await timed(page, `${dev} ${from}→${to}`, '.cal-views button', want, { hasText: to });
      ok(t < FAST, `${dev}: ${from} → ${to} in under ${FAST} ms (${t})`);
      await page.waitForTimeout(150);
    }
    for (const [i, step] of ['Later', 'Later', 'Earlier', 'Earlier', 'Earlier'].entries()) {
      const t = await timed(page, `${dev} month ${step} ${i + 1}`, `.cal-step[aria-label="${step}"]`, '.cal-month', { titleChanges: true });
      ok(t < FAST, `${dev}: Month, ${step} (${i + 1}) in under ${FAST} ms (${t})`);
      await page.waitForTimeout(400);                 // a glance at each month
    }
    await page.locator('.cal-today').click(); await page.waitForTimeout(200);
    const t = await timed(page, `${dev} Open day`, '.cal-side a', '.cal-dayview', { hasText: 'Open day' });
    ok(t < FAST, `${dev}: "Open day" in under ${FAST} ms (${t})`);
    await page.waitForTimeout(150);
    for (let i = 0; i < 6; i++) {                      // into next month, a day at a time
      const s = await timed(page, `${dev} day Later ${i + 1}`, '.cal-step[aria-label="Later"]', '.cal-dayview', { titleChanges: true });
      ok(s < FAST, `${dev}: Day, Later (${i + 1}) in under ${FAST} ms (${s})`);
      await page.waitForTimeout(120);
    }
    ok(errs.length === 0, `${dev}: no page errors ${errs.join(' | ')}`);
  } finally { fs.unlinkSync(`${OUT}/slow-calendar`); }
  await ctx.close();
}
fs.writeFileSync(`${OUT}/calendar-speed.json`, JSON.stringify(times, null, 1));
await browser.close();
