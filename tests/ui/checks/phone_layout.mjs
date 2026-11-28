// Home's phone layout (Greg, 28 Nov): "Same as desktop" (the desktop's widgets, sizes and order) or "Its own" (a list of its
// own: which widgets, their order, Compact or Full each). Switched in Home's Edit, on a phone and on a desktop, and in
// Settings › Home: one pref. The first switch to Its own copies what the phone showed; switching back keeps the list, so
// switching again restores it; editing one never touches the other.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const out = process.argv[2] || '.';
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}/`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// One login's prefs, kept here: what the page saves comes back with /api/v2/me, as on the server.
const store = {};
const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
await ctx.route('**/api/v2/me/prefs', async (route) => { Object.assign(store, JSON.parse(route.request().postData() || '{}')); await route.fulfill({ json: { ok: true } }); });
await ctx.route('**/api/v2/me', async (route) => { const res = await route.fetch(); const d = await res.json(); d.prefs = { ...d.prefs, ...JSON.parse(JSON.stringify(store)) }; await route.fulfill({ json: d }); });
const page = await ctx.newPage();
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
const shown = () => page.$$eval('.home-widgets > .home-widget:not(.off)', (els) => els.filter((e) => getComputedStyle(e).display !== 'none')
  .map((e) => ({ id: e.dataset.widget, size: e.classList.contains('size-compact') ? 'compact' : 'full' })));
const ids = (l) => l.map((i) => i.id);
const pressed = () => page.$eval('.home-phone-mode', (g) => (g.hidden ? null : g.querySelector('button[aria-pressed="true"]')?.dataset.mode));
const edit = async () => { await page.locator('.home-edit-link').click(); await page.waitForTimeout(500); };
const done = async () => { await page.locator('.home-edit-link').click(); await page.waitForTimeout(300); };
const wid = (id) => page.locator(`.home-widget[data-widget="${id}"]`);

// ── a phone, the same as the desktop (the default) ──
await page.goto(`${BASE}#/home`); await page.waitForTimeout(2500);
const cloned = await shown();
ok(cloned.length >= 4 && !('phone_layout' in store), `phone: the desktop's widgets, as before (${ids(cloned).join(' ')})`);
ok(await page.locator('.home-phone-mode').isHidden(), 'phone: no switch until Edit');
await edit();
ok(await pressed() === 'same', 'phone Edit: the bar has "Phone: Same as desktop | Its own", Same as desktop on');
ok(await page.locator('.home-phone-mode').evaluate((g) => { const r = g.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }), 'phone Edit: the switch fits the screen');
ok(await wid('lists').locator('.widget-step').count() === 2, 'phone Edit: each widget has Up and Down');

// ── Its own: the first time, a copy of what the phone shows ──
await page.locator('.home-phone-mode button[data-mode="own"]').click(); await page.waitForTimeout(500);
ok(store.phone_layout === 'own' && same(store.widget_phone, cloned), `the first switch copies the phone's view (${JSON.stringify(store.widget_phone)})`);
{ const now = await shown(), on = await pressed();
  ok(same(now, cloned) && on === 'own', `and the phone looks the same at first, Its own on (${JSON.stringify(now)} ${on})`); }
await page.screenshot({ path: `${out}/home-phone-own-edit.png` });

// ── editing the phone's own list: remove, Compact/Full, Up and Down, add ──
const desk0 = JSON.stringify({ widgets: store.widgets, widget_sizes: store.widget_sizes, widget_grid: store.widget_grid });
const first = cloned[0].id, gone = cloned[1].id;
await wid(gone).locator('.widget-remove').click(); await page.waitForTimeout(400);
await wid('lists').locator('.widget-size button', { hasText: 'Full' }).click(); await page.waitForTimeout(400);
await wid('lists').locator('.widget-step[aria-label$=" up"]').click(); await page.waitForTimeout(400);
ok(await page.evaluate(() => document.activeElement?.matches('.home-widget[data-widget="lists"] .widget-step')), 'Up keeps the focus on the moved widget');
await wid(first).locator('.widget-step[aria-label$=" down"]').click(); await page.waitForTimeout(400);
const addable = page.locator('.home-tray .home-add', { hasText: 'Month' });
const added = (await addable.textContent()).replace('+ ', '').trim();
await addable.click(); await page.waitForTimeout(600);
const mine = await shown();
ok(!ids(mine).includes(gone) && mine.find((i) => i.id === 'lists').size === 'full' && ids(mine)[1] === first, `the phone's list: one taken off, Lists Full, moved up and down (${ids(mine).join(' ')})`);
ok(mine.length === cloned.length, `and one added from the Add list (${added})`);
ok(same(store.widget_phone, mine), 'all of it kept in the phone\'s own list');
ok(JSON.stringify({ widgets: store.widgets, widget_sizes: store.widget_sizes, widget_grid: store.widget_grid }) === desk0, 'the desktop layout is untouched by the phone\'s edits');
await done();

// ── a reload keeps it ──
await page.reload(); await page.waitForTimeout(2500);
ok(same(await shown(), mine), 'a reload shows the phone\'s own list');

