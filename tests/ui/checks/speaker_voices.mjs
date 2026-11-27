// Settings › House speaker (Voice V1.2): each agent's voice, with a sample to play for every voice.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const browser = await chromium.launch({ executablePath: process.env.CHROME, args: ['--autoplay-policy=no-user-gesture-required'] });

// Phone: the row says Home's voice, and opens its page.
let page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
const asked = [];
page.on('request', (r) => { if (r.url().includes('/api/v2/speaker/sample') || r.url().includes('/speaker-voice')) asked.push(`${r.method()} ${r.url().replace(/^https?:\/\/[^/]+/, '')} ${r.postData() || ''}`); });
await page.goto('http://127.0.0.1:8779/#/settings'); await page.waitForTimeout(2500);
const row = page.locator('.set-row', { hasText: 'Speaker' });
ok(await row.count() === 1, 'a Speaker row under You');
ok((await row.locator('.set-value').textContent()) === 'Northern English', `it says Home's voice (${await row.locator('.set-value').textContent()})`);
await row.tap(); await page.waitForTimeout(1500);
ok(page.url().endsWith('#/settings/speaker') && (await page.locator('#screen-name').textContent()) === 'House speaker', 'it opens its own page');
const home = page.locator('section[aria-label="Household agent\'s voice"]');
const homeEn = home.locator('[aria-label="Household agent: voice"]'), homeFr = home.locator('[aria-label="Household agent: French voice"]');
ok(await home.count() === 1 && await homeEn.locator('.voice-row').count() === 12, 'Household: all twelve English voices');
ok(await homeFr.locator('.voice-row').count() === 4 && /In French/.test(await home.textContent()), 'and below them, four French voices');
ok(await homeFr.locator('input[type=radio]:checked').getAttribute('value') === 'fr_FR-tom-medium', 'its French voice is chosen');
ok(await homeEn.locator('input[type=radio]:checked').getAttribute('value') === 'en_GB-northern_english_male-medium', 'its own voice is chosen');
ok(/its usual voice/.test(await home.locator('.voice-row', { hasText: 'Northern English' }).textContent()), 'and marked as its usual one');
ok(/either of you/.test(await home.textContent()), 'shared: either person can change it');
const greg = page.locator('details', { hasText: "Greg's agent" });
ok(await greg.count() === 1 && !(await greg.getAttribute('open') !== null), "Greg's agent (not on the speaker yet) is folded away");
ok(/Not on the speaker yet/.test(await greg.locator('section').textContent()), 'and says why');
ok(await home.locator('.voice-play svg').first().isVisible(), 'each play button shows its icon');
const fits = await page.$$eval('.voice-row', (els) => els.every((e) => e.getBoundingClientRect().right <= innerWidth));
ok(fits, 'every voice and its play button fit the screen');
await home.locator('.voice-row', { hasText: 'Alba' }).locator('label').tap(); await page.waitForTimeout(600);
ok(asked.some((a) => a.startsWith('PUT /api/v2/agents/household/speaker-voice') && a.includes('en_GB-alba-medium')), 'choosing a voice saves it for that agent');
ok(/Saved: Household agent speaks as Alba/.test(await home.locator('.appearance-note').first().textContent()), 'and says so');
await home.locator('.voice-row', { hasText: 'Northern English' }).locator('label').tap(); await page.waitForTimeout(600);
ok(asked.some((a) => a.startsWith('PUT') && a.includes('"voice":""')), 'back to its usual voice: the stored choice is cleared');
await home.locator('.voice-row', { hasText: 'Ryan' }).locator('.voice-play').tap(); await page.waitForTimeout(800);
ok(asked.some((a) => a.startsWith('GET /api/v2/speaker/sample?voice=en_US-ryan-medium')), 'play asks for that voice\'s sample');
ok(await home.locator('.voice-row', { hasText: 'Ryan' }).locator('.voice-play').getAttribute('aria-label') === 'Stop the sample', 'and while it plays, the button stops it');
ok(await homeEn.locator('input[type=radio]:checked').getAttribute('value') === 'en_GB-northern_english_male-medium', 'playing a sample does not choose the voice');
await homeFr.locator('.voice-row', { hasText: 'Siwis' }).locator('label').tap(); await page.waitForTimeout(600);
ok(asked.some((a) => a.startsWith('PUT /api/v2/agents/household/speaker-voice') && a.includes('"voice_fr":"fr_FR-siwis-medium"')), 'choosing a French voice saves it as the French one');
ok(/Saved: Household agent speaks French as Siwis/.test(await home.locator('.appearance-note').last().textContent()), 'and says so');
ok(await homeEn.locator('input[type=radio]:checked').getAttribute('value') === 'en_GB-northern_english_male-medium', 'the English choice is untouched');
await page.screenshot({ path: `${OUT}/speaker-phone.png`, fullPage: true });
await page.close();

// Desktop: beside the list.
page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on('pageerror', (e) => errs.push(e.message));
await page.goto('http://127.0.0.1:8779/#/settings/speaker'); await page.waitForTimeout(2000);
ok(await page.locator('.settings-layout .set-row[aria-current="page"]', { hasText: 'Speaker' }).count() === 1, 'desktop: marked in the list, its page beside it');
await page.screenshot({ path: `${OUT}/speaker-desktop.png` });
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
