// From an agent chat you can switch to a group (Greg, 28 Nov: Household Group -> Greg's agent had no way back); agents first, then groups.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const b = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, phone] of [[1440, 900, false], [390, 844, true]]) {
  const p = await b.newPage({ viewport: { width: w, height: hgt }, isMobile: phone, hasTouch: phone });
  await p.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await p.waitForTimeout(2500);
  const btn = phone ? p.locator('.agent-bar-switch').first() : p.locator('.switch-btn').first();
  await btn.click(); await p.waitForTimeout(800);
  const names = await p.locator('.switch-pop:not([hidden]) .switch-item strong').allTextContents();
  const g = names.findIndex((n) => /Group/i.test(n)), lastAgent = names.findIndex((n) => /Household agent/i.test(n));
  ok(g > -1, `${w}: a group is offered from an agent chat (${names.join(', ')})`);
  ok(g > lastAgent, `${w}: agents first, then groups`);
  await p.locator('.switch-pop:not([hidden]) .switch-item', { hasText: /Group/ }).first().click(); await p.waitForTimeout(1200);
  ok(/group/.test(await p.evaluate(() => location.hash)), `${w}: picking it opens the group (${await p.evaluate(() => location.hash)})`);
  await p.close();
}
await b.close();
