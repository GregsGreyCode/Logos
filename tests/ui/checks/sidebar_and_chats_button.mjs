import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt] of [[1440, 900], [1024, 768]]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt } });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2000);
  await page.mouse.wheel(0, 3000); await page.waitForTimeout(400);
  const so = await page.locator('.nav .logout-btn').boundingBox();
  ok(so && so.y + so.height <= hgt && so.y > hgt - 80, `${w}: Sign out snug at the bottom after scrolling (y=${so && Math.round(so.y)})`);
  ok((await page.locator('.nav .logo').boundingBox()).y >= 0, `${w}: the menu did not scroll away`);
  await page.close();
}
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(3000);
const btn = page.locator('.chat-head .side-reopen');
ok(await btn.isVisible() && (await btn.getAttribute('aria-label')) === 'Show or hide your chats', 'a chat: a chats icon in its header');
// From 1400 px the panel stays open on the left (Greg, 28 Nov): the icon hides it and brings it back.
ok(await page.locator('#chat-side').isVisible(), 'at 1440 the panel is open');
await btn.click(); await page.waitForTimeout(500);
ok(!(await page.locator('#chat-side').isVisible()), 'the icon hides it');
await btn.click(); await page.waitForTimeout(500);
ok(await page.locator('#chat-side').isVisible(), 'which opens the panel');
await page.screenshot({ path: process.argv[2] + '/desk/b2-chat.png' });
await browser.close();
