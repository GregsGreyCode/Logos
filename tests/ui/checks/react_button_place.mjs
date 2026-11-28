// The ☺ on a message (a mouse) stays beside its own message (Greg, 27 Nov: it crossed onto other pills and
// flickered). Desktop: hover each message; its ☺ must not overlap any other message, chip or pill.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2500);
const turns = page.locator('.mx-turn:has(.mx-react-open)');
const n = await turns.count();
ok(n > 0, `messages with a ☺ (${n})`);
let crossed = [];
for (let i = 0; i < n; i++) {
  const t = turns.nth(i);
  await t.locator('.msg').first().hover(); await page.waitForTimeout(150);
  const hit = await t.evaluate((turn) => {
    const b = turn.querySelector('.mx-react-open').getBoundingClientRect();
    const others = [...document.querySelectorAll('.mx-turn .msg, .mx-reactions, .mx-chip, .chip, .pill, .mx-foot')]
      .filter((el) => !turn.contains(el) && !el.contains(turn));
    const over = (r) => r.width && r.height && b.left < r.right && b.right > r.left && b.top < r.bottom && b.bottom > r.top;
    return others.filter((el) => over(el.getBoundingClientRect())).map((el) => el.className).slice(0, 3);
  });
  if (hit.length) crossed.push(`${i}: ${hit.join(', ')}`);
}
ok(crossed.length === 0, `no ☺ overlaps another message or pill ${crossed.join(' | ')}`);
await page.screenshot({ path: `${process.env.OUT || 'tests/ui/out'}/react_button_place.png` });
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
