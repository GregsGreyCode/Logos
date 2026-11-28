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
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}/`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const pad = (n) => String(n).padStart(2, '0');
const now = new Date(), today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
const box = (page, sel) => page.locator(sel).first().boundingBox();

for (const [w, hgt] of [[1280, 800], [1440, 900], [1920, 1080]]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt } });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  for (const v of ['month', 'week', 'day', 'list']) {
    await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/calendar/${v}/${today}`); await page.waitForTimeout(1300);
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
    // "‹ period ›": the arrows either side of the heading, saying what they step by; Today apart, quiet when today is shown.
    const unit = v === 'list' ? '4 weeks' : v;
    const nav = await page.evaluate(() => {
      const [p, n] = document.querySelectorAll('.cal-nav .cal-step'), t = document.querySelector('.cal-title').getBoundingClientRect();
      const a = p.getBoundingClientRect(), b = n.getBoundingClientRect(), td = document.querySelector('.cal-today');
      return { p: p.getAttribute('aria-label'), n: n.getAttribute('aria-label'), pt: p.title, around: a.right <= t.left + 1 && b.left >= t.right - 1 && b.left - t.right < 16 && t.left - a.right < 16,
        todayApart: !document.querySelector('.cal-nav').contains(td), disabled: td.disabled };
    });
    ok(nav.p === `Previous ${unit}` && nav.n === `Next ${unit}` && nav.pt === nav.p, `${w} ${v}: the arrows say "${nav.p}" / "${nav.n}"`);
    ok(nav.around, `${w} ${v}: the arrows sit either side of the period`);
    ok(nav.todayApart && nav.disabled, `${w} ${v}: Today is its own button, disabled while today is shown`);
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
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/calendar/day/${today}`); await page.waitForTimeout(1000);
  await page.locator('.cal-aside .cal-mini-day:not(.other)').first().click(); await page.waitForTimeout(600);
  ok(/#\/calendar\/day\/\d{4}-\d\d-01$/.test(await page.evaluate(() => location.hash)) && await page.locator('.cal-dayview').isVisible(), `${w}: a day in the month beside opens it`);

  // Month (Greg, 28 Nov): a click on a day opens that day; its "+" adds an event on it; an event in it opens the event.
  await page.goto(`${BASE}#/calendar/month/${today}`); await page.waitForTimeout(1000);
  const target = await page.$$eval('.cal-month .cal-day:not(.other)', (els, t) => els.map((e) => e.dataset.day).find((d) => d !== t), today);
  const cell = page.locator(`.cal-day[data-day="${target}"]`);
  await cell.locator('.cal-num').click(); await page.waitForTimeout(150);
  ok(await page.evaluate(() => location.hash) === `#/calendar/day/${target}` && await page.locator('.cal-dayview').isVisible()
    && (await page.locator('.cal-title').textContent()).includes(String(Number(target.slice(8)))), `${w}: a click on ${target} opens that day in Day view, at once`);
  await page.goBack(); await page.waitForTimeout(700);
  ok((await page.evaluate(() => location.hash)).startsWith('#/calendar/month') && await page.locator('.cal-month').isVisible(), `${w}: Back returns to the month`);
  await page.mouse.move(2, 2); await page.waitForTimeout(200);
  ok(await cell.locator('.cal-day-add').evaluate((e) => getComputedStyle(e).opacity) === '0', `${w}: the "+" stays hidden until the pointer is over the day`);
  await cell.hover(); await page.waitForTimeout(200);
  const plus = cell.locator('.cal-day-add');
  const [pb, cb] = [await plus.boundingBox(), await cell.boundingBox()];
  ok(await plus.evaluate((e) => getComputedStyle(e).opacity) === '1' && pb.x + pb.width > cb.x + cb.width - 12 && pb.y < cb.y + 12, `${w}: hovering shows a "+" in the day's corner`);
  ok(/^Add an event on /.test(await plus.getAttribute('aria-label')), `${w}: the "+" is labelled ("${await plus.getAttribute('aria-label')}")`);
  await plus.click(); await page.waitForTimeout(250);
  ok(await page.locator('.cal-form').isVisible() && (await page.inputValue('#cal-f-date')) === target && !(await page.isChecked('#cal-f-allday'))
    && (await page.inputValue('#cal-f-from')) === '09:00', `${w}: "+" opens Add with ${target} filled in, not all day, from 09:00`);
  ok((await page.evaluate(() => location.hash)).startsWith('#/calendar/month') && await page.locator('.cal-month').isVisible(), `${w}: and stays on the month`);
  const opts = await page.$$eval('#cal-f-cal option', (os) => os.map((o) => o.textContent));
  ok(JSON.stringify(opts) === JSON.stringify(['Your calendar', 'Household']), `${w}: only the calendars you can change (${opts})`);
  // ✕ in the corner, Escape closes, the focus goes back to the "+"
  const [xb, panel, ttl] = [await box(page, '.cal-panel .cal-x'), await box(page, '.cal-panel'), await box(page, '.cal-panel-title h2')];
  ok(xb && panel.x + panel.width - (xb.x + xb.width) < 24 && xb.y - panel.y < 30 && Math.abs((xb.y + xb.height / 2) - (ttl.y + ttl.height / 2)) < 8, `${w}: the form's ✕ in its top-right corner, in line with the title`);
  ok(await page.locator('.cal-panel button', { hasText: /^Close$/ }).count() === 0 && (await page.locator('.cal-panel .cal-x').getAttribute('aria-label')) === 'Close', `${w}: one close, ✕ labelled Close`);
  await page.keyboard.press('Escape'); await page.waitForTimeout(150);
  ok(await page.locator('.cal-panel').count() === 0 && await page.evaluate(() => document.activeElement.classList.contains('cal-day-add')), `${w}: Escape closes it, back on the "+"`);
  // Keyboard: Enter on a day opens it; Tab from a day reaches its "+", which shows.
  await cell.focus(); await page.keyboard.press('Tab');
  ok(await page.evaluate(() => document.activeElement.classList.contains('cal-day-add') && getComputedStyle(document.activeElement).opacity === '1'), `${w}: Tab from a day reaches its "+", and it shows`);
  await cell.focus(); await page.keyboard.press('Enter'); await page.waitForTimeout(150);
  ok(await page.evaluate(() => location.hash) === `#/calendar/day/${target}`, `${w}: Enter on a day opens it`);
  await page.goto(`${BASE}#/calendar/month/${today}`); await page.waitForTimeout(800);
  await page.locator(`.cal-day[data-day="${today}"] .cal-pill`, { hasText: 'Call Maman' }).click(); await page.waitForTimeout(250);
  ok(/Call Maman/.test(await page.locator('.cal-panel').innerText().catch(() => '')) && (await page.evaluate(() => location.hash)).startsWith('#/calendar/month'), `${w}: an event in a day opens that event, not the day`);
  ok((await page.locator('.cal-panel .cal-owner').innerText()) === 'Béré\'s calendar' && await page.locator('.cal-panel .row').count() === 0, `${w}: Béré's event says whose, with nothing to change`);
  if (w === 1440) await page.screenshot({ path: `${OUT}/desk/calendar-${w}-event.png` });
  await page.keyboard.press('Escape'); await page.waitForTimeout(100);
  if (w === 1440) { await cell.hover(); await page.waitForTimeout(200); await page.screenshot({ path: `${OUT}/desk/calendar-${w}-month-plus.png` }); }

  // Week: arrows before Monday and after Sunday, one week a click, sliding in; Left/Right too; the bar's agree.
  await page.goto(`${BASE}#/calendar/week/${today}`); await page.waitForTimeout(1000);
  const [pa, na, wk] = [await box(page, '[data-nav="week-prev"]'), await box(page, '[data-nav="week-next"]'), await box(page, '.cal-week')];
  const mid = wk.y + wk.height / 2;
  ok(pa && na && pa.x + pa.width <= wk.x + 1 && wk.x - (pa.x + pa.width) < 20 && na.x >= wk.x + wk.width - 1 && na.x - (wk.x + wk.width) < 20
    && Math.abs(pa.y + pa.height / 2 - mid) < 4 && Math.abs(na.y + na.height / 2 - mid) < 4 && pa.width >= 40,
  `${w} week: an arrow at the grid's left and right edges, centred on it (${Math.round(pa.x + pa.width)} | ${Math.round(wk.x)}–${Math.round(wk.x + wk.width)} | ${Math.round(na.x)})`);
  ok((await page.locator('[data-nav="week-prev"]').getAttribute('aria-label')) === 'Previous week' && (await page.locator('[data-nav="week-next"]').getAttribute('aria-label')) === 'Next week', `${w} week: labelled Previous week / Next week`);
  const t0 = await page.locator('.cal-title').textContent(), mon0 = await page.locator('.cal-week .cal-col').first().getAttribute('data-day');
  const plus7 = (s, n) => { const d = new Date(`${s}T12:00:00`); d.setDate(d.getDate() + n); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  await page.locator('[data-nav="week-next"]').click();
  const anims = await page.evaluate(() => document.getAnimations().filter((a) => a.effect && a.effect.target && a.effect.target.classList.contains('cal-week')).length);
  await page.waitForTimeout(350);
  ok(anims === 1, `${w} week: the new week slides in (${anims})`);
  ok(await page.locator('.cal-week .cal-col').first().getAttribute('data-day') === plus7(mon0, 7) && await page.evaluate(() => location.hash) === `#/calendar/week/${plus7(today, 7)}`
    && (await page.locator('.cal-title').textContent()) !== t0, `${w} week: Next week moves exactly one week, heading and address with it`);
  ok(!(await page.locator('.cal-today').isDisabled()), `${w} week: Today wakes up once today is not shown`);
  ok(await page.evaluate(() => document.activeElement.dataset.nav) === 'week-next', `${w} week: the arrow keeps the focus`);
  if (w === 1440) await page.screenshot({ path: `${OUT}/desk/calendar-${w}-week-next.png` });
  await page.keyboard.press('ArrowLeft'); await page.waitForTimeout(250);
  ok(await page.locator('.cal-week .cal-col').first().getAttribute('data-day') === mon0 && (await page.locator('.cal-title').textContent()) === t0, `${w} week: Left goes back a week while the week has the focus`);
  await page.keyboard.press('ArrowRight'); await page.waitForTimeout(250);
  ok(await page.locator('.cal-week .cal-col').first().getAttribute('data-day') === plus7(mon0, 7), `${w} week: Right goes on a week`);
  await page.locator('.cal-nav .cal-step[aria-label="Previous week"]').click(); await page.waitForTimeout(250);
  ok(await page.locator('.cal-week .cal-col').first().getAttribute('data-day') === mon0 && await page.locator('.cal-today').isDisabled(), `${w} week: the bar's arrow steps the same week, Today quiet again`);
  await page.locator('[data-nav="week-prev"]').click(); await page.waitForTimeout(250);
  await page.locator('.cal-today').click(); await page.waitForTimeout(250);
  ok(await page.locator('.cal-week .cal-col').first().getAttribute('data-day') === mon0, `${w} week: Today comes back to this week`);
  ok(errs.length === 0, `${w}: no page errors ${errs.join(' | ')}`);
  await page.close();
}

