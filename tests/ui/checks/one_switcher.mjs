// One agent bar on every agent screen (UI review, recommendation 6, Greg, 28 Nov): as Home Assistant's one top bar, the same
// ‹ back, name ▾ (the agents, then the groups) and view pill in the same place in an agent's chat, its Brain, Routines and
// Settings, and in a group (its picture and members in the name, no views: its ≡ where the pill is). Desktop and phone.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const out = process.argv[2] || '.';
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}/`;
const GROUP = '#/chat/household/group/' + encodeURIComponent('!group:test');
const SCREENS = [['chat', '#/chat/greg', 'Chat'], ['brain', '#/brain/greg', 'Brain'], ['routines', '#/agents/greg', 'Routines'],
  ['settings', '#/agents/greg/settings', 'Settings'], ['group', GROUP, null]];
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const near = (a, b, d = 2) => a != null && b != null && Math.abs(a - b) <= d;

for (const [tag, opts] of [
  ['desktop', { viewport: { width: 1440, height: 900 } }],
  ['phone', { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }]]) {
  const ctx = await browser.newContext(opts); const page = await ctx.newPage();
  // The agent chat's pinned chats list (from 1400 px, Greg 28 Nov) closed, so the bars are compared on their own (wide_chat checks the list).
  await ctx.addInitScript(() => { try { localStorage.setItem('logos.side.wide', 'closed'); } catch (e) {} });
  const errs = []; page.on('pageerror', (e) => errs.push(`${e.message} @ ${page.url().split('#')[1]} ${(e.stack || '').split('\n')[1] || ''}`));
  await page.goto(BASE + '#/home'); await page.waitForTimeout(1500);
  const at = {};
  for (const [name, hash, view] of SCREENS) {
    await page.evaluate((h) => { location.hash = h; }, hash); await page.waitForTimeout(2200);
    // Where the bar's parts are: the one visible agent bar, its ‹, its name switch and its right-hand control.
    at[name] = await page.evaluate(() => {
      const bars = [...document.querySelectorAll('nav.agent-bar')].filter((e) => e.offsetParent);
      const bar = bars[0];
      const r = (el) => { if (!el || !el.offsetParent) return null; const b = el.getBoundingClientRect(); return { l: Math.round(b.left), r: Math.round(b.right), t: Math.round(b.top), b: Math.round(b.bottom), cy: Math.round(b.top + b.height / 2) }; };
      if (!bar) return { bars: 0 };
      const right = [...bar.children].filter((e) => e.offsetParent).pop();
      return { bars: bars.length, bar: r(bar), back: r(bar.querySelector('.agent-back')), name: r(bar.querySelector('.agent-bar-switch')),
        caret: r(bar.querySelector('.agent-bar-switch .switch-caret')), pill: r(bar.querySelector('.agent-menu > summary')),
        pillText: bar.querySelector('.agent-menu > summary')?.textContent.replace('▾', '').trim() || null,
        right: r(right), rightIsPill: !!right?.matches('.agent-menu'), logo: !!bar.querySelector('.agent-bar-switch .agent-mark'),
        pic: !!bar.querySelector('.agent-bar-switch .void-avatar.group'),
        switchers: [...document.querySelectorAll('.agent-bar-switch, .switch-btn, .brain-pick')].filter((e) => e.offsetParent).length };
    });
    const a = at[name];
    ok(a.bars === 1, `${tag} ${name}: one agent bar (${a.bars})`);
    ok(a.switchers === 1, `${tag} ${name}: one name switch, no other agent picker (${a.switchers})`);
    ok(a.back && a.name && a.caret && a.name.l >= a.back.r - 2, `${tag} ${name}: ‹ then the name with ▾`);
    if (view) ok(a.pillText === view && a.rightIsPill, `${tag} ${name}: the view pill says ${view}, at the right end (${a.pillText})`);
    else ok(!a.pill && a.right && !a.rightIsPill, `${tag} ${name}: a group has no views; its ≡ is at the right end`);
    if (name !== 'group') ok(!a.logo, `${tag} ${name}: no logo beside the name (Greg, 28 Nov)`);
    else ok(a.pic, `${tag} group: the group keeps its picture`);
    await page.screenshot({ path: `${out}/one-switcher-${tag}-${name}.png`, clip: { x: 0, y: 0, width: opts.viewport.width, height: Math.min(260, opts.viewport.height) } });
  }
  // The same place on each screen: ‹ and the name's left edge, the bar's middle, and the right-hand control's right edge.
  const ref = at.chat;
  for (const [name] of SCREENS.slice(1)) {
    const a = at[name];
    ok(near(a.back?.l, ref.back?.l) && near(a.back?.cy, ref.back?.cy, 6), `${tag} ${name}: ‹ where the chat has it (${a.back?.l},${a.back?.cy} / ${ref.back?.l},${ref.back?.cy})`);
    ok(near(a.name?.l, ref.name?.l), `${tag} ${name}: the name switch starts where the chat's does (${a.name?.l} / ${ref.name?.l})`);
    ok(near(a.right?.r, ref.right?.r), `${tag} ${name}: the right-hand control ends where the chat's pill does (${a.right?.r} / ${ref.right?.r})`);
    if (name !== 'group') ok(near(a.bar?.b, ref.bar?.b), `${tag} ${name}: the bar is as tall as the chat's (${a.bar?.b} / ${ref.bar?.b})`);
  }
  // The same list behind ▾ on every screen: agents first, then groups, the current one ticked; picking an agent keeps the view.
  for (const [name, hash, view] of SCREENS) {
    await page.evaluate((h) => { location.hash = h; }, hash); await page.waitForTimeout(2000);
    await page.locator('nav.agent-bar .agent-bar-switch').filter({ visible: true }).first().click(); await page.waitForTimeout(700);
    const pop = page.locator('.agent-bar-pop:not([hidden])');
    const names = await pop.locator('.switch-item .switch-text strong').allTextContents();
    const g = names.findIndex((n) => /Group/.test(n)), lastAgent = names.findIndex((n) => /Household agent/.test(n));
    const ticked = await pop.locator('.switch-item.on .switch-text strong').allTextContents();
    ok(names.length >= 3 && lastAgent > -1 && g > lastAgent, `${tag} ${name}: ▾ lists the agents, then the groups (${names.join(', ')})`);
    ok(ticked.length === 1 && ticked[0] === (view ? "Greg's agent" : 'Household Group'), `${tag} ${name}: the one you are in is ticked (${ticked})`);
    const box = await pop.boundingBox();
    ok(box && box.x >= 0 && box.x + box.width <= opts.viewport.width, `${tag} ${name}: the list fits on the screen`);
    if (name === 'routines') await page.screenshot({ path: `${out}/one-switcher-${tag}-routines-open.png` });
    const want = { chat: '#/chat/household', brain: '#/brain/household', routines: '#/agents/household', settings: '#/agents/household/settings', group: '#/chat/household' }[name];
    await pop.locator('.switch-item', { hasText: 'Household agent' }).click(); await page.waitForTimeout(1200);
    ok(await page.evaluate(() => location.hash) === want, `${tag} ${name}: picking an agent keeps you in the same view (${await page.evaluate(() => location.hash)})`);
  }
  // Ctrl+K opens the switcher in a chat and in a group.
  for (const hash of ['#/chat/greg', GROUP]) {
    await page.evaluate((h) => { location.hash = h; }, hash); await page.waitForTimeout(2000);
    await page.keyboard.press('Control+k'); await page.waitForTimeout(300);
    ok(await page.locator('.agent-bar-pop:not([hidden])').count() === 1, `${tag} ${hash.includes('group') ? 'group' : 'chat'}: Ctrl+K opens the switcher`);
    await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  }
  if (tag === 'phone') {
    // The phone chat's + and ☰ say what they are.
    await page.evaluate(() => { location.hash = '#/chat/greg'; }); await page.waitForTimeout(2000);
    const chips = await page.locator('.mx-chips').innerText();
    ok(/New/.test(chips) && /Main/.test(chips) && /Chats/.test(chips), `phone chat: + New, Main and ☰ Chats have words (${chips.replace(/\s+/g, ' ')})`);
  }
  ok(errs.length === 0, `${tag}: no page errors ${errs.slice(0, 2).join('|')}`);
  await ctx.close();
}
await browser.close();
