// Home's desktop grid (F49, Greg 23 Nov): widgets can be placed anywhere and resized; nothing overlaps; the layout is kept;
// a phone keeps its plain list. First the layout maths (no browser), then drag, resize, fit, reset and a reload.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const G = await import(new URL('../../../gateway/ui/home-grid.js', import.meta.url));
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── the maths ──
const A = (id, x, y, w, h) => ({ id, x, y, w, h });
const free = (items) => items.every((a, i) => items.every((b, j) => i === j || !(a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y)));
ok(same(G.compact([A('a', 0, 5, 6, 10)]).map((i) => i.y), [0]), 'a lone widget packs up to the top');
let r = G.compact([A('a', 0, 0, 6, 20), A('b', 6, 0, 6, 30), A('c', 0, 40, 6, 10)]);
ok(r.find((i) => i.id === 'c').y === 20, 'a widget with a gap above it falls up under the one above');
r = G.compact([A('a', 0, 0, 6, 20), A('b', 6, 0, 6, 30), A('c', 0, 20, 6, 10)].map((i) => (i.id === 'c' ? { ...i, x: 6, y: 5 } : i)), 'c');
ok(free(r) && r.find((i) => i.id === 'c').y <= 5, 'dropping one onto another makes room: no overlap, the dropped one keeps its place');
r = G.compact([A('a', 0, 0, 12, 10), A('b', 0, 0, 6, 10)], 'b');
ok(free(r), 'two on the same spot are separated');
r = G.compact([A('a', 0, 0, 6, 10), A('n', null, 0, 6, 8)]);
ok(free(r) && r.find((i) => i.id === 'n').x === 6 && r.find((i) => i.id === 'n').y === 0, 'a new widget takes the first free spot (the empty column)');
r = G.compact([A('a', 9, 0, 6, 10)]);
ok(r[0].x === 6, 'a widget too far right is pulled back inside the 12 columns');
ok(G.defaultWidth(1400) === 6 && G.defaultWidth(1920) === 4 && G.defaultWidth(2560) === 3 && G.minWidth('weather') === 4 && G.minWidth('sign') === 3, 'default and minimum widths');
ok(same(G.readingOrder({ a: { x: 6, y: 0 }, b: { x: 0, y: 0 }, c: { x: 0, y: 30 } }, ['c', 'a', 'b']), ['b', 'a', 'c']), 'the phone order follows the layout, down then across');
ok(same(G.clean({ a: { x: 1, y: 2, w: 3, h: 4, junk: 1 }, b: { x: 'x' } }, ['a', 'b', 'z']), { a: { x: 1, y: 2, w: 3, h: 4 } }), 'only plain numbers are kept');

// ── the page ──
const browser = await chromium.launch({ executablePath: process.env.CHROME });
let saved = null;
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.route('**/api/v2/me/prefs', async (route) => { const b = JSON.parse(route.request().postData() || '{}'); if (b.widget_grid) saved = b.widget_grid; await route.fulfill({ json: { ok: true } }); });
await page.route('**/api/v2/me', async (route) => { const res = await route.fetch(); const d = await res.json(); if (saved) d.prefs = { ...d.prefs, widget_grid: saved }; await route.fulfill({ json: d }); });
const rects = () => page.evaluate(() => Object.fromEntries([...document.querySelectorAll('.home-widgets > .home-widget')].filter((e) => e.offsetParent).map((e) => { const b = e.getBoundingClientRect(); return [e.dataset.widget, { x: b.x, y: b.y, w: b.width, h: b.height, r: b.right, b: b.bottom }]; })));
const clear = (rs) => { const v = Object.values(rs); return v.every((a, i) => v.every((b, j) => i === j || a.r <= b.x + 1 || b.r <= a.x + 1 || a.b <= b.y + 1 || b.b <= a.y + 1)); };
await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(3000);
ok(await page.locator('.home-widgets.free').count() === 1, 'desktop: Home uses the free grid');
let rs = await rects();
ok(Object.keys(rs).length >= 4 && clear(rs), `desktop: ${Object.keys(rs).length} widgets laid out with no overlap`);
await page.locator('.home-edit-link').click(); await page.waitForTimeout(700);
ok(await page.locator('.grip-layer .widget-grip').count() >= 4 && await page.locator('.home-widget .widget-grip').count() === 0, 'desktop: Edit shows a resize corner on each widget, in a layer of its own (never inside a widget)');
{ const w = await page.locator('.home-widget[data-widget="weather"]').boundingBox(), g = await page.locator('.grip-layer .widget-grip[data-widget="weather"]').boundingBox();
  const dx = Math.abs(g.x + g.width / 2 - (w.x + w.width)), dy = Math.abs(g.y + g.height / 2 - (w.y + w.height));
  ok(dx <= 6 && dy <= 6, `the corner sits on its widget's corner (${Math.round(dx)}, ${Math.round(dy)} px off)`); }

