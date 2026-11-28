import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [w, hgt] of [[1440, 900], [1024, 768]]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt } });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat`); await page.waitForTimeout(2500);
  const marks = await page.$$eval('.void-field .agent-mark', (els) => els.map((e) => { const r = e.getBoundingClientRect(); const l = e.querySelector('.mark-label'); return { bottom: r.bottom, top: r.top, label: l ? getComputedStyle(l).opacity : '-' }; }));
  const conv = await page.locator('.void-groups').boundingBox();
  if (w >= 1100) {
    // With the full side menu the chats are a column on the left (Greg, 28 Nov; agents_list_left.mjs), the agents beside it.
    const left = await page.$$eval('.void-field .agent-mark', (els) => Math.min(...els.map((e) => e.getBoundingClientRect().left)));
    ok(marks.every((m) => m.top > 68) && left >= conv.x + conv.width, `${w}: agents under the title, right of the chats column`);
    ok(conv.y < 4, `${w}: the chats column starts at the top (${Math.round(conv.y)})`);
  } else {
    ok(marks.every((m) => m.top > 68 && m.bottom < conv.y + 4), `${w}: agents sit between the title and the conversations`);
    ok(conv.y < hgt * 0.7, `${w}: conversations start in the upper part (${Math.round(conv.y)})`);
  }
  ok(marks.every((m) => m.label === '1'), `${w}: names shown`);
  await page.screenshot({ path: `${process.argv[2]}/desk/b2-agents-${w}.png` });
  await page.close();
}
// One status vocabulary (UI review, 28 Nov): Routines says what the Agents screen says for the same agent, never "Running".
{
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat`); await page.waitForTimeout(2500);
  const onAgents = Object.fromEntries(await page.$$eval('.void-field .agent-mark', (els) => els.map((e) => [e.dataset.agent, e.querySelector('.mark-label > span').textContent])));
  const band = await page.locator('.void-band > h1').boundingBox(), list = await page.locator('.void-groups').boundingBox();
  ok(band && band.x - (list.x + list.width) <= 44, `the Agents title sits on the left of its column like every screen's (${band && Math.round(band.x - list.x - list.width)}px in)`);
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/agents/greg`); await page.waitForTimeout(2000);
  const onRoutines = Object.fromEntries(await page.$$eval('.agent-card', (els) => els.map((e) => [e.getAttribute('href').split('/').pop(), e.querySelector('.status').textContent])));
  ok(Object.keys(onRoutines).length >= 2 && Object.entries(onRoutines).every(([k, v]) => v === onAgents[k] && !/^Running$/.test(v)),
    `Routines uses the Agents screen's words (${JSON.stringify(onRoutines)} vs ${JSON.stringify(onAgents)})`);
  await page.close();
}
const ph = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
await ph.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat`); await ph.waitForTimeout(2000);
await ph.screenshot({ path: `${process.argv[2]}/desk/b2-agents-phone.png` });
await browser.close();
