// The tree logo is centred above Home and Agents in the sidebar on a wide screen (Greg, 23 Nov).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const w of [1440, 1100, 900]) {
  const page = await browser.newPage({ viewport: { width: w, height: 900 } });
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(1800);
  const logo = await page.locator('.nav .brand-row .logo').boundingBox();
  const nav = await page.locator('nav.nav').boundingBox();
  const home = await page.locator('.nav-links a', { hasText: 'Home' }).boundingBox();
  const mid = (b) => b.x + b.width / 2;
  ok(Math.abs(mid(logo) - mid(nav)) <= 1.5, `${w}px: the logo is centred in the sidebar (logo ${Math.round(mid(logo))}, sidebar ${Math.round(mid(nav))})`);
  ok(logo.y + logo.height <= home.y, `${w}px: and sits above Home`);
  if (w === 1440) await page.screenshot({ path: `${OUT}/logo-centered.png`, clip: { x: 0, y: 0, width: 420, height: 260 } });
  await page.close();
}
await browser.close();
