// Models › Local model on llama-swap (28 Nov): the PC's live numbers in the model's card (GPU, speed,
// prompt cache, its queue), and a hint when llama-server has no --metrics.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const URL = `http://127.0.0.1:${process.env.PORT || 8779}/#/models`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });

const facts = async (page) => page.evaluate(() => {
  const out = {};
  for (const dt of document.querySelectorAll('section[aria-label^="Local model on"] dl.facts dt')) out[dt.textContent] = dt.nextElementSibling.textContent;
  return out;
});

for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const page = await ctx.newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.goto(URL); await page.waitForTimeout(2000);
  const f = await facts(page);
  ok(f['GPU now'] === '14.8 of 16.0 GB · 61 °C · 35 % busy · NVIDIA GeForce RTX 4080 SUPER', `${n}: GPU memory, temperature and use (${f['GPU now']})`);
  ok(/^reads 2,450 · writes 64 tokens\/s/.test(f['Speed'] || ''), `${n}: reading and writing speed (${f['Speed']})`);
  ok(/^75 % reused/.test(f['Prompt cache'] || ''), `${n}: the prompt cache's hit rate (${f['Prompt cache']})`);
  ok(/^1 now · 0 waiting/.test(f['Working on'] || ''), `${n}: what the PC is working on (${f['Working on']})`);
  ok(!('KV cache in use' in f), `${n}: no KV cache use when llama-server does not report it`);
  ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${n}: no sideways scroll`);
  await page.locator('section[aria-label^="Local model on"] .card').first().screenshot({ path: `${OUT}/models-live-${n}.png` });

  // llama-server without --metrics: say how to turn it on
  await page.route('**/api/v2/models/local', async (r) => {
    const res = await r.fetch(); const d = await res.json();
    d.servers[0].loaded[0].live = { gpu: d.servers[0].loaded[0].live.gpu, metrics_on: false };
    await r.fulfill({ json: d });
  });
  await page.reload(); await page.waitForTimeout(1800);
  const g = await facts(page);
  ok(/^Not reported · add --metrics/.test(g['Speed'] || '') && !('Prompt cache' in g), `${n}: without --metrics, a hint instead of numbers (${g['Speed']})`);
  ok(!errs.length, `${n}: no page errors ${errs.join('; ')}`);
  await ctx.close();
}
await browser.close();
