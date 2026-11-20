#!/usr/bin/env node
// L5: screenshots of every dashboard screen in every look (Calm glass, Terminal HUD, Warm), light and
// dark, at phone and desktop width, with three automatic checks on each:
//   overflow  - the page is wider than the screen, or something runs off its right edge
//   cut off   - text clipped by its box without an ellipsis
//   contrast  - text below WCAG AA against what is behind it (4.5:1, or 3:1 for large text)
//   cramped   - text closer than 6 px to the inside edge of its box
//
// Usage (on the dev VM, against the dev dashboard):
//   node scripts/logos-ui-check.cjs [--url http://127.0.0.1:8091] [--out DIR] [--looks glass,hud,warm]
//        [--screens home,chat] [--widths phone,desktop] [--strict]
// Signs in with ~/.config/logos/dashboard-login (user=/password= lines) or LOGOS_UI_USER / LOGOS_UI_PASSWORD.
// The look is switched through the real setting (PUT /api/v2/me/look) and put back afterwards.
// Writes PNGs and report.json to --out; --strict exits 1 when anything is flagged.
const fs = require('fs');
const os = require('os');
const path = require('path');

function playwright() {
  const tries = [process.env.PLAYWRIGHT_CORE, path.join(__dirname, '..', 'node_modules', 'playwright-core'), 'playwright-core', 'playwright']
    .filter(Boolean);
  for (const t of tries) { try { return require(t); } catch (e) { /* next */ } }
  console.error('playwright-core is not installed: npm install --no-save playwright-core (and npx playwright install chromium)');
  process.exit(2);
}
function chromePath() {
  if (process.env.CHROME) return process.env.CHROME;
  const base = path.join(os.homedir(), '.cache', 'ms-playwright');
  const dirs = fs.existsSync(base) ? fs.readdirSync(base).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse() : [];
  for (const d of dirs) {
    const p = path.join(base, d, 'chrome-linux64', 'chrome');
    if (fs.existsSync(p)) return p;
  }
  return undefined;   // playwright's own default
}
function arg(name, dflt) {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
}
function login() {
  if (process.env.LOGOS_UI_USER) return { identifier: process.env.LOGOS_UI_USER, password: process.env.LOGOS_UI_PASSWORD || '' };
  const file = path.join(os.homedir(), '.config', 'logos', 'dashboard-login');
  const kv = Object.fromEntries(fs.readFileSync(file, 'utf8').split('\n').filter((l) => l.includes('='))
    .map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]));
  return { identifier: kv.user, password: kv.password };
}

const URL_ = arg('url', 'http://127.0.0.1:8091');
const OUT = arg('out', path.join(os.tmpdir(), 'logos-ui-check'));
const LOOKS = { glass: ['dark', 'light'], hud: ['dark'], warm: ['light', 'dark'] };
const looks = arg('looks', 'glass,hud,warm').split(',');
const screens = arg('screens', 'home,chat,brain,noticeboard,agents,advisor,models,people,updates').split(',');
const WIDTHS = { phone: [390, 844], desktop: [1280, 860] };
const widths = arg('widths', 'phone,desktop').split(',');
const strict = process.argv.includes('--strict');