// Reduced motion: the week changes, without sliding.
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  const page = await ctx.newPage();
  await page.goto(`${BASE}#/calendar/week/${today}`); await page.waitForTimeout(1000);
  const t0 = await page.locator('.cal-title').textContent();
  await page.locator('[data-nav="week-next"]').click();
  const n = await page.evaluate(() => document.getAnimations().filter((a) => a.effect && a.effect.target && a.effect.target.classList.contains('cal-week')).length);
  ok(n === 0 && (await page.locator('.cal-title').textContent()) !== t0, `reduced motion: Next week, without the slide (${n})`);
  await ctx.close();
}

// A 320 px phone: ‹ period › on one line, then Today and Add, then the views; nothing sideways.
{
  const ctx = await browser.newContext({ viewport: { width: 320, height: 700 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  for (const v of ['month', 'week', 'day', 'list']) {
    await page.goto(`${BASE}#/calendar/${v}/${today}`); await page.waitForTimeout(900);
    const r = await page.evaluate(() => {
      const g = (s) => document.querySelector(s).getBoundingClientRect();
      const [p, n] = [...document.querySelectorAll('.cal-nav .cal-step')].map((e) => e.getBoundingClientRect());
      const t = g('.cal-title'), td = g('.cal-today'), ad = g('.cal-add'), vw = g('.cal-views');
      const line = parseFloat(getComputedStyle(document.querySelector('.cal-title')).lineHeight);
      return { row1: Math.abs(p.top - n.top) < 2 && p.right <= t.left + 1 && n.left >= t.right - 1, oneLine: t.height < line * 1.5,
        row2: td.top >= Math.max(p.bottom, t.bottom) - 1 && Math.abs(td.top - ad.top) < 2 && ad.right > innerWidth - 40, row3: vw.top >= td.bottom - 1,
        wide: document.documentElement.scrollWidth > innerWidth + 1, text: document.querySelector('.cal-title').textContent };
    });
    ok(r.row1 && r.oneLine && r.row2 && r.row3 && !r.wide, `320 ${v}: "‹ ${r.text} ›" on one line, Today and Add under it, the views under them`);
    await page.screenshot({ path: `${OUT}/calendar-320-${v}-bar.png` });
  }
  await ctx.close();
}
await browser.close();
