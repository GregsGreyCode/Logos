// The desktop menu (Greg, 23 Nov): no label is cut ("Notificatio…" in the Terminal HUD theme), in any of the three looks.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto('http://127.0.0.1:8779/#/noticeboard'); await page.waitForTimeout(4000);
for (const look of ['glass', 'hud', 'warm']) {
  await page.evaluate((l) => { document.documentElement.dataset.look = l; }, look); await page.waitForTimeout(400);
  const cut = await page.evaluate(() => [...document.querySelectorAll('.nav-links a .nav-label')].filter((e) => e.offsetParent && e.scrollWidth > e.clientWidth + 1).map((e) => e.textContent));
  ok(cut.length === 0, `${look}: every menu label fits${cut.length ? ' (cut: ' + cut.join(', ') + ')' : ''}`);
}
await browser.close();
