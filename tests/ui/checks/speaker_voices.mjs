// Settings › Speaker (Voice V1.2; settings, not a status page, since 28 Nov): for each agent, its languages
// with a switch each, the voice for each language with ▶ and Change, and the full list in a sheet (phone) or
// a dialog (desktop), grouped by language, each voice with its sample.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME, args: ['--autoplay-policy=no-user-gesture-required'] });
const errs = [];

for (const phone of [true, false]) {
  const n = phone ? 'phone' : 'desktop';
  const page = await browser.newPage(phone ? { viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true } : { viewport: { width: 1440, height: 900 } });
  const tap = (l) => (phone ? l.tap() : l.click());
  page.on('pageerror', (e) => errs.push(e.message));
  const asked = [];
  page.on('request', (r) => { if (/speaker\/sample|speaker-voice|speaker-languages/.test(r.url())) asked.push(`${r.method()} ${r.url().replace(/^https?:\/\/[^/]+/, '')} ${r.postData() || ''}`); });
  await page.goto('http://127.0.0.1:8779/#/settings'); await page.waitForTimeout(2500);
  const row = page.locator('.set-row', { hasText: 'Speaker' });
  ok(await row.count() === 1 && (await row.locator('.set-value').textContent()) === 'Northern English', `${n}: the Speaker row says Home's voice`);
  await tap(row); await page.waitForTimeout(1500);
  ok(page.url().endsWith('#/settings/speaker'), `${n}: it opens its own page`);
  if (phone) ok((await page.locator('#screen-name').textContent()) === 'Speaker', `${n}: titled as the row is named`);
  else ok((await page.locator('.set-crumb-page').textContent()) === 'Speaker', `${n}: Settings › Speaker over the page`);
  ok(/Speaker voices on/.test(await page.locator('.speaker-service').textContent()), `${n}: how the speaker is doing is one small line`);

  const home = page.locator('section[aria-label="Household agent\'s voice"]');
  ok(await home.locator('.speaker-lang').count() === 2, `${n}: Household: English and French, each a line`);
  ok(await home.locator('.speaker-lang input[type=checkbox]:checked').count() === 2, `${n}: both on`);
  ok(/Northern English/.test(await home.locator('.speaker-lang[data-lang="en"]').textContent())
    && /Tom/.test(await home.locator('.speaker-lang[data-lang="fr"]').textContent()), `${n}: with the voice for each`);
  ok(/either of you/.test(await home.textContent()) && /last answered/.test(await home.locator('.speaker-status').textContent()), `${n}: shared, and when it last answered, in its status line`);
  ok(/Not on the speaker yet/.test(await page.locator('section[aria-label="Greg\'s agent\'s voice"]').textContent()), `${n}: an agent not on the speaker says so`);
  const fits = await page.$$eval('.speaker-lang', (els) => els.every((e) => e.getBoundingClientRect().right <= innerWidth));
  ok(fits, `${n}: every line fits the screen`);
  if (phone) await page.screenshot({ path: `${OUT}/speaker-${n}.png`, fullPage: true });

  // ▶ beside the current voice.
  await tap(home.locator('.speaker-lang[data-lang="en"] .voice-play')); await page.waitForTimeout(700);
  ok(asked.some((a) => a.startsWith('GET /api/v2/speaker/sample?voice=en_GB-northern_english_male-medium')), `${n}: ▶ plays the current voice`);

  // Languages: French off, then English can no longer be switched off (one stays on).
  await tap(home.locator('.speaker-lang[data-lang="fr"] .switch')); await page.waitForTimeout(600);
  ok(asked.some((a) => a.startsWith('PUT /api/v2/agents/household/speaker-languages') && a.includes('"off":["fr"]')), `${n}: switching French off saves it`);
  ok(/answered in English instead/.test(await home.locator('.speaker-lang[data-lang="fr"]').textContent()), `${n}: and says it is answered in English instead`);
  ok(await home.locator('.speaker-lang[data-lang="en"] input').isDisabled(), `${n}: the last language on cannot be switched off`);
  await tap(home.locator('.speaker-lang[data-lang="fr"] .switch')); await page.waitForTimeout(600);
  ok(asked.some((a) => a.includes('speaker-languages') && a.includes('"off":[]')), `${n}: and back on`);

  // Add a language: only what Logos can speak; the rest says what it needs.
  await tap(home.locator('.speaker-add')); await page.waitForTimeout(300);
  const add = page.locator('.pick-panel[aria-label="Add a language"]');
  ok(await add.isVisible() && /German/.test(await add.textContent()) && /Needs a voice installed first/.test(await add.textContent()), `${n}: Add a language lists what needs a voice first`);
  await tap(add.locator('.pick-close')); await page.waitForTimeout(200);

  // Change: the full list, grouped by language, with samples.
  await tap(home.locator('.speaker-lang[data-lang="en"] .voice-change')); await page.waitForTimeout(400);
  const sheet = page.locator('.pick-panel[aria-label="Household agent\'s voice"]');
  ok(await sheet.isVisible(), `${n}: Change opens the voices in a ${phone ? 'sheet' : 'dialog'}`);
  const groups = await sheet.locator('.pick-group h3').allTextContents();
  ok(JSON.stringify(groups) === JSON.stringify(['English', 'French']), `${n}: grouped by language (${groups})`);
  ok(await sheet.locator('.pick-group[data-lang="en"] .voice-row').count() === 12 && await sheet.locator('.pick-group[data-lang="fr"] .voice-row').count() === 4, `${n}: twelve English voices, four French`);
  ok(await sheet.locator('.pick-group[data-lang="en"] input:checked').getAttribute('value') === 'en_GB-northern_english_male-medium', `${n}: its voice is chosen`);
  ok(/its usual voice/.test(await sheet.locator('.voice-row', { hasText: 'Northern English' }).textContent()), `${n}: and marked as its usual one`);
  if (phone) {
    const box = await sheet.boundingBox();
    ok(box && Math.abs(box.y + box.height - 915) < 4, `${n}: a sheet from the bottom`);
  } else {
    const box = await sheet.boundingBox();
    ok(box && box.y > 20 && box.width <= 560, `${n}: a dialog in the middle`);
  }
  await page.screenshot({ path: `${OUT}/speaker-picker-${n}.png` });
  await tap(sheet.locator('.voice-row', { hasText: 'Ryan' }).locator('.voice-play')); await page.waitForTimeout(700);
  ok(asked.some((a) => a.startsWith('GET /api/v2/speaker/sample?voice=en_US-ryan-medium')), `${n}: ▶ in the list plays that voice`);
  ok(await sheet.locator('.pick-group[data-lang="en"] input:checked').getAttribute('value') === 'en_GB-northern_english_male-medium', `${n}: playing a sample does not choose the voice`);
  await tap(sheet.locator('.voice-row', { hasText: 'Alba' }).locator('label')); await page.waitForTimeout(600);
  ok(asked.some((a) => a.startsWith('PUT /api/v2/agents/household/speaker-voice') && a.includes('"voice":"en_GB-alba-medium"')), `${n}: choosing a voice saves it`);
  ok(/Saved: Household agent speaks as Alba/.test(await sheet.locator('.appearance-note').textContent()), `${n}: and says so`);
  await tap(sheet.locator('.pick-group[data-lang="fr"] .voice-row', { hasText: 'Siwis' }).locator('label')); await page.waitForTimeout(600);
  ok(asked.some((a) => a.includes('speaker-voice') && a.includes('"voice_fr":"fr_FR-siwis-medium"')), `${n}: a French voice is saved as the French one`);
  await tap(sheet.locator('.pick-close')); await page.waitForTimeout(300);
  ok(/Alba/.test(await home.locator('.speaker-lang[data-lang="en"]').textContent()) && /Siwis/.test(await home.locator('.speaker-lang[data-lang="fr"]').textContent()),
    `${n}: closed, the card shows the new voices`);
  if (!phone) await page.screenshot({ path: `${OUT}/speaker-${n}.png` });
  await page.close();
}
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
