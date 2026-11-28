// What was said through the house speaker shows in the agent's chat (V1.3, logos-speaker-chat): the agent's
// message, with the words heard quoted at the top, never as a message of yours.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2500);
const post = (heard, answer, late) => page.evaluate(([heard, answer, late]) => window.__mx.addEvent('!dm:test', {
  msgtype: late ? 'm.text' : 'm.notice', body: `🔊 Heard at the house speaker:\n> “${heard}”\n\n${answer}`,
  'org.logos.speaker': { heard, late } }, '@greg-agent:test'), [heard, answer, late]);
await post("What's on tomorrow?", 'The **dentist** at ten, then nothing until the evening.', false);
await page.waitForTimeout(800);
const turn = page.locator('.mx-turn', { hasText: 'dentist at ten' }).last();
ok(await turn.evaluate((e) => e.classList.contains('agent')), "it is the agent's message, on the agent's side");
ok(await turn.locator('.mx-speaker-label').innerText() === 'Heard at the house speaker', 'labelled as heard at the house speaker');
ok(await turn.locator('.mx-speaker-heard').innerText() === "What's on tomorrow?", 'the words heard are quoted at the top');
ok(await turn.locator('.msg strong').innerText() === 'dentist', 'the answer underneath is formatted like any reply');
ok(!(await turn.innerText()).includes('> “'), 'the plain-text quote (for Element X) is not shown twice');
await turn.screenshot({ path: process.argv[2] + '/speaker-chat.png' });
await post('Plan a week of dinners', 'Here is the plan: pasta on Monday.', true);
await page.waitForTimeout(800);
ok(await page.locator('.mx-turn', { hasText: 'pasta on Monday' }).last().locator('.mx-speaker-label').innerText() === 'Heard at the house speaker · answered here',
  'an answer that came too late for the speaker says it was answered here');
// Someone else's message carrying the field is shown as it is: only an agent's speaker posts get the card.
await page.evaluate(() => window.__mx.addEvent('!dm:test', { msgtype: 'm.text', body: 'just text', 'org.logos.speaker': { heard: 'fake' } }, '@greg:test'));
await page.waitForTimeout(800);
ok(await page.locator('.mx-turn', { hasText: 'just text' }).last().locator('.mx-speaker').count() === 0, "a person's message is never shown as a speaker exchange");
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
