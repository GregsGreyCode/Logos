// The Las Vegas sign: scrolling text between the date and the pencil on a wide screen, from your own lines and RSS feeds
// (Greg, 23 Nov).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const kind = (set_up) => ({ widgets: [{ id: 'marquee', name: 'Las Vegas sign', who: 'home', refresh: 120, set_up, can_set_up: true,
  about: 'A sign that scrolls across the top of Home.', fields: [{ key: 'text', label: 'Your own messages', kind: 'lines', value: '' },
    { key: 'feeds', label: 'Feeds (RSS or Atom)', kind: 'lines', value: '' }, { key: 'speed', label: 'Speed', kind: 'choice', options: [['medium', 'Medium'], ['slow', 'Slow'], ['fast', 'Fast']], value: 'medium' }] }] });
const DATA = { state: 'ok', summary: '', speed: 'medium', problems: [], at: Date.now() / 1000, items: [
  { text: 'Welcome home', source: 'text' }, { text: 'BTC $64,210 (+1.2%)', source: 'x' }, { text: 'ETH $3,120 (-0.4%)', source: 'x' }, { text: 'ACME 123.4 ▲ 0.8%', source: 'y' }] };

async function open(w, hgt, { on, setUp }) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt } });
  const st = { errs: [], put: null, puts: [] };
  page.on('pageerror', (e) => st.errs.push(e.message));
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs.widgets = on ? ['weather', 'marquee'] : ['weather']; me.prefs.widgets_v2 = true; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/widgets', (r) => r.fulfill({ json: kind(setUp) }));
  await page.route('**/api/v2/widgets/marquee', (r) => {
    if (r.request().method() === 'PUT') { st.put = r.request().postDataJSON(); return r.fulfill({ json: { ok: true, summary: '' } }); }
    return r.fulfill({ json: setUp ? DATA : { state: 'setup' } });
  });
  await page.route('**/api/v2/me/prefs', (r) => { if (r.request().method() === 'PUT') { st.puts.push(r.request().postDataJSON()); return r.fulfill({ json: { prefs: {} } }); } return r.continue(); });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await page.waitForTimeout(2500);
  return { page, st };
}

// wide: between the date and the pencil
let { page, st } = await open(1440, 900, { on: true, setUp: true });
const date = await page.locator('.home-date').boundingBox(), band = await page.locator('.home-band-widget').boundingBox(), pencil = await page.locator('.home-edit-link').boundingBox();
ok(band && band.x >= date.x + date.width && band.x + band.width <= pencil.x + 1, `the sign sits between the date and the pencil (${Math.round(date.x + date.width)} < ${Math.round(band.x)}–${Math.round(band.x + band.width)} < ${Math.round(pencil.x)})`);
ok(Math.abs((band.y + band.height / 2) - (date.y + date.height / 2)) < 20, 'on the same line as the date');
ok(/BTC \$64,210/.test(await page.locator('.mq').textContent()) && await page.locator('.mq-run').count() === 2, 'it carries the items, run twice end to end so it loops without a jump');
const x1 = await page.locator('.mq-track').evaluate((e) => new DOMMatrix(getComputedStyle(e).transform).m41); await page.waitForTimeout(900);
const x2 = await page.locator('.mq-track').evaluate((e) => new DOMMatrix(getComputedStyle(e).transform).m41);
ok(x2 < x1, `and it scrolls left (${Math.round(x1)} → ${Math.round(x2)})`);
await page.locator('.mq').hover(); await page.waitForTimeout(200);
const p1 = await page.locator('.mq-track').evaluate((e) => new DOMMatrix(getComputedStyle(e).transform).m41); await page.waitForTimeout(700);
const p2 = await page.locator('.mq-track').evaluate((e) => new DOMMatrix(getComputedStyle(e).transform).m41);
ok(Math.abs(p2 - p1) < 0.5, 'pointing at it pauses it');
ok(await page.locator('.mq').getAttribute('role') === 'marquee' && /Welcome home/.test(await page.locator('.mq').getAttribute('aria-label')), 'named for a screen reader');
await page.screenshot({ path: `${OUT}/las-vegas-sign.png`, clip: { x: 0, y: 0, width: 1440, height: 140 } });

// arranging: take it off, put it back
await page.locator('.home-edit-link').click(); await page.waitForTimeout(400);
ok(await page.locator('.home-band-widget .widget-bar .linkish', { hasText: 'Edit' }).count() === 1 && await page.locator('.home-band-widget .widget-remove').count() === 1, 'arranging: the sign has Edit and ✕');
await page.locator('.home-band-widget .widget-remove').click(); await page.waitForTimeout(500);
ok(st.puts.at(-1).widgets && !st.puts.at(-1).widgets.includes('marquee') && await page.locator('.home-band-widget').isHidden(), '✕ takes it off Home (kept with your login)');
await page.locator('.home-add', { hasText: 'Las Vegas sign' }).click(); await page.waitForTimeout(600);
ok(st.puts.at(-1).widgets.includes('marquee') && await page.locator('.home-band-widget').isVisible(), 'and Add puts it back');
ok(st.errs.length === 0, `no page errors ${st.errs.join('|')}`);
await page.close();

// not set up yet: the way to set it up, in the band
({ page, st } = await open(1440, 900, { on: true, setUp: false }));
ok(await page.locator('.home-band-widget .w-card.setup button', { hasText: 'Set it up' }).count() === 1, 'not set up: a small "Set it up" in the band');
await page.locator('.home-band-widget').getByRole('button', { name: 'Set it up' }).click(); await page.waitForTimeout(400);
await page.locator('.w-setup textarea').first().fill('Welcome home\nBins out tonight');
await page.locator('.w-setup textarea').nth(1).fill('https://example.com/stocks.rss');
await page.locator('.w-setup').getByRole('button', { name: 'Save' }).click(); await page.waitForTimeout(800);
ok(st.put && st.put.fields.text.includes('Bins out') && st.put.fields.feeds === 'https://example.com/stocks.rss' && st.put.fields.speed === 'medium', 'the setup sheet takes your lines and your feed address');
await page.close();

// phone: no sign
({ page, st } = await open(412, 915, { on: true, setUp: true }));
ok(await page.locator('.home-band').isHidden(), 'on a phone the sign is not shown');
await page.close();
await browser.close();
