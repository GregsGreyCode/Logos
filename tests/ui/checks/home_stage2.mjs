// Home's widgets, stage 2 (F49, Greg 24 Nov), on a phone and a desktop: (1) every widget Compact or Full, kept per person;
// (2) each widget's own minimum size on the desktop grid; (3) Weather in more than one place, in an order you keep;
// (4) a ghost outline and snap guides while dragging, and Tidy up. And (27 Nov) each agent in Agents today opens its chat.
import { createRequire } from 'module';
import { routeAll, ALL, PLACES } from '../widget-fixtures.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const G = await import(new URL('../../../gateway/ui/home-grid.js', import.meta.url));
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const URL_HOME = 'http://127.0.0.1:8779/#/home';

// ── the Tidy up maths ──
{
  const items = [{ id: 'a', w: 5, h: 10 }, { id: 'b', w: 5, h: 30 }, { id: 'c', w: 4, h: 10 }, { id: 'd', w: 12, h: 5 }, { id: 'e', w: 3, h: 8 }];
  const t = G.tidy(items);
  const placed = t.map((p) => ({ ...p, h: items.find((i) => i.id === p.id).h }));
  const free = placed.every((a, i) => placed.every((b, j) => i === j || !(a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y)));
  const order = [...placed].sort((a, b) => a.y - b.y || a.x - b.x).map((p) => p.id).join('');
  ok(free && order === 'abcde', `Tidy up: nothing overlaps and the order is kept (${order})`);
  ok(placed.every((p) => p.x % 6 === 0 && p.w % 6 === 0) && placed.find((p) => p.id === 'b').x === 6, 'Tidy up: widgets go side by side on equal tracks (6 columns here), so their edges line up');
  ok(G.tidy([{ id: 'a', w: 4, h: 5, min: 5 }], 4)[0].w === 8, 'Tidy up: never narrower than a widget\'s minimum (a whole number of tracks)');
  // what Home's own packing then does to a tidied layout changes nothing: it is already packed
  const again = G.compact(placed.map((p) => ({ ...p })));
  ok(again.every((p) => { const q = placed.find((x) => x.id === p.id); return q.x === p.x && q.y === p.y; }), 'Tidy up: the result is already packed (Home leaves it as it is)');
  const g = G.guides([{ id: 'a', x: 0, y: 0, w: 6, h: 10 }, { id: 'b', x: 6, y: 0, w: 6, h: 20 }], { id: 'm', x: 6, y: 20, w: 3, h: 5 });
  ok(g.cols.includes(6) && g.rows.includes(20), `guides: an edge that lines up with another widget's is shown (${JSON.stringify(g)})`);
}

const rects = (page) => page.evaluate(() => Object.fromEntries([...document.querySelectorAll('.home-widgets > .home-widget')].filter((e) => e.offsetParent)
  .map((e) => { const b = e.getBoundingClientRect(); return [e.dataset.widget, { x: b.x, y: b.y, w: b.width, h: b.height, r: b.right, b: b.bottom }]; })));
const clear = (rs) => { const v = Object.values(rs); return v.every((a, i) => v.every((b, j) => i === j || a.r <= b.x + 1 || b.r <= a.x + 1 || a.b <= b.y + 1 || b.b <= a.y + 1)); };
const edit = async (page) => { await page.locator('.home-edit-link').evaluate((b) => b.click()); await page.waitForTimeout(500); };

