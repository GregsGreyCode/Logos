// Homelab widgets on Home (F42): the same states for every kind, and setting one up from Home.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const now = Date.now() / 1000;

const KINDS = (serversSetUp) => ({ widgets: [
  { id: 'services', name: 'Services', who: 'home', refresh: 60, set_up: true, can_set_up: true, about: 'Whether each of your services answers.',
    fields: [{ key: 'services', label: 'Services', kind: 'lines', value: 'Nextcloud, http://nc' }] },
  { id: 'servers', name: 'Servers', who: 'admin', refresh: 30, set_up: serversSetUp, can_set_up: true, about: 'CPU, memory, disk and uptime of each Proxmox node.',
    fields: [{ key: 'url', label: 'Proxmox address', kind: 'url', value: '' }, { key: 'token_id', label: 'API token ID', kind: 'text', value: '' },
      { key: 'secret', label: 'Token secret', kind: 'secret', set: false }, { key: 'own_certificate', label: 'Proxmox uses its own certificate', kind: 'check', value: false }] },
] });
const SERVICES = { state: 'needs', summary: '1 down', at: now, down: 1, items: [
  { name: 'Nextcloud', url: 'http://nc', up: false, detail: 'not answering', since: now - 720 },
  { name: 'Immich', url: 'http://im', up: true, ms: 2400, slow: true },
  { name: 'Home Assistant', url: 'http://ha', up: true, ms: 84 }] };
const SERVERS = { state: 'ok', summary: '1 node', at: now, items: [
  { name: 'pve', online: true, cpu: 18, mem: 64, mem_gb: 41, mem_total_gb: 64, disk: 86, uptime: 23 * 86400 }] };

for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  let serversSetUp = false, put = null, stale = false;
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs.widgets = ['services', 'servers', 'weather']; me.prefs.widgets_v2 = true; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/widgets', (r) => r.fulfill({ json: KINDS(serversSetUp) }));
  await page.route('**/api/v2/widgets/services', (r) => r.fulfill({ json: stale ? { ...SERVICES, state: 'ok', stale: true, at: now - 1200, error: 'Nothing answered at that address.' } : SERVICES }));
  await page.route('**/api/v2/widgets/servers', (r) => {
    if (r.request().method() === 'PUT') { put = r.request().postDataJSON(); serversSetUp = true; return r.fulfill({ json: { ok: true, summary: '1 node' } }); }
    return r.fulfill({ json: serversSetUp ? SERVERS : { state: 'setup' } });
  });
  await page.route('**/api/v2/widgets/servers/test', (r) => r.fulfill({ json: { ok: false, error: 'Proxmox refused the token: check the token ID and secret.' } }));
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2500);

  const svc = page.locator('section.w-services');
  ok(await svc.count() === 1 && (await svc.locator('.w-summary').textContent()) === '1 down', `${n}: Services on Home, "1 down"`);
  ok(await svc.evaluate((e) => e.classList.contains('needs')), `${n}: needing you, it is marked`);
  const rows = await svc.locator('.w-row .w-name').allTextContents();
  ok(rows[0] === 'Nextcloud' && /not answering · 12 min/.test(await svc.locator('.w-row.down .w-meta').textContent()), `${n}: the problem first, with how long (${rows.join(', ')})`);
  ok(/slow · 2.4 s/.test(await svc.locator('.w-row.slow .w-meta').textContent()), `${n}: a slow one says so`);

  const srv = page.locator('section.w-servers');
  ok(await srv.locator('.w-card.setup').count() === 1, `${n}: Servers not set up: a dashed card with what it shows`);
  await srv.getByRole('button', { name: 'Set it up' }).click(); await page.waitForTimeout(400);
  const sheet = page.locator('.w-setup');
  ok(await sheet.count() === 1 && await sheet.locator('input[type="password"]').count() === 1, `${n}: the setup sheet asks for the address, token and secret`);
  await sheet.locator('input[type="url"]').fill('https://192.0.2.10:8006');
  await sheet.locator('input[type="text"]').fill('logos@pve!home');
  await sheet.locator('input[type="password"]').fill('wrong');
  await sheet.getByRole('button', { name: 'Test' }).click(); await page.waitForTimeout(400);
  ok(/refused the token/.test(await sheet.locator('[role="status"]').textContent()), `${n}: Test says what is wrong, in plain words`);
  await page.screenshot({ path: `${OUT}/widgets-setup-${n}.png` });
  await sheet.locator('input[type="password"]').fill('s3cret');
  await sheet.locator('input[type="checkbox"]').check();
  await sheet.getByRole('button', { name: 'Save' }).click(); await page.waitForTimeout(1200);
  ok(put && put.fields.secret === 's3cret' && put.fields.own_certificate === true && await page.locator('.w-setup').count() === 0, `${n}: Save sends it and closes`);
  ok(await srv.locator('.w-meter').count() === 3 && /41 of 64 GB/.test(await srv.textContent()), `${n}: then the node's CPU, memory and disk`);
  ok(await srv.locator('.w-meter.warn').count() === 1, `${n}: a disk at 86% shows amber`);
  const fits = await page.$$eval('section.home-widget-kind', (els) => els.every((e) => e.getBoundingClientRect().right <= innerWidth));
  ok(fits, `${n}: the widgets fit the screen`);
  await page.screenshot({ path: `${OUT}/widgets-${n}.png`, fullPage: true });

  // The source out of reach: the last copy, dimmed, with its age.
  stale = true;
  await page.evaluate(() => { for (const k of Object.keys(localStorage)) if (k.includes('/api/v2/widgets/')) localStorage.removeItem(k); });
  await page.reload(); await page.waitForTimeout(2500);
  ok(/^as of /.test(await page.locator('section.w-services .w-summary').textContent())
    && /Nothing answered/.test(await page.locator('section.w-services .w-stale').textContent()), `${n}: out of reach: "as of …", why, and trying again`);
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
