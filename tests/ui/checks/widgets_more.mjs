// The second batch of homelab widgets (F42): Proxmox, Backups, Ad blocking, Now playing, Downloads,
// Bookmarks, each drawn from stand-in numbers, on a phone and a desktop.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const now = Date.now() / 1000;
const kind = (id, name, extra = {}) => ({ id, name, who: 'home', refresh: 60, set_up: true, can_set_up: true, fields: [], ...extra });
const KINDS = { widgets: [kind('proxmox', 'Proxmox', { uses: 'servers' }), kind('backups', 'Backups'), kind('adblock', 'Ad blocking'),
  kind('playing', 'Now playing'), kind('downloads', 'Downloads'),
  kind('bookmarks', 'Bookmarks', { set_up: false, about: 'Your services, a tap away.',
    fields: [{ key: 'links', label: 'Links', kind: 'lines', value: '' }] })] };
const DATA = {
  proxmox: { state: 'ok', summary: '2 running · 1 stopped', at: now, items: [
    { id: 101, name: 'homeassistant', running: true, kind: 'vm', cpu: 3, mem_gb: 2 }, { id: 110, name: 'logos', running: true, kind: 'vm', cpu: 9, mem_gb: 6.1 },
    { id: 130, name: 'test', running: false, kind: 'container' }] },
  backups: { state: 'needs', summary: 'Photos to NAS: failed', at: now, items: [
    { name: 'Photos to NAS', ran: now - 3 * 86400, ok: false, detail: 'failed' }, { name: 'Logos export', ran: now - 7200, ok: true, detail: '' }] },
  adblock: { state: 'ok', summary: 'Pi-hole', at: now, percent: 23, blocked: 4812, total: 20940, top: ['ads.example', 'telemetry.example'] },
  playing: { state: 'ok', summary: '2 playing', at: now, items: [
    { title: '[Film title]', where: 'Living room TV', who: 'Béré', paused: false, position: 3720, length: 6420, percent: 58 },
    { title: '[Series] · S2 E4', where: "Greg's laptop", who: 'Greg', paused: true, position: 600, length: 2800, percent: 21 }] },
  downloads: { state: 'needs', summary: '1 stalled', at: now, upcoming: 4, items: [
    { title: '[Episode] S01E05', app: 'sonarr', percent: 72, left: '00:04:00', waiting: false, stalled: false },
    { title: '[Film]', app: 'radarr', percent: 0, left: '', waiting: true, stalled: false },
    { title: '[Episode] S01E06', app: 'sonarr', percent: 0, left: '', waiting: false, stalled: true }] },
  bookmarks: { state: 'setup' },
};

for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  let marks = false;
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs.widgets = ['backups', 'downloads', 'proxmox', 'adblock', 'playing', 'bookmarks']; me.prefs.widgets_v2 = true; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/widgets', (r) => r.fulfill({ json: KINDS }));
  await page.route(/\/api\/v2\/widgets\/(\w+)$/, (r) => {
    const id = r.request().url().split('/').pop();
    if (r.request().method() === 'PUT') { marks = true; return r.fulfill({ json: { ok: true, summary: '' } }); }
    return r.fulfill({ json: id === 'bookmarks' && marks ? { state: 'ok', summary: '', at: now, items: [
      { name: 'Proxmox', url: 'https://pve:8006' }, { name: 'Home Assistant', url: 'http://ha:8123' }, { name: 'Jellyfin', url: 'http://jf' }] } : DATA[id] });
  });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2500);

  ok(await page.locator('section.w-proxmox .w-guest').count() === 3 && await page.locator('section.w-proxmox .w-guest.off').count() === 1,
    `${n}: Proxmox: each guest, the stopped one dashed`);
  ok((await page.locator('section.w-backups .w-summary').textContent()) === 'Photos to NAS: failed'
    && (await page.locator('section.w-backups .w-row').first().textContent()).includes('Photos to NAS'), `${n}: Backups: the failed one first, and said`);
  ok(await page.locator('section.w-adblock svg.w-ring').count() === 1 && /23%/.test(await page.locator('section.w-adblock').textContent()), `${n}: Ad blocking: the ring and 23%`);
  ok(/1 h 02 of 1 h 47/.test(await page.locator('section.w-playing').textContent()) && await page.locator('section.w-playing .w-play.paused').count() === 1,
    `${n}: Now playing: how far in, and paused`);
  ok(/72% · 4 min left/.test(await page.locator('section.w-downloads .w-dl').first().textContent()), `${n}: Downloads: how long is left, in words`);
  ok(await page.locator('section.w-adblock').evaluate((e) => getComputedStyle(e).flexDirection === 'column' && e.getBoundingClientRect().width > 300), `${n}: Ad blocking takes its full width`);
  ok(await page.locator('section.w-downloads .w-dl.stalled').count() === 1 && /4 episodes/.test(await page.locator('section.w-downloads').textContent()),
    `${n}: Downloads: stalled in red, and what is coming`);
  ok(await page.locator('section.w-bookmarks .w-card.setup').count() === 1, `${n}: Bookmarks not set up yet`);
  await page.locator('section.w-bookmarks').getByRole('button', { name: 'Set it up' }).click(); await page.waitForTimeout(300);
  await page.locator('.w-setup textarea').fill('Proxmox, https://pve:8006\nHome Assistant, http://ha:8123\nJellyfin, http://jf');
  await page.locator('.w-setup').getByRole('button', { name: 'Save' }).click(); await page.waitForTimeout(1200);
  ok(await page.locator('section.w-bookmarks .w-mark').count() === 3 && (await page.locator('section.w-bookmarks .w-mark-tile').first().textContent()) === 'P',
    `${n}: Bookmarks: set up, a tile each`);
  const fits = await page.$$eval('section.home-widget-kind', (els) => els.every((e) => e.getBoundingClientRect().right <= innerWidth + 0.5));
  ok(fits, `${n}: every widget fits the screen`);
  await page.screenshot({ path: `${OUT}/widgets-more-${n}.png`, fullPage: true });

  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await page.close();
}
await browser.close();