for (const [vw, vh, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const page = await browser.newPage({ viewport: { width: vw, height: vh }, isMobile: mob, hasTouch: mob });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  const st = await routeAll(page, { widgets: ['weather', 'agents', 'lights', 'network', 'coming', 'calendar', 'services'] });
  await page.route('**/api/v2/weather/places*', (r) => r.fulfill({ json: { places: [PLACES[2]] } }));
  await page.goto(URL_HOME); await page.waitForTimeout(3000);

  // ── (3) Weather: two places, in the order kept ──
  const names = async () => page.$$eval('.home-weather-place .home-weather-name', (els) => els.map((e) => e.textContent.trim()));
  ok((await names()).join('|') === 'Exampleton|Testburg', `${n}: Weather shows each place, in your order (${(await names()).join(', ')})`);
  ok(await page.locator('.home-weather-place').nth(1).locator('.when').filter({ hasText: 'Tomorrow' }).count() === 1, `${n}: each with its own tomorrow`);

  // ── (1) every widget has Compact / Full ──
  await edit(page);
  const bars = await page.$$eval('.home-widgets > .home-widget:not(.off)', (els) => els.map((e) => [e.dataset.widget, !!e.querySelector(':scope > .widget-bar .widget-size')]));
  ok(bars.length >= 7 && bars.every(([, has]) => has), `${n}: every widget has Compact / Full in Edit (${bars.filter(([, x]) => !x).map(([id]) => id).join(', ') || 'all'})`);
  // Places: add a third (found by name), move it to the top, take Testburg off
  await page.locator('[data-widget="weather"] .widget-bar').getByRole('button', { name: 'Places' }).click(); await page.waitForTimeout(300);
  const ed = page.locator('.home-places-edit');
  ok(await ed.locator('.home-place-row').count() === 2, `${n}: Places lists your two places`);
  await ed.locator('input[type=search]').fill('Sampleville'); await ed.getByRole('button', { name: 'Find' }).click(); await page.waitForTimeout(300);
  await ed.getByRole('button', { name: /Sampleville/ }).click(); await page.waitForTimeout(500);
  ok(st.prefs.weather_places.map((p) => p.name.split(',')[0]).join('|') === 'Exampleton|Testburg|Sampleville', `${n}: a place found by name is added at the end and kept`);
  await ed.getByRole('button', { name: 'Move Sampleville up' }).click(); await page.waitForTimeout(200);
  await ed.getByRole('button', { name: 'Move Sampleville up' }).click(); await page.waitForTimeout(200);
  await ed.getByRole('button', { name: 'Take Testburg off' }).click(); await page.waitForTimeout(600);
  ok(st.prefs.weather_places.map((p) => p.name.split(',')[0]).join('|') === 'Sampleville|Exampleton', `${n}: moved to the top and one taken off: kept in that order (${st.prefs.weather_places.map((p) => p.name.split(',')[0])})`);
  ok((await names()).join('|') === 'Sampleville|Exampleton', `${n}: and Weather shows them so at once`);
  await page.screenshot({ path: `${OUT}/stage2-places-${n}.png`, fullPage: !mob ? false : true });

  // Compact: Weather a line a place, Lights names only, Network down and up, Agents counts
  for (const id of ['weather', 'lights', 'network', 'agents']) await page.locator(`[data-widget="${id}"] .widget-size`).getByRole('button', { name: 'Compact' }).click();
  await page.waitForTimeout(500);
  ok(await page.locator('.home-weather-place.compact').count() === 2 && await page.locator('.home-weather-place.compact .home-weather-tomorrow').count() === 2,
    `${n}: Compact Weather is a line a place, with tomorrow`);
  ok(await page.locator('.home-lights.compact').count() === 1, `${n}: Compact Lights: smaller pills`);
  ok(await page.locator('[data-widget="network"] .w-fig').count() === 2, `${n}: Compact Network: down and up only`);
  ok(/6 today/.test(await page.locator('[data-widget="agents"] .w-agent').first().textContent()), `${n}: Compact Agents today: how many today`);
  ok(['weather', 'lights', 'network', 'agents'].every((id) => st.prefs.widget_sizes[id] === 'compact'), `${n}: each choice kept with your login (${JSON.stringify(st.prefs.widget_sizes)})`);
  await page.screenshot({ path: `${OUT}/stage2-compact-${n}.png`, fullPage: true });

  // ── (agents) each agent opens its chat; not while arranging ──
  await page.locator('[data-widget="agents"] .w-agent').first().click({ force: true }).catch(() => {}); await page.waitForTimeout(300);
  ok(page.url().endsWith('#/home'), `${n}: while arranging, tapping an agent does not open its chat`);
  await edit(page);                                                           // Done
  ok(await page.locator('[data-widget="agents"] a.w-agent[href="#/chat/greg"]').count() === 1, `${n}: each agent is a link to its chat`);
  await page.locator('[data-widget="agents"] a.w-agent').first().click(); await page.waitForTimeout(500);
  ok(/#\/chat\/greg/.test(page.url()), `${n}: tapping Greg's agent in Agents today (Compact) opens its chat (${page.url().split('#')[1]})`);
  await page.goto(URL_HOME); await page.waitForTimeout(2500);
  ok(await page.locator('.home-weather-place.compact').count() === 2, `${n}: after a reload Home opens on your sizes`);

  // Full again (agents), then the keyboard: Tab to an agent, Enter
  st.prefs.widget_sizes = { ...st.prefs.widget_sizes, agents: 'full', network: 'full' };
  await page.reload(); await page.waitForTimeout(2500);
  ok(/messages today · last/.test(await page.locator('[data-widget="agents"] .w-agent').first().textContent()), `${n}: Full Agents today says when the last went`);
  await page.locator('[data-widget="agents"] a.w-agent').nth(1).focus(); await page.keyboard.press('Enter'); await page.waitForTimeout(500);
  ok(/#\/chat\/household/.test(page.url()), `${n}: with the keyboard (Full), Enter on an agent opens its chat`);
  await page.goto(URL_HOME); await page.waitForTimeout(2500);

  if (mob) {
    // a phone keeps its one column: no grid, no Tidy up, no guides
    await edit(page);
    const rs = await rects(page);
    ok(await page.locator('.home-widgets.free').count() === 0 && Object.values(rs).every((r) => Math.abs(r.w - Object.values(rs)[0].w) < 2), `${n}: one column, every widget the full width`);
    ok(await page.locator('.home-tidy').count() === 0 && await page.locator('.guide-layer:visible').count() === 0, `${n}: no Tidy up and no guides on a phone`);
    ok(!/null/.test(await page.locator('.home-tray').textContent()), `${n}: the Add tray says no stray "null"`);
    const hb = await page.locator('[data-widget="lights"] .widget-handle').boundingBox();
    await page.mouse.move(hb.x + 10, hb.y + 10); await page.mouse.down(); await page.mouse.move(hb.x + 10, hb.y + 80, { steps: 5 });
    ok(await page.locator('.grid-ghost').count() === 0, `${n}: dragging on a phone shows no grid ghost`);
    await page.mouse.up(); await page.waitForTimeout(300);
  } else {
    // ── (2) minimum sizes ──
    await edit(page);
    const boardW = await page.locator('.home-widgets').evaluate((e) => e.getBoundingClientRect().width);
    const grip = page.locator('.grip-layer .widget-grip[data-widget="network"]');
    let gb = await grip.boundingBox();
    await page.mouse.move(gb.x + 12, gb.y + 12); await page.mouse.down(); await page.mouse.move(gb.x - 900, gb.y - 600, { steps: 10 }); await page.mouse.up(); await page.waitForTimeout(500);
    let r = (await rects(page)).network;
    const [fw, fh] = G.minPx('network', 'full');
    ok(r.w >= fw - 1 && r.h >= fh - 1, `${n}: Network (Full) will not be made smaller than ${fw}×${fh} px (${Math.round(r.w)}×${Math.round(r.h)}; ${st.prefs.widget_grid.network.w} columns)`);
    ok(st.prefs.widget_grid.network.w === G.minCols('network', 'full', boardW), `${n}: that is its own minimum in columns on this board (${G.minCols('network', 'full', boardW)})`);
    await page.locator('[data-widget="network"] .widget-size').getByRole('button', { name: 'Compact' }).click(); await page.waitForTimeout(300);
    gb = await grip.boundingBox();
    await page.mouse.move(gb.x + 12, gb.y + 12); await page.mouse.down(); await page.mouse.move(gb.x - 900, gb.y - 600, { steps: 10 }); await page.mouse.up(); await page.waitForTimeout(500);
    const cw = st.prefs.widget_grid.network.w;
    ok(cw === G.minCols('network', 'compact', boardW) && cw < G.minCols('network', 'full', boardW), `${n}: Compact Network may be narrower (${cw} columns)`);
    await page.locator('[data-widget="network"] .widget-size').getByRole('button', { name: 'Full' }).click(); await page.waitForTimeout(500);
    r = (await rects(page)).network;
    ok(r.w >= fw - 1 && st.prefs.widget_grid.network.w >= G.minCols('network', 'full', boardW), `${n}: turned Full again, it grows back to Full's minimum (${Math.round(r.w)} px)`);
    ok(clear(await rects(page)), `${n}: nothing overlaps`);

    // ── (4) the ghost and the guides while dragging ──
    const before = await rects(page);
    const hb = await page.locator('[data-widget="lights"] .widget-handle').boundingBox();
    await page.mouse.move(hb.x + 10, hb.y + 10); await page.mouse.down();
    await page.mouse.move(hb.x + 160, hb.y + 30, { steps: 6 }); await page.waitForTimeout(150);
    const ghost = await page.locator('.guide-layer .grid-ghost').boundingBox().catch(() => null);
    const guidesN = await page.locator('.guide-layer .snap-guide').count(), colsN = await page.locator('.guide-layer .grid-col').count();
    const lightsNow = await page.locator('.home-widgets > [data-widget="lights"]').evaluate((el) => { const t = el.style.transform; el.style.transform = ''; const b = el.getBoundingClientRect(); el.style.transform = t; return { x: b.x, y: b.y, w: b.width }; });
    await page.screenshot({ path: `${OUT}/stage2-dragging-desktop.png` });
    ok(ghost && Math.abs(ghost.x - lightsNow.x) < 3 && Math.abs(ghost.width - lightsNow.w) < 3, `${n}: while dragging, a ghost outline shows where it will land (${ghost && Math.round(ghost.x)},${ghost && Math.round(ghost.y)})`);
    ok(colsN === 12 && guidesN >= 1, `${n}: with the 12 columns and ${guidesN} snap guide(s) where edges line up`);
    await page.mouse.up(); await page.waitForTimeout(500);
    ok(await page.locator('.guide-layer .grid-ghost').count() === 0 && await page.locator('.guide-layer .snap-guide').count() === 0, `${n}: and they go when it is let go`);
    // resizing shows the ghost too
    const g2 = await page.locator('.grip-layer .widget-grip[data-widget="coming"]').boundingBox();
    await page.mouse.move(g2.x + 12, g2.y + 12); await page.mouse.down(); await page.mouse.move(g2.x + 80, g2.y + 60, { steps: 5 });
    ok(await page.locator('.guide-layer .grid-ghost').count() === 1, `${n}: resizing shows the ghost as well`);
    await page.mouse.up(); await page.waitForTimeout(400);

    // ── (4) Tidy up: a messy layout packed, the order kept ──
    st.prefs.widget_grid = { weather: { x: 1, y: 0, w: 5, h: 0 }, agents: { x: 7, y: 0, w: 4, h: 0 }, lights: { x: 0, y: 60, w: 5, h: 0 },
      network: { x: 6, y: 70, w: 5, h: 0 }, coming: { x: 2, y: 140, w: 4, h: 0 }, calendar: { x: 7, y: 150, w: 5, h: 0 }, services: { x: 3, y: 220, w: 4, h: 0 } };
    await page.reload(); await page.waitForTimeout(2500); await edit(page);
    const order0 = G.readingOrder(st.prefs.widget_grid, Object.keys(st.prefs.widget_grid));
    await page.screenshot({ path: `${OUT}/stage2-before-tidy-desktop.png`, fullPage: true });
    await page.getByRole('button', { name: 'Tidy up' }).click(); await page.waitForTimeout(700);
    const lay = st.prefs.widget_grid;
    const order1 = G.readingOrder(lay, Object.keys(lay));
    ok(order1.join() === order0.join(), `${n}: Tidy up keeps the order (${order1.join(', ')})`);
    const rs = await rects(page);
    ok(clear(rs), `${n}: nothing overlaps after Tidy up`);
    const track = G.defaultWidth(1440), edges = new Set(Object.values(lay).flatMap((c) => [c.x, c.x + c.w]));
    ok([...edges].every((e) => e % track === 0) && edges.has(0) && edges.has(12), `${n}: every edge lines up on a track of ${track} columns, from the left edge to the right (${[...edges].sort((a, b) => a - b)})`);
    const tallBefore = 220, tallAfter = Math.max(...Object.values(lay).map((c) => c.y));
    ok(tallAfter < tallBefore, `${n}: and it is packed up (lowest widget starts at row ${tallAfter}, was ${tallBefore})`);
    ok(Object.entries(lay).every(([id, c]) => c.w >= G.minCols(id, st.prefs.widget_sizes?.[id] || 'full', boardW)), `${n}: no widget is made smaller than its minimum`);
    await page.screenshot({ path: `${OUT}/stage2-tidy-desktop.png`, fullPage: true });
  }
  ok(errs.length === 0, `${n}: no page errors ${errs.slice(0, 2).join('; ')}`);
  await page.close();
}
await browser.close();
