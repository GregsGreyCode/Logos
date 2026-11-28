// Reply and tap-to-react (Greg, 28 Nov): a tap or click on a message brings its emoji row (not only ☺ or a long
// press), which has Copy and Reply; Reply puts a "Replying to …" strip above the box, ✕ drops it, and the message
// sent carries m.in_reply_to with the quote shown above it. On a desktop and a phone.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const out = process.argv[2] || '.';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
for (const [tag, opts] of [['desktop', { viewport: { width: 1440, height: 900 } }],
  ['phone', { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }]]) {
  const page = await (await browser.newContext(opts)).newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2500);
  const msg = page.locator('.log .msg.assistant').last();
  if (tag === 'phone') await msg.tap({ position: { x: 20, y: 12 } }); else await msg.click({ position: { x: 20, y: 12 } });
  await page.waitForTimeout(400);
  ok(await page.locator('.mx-picker').isVisible(), `${tag}: a tap on a message brings its emoji row`);
  ok(await page.locator('.mx-picker .mx-reply').isVisible() && await page.locator('.mx-picker button', { hasText: '👍' }).isVisible(),
    `${tag}: the row has the emoji and Reply`);
  await page.locator('.mx-picker .mx-reply').click(); await page.waitForTimeout(300);
  const bar = page.locator('.mx-reply-bar');
  ok(await bar.isVisible() && /Replying to/.test(await bar.innerText()), `${tag}: Replying to … above the box`);
  ok(await page.evaluate(() => document.activeElement?.id?.startsWith('chat-input')), `${tag}: the box has the focus`);
  if (tag === 'desktop') {
    const b = await bar.boundingBox(), box = await page.locator('.composer').boundingBox();
    ok(b && box && b.y + b.height <= box.y + 2, `${tag}: the strip sits right above the box`);
  }
  await page.locator('.mx-reply-x').click(); await page.waitForTimeout(200);
  ok(await bar.isHidden(), `${tag}: ✕ drops the reply`);
  // Send a reply to an earlier message (not the one just above) and see its quote.
  const earlier = page.locator('.log .msg.user').first();
  await earlier.click({ position: { x: 10, y: 10 } }); await page.waitForTimeout(300);
  await page.locator('.mx-picker .mx-reply').click(); await page.waitForTimeout(200);
  await page.locator('.composer textarea').first().fill('answering that one');
  await page.locator('.composer textarea').first().press('Enter'); await page.waitForTimeout(1200);
  const mine = page.locator('.log .msg.user').last();
  ok(await mine.locator('.mx-quote').isVisible(), `${tag}: the reply shows a quote of the message it answers`);
  ok((await mine.innerText()).includes('answering that one') && !(await mine.innerText()).includes('> <'), `${tag}: no "> <name>" text in the reply`);
  ok(await bar.isHidden(), `${tag}: the strip goes once sent`);
  // As Matrix writes a reply, so hermes tells the agent which message: "> <@user:server> the whole quote".
  const sent = await page.evaluate(() => window.__mx.sent[window.__mx.sent.length - 1][1]);
  ok(/^> <@[^>]+:[^>]+> /.test(sent.body) && sent.body.endsWith('\n\nanswering that one') && !sent.body.includes('…'),
    `${tag}: sent with the author's Matrix id and the whole quote (${JSON.stringify(sent.body).slice(0, 80)})`);
  ok(sent['m.relates_to']?.['m.in_reply_to']?.event_id, `${tag}: and m.in_reply_to`);
  // A reply to the message just above (an agent marks every answer so) shows no quote: it would say nothing.
  await page.locator('.log .msg.user').last().click({ position: { x: 10, y: 10 } }); await page.waitForTimeout(300);
  await page.locator('.mx-picker .mx-reply').click(); await page.waitForTimeout(200);
  await page.locator('.composer textarea').first().fill('and this one');
  await page.locator('.composer textarea').first().press('Enter'); await page.waitForTimeout(1200);
  ok(!(await page.locator('.log .msg.user').last().locator('.mx-quote').count()), `${tag}: no quote of the message just above`);
  if (tag === 'desktop') await page.screenshot({ path: `${out}/desk/reply_and_tap.png` });
  ok(!errs.length, `${tag}: no page errors (${errs.join('; ').slice(0, 120)})`);
}
await browser.close();
