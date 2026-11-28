// Settings › Your voice (Voice VO.2): record a voice print with the real recorder (Chrome's fake microphone),
// see how good it is, test it (you, someone else), check what the house speaker heard, and delete it.
// VO.3: it says whether it is still learning (with how far along) or recognising you; "That was me" / "Not me"
// for the speaker's last 30 minutes; start learning again; an admin sees everyone's state.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME,
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] });
const PROGRESS = { labelled: 23, days: 2, window: 20, hits: 19, rate: 0.95, held: 2, ready: false,
  missing: ['27 more commands at the speaker', 'commands on 1 more day'] };
const SUMMARY = { samples: 28, learned: 23, speaker: 23, threshold: 0.56, speaker_threshold: 0.52, own: [0.81, 0.93], others: 120, others_top: 0.43,
  tested_others: 0, state: 'learning', progress: PROGRESS };
const ACTIVE = { ...SUMMARY, samples: 60, speaker: 55, learned: 55, state: 'active', progress: { ...PROGRESS, labelled: 55, days: 4, window: 50, hits: 50, rate: 1, ready: true, missing: [] } };
const RULES = { samples: 50, days: 3, window: 50, rate: 0.99, off: 0.95 };

async function open(hash, mob = true, admin = false) {
  const ctx = await browser.newContext({ viewport: mob ? { width: 412, height: 915 } : { width: 1440, height: 900 }, isMobile: mob, hasTouch: mob, permissions: ['microphone'] });
  const page = await ctx.newPage();
  const st = { errs: [], posts: [], enrolled: false, deleted: 0, claimed: 0, notme: 0, reset: 0, active: false };
  page.on('pageerror', (e) => st.errs.push(e.message));
  await page.route('**/api/v2/voiceprint', (r) => {
    if (r.request().method() === 'DELETE') { st.deleted += 1; st.enrolled = false; return r.fulfill({ json: { ok: true } }); }
    return r.fulfill({ json: { on: true, enrolled: st.enrolled, summary: st.enrolled ? (st.active ? ACTIVE : SUMMARY) : null, seconds: 25, need: 6, admin, rules: RULES,
      prompt: 'Read this aloud: “What\'s on my calendar today?” Then count slowly from twenty to thirty.' } });
  });
  await page.route('**/api/v2/voiceprint?step=*', (r) => {
    const url = r.request().url(), step = new URL(url).searchParams.get('step');
    st.posts.push({ step, add: new URL(url).searchParams.get('add'), bytes: r.request().postDataBuffer()?.length || 0, type: r.request().headers()['content-type'] });
    if (step === 'enrol' && st.posts.filter((p) => p.step === 'enrol').length === 1)
      return r.fulfill({ json: { ok: false, quality: { speech: 2.1, snr: 20, clipped: false }, problem: 'I heard 2.1 seconds of speech and need 6. Record again, and keep reading until the time runs out.' } });
    if (step === 'enrol') { st.enrolled = true; return r.fulfill({ json: { ok: true, quality: { speech: 10.4, snr: 24, clipped: false }, summary: SUMMARY } }); }
    if (step === 'me') return r.fulfill({ json: { ok: true, quality: { speech: 4, snr: 22 }, score: 0.82, threshold: 0.56, recognised: true, summary: SUMMARY } });
    return r.fulfill({ json: { ok: true, quality: { speech: 4, snr: 22 }, score: 0.31, threshold: 0.56, recognised: false, summary: { ...SUMMARY, tested_others: 1 } } });
  });
  const now = Date.now() / 1000;
  await page.route('**/api/v2/voiceprint/heard', (r) => r.fulfill({ json: { threshold: 0.52, state: 'learning', heard: [
    { id: 7, at: now - 12, ago: 12, speech: 2.2, score: 0.31, recognised: false, added: false, held: 'did not sound like you' },
    { id: 6, at: now - 1500, ago: 1500, speech: 1.9, score: 0.74, recognised: true, added: true, held: null }] } }));
  await page.route('**/api/v2/voiceprint/heard/7', (r) => { st.claimed += 1; return r.fulfill({ json: { ok: true, score: 0.6, summary: { ...SUMMARY, progress: { ...PROGRESS, labelled: 24 } } } }); });
  await page.route('**/api/v2/voiceprint/heard/6', (r) => { st.notme += r.request().method() === 'DELETE' ? 1 : 0; return r.fulfill({ json: { ok: true, summary: { ...SUMMARY, progress: { ...PROGRESS, labelled: 22 } } } }); });
  await page.route('**/api/v2/voiceprint/state', (r) => { st.reset += 1; st.active = false; return r.fulfill({ json: { ok: true, summary: SUMMARY } }); });
  await page.route('**/api/v2/voiceprint/people', (r) => r.fulfill({ json: { people: [
    { username: 'alex', display_name: 'Alex', state: 'active', speaker: 55, labelled: 55, days: 4, window: 50, hits: 50, held: 0 },
    { username: 'sam', display_name: 'Sam', state: 'learning', speaker: 9, labelled: 9, days: 1, window: 7, hits: 6, held: 1 }] } }));
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/${hash}`); await page.waitForTimeout(2500);
  return { page, ctx, st };
}

async function record(page, seconds = 1.6) {
  await page.locator('.voiceprint-form .mic-btn').click(); await page.waitForTimeout(seconds * 1000);
  await page.locator('.voiceprint-form .rec-done').click(); await page.waitForTimeout(900);
}

// Phone: the row, then its page.
let { page, ctx, st } = await open('#/settings');
const row = page.locator('.set-row', { hasText: 'Your voice' });
ok(await row.count() === 1, 'a "Your voice" row under You');
ok((await row.locator('.set-value').textContent()) === 'Not set up', `it says it is not set up (${await row.locator('.set-value').textContent()})`);
await row.tap(); await page.waitForTimeout(1500);
ok(page.url().endsWith('#/settings/voice') && (await page.locator('#screen-name').textContent()) === 'Your voice', 'it opens its own page');
const text = await page.locator('.settings-body').textContent();
ok(/never the recording/.test(text) && /strong hint, not a lock/.test(text), 'it says only numbers are kept, and that it is a hint, not a lock');
ok(/count slowly from twenty/.test(await page.locator('.voiceprint-prompt').textContent()), 'it shows what to read');
ok(await page.locator('section[aria-label="Test it"]').isHidden() && await page.locator('section[aria-label="Delete your voice print"]').isHidden(), 'no test or delete before there is a voice print');

// Too short: says why, nothing saved.
await record(page);
ok(st.posts[0] && st.posts[0].step === 'enrol' && st.posts[0].bytes > 0 && /audio/.test(st.posts[0].type || ''), `the recording is sent as audio (${JSON.stringify(st.posts[0])})`);
ok(/2\.1 s of speech/.test(await page.locator('.settings-body').textContent()) && /need 6/.test(await page.locator('.settings-body').textContent()), 'a poor recording says what was wrong');
// Again: saved, with how good it is.
await record(page);
const now = await page.locator('.voiceprint-now').textContent();
ok(/Learning your voice/.test(now) && /23 recordings from the speaker/.test(now) && /19 of the last 20 times/.test(now) && /switches on at 99%/.test(now),
  `it says in plain words that it is still learning, and how far along (${now})`);
ok(/nothing private is said out loud/.test(now), 'and what that means meanwhile');
await page.locator('.voiceprint-details summary').click(); await page.waitForTimeout(200);
const det = await page.locator('.voiceprint-details').textContent();
ok(/0\.81 to 0\.93/.test(det) && /120 other voices/.test(det) && /0\.56/.test(det) && /0\.52/.test(det) && /on 3 different days/.test(det) && /27 more commands/.test(det),
  `the numbers are under Details (${det})`);
ok(/10\.4 s of speech, clean/.test(await page.locator('.settings-body').textContent()), 'and the recording\'s');
ok(await page.locator('section[aria-label="Test it"]').isVisible(), 'now it can be tested');
await page.screenshot({ path: `${OUT}/your-voice-phone.png`, fullPage: true });

// Test: me, then someone else.
await page.locator('button', { hasText: 'Test: me' }).click(); await page.waitForTimeout(1600);
await page.locator('.voiceprint-form .rec-done').click(); await page.waitForTimeout(900);
ok(st.posts.at(-1).step === 'me' && /You scored 0\.82 \(needs 0\.56\): recognised/.test(await page.locator('section[aria-label="Test it"]').textContent()), 'Test: me says it recognised you');
await page.locator('button', { hasText: 'Test: someone else' }).click(); await page.waitForTimeout(1600);
await page.locator('.voiceprint-form .rec-done').click(); await page.waitForTimeout(900);
ok(st.posts.at(-1).step === 'other' && /refused, as it should be/.test(await page.locator('section[aria-label="Test it"]').textContent()), 'Test: someone else says they were refused');
ok(/raised by 1 other voice/.test(await page.locator('.voiceprint-details').textContent()), 'and the print says it has heard someone else');
// Recording again adds to it.
await record(page);
ok(st.posts.at(-1).step === 'enrol' && st.posts.at(-1).add === '1', 'recording again adds to the voice print');

// At the speaker.
await page.locator('button', { hasText: 'Check what it heard' }).click(); await page.waitForTimeout(700);
const heard = page.locator('.voiceprint-heard-row');
const first = await heard.first().textContent();
ok(await heard.count() === 2 && /held back: did not sound like you/.test(first) && /2\.2 s/.test(first) && /\d\d:\d\d/.test(first), `what the speaker heard, newest first, with time and length (${first})`);
ok(/added to your voice/.test(await heard.nth(1).textContent()) && await heard.nth(1).locator('button', { hasText: 'Not me' }).count() === 1, 'one that was added can be taken out');
await heard.first().locator('button', { hasText: 'That was me' }).click(); await page.waitForTimeout(600);
ok(st.claimed === 1 && /Added/.test(await heard.first().textContent()) && /24 recordings/.test(await page.locator('.voiceprint-now').textContent()), '"That was me" adds a held-back one');
await heard.nth(1).locator('button', { hasText: 'Not me' }).click(); await page.waitForTimeout(600);
ok(st.notme === 1 && /Taken out/.test(await heard.nth(1).textContent()) && /22 recordings/.test(await page.locator('.voiceprint-now').textContent()), '"Not me" takes one out');
ok(await page.locator('section[aria-label="Everyone\'s voice"]').isHidden(), 'only an admin sees everyone\'s state');
const fits = await page.$$eval('.settings-body *', (els) => els.every((e) => e.getBoundingClientRect().right <= innerWidth + 1));
ok(fits, 'everything fits the phone\'s width');

// Delete, after asking.
await page.locator('button', { hasText: 'Delete my voice print' }).click(); await page.waitForTimeout(400);
await page.locator('.ask-panel button[type=submit]').click();
await page.waitForTimeout(600);
ok(st.deleted === 1 && /was deleted/.test(await page.locator('.settings-body').textContent()) && /No voice print yet/.test(await page.locator('.voiceprint-now').textContent()), 'delete asks, then forgets it');
ok(st.errs.length === 0, `no page errors on the phone ${st.errs.join('|')}`);
await ctx.close();

// Desktop, as an admin whose print is recognising: beside the list.
({ page, ctx, st } = await open('#/settings/voice', false, true));
st.enrolled = true; st.active = true;
await page.reload(); await page.waitForTimeout(2500);
ok(await page.locator('.settings-layout .set-row[aria-current="page"]', { hasText: 'Your voice' }).count() === 1, 'desktop: marked in the list, its page beside it');
ok((await page.locator('.settings-layout .set-row', { hasText: 'Your voice' }).locator('.set-value').textContent()) === 'Recognising', 'the row says it is recognising');
const act = await page.locator('.voiceprint-now').textContent();
ok(/Recognising you/.test(act) && /50 of the last 50 times/.test(act) && !/Learning your voice/.test(act), `recognising, with its numbers (${act})`);
const ppl = await page.locator('section[aria-label="Everyone\'s voice"]').textContent();
ok(/Alex/.test(ppl) && /recognising/.test(ppl) && /Sam/.test(ppl) && /learning/.test(ppl) && /6 of the last 7/.test(ppl), `an admin sees everyone's state (${ppl})`);
await page.screenshot({ path: `${OUT}/your-voice-desktop.png` });
await page.locator('button', { hasText: 'Start learning again' }).click(); await page.waitForTimeout(400);
await page.locator('.ask-panel button[type=submit]').click(); await page.waitForTimeout(600);
ok(st.reset === 1 && /Learning your voice/.test(await page.locator('.voiceprint-now').textContent()), 'start learning again, after asking');
ok(st.errs.length === 0, `no page errors on the desktop ${st.errs.join('|')}`);
await browser.close();
