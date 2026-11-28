// No widget says "null", "undefined" or "NaN" (Greg, 28 Nov: "the compact version of the widget Next shows null"): every
// widget, Compact and Full, at phone and desktop sizes, normally and while arranging. Then Next's Compact (one line: the
// calendar's colour, the time, the title, how long), with an event still to come today, with only tomorrow's, and with
// nothing at all.
import { createRequire } from 'module';
import { routeAll, ALL } from '../widget-fixtures.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const URL_HOME = `http://127.0.0.1:${process.env.PORT || 8779}/#/home`;
const BAD = /\b(null|undefined|NaN)\b/;
const TIPS = ['home', 'agents', 'chat', 'notifications', 'routines'];
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const sizes = [[390, 844, true, 'phone'], [1440, 900, false, 'desk']];

for (const [w, hgt, mob, name] of sizes) for (const size of ['compact', 'full']) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await routeAll(page, { prefs: { tips_seen: TIPS, widget_sizes: Object.fromEntries(ALL.map((id) => [id, size])) } });
  await page.goto(URL_HOME); await page.waitForTimeout(3000);
  const bad = async () => page.$$eval('.home-widgets > .home-widget, .home-band-widget, .home-tray, .home-waiting', (els, re) => els
    .filter((e) => new RegExp(re).test(e.textContent)).map((e) => `${e.dataset.widget || e.className}: ${e.textContent.match(new RegExp(re))[0]}`), BAD.source);
  let b = await bad();
  ok(b.length === 0, `${name} ${size}: no widget says null, undefined or NaN ${b.join(' | ')}`);
  await page.locator('.home-edit-link').click(); await page.waitForTimeout(600);
  b = await bad();
  ok(b.length === 0, `${name} ${size}: nor while arranging ${b.join(' | ')}`);
  ok(errs.length === 0, `${name} ${size}: no page errors ${errs.join('|')}`);
  await page.close();
}

// Next, Compact: with an event to come today, with only tomorrow's, and with nothing.
const pad = (n) => String(n).padStart(2, '0');
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const hm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const soon = new Date(Date.now() + 40 * 60000), tmr = new Date(Date.now() + 86400000);
const today = ymd(new Date());
const CASES = {
  coming: [{ start: `${ymd(soon)} ${hm(soon)}`, title: 'Call with a rather long name that will not fit on one line at all', calendar: 'household' }],
  tomorrow: [{ start: `${ymd(tmr)} 09:00`, title: 'Standup', calendar: 'bere' }],
  nothing: [],
};
for (const [w, hgt, mob, name] of sizes) for (const [label, events] of Object.entries(CASES)) {
  if (label === 'coming' && ymd(soon) !== today) continue;     // too near midnight to have one still today
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob, deviceScaleFactor: 2 });
  await routeAll(page, { widgets: ['next', 'weather'], prefs: { tips_seen: TIPS, widget_sizes: { next: 'compact' } } });
  await page.route('**/api/v2/home', async (r) => { const res = await r.fetch(); const d = await res.json(); d.today = { ...d.today, date: today, events, busy: [] }; await r.fulfill({ json: d }); });
  await page.goto(URL_HOME); await page.waitForTimeout(2500);
  const sec = page.locator('.home-widget[data-widget="next"]'), line = sec.locator('.home-next');
  ok(await sec.isVisible() && await line.isVisible(), `${name} Next compact, ${label}: it shows`);
  const text = (await line.innerText()).replace(/\s+/g, ' ').trim();
  ok(!BAD.test(text), `${name} Next compact, ${label}: no null/undefined/NaN ("${text}")`);
  const box = await line.boundingBox();
  const lh = await line.evaluate((e) => parseFloat(getComputedStyle(e).lineHeight) || 24);
  ok(box.height <= lh + 26, `${name} Next compact, ${label}: one line (${Math.round(box.height)} px)`);
  ok(await line.evaluate((e) => e.scrollWidth <= e.clientWidth + 1), `${name} Next compact, ${label}: nothing spills out of it`);
  if (label === 'coming') {
    ok(/^\d\d:\d\d Call with/.test(text) && /\b\d+ min$/.test(text) && !/Next/.test(text), `${name}: the time, the title and how long in a word ("${text}")`);
    ok(await line.locator('.dot.household').count() === 1, `${name}: in its calendar's colour`);
    ok(await line.locator('.home-next-what').evaluate((e) => e.scrollWidth > e.clientWidth && getComputedStyle(e).textOverflow === 'ellipsis'), `${name}: a long title is cut with …, the time still in view`);
  } else if (label === 'tomorrow') {
    ok(text === 'Tomorrow 09:00 Standup' && await line.locator('.dot.bere').count() === 1, `${name}: nothing more today: tomorrow's first instead ("${text}")`);
  } else ok(text === 'Nothing more today' && await line.locator('.dot').count() === 0, `${name}: nothing at all: says so, no empty box ("${text}")`);
  await page.screenshot({ path: `${process.argv[2]}/next-compact-${label}-${name}.png`, clip: { x: Math.max(0, box.x - 16), y: Math.max(0, box.y - 16), width: Math.min(w - Math.max(0, box.x - 16), box.width + 32), height: box.height + 32 } });
  await page.close();
}
await browser.close();
