// People in one row style, and Sites the agents may reach as one row opening its own page (28 Nov): by agent,
// folded, a search, ten rows then "Show more", and Edit to remove a site, lift a Deny or add one by hand.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const errs = [];

for (const phone of [true, false]) {
  const n = phone ? 'phone' : 'desktop';
  const page = await browser.newPage(phone ? { viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true } : { viewport: { width: 1440, height: 900 } });
  const tap = (l) => (phone ? l.tap() : l.click());
  page.on('pageerror', (e) => errs.push(e.message));
  const sent = [];
  page.on('request', (r) => { if (r.method() === 'POST' && r.url().includes('/network-sites/')) sent.push(`${r.url().replace(/^https?:\/\/[^/]+/, '')} ${r.postData()}`); });

  // People: rows, no table; the form behind a button; Sites as one row.
  await page.goto(`${BASE}/#/people`); await page.waitForTimeout(2500);
  ok(await page.locator('#view table').count() === 0, `${n}: People: no table`);
  const people = page.locator('section[aria-label="People"] .set-row');
  ok(await people.count() === 2 && await people.locator('.set-icon').count() === 2 && await people.locator('.set-chevron').count() === 2, `${n}: each person a row: icon, name, chevron`);
  ok(/@greg · you/.test(await people.first().locator('.set-value').textContent()), `${n}: with a status line`);
  ok(await page.locator('section[aria-label="Invited"] .set-row.two-line').count() === 1, `${n}: invited people in the same row style`);
  ok(!(await page.locator('form[aria-label="Add a person"]').isVisible()) && await page.getByRole('button', { name: 'Add a person' }).isVisible(), `${n}: Add a person is a button`);
  ok(await page.locator('.network-sites').count() === 0, `${n}: the sites are no longer listed in full here`);
  const sitesRow = page.locator('section[aria-label="Sites"] .set-row');
  ok(/14 allowed, 1 denied/.test(await sitesRow.textContent()), `${n}: one row sums them up`);
  if (phone) {
    ok(await page.locator('.topbar .topbar-back[href="#/settings"]').isVisible(), `${n}: a back arrow to Settings`);
    await page.screenshot({ path: `${OUT}/people-rows-${n}.png`, fullPage: true });
  } else await page.screenshot({ path: `${OUT}/people-rows-${n}.png` });

  // Sites: its own page.
  await tap(sitesRow); await page.waitForTimeout(1800);
  ok(page.url().endsWith('#/settings/sites'), `${n}: the row opens the Sites page`);
  const greg = page.locator('details[data-agent="greg"]'), home = page.locator('details[data-agent="household"]');
  ok(await greg.count() === 1 && await home.count() === 1, `${n}: grouped by agent`);
  ok(/13 allowed, 1 denied/.test(await greg.locator('summary').textContent()), `${n}: each with its count, folded`);
  await tap(greg.locator('summary')); await page.waitForTimeout(200);
  ok(await greg.locator('.site-row').count() === 10, `${n}: ten rows, then…`);
  const more = greg.locator('.sites-more');
  ok(/Show 4 more/.test(await more.textContent()), `${n}: "Show 4 more"`);
  await tap(more); await page.waitForTimeout(200);
  ok(await greg.locator('.site-row').count() === 14, `${n}: then all of them`);
  ok(/Allowed once · until/.test(await greg.locator('.site-once').textContent()) && /Denied · until/.test(await greg.locator('.site-deny').textContent()), `${n}: allowed once and denied say until when`);
  await page.locator('.sites-tools input').fill('tracker'); await page.waitForTimeout(200);
  ok(await page.locator('.site-row').count() === 1 && /tracker\.example\.net/.test(await page.locator('.site-row').textContent()), `${n}: the search finds a site`);
  await page.locator('.sites-tools input').fill(''); await page.waitForTimeout(200);
  ok(await greg.locator('button', { hasText: 'Remove' }).count() === 0, `${n}: no buttons until Edit`);
  await tap(page.locator('.sites-edit')); await page.waitForTimeout(200);
  const edit = greg;
  ok(await edit.locator('button', { hasText: 'Remove' }).count() === 13 && await edit.locator('button', { hasText: 'Lift' }).count() === 1, `${n}: Edit: Remove on each allowed site, Lift on the deny`);
  if (phone) await page.screenshot({ path: `${OUT}/sites-edit-${n}.png`, fullPage: true });
  else await page.screenshot({ path: `${OUT}/sites-edit-${n}.png` });
  await tap(edit.locator('.site-row', { hasText: 'site01.example.org' }).locator('button')); await page.waitForTimeout(400);
  ok(sent.some((s) => s.startsWith('/api/v2/network-sites/greg/remove') && s.includes('site01.example.org')), `${n}: Remove takes it away`);
  await tap(edit.locator('.site-deny button')); await page.waitForTimeout(400);
  ok(sent.some((s) => s.startsWith('/api/v2/network-sites/greg/undeny') && s.includes('tracker.example.net')), `${n}: Lift ends the deny early`);
  const add = page.locator('details[data-agent="greg"] .site-add');
  await add.locator('input').fill('https://news.example.com/today'); await tap(add.getByRole('button', { name: 'Add' })); await page.waitForTimeout(400);
  ok(sent.some((s) => s.startsWith('/api/v2/network-sites/greg/add') && s.includes('"host":"news.example.com"') && s.includes('"port":443')), `${n}: Add sends the site's name alone`);
  // A refusal is said as the server says it.
  await page.route('**/network-sites/greg/add', (r) => r.fulfill({ status: 400, json: { error: 'refused', message: 'nas.local cannot be allowed: a name inside a home network.' } }));
  await add.locator('input').fill('nas.local'); await tap(add.getByRole('button', { name: 'Add' })); await page.waitForTimeout(400);
  ok(/cannot be allowed: a name inside a home network/.test(await page.locator('.settings-body [role=status]').last().textContent()), `${n}: a site that may not be allowed says why`);
  const fits = await page.evaluate(() => document.scrollingElement.scrollWidth <= innerWidth + 1);
  ok(fits, `${n}: nothing wider than the screen`);
  await page.close();
}

// Someone who is not an admin sees only their own agents' sites (the server lists those), and no Edit for others'.
{
  const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  page.on('pageerror', (e) => errs.push(e.message));
  await page.route('**/api/v2/network', (r) => r.fulfill({ json: { requests: [], is_admin: false, agents: [
    { name: 'household', display_name: 'Household agent', yours: true, people: ['greg', 'bere'], always: [{ host: 'weather.example.org', port: 443, by: 'bere' }], once: [], deny: [] }] } }));
  await page.goto(`${BASE}/#/settings/sites`); await page.waitForTimeout(1800);
  ok(await page.locator('details.site-agent').count() === 1 && await page.locator('details[open]').count() === 1, 'member: their one agent, unfolded');
  await page.close();
}
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
