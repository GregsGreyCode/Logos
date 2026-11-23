import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
const mid = async () => page.evaluate(() => { const e = document.getElementById('screen-name'); const r = e.getBoundingClientRect();
  const range = document.createRange(); range.selectNodeContents(e); const t = range.getBoundingClientRect();
  return { text: e.textContent, centre: Math.round(t.left + t.width / 2), w: innerWidth, divider: getComputedStyle(e, '::before').content + '|' + getComputedStyle(e).borderLeftWidth }; });
for (const hash of ['#/home', '#/chat', '#/noticeboard', '#/settings/email', '#/people', '#/models']) {
  await page.goto('http://127.0.0.1:8779/' + hash); await page.waitForTimeout(1500);
  const m = await mid();
  ok(Math.abs(m.centre - m.w / 2) <= 3, `${hash}: "${m.text}" centred (${m.centre} of ${m.w}) ${m.divider}`);
  if (hash === '#/settings/email') await page.screenshot({ path: process.argv[2] + '/set-phone.png', clip: { x: 0, y: 0, width: 412, height: 140 } });
}
await browser.close();
