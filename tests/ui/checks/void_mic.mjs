// The Agents screen: a microphone on each agent's mark (tap to start, tap ■ to send); holding a mark only lifts it
// (Greg, 23 Nov).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME, args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] });
for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob, permissions: ['microphone'] });
  const page = await ctx.newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  let posts = 0, gets = 0;
  await page.route('**/api/v2/agents', async (r) => { const res = await r.fetch(); const d = await res.json(); for (const a of d.agents || []) a.voice = true; await r.fulfill({ json: d }); });
  await page.route('**/api/v2/agents/*/voice?job=1', (r) => { posts += 1; return r.fulfill({ status: 202, json: { job: 'jobv' } }); });
  await page.route('**/api/v2/agents/*/voice/jobv', (r) => { gets += 1; return r.fulfill({ json: gets < 2 ? { state: 'working' } : { state: 'done', text: 'Is the dentist still on Thursday?' } }); });
  await page.goto('http://127.0.0.1:8779/#/chat'); await page.waitForTimeout(2800);
  const marks = page.locator('.void-field .agent-mark[data-href]');
  ok(await marks.count() >= 1 && await page.locator('.void-field .agent-mark .mark-mic').count() === await marks.count(), `${n}: each of your agents' marks has a microphone`);
  const first = marks.first(), name = await first.getAttribute('data-agent');
  const box = await first.boundingBox(), mic = await first.locator('.mark-mic').boundingBox();
  ok(mic.width >= 36 && mic.x >= box.x - 12 && mic.x + mic.width <= box.x + box.width + 12, `${n}: a thumb-sized button on the mark (${Math.round(mic.width)}px)`);

  // holding a mark lifts it, and does not record
  const cx = box.x + box.width / 2 - 20, cy = box.y + box.height / 3;
  await page.mouse.move(cx, cy); await page.mouse.down(); await page.waitForTimeout(1700);
  ok(await page.locator('.void-rec-dock .rec-bar:not([hidden])').count() === 0 && !(await first.evaluate((e) => e.classList.contains('recording'))), `${n}: holding a mark does not record any more`);
  await page.mouse.up(); await page.waitForTimeout(400);

  // tap the microphone
  await first.locator('.mark-mic').click(); await page.waitForTimeout(1600);
  ok(page.url().endsWith('#/chat'), `${n}: tapping the microphone does not open the chat`);
  ok(await page.locator('.void-rec-dock .rec-bar').isVisible() && await first.evaluate((e) => e.classList.contains('recording')), `${n}: it records: the strip over the screen, the mark marked`);
  await page.screenshot({ path: `${OUT}/void-recording-${n}.png` });
  await page.waitForTimeout(1200);
  ok(/^0:0[2-9]$/.test(await page.locator('.void-rec-dock .rec-time').textContent()), `${n}: it keeps recording with no finger held (${await page.locator('.void-rec-dock .rec-time').textContent()})`);
  await page.locator('.void-rec-dock .rec-done').click(); await page.waitForTimeout(4200);
  ok(posts === 1, `${n}: ■ sends it to be turned into words`);
  ok(page.url().endsWith(`#/chat/${name}`), `${n}: and opens that agent's chat (${page.url().split('#')[1]})`);
  const sent = await page.evaluate(() => window.__mx.sent.map(([, c]) => c).filter((c) => c['org.logos.voice']));
  ok(sent.length === 1 && sent[0].body === 'Is the dentist still on Thursday?' && sent[0]['org.logos.voice'].seconds >= 2, `${n}: where it is sent as a voice message`);
  ok(await page.locator('.mx-turn.me .mx-voice-bar').count() >= 1, `${n}: which shows as a voice bubble with its words`);
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await ctx.close();
}
await browser.close();
