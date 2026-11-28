// While an agent works, only its mark in the "thinking" row pulses; the chat header has no agent logo (Greg, 28 Nov:
// it could not keep step with the menu's logo, so it went). Phone and desktop, agent chat and group.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const out = process.argv[2] || '.';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const HEAD = '.chat-head .agent-mark .mark-glow, .chat-head .phone-mark .mark-glow, .chat-head .group-switch .void-avatar';
// The pulse animations running on the visible header mark and on the thinking row's mark.
const pulses = (page) => page.evaluate((HEAD) => {
  const vis = (sel) => [...document.querySelectorAll(sel)].find((e) => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  const run = (el) => (el ? el.getAnimations().filter((a) => a.animationName === 'work-pulse' && a.playState === 'running') : []);
  const head = run(vis(HEAD)), row = run(vis('.mx-thinking:not([hidden]) .mx-thinking-mark .mark-glow'));
  return { head: head.length, row: row.length, apart: head[0] && row[0] ? Math.abs(Number(head[0].currentTime) - Number(row[0].currentTime)) : null,
    headSeen: !!vis(HEAD) };
}, HEAD);

for (const [tag, opts] of [['desktop', { viewport: { width: 1440, height: 900 } }],
  ['phone', { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }]]) {
  for (const [kind, hash, room, ask, agentId] of [
    ['agent chat', '#/chat/greg', '!dm:test', { msgtype: 'm.text', body: 'What is on tomorrow?' }, '@greg-agent:test'],
    ['group', '#/chat/household/group/' + encodeURIComponent('!group:test'), '!group:test',
      { msgtype: 'm.text', body: 'Household agent: what is for dinner?', 'm.mentions': { user_ids: ['@household-agent:test'] } }, '@household-agent:test']]) {
    const ctx = await browser.newContext(opts); const page = await ctx.newPage();
    const errs = []; page.on('pageerror', (e) => errs.push(e.message));
    await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/` + hash); await page.waitForTimeout(2500);
    const idle = await pulses(page);
    ok(idle.head === 0 && idle.row === 0 && (kind === 'group' || !idle.headSeen), `${tag} ${kind}: no agent logo in the header, and nothing pulses while the agent rests`);
    await page.evaluate(([room, ask]) => window.__mx.addEvent(room, ask, '@greg:test'), [room, ask]); await page.waitForTimeout(1200);
    const busy = await pulses(page);
    ok(busy.head === 0 && busy.row === 1, `${tag} ${kind}: while it works, only the thinking row's mark pulses (${busy.head}, ${busy.row})`);
    await page.waitForTimeout(900);
    const later = await pulses(page);
    ok(later.row === 1 && later.head === 0, `${tag} ${kind}: still only the thinking row a moment later`);
    await page.screenshot({ path: `${out}/working-pulse-${tag}-${kind.replace(' ', '-')}.png` });
    await page.evaluate(([room, agentId]) => window.__mx.addEvent(room, { msgtype: 'm.text', body: 'Pasta, as planned.' }, agentId), [room, agentId]);
    await page.waitForTimeout(1200);
    const done = await pulses(page);
    ok(done.head === 0 && done.row === 0, `${tag} ${kind}: it stops when the reply comes`);
    ok(errs.length === 0, `${tag} ${kind}: no page errors ${errs.join('|')}`);
    await ctx.close();
  }
}
// Reduced motion: no pulsing at all.
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' }); const page = await ctx.newPage();
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2500);
await page.evaluate(() => window.__mx.addEvent('!dm:test', { msgtype: 'm.text', body: 'Anything else?' }, '@greg:test')); await page.waitForTimeout(1200);
const still = await pulses(page);
ok(still.head === 0 && still.row === 0, 'reduced motion: the marks do not pulse');
await browser.close();
