import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt, cols] of [[1440, 900, 2], [1920, 1080, 3], [2560, 1440, 4]]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt } });
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2200);
  const xs = new Set(await page.$$eval('.home-widgets > .home-widget:not([hidden]):not(.off)', (els) => els.map((e) => Math.round(e.getBoundingClientRect().left))));
  ok(xs.size === cols, `${w}: Home in ${cols} columns (${xs.size})`);
  for (const hash of ['#/home', '#/chat', '#/noticeboard', '#/agents/greg', '#/settings', '#/chat/greg']) {
    await page.goto('http://127.0.0.1:8779/' + hash); await page.waitForTimeout(1200);
    const over = await page.evaluate(() => { const vw = document.documentElement.clientWidth;
      return [...document.querySelectorAll('.void-groups, .feed-days, .routine-cards, .settings-body, .home-widgets, .agent-cards, .page-band > *:last-child')]
        .filter((e) => { const r = e.getBoundingClientRect(); return r.width && r.right > vw - 24; }).map((e) => `${e.className}@${Math.round(e.getBoundingClientRect().right)}/${vw}`); });
    ok(over.length === 0, `${w} ${hash}: content keeps its margin from the right edge ${over.join(' | ')}`);
  }
  await page.close();
}
const p = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await p.goto('http://127.0.0.1:8779/#/chat'); await p.waitForTimeout(2000);
await p.screenshot({ path: process.argv[2] + '/wide/after2-1920-agents.png' });
await browser.close();
