// Reactions (Greg, 23 Nov): tapping your own takes it back, at once and once, even when the chat library
// never marks it redacted (as on his phone); then the same emoji can be put back, to ask again.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2500);

const mine = page.locator('.mx-turn.me').first();
const chip = () => mine.locator('.mx-react', { hasText: '🔁' });
await mine.locator('.mx-react-open').evaluate((b) => b.click()); await page.waitForTimeout(300);
await page.locator('.mx-picker button', { hasText: '🔁' }).first().click(); await page.waitForTimeout(500);
ok(await chip().count() === 1 && (await chip().getAttribute('aria-pressed')) === 'true', '🔁 on your own message shows, as yours');

await chip().tap(); await page.waitForTimeout(500);
const red = await page.evaluate(() => window.__mx.redacted || []);
ok(await chip().count() === 0, 'tapping it takes it back, and it goes at once');
ok(red.length === 1, `one take-back sent (${red.length})`);

await mine.locator('.mx-react-open').evaluate((b) => b.click()); await page.waitForTimeout(300);
await page.locator('.mx-picker button', { hasText: '🔁' }).first().click(); await page.waitForTimeout(500);
const sent = await page.evaluate(() => (window.__mx.events || []).filter(([t, c]) => t === 'm.reaction' && c['m.relates_to'].key === '🔁').length);
ok(await chip().count() === 1 && sent === 2, 'and it can be put back, which asks again');
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
