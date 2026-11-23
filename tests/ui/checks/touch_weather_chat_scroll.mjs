import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
// 1. phone: no selecting Home's text; still selecting in a field
const ph = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
await ph.goto('http://127.0.0.1:8779/#/home'); await ph.waitForTimeout(2200);
ok(await ph.evaluate(() => getComputedStyle(document.querySelector('.home-weather')).userSelect === 'none'), 'phone: Home text cannot be selected');
ok(await ph.evaluate(() => getComputedStyle(document.querySelector('#home-ask')).userSelect !== 'none'), 'phone: the Ask box still can');
// 2. weather says everything
const cut = await ph.$$eval('.home-weather-text > span', (els) => els.some((e) => e.scrollWidth > e.clientWidth + 1));
ok(!cut, 'phone: the weather is not cut off');
await ph.locator('.home-weather').screenshot({ path: process.argv[2] + '/weather-phone.png' });
// desktop keeps normal selection
const dk = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await dk.goto('http://127.0.0.1:8779/#/home'); await dk.waitForTimeout(1800);
ok(await dk.evaluate(() => getComputedStyle(document.querySelector('.home-weather')).userSelect !== 'none'), 'desktop: text can still be selected');
// 3. chat stays at the newest message as it grows
await dk.goto('http://127.0.0.1:8779/#/chat/greg'); await dk.waitForTimeout(2500);
await dk.evaluate(() => { const log = document.querySelector('.chat .log'); for (let i = 0; i < 40; i++) { const d = document.createElement('div'); d.style.height = '60px'; d.textContent = 'filler ' + i; log.append(d); } });
await dk.waitForTimeout(300);
const gap = await dk.evaluate(() => { const l = document.querySelector('.chat .log'); return l.scrollHeight - l.scrollTop - l.clientHeight; });
ok(gap < 5, `desktop chat: still at the bottom after it grew (${gap}px short)`);
await dk.evaluate(() => { const l = document.querySelector('.chat .log'); l.scrollTop = 0; l.dispatchEvent(new Event('scroll')); }); await dk.waitForTimeout(100);
await dk.evaluate(() => { const l = document.querySelector('.chat .log'); const d = document.createElement('div'); d.style.height = '60px'; l.append(d); });
await dk.waitForTimeout(300);
ok(await dk.evaluate(() => document.querySelector('.chat .log').scrollTop) < 50, 'desktop chat: scrolled up to read, it is left where you are');
await browser.close();
