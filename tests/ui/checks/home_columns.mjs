import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2500);
const boxes = await page.$$eval('.home-widgets > .home-widget:not([hidden]):not(.off)', (els) => els.map((e) => { const r = e.getBoundingClientRect(); return { w: e.dataset.widget, x: Math.round(r.left), top: Math.round(r.top), bottom: Math.round(r.bottom) }; }));
const cols = {}; for (const b of boxes) (cols[b.x] ||= []).push(b);
let maxGap = 0;
for (const list of Object.values(cols)) { list.sort((a, b) => a.top - b.top); for (let i = 1; i < list.length; i++) maxGap = Math.max(maxGap, list[i].top - list[i - 1].bottom); }
ok(Object.keys(cols).length === 2, `two columns (${Object.values(cols).map((l) => l.map((b) => b.w).join(',')).join(' | ')})`);
ok(maxGap <= 30, `no gap over 30px in either column (largest ${maxGap})`);
await page.screenshot({ path: process.argv[2] + '/desk/b2-home.png' });
await browser.close();
