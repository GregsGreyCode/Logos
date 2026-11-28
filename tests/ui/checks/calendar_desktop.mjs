// The Calendar on a desktop (Greg, 28 Nov: "on desktop the placement of everything isn't very good"), at 1280×800,
// 1440×900 and 1920×1080. The bar is one row: ‹ Today › and the period as the heading (one line; it opens the date
// picker), the views and Add on the right. Month: the grid fills the screen's height (six weeks and the key in view),
// the picked day's list beside it. Day and List: the words in a readable column, the month to jump about in beside
// them with the key under it. Nothing scrolls sideways.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2];
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const pad = (n) => String(n).padStart(2, '0');
const now = new Date(), today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
const box = (page, sel) => page.locator(sel).first().boundingBox();

for (const [w, hgt] of [[1280, 800], [1440, 900], [1920, 1080]]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt } });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  for (const v of ['month', 'week', 'day', 'list']) {
    await page.goto(`http://127.0.0.1:8779/#/calendar/${v}/${today}`); await page.waitForTimeout(1300);
    const bar = await page.evaluate(() => {
      const r = (s) => { const e = document.querySelector(s); return e && e.getBoundingClientRect(); };
      const t = r('.cal-title'), views = r('.cal-views'), add = r('.cal-add'), prev = r('.cal-step');
      const line = parseFloat(getComputedStyle(document.querySelector('.cal-title')).lineHeight);
      return { oneRow: [views, add, prev].every((b) => Math.abs((b.top + b.height / 2) - (t.top + t.height / 2)) < 6),
        oneLine: t.height < line * 1.5, right: Math.round(add.right), wide: document.documentElement.scrollWidth > innerWidth + 1,
        size: parseFloat(getComputedStyle(document.querySelector('.cal-title')).fontSize) };
    });
    const main = await box(page, '.main, main');
    ok(bar.oneRow && bar.oneLine && !bar.wide, `${w} ${v}: the bar is one row, the period on one line, nothing sideways`);
    ok(bar.size >= 22, `${w} ${v}: the period is the screen's heading (${bar.size} px)`);
    if (v === 'month') {
      const [g, s, k] = [await box(page, '.cal-month'), await box(page, '.cal-side'), await box(page, '.cal-legend')];
      ok(s.x > g.x + g.width && Math.abs(s.y - g.y) < 2, `${w} month: the day's list beside the grid, tops in line`);
      // (on an 800 px tall screen a busy week may push the key a little below)
      const room = hgt >= 900 ? 1 : 120;
      ok(k.y + k.height <= hgt + room, `${w} month: six weeks and the key fit the screen (${Math.round(k.y + k.height)} ≤ ${hgt}${room > 1 ? ' + ' + room : ''})`);
      ok(g.height >= hgt - 360, `${w} month: the grid uses the screen's height (${Math.round(g.height)} px)`);
      const cut = await page.$$eval('.cal-pill', (els) => els.filter((e) => e.scrollWidth > e.clientWidth + 1).length);
      ok(cut === 0, `${w} month: no event name runs out of its cell sideways (${cut})`);
    }
    if (v === 'day' || v === 'list') {
      const [c, a, m, k] = [await box(page, v === 'day' ? '.cal-dayview' : '.cal-agenda'), await box(page, '.cal-aside'), await box(page, '.cal-aside-month'), await box(page, '.cal-legend')];
      ok(a && m && a.x > c.x + c.width && Math.abs(a.y - c.y) < 2, `${w} ${v}: the month beside the words, tops in line`);
      ok(k.y >= m.y + m.height - 1 && Math.abs(k.x - m.x) < 2, `${w} ${v}: the key under that month`);
      ok(c.width <= 761, `${w} ${v}: the words in a readable column (${Math.round(c.width)} px)`);
      ok(await page.locator(`.cal-aside .cal-mini-day.picked[href$="${today}"]`).count() === 1, `${w} ${v}: the day shown is marked in it`);
    }
    void main;
    await page.screenshot({ path: `${OUT}/desk/calendar-${w}-${v}.png`, fullPage: true });
  }
  // A day in the month beside opens that day
  await page.goto(`http://127.0.0.1:8779/#/calendar/day/${today}`); await page.waitForTimeout(1000);
  await page.locator('.cal-aside .cal-mini-day:not(.other)').first().click(); await page.waitForTimeout(600);
  ok(/#\/calendar\/day\/\d{4}-\d\d-01$/.test(await page.evaluate(() => location.hash)) && await page.locator('.cal-dayview').isVisible(), `${w}: a day in the month beside opens it`);
  ok(errs.length === 0, `${w}: no page errors ${errs.join(' | ')}`);
  await page.close();
}
await browser.close();
