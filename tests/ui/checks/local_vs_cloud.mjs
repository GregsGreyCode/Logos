// Models › Local vs cloud (28 Nov): tokens, the model PC's electricity and what Claude would have cost,
// today / this month / since tracking, with "saves" or, honestly, "cheaper on" when the cloud would cost
// less; the projected month; each agent's share; the prices and settings (admin only). Made-up figures.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const URL = `http://127.0.0.1:${process.env.PORT || 8779}/#/models`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });

const AGENT = (name, display, prompt, completion) => ({ name, display_name: display, test: false, fallbacks: [], month_usd: 0, limit_usd: 0,
  left_usd: 0, projected_usd: 0, over: false, now: { kind: 'local', label: 'Local model' },
  current: { provider: 'lm-studio', model: 'qwen/qwen3.6-35b-a3b', kind: 'local', since: null, last_call: null },
  models: [{ provider: 'lm-studio', model: 'qwen/qwen3.6-35b-a3b', kind: 'local', requests: 10, prompt_tokens: prompt, completion_tokens: completion, cached_tokens: 0, usd: 0 }] });

const card = 'section[aria-label="Local vs cloud"]';
const facts = async (page) => page.evaluate((sel) => {
  const out = {};
  for (const dt of document.querySelectorAll(`${sel} dl.facts dt`)) out[dt.textContent] = dt.nextElementSibling.textContent;
  return out;
}, card);
const verdicts = async (page) => page.evaluate((sel) => Object.fromEntries([...document.querySelectorAll(`${sel} tbody tr`)]
  .map((tr) => [tr.dataset.model, { cost: tr.children[1].textContent, verdict: tr.children[2].textContent, bad: !!tr.querySelector('.bad') }])), card);

