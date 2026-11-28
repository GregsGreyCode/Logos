// A group's header is the agent chat's header (Greg, 28 Nov: "on desktop the Household Group UI is different from the
// agent UIs"): ‹ back, a picture, the name with ▾ (switch to another chat) and who is in it, ≡ on the right; the
// group's panel has its members, Invite and Leave. On a phone and a desktop, and nothing cut at the top of the chat.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const out = process.argv[2] || '.';
const GROUP = '#/chat/household/group/' + encodeURIComponent('!group:test');
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const box = (page, sel) => page.locator(sel).first().boundingBox();
// Where the header's parts are: the back arrow, the name and the menu button, and the top of the messages.
const parts = (page, menuSel) => page.evaluate((menuSel) => {
  const r = (sel) => { const el = [...document.querySelectorAll(sel)].find((e) => e.offsetParent); if (!el) return null; const b = el.getBoundingClientRect(); return { l: Math.round(b.left), r: Math.round(b.right), t: Math.round(b.top), b: Math.round(b.bottom) }; };
  const log = document.querySelector('.chat .log');
  return { pic: r('.chat-head .agent-mark.current .mark-glyph, .chat-head .phone-mark .mark-glyph, .chat-head .group-switch .void-avatar'), back: r('.chat-head .agent-back'), name: r('.chat-head .switch-caret'), menu: r(menuSel), head: r('.chat-head'), log: r('.chat .log'),
    scrolled: log.scrollTop, fade: log.classList.contains('fade-top'), olderGap: Math.round(log.querySelector('.mx-older')?.getBoundingClientRect().top - log.getBoundingClientRect().top + log.scrollTop) };
}, menuSel);

for (const [tag, opts, menuSel] of [
  ['desktop', { viewport: { width: 1440, height: 900 } }, '.chat-head .side-reopen'],
  ['phone', { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }, null]]) {
  const ctx = await browser.newContext(opts); const page = await ctx.newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto('http://127.0.0.1:8779/#/chat/household'); await page.waitForTimeout(2500);
  // The agent chat's ≡ on a phone is at the end of its chips row; its right-hand control in the header is the Chat pill.
  const agent = await parts(page, menuSel || '.chat-head .agent-menu > summary');
  await page.evaluate((h) => { location.hash = h; }, GROUP); await page.waitForTimeout(2500);
  const grp = await parts(page, menuSel || '.chat-head .conv-btn');
  ok(grp.back && agent.back && Math.abs(grp.back.l - agent.back.l) <= 2, `${tag}: ‹ back in the same place as in an agent chat (${grp.back?.l} / ${agent.back?.l})`);
  ok(grp.name && grp.name.l > grp.back.l && grp.name.r < grp.menu.l, `${tag}: the name with ▾ after it`);
  ok(grp.menu && agent.menu && Math.abs(grp.menu.r - agent.menu.r) <= 2, `${tag}: ≡ on the right, where the agent chat has its right-hand control (${grp.menu?.r} / ${agent.menu?.r})`);
  const pic = page.locator('.chat-head .group-switch .void-avatar.group');
  ok(await pic.isVisible() && grp.pic && agent.pic && Math.abs(grp.pic.l - agent.pic.l) <= 12,
    `${tag}: the group picture after ‹, where the agent chat has the agent's mark (${grp.pic?.l} / ${agent.pic?.l})`);
  ok((await page.locator('.chat-head .group-who').innerText()).includes('Béré'), `${tag}: who is in it, under the name`);
  const nameText = await page.locator('.chat-head .group-title .mark-name').evaluate((e) => e.scrollWidth <= e.clientWidth + 1);
  ok(nameText, `${tag}: the group's name is not cut`);
  ok(await page.locator('.nav-links a[aria-current="page"], .tabbar a[aria-current="page"]').filter({ visible: true }).first().innerText().then((t) => /Agents/.test(t)),
    `${tag}: Agents is lit in the menu for a group`);
  // Nothing cut at the top: the messages start below the header; scrolled, the top edge fades; at the top, "Earlier messages"
  // sits as far from the top as in an agent chat.
  ok(grp.log.t >= grp.head.b - 1, `${tag}: the messages start under the header (${grp.log.t} ≥ ${grp.head.b})`);
  ok(grp.scrolled === 0 || grp.fade, `${tag}: scrolled down, the top edge fades instead of cutting a line`);
  ok(grp.olderGap === agent.olderGap, `${tag}: "Earlier messages" at the top, spaced as in an agent chat (${grp.olderGap} / ${agent.olderGap})`);
  await page.locator('.chat .log').evaluate((e) => { e.scrollTop = 0; }); await page.waitForTimeout(200);
  ok(!(await page.locator('.chat .log').evaluate((e) => e.classList.contains('fade-top'))), `${tag}: at the very top, no fade`);
  await page.screenshot({ path: `${out}/group-header-${tag}.png` });
  // ▾: your chats, this group ticked.
  await page.locator('.chat-head .group-switch').click(); await page.waitForTimeout(400);
  const rows = await page.locator('.group-pop .switch-item').allInnerTexts();
  ok(rows.length >= 2 && await page.locator('.group-pop .switch-item.on', { hasText: 'Household Group' }).count() === 1, `${tag}: ▾ lists your chats, this group ticked (${rows.length})`);
  const pop = await box(page, '.group-pop');
  ok(pop && pop.x >= 0 && pop.x + pop.width <= opts.viewport.width, `${tag}: the list fits on the screen`);
  await page.screenshot({ path: `${out}/group-header-${tag}-switch.png` });
  await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  ok(await page.locator('.group-pop').isHidden(), `${tag}: Escape closes it`);
  // ≡: members, Invite, Leave.
  await page.locator(menuSel || '.chat-head .conv-btn').click(); await page.waitForTimeout(700);
  const side = page.locator('#chat-side');
  ok(await side.getByText('In this group').isVisible() && (await side.innerText()).includes('Béré'), `${tag}: ≡ opens the group's panel with its members`);
  await side.getByText('+ Invite someone').click(); await page.waitForTimeout(500);
  const pick = side.locator('.group-invite-pick').first();
  ok(await pick.count() === 1 && (await pick.innerText()).includes("Greg's agent"), `${tag}: Invite lists who is not in it yet`);
  await pick.click(); await page.waitForTimeout(300);
  ok(await page.evaluate(() => JSON.stringify(window.__mx.invited || [])) === JSON.stringify([['!group:test', '@greg-agent:test']]), `${tag}: Invite invites them to the group`);
  const leave = side.locator('.group-leave');
  await leave.click(); await page.waitForTimeout(200);
  ok(/Tap again/.test(await leave.innerText()) && !(await page.evaluate(() => (window.__mx.left || []).length)), `${tag}: Leave asks once more before leaving`);
  await page.screenshot({ path: `${out}/group-header-${tag}-panel.png` });
  await leave.click(); await page.waitForTimeout(500);
  ok(await page.evaluate(() => (window.__mx.left || []).join()) === '!group:test' && /#\/chat$/.test(page.url()), `${tag}: a second tap leaves and goes back to the chats`);
  ok(errs.length === 0, `${tag}: no page errors ${errs.join('|')}`);
  await ctx.close();
}
await browser.close();
