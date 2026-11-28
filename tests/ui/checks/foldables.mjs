import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
// unfolded foldables (Galaxy Z Fold / Pixel Fold class), portrait and landscape, and a folded cover screen
for (const [w, hgt, name] of [[840, 904, 'fold-portrait'], [904, 840, 'fold-land'], [768, 1000, 'tablet'], [360, 800, 'cover']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: true, hasTouch: true, deviceScaleFactor: 2.2 });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  for (const hash of ['#/home', '#/chat', '#/chat/greg', '#/noticeboard', '#/agents/greg', '#/settings/appearance', '#/brain/greg']) {
    await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/` + hash); await page.waitForTimeout(hash === '#/chat/greg' ? 2600 : 1500);
    const over = await page.evaluate(() => { const vw = document.documentElement.clientWidth;
      const bad = [...document.querySelectorAll('#view *')].filter((e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
        return r.width > 0 && r.right > vw + 1 && cs.position !== 'fixed' && !e.closest('.side.tucked, .switch-pop, [hidden]'); });
      return { scroll: document.documentElement.scrollWidth > vw + 1, bad: bad.slice(0, 3).map((e) => `${e.className || e.tagName}@${Math.round(e.getBoundingClientRect().right)}`) }; });
    ok(!over.scroll && over.bad.length === 0, `${name} ${w}x${hgt} ${hash}: fits ${over.scroll ? '(page scrolls sideways) ' : ''}${over.bad.join(' | ')}`);
    if (hash === '#/chat') await page.screenshot({ path: `${process.argv[2]}/fold-${name}-agents.png` });
  }
  ok(errs.filter((e) => !/month|model_server/.test(e)).length === 0, `${name}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
