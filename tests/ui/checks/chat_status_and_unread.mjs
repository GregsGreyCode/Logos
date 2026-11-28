// A message the server has says "received" once the agent is working on it (not "sending…"), and a chat with a
// reply you have not seen comes first, with a dot (Greg, 23 Nov).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2500);
const lastMeta = () => page.locator('.mx-turn.me').last().locator('.mx-meta').textContent();

await page.evaluate(() => window.__mx.addMine('!dm:test', 'a photo on its way', 'sending')); await page.waitForTimeout(500);
ok(/sending…/.test(await lastMeta()), 'a message still leaving the phone says "sending…"');
await page.evaluate(() => window.__mx.addMine('!dm:test', 'my caption', 'sent')); await page.waitForTimeout(500);
ok(/received/.test(await lastMeta()) && !/sending/.test(await lastMeta()), 'the server has it and the agent is working on it: "received"');
ok(await page.locator('.mx-thinking:not([hidden])').count() === 1, '(the agent is shown thinking)');
await page.screenshot({ path: `${OUT}/received.png`, clip: { x: 0, y: 500, width: 412, height: 415 } });

// the list
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat`); await page.waitForTimeout(2500);
await page.evaluate(() => window.__mx.addMine('!dm:test', 'something I just wrote', null)); await page.waitForTimeout(600);
const names = await page.locator('.void-group.with-pic .void-group-name').allTextContents();
ok(names[0] === 'Household Group' && await page.locator('.void-group.with-pic').first().evaluate((e) => e.classList.contains('unread')),
  `the chat with a reply you have not seen is first, though another was written in later (${names.join(', ')})`);
ok(await page.locator('.void-group.with-pic').first().locator('.void-dot').count() === 1, 'with a dot on its picture');
ok(await page.locator('.void-group.with-pic').nth(1).locator('.void-dot').count() === 0, 'and none on the one with nothing new');
// the picture of an agent's chat and of a group line up: their text starts at the same place
const starts = await page.locator('.void-group.with-pic').evaluateAll((els) => els.map((e) => Math.round(e.querySelector('.void-group-body').getBoundingClientRect().left)));
ok(new Set(starts).size === 1, `every chat's text starts at the same place (${starts.join(', ')})`);
await page.screenshot({ path: `${OUT}/chats-unread.png`, fullPage: true });
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
