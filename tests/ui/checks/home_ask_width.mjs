// Home's Ask fills the width of the page, as wide as the widgets above it (Greg, 23 Nov).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [900, 900, false, 'foldable'], [1440, 900, false, 'desktop'], [1920, 1080, false, 'large']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2200);
  const ask = await page.locator('form.home-ask').boundingBox();
  const board = await page.locator('.home-widgets').boundingBox();
  ok(Math.abs(ask.x - board.x) <= 2 && Math.abs((ask.x + ask.width) - (board.x + board.width)) <= 2, `${n}: Ask spans the same width as the widgets (${Math.round(ask.x)}–${Math.round(ask.x + ask.width)} vs ${Math.round(board.x)}–${Math.round(board.x + board.width)})`);
  const ta = await page.locator('#home-ask').boundingBox(), go = await page.locator('.home-ask .ask-go').boundingBox();
  ok(ta.x >= ask.x - 1 && go.x + go.width <= ask.x + ask.width + 1 && ta.width > ask.width * 0.5, `${n}: and its box and button fill that width (box ${Math.round(ta.width)} of ${Math.round(ask.width)})`);
  await page.screenshot({ path: `${OUT}/home-ask-${n}.png`, fullPage: false });
  await page.close();
}
await browser.close();
