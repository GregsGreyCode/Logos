// The last widget, "Agents today", and the Prometheus endpoint's switch in Settings (F42, Greg 23 Nov).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const now = Date.now() / 1000;
const TOKEN = 'tok_' + 'a1b2c3d4'.repeat(5);

for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write']).catch(() => {});
  const page = await ctx.newPage();
  const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  let enabled = false, puts = [];
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs.widgets = ['agents', 'weather']; me.prefs.widgets_v2 = true; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/widgets', (r) => r.fulfill({ json: { widgets: [{ id: 'agents', name: 'Agents today', who: 'home', refresh: 60, set_up: true, can_set_up: true, fields: [],
    about: 'What each of your agents did today.' }] } }));
  await page.route('**/api/v2/widgets/agents', (r) => r.fulfill({ json: { state: 'ok', summary: 'AI computer on', at: now, model_up: true, items: [
    { agent: 'greg', name: "Greg's agent", ready: true, messages: 6, last: now - 1800, total: 120 },
    { agent: 'household', name: 'Household agent', ready: true, messages: 0, last: now - 86400 * 2, total: 40 },
    { agent: 'bere', name: "Béré's agent", ready: false, messages: 0, last: 0, total: 3 }] } }));
  await page.route('**/api/v2/metrics', async (r) => {
    if (r.request().method() === 'PUT') {
      const b = r.request().postDataJSON(); puts.push(b);
      enabled = b.enabled;
      return r.fulfill({ json: { enabled, token: b.enabled ? TOKEN + (b.rotate ? '2' : '') : null, since: 1 } });
    }
    return r.fulfill({ json: { enabled, since: enabled ? 1 : null } });
  });

  // the widget
  await page.goto('http://127.0.0.1:8779/#/home'); await page.waitForTimeout(2500);
  const sec = page.locator('section.w-agents-today, section[aria-label="Agents today"]');
  ok(await sec.count() === 1 && (await sec.locator('.w-summary').textContent()) === 'AI computer on', `${n}: Agents today on Home, with whether the AI computer is on`);
  const lines = await sec.locator('.w-agent-words .when').allTextContents();
  ok(/^6 messages today · last /.test(lines[0]) && /^Quiet today · last /.test(lines[1]) && lines[2] === 'Not running', `${n}: each agent: messages and last time, quiet, or not running (${lines.join(' | ')})`);
  ok(await sec.locator('.w-agent.idle').count() === 1, `${n}: one that is not running is dimmed`);
  ok(/never what was said/.test(await sec.textContent()), `${n}: and it says it is counts only`);
  await page.screenshot({ path: `${OUT}/agents-widget-${n}.png`, clip: { x: 0, y: 0, width: w, height: 420 } });

  // the switch
  await page.goto('http://127.0.0.1:8779/#/settings'); await page.waitForTimeout(2000);
  const row = page.locator('.set-row', { hasText: 'Metrics' });
  ok(await row.count() === 1 && (await row.locator('.set-value').textContent()) === 'Off', `${n}: Settings › Metrics, "Off"`);
  await row.first().click(); await page.waitForTimeout(800);
  ok(await page.getByRole('button', { name: 'Turn on' }).count() === 1 && /Numbers only/.test(await page.locator('details.colours-more').textContent()), `${n}: off, with what it would share`);
  await page.getByRole('button', { name: 'Turn on' }).click(); await page.waitForTimeout(600);
  ok(await page.locator('.w-token').textContent() === TOKEN, `${n}: on: the token, shown`);
  const cfg = await page.locator('.w-config').textContent();
  ok(cfg.includes(`credentials: ${TOKEN}`) && cfg.includes("targets: ['127.0.0.1:8779']") && cfg.includes('metrics_path: /metrics'), `${n}: with a Prometheus config ready to paste`);
  await page.screenshot({ path: `${OUT}/metrics-page-${n}.png`, fullPage: !mob });
  await page.reload(); await page.waitForTimeout(1500);
  ok(await page.locator('.w-token').count() === 0 && /shown when you turned it on/.test(await page.locator('.settings-body').textContent()), `${n}: reloaded, the token is not shown again`);
  await page.getByRole('button', { name: 'New token' }).click(); await page.waitForTimeout(300);
  await page.locator('.ask-panel').getByRole('button', { name: 'New token' }).click(); await page.waitForTimeout(500);
  ok(await page.locator('.w-token').textContent() === TOKEN + '2' && puts.at(-1).rotate === true, `${n}: a new token replaces it`);
  await page.getByRole('button', { name: 'Turn off' }).click(); await page.waitForTimeout(300);
  await page.locator('.ask-panel').getByRole('button', { name: 'Turn off' }).click(); await page.waitForTimeout(500);
  ok(puts.at(-1).enabled === false && await page.getByRole('button', { name: 'Turn on' }).count() === 1, `${n}: turned off again`);
  ok(errs.length === 0, `${n}: no page errors ${errs.join('|')}`);
  await ctx.close();
}
// A member (not whoever manages Logos) neither sees the Metrics row nor gets the page from a typed-in link.
for (const [w, hgt, mob, n] of [[412, 915, true, 'phone'], [1440, 900, false, 'desktop']]) {
  const page = await browser.newPage({ viewport: { width: w, height: hgt }, isMobile: mob, hasTouch: mob });
  let asked = false;
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json(); me.role = 'user'; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/metrics', (r) => { asked = true; return r.fulfill({ status: 403, json: { error: 'forbidden' } }); });
  await page.goto('http://127.0.0.1:8779/#/settings'); await page.waitForTimeout(2000);
  ok(await page.locator('.set-row', { hasText: 'Metrics' }).count() === 0, `${n}: a member does not see Settings › Metrics`);
  await page.goto('http://127.0.0.1:8779/#/settings/metrics'); await page.waitForTimeout(1500);
  ok(await page.getByText('Prometheus metrics').count() === 0 && await page.getByRole('button', { name: 'Turn on' }).count() === 0 && !asked,
    `${n}: and a typed-in link shows nothing of it, nor asks the server`);
  await page.close();
}
await browser.close();
