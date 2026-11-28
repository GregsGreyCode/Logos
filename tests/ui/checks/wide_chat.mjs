// Big windows (Greg, 28 Nov): one column for the whole chat (messages, "… is thinking", the message box, edges aligned);
// the chats list pinned on the left from 1400 px; two chats side by side from 1800 px; a wide table may use the column.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const out = process.argv[2] || '.';
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}/`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });
// Column edges of each chat on screen: the agent's bubble, the thinking mark and the box's first control on the left;
// your bubble and Send on the right.
const edges = (page) => page.evaluate(() => [...document.querySelectorAll('section.chat')].filter((s) => s.offsetParent).map((sec) => {
  const R = (e) => (e ? e.getBoundingClientRect() : null);
  const last = (s) => [...sec.querySelectorAll(s)].pop();
  return { agent: R(last('.log .msg.assistant'))?.left, user: R(last('.log .msg.user'))?.right, think: R(sec.querySelector('.mx-thinking-mark'))?.left,
    box: R(sec.querySelector('.composer > :not(label, input, .mx-ask[hidden])'))?.left, send: R(sec.querySelector('.composer .send-btn'))?.right };
}));
const near = (a, b) => a != null && b != null && Math.abs(a - b) <= 4;

for (const w of [1280, 1440, 1920, 2560]) {
  const page = await (await browser.newContext({ viewport: { width: w, height: 900 } })).newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto(`${BASE}#/chat/greg`); await page.waitForTimeout(2500);
  await page.evaluate(() => { document.querySelector('.mx-thinking').hidden = false; });
  const side = await page.locator('#chat-side').isVisible();
  if (w < 1400) ok(!side, `${w}px: the chats list is not pinned (it slides away as before)`);
  else {
    const s = await page.locator('#chat-side').boundingBox(), c = await page.locator('section.chat').boundingBox();
    ok(side && s.x + s.width <= c.x + 1, `${w}px: the chats list stays open, on the left of the chat`);
  }
  if (w === 1280) { await page.context().close(); continue; }
  const [e] = await edges(page);
  ok(near(e.agent, e.think) && near(e.agent, e.box), `${w}px: messages, thinking line and message box start at one edge (${e.agent} / ${e.think} / ${e.box})`);
  ok(near(e.user, e.send), `${w}px: your messages and Send end at one edge (${e.user} / ${e.send})`);
  const col = e.send - e.box;
  ok(col <= 1000 && col >= 700, `${w}px: a column of readable width (${Math.round(col)} px)`);
  await page.screenshot({ path: `${out}/wide/chat-${w}.png` });
  if (w === 1440) {
    // ≡ hides the pinned list; this device remembers it, and ≡ brings it back.
    await page.locator('.chat-head .side-reopen').click(); await page.waitForTimeout(400);
    ok(!(await page.locator('#chat-side').isVisible()), '1440px: ≡ hides the chats list');
    await page.reload(); await page.waitForTimeout(2500);
    ok(!(await page.locator('#chat-side').isVisible()), '1440px: still hidden after a reload (remembered here)');
    await page.locator('.chat-head .side-reopen').click(); await page.waitForTimeout(400);
    ok(await page.locator('#chat-side').isVisible(), '1440px: ≡ shows it again');
    ok(await page.locator('.bar-beside').isHidden(), '1440px: no "Open beside" (too narrow for two chats)');
  }
  if (w === 1920) {
    // A wide table: the bubble grows past its reading width to the column, text in it keeps its line length.
    const rows = Array.from({ length: 4 }, (_, i) => `| Day ${i + 1} | ${Array.from({ length: 14 }, (_, j) => `slot ${j + 1} sample text`).join(' | ')} |`);
    await page.evaluate((body) => window.__mx.addEvent('!dm:test', { msgtype: 'm.text', body }),
      `Here is the plan for the week, as a table to compare.\n\n| Day | ${Array.from({ length: 14 }, (_, j) => `Column ${j + 1}`).join(' | ')} |\n|${'---|'.repeat(15)}\n${rows.join('\n')}`);
    await page.waitForTimeout(800);
    const t = await page.evaluate(() => {
      const msg = [...document.querySelectorAll('.log .msg.assistant')].pop(), box = msg.querySelector('.md-table'), p = msg.querySelector('p');
      const sec = msg.closest('section.chat'), send = sec.querySelector('.send-btn').getBoundingClientRect(), first = sec.querySelector('.composer .mx-attach').getBoundingClientRect();
      return { msg: msg.getBoundingClientRect().width, col: send.right - first.left, scrolls: box.scrollWidth > box.clientWidth, pfs: parseFloat(getComputedStyle(p).fontSize), p: p.getBoundingClientRect().width };
    });
    ok(t.msg > t.col - 6 && t.msg <= t.col + 1, `1920px: a wide table's bubble uses the full column (${Math.round(t.msg)} / ${Math.round(t.col)})`);
    ok(t.scrolls, '1920px: still wider than the column, the table scrolls sideways inside it');
    await page.screenshot({ path: `${out}/wide/chat-table-1920.png` });
    // Open beside: the group, picked from the header's beside button (the switcher's list).
    await page.locator('.bar-beside').click(); await page.waitForTimeout(500);
    ok(await page.locator('.switch-beside-head').isVisible(), '1920px: "Open beside" opens the list of chats');
    await page.locator('.agent-bar-pop:visible .switch-item', { hasText: 'Household Group' }).click(); await page.waitForTimeout(2500);
    ok(/#\/chat\/greg\?beside=household\/group\//.test(page.url()), `1920px: the address keeps both chats (${page.url().split('#')[1]})`);
    ok(await page.locator('section.chat').count() === 2 && await page.locator('.chat-pane.beside .group-switch').isVisible(), '1920px: the group opens beside the agent chat');
    await page.reload(); await page.waitForTimeout(2500);
    ok(await page.locator('.chat-pane.beside section.chat').isVisible(), '1920px: a reload keeps both');
    await page.evaluate(() => { for (const t of document.querySelectorAll('.mx-thinking')) t.hidden = false; });
    const both = await edges(page);
    await page.evaluate(() => { for (const t of document.querySelectorAll('.mx-thinking')) t.hidden = true; });
    ok(both.length === 2 && both.every((x) => near(x.agent, x.think) && near(x.agent, x.box) && near(x.user, x.send)), `1920px: each chat keeps its own aligned column (${JSON.stringify(both.map((x) => [x.agent, x.box, x.user, x.send].map(Math.round)))})`);
    // Each box types and sends on its own.
    await page.fill('#chat-input', 'left words');
    const rightSend = page.locator('.chat-pane.beside .send-btn'), leftSend = page.locator('.chat-pane.first .send-btn');
    ok(!(await leftSend.isDisabled()) && await rightSend.isDisabled(), '1920px: typing in the first chat enables only its Send');
    await page.fill('#chat-input', '');
    await page.fill('#chat-input-2', 'right words');
    ok(!(await rightSend.isDisabled()) && await leftSend.isDisabled(), '1920px: typing in the chat beside enables only its Send');
    await rightSend.click(); await page.waitForTimeout(500);
    const sent = await page.evaluate(() => window.__mx.sent.map(([r, c]) => [r, c.body]));
    ok(sent.length === 1 && sent[0][0] === '!group:test' && sent[0][1].includes('right words'), `1920px: it goes to the chat beside (${JSON.stringify(sent)})`);
    // Ctrl+K goes to the chat you are in.
    await page.locator('#chat-input-2').click(); await page.keyboard.press('Control+k'); await page.waitForTimeout(300);
    ok(await page.locator('.chat-pane.beside .agent-bar-pop').isVisible() && await page.locator('.chat-pane.first .agent-bar-pop').isHidden(), '1920px: Ctrl+K opens the switcher of the chat you are in');
    await page.keyboard.press('Escape');
    await page.locator('#chat-input').click(); await page.keyboard.press('Control+k'); await page.waitForTimeout(300);
    ok(await page.locator('.chat-pane.first .agent-bar-pop').isVisible() && await page.locator('.chat-pane.beside .agent-bar-pop').isHidden(), '1920px: and in the first chat, its own');
    await page.keyboard.press('Escape');
    await page.screenshot({ path: `${out}/wide/chat-beside-1920.png` });
    // Narrower than 1800: the second chat hides, the first stays.
    await page.setViewportSize({ width: 1500, height: 900 }); await page.waitForTimeout(300);
    ok(await page.locator('.chat-pane.beside section.chat').isHidden() && await page.locator('.chat-pane.first section.chat').isVisible(), '1500px: the chat beside hides, the first stays');
    await page.setViewportSize({ width: 1920, height: 900 }); await page.waitForTimeout(300);
    await page.locator('.pane-close').click(); await page.waitForTimeout(2000);
    ok(page.url().endsWith('#/chat/greg') && await page.locator('section.chat').count() === 1, `1920px: ✕ closes the chat beside (${page.url().split('#')[1]})`);
  }
  ok(errs.length === 0, `${w}px: no page errors ${errs.join('|')}`);
  await page.context().close();
}
await browser.close();
