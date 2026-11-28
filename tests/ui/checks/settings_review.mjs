// Settings after the Home Assistant review (28 Nov): "Needs attention" only when something is wrong, one
// sub-page header, empty states that know who is looking, search (and Ctrl+K), and the Agents group.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const BASE = 'http://127.0.0.1:8779';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const errs = [];
const PHONE = { viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true };
const DESK = { viewport: { width: 1440, height: 900 } };

// Something wrong: two updates waiting, a mailbox not refreshing, Greg's agent not running.
async function broken(page) {
  await page.route('**/api/v2/updates?check=1', (r) => r.fulfill({ json: { plan: { commits: [{ sha: 'a' }, { sha: 'b' }] } } }));
  await page.route('**/api/v2/mail', (r) => r.fulfill({ json: { agent: 'greg', mailboxes: [{ id: 'b1', kind: 'gmail', address: 'me@example.org', exclude: [], health: { error: 'login refused' } }] } }));
  await page.route('**/api/v2/agents', (r) => r.fulfill({ json: { agents: [
    { name: 'household', display_name: 'Household agent', running: true, model_up: true, model_loaded: true, shared: true },
    { name: 'greg', display_name: "Greg's agent", running: false, model_up: true, shared: false }], others: [] } }));
}

for (const phone of [true, false]) {
  const n = phone ? 'phone' : 'desktop';
  const ctx = await browser.newContext(phone ? PHONE : DESK);
  const page = await ctx.newPage();
  const tap = (l) => (phone ? l.tap() : l.click());
  page.on('pageerror', (e) => errs.push(e.message));

  // 1. Nothing wrong: no card.
  await page.goto(`${BASE}/#/settings`); await page.waitForTimeout(2500);
  ok(await page.locator('.set-attention').isHidden(), `${n}: nothing wrong, no "Needs attention" card`);

  // ... something wrong: one line each, each to its page.
  await broken(page);
  await page.goto(`${BASE}/#/settings/tips`); await page.goto(`${BASE}/#/settings`); await page.waitForTimeout(2500);
  const card = page.locator('.set-attention');
  ok(await card.isVisible(), `${n}: something wrong: "Needs attention" at the top`);
  const lines = await card.locator('.set-attention-link').allTextContents();
  ok(lines.length === 3 && lines.some((l) => /2 updates waiting/.test(l)) && lines.some((l) => /me@example.org is not refreshing/.test(l))
    && lines.some((l) => /Greg's agent is not running/.test(l)), `${n}: one line each (${lines.join(' | ')})`);
  ok(await card.locator('a[href="#/settings/updates"]').count() === 1 && await card.locator('a[href="#/agents/greg/settings"]').count() === 1, `${n}: each line links to its page`);
  ok(await page.locator('.set-row', { hasText: 'Email' }).locator('.set-value.attention').count() === 1, `${n}: the row keeps its tint`);
  if (phone) await page.screenshot({ path: `${OUT}/settings-attention-${n}.png`, fullPage: true });
  else await page.screenshot({ path: `${OUT}/settings-attention-${n}.png` });
  await tap(card.locator('.set-attention-row', { hasText: 'updates waiting' }).locator('.set-dismiss')); await page.waitForTimeout(200);
  ok(await card.locator('.set-attention-row').count() === 2, `${n}: Dismiss hides that line`);
  await page.reload(); await page.waitForTimeout(2500);
  ok(await page.locator('.set-attention .set-attention-row').count() === 2, `${n}: and it stays hidden (for a day)`);
  await page.evaluate(() => localStorage.setItem('logos.settings.dismissed', JSON.stringify({ updates: Date.now() - 90000000 })));
  await page.reload(); await page.waitForTimeout(2500);
  ok(await page.locator('.set-attention .set-attention-row').count() === 3, `${n}: a day later it is back`);
  await page.unrouteAll({ behavior: 'ignoreErrors' });

  // 6. The Agents group: one row per agent, in the Agents screen's words, to its settings.
  await page.goto(`${BASE}/#/settings/tips`); await page.goto(`${BASE}/#/settings`); await page.waitForTimeout(2500);
  const agents = page.locator('section[aria-label="Agents"]');
  const arows = await agents.locator('.set-agent').allTextContents();
  ok(arows.length === 2 && arows.every((t) => /Asleep|Awake|Unplugged|Not running|Thinking|Waiting on you|Waking up/.test(t)), `${n}: an Agents group, each with its state (${arows.join(' | ')})`);
  ok(await agents.locator('a[href="#/agents/greg/settings"]').count() === 1, `${n}: each opens that agent's settings`);
  ok(/14 allowed, 1 denied/.test(await agents.locator('.set-sites').textContent()), `${n}: and Sites, summed up in one row`);

  // 4. Search: by name and by value.
  const search = page.locator('.set-search input');
  await search.fill('dark'); await page.waitForTimeout(200);
  let visible = await page.$$eval('.set-index .set-row', (els) => els.filter((e) => !e.hidden && e.offsetParent).map((e) => e.querySelector('.set-name').textContent));
  ok(JSON.stringify(visible) === JSON.stringify(['Look']), `${n}: search finds a row by its value ("dark": ${visible})`);
  await search.fill('email'); await page.waitForTimeout(200);
  visible = await page.$$eval('.set-index .set-row', (els) => els.filter((e) => !e.hidden && e.offsetParent).map((e) => e.querySelector('.set-name').textContent));
  ok(JSON.stringify(visible) === JSON.stringify(['Email']), `${n}: and by its name (${visible})`);
  ok(await page.locator('section[aria-label="This device"]').isHidden(), `${n}: groups with nothing found fold away`);
  await search.fill('zzz'); await page.waitForTimeout(200);
  ok(await page.locator('.set-nomatch').isVisible(), `${n}: nothing found says so`);
  await search.fill('email'); await search.press('Enter'); await page.waitForTimeout(1200);
  ok(page.url().endsWith('#/settings/email'), `${n}: Enter opens the first one found`);

  // 2. One sub-page header.
  if (phone) {
    ok(await page.locator('.topbar .topbar-back').isVisible() && !(await page.locator('.topbar .logo-link').isVisible()), `${n}: the back arrow takes the logo's place in the top bar`);
    ok((await page.locator('#screen-name').textContent()) === 'Email', `${n}: beside the page's name`);
    ok(await page.locator('.settings-body h2.label', { hasText: /^Email$/ }).count() === 0 && !(await page.locator('.subpage-head').isVisible()), `${n}: and nothing repeated under it`);
    await page.screenshot({ path: `${OUT}/settings-subpage-${n}.png` });
    await page.locator('.topbar .topbar-back').tap(); await page.waitForTimeout(1200);
    ok(page.url().endsWith('#/settings') && await page.locator('.topbar .logo-link').isVisible(), `${n}: back on the list, the logo is back`);
  } else {
    ok((await page.locator('.set-crumbs h1').textContent()).replace(/\s+/g, ' ') === 'Settings › Email', `${n}: Settings › Email heads the page`);
    ok(await page.locator('.settings-body h2.label', { hasText: /^Email$/ }).count() === 0, `${n}: and nothing repeated under it`);
    // Ctrl+K from anywhere opens the search.
    await page.goto(`${BASE}/#/home`); await page.waitForTimeout(1500);
    await page.keyboard.press('Control+k'); await page.waitForTimeout(1800);
    ok(page.url().includes('#/settings') && await page.evaluate(() => document.activeElement && document.activeElement.closest('.set-search') !== null), `${n}: Ctrl+K opens the Settings search`);
  }
  await ctx.close();
}

// 3. Empty states that know who is looking: whoever manages Logos gets the action, anyone else whom to ask.
for (const role of ['admin', 'user']) {
  const ctx = await browser.newContext(PHONE);
  const page = await ctx.newPage();
  page.on('pageerror', (e) => errs.push(e.message));
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const j = await res.json(); r.fulfill({ json: { ...j, role, admins: ['Greg'] } }); });
  await page.route('**/api/v2/mail', (r) => r.fulfill({ json: { agent: null, mailboxes: [] } }));
  await page.route('**/api/v2/speaker', async (r) => { const res = await r.fetch(); const j = await res.json(); r.fulfill({ json: { ...j, on: false, problem: '' } }); });
  await page.goto(`${BASE}/#/settings/email`); await page.waitForTimeout(1800);
  const es = page.locator('.empty-state');
  ok(/does not read email yet/.test(await es.textContent()), `${role}: Email: an empty state says what is going on`);
  if (role === 'admin') ok(await es.getByRole('link', { name: 'Set it up' }).count() === 1 && await es.locator('.empty-ask').count() === 0, 'admin: gets "Set it up"');
  else ok((await es.locator('.empty-ask').textContent()) === 'Ask Greg to turn it on.' && await es.locator('.btn').count() === 0, `member: gets "Ask Greg" (${await es.locator('.empty-ask').textContent().catch(() => '')})`);
  await page.screenshot({ path: `${OUT}/empty-email-${role}.png` });
  await page.goto(`${BASE}/#/settings/speaker`); await page.waitForTimeout(1800);
  const sp = page.locator('.speaker-top .empty-state');
  ok(/speaker voices are off/.test(await sp.textContent()), `${role}: Speaker off: said once, at the top`);
  ok(role === 'admin' ? await sp.getByRole('button', { name: 'How to turn them on' }).count() === 1 : /Ask Greg to switch them on/.test(await sp.textContent()), `${role}: with the action that suits`);
  ok(await page.locator('.speaker-agent').count() === 2, `${role}: and the voices can still be chosen`);
  await ctx.close();
}

// Metrics (admin only): off is an empty state with Turn on.
{
  const page = await browser.newPage(DESK);
  page.on('pageerror', (e) => errs.push(e.message));
  await page.route('**/api/v2/metrics', (r) => r.fulfill({ json: { enabled: false } }));
  await page.goto(`${BASE}/#/settings/metrics`); await page.waitForTimeout(1800);
  ok(/Metrics are off/.test(await page.locator('.empty-state').textContent()) && await page.locator('.empty-state').getByRole('button', { name: 'Turn on' }).count() === 1, 'Metrics off: an empty state with Turn on');
  await page.close();
}
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
