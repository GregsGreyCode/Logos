// The Brain, redesigned to match the rest (Greg, 24 Nov): the band is the title, the chat's agent switcher and the Chat | Brain |
// Routines pill; search and one "New page" button head the page list; the page has Edit (pencil) on the right and the "ask the
// agent" box docked at the bottom like the chat's; editing has Cancel and Save in the title row and a segmented toolbar.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.goto('http://127.0.0.1:8779/#/brain/greg'); await page.waitForTimeout(2500);
ok(await page.locator('.brain-head h1').innerText() === 'Brain' && await page.locator('.brain-head .agent-bar-switch').isVisible(), 'band: the title and the chat\'s agent switcher');
ok(await page.locator('.brain-head select, .brain-head input[type="search"]:visible').count() === 0, 'band: no dropdown and no search crowding it');
ok(await page.locator('.brain > .agent-bar .agent-menu > summary').isVisible(), 'band: the Chat | Brain | Routines pill');
await page.locator('.brain-head .agent-bar-switch').click(); await page.waitForTimeout(400);
const pop = await page.locator('.brain-head .agent-bar-pop:not([hidden])').boundingBox();
ok(pop && pop.x >= 216 && pop.width > 250, 'the switcher opens as a popover beside the name (not full screen)');
await page.keyboard.press('Escape'); await page.mouse.click(900, 500); await page.waitForTimeout(300);
ok(await page.locator('.tree .tree-top input[type="search"]').isVisible(), 'page list: search at the top');
ok(await page.locator('.brain-new').isVisible() && (await page.locator('.brain-new .icon').count()) === 1, 'page list: one "New page" button with a plus');
ok(await page.locator('.tree-add').count() === 0, 'page list: no + on every section');
await page.locator('.brain-new').click(); await page.waitForTimeout(400);
ok(await page.locator('.new-page:not([hidden])').isVisible() && !(await page.locator('.brain-new').isVisible()), 'New page opens the form in place');
await page.locator('.new-page .btn', { hasText: 'Cancel' }).click(); await page.waitForTimeout(300);
ok(await page.locator('.brain-new').isVisible(), 'Cancel brings the button back');
const edit = page.locator('.doc-head .btn', { hasText: 'Edit' });
ok(await edit.isVisible() && (await edit.locator('.icon').count()) === 1, 'page: Edit with a pencil, at the title row');
const ask = await page.locator('.doc .ask').boundingBox(), doc = await page.locator('.doc').boundingBox();
ok(ask.y + ask.height >= doc.y + doc.height - 40, `page: the ask box is docked at the bottom (${Math.round(ask.y + ask.height)} of ${Math.round(doc.y + doc.height)})`);
const send = await page.locator('.doc .ask-go').boundingBox();
ok(Math.abs(send.width - 44) <= 2 && Math.abs(send.height - 44) <= 2, `page: a round icon send button (${Math.round(send.width)}px)`);
await edit.click(); await page.waitForTimeout(700);
ok(await page.locator('.doc-head.editing .doc-actions .btn', { hasText: 'Save' }).isVisible() && await page.locator('.doc-head.editing .doc-actions .btn', { hasText: 'Cancel' }).isVisible(), 'editing: Cancel and Save in the title row');
ok(await page.locator('.toolbar .seg .tool').count() === 5, 'editing: the formatting buttons are one segmented control');
ok((await page.locator('.doc-head.editing h1').innerText()).startsWith('Editing') === false, 'editing: the page keeps its title (a small "Editing" tag says the state)');
await page.locator('.doc-head.editing .btn', { hasText: 'Cancel' }).click(); await page.waitForTimeout(500);
ok(await page.locator('.doc-head .btn', { hasText: 'Edit' }).isVisible(), 'Cancel returns to the page');
// the phone keeps a search in the band
const phone = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
await phone.goto('http://127.0.0.1:8779/#/brain/greg'); await phone.waitForTimeout(2500);
ok(await phone.locator('.brain-phone-search').isVisible(), 'phone: search stays at the top');
ok(errs.length === 0, `no page errors ${errs.slice(0, 2).join('; ')}`);
await browser.close();
