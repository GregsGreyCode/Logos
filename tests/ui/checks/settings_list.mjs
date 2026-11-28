// Settings as a list, like Element X (F45, Greg 23 Nov).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });

// Phone: the list, then a row's own page with a way back.
let page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.goto('http://127.0.0.1:8779/#/settings'); await page.waitForTimeout(2500);
await page.screenshot({ path: `${OUT}/settings-phone.png`, fullPage: true });
const groups = await page.$$eval('.set-group-title', (els) => els.map((e) => e.textContent));
ok(JSON.stringify(groups) === JSON.stringify(['You', 'Agents', 'This device', 'Logos']), `grouped: ${groups.join(', ')}`);
ok(await page.locator('.set-profile', { hasText: 'Greg' }).count() === 1, 'your name and initial at the top');
const look = await page.locator('.set-row', { hasText: 'Look' }).locator('.set-value').textContent();
ok(/^(Glass|HUD|Warm) · (Dark|Light|Auto)$/.test(look), `each row says what it is set to (Look: ${look})`);
ok(await page.locator('.set-row', { hasText: 'Updates' }).locator('.set-value').textContent() === 'Up to date', 'Updates: up to date');
ok(await page.locator('.set-row.set-danger', { hasText: 'Sign out' }).count() === 1, 'Sign out at the end, in red');
ok(/^Logos abc1234 · .*\b2026\b/.test(await page.locator('.set-version').textContent()), 'and under it, which Logos this is (its date in the browser\'s own way)');
ok(await page.locator('.set-row', { hasText: 'People' }).isVisible(), 'People, Models and the Advisor are reachable from here on a phone');
const fits = await page.$$eval('.set-row, .set-profile', (els) => els.every((e) => e.getBoundingClientRect().right <= innerWidth));
ok(fits, 'every row fits the screen');
await page.locator('.set-row', { hasText: 'Phone alerts' }).tap(); await page.waitForTimeout(1200);
ok(page.url().endsWith('#/settings/notifications'), 'a row opens its page');
ok((await page.locator('#screen-name').textContent()) === 'Phone alerts', 'the top bar names it');
ok(await page.locator('.settings-body h2.label', { hasText: 'Phone alerts' }).count() === 0, 'and no second title under it');
await page.locator('.topbar .topbar-back').tap(); await page.waitForTimeout(1200);
ok(page.url().endsWith('#/settings') && await page.locator('.set-index').count() === 1, 'the back arrow in the top bar goes back to the list');
await page.close();

// Desktop: the list beside the page; the sidebar already has People and Models.
page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on('pageerror', (e) => errs.push(e.message));
await page.goto('http://127.0.0.1:8779/#/settings'); await page.waitForTimeout(2500);
ok(await page.locator('.settings-layout .set-row[aria-current="page"]', { hasText: 'Look' }).count() === 1, 'desktop opens on Look, marked in the list');
ok(await page.locator('.settings-body section[aria-label="Look"]').count() === 1, 'with its page beside the list');
ok(!(await page.locator('.settings-layout .set-row', { hasText: 'Models' }).isVisible()), 'People and Models only in the sidebar');
await page.locator('.set-row', { hasText: 'Tips' }).click(); await page.waitForTimeout(1000);
ok(await page.getByRole('button', { name: 'Show the tips again' }).count() === 1, 'Tips has its own page');
await page.goto('http://127.0.0.1:8779/#/settings/chat'); await page.waitForTimeout(1500);
ok(await page.locator('.settings-body section[aria-label="Look"]').count() === 1, 'an old link (#/settings/chat) still lands on Look');
await page.screenshot({ path: `${OUT}/settings-desktop.png` });
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
