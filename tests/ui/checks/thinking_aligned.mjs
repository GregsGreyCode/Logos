// "… is thinking" lines up with the agent's messages in the centred chat column on a desktop (Greg, 28 Nov: it sat
// at the left edge of the chat, out of line with everything else), at a narrow, a usual and a wide desktop.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const out = process.argv[2] || '.';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const w of [900, 1440, 1920]) {
  const page = await (await browser.newContext({ viewport: { width: w, height: 900 } })).newPage();
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2500);
  await page.evaluate(() => { document.querySelector('.mx-thinking').hidden = false; });
  const m = await page.evaluate(() => {
    const L = (s) => { const e = [...document.querySelectorAll(s)].filter((x) => x.offsetParent).pop(); return e ? Math.round(e.getBoundingClientRect().left) : null; };
    return { bubble: L('.log .msg.assistant'), mark: L('.mx-thinking-mark') };
  });
  ok(m.bubble != null && m.mark != null && Math.abs(m.mark - m.bubble) <= 4, `${w}px: the thinking line starts where the agent's messages do (${m.mark} / ${m.bubble})`);
  if (w === 1440) await page.screenshot({ path: `${out}/desk/thinking_aligned.png` });
}
await browser.close();
