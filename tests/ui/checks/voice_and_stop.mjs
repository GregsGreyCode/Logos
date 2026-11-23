// Tap-to-record microphone, a longer limit, Send that becomes Stop, and Home's Ask going to the agent's chat
// (Greg, 23 Nov). Chrome's fake microphone feeds the real recorder.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME,
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] });

async function open(hash, mob = true) {
  const ctx = await browser.newContext({ viewport: mob ? { width: 412, height: 915 } : { width: 1440, height: 900 }, isMobile: mob, hasTouch: mob, permissions: ['microphone'] });
  const page = await ctx.newPage();
  const state = { errs: [], posts: [], gets: 0, fail: 0, text: 'Hello, this is what I said.' };
  page.on('pageerror', (e) => state.errs.push(e.message));
  await page.route('**/api/v2/agents/greg/voice?job=1', (r) => {
    state.posts.push(r.request().postDataBuffer()?.length || 0);
    if (state.fail > 0) { state.fail -= 1; return r.fulfill({ status: 503, json: { error: 'voice_unreachable', message: 'The voice service did not answer.' } }); }
    return r.fulfill({ status: 202, json: { job: 'job1' } });
  });
  await page.route('**/api/v2/agents/greg/voice/job1', (r) => { state.gets += 1; return r.fulfill({ json: state.gets < 2 ? { state: 'working' } : { state: 'done', text: state.text } }); });
  await page.route('**/api/v2/home', async (r) => { const res = await r.fetch(); const d = await res.json();
    if (d.agent) Object.assign(d.agent, { matrix: true, voice: true }); await r.fulfill({ json: d }); });
  await page.goto(`http://127.0.0.1:8779/${hash}`); await page.waitForTimeout(2500);
  return { page, ctx, state };
}

// ── the chat ──────────────────────────────────────────────────────────────────────────────────
let { page, ctx, state } = await open('#/chat/greg');
const send = page.locator('form.composer .send-btn');
ok(await page.locator('form.composer .mic-btn').count() === 1, 'the chat has a microphone button of its own');
ok(await send.isDisabled() && (await send.getAttribute('aria-label')) === 'Send', 'with the box empty and the agent idle, Send rests');
await page.fill('#chat-input', 'hello'); await page.waitForTimeout(150);
ok(await send.isEnabled() && (await send.getAttribute('data-mode')) === 'send', 'words in the box: Send is ready');
await page.fill('#chat-input', ''); await page.waitForTimeout(150);

