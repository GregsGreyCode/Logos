import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const w of [412, 360]) {
  const page = await browser.newPage({ viewport: { width: w, height: 900 }, isMobile: true, hasTouch: true });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(1800);
  const r = await page.evaluate(() => { const n = document.querySelector('.screen-name'), a = document.getElementById('acct-btn'); return { text: n.textContent, cut: n.scrollWidth > n.clientWidth + 1, right: n.getBoundingClientRect().right, acct: a.getBoundingClientRect().left }; });
  ok(!r.cut && r.right <= r.acct, `${w}px: "${r.text}" whole and clear of your initial`);
  await page.close();
}
await browser.close();
