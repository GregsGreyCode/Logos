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
await browser.close();