// tap to start; the strip replaces the box; it keeps recording with no finger down
await page.locator('.mic-btn').click(); await page.waitForTimeout(1600);
ok(await page.locator('.rec-bar').isVisible() && await page.locator('form.composer.recording').count() === 1, 'a tap starts recording: the strip takes the box\'s place');
ok(!(await page.locator('#chat-input').isVisible()), '(the box is out of the way while recording)');
await page.screenshot({ path: `${OUT}/recording-phone.png`, clip: { x: 0, y: 760, width: 412, height: 155 } });
await page.waitForTimeout(1500);
ok(/^0:0[2-9]$/.test(await page.locator('.rec-time').textContent()), `still recording with no finger held down (${await page.locator('.rec-time').textContent()})`);
ok(await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.rec-bar')).getPropertyValue('--level') || '0')) >= 0, 'with a level that moves with the voice');
await page.locator('.rec-done').click(); await page.waitForTimeout(3500);
ok(state.posts.length === 1 && state.posts[0] > 500, `finishing sends the recording (${state.posts[0]} bytes)`);
ok(await page.locator('#chat-input').inputValue() === 'Hello, this is what I said.', 'its words land in the box to check');
ok(/Check it, then send/.test(await page.locator('.mx-note').textContent()) && await page.locator('form.composer.recording').count() === 0, 'with "Check it, then send." and the strip gone');
await page.fill('#chat-input', '');

// a short tap-and-finish is not a recording
await page.locator('.mic-btn').click(); await page.waitForTimeout(150); await page.locator('.rec-done').click(); await page.waitForTimeout(500);
ok(/too short/.test(await page.locator('.mx-note').textContent()) && state.posts.length === 1, 'a tap and finish at once says it was too short and sends nothing');

// throw one away (short: no question)
await page.locator('.mic-btn').click(); await page.waitForTimeout(1200);
await page.locator('.rec-discard').click(); await page.waitForTimeout(500);
ok(state.posts.length === 1 && await page.locator('.rec-bar').isHidden() && await page.locator('.ask-layer').count() === 0, '✕ throws a short one away at once, nothing sent');

// it fails: nothing is lost, Try again sends the same recording
state.fail = 1; state.gets = 0;
await page.locator('.mic-btn').click(); await page.waitForTimeout(1500); await page.locator('.rec-done').click(); await page.waitForTimeout(1500);
const retry = page.locator('.mx-note button', { hasText: 'Try again' });
ok(await retry.count() === 1 && /did not answer/.test(await page.locator('.mx-note').textContent()), 'if turning it into words fails, it says why, with Try again');
const firstSize = state.posts.at(-1);
await retry.click(); await page.waitForTimeout(3500);
ok(state.posts.at(-1) === firstSize && await page.locator('#chat-input').inputValue() === 'Hello, this is what I said.', 'Try again sends the very same recording and the words arrive');
await page.fill('#chat-input', '');

// the limit
const limit = await page.evaluate(async () => (await import('/ui/voice.js')).MAX_RECORD_S);
ok(limit >= 1800, `it records up to ${limit / 60} minutes (was 5)`);

// Send becomes Stop while the agent is working, with nothing in the box
await page.evaluate(() => window.__mx.addMine('!dm:test', 'a question', 'sent')); await page.waitForTimeout(700);
ok(await page.locator('.mx-thinking:not([hidden])').count() === 1 && (await send.getAttribute('data-mode')) === 'stop' && await send.isEnabled(), 'the agent working and the box empty: Send becomes Stop');
ok(/^Stop /.test(await send.getAttribute('aria-label')), `named for a screen reader (${await send.getAttribute('aria-label')})`);
ok(await send.evaluate((e) => getComputedStyle(e).backgroundColor !== 'rgba(0, 0, 0, 0)'), 'and looks like Stop (red)');
await page.screenshot({ path: `${OUT}/stop-phone.png`, clip: { x: 0, y: 700, width: 412, height: 215 } });
await page.fill('#chat-input', 'one more thing'); await page.waitForTimeout(200);
ok((await send.getAttribute('data-mode')) === 'send', 'with words typed it is Send again (a follow-up)');
await page.fill('#chat-input', ''); await page.waitForTimeout(200);
await send.click(); await page.waitForTimeout(800);
const sent = await page.evaluate(() => window.__mx.sent.map(([, c]) => c.body));
ok(sent.at(-1) === '/stop', `Stop asks the agent to stop (sent: ${sent.at(-1)})`);
ok((await send.getAttribute('data-mode')) === 'idle', 'and the chat is no longer waiting on the agent for it');
ok(state.errs.length === 0, `no page errors ${state.errs.join('|')}`);
await ctx.close();

// ── Home's Ask ────────────────────────────────────────────────────────────────────────────────
({ page, ctx, state } = await open('#/home'));
ok(await page.locator('form.home-ask textarea').count() === 1 && await page.locator('form.home-ask .mic-btn').isVisible(), 'Home\'s Ask is a growing box with the microphone');
await page.locator('.home-ask .mic-btn').click(); await page.waitForTimeout(1800);
ok(await page.locator('.home-ask .rec-bar').isVisible(), 'the microphone works there too');
await page.locator('.home-ask .rec-done').click(); await page.waitForTimeout(3500);
ok(await page.locator('#home-ask').inputValue() === 'Hello, this is what I said.', 'its words land in Ask');
await page.fill('#home-ask', 'Is the dentist still on Thursday?');
await page.screenshot({ path: `${OUT}/home-ask-phone.png`, clip: { x: 0, y: 760, width: 412, height: 155 } });
await page.locator('#home-ask').press('Enter'); await page.waitForTimeout(3500);
ok(page.url().endsWith('#/chat/greg'), `Ask opens your agent's own chat (${page.url().split('/').slice(-2).join('/')}), not the archive`);
const asked = await page.evaluate(() => window.__mx.sent.map(([, c]) => c.body));
ok(asked.includes('Is the dentist still on Thursday?'), 'and the question is sent there');
ok(state.errs.length === 0, `no page errors ${state.errs.join('|')}`);
await ctx.close();
await browser.close();
