// An agent that asked for a new name (Greg, 28 Nov): a card under Needs you with Confirm / Not now; nothing
// changes until a person taps Confirm, and then it says what changed (the new wake word too).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const now = Date.now() / 1000;
const ask = { id: 'rename:r1', kind: 'rename', type: 'needs', ts: now - 60, agent: 'greg', who: "Greg's agent", rid: 'r1',
  display_name: 'Finn', speaker_name: 'Finn', title: "Greg's agent wants to be called Finn",
  text: 'Asked in its chat. At the house speaker it would wake to "Hey Finn" instead of "Hey Logos".' };
const done = { id: 'renamelog:greg:1', kind: 'rename_done', type: 'update', ts: now - 30, agent: 'greg', who: 'Finn',
  title: "Greg's agent is now Finn", text: 'You confirmed it. Say "Hey Finn" at the house speaker; "Hey Logos" no longer wakes it' };
for (const [w, hgt, mobile, tag] of [[412, 915, true, 'phone'], [1280, 860, false, 'desk']]) {
  for (const answer of ['confirm', 'dismiss']) {
    const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mobile, hasTouch: mobile, deviceScaleFactor: mobile ? 2 : 1 });
    const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push((e.stack || e.message).split('\n').slice(0, 2).join(' < ')));
    let decided = false; const posts = [];
    await page.route('**/api/v2/notifications', (r) => r.fulfill({ json: { items: decided ? [done] : [ask], seen_until: 0, read_ids: [] } }));
    await page.route('**/api/v2/agent-rename/*/*', (r) => { posts.push(new URL(r.request().url()).pathname); decided = true;
      r.fulfill({ json: answer === 'confirm'
        ? { ok: true, decision: 'confirm', message: "Greg's agent is now called Finn. At the house speaker say \"Hey Finn\"; \"Hey Logos\" no longer wakes it." }
        : { ok: true, decision: 'dismiss', message: "Greg's agent keeps its name." } }); });
    await page.goto('http://127.0.0.1:8779/#/noticeboard'); await page.waitForTimeout(2500);
    const card = page.locator('.notifications .rename-ask');
    ok(await card.count() === 1, `${tag}/${answer}: the question is a card under Needs you`);
    const text = await card.textContent();
    ok(text.includes("Greg's agent wants to be called Finn") && text.includes('"Hey Finn"'), `${tag}/${answer}: it says who, the new name and the wake word`);
    const labels = await card.locator('button').allTextContents();
    ok(JSON.stringify(labels) === JSON.stringify(['Confirm', 'Not now']), `${tag}/${answer}: two answers (${labels.join(', ')})`);
    const box = await card.boundingBox();
    ok(box && box.x >= 0 && box.x + box.width <= w + 1, `${tag}/${answer}: the card fits the screen`);
    ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `${tag}/${answer}: no sideways scroll`);
    if (answer === 'confirm') await page.screenshot({ path: `${process.argv[2]}/rename-${tag}.png` });
    await card.getByRole('button', { name: answer === 'confirm' ? 'Confirm' : 'Not now' }).click(); await page.waitForTimeout(300);
    ok(posts[0] === `/api/v2/agent-rename/r1/${answer}`, `${tag}/${answer}: posts the answer (${posts[0]})`);
    const said = await card.textContent();
    ok(answer === 'confirm' ? said.includes('"Hey Logos" no longer wakes it') : said.includes('keeps its name'), `${tag}/${answer}: and says what happened`);
    if (answer === 'confirm') await page.screenshot({ path: `${process.argv[2]}/rename-${tag}-done.png` });
    await page.waitForTimeout(2800);
    ok(await page.locator('.notifications .rename-ask').count() === 0, `${tag}/${answer}: then the question is gone`);
    ok(await page.locator('.feed-item.kind-rename_done').count() === 1, `${tag}/${answer}: the answer shows in the day's list`);
    ok(errs.length === 0, `${tag}/${answer}: no page errors ${errs.join(' | ')}`);
    await ctx.close();
  }
}
await browser.close();
