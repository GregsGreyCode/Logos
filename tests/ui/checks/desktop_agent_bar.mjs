// Desktop agent chat (Greg, 23 Nov): the phone's header (‹ back, name switcher, Chat | Brain | Routines), a solid switcher
// that no bubble draws over, long messages that open by tapping them (no "Show all"), and Beta on People, Models, Advisor.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto('http://127.0.0.1:8779/#/chat'); await page.waitForTimeout(2500);
const href = await page.locator('.void-field .agent-mark[data-href]').first().getAttribute('data-href');
await page.evaluate((h) => { location.hash = h; }, href); await page.waitForTimeout(2500);
const back = page.locator('.chat-head .agent-back');
ok(await back.isVisible(), 'desktop: a back button in the chat header');
await page.locator('.chat-head .agent-menu > summary').click();
const items = await page.locator('.chat-head .agent-menu-list a').allInnerTexts();
ok(items.map((t) => t.replace(/[↗\s]+$/, '')).join('|') === 'Chat|Brain|Routines', `desktop: the Chat pill opens to ${items.join(', ')}`);
ok((await page.locator('.chat-head .agent-menu-list a[href^="#/brain"]').count()) === 1, 'desktop: Brain opens the reader in Logos (no Obsidian app)');
await page.locator('.chat-head .agent-menu > summary').click();
ok(!(await page.locator('.chat-head .side-reopen').innerText()).includes('Chats'), 'desktop: no "Chats" word button: a small icon');
await page.locator('.chat-head .switch-btn').click(); await page.waitForTimeout(500);
const pop = await page.locator('.switch-pop:not([hidden])').boundingBox();
const covered = await page.evaluate(([x, y]) => !document.elementFromPoint(x, y)?.closest('.switch-pop'), [pop.x + pop.width - 12, pop.y + pop.height - 12]);
ok(!covered, 'desktop: the agent switcher is solid: no message bubble is drawn over it');
await page.keyboard.press('Escape'); await page.mouse.click(900, 600); await page.waitForTimeout(300);
ok(await page.getByText('Show all', { exact: true }).count() === 0, 'a long message has no "Show all" words');
const long = page.locator('.msg.foldable').first();
if (await long.count()) {
  const before = (await long.boundingBox()).height; await long.click({ position: { x: 40, y: 20 } }); await page.waitForTimeout(500);
  const after = (await page.locator('.msg.foldable').first().boundingBox()).height;
  ok(after > before + 40, `a tap on the long message opens it (${Math.round(before)} to ${Math.round(after)}px)`);
} else ok(false, 'a long message is foldable');
await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(1500);
ok(await page.locator('.nav-links .beta-tag').count() === 3, 'Beta beside People, Models and Claude Advisor in the menu');
await browser.close();
