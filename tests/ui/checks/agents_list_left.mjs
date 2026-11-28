// The Agents page's chats list on the left (Greg, 28 Nov): with the full side menu (1100 px and up) the list is a column on
// the left, at the same edge and width as the chats list pinned inside a chat from 1400 px, so going into a chat it does not
// move; the agents float to its right. Narrower (the slim menu) and on a phone: agents, then chats, under them, as before.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const out = process.argv[2] || '.';
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}/`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const near = (a, b) => Math.abs(a - b) <= 1;
const box = (page, s) => page.locator(s).first().boundingBox();

for (const [w, hgt] of [[1440, 900], [1920, 1080]]) {
  const page = await (await browser.newContext({ viewport: { width: w, height: hgt } })).newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto(`${BASE}#/chat`); await page.waitForTimeout(2500);
  const list = await box(page, '.void-groups'), field = await box(page, '.void-field'), nav = await box(page, '.nav');
  ok(near(list.x, nav.x + nav.width) && list.y < 2 && near(list.height, hgt), `${w}: the chats list is a full-height column at the left, by the menu (${Math.round(list.x)}, ${Math.round(list.width)} wide)`);
  ok(field.x >= list.x + list.width, `${w}: the agents float to its right`);
  await page.screenshot({ path: `${out}/wide/agents-list-left-${w}.png` });
  // Into Greg's agent's chat from the list: its own chats list is pinned in the same place.
  await page.locator('.void-groups a.void-group', { hasText: "Greg's agent" }).click(); await page.waitForTimeout(2500);
  ok(/#\/chat\/greg/.test(page.url()), `${w}: a row opens its chat (${page.url().split('#')[1]})`);
  const side = await box(page, '#chat-side');
  ok(side && near(side.x, list.x) && near(side.width, list.width), `${w}: the chat's list has the same left edge and width (${side && Math.round(side.x)}/${Math.round(list.x)}, ${side && Math.round(side.width)}/${Math.round(list.width)}): it does not move`);
  const bg = await page.evaluate(() => getComputedStyle(document.querySelector('#chat-side')).backgroundColor);
  await page.goBack(); await page.waitForTimeout(2000);
  const back = await box(page, '.void-groups');
  ok(near(back.x, list.x) && near(back.width, list.width), `${w}: back on Agents, the list is where it was`);
  ok(await page.evaluate(() => getComputedStyle(document.querySelector('.void-groups')).backgroundColor) === bg, `${w}: and looks the same as the chat's (${bg})`);
  ok(errs.length === 0, `${w}: no page errors ${errs.join('|')}`);
  await page.context().close();
}

// Between the phone and 1400 px: from 1100 (the full menu) the same left column; the slim menu (600-1099) stacks as before.
for (const [w, left] of [[1280, true], [1024, false], [700, false]]) {
  const page = await browser.newPage({ viewport: { width: w, height: 800 } });
  await page.goto(`${BASE}#/chat`); await page.waitForTimeout(2200);
  const list = await box(page, '.void-groups'), field = await box(page, '.void-field');
  if (left) ok(field.x >= list.x + list.width && list.width === 300, `${w}: the chats list on the left, 300 px wide`);
  else ok(list.y >= field.y + field.height - 4, `${w}: the agents, then the chats under them`);
  await page.close();
}
const ph = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
await ph.goto(`${BASE}#/chat`); await ph.waitForTimeout(2200);
const list = await box(ph, '.void-groups'), field = await box(ph, '.void-field');
ok(list.y > field.y + 200 && list.x < 4 && list.width >= 400, 'phone: unchanged, the chats under the agents, across the screen');
await browser.close();
