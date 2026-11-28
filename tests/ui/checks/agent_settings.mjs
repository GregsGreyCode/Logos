// Agent settings (E6): #/agents/<name>/settings on a phone and a desktop. Personality with a preview of
// what changes and its history, the model, the chat settings, and "Save and restart" with its progress.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });

for (const [label, opts] of [['phone', { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }],
  ['desktop', { viewport: { width: 1440, height: 900 } }]]) {
  const ctx = await browser.newContext(opts);
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  const reqs = []; page.on('request', (r) => { if (r.method() !== 'GET' && r.url().includes('/api/')) reqs.push(`${r.method()} ${new URL(r.url()).pathname} ${r.postData() || ''}`); });
  // Reached from the agent's Routines page.
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/agents/greg`); await page.waitForTimeout(1500);
  const link = page.locator('.routines-top a', { hasText: 'Settings' });
  ok(await link.isVisible(), `${label}: Routines links to the agent's settings`);
  await link.click(); await page.waitForTimeout(1500);
  ok(page.url().endsWith('#/agents/greg/settings'), `${label}: it opens #/agents/greg/settings`);
  const heads = await page.$$eval('.settings-card h2', (els) => els.map((e) => e.textContent));
  ok(heads.join('|') === 'Personality|Model|Chat|Apply', `${label}: four sections (${heads})`);
  const width = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  ok(width <= 0, `${label}: nothing wider than the screen (${width})`);
  if (label === 'phone') {
    ok(await page.locator('.agent-bar .agent-menu > summary').innerText().then((t) => t.startsWith('Settings')), 'phone: the agent bar says Settings');
    await page.locator('.agent-bar .agent-menu > summary').tap();
    const items = (await page.locator('.agent-bar .agent-menu-list a').allInnerTexts()).map((t) => t.replace(/[↗\s]+$/, ''));
    ok(items.join('|') === 'Chat|Brain|Routines|Settings', `phone: the menu has Settings (${items})`);
    await page.locator('.agent-bar .agent-menu > summary').tap();
  }
  // Personality: the text, a count, a preview of what changes.
  const soul = page.locator('#soul-text');
  ok((await soul.inputValue()).startsWith('# General'), `${label}: the agent's own soul is in the box`);
  ok((await page.locator('.chip-tag').first().innerText()) === 'The default', `${label}: it says it is the default`);
  await soul.fill((await soul.inputValue()) + '\nAnswer in French when spoken to in French.\n');
  ok((await page.locator('.soul-count').innerText()).includes('of 8,000 characters'), `${label}: a character count`);
  ok((await page.locator('.apply-status').innerText()).includes('One change not saved yet'), `${label}: it says a change is waiting`);
  await page.locator('button', { hasText: 'Preview changes' }).click(); await page.waitForTimeout(200);
  const added = await page.locator('.soul-preview .diff-line.add').allInnerTexts();
  ok(added.some((t) => t.includes('Answer in French')), `${label}: the preview shows the added line (${added})`);
  ok(await page.locator('.soul-preview .diff-line.del').count() === 0, `${label}: and nothing removed`);
  // History: compare an earlier version.
  await page.locator('.soul-history > summary').click();
  ok((await page.locator('.soul-version').count()) === 2, `${label}: two earlier versions`);
  await page.locator('.soul-version').first().locator('button', { hasText: 'Compare' }).click(); await page.waitForTimeout(300);
  ok((await page.locator('.soul-old .diff').count()) === 1, `${label}: an earlier version compares with the text`);
  // Model and chat settings.
  const models = await page.locator('#model-pick option').allInnerTexts();
  ok(models.length === 2 && models[0].includes('(default)') && models[1].includes('not loaded'), `${label}: the models on offer (${models})`);
  ok((await page.locator('#model-window option').allInnerTexts()).some((t) => t.startsWith('65,536 tokens (default)')), `${label}: the context window, default marked`);
  ok(await page.locator('#chat-name').inputValue() === "Greg's agent", `${label}: its name`);
  ok(await page.locator('#chat-echo').count() === 1, `${label}: voice note text on or off`);
  ok(await page.locator('a[href="#/settings/speaker"]').isVisible(), `${label}: its speaker voice links to Settings › House speaker`);
  await page.screenshot({ path: `${process.argv[2]}/agent_settings_${label}.png`, fullPage: true });
  // Save and restart: saves the soul, asks, starts the restart, shows its progress, then that it is back.
  await page.locator('button', { hasText: 'Save and restart the agent' }).click(); await page.waitForTimeout(400);
  ok(reqs.some((r) => r.startsWith('PUT /api/v2/agents/greg/settings') && r.includes('Answer in French')), `${label}: the soul is saved`);
  ok(await page.locator('.ask-panel').isVisible() && (await page.locator('.ask-panel').innerText()).includes('about a minute'), `${label}: it asks first, saying how long`);
  await page.locator('.ask-panel button[type=submit]').click(); await page.waitForTimeout(300);
  ok(reqs.some((r) => r.startsWith('POST /api/v2/agents/greg/settings/apply')), `${label}: then restarts the agent`);
  ok((await page.locator('.apply-status').innerText()).includes('Recreating'), `${label}: progress is shown (${await page.locator('.apply-status').innerText()})`);
  ok(await page.locator('#soul-text').isDisabled(), `${label}: nothing can be changed meanwhile`);
  await page.waitForTimeout(2600);
  ok((await page.locator('.apply-status').innerText()).includes('is back'), `${label}: and when it is back (${await page.locator('.apply-status').innerText()})`);
  ok(!(await page.locator('#soul-text').isDisabled()), `${label}: the page works again`);
  await page.screenshot({ path: `${process.argv[2]}/agent_settings_${label}_done.png` });
  ok(errs.length === 0, `${label}: no page errors ${errs.join(' | ')}`);
  await ctx.close();
}
await browser.close();