// ── back to Same as desktop: the cloned view, the phone's list kept ──
await edit();
await page.locator('.home-phone-mode button[data-mode="same"]').click(); await page.waitForTimeout(500);
ok(store.phone_layout === 'same' && same(await shown(), cloned), 'Same as desktop: the phone shows the desktop\'s view again');
ok(same(store.widget_phone, mine), 'and its own list is kept');
await page.locator('.home-phone-mode button[data-mode="own"]').click(); await page.waitForTimeout(500);
ok(store.phone_layout === 'own' && same(await shown(), mine) && same(store.widget_phone, mine), 'Its own again: the list comes back as it was (not copied again)');
await done();

// ── the desktop: its own grid, its own edits ──
await page.setViewportSize({ width: 1440, height: 900 }); await page.waitForTimeout(1000);
ok(same(ids(await shown()).sort(), ids(cloned).sort()), 'a window made desktop-wide shows the desktop\'s widgets');
await page.setViewportSize({ width: 412, height: 915 }); await page.waitForTimeout(1000);
ok(same(await shown(), mine), 'and phone-narrow again, the phone\'s own');
const desk = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await desk.route('**/api/v2/me/prefs', async (route) => { Object.assign(store, JSON.parse(route.request().postData() || '{}')); await route.fulfill({ json: { ok: true } }); });
await desk.route('**/api/v2/me', async (route) => { const res = await route.fetch(); const d = await res.json(); d.prefs = { ...d.prefs, ...JSON.parse(JSON.stringify(store)) }; await route.fulfill({ json: d }); });
const dp = await desk.newPage(); dp.on('pageerror', (e) => errs.push(e.message));
await dp.goto(`${BASE}#/home`); await dp.waitForTimeout(2500);
const deskShown = await dp.$$eval('.home-widgets > .home-widget:not(.off)', (els) => els.map((e) => e.dataset.widget));
ok(deskShown.includes(gone) && await dp.locator('.home-widgets.free').count() === 1, 'desktop: the grid, with the widget the phone took off');
await dp.locator('.home-edit-link').click(); await dp.waitForTimeout(600);
ok(await dp.$eval('.home-phone-mode', (g) => !g.hidden && g.querySelector('button[aria-pressed="true"]')?.dataset.mode) === 'own', 'desktop Edit: the same switch in the bar, Its own on');
ok(await dp.locator('.home-widget .widget-step').count() === 0, 'desktop Edit: no Up and Down (widgets are dragged on the grid)');
await dp.screenshot({ path: `${out}/home-desktop-edit-switch.png` });
const phone0 = JSON.stringify(store.widget_phone);
await dp.locator(`.home-widget[data-widget="${gone}"] .widget-size button`, { hasText: 'Compact' }).click(); await dp.waitForTimeout(400);
await dp.locator('.home-widget[data-widget="weather"] .widget-remove').click(); await dp.waitForTimeout(600);
ok(store.widget_sizes?.[gone] === 'compact' && Array.isArray(store.widgets) && !store.widgets.includes('weather'), 'desktop edits are kept in the desktop layout');
ok(JSON.stringify(store.widget_phone) === phone0, 'and never change the phone\'s own list');
await dp.locator('.home-edit-link').click(); await dp.waitForTimeout(300);

// ── Settings › Home: the same pref ──
await page.goto(`${BASE}#/settings/home`); await page.waitForTimeout(1500);
ok(await page.locator('input[name="phone_layout"][value="own"]').isChecked(), 'Settings › Home shows Its own, as switched on Home');
await page.locator('label', { has: page.locator('input[name="phone_layout"][value="same"]') }).click(); await page.waitForTimeout(500);
ok(store.phone_layout === 'same', 'Settings: Same as desktop saves the same pref');
await page.screenshot({ path: `${out}/settings-home-phone.png` });
await page.goto(`${BASE}#/home`); await page.reload(); await page.waitForTimeout(2500);     // a fresh load: the desktop's edits too
ok(!ids(await shown()).includes('weather') && ids(await shown()).includes(gone), 'and Home on the phone follows the desktop again');
await edit();
ok(await pressed() === 'same', 'with Same as desktop on in its Edit bar');
await page.goto(`${BASE}#/settings`); await page.waitForTimeout(1500);
ok((await page.locator('.set-row', { hasText: 'Home' }).locator('.set-value').textContent()) === 'Same on phone', 'the Settings list says it');
// A fresh login switching on the desktop first: the copy is what the phone would have shown.
for (const k of Object.keys(store)) delete store[k];
await dp.goto(`${BASE}#/settings/home`); await dp.reload(); await dp.waitForTimeout(1500);
await dp.locator('label', { has: dp.locator('input[name="phone_layout"][value="own"]') }).click(); await dp.waitForTimeout(500);
ok(store.phone_layout === 'own' && same(store.widget_phone, cloned), 'switched first in Settings (on a desktop), the phone\'s list starts as its cloned view');
ok(await dp.locator('.set-row[href="#/settings/home"] .set-value').textContent() === 'Phone: its own', 'and the list beside says so at once');
ok(errs.length === 0, `no page errors ${errs.join(' | ')}`);
await browser.close();
