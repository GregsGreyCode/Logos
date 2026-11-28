// Each widget's smallest size, Compact and Full (F49 stage 2, Greg 24 Nov: "correct each widget's minimum size so it makes
// sense for what it shows"). Every widget is drawn with real-looking content (widget-fixtures.mjs) at widths from 160 to
// 640 px. The narrowest width that works is the one from which every wider one has, compared with 640 px, nothing more
// spilling out of the widget, no more text cut off ("…"), and no text squeezed onto more lines (text that is meant to wrap
// may take one more line; a line meant to stay one line may not). The
// height is what shows what the widget is for at that width: all of a fixed-shape widget, the first rows of a list.
// The minimums in gateway/ui/home-grid.js (MIN) must be at least these: this fails when a widget no longer fits its own.
//   MEASURE=1 node tests/ui/checks/widget_minimums.mjs out   prints the measured table (to copy into home-grid.js)
import { createRequire } from 'module';
import { routeAll, ALL } from '../widget-fixtures.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const G = await import(new URL('../../../gateway/ui/home-grid.js', import.meta.url));
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const MEASURE = !!process.env.MEASURE;

// What a widget is for, at its smallest: [selector, how many]. The bottom of the last one is its minimum height; none
// given: all of it.
const ESSENTIAL = {
  month: ['.cal-mini, .home-month .home-line', 2], calendar: ['.home-line', 2], coming: ['.home-line', 2], messages: ['.home-msg', 2], lights: ['.home-light', 1], lists: ['.home-list-card', 1],
  weather: ['.home-weather-place', 1], services: ['.w-row', 2], backups: ['.w-row', 2], proxmox: ['.w-card, .w-row', 2], servers: ['.w-card, .w-row', 1],
  playing: ['.w-card, .w-row', 1], downloads: ['.w-dl', 2], bookmarks: ['.w-mark, .w-mark-chip', 1], cameras: ['.w-cam', 1], agents: ['.w-agent', 2],
};
const WIDTHS = Array.from({ length: 49 }, (_, i) => 160 + i * 10);
// A message's first words are a preview, cut to one line on purpose: not counted as cut (in the page code below).

