// People › Add a person (F38): inviting someone, their page (what was set up, who can see what, removing,
// restoring), the admin actions, and the page the invited person opens.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const BASE = 'http://127.0.0.1:8779';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const errs = [];
const fits = (page) => page.evaluate(() => document.scrollingElement.scrollWidth <= innerWidth + 1);

// Phone: the list, inviting someone, the link to send.
let page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
page.on('pageerror', (e) => errs.push(e.message));
await page.goto(`${BASE}/#/people`); await page.waitForTimeout(2500);
ok(await page.locator('section[aria-label="Invited"] .set-row', { hasText: 'Cléo' }).count() === 1, 'someone invited is listed under Invited');
ok(/^Until /.test(await page.locator('section[aria-label="Invited"] .set-value').first().textContent()), 'with how long their link works');
ok(/Restorable until/.test(await page.locator('section[aria-label="Removed"] .set-row', { hasText: 'Dan' }).textContent()), 'someone removed: restorable until a date');
ok(await page.locator('section[aria-label="Admin actions"] .audit-row').count() === 3, 'the admin actions are listed');
const form = page.locator('form[aria-label="Add a person"]');
ok(!(await form.isVisible()), 'the form waits behind a button');
await page.getByRole('button', { name: 'Add a person' }).tap(); await page.waitForTimeout(300);
ok(await form.locator('.tick').count() === 4, 'chat, calendar, household and email can each be ticked');
await form.locator('#np-name').fill('Ella Martin');
ok(await form.locator('#np-user').inputValue() === 'ella', 'the username follows the name');
ok(await form.locator('input[type="password"]').count() === 0, 'no password is typed by the admin');
await form.getByRole('button', { name: 'Invite' }).tap(); await page.waitForTimeout(800);
const link = await form.locator('.invite-link').inputValue().catch(() => '');
ok(link === `${BASE}/invite#TESTTOKEN-not-a-secret`, `the link to send them, secret after # (${link})`);
ok(/shown only now/.test(await form.locator('.invite-box').textContent()), 'and that it is shown only once');
ok(await fits(page), 'nothing is wider than the phone');
await page.screenshot({ path: `${OUT}/people-phone.png`, fullPage: true });

// Their page: what was set up, who can see what, removing.
await page.goto(`${BASE}/#/people/ann`); await page.waitForTimeout(1500);
ok((await page.locator('.person-page h1').textContent()) === 'Ann', 'a person has their own page');
ok(await page.locator('.check.warn', { hasText: 'Household agent' }).count() === 1, 'a step that failed is marked');
ok(await page.getByRole('button', { name: 'Run the setup again' }).count() === 1, 'and the setup can be run again');
ok(await page.locator('section[aria-label="Who can see what is theirs"] .row-item').count() === 2, 'who can see what is theirs');
ok(/busy times only/.test(await page.locator('section[aria-label="Who can see what is theirs"]').textContent()), 'down to the household agent seeing busy times only');
ok(await page.locator('section[aria-label="Agents they use"] .row-item').count() === 2, 'the agents they use');
const rm = page.locator('section[aria-label="Remove"]');
ok(/30 days/.test(await rm.textContent()), 'removing says what is kept, and for how long');
await rm.getByRole('button', { name: 'Remove them' }).tap(); await page.waitForTimeout(300);
ok(/Type “ann” first/.test(await rm.locator('.result').textContent()), 'and needs their username typed first');
ok(await fits(page), 'their page fits the phone');
await page.screenshot({ path: `${OUT}/people-person-phone.png`, fullPage: true });
await page.locator('.topbar .topbar-back').tap(); await page.waitForTimeout(1000);
ok(page.url().endsWith('#/people'), '← People goes back');

await page.goto(`${BASE}/#/people/cleo`); await page.waitForTimeout(1500);
ok(await page.getByRole('button', { name: 'Make a new link' }).count() === 1 && await page.getByRole('button', { name: 'Cancel the invitation' }).count() === 1,
  'an open invitation: a new link, or cancel it');
await page.goto(`${BASE}/#/people/dan`); await page.waitForTimeout(1500);
ok(await page.getByRole('button', { name: 'Restore Dan' }).count() === 1, 'someone removed can be restored from their page');
await page.close();

// Desktop.
page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on('pageerror', (e) => errs.push(e.message));
await page.goto(`${BASE}/#/people`); await page.waitForTimeout(2500);
await page.getByRole('button', { name: 'Add a person' }).click(); await page.waitForTimeout(300);
ok(await page.locator('.people-add form[aria-label="Add a person"]').isVisible(), 'desktop: the form opens from its button');
await page.screenshot({ path: `${OUT}/people-desktop.png`, fullPage: true });
await page.goto(`${BASE}/#/people/ann`); await page.waitForTimeout(1500);
await page.screenshot({ path: `${OUT}/people-person-desktop.png`, fullPage: true });
await page.close();

// The invited person's page.
page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
page.on('pageerror', (e) => errs.push(e.message));
await page.goto(`${BASE}/invite#GOODTOKEN`); await page.waitForTimeout(1200);
ok(!page.url().includes('#'), 'the secret leaves the address bar at once');
ok((await page.locator('#hello').textContent()) === 'Welcome, Ella', 'it greets them by name');
ok(/@ella:test/.test(await page.locator('#also').textContent()), 'and says the password is also their chat password');
await page.locator('#pw').fill('a long password'); await page.locator('#pw2').fill('a long passw0rd');
await page.locator('#go').tap(); await page.waitForTimeout(300);
ok(/not the same/.test(await page.locator('#error').textContent()), 'two different passwords are caught');
await page.locator('#pw2').fill('a long password');
await page.locator('#go').tap(); await page.waitForTimeout(800);
ok(await page.locator('#done').isVisible() && await page.locator('#steps li').count() === 2, 'then: you are in, with what was set up');
ok(await page.locator('#pw').inputValue() === '', 'and the password is cleared from the page');
ok(await fits(page), 'the invitation page fits the phone');
await page.screenshot({ path: `${OUT}/invite-phone.png`, fullPage: true });
await page.goto("about:blank"); await page.goto(`${BASE}/invite#NOTVALID`); await page.waitForTimeout(1000);
ok(await page.locator('#gone').isVisible(), 'a used or wrong link says it cannot be used');
await page.close();

ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
