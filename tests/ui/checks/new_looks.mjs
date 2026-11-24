// The two new looks (Greg, 23 Nov): Paper & ink and Sorbet pop, each in light and dark. They are chosen in Settings, they are
// different from the older three, they paint every screen without errors, and their colours are readable (WCAG AA, 4.5:1).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const lum = (hex) => { const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await page.goto('http://127.0.0.1:8779/#/settings/look'); await page.waitForTimeout(2200);
const names = await page.locator('.look-option, [name="look"]').count();
const text = await page.locator('#view').innerText();
ok(text.includes('Paper & ink') && text.includes('Sorbet pop'), 'Settings › Look offers Paper & ink and Sorbet pop');
await page.getByText('Sorbet pop', { exact: true }).click(); await page.waitForTimeout(600);
ok(await page.evaluate(() => document.documentElement.dataset.look) === 'pop', 'choosing Sorbet pop applies it');
const grounds = {};
for (const look of ['glass', 'hud', 'warm', 'paper', 'pop']) {
  for (const theme of look === 'hud' ? ['dark'] : ['light', 'dark']) {
    await page.evaluate(([l, t]) => { const d = document.documentElement; d.dataset.look = l; d.dataset.theme = t; d.dataset.mode = t; }, [look, theme]); await page.waitForTimeout(250);
    const v = await page.evaluate(() => { const s = getComputedStyle(document.documentElement); const g = (k) => s.getPropertyValue(k).trim();
      return { ground: g('--ground'), surface: g('--surface'), ink: g('--ink'), muted: g('--muted'), accent: g('--accent'), accentInk: g('--accent-ink'), accentText: g('--accent-text'), ok: g('--ok'), bad: g('--bad'), warnInk: g('--warn-ink'), warnBg: g('--warn-bg') }; });
    grounds[`${look}-${theme}`] = v.ground;
    if (look === 'paper' || look === 'pop') {
      const hex = (c) => (/^#[0-9a-f]{6}$/i.test(c) ? c : null);
      const pairs = [['ink', v.ink, v.ground], ['ink on cards', v.ink, v.surface], ['muted', v.muted, v.ground], ['accent text', v.accentText, v.ground], ['text on accent', v.accentInk, v.accent], ['ok', v.ok, v.ground], ['bad', v.bad, v.ground]];
      for (const [n, a, b] of pairs) if (hex(a) && hex(b)) { const r = ratio(a, b); ok(r >= 4.5, `${look} ${theme}: ${n} is readable (${r.toFixed(1)}:1)`); }
    }
  }
}
ok(new Set(Object.values(grounds)).size >= 8, `the looks are visibly different from each other (${new Set(Object.values(grounds)).size} grounds)`);
for (const look of ['paper', 'pop']) for (const r of ['home', 'chat/greg', 'noticeboard', 'brain/greg', 'settings']) {
  await page.evaluate((l) => { document.documentElement.dataset.look = l; document.documentElement.dataset.theme = 'light'; }, look);
  await page.goto('http://127.0.0.1:8779/#/' + r); await page.waitForTimeout(1500);
  await page.evaluate((l) => { document.documentElement.dataset.look = l; }, look); await page.waitForTimeout(200);
  ok((await page.locator('#view').innerText()).length > 20, `${look}: ${r} draws`);
}
ok(errs.length === 0, `no page errors ${errs.slice(0, 2).join('; ')}`);
await browser.close();
