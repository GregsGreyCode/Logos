// The third batch of homelab widgets (F42): Energy and UPS, Network, Cameras; Plex as a choice.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const now = Date.now() / 1000;
// a 1×1 grey JPEG, for the snapshots
const JPEG = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');
const kind = (id, name, extra = {}) => ({ id, name, who: 'home', refresh: 60, set_up: true, can_set_up: true, fields: [], ...extra });
const KINDS = { widgets: [kind('energy', 'Energy'), kind('network', 'Network'), kind('cameras', 'Cameras'),
  kind('playing', 'Now playing', { set_up: false, fields: [
    { key: 'product', label: 'Which', kind: 'choice', options: [['jellyfin', 'Jellyfin'], ['plex', 'Plex']], value: '' },
    { key: 'url', label: 'Address', kind: 'url', value: '' }, { key: 'api_key', label: 'API key or token', kind: 'secret', set: false }] })] };
const DATA = {
  energy: { state: 'needs', summary: 'UPS on battery · 38 min left', at: now, power_w: 412, energy_kwh: 6.8, cost: 1.71,
    ups: { battery: 64, status: 'On Battery', on_battery: true, minutes: 38 } },
  network: { state: 'ok', summary: 'Speedtest Tracker', at: now, down: 512, up: 96, ping: 9, tested: '2026-11-23 07:00:00' },
  cameras: { state: 'ok', summary: '2 cameras', at: now, items: [{ name: 'front_door', label: 'person', at: now - 3600 }, { name: 'hallway', label: '', at: null }] },
  playing: { state: 'setup' },
};

for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  let snaps = 0, put = null;
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs.widgets = ['energy', 'network', 'cameras', 'playing']; me.prefs.widgets_v2 = true; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/widgets', (r) => r.fulfill({ json: KINDS }));
  await page.route('**/api/v2/widgets/cameras/snapshot/**', (r) => { snaps += 1; return r.fulfill({ body: JPEG, contentType: 'image/jpeg' }); });
  await page.route(/\/api\/v2\/widgets\/(\w+)$/, (r) => {
    const id = r.request().url().split('/').pop();
    if (r.request().method() === 'PUT') { put = r.request().postDataJSON(); return r.fulfill({ json: { ok: true, summary: 'nothing playing' } }); }
    return r.fulfill({ json: DATA[id] });
  });
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2500);

  const en = page.locator('section.w-energy');
  ok(/412\s*W/.test(await en.textContent()) && /about £1\.71/.test(await en.textContent()), `${n}: Energy: now, today and its cost`);
  ok(await en.evaluate((e) => e.classList.contains('needs')) && (await en.locator('.w-pill').textContent()) === 'On battery'
    && /38 min/.test(await en.locator('.w-summary').textContent()), `${n}: the UPS on battery needs you, with the minutes left`);
  ok(/512\s*Mbit\/s/.test(await page.locator('section.w-network').textContent()) && await page.locator('section.w-network .w-fig').count() === 3,
    `${n}: Network: down, up and ping`);
  ok(await page.locator('section.w-cameras img').count() === 2 && snaps >= 2, `${n}: Cameras: a snapshot each, through Logos`);
  ok(/front door · person/.test(await page.locator('section.w-cameras .w-cam-label').first().textContent())
    && /hallway · quiet/.test(await page.locator('section.w-cameras .w-cam-label').nth(1).textContent()), `${n}: each with its latest event`);

  await page.locator('section.w-playing').getByRole('button', { name: 'Set it up' }).click(); await page.waitForTimeout(300);
  await page.locator('.w-setup').getByText('Plex', { exact: true }).click();
  await page.locator('.w-setup input[type="url"]').fill('http://192.0.2.30:32400');
  await page.locator('.w-setup input[type="password"]').fill('tok');
  await page.locator('.w-setup').getByRole('button', { name: 'Save' }).click(); await page.waitForTimeout(800);
  ok(put && put.fields.product === 'plex' && put.fields.api_key === 'tok', `${n}: Now playing can be set up for Plex`);
  const fits = await page.$$eval('section.home-widget-kind', (els) => els.every((e) => e.getBoundingClientRect().right <= innerWidth + 0.5));
  ok(fits, `${n}: every widget fits the screen`);
  await page.screenshot({ path: `${OUT}/widgets-third-${n}.png`, fullPage: !mob });
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
