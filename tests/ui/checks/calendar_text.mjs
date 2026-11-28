// The Calendar's words in full (Greg, 27 Nov: "most of the explanations are cut off" in the Day view). In Day,
// Week and List, on a 360 px phone and a desktop, no event's title, time, place or notes is cut short with an
// ellipsis or clipped; the Day view shows each event's place and notes as well, whole, line breaks kept.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2];
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const pad = (n) => String(n).padStart(2, '0');
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const now = new Date(), tomorrow = iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
const LONG = 'the spring trip to the science museum';
const WHERE = 'use the side gate on Orchard Lane after 6 pm';
const NOTE = 'Parking is full by 6:15, so walk if it is dry.';

// Every piece of event text, and whether any is cut: wider than its box, an ellipsis, or a box shorter than its words.
const clipped = (page, sel) => page.evaluate((sel) => {
  const out = [];
  for (const el of document.querySelectorAll(`${sel} .cal-event *, ${sel} .cal-event`)) {
    if (!el.offsetParent || !el.textContent.trim()) continue;
    const cs = getComputedStyle(el);
    if (el.scrollWidth > el.clientWidth + 1 && cs.overflowX !== 'visible') out.push(`wide: ${el.className}`);
    if (el.scrollHeight > el.clientHeight + 1 && cs.overflowY !== 'visible') out.push(`tall: ${el.className}`);
    if (cs.textOverflow === 'ellipsis' && cs.overflowX !== 'visible') out.push(`ellipsis: ${el.className}`);
  }
  return [...new Set(out)];
}, sel);

for (const [w, hgt, name, mobile] of [[360, 800, 'phone', true], [1440, 900, 'desk', false]]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mobile, hasTouch: mobile, deviceScaleFactor: 2 });
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  // Day
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/calendar/day/${tomorrow}`); await page.waitForTimeout(1500);
  const day = page.locator('.cal-dayview');
  const text = await day.innerText();
  ok(text.includes(LONG) && text.includes(WHERE) && text.includes(NOTE), `${name} Day: the long title, its place and its notes in full`);
  ok(/Bring the signed trip form and the reading record\.\n/.test(text), `${name} Day: the notes keep their lines`);
  const cut = await clipped(page, '.cal-dayview');
  ok(cut.length === 0, `${name} Day: no event text cut${cut.length ? ' (' + cut.join(', ') + ')' : ''}`);
  ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${name} Day: nothing wider than the screen`);
  await page.screenshot({ path: `${OUT}/calendar-text-${name}-day.png`, fullPage: true });
  await page.locator('.cal-dayview .cal-event', { hasText: 'Parents' }).click(); await page.waitForTimeout(300);
  ok((await page.locator('.cal-panel').innerText()).includes(NOTE), `${name} Day: a tap still opens the event in full`);
  await page.keyboard.press('Escape'); await page.waitForTimeout(100);
  // Week and List
  for (const v of ['week', 'list']) {
    await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/calendar/${v}/${tomorrow}`); await page.waitForTimeout(1200);
    const sel = v === 'week' ? '.cal-week' : '.cal-agenda';
    ok((await page.locator(sel).innerText()).includes(LONG), `${name} ${v}: the long title in full`);
    const c = await clipped(page, sel);
    ok(c.length === 0, `${name} ${v}: no event text cut${c.length ? ' (' + c.join(', ') + ')' : ''}`);
    await page.screenshot({ path: `${OUT}/calendar-text-${name}-${v}.png`, fullPage: true });
  }
  // The bar above: the month name and Add fit (the Calendar is a phone tab now, 27 Nov; 320 px too).
  if (mobile) {
    await page.setViewportSize({ width: 320, height: 640 });
    await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/calendar/month/${tomorrow}`); await page.waitForTimeout(1200);
    const bar = await page.evaluate(() => { const vw = document.documentElement.clientWidth;
      return [...document.querySelectorAll('.cal-bar button, .cal-bar h2')].filter((e) => e.offsetParent && e.getBoundingClientRect().right > vw + 1).map((e) => e.className); });
    ok(bar.length === 0, `320 px: the Calendar's bar fits${bar.length ? ' (' + bar.join(', ') + ')' : ''}`);
    await page.screenshot({ path: `${OUT}/calendar-text-320-month.png` });
  }
  ok(errs.length === 0, `${name}: no page errors ${errs.join('|')}`);
  await ctx.close();
}
await browser.close();
