// The Lists screen (Greg, 28 Nov): your personal lists ("Yours", only you see them) and the shared ones, on a phone
// and a desktop. Reached from the menu (desktop), the account panel (phone) and Home's Lists label. Add, tick, edit,
// remove items; make a list; share one, make a shared one yours, delete one (each asked first); empty states.
// Made-up lists from serve.py (reset at the start of each size). Honours $PORT.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });

for (const phone of [true, false]) {
  const n = phone ? 'phone' : 'desktop';
  const page = await browser.newPage(phone ? { viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true } : { viewport: { width: 1440, height: 900 } });
  const tap = (l) => (phone ? l.tap() : l.click());
  const errs = [];
  page.on('pageerror', (e) => errs.push(e.message));
  await page.request.get(`${BASE}/api/v2/lists?reset=1`);
  const sec = (name) => page.locator(`.lists-sec[aria-label="${name}"]`);
  const card = (name) => page.locator(`.list-card[aria-label="${name}"]`);
  const texts = async (name) => card(name).locator('.list-items').first().locator('.list-text > span:first-child').allTextContents();
  const confirm = async () => { await page.locator('.ask-panel button[type="submit"]').click(); await page.waitForTimeout(700); };

  // How you get there
  await page.goto(`${BASE}/#/home`); await page.waitForTimeout(1800);
  ok(await page.locator('.home-lists h2 a.label-link[href="#/lists"]').count() === 1, `${n}: Home's Lists label opens the Lists screen`);
  if (phone) {
    await tap(page.locator('#acct-btn')); await page.waitForTimeout(400);
    const row = page.locator('#acct-sheet a.sheet-row[href="#/lists"]');
    ok(await row.isVisible() && (await row.textContent()).trim() === 'Lists', `${n}: Lists is in the account panel (the tab bar is full)`);
    ok(await page.locator('.tabbar a').count() === 4, `${n}: the tab bar keeps its four tabs`);
    await tap(row);
  } else {
    const link = page.locator('.nav-links a[href="#/lists"]');
    ok(await link.isVisible() && (await link.textContent()).trim() === 'Lists', `${n}: Lists is in the menu`);
    await link.click();
  }
  await page.waitForTimeout(1200);
  ok(page.url().endsWith('#/lists'), `${n}: it opens the Lists screen`);
  if (phone) ok((await page.locator('#screen-name').textContent()) === 'Lists', `${n}: the top bar says Lists`);
  else ok(await page.locator('.lists-page .page-band h1').textContent() === 'Lists', `${n}: the page is titled Lists`);

  // Yours and Shared
  ok(await sec('Yours').count() === 1 && await sec('Shared').count() === 1, `${n}: two sections, Yours and Shared`);
  ok(await sec('Yours').locator('.list-card').count() === 1 && await sec('Yours').locator('.list-card[aria-label="Video games"]').count() === 1, `${n}: your own list under Yours`);
  ok(await sec('Shared').locator('.list-card').count() === 2, `${n}: the two shared lists under Shared`);
  ok(JSON.stringify(await texts('Video games')) === '["Hollow Knight","Celeste"]', `${n}: open items, ticked ones folded away: ${await texts('Video games')}`);
  ok(/the farewell chapter/.test(await card('Video games').textContent()) && /Due 2026-12-01/.test(await card('Shopping list').textContent()), `${n}: details and due dates shown`);
  ok(/Ticked \(1\)/.test(await card('Video games').locator('.list-done summary').textContent()), `${n}: how many are ticked`);
  ok(await card('Video games').locator('.list-delete').count() === 1 && await card('Shopping list').locator('.list-delete').count() === 0, `${n}: only a personal list can be deleted`);
  const fits = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1);
  ok(fits, `${n}: it fits the screen`);
  await page.screenshot({ path: `${OUT}/lists-${n}.png`, fullPage: true });

  // Add, tick, edit, remove
  const add = card('Video games').locator('.list-add input');
  await add.fill('Outer Wilds'); await add.press('Enter'); await page.waitForTimeout(800);
  ok((await texts('Video games')).includes('Outer Wilds'), `${n}: an item is added`);
  await tap(card('Video games').getByRole('checkbox', { name: 'Tick off Hollow Knight' })); await page.waitForTimeout(800);
  ok(!(await texts('Video games')).includes('Hollow Knight') && /Ticked \(2\)/.test(await card('Video games').locator('.list-done summary').textContent()), `${n}: ticking it off moves it to Ticked`);
  await tap(card('Video games').locator('.list-done summary')); await page.waitForTimeout(300);
  await tap(card('Video games').getByRole('checkbox', { name: 'Put back Tetris' })); await page.waitForTimeout(800);
  ok((await texts('Video games')).includes('Tetris'), `${n}: a ticked item can be put back`);
  await tap(card('Video games').locator('.list-text', { hasText: 'Celeste' })); await page.waitForTimeout(300);
  const edit = card('Video games').locator('.list-edit input');
  ok(await edit.count() === 1, `${n}: tapping an item edits it in place`);
  await edit.fill('Celeste B-sides'); await card('Video games').locator('.list-edit button[type="submit"]').click(); await page.waitForTimeout(800);
  ok((await texts('Video games')).includes('Celeste B-sides'), `${n}: the new words are saved`);
  await tap(card('Shopping list').getByRole('button', { name: 'Remove Oat milk' })); await page.waitForTimeout(800);
  ok(!(await texts('Shopping list')).includes('Oat milk'), `${n}: an item is removed`);

  // A new list: yours by default
  await tap(page.locator('.lists-new')); await page.waitForTimeout(400);
  const dlg = page.locator('.ask-panel.lists-dialog');
  ok(await dlg.isVisible() && await dlg.locator('input[value="personal"]').isChecked(), `${n}: New list asks for a name, "Just you" chosen`);
  await dlg.locator('#list-new-name').fill('Books'); await dlg.locator('button[type="submit"]').click(); await page.waitForTimeout(900);
  ok(await sec('Yours').locator('.list-card[aria-label="Books"]').count() === 1 && /only you see it/.test(await page.locator('.lists-msg').textContent()), `${n}: it is made, under Yours`);

  // Share it, asked first
  await tap(card('Books').locator('.list-move')); await page.waitForTimeout(400);
  ok(/Share “Books”\?/.test(await page.locator('.ask-panel h2').textContent()), `${n}: sharing asks first`);
  await confirm();
  ok(await sec('Shared').locator('.list-card[aria-label="Books"]').count() === 1, `${n}: Books is shared now`);
  // A shared one made yours, asked first (in red)
  await tap(card('To do').locator('.list-move')); await page.waitForTimeout(400);
  ok(await page.locator('.ask-panel button.danger[type="submit"]').count() === 1, `${n}: making a shared list yours asks first, in red`);
  await confirm();
  ok(await sec('Yours').locator('.list-card[aria-label="To do"]').count() === 1 && /still in Home Assistant/.test(await page.locator('.lists-msg').textContent()),
    `${n}: To do is yours, and it says the shared copy is still in Home Assistant`);
  // Cancel keeps it; Delete asks, then deletes
  await tap(card('Video games').locator('.list-delete')); await page.waitForTimeout(400);
  await page.locator('.ask-panel button.quiet').click(); await page.waitForTimeout(400);
  ok(await card('Video games').count() === 1, `${n}: Cancel keeps the list`);
  await tap(card('Video games').locator('.list-delete')); await page.waitForTimeout(400);
  await confirm();
  ok(await card('Video games').count() === 0, `${n}: Delete deletes it`);

  // A refusal is said in plain words
  await page.route('**/api/v2/lists/add', (r) => r.fulfill({ status: 409, json: { error: 'refused', message: 'Home Assistant is not answering, so nothing was changed.' } }));
  const add2 = card('Shopping list').locator('.list-add input');
  await add2.fill('Eggs'); await add2.press('Enter'); await page.waitForTimeout(800);
  ok(/not answering/.test(await page.locator('.lists-msg').textContent()), `${n}: a refusal is shown in plain words`);
  await page.unroute('**/api/v2/lists/add');

  // Empty states
  await page.route('**/api/v2/lists', (r) => r.fulfill({ json: { personal: true, lists: [] } }));
  await page.goto(`${BASE}/#/home`); await page.goto(`${BASE}/#/lists`); await page.waitForTimeout(1200);
  ok(await sec('Yours').locator('.empty-state').count() === 1 && await sec('Shared').locator('.empty-state').count() === 1, `${n}: empty states when there are no lists`);
  ok(await sec('Yours').locator('.empty-state .btn').count() === 1, `${n}: with a way to make one`);
  await page.screenshot({ path: `${OUT}/lists-empty-${n}.png`, fullPage: true });
  await page.unroute('**/api/v2/lists');
  await page.route('**/api/v2/lists', (r) => r.fulfill({ json: { personal: false, lists: [{ name: 'Shopping list', kind: 'shared', items: [] }] } }));
  await page.reload(); await page.waitForTimeout(1200);
  ok(/No personal lists/.test(await sec('Yours').textContent()) && await page.locator('.list-move').count() === 0, `${n}: without your own agent, no personal lists and nothing to move`);
  await page.unroute('**/api/v2/lists');
  await page.route('**/api/v2/lists', (r) => r.fulfill({ json: { error: 'house_unavailable', message: 'The house service did not answer.', personal: true, lists: [] } }));
  await page.reload(); await page.waitForTimeout(1200);
  ok(/could not be loaded/.test(await page.locator('.lists-body').textContent()) && await page.locator('.lists-body .empty-state button').count() === 1, `${n}: when it cannot load, it says so, with Try again`);
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