// drag Lists (by its handle) into the right-hand column, near the top
rs = await rects(); const before = rs.lists;
const handle = page.locator('.home-widget[data-widget="lists"] .widget-handle');
const hb = await handle.boundingBox();
await page.mouse.move(hb.x + 10, hb.y + 10); await page.mouse.down();
const toRight = before.x < 700, tx = toRight ? 1000 : 420;                  // to the other column
await page.mouse.move(hb.x + (toRight ? 200 : -200), hb.y - 40, { steps: 6 }); await page.mouse.move(tx, 140, { steps: 10 }); await page.waitForTimeout(250);
await page.mouse.up(); await page.waitForTimeout(700);
rs = await rects();
ok(Math.abs(rs.lists.x - before.x) > 300 && clear(rs), `a widget dragged to the other column goes there (x ${Math.round(before.x)} to ${Math.round(rs.lists.x)}), nothing overlaps`);
ok(saved && saved.lists && (toRight ? saved.lists.x >= 6 : saved.lists.x < 6), `and the layout is kept (${saved && JSON.stringify(saved.lists)})`);

// resize Lists wider and taller with its grip
const wBefore = rs.lists.w, hBefore = rs.lists.h;
const grip = page.locator('.grip-layer .widget-grip[data-widget="lists"]'); const gb = await grip.boundingBox();
await page.mouse.move(gb.x + 14, gb.y + 14); await page.mouse.down(); await page.mouse.move(gb.x + 60, gb.y + 60, { steps: 5 });
await page.mouse.move(gb.x - 160, gb.y + 150, { steps: 8 }); await page.mouse.up(); await page.waitForTimeout(700);
rs = await rects();
ok(rs.lists.h > hBefore + 60 && clear(rs), `the grip resizes: taller (${Math.round(hBefore)} to ${Math.round(rs.lists.h)}px), still no overlap`);
ok(saved.lists.h > 0, `a stretched height is kept (${saved.lists.h} rows)`);
ok(await page.evaluate(() => [...document.querySelectorAll('.home-widgets > .home-widget')].every((e) => e.offsetWidth - e.clientWidth <= 0)), 'a stretched widget shows no scrollbar');
{ const w = await page.locator('.home-widget[data-widget="lists"]').boundingBox(), g = await page.locator('.grip-layer .widget-grip[data-widget="lists"]').boundingBox();
  ok(Math.abs(g.x + g.width / 2 - (w.x + w.width)) <= 6 && Math.abs(g.y + g.height / 2 - (w.y + w.height)) <= 6, 'and its corner is still there, not clipped by it'); }
await page.locator('.grip-layer .widget-grip[data-widget="lists"]').dblclick(); await page.waitForTimeout(700);
ok(saved.lists.h === 0, 'double-clicking the grip fits the widget to its content again');
// the narrowest it can be
const g2 = await page.locator('.grip-layer .widget-grip[data-widget="weather"]').boundingBox();
await page.mouse.move(g2.x + 14, g2.y + 14); await page.mouse.down(); await page.mouse.move(g2.x - 300, g2.y + 14, { steps: 8 }); await page.mouse.up(); await page.waitForTimeout(600);
ok(saved.weather.w >= 4, `a widget will not shrink past its minimum width (${saved.weather.w} columns)`);

// a reload keeps it
await page.locator('.home-edit-link').click(); await page.waitForTimeout(400);
const keep = (await rects()).lists;
await page.reload(); await page.waitForTimeout(3000);
const again = (await rects()).lists;
ok(Math.abs(again.x - keep.x) < 6 && Math.abs(again.y - keep.y) < 6, `after a reload the widget is where it was (${Math.round(keep.x)},${Math.round(keep.y)} to ${Math.round(again.x)},${Math.round(again.y)})`);
// reset
await page.locator('.home-edit-link').click(); await page.waitForTimeout(500);
await page.getByRole('button', { name: 'Reset layout' }).click(); await page.waitForTimeout(700);
const rr = await rects();
ok(rr.lists.x < 600 || rr.weather.x < 600, 'Reset layout puts the widgets back in their two columns');
await page.screenshot({ path: `${process.argv[2] || 'out'}/home-grid-desktop.png` });
ok(errs.length === 0, `no page errors ${errs.slice(0, 2).join('; ')}`);
await ctx.close();

// ── a phone keeps its plain list ──
const phone = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
await phone.goto('http://127.0.0.1:8779/#/home'); await phone.waitForTimeout(2800);
ok(await phone.locator('.home-widgets.free').count() === 0, 'phone: no free grid');
await browser.close();
