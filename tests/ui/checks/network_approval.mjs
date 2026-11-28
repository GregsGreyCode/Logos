// Story E6: a site an agent's sandbox refused shows under Needs you with Allow once / Always allow / Deny,
// and the admin's People page lists what each agent may reach.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const now = Date.now() / 1000;
const ask = { id: 'net:abc123', kind: 'network', type: 'needs', ts: now - 60, agent: 'greg', who: "Greg's agent", rid: 'abc123',
  host: 'example.org', port: 443, title: "Greg's agent wants to reach example.org", text: 'For: Train times to Lyon · Using curl', link: '#/noticeboard' };
const done = { id: 'netlog:greg:pypi.org:1', kind: 'network_done', type: 'update', ts: now - 3600, agent: 'greg', who: "Greg's agent",
  title: "Greg's agent · pypi.org", text: 'You allowed it from now on' };
let decided = false;
for (const [w, hgt, mobile, tag] of [[412, 915, true, 'phone'], [1280, 860, false, 'desk']]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: hgt }, isMobile: mobile, hasTouch: mobile, deviceScaleFactor: mobile ? 2 : 1 });
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push((e.stack || e.message).split('\n').slice(0, 2).join(' < ')));
  decided = false;
  const posts = [];
  await page.route('**/api/v2/notifications', (r) => r.fulfill({ json: { items: decided ? [done] : [ask, done], seen_until: 0, read_ids: [] } }));
  await page.route('**/api/v2/network/*/*', (r) => { posts.push(new URL(r.request().url()).pathname); decided = true;
    r.fulfill({ json: { ok: true, decision: 'always', agent: 'greg', host: 'example.org', port: 443 } }); });
  await page.goto('http://127.0.0.1:8779/#/noticeboard'); await page.waitForTimeout(2500);
  const card = page.locator('.notifications .network-ask');
  ok(await card.count() === 1, `${tag}: the question is a card under Needs you`);
  ok((await card.textContent()).includes("Greg's agent wants to reach example.org") && (await card.textContent()).includes('Train times to Lyon'),
    `${tag}: it says who, which site and what for`);
  const labels = await card.locator('button').allTextContents();
  ok(JSON.stringify(labels) === JSON.stringify(['Allow once', 'Always allow', 'Deny']), `${tag}: three answers (${labels.join(', ')})`);
  const nl = await page.locator('section[aria-label="Needs you"] h2.label').textContent(); ok(/^Needs you · \d+$/.test(nl), `${tag}: counted under Needs you (${nl})`);
  const box = await card.boundingBox();
  ok(box && box.x >= 0 && box.x + box.width <= w + 1, `${tag}: the card fits the screen`);
  ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `${tag}: no sideways scroll`);
  ok(await page.locator('.feed-item.kind-network_done').count() === 1, `${tag}: an answer shows in the day's list`);
  await page.screenshot({ path: `${process.argv[2]}/network-${tag}.png` });
  await card.getByRole('button', { name: 'Always allow' }).click(); await page.waitForTimeout(300);
  ok(posts[0] === '/api/v2/network/abc123/always', `${tag}: Always allow posts the answer (${posts[0]})`);
  ok((await card.textContent()).includes('Always allowed.'), `${tag}: and says so`);
  await page.waitForTimeout(1800);
  ok(await page.locator('.notifications .network-ask').count() === 0, `${tag}: then the question is gone`);
  ok(errs.length === 0, `${tag}: no page errors ${errs.join(' | ')}`);
  await ctx.close();
}
// The admin's People page: the sites beyond Logos each agent may reach, with Remove.
{
  const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  const removed = [];
  await page.route('**/api/v2/people', (r) => r.fulfill({ json: { is_admin: true, people: [{ username: 'greg', display_name: 'Greg', is_you: true, is_admin: true, agents: { greg: true } }],
    agents: [{ name: 'greg', display_name: "Greg's agent" }] } }));
  await page.route('**/api/v2/network', (r) => r.fulfill({ json: { is_admin: true, requests: [], agents: [
    { name: 'greg', display_name: "Greg's agent", yours: true, people: ['greg'], always: [{ host: 'pypi.org', port: 443, by: 'greg', at: now }],
      once: [{ host: 'example.org', port: 8443, by: 'greg', at: now, until: now + 3000 }], deny: [] },
    { name: 'household', display_name: 'Household agent', yours: false, people: ['greg', 'bere'], always: [], once: [], deny: [] }] } }));
  await page.route('**/api/v2/network-sites/*/remove', (r) => { removed.push(r.request().postData()); r.fulfill({ json: { ok: true } }); });
  await page.goto('http://127.0.0.1:8779/#/people'); await page.waitForTimeout(2000);
  const sites = page.locator('.network-sites');
  ok(await sites.isVisible(), 'people: the admin sees the sites each agent may reach');
  const text = await sites.textContent();
  ok(text.includes('pypi.org') && text.includes('example.org:8443') && text.includes('Only Logos itself.'), 'people: per agent, with the port when not 443');
  await sites.getByRole('button', { name: 'Remove' }).first().click(); await page.waitForTimeout(300);
  ok(removed[0] && removed[0].includes('pypi.org'), `people: Remove takes it away (${removed[0]})`);
  await page.screenshot({ path: `${process.argv[2]}/network-people.png` });
  ok(errs.length === 0, `people: no page errors ${errs.join(' | ')}`);
  await ctx.close();
}
await browser.close();
