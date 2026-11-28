import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
for (const [hash, want] of [['#/chat/greg', '#/chat/household'], ['#/agents/greg', '#/agents/household']]) {
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/` + hash); await page.waitForTimeout(2500);
  const btn = page.locator('.agent-bar .agent-bar-switch');
  ok(await btn.isVisible(), `${hash}: the agent's name is a switcher`);
  await btn.tap(); await page.waitForTimeout(600);
  ok(await page.locator('.agent-bar-pop').isVisible() && await page.locator('.agent-bar-pop .switch-item.on', { hasText: "Greg's agent" }).count() === 1, `${hash}: it lists the agents, this one ticked`);
  if (hash === '#/chat/greg') await page.screenshot({ path: process.argv[2] + '/phoneswitch.png' });
  await page.locator('.agent-bar-pop .switch-item', { hasText: 'Household agent' }).tap(); await page.waitForTimeout(1200);
  ok(await page.evaluate(() => location.hash) === want, `${hash}: picking one keeps you in the same place (${await page.evaluate(() => location.hash)})`);
}
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
