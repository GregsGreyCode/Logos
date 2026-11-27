// The phone tab bar (Greg, 27 Nov): Calendar is the fourth tab, after Home, Agents and Notifications.
// Every label fits and nothing spills at 320 px; the tab lights up on the Calendar (and the account
// initial does not); the bar keeps the home-indicator gap.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt] of [[320, 640], [360, 800], [412, 915]]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto('http://127.0.0.1:8779/#/calendar'); await page.waitForTimeout(1800);
  const bar = await page.evaluate(() => {
    const tabs = [...document.querySelectorAll('.tabbar a')];
    const vw = document.documentElement.clientWidth;
    return {
      labels: tabs.map((a) => [...a.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').trim()),
      cut: tabs.filter((a) => a.scrollWidth > a.clientWidth + 1).map((a) => a.textContent.trim()),
      out: tabs.filter((a) => { const r = a.getBoundingClientRect(); return r.left < -1 || r.right > vw + 1; }).length,
      icons: tabs.map((a) => Math.round(a.querySelector('svg').getBoundingClientRect().width)),
      current: tabs.filter((a) => a.getAttribute('aria-current') === 'page').map((a) => a.textContent.trim()),
      acct: document.querySelector('.avatar-btn')?.getAttribute('aria-current') || null,
      pad: getComputedStyle(document.querySelector('.tabbar')).paddingBottom,
      scroll: document.documentElement.scrollWidth > vw + 1,
      sheetCal: !!document.querySelector('#acct-sheet a[href="#/calendar"]'),
    };
  });
  ok(JSON.stringify(bar.labels) === JSON.stringify(['Home', 'Agents', 'Notifications', 'Calendar']), `${w}px: four tabs (${bar.labels})`);
  ok(bar.cut.length === 0 && bar.out === 0 && !bar.scroll, `${w}px: every tab fits${bar.cut.length ? ' (cut: ' + bar.cut + ')' : ''}`);
  ok(new Set(bar.icons).size === 1, `${w}px: the icons are one size (${bar.icons})`);
  ok(JSON.stringify(bar.current) === '["Calendar"]' && bar.acct !== 'page', `${w}px: the Calendar tab is lit, the account initial is not (${bar.current})`);
  ok(bar.pad !== '' && !bar.sheetCal, `${w}px: the safe-area padding stays (${bar.pad}); the Calendar left the account panel`);
  await page.screenshot({ path: `${process.argv[2]}/phone-tabs-${w}.png` });
  await page.locator('.tabbar a', { hasText: 'Home' }).click(); await page.waitForTimeout(900);
  ok(await page.evaluate(() => location.hash) === '#/home' && (await page.locator('.tabbar a[aria-current="page"]').innerText()).trim() === 'Home', `${w}px: the Home tab goes back`);
  await page.locator('.tabbar a', { hasText: 'Calendar' }).click(); await page.waitForTimeout(1200);
  ok((await page.evaluate(() => location.hash)).startsWith('#/calendar'), `${w}px: the Calendar tab opens the Calendar`);
  ok(errs.filter((e) => !/model_server/.test(e)).length === 0, `${w}px: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