for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  const page = await ctx.newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  await page.route('**/api/v2/models', async (r) => {
    const res = await r.fetch(); const d = await res.json();
    d.agents = [AGENT('greg', "Greg's agent", 60_000_000, 2_000_000), AGENT('household', 'Household agent', 30_000_000, 1_000_000)];
    await r.fulfill({ json: d });
  });
  let put = null;
  await page.route('**/api/v2/models/local-cost', async (r) => {
    const res = await r.fetch(); const d = await res.json();
    if (r.request().method() === 'PUT') { put = r.request().postDataJSON(); return r.fulfill({ json: { settings: { ...d.settings, ...put } } }); }
    if (put) d.settings = { ...d.settings, ...put };
    await r.fulfill({ json: d });
  });
  await page.goto(URL); await page.waitForTimeout(2000);
  ok(await page.locator(card).count() === 1, `${n}: the Local vs cloud card is there`);
  let f = await facts(page);
  ok(/^4\.6M · 75 % reused from the cache/.test(f['Tokens in'] || ''), `${n}: tokens in and the cached share (${f['Tokens in']})`);
  ok(f['Tokens out'] === '159K', `${n}: tokens out (${f['Tokens out']})`);
  ok(/^150 · 1\.1 h answering/.test(f['Requests'] || ''), `${n}: requests and time answering (${f['Requests']})`);
  ok(/^£0\.60 · 0\.81 kWh answering \(£0\.20\) · 1\.58 kWh kept loaded \(£0\.40\)/.test(f['Electricity'] || ''), `${n}: electricity, answering and kept loaded (${f['Electricity']})`);
  let v = await verdicts(page);
  ok(v['haiku-5.5'].bad && /^£0\.38 cheaper on Haiku 5\.5$/.test(v['haiku-5.5'].verdict), `${n}: honest when Claude would cost less (${v['haiku-5.5'].verdict})`);
  ok(v['sonnet-5.5'].cost === '£3.35' && v['sonnet-5.5'].verdict === 'saves £2.75', `${n}: Sonnet would cost more: saved (${v['sonnet-5.5'].cost}, ${v['sonnet-5.5'].verdict})`);
  ok(v['opus-5.5'].verdict === 'saves £6.09', `${n}: Opus (${v['opus-5.5'].verdict})`);
  const notes = await page.locator(`${card} .cost-notes`).textContent();
  ok(/electricity only/i.test(notes) && /hardware/.test(notes), `${n}: says it is electricity only, no hardware`);
  ok(/Haiku 5\.5's cache-read price is not published: assumed 10 %/.test(notes) && /cache-write premium is left out/.test(notes), `${n}: the cache price assumptions are labelled`);
  ok(/30 W of GPU idle draw is left out \(measured/.test(notes), `${n}: the idle baseline is explained`);
  ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${n}: no sideways scroll`);
  await page.locator(`${card} .card`).screenshot({ path: `${OUT}/local-vs-cloud-${n}.png` });

  // This month: the projected month and each agent's share
  await page.getByRole('button', { name: 'This month' }).click();
  await page.waitForTimeout(200);
  ok(await page.getByRole('button', { name: 'This month' }).getAttribute('aria-pressed') === 'true', `${n}: This month is chosen`);
  const proj = await page.locator(`${card} .cost-projected`).textContent().catch(() => '');
  ok(/^Projected for November: electricity £12\.96; Haiku 5\.5 £4\.62, Sonnet 5\.5 £72\.57, Opus 5\.5 £145\.15/.test(proj), `${n}: projected month (${proj})`);
  const agents = await page.locator(`${card} .cost-agents li`).allTextContents();
  ok(agents.length === 2 && /^Greg's agent 60M in · 2M out · 67 %/.test(agents[0]), `${n}: each agent's share this month (${agents[0]})`);
  f = await facts(page);
  ok(/^87\.4M/.test(f['Tokens in'] || ''), `${n}: month tokens (${f['Tokens in']})`);
  await page.getByRole('button', { name: /^Since / }).click();
  await page.waitForTimeout(200);
  ok(!(await page.locator(`${card} .cost-projected`).count()) && !(await page.locator(`${card} .cost-agents`).count()), `${n}: since tracking: no projection or agent list`);

  // Settings: price per kWh, currency, dollar rate, idle baseline
  await page.locator(`${card} .cost-settings > summary`).click();
  const base = page.locator(`${card} input[name="baseline"]`);
  ok(await base.getAttribute('placeholder') === 'measured 30', `${n}: the measured baseline is the placeholder`);
  await page.locator(`${card} input[name="price"]`).fill('0.3');
  await page.locator(`${card} input[name="usd_rate"]`).fill('0.8');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.waitForTimeout(500);
  ok(put && put.price_per_kwh === 0.3 && put.usd_rate === 0.8 && put.currency === 'GBP' && put.baseline_watts === null, `${n}: settings sent (${JSON.stringify(put)})`);
  ok(await page.locator(`${card} input[name="price"]`).inputValue() === '0.3', `${n}: the card shows the new price after saving`);
  ok(!errs.length, `${n}: no page errors ${errs.join('; ')}`);
  await ctx.close();
}

// Without llama-server's cache numbers, and for someone who does not manage Logos
{
  const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  const page = await ctx.newPage();
  await page.route('**/api/v2/models/local-cost', async (r) => {
    const d = await (await r.fetch()).json();
    d.today = { ...d.today, cache_measured: false, days_without_cache: 1, tokens: { ...d.today.tokens, cached: 0 } };
    await r.fulfill({ json: d });
  });
  await page.goto(URL); await page.waitForTimeout(2000);
  const f = await facts(page);
  ok(/cache not measured: counted as not cached/.test(f['Tokens in'] || ''), `no cache numbers: said so (${f['Tokens in']})`);
  await page.unroute('**/api/v2/models/local-cost');
  await page.route('**/api/v2/models', async (r) => {
    const d = await (await r.fetch()).json(); d.all_agents = false; d.total = null; await r.fulfill({ json: d });
  });
  await page.reload(); await page.waitForTimeout(2000);
  ok(await page.locator(card).count() === 0, 'not for someone who does not manage Logos');
  await ctx.close();
}
await browser.close();
