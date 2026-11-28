// Desktop polish (Greg, 24 Nov): Notifications in one column on a 27-inch 1080p screen; the Las Vegas sign's Edit and ✕ inside
// its pill; a widget taken off Home disappears at once, not after the network.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });

// Notifications: one column, every day under the one before
const p1 = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await p1.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/noticeboard`); await p1.waitForTimeout(2500);
const days = await p1.locator('.feed-days > section').evaluateAll((els) => els.map((e) => { const b = e.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width) }; }));
ok(days.length >= 2 && new Set(days.map((d) => d.x)).size === 1, `1920×1080: the days are in one column (${days.map((d) => d.x + ',' + d.y).join(' · ')})`);
ok(days.every((d, i) => i === 0 || d.y > days[i - 1].y), 'each day sits under the one before');
const need = await p1.locator('.notifications > section[aria-label="Needs you"]').boundingBox();
ok(Math.abs(need.width - days[0].w) < 4, `Needs you and the days share one width (${Math.round(need.width)} and ${days[0].w}px)`);
ok(days[0].w <= 1000 && days[0].w >= 700, `a comfortable width, not the whole screen (${days[0].w}px)`);
await p1.close();

// the sign's Edit and ✕ are inside its pill
const kind = { widgets: [{ id: 'marquee', name: 'Las Vegas sign', who: 'home', refresh: 120, set_up: true, can_set_up: true, about: 'x', fields: [] }] };
const DATA = { state: 'ok', summary: '', speed: 'medium', problems: [], at: Date.now() / 1000, items: [{ text: 'Welcome home', source: 'text' }, { text: 'BTC $64,210', source: 'x' }] };
const p2 = await browser.newPage({ viewport: { width: 1440, height: 900 } });
let putAt = 0, goneAt = 0;
await p2.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json(); me.prefs.widgets = ['weather', 'lights', 'marquee']; me.prefs.widgets_v2 = true; await r.fulfill({ json: me }); });
await p2.route('**/api/v2/widgets', (r) => r.fulfill({ json: kind }));
await p2.route('**/api/v2/widgets/marquee', (r) => r.fulfill({ json: DATA }));
await p2.route('**/api/v2/me/prefs', async (r) => { if (r.request().method() === 'PUT') { putAt = Date.now(); await new Promise((res) => setTimeout(res, 1500)); } await r.fulfill({ json: { ok: true } }); });
await p2.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/home`); await p2.waitForTimeout(2500);
await p2.locator('.home-edit-link').click(); await p2.waitForTimeout(700);
const sign = await p2.locator('.home-band-widget .mq, .home-band-widget').first().boundingBox();
const bar = await p2.locator('.home-band-widget .band-bar').boundingBox();
const wrap = await p2.locator('.home-band-widget').boundingBox();
ok(bar.x >= wrap.x && bar.x + bar.width <= wrap.x + wrap.width + 1 && bar.y >= wrap.y && bar.y + bar.height <= wrap.y + wrap.height + 1, `the sign's Edit and ✕ are inside its pill (bar ${Math.round(bar.x)}–${Math.round(bar.x + bar.width)} in ${Math.round(wrap.x)}–${Math.round(wrap.x + wrap.width)}; y ${Math.round(bar.y)}–${Math.round(bar.y + bar.height)} in ${Math.round(wrap.y)}–${Math.round(wrap.y + wrap.height)})`);
// ✕ on a widget: gone at once while the saving takes 1.5 s
const t0 = Date.now();
await p2.locator('.home-widget[data-widget="lights"] .widget-remove').click();
await p2.waitForFunction(() => !document.querySelector('.home-widget[data-widget="lights"]:not(.off)') || document.querySelector('.home-widget[data-widget="lights"]').classList.contains('off') || !document.querySelector('.home-widget[data-widget="lights"]').offsetParent, null, { timeout: 3000 });
goneAt = Date.now() - t0;
ok(goneAt < 400, `a widget taken off Home is gone at once (${goneAt} ms, while saving takes 1500 ms)`);
await p2.close();
await browser.close();
