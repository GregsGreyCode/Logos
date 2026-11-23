// A fuller To-do (Lists) widget, chosen in Edit, each person's own (Greg, 23 Nov): his stays compact, Béré's full.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const TEXT = `# Shared lists
## Shopping list
<!-- todo.shopping -->
- [ ] pasta
- [ ] lemon
- [ ] something sweet
- [ ] oat milk (due 2026-11-25)
## To-do
<!-- todo.todo -->
${Array.from({ length: 14 }, (_, i) => `- [ ] Job number ${i + 1}${i === 0 ? ' (due 2026-11-24)' : ''}`).join('\n')}
## Done already
<!-- todo.done -->
`;

for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  let put = null, saved = null;
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs.widgets = ['lists', 'weather']; me.prefs.widgets_v2 = true; if (saved) me.prefs.widget_sizes = saved; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/agents/household/brain/page*', (r) => r.fulfill({ json: { path: 'lists.md', title: 'Shared lists', text: TEXT } }));
  await page.route('**/api/v2/me/prefs', (r) => { if (r.request().method() === 'PUT') { put = r.request().postDataJSON(); return r.fulfill({ json: { prefs: put } }); } return r.continue(); });
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2500);

  ok(await page.locator('.home-lists-row .home-list-card:not(.full)').count() === 2 && await page.locator('.home-list-card.full').count() === 0,
    `${n}: by default it is the compact tiles, as Greg has it (a tile per list, empty ones left out)`);
  ok(/pasta · lemon · something sweet · \+1/.test(await page.locator('.home-list-items').first().textContent()), `${n}: with the first three things`);

  // Edit: the switch is in the widget's bar
  const pencil = page.locator('.home-edit-link');
  await pencil.evaluate((b) => b.click()); await page.waitForTimeout(400);
  const bar = page.locator('section[data-widget="lists"] .widget-size');
  ok(await bar.count() === 1 && await bar.locator('button.on').textContent() === 'Compact', `${n}: Edit has Compact / Full on Lists, Compact chosen`);
  ok(await page.locator('section[data-widget="weather"] .widget-size').count() === 0, `${n}: and only on a widget that has sizes`);
  await bar.getByRole('button', { name: 'Full' }).click(); await page.waitForTimeout(600);
  ok(put && put.widget_sizes && put.widget_sizes.lists === 'full', `${n}: choosing Full is kept with your login`);
  ok(await page.locator('.home-list-card.full').count() === 2, `${n}: and the lists redraw as cards at once`);
  const items = await page.locator('.home-list-card.full').nth(1).locator('li').count();
  ok(items === 12 && /\+2 more/.test(await page.locator('.home-list-card.full').nth(1).textContent()), `${n}: each shows its things (up to 12, then how many more): ${items}`);
  ok(/2026-11-25/.test(await page.locator('.home-list-card.full').first().textContent()) && await page.locator('.home-list-due').count() === 2, `${n}: with when each is due`);
  const fits = await page.$$eval('.home-list-card.full', (els) => els.every((e) => e.getBoundingClientRect().right <= innerWidth + 0.5));
  ok(fits, `${n}: it fits the screen`);
  await page.screenshot({ path: `${OUT}/lists-full-${n}.png`, fullPage: true });
  await bar.getByRole('button', { name: 'Compact' }).click(); await page.waitForTimeout(500);
  ok(await page.locator('.home-list-card.full').count() === 0 && put.widget_sizes.lists === 'compact', `${n}: and back to Compact`);

  // Someone whose choice is already Full opens Home on it
  saved = { lists: 'full' };
  await page.reload(); await page.waitForTimeout(2500);
  ok(await page.locator('.home-list-card.full').count() === 2, `${n}: Béré's Full is how Home opens for her`);
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await page.close();
}
// ── ticking items off, live from Home Assistant (Greg, 23 Nov) ──
{
  const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  const ticks = []; let failNext = false;
  const live = { lists: [{ name: 'Shopping list', id: 'todo.shopping', items: [{ text: 'pasta', due: '' }, { text: 'lemon', due: '' }, { text: 'oat milk', due: '2026-11-25' }] },
                         { name: 'To-do', id: 'todo.todo', items: [{ text: 'Call the dentist', due: '' }] }] };
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs.widgets = ['lists']; me.prefs.widgets_v2 = true; me.prefs.widget_sizes = { lists: 'full' }; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/home/lists', (r) => r.fulfill({ json: live }));
  await page.route('**/api/v2/home/lists/tick', (r) => {
    const b = r.request().postDataJSON(); ticks.push(b);
    if (failNext) { failNext = false; return r.fulfill({ status: 409, json: { error: 'refused', message: "'pasta' could be pasta, pasta sauce. Say which." } }); }
    if (b.done) live.lists[0].items = live.lists[0].items.filter((i) => i.text !== b.item); else live.lists[0].items.unshift({ text: b.item, due: '' });
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2500);
  ok(await page.locator('.home-list-card.full button.home-list-box.tick').count() === 4, 'live: every item has a box to tick');
  const box = page.locator('.home-list-card.full').first().locator('li', { hasText: 'pasta' }).locator('button.tick');
  failNext = true;
  await box.click(); await page.waitForTimeout(600);
  ok(/Could not tick it off: 'pasta' could be pasta, pasta sauce/.test(await page.locator('.home-lists-note').textContent()) && await box.getAttribute('aria-checked') === 'false',
    'if Home Assistant says no, the item comes back with the reason in plain words');
  await box.click(); await page.waitForTimeout(100);
  ok(await page.locator('li.ticking', { hasText: 'pasta' }).count() === 1, 'a tap strikes it through at once');
  await page.waitForTimeout(1200);
  ok(ticks.at(-1).list === 'Shopping list' && ticks.at(-1).item === 'pasta' && ticks.at(-1).done === true, 'and asks Home Assistant to tick that item on that list');
  ok(await page.locator('.home-list-card.full').first().locator('li').count() === 2 && /^2$/.test(await page.locator('.home-list-count').first().textContent()), 'then it leaves the list, and the count goes down');
  ok(/Ticked off .pasta./.test(await page.locator('.home-lists-note').textContent()) && await page.locator('.home-lists-note button', { hasText: 'Undo' }).count() === 1, 'with Undo for a few seconds');
  await page.screenshot({ path: `${OUT}/lists-tick-phone.png`, fullPage: true });
  await page.locator('.home-lists-note button', { hasText: 'Undo' }).click(); await page.waitForTimeout(1200);
  ok(ticks.at(-1).done === false && await page.locator('.home-list-card.full').first().locator('li', { hasText: 'pasta' }).count() === 1, 'Undo puts it back on the list');
  ok(errs.length === 0, `no page errors ${errs.join('|')}`);
  await page.close();
}
// and when Home Assistant cannot be reached the copy still shows, to read only
{
  const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs.widgets = ['lists']; me.prefs.widgets_v2 = true; me.prefs.widget_sizes = { lists: 'full' }; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/home/lists', (r) => r.fulfill({ json: { error: 'house_unavailable', message: 'down' } }));
  await page.route('**/api/v2/agents/household/brain/page*', (r) => r.fulfill({ json: { path: 'lists.md', title: 'Shared lists', text: TEXT } }));
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2500);
  ok(await page.locator('.home-list-card.full').count() === 2 && await page.locator('button.home-list-box.tick').count() === 0, 'Home Assistant unreachable: the saved copy shows, with no boxes to tick');
  await page.close();
}
await browser.close();