// Runs in the page: the three checks.
function inspect() {
  const vw = document.documentElement.clientWidth;
  const out = { overflow: [], cutoff: [], contrast: [], cramped: [] };
  const name = (el) => el.tagName.toLowerCase() + (el.className && typeof el.className === 'string'
    ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
  const text = (el) => [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').trim();
  const visible = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  if (document.documentElement.scrollWidth > vw + 1) out.overflow.push(`page is ${document.documentElement.scrollWidth}px wide on a ${vw}px screen`);
  const rgba = (s) => { const m = s.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const over = (top, under) => { const a = top[3]; return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat(1); };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const ground = rgba(getComputedStyle(document.body).backgroundColor) || [0, 0, 0, 0];
  const groundVar = getComputedStyle(document.documentElement).getPropertyValue('--ground').trim();
  const probe = document.createElement('span'); probe.style.color = groundVar; document.body.append(probe);
  const base = rgba(getComputedStyle(probe).color) || [255, 255, 255, 1]; probe.remove();
  function behind(el) {
    const layers = [];
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.backgroundImage !== 'none' && e !== document.body) return null;     // a picture or gradient: not judged
      const c = rgba(cs.backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let col = ground[3] >= 1 ? ground : base;
    for (const l of layers.reverse()) col = over(l, col);
    return col;
  }
  const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('svg, .visually-hidden, .sr') || !visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.right > vw + 2 && !el.closest('.table-box, .md-table, .agent-cards, .home-list, pre, .log-output, .tabs, .chart-wrap')) {
      out.overflow.push(`${name(el)} runs ${Math.round(r.right - vw)}px past the edge`);
    }
    const t = text(el);
    if (!t) continue;
    const cs = getComputedStyle(el);
    if ((cs.overflow.includes('hidden') || cs.overflow.includes('clip')) && cs.textOverflow !== 'ellipsis'
        && (el.scrollWidth > el.clientWidth + 2 || el.scrollHeight > el.clientHeight + 4) && !['TEXTAREA', 'INPUT'].includes(el.tagName)) {
      out.cutoff.push(`${name(el)} "${t.slice(0, 40)}"`);
    }
    let op = 1; for (let e = el; e; e = e.parentElement) op *= Number(getComputedStyle(e).opacity);
    if (op < 0.99 || el.closest('[disabled], .off')) continue;              // faded on purpose
    const bg = behind(el); const fg = rgba(cs.color);
    if (!bg || !fg) continue;
    const ratio = (Math.max(lum(over(fg, bg)), lum(bg)) + 0.05) / (Math.min(lum(over(fg, bg)), lum(bg)) + 0.05);
    const size = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700;
    const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
    const key = `${name(el)}|${cs.color}`;
    if (ratio < need && !seen.has(key)) { seen.add(key); out.contrast.push(`${name(el)} "${t.slice(0, 30)}" ${ratio.toFixed(2)}:1 (needs ${need})`); }
  }
  // cramped: text closer than 6 px to the inside edge of the box it sits in (a box = a border or
  // a background of its own with rounded corners). Inputs and the box's own scroll areas are skipped.
  const boxOf = (el) => {
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cs = getComputedStyle(e);
      const border = parseFloat(cs.borderLeftWidth) > 0 && cs.borderLeftStyle !== 'none' && parseFloat(cs.borderRightWidth) > 0;
      const filled = (rgba(cs.backgroundColor) || [0, 0, 0, 0])[3] > 0.02 && parseFloat(cs.borderTopLeftRadius) > 0;
      if (border || filled) return e;
    }
    return null;
  };
  const tight = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!n.textContent.trim() || !el || el.closest('svg, .visually-hidden, .sr, input, textarea, select, .chart-wrap') || !visible(el)) continue;
    const box = boxOf(el);
    if (!box || (box === el && box.getBoundingClientRect().width < 36)) continue;   // a glyph in a small badge is meant to be snug
    const range = document.createRange(); range.selectNodeContents(n);
    const t = range.getBoundingClientRect(); const b = box.getBoundingClientRect(); const bs = getComputedStyle(box);
    if (!t.width || t.right < b.left || t.left > b.right || t.bottom < b.top || t.top > b.bottom) continue;
    const left = t.left - (b.left + parseFloat(bs.borderLeftWidth));
    const right = (b.right - parseFloat(bs.borderRightWidth)) - t.right;
    const gap = Math.min(left, right > -1 && right < 6 ? right : 99);
    if (gap < 6 && box.scrollWidth <= box.clientWidth + 1) {
      const key = name(box) + '>' + name(el);
      if (!tight.has(key)) { tight.add(key); out.cramped.push(`${name(el)} "${n.textContent.trim().slice(0, 30)}" ${Math.round(gap)}px from the edge of ${name(box)}`); }
    }
  }
  return out;
}

(async () => {
  const { chromium } = playwright();
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: chromePath() });
  const report = [];
  const ctx0 = await browser.newContext();
  const res = await ctx0.request.post(`${URL_}/auth/login`, { data: login() });
  if (!res.ok()) { console.error(`sign-in failed: ${res.status()}`); process.exit(2); }
  const state = await ctx0.storageState();
  const csrf = (state.cookies.find((c) => c.name === 'csrf_token') || {}).value || '';
  const me = await (await ctx0.request.get(`${URL_}/api/v2/me`)).json();
  const setLook = (look, mode) => ctx0.request.put(`${URL_}/api/v2/me/look`, { data: { look, mode }, headers: { 'X-CSRF-Token': csrf } });
  try {
    for (const look of looks) {
      for (const mode of LOOKS[look] || []) {
        await setLook(look, mode);
        for (const w of widths) {
          const [width, height] = WIDTHS[w];
          const ctx = await browser.newContext({ viewport: { width, height }, storageState: await ctx0.storageState(),
            colorScheme: mode === 'light' ? 'light' : 'dark' });
          await ctx.addInitScript(([l, m]) => { try { localStorage.setItem('logos-look', l); localStorage.setItem('logos-mode', m); } catch (e) {} }, [look, mode]);
          const page = await ctx.newPage();
          for (const s of screens) {
            await page.goto(`${URL_}/#/${s}`);
            // Every screen says "Loading…" until its data is in; wait for that to go (at most 10 s).
            await page.waitForTimeout(400);
            await page.waitForFunction(() => !/Loading…/.test(document.body.innerText), null, { timeout: 10000 }).catch(() => {});
            await page.waitForTimeout(500);
            const file = `${w}-${look}-${mode}-${s}.png`;
            await page.screenshot({ path: path.join(OUT, file), fullPage: w === 'phone' });
            const found = await page.evaluate(inspect);
            const n = found.overflow.length + found.cutoff.length + found.contrast.length + found.cramped.length;
            report.push({ screen: s, look, mode, width: w, file, ...found });
            console.log(`${n ? '!!' : 'ok'} ${w.padEnd(7)} ${look}/${mode.padEnd(5)} ${s.padEnd(11)} ${n ? JSON.stringify(found) : ''}`);
          }
          await ctx.close();
        }
      }
    }
  } finally {
    await setLook((me.look && me.look.look) || 'glass', (me.look && me.look.mode) || 'dark');   // put it back
    await browser.close();
  }
  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 1));
  const bad = report.filter((r) => r.overflow.length + r.cutoff.length + r.contrast.length + r.cramped.length);
  console.log(`\n${report.length} screenshots in ${OUT}; ${bad.length} with something flagged.`);
  process.exit(strict && bad.length ? 1 : 0);
})();
