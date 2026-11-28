import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
const widths = new Set(); const bands = {};
for (const hash of ['#/home', '#/chat', '#/noticeboard', '#/brain/greg', '#/agents/greg', '#/settings', '#/people']) {
  await page.goto('http://127.0.0.1:8779/' + hash); await page.waitForTimeout(1800);
  widths.add(Math.round((await page.locator('.nav').boundingBox()).width));
  bands[hash] = await page.evaluate(() => { const h = [...document.querySelectorAll('#view h1')].find((e) => e.offsetParent && e.getBoundingClientRect().top < 60);
    return h ? `${h.textContent.trim().slice(0, 20)}@${Math.round(h.getBoundingClientRect().top)}` : 'none'; });
  await page.screenshot({ path: `${process.argv[2]}/desk/b1-${hash.replace(/[#/]/g, '') || 'x'}.png` });
}
ok(widths.size === 1, `one menu width everywhere (${[...widths]})`);
for (const [k, v] of Object.entries(bands)) ok(v !== 'none', `${k} has a title in the band: ${v}`);
const labels = await page.$$eval('.nav-links a:not([hidden]) .nav-label, .nav-group:not([hidden]) > .nav-group-name', (els) => els.map((e) => e.textContent));
ok(JSON.stringify(labels) === JSON.stringify(['Home', 'Agents', 'Notifications', 'Calendar', 'Brain', 'Routines', 'Settings', 'Manage', 'People', 'Models', 'Claude Advisor']), `menu: ${labels}`);
const ph = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
await ph.goto('http://127.0.0.1:8779/#/noticeboard'); await ph.waitForTimeout(1800);
ok(await ph.locator('.feed-allread').isVisible(), 'phone: Mark all read still there');
ok(!(await ph.locator('.page-band h1').isVisible()), 'phone: no second title under the top bar');
await ph.screenshot({ path: `${process.argv[2]}/desk/b1-phone-nb.png` });
ok(errs.filter((e) => !/month|model_server/.test(e)).length === 0, `no new page errors ${errs.join('|')}`);
await browser.close();
