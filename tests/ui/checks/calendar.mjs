// The Calendar (Béré's request, 27 Nov; a full app, Greg 27 Nov): month, week, day and list on a phone and a desktop,
// colours and switches per calendar, today marked, a day's events, an event in full, adding and changing through
// the person's own agent, one call per range; Home's Month widget, and Home's calendar widgets opening it.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2];
const BASE = 'http://127.0.0.1:8779/';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const pad = (n) => String(n).padStart(2, '0');
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const now = new Date(), today = iso(now);

async function open(viewport, mobile, { widgets } = {}) {
  const ctx = await browser.newContext({ viewport, isMobile: mobile, hasTouch: mobile, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errs = [], calls = [], posts = [];
  page.on('pageerror', (e) => errs.push(e.message));
  page.on('request', (r) => {
    const u = new URL(r.url());
    if (u.pathname === '/api/v2/calendar') calls.push(u.search);
    if (r.method() === 'POST' && u.pathname.startsWith('/api/v2/calendar/')) posts.push(`${u.pathname} ${r.postData() || ''}`);
  });
  if (widgets) {               // Home with the calendar widgets on it
    await page.route('**/api/v2/me', async (route) => {
      const res = await route.fetch(); const me = await res.json();
      me.prefs = { ...me.prefs, widgets };
      route.fulfill({ response: res, json: me });
    });
  }
  return { ctx, page, errs, calls, posts };
}
const noSideScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1);

// ── Phone ───────────────────────────────────────────────────────────────────────────────────
{
  const { ctx, page, errs, calls, posts } = await open({ width: 412, height: 915 }, true);
  await page.goto(BASE + '#/calendar'); await page.waitForTimeout(1500);
  ok((await page.locator('#screen-name').textContent()) === 'Calendar', 'the top bar names it');
  const cells = await page.locator('.cal-month .cal-day').count();
  ok(cells === 35 || cells === 42 || cells === 28, `a month grid of whole weeks (${cells} days)`);
  ok(await page.locator(`.cal-day.today[data-day="${today}"][aria-current="date"]`).count() === 1, 'today is marked');
  ok(calls.filter((c) => c === calls[0]).length === 1, `one call for the month shown (${calls.join(', ')})`);
  ok(calls.length === 3, `then the months either side, fetched ahead (${calls.length})`);
  const chips = await page.$$eval('.cal-legend .cal-chip', (els) => els.map((e) => e.textContent.trim()));
  ok(JSON.stringify(chips) === JSON.stringify(['Yours', 'Household', 'Béré']), `a switch per calendar, yours first: ${chips}`);
  ok(await page.locator('.cal-day.today .cal-dots .dot').count() >= 2, 'a phone month shows dots for the day\'s events');
  ok(await noSideScroll(page), 'nothing wider than the phone');
  const side = await page.locator('.cal-side').innerText();
  ok(side.includes('Call Maman') && side.includes('Rubbish bins'), 'today\'s events under the month');
  await page.screenshot({ path: `${OUT}/calendar-phone-month.png` });

  // A switch hides a calendar, and shows it again.
  await page.locator('.cal-chip', { hasText: 'Household' }).tap(); await page.waitForTimeout(150);
  ok(!(await page.locator('.cal-side').innerText()).includes('Rubbish bins') && (await page.locator('.cal-chip', { hasText: 'Household' }).getAttribute('aria-pressed')) === 'false', 'Household switched off');
  await page.locator('.cal-chip', { hasText: 'Household' }).tap(); await page.waitForTimeout(150);
  ok((await page.locator('.cal-side').innerText()).includes('Rubbish bins'), 'and on again');

  // Someone else's event, in full: seen, not changed.
  await page.locator('.cal-side .cal-event', { hasText: 'Call Maman' }).tap(); await page.waitForTimeout(300);
  const dlg = page.locator('.cal-panel');
  const dtext = await dlg.innerText();
  ok(dtext.includes('When') && dtext.includes('Béré') && /can see it, not change it/.test(dtext), 'an event in full, Béré\'s read only');
  ok(await dlg.locator('button', { hasText: 'Change' }).count() === 0, 'no Change on someone else\'s');
  await page.screenshot({ path: `${OUT}/calendar-phone-event.png` });
  await dlg.locator('button', { hasText: 'Close' }).tap(); await page.waitForTimeout(150);
  ok(await page.locator('.cal-panel').count() === 0, 'Close closes it');

  // A repeating one says where to change it.
  await page.locator('.cal-side .cal-event', { hasText: 'Rubbish bins' }).tap(); await page.waitForTimeout(250);
  ok(/repeating event/i.test(await page.locator('.cal-panel').innerText()) && await page.locator('.cal-panel button', { hasText: 'Change' }).count() === 0, 'a repeating event is changed on the phone');
  await page.keyboard.press('Escape'); await page.waitForTimeout(100);

  // Week: the days one under another.
  await page.locator('.cal-views button', { hasText: 'Week' }).tap(); await page.waitForTimeout(600);
  ok(await page.locator('.cal-week .cal-col').count() === 7, 'Week shows seven days');
  ok((await page.evaluate(() => location.hash)).startsWith('#/calendar/week/'), 'the address follows the view');
  const cols = await page.$$eval('.cal-week .cal-col', (els) => els.map((e) => Math.round(e.getBoundingClientRect().left)));
  ok(new Set(cols).size === 1, 'one day under another on a phone');
  ok(await noSideScroll(page), 'the week fits the phone');
  await page.screenshot({ path: `${OUT}/calendar-phone-week.png`, fullPage: true });

  // List: only the days with something on.
  await page.locator('.cal-views button', { hasText: 'List' }).tap(); await page.waitForTimeout(600);
  const listText = await page.locator('.cal-agenda').innerText();
  ok(listText.includes('Weekend in Bath') && listText.includes('Team dinner') && await page.locator('.cal-agenda .none').count() === 0, 'List: what is coming, day by day');

  // Add one: through your own agent, to your calendar.
  await page.locator('.cal-add').tap(); await page.waitForTimeout(300);
  ok(await page.locator('.cal-form').isVisible(), 'Add opens the form');
  await page.fill('#cal-f-title', 'Haircut');
  await page.fill('#cal-f-from', '16:00'); await page.fill('#cal-f-to', '16:45');
  await page.screenshot({ path: `${OUT}/calendar-phone-add.png` });
  await page.locator('.cal-form button[type=submit]').tap(); await page.waitForTimeout(600);
  ok(posts.some((p) => p.startsWith('/api/v2/calendar/add') && p.includes('"title":"Haircut"') && p.includes('"calendar":"personal"') && / 16:00"/.test(p) && / 16:45"/.test(p)),
    `added through the calendar service (${posts.join(' | ')})`);
  ok(await page.locator('.cal-panel').count() === 0 && /Haircut/.test(await page.locator('.cal-note').textContent()), 'the form closes and says so');

  // Your own event can be changed or cancelled.
  await page.goto(BASE + `#/calendar/day/${iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1))}`); await page.waitForTimeout(900);
  await page.locator('.cal-dayview .cal-event', { hasText: 'Dentist' }).tap(); await page.waitForTimeout(250);
  const mine = await page.locator('.cal-panel').innerText();
  ok(mine.includes('Change') && mine.includes('Cancel event') && mine.includes('Map') && mine.includes('Bring the form.'), 'your own event: where (as a Map link), notes, Change and Cancel');
  await page.locator('.cal-panel button', { hasText: 'Change' }).tap(); await page.waitForTimeout(250);
  ok((await page.inputValue('#cal-f-title')) === 'Dentist' && (await page.inputValue('#cal-f-from')) === '10:30', 'Change starts from the event as it is');
  await page.fill('#cal-f-from', '11:00'); await page.fill('#cal-f-to', '11:45');
  await page.locator('.cal-form button[type=submit]').tap(); await page.waitForTimeout(500);
  ok(posts.some((p) => p.startsWith('/api/v2/calendar/update') && p.includes('"id":"personal/dentist.ics"') && / 11:00"/.test(p)), 'the change is sent for that event');

  // Earlier and later: a call per new range, kept after that.
  await page.goto(BASE + '#/calendar/month/' + today); await page.waitForTimeout(800);
  const before = calls.slice(), t0 = await page.locator('.cal-title').textContent();
  await page.locator('.cal-step[aria-label="Later"]').tap(); await page.waitForTimeout(500);
  const shown = calls.slice(before.length);
  ok((await page.locator('.cal-title').textContent()) !== t0 && shown.length === 1 && !before.includes(shown[0]), `Later: the next month at once (fetched ahead), only the one after it asked for (${shown})`);
  await page.locator('.cal-step[aria-label="Earlier"]').tap(); await page.waitForTimeout(400);
  ok((await page.locator('.cal-title').textContent()) === t0, 'Earlier: back again');
  ok(errs.length === 0, `no page errors on the phone ${errs.join(' | ')}`);
  await ctx.close();
}

// ── Desktop ─────────────────────────────────────────────────────────────────────────────────
{
  const { ctx, page, errs, calls } = await open({ width: 1440, height: 900 }, false);
  await page.goto(BASE + '#/home'); await page.waitForTimeout(1200);
  const link = page.locator('.nav-links a[data-view="calendar"]');
  ok(await link.isVisible() && (await link.innerText()).trim() === 'Calendar', 'Calendar in the side menu');
  await link.click(); await page.waitForTimeout(1200);
  ok((await link.getAttribute('aria-current')) === 'page', 'and lit while open');
  ok(await page.locator('.page-band h1', { hasText: 'Calendar' }).isVisible(), 'its title in the band');
  const grid = await page.locator('.cal-month').boundingBox(), side = await page.locator('.cal-side').boundingBox();
  ok(grid && side && side.x > grid.x + grid.width - 2, 'the picked day sits beside the month');
  ok(await page.locator('.cal-day.today .cal-pill').count() >= 2, 'a desktop month shows the events by name');
  ok(await page.locator('.cal-goto').isVisible(), 'a date to go to');
  await page.screenshot({ path: `${OUT}/calendar-desk-month.png` });
  await page.locator('.cal-views button', { hasText: 'Week' }).click(); await page.waitForTimeout(600);
  const tops = await page.$$eval('.cal-week .cal-col', (els) => els.map((e) => Math.round(e.getBoundingClientRect().top)));
  ok(tops.length === 7 && new Set(tops).size === 1, 'a desktop week: seven columns side by side');
  await page.screenshot({ path: `${OUT}/calendar-desk-week.png` });
  await page.locator('.cal-views button', { hasText: 'Day' }).click(); await page.waitForTimeout(600);
  ok(await page.locator('.cal-dayview').isVisible(), 'Day');
  // Light theme too.
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light'; document.documentElement.dataset.mode = 'light'; });
  await page.locator('.cal-views button', { hasText: 'Month' }).click(); await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/calendar-desk-light.png` });
  ok(await noSideScroll(page), 'nothing wider than the window');
  ok(new Set(calls).size === calls.length, `one call per range (${calls.length})`);
  ok(errs.length === 0, `no page errors on the desktop ${errs.join(' | ')}`);
  await ctx.close();
}

// ── Home: the Month widget, and Today / Coming up opening the Calendar ───────────────────────────
{
  const { ctx, page, errs } = await open({ width: 412, height: 915 }, true, { widgets: ['month', 'calendar', 'coming'] });
  await page.goto(BASE + '#/home'); await page.waitForTimeout(1800);
  const w = page.locator('.home-month');
  ok(await w.isVisible(), 'the Month widget is on Home');
  ok(await w.locator(`.cal-mini-day.today[aria-current="date"]`).count() === 1 && (await w.innerText()).includes('Call Maman'), 'this month small, today marked, today\'s events under it');
  ok(await noSideScroll(page), 'Home still fits the phone');
  await page.screenshot({ path: `${OUT}/calendar-home-phone.png`, fullPage: true });
  ok((await page.locator('.home-coming h2 a').getAttribute('href')) === '#/calendar/list', 'Coming up\'s title opens the Calendar');
  await page.locator('.home-cal .home-row[data-href]', { hasText: 'Call Maman' }).tap(); await page.waitForTimeout(1300);
  ok((await page.evaluate(() => location.hash)).startsWith(`#/calendar/day/${today}`), 'an event on Home opens its day');
  ok(/Call Maman/.test(await page.locator('.cal-panel').innerText().catch(() => '')), 'with the event open');
  await page.keyboard.press('Escape');
  await page.goto(BASE + '#/home'); await page.waitForTimeout(1200);
  await page.locator('.home-cal .sub-label [data-href]', { hasText: 'Tomorrow' }).tap(); await page.waitForTimeout(900);
  ok(/#\/calendar\/day\/\d{4}-\d\d-\d\d$/.test(await page.evaluate(() => location.hash)) && await page.locator('.cal-dayview').isVisible(), 'Tomorrow opens that day');
  await page.goto(BASE + '#/home'); await page.waitForTimeout(1200);
  await page.locator('.home-coming .home-row[data-href]').first().tap(); await page.waitForTimeout(900);
  ok((await page.evaluate(() => location.hash)).startsWith('#/calendar/day/'), 'a Coming up line opens its day');
  await page.goto(BASE + '#/home'); await page.waitForTimeout(1200);
  await w.locator('.cal-mini-day.today').tap(); await page.waitForTimeout(900);
  ok((await page.evaluate(() => location.hash)) === `#/calendar/day/${today}`, 'a day in the Month widget opens that day');
  ok(errs.length === 0, `no page errors on Home ${errs.join(' | ')}`);
  await ctx.close();
}
{
  const { ctx, page, errs } = await open({ width: 1440, height: 900 }, false, { widgets: ['weather', 'month', 'calendar', 'coming'] });
  await page.goto(BASE + '#/home'); await page.waitForTimeout(1800);
  ok(await page.locator('.home-month .cal-mini').isVisible(), 'the Month widget on a desktop Home');
  await page.screenshot({ path: `${OUT}/calendar-home-desk.png` });
  ok(errs.length === 0, `no page errors on the desktop Home ${errs.join(' | ')}`);
  await ctx.close();
}
await browser.close();
