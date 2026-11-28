// Settings › Household words (28 Nov): names speech-to-text gets wrong. A row in Settings with how many;
// its page lists the words (people's and agents' names joined by themselves, no Remove on those); add a word,
// edit what speech-to-text writes, remove one; an empty list says so. Made-up names. Honours $PORT.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const OUT = process.argv[2] || 'out';
const BASE = `http://127.0.0.1:${process.env.PORT || 8779}`;
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const START = [
  { word: 'Zoé', heard_as: ['Zoey', 'Joey'], note: 'Sam’s sister' },
  { word: 'Sam', heard_as: [], source: 'person' },
  { word: 'Quill', heard_as: [], source: 'agent' }];
const SOURCES = { Sam: 'person', Quill: 'agent' };

for (const phone of [true, false]) {
  const n = phone ? 'phone' : 'desktop';
  const page = await browser.newPage(phone ? { viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true } : { viewport: { width: 1440, height: 900 } });
  const tap = (l) => (phone ? l.tap() : l.click());
  const errs = [];
  page.on('pageerror', (e) => errs.push(e.message));
  let words = START.map((w) => ({ ...w })), puts = [];
  await page.route('**/api/v2/household-words', (r) => {
    if (r.request().method() === 'PUT') {
      const body = JSON.parse(r.request().postData());
      puts.push(body);
      const got = body.words.map((w) => ({ ...w, ...(SOURCES[w.word] ? { source: SOURCES[w.word] } : {}) }));
      words = [...got, ...Object.entries(SOURCES).filter(([k]) => !got.some((w) => w.word === k)).map(([k, v]) => ({ word: k, heard_as: [], source: v }))];
    }
    return r.fulfill({ json: { max: 100, words } });
  });

  await page.goto(`${BASE}/#/settings`); await page.waitForTimeout(1800);
  const row = page.locator('a.set-row[href="#/settings/words"]');
  ok(await row.count() === 1 && /Household words/.test(await row.textContent()), `${n}: a Household words row in Settings`);
  ok(/3 words/.test(await row.locator('.set-value').textContent()), `${n}: it says how many`);
  await tap(row); await page.waitForTimeout(1200);
  ok(page.url().endsWith('#/settings/words'), `${n}: the row opens its page`);
  const rows = page.locator('.hw-row');
  ok(await rows.count() === 3, `${n}: one row per word`);
  const zoe = page.locator('.hw-row[data-word="Zoé"]');
  ok(/Written as Zoey, Joey/.test(await zoe.textContent()) && /Sam’s sister/.test(await zoe.textContent()), `${n}: what speech-to-text writes, and the note`);
  ok(/A person here/.test(await page.locator('.hw-row[data-word="Sam"]').textContent())
    && /An agent’s name/.test(await page.locator('.hw-row[data-word="Quill"]').textContent()), `${n}: people's and agents' names say where they come from`);
  ok(await page.locator('.hw-row[data-word="Sam"] button', { hasText: 'Remove' }).count() === 0
    && await zoe.locator('button', { hasText: 'Remove' }).count() === 1, `${n}: Remove only on words added by hand`);
  if (phone) ok(await page.locator('.topbar .topbar-back[href="#/settings"]').isVisible(), `${n}: a back arrow to Settings`);
  const wide = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
  ok(wide, `${n}: no sideways scroll`);
  await page.screenshot({ path: `${OUT}/household-words-${n}.png`, fullPage: phone });

  // Add a word.
  await tap(page.locator('.hw-add')); await page.waitForTimeout(200);
  const add = page.locator('form[aria-label="Add a word"]');
  ok(await add.isVisible() && !(await page.locator('.hw-add').isVisible()), `${n}: Add a word opens a form`);
  await add.locator('input').nth(0).fill('Anaïs');
  await add.locator('input').nth(1).fill('Annie Ice,  Anise ,');
  await page.screenshot({ path: `${OUT}/household-words-add-${n}.png`, fullPage: phone });
  await tap(add.locator('button[type="submit"]')); await page.waitForTimeout(500);
  const sent = puts.at(-1);
  ok(sent && sent.words[0].word === 'Anaïs' && JSON.stringify(sent.words[0].heard_as) === '["Annie Ice","Anise"]', `${n}: it saves the whole list, the new word first`);
  ok(await page.locator('.hw-row').count() === 4 && /Speech-to-text uses it within a minute/.test(await page.locator('[role="status"].when').last().textContent()), `${n}: the new word is listed, and when it is used`);

  // Edit a person's name: what speech-to-text writes for it.
  await tap(page.locator('.hw-row[data-word="Sam"] button', { hasText: 'Edit' })); await page.waitForTimeout(200);
  const edit = page.locator('form[aria-label="Edit Sam"]');
  ok(await edit.locator('input').nth(0).getAttribute('readonly') !== null, `${n}: a person's name itself is not changed here`);
  await edit.locator('input').nth(1).fill('Psalm');
  await tap(edit.locator('button[type="submit"]')); await page.waitForTimeout(500);
  ok(puts.at(-1).words.some((w) => w.word === 'Sam' && w.heard_as[0] === 'Psalm'), `${n}: Edit saves what it writes`);
  ok(/Written as Psalm/.test(await page.locator('.hw-row[data-word="Sam"]').textContent()), `${n}: and shows it`);

  // A word already there is not added twice.
  await tap(page.locator('.hw-add')); await page.waitForTimeout(200);
  await page.locator('form[aria-label="Add a word"] input').nth(0).fill('zoé');
  const before = puts.length;
  await tap(page.locator('form[aria-label="Add a word"] button[type="submit"]')); await page.waitForTimeout(300);
  ok(puts.length === before && /on the list already/.test(await page.locator('[role="status"].when').last().textContent()), `${n}: a word on the list already is not added twice`);
  await tap(page.locator('form[aria-label="Add a word"] button', { hasText: 'Cancel' })); await page.waitForTimeout(200);

  // Remove.
  await tap(zoe.locator('button', { hasText: 'Remove' })); await page.waitForTimeout(500);
  ok(!puts.at(-1).words.some((w) => w.word === 'Zoé') && await page.locator('.hw-row[data-word="Zoé"]').count() === 0, `${n}: Remove takes it off the list`);

  // Empty: it says so.
  words = []; await page.unroute('**/api/v2/household-words');
  await page.route('**/api/v2/household-words', (r) => r.fulfill({ json: { max: 100, words: [] } }));
  await page.goto(`${BASE}/#/settings/words`); await page.reload(); await page.waitForTimeout(1200);
  ok(/No household words yet/.test(await page.locator('.hw-list').textContent()), `${n}: an empty list says so`);
  ok(errs.length === 0, `${n}: no page errors${errs.length ? `: ${errs[0]}` : ''}`);
  await page.close();
}
await browser.close();