const browser = await chromium.launch({ executablePath: process.env.CHROME });
const table = {};
for (const size of ['compact', 'full']) {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await routeAll(page, { prefs: { widget_sizes: Object.fromEntries(ALL.map((id) => [id, size])) } });
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(3500);
  const got = await page.evaluate(({ ids, widths, essential }) => {
    // Messages come from Matrix, which the stand-in has none unread of: two rows drawn as Home draws them.
    const msgs = document.querySelector('[data-widget="messages"]');
    if (msgs && msgs.hidden) {
      msgs.hidden = false;
      const box = msgs.querySelector('.home-msgs'), compact = msgs.classList.contains('size-compact');
      box.innerHTML = ['Household agent', "Greg's agent"].map((t) => `<a class="home-msg" href="#"><span class="home-msg-head"><strong>${t}</strong><span class="when">10 min</span></span>${compact ? '' : '<span class="home-msg-text">The bins go out tonight, recycling this week.</span>'}</a>`).join('');
    }
    const host = document.createElement('div');
    host.style.cssText = 'position:absolute;left:0;top:0;z-index:99;background:var(--ground)';
    document.querySelector('.home').append(host);
    const textEls = (root) => [root, ...root.querySelectorAll('*')].filter((e) => [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()));
    const lines = (e) => { const tops = new Set(); for (const n of e.childNodes) if (n.nodeType === 3 && n.textContent.trim()) {
      const r = document.createRange(); r.selectNodeContents(n); for (const b of r.getClientRects()) if (b.width > 0) tops.add(Math.round(b.top / 4)); } return tops.size; };
    const out = {};
    for (const id of ids) {
      const el = document.querySelector(`.home-widgets > [data-widget="${id}"]`);
      if (!el || el.classList.contains('off') || el.hidden) { out[id] = null; continue; }
      const mark = document.createComment('x'); el.before(mark); host.append(el);
      for (const k of ['gridColumn', 'gridRow', 'height', 'overflow']) el.style[k] = '';
      host.style.width = '640px';
      const els = textEls(el), roomy = els.map(lines);
      const all = [...el.querySelectorAll('*')];
      const problems = () => {
        const box = el.getBoundingClientRect(), bad = new Map();
        all.forEach((d, i) => {
          const r = d.getBoundingClientRect();
          if (!r.width || d.closest('.visually-hidden, .sr') || d.matches('.home-msg-text')) return;
          if (r.right > box.right + 1 || r.left < box.left - 1) bad.set(`s${i}`, `spills: ${d.className || d.tagName}`);
          const cs = getComputedStyle(d);
          if ((cs.overflowX === 'hidden' || cs.overflowX === 'clip' || cs.textOverflow === 'ellipsis') && d.scrollWidth > d.clientWidth + 1 && d.textContent.trim())
            bad.set(`c${i}`, `cut: ${d.className || d.tagName} "${d.textContent.trim().slice(0, 24)}" ${d.scrollWidth}>${d.clientWidth}`);
        });
        return bad;
      };
      const base = problems();                                  // what is so even with room (a long name cut on purpose)
      const rows = widths.map((w) => {
        host.style.width = `${w}px`;
        const box = el.getBoundingClientRect(), why = [];
        for (const [k, v] of problems()) if (!base.has(k)) why.push(v);
        els.forEach((e, i) => { const wraps = getComputedStyle(e).whiteSpace === 'normal' ? 1 : 0;
          if (lines(e) > roomy[i] + wraps) why.push(`wraps: ${e.className || e.tagName}`); });
        const [sel, n] = essential[id] || [null, 0];
        const ess = sel ? [...el.querySelectorAll(sel)].filter((e) => e.getBoundingClientRect().height)[Math.max(0, n - 1)] || null : null;
        const kids = ess ? null : [...el.children].filter((e) => e.getBoundingClientRect().height);
        const bottom = ess ? ess.getBoundingClientRect().bottom : Math.max(box.top, ...kids.map((e) => e.getBoundingClientRect().bottom));
        return { w, ok: !why.length, why: why.slice(0, 2), h: Math.ceil(bottom - box.top) };
      });
      mark.replaceWith(el);
      out[id] = rows;
    }
    host.remove();
    return out;
  }, { ids: ALL, widths: WIDTHS, essential: ESSENTIAL });
  for (const [id, rows] of Object.entries(got)) {
    if (!rows) { ok(false, `${size}: ${id} is on Home to be measured`); continue; }
    // the narrowest width from which every wider one works
    let i = rows.length - 1;
    while (i > 0 && rows[i - 1].ok) i--;
    const at = rows[i];
    if (!rows[rows.length - 1].ok) ok(false, `${size}: ${id} works at 640 px (${rows[rows.length - 1].why.join(', ')})`);
    const shown = (rows[i - 1] || {}).why || [];
    (table[id] ||= {})[size] = [at.w, at.h, shown.join(', ')];
    if ((process.env.DEBUG || '').split(',').includes(id)) console.log(size, id, rows.filter((r) => !r.ok).map((r) => `${r.w}:${r.why.join('/')}`).join('  '));
  }
  ok(errs.length === 0, `${size}: no page errors ${errs.slice(0, 2).join('; ')}`);
  await page.close();
}
await browser.close();

if (MEASURE) {
  for (const [id, s] of Object.entries(table)) console.log(`  ${id}: { compact: [${s.compact[0]}, ${s.compact[1]}], full: [${s.full[0]}, ${s.full[1]}] },   // narrower: ${s.compact[2] || '-'} | ${s.full[2] || '-'}`);
}
for (const [id, s] of Object.entries(table)) for (const size of ['compact', 'full']) {
  const [w, hgt] = G.minPx(id, size), [mw, mh] = s[size];
  ok(w >= mw && hgt >= mh, `${id} ${size}: its minimum ${w}×${hgt} px holds what it shows (measured ${mw}×${mh})`);
  ok(w <= mw + 40, `${id} ${size}: and is not much wider than it needs (${w} vs ${mw} px)`);
}
// Every widget on Home has its own measured minimum (none falls back to the unmeasured guess)
ok(Object.keys(table).every((id) => G.MIN[id]), `every widget is in the table (${Object.keys(table).filter((id) => !G.MIN[id]).join(', ') || 'all'})`);
