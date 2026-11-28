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
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/brain/greg`); await page.waitForTimeout(2500);
// The band is the one agent bar (UI review, 28 Nov): ‹, the agent's name switch, and the pill saying Brain.
ok(await page.locator('.brain > .agent-bar .agent-bar-switch').isVisible() && (await page.locator('.brain > .agent-bar .agent-menu > summary').innerText()).startsWith('Brain'), 'band: the chat\'s agent switcher and a pill saying Brain');
ok(await page.locator('.brain > .agent-bar select, .brain > .agent-bar input[type="search"]:visible, .brain-head:visible').count() === 0, 'band: no dropdown and no search crowding it');
ok(await page.locator('.brain > .agent-bar .agent-menu > summary').isVisible(), 'band: the Chat | Brain | Routines pill');
await page.locator('.brain > .agent-bar .agent-bar-switch').click(); await page.waitForTimeout(400);
const pop = await page.locator('.brain > .agent-bar .agent-bar-pop:not([hidden])').boundingBox();
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
await phone.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/brain/greg`); await phone.waitForTimeout(2500);
ok(await phone.locator('.brain-phone-search').isVisible(), 'phone: search stays at the top');
// The phone Brain has the desktop's structure (UI review, 28 Nov; at 390 the picker, search and ask box were doubled or cut).
const p390 = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
p390.on('pageerror', (e) => errs.push(e.message));
await p390.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/brain/greg`); await p390.waitForTimeout(2500);
ok(await p390.locator('.agent-bar-switch:visible').count() === 1, `390: one agent picker (${await p390.locator('.agent-bar-switch:visible').count()})`);
const searches = p390.locator('input[type="search"]:visible');
ok(await searches.count() === 1, `390: one search box (${await searches.count()})`);
ok(await searches.first().evaluate((el) => getComputedStyle(el).borderRadius !== '0px' && parseFloat(getComputedStyle(el).height) >= 40), '390: the search is the styled one');
const vh = 844, tabs = await p390.locator('.tabbar').boundingBox();
const a390 = await p390.locator('.doc .ask').boundingBox(), h390 = await p390.locator('.doc .doc-head').boundingBox();
ok(a390 && h390 && a390.y > h390.y + h390.height + 100, '390: the ask box is below the page, not above it');
ok(a390 && Math.abs(a390.y + a390.height - (tabs ? tabs.y : vh)) <= 4, `390: the ask box is docked just above the tabs (${Math.round(a390.y + a390.height)} vs ${Math.round(tabs ? tabs.y : vh)})`);
const ta = p390.locator('.doc .ask textarea');
ok(await ta.evaluate((el) => el.clientHeight <= 52 && getComputedStyle(el, '::placeholder').whiteSpace === 'nowrap'), '390: the ask hint stays on one line');
const send390 = await p390.locator('.doc .ask-go').boundingBox(), taBox = await ta.boundingBox();
ok(send390.x >= taBox.x + taBox.width && send390.x + send390.width <= 390 - 12, '390: the send button sits beside the box, inside the edge');
await p390.screenshot({ path: process.argv[2] + '/brain-390.png' });
ok(errs.length === 0, `no page errors ${errs.slice(0, 2).join('; ')}`);
await browser.close();
