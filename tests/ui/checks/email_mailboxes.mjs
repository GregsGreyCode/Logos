// F43 (27 Nov): several mailboxes per person, Gmail and Outlook mixed. One card each (status, private
// senders, Disconnect) and "+ Add another mailbox", on a phone and on a desktop.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const sizes = [['phone', { viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }],
               ['desktop', { viewport: { width: 1280, height: 860 } }]];
for (const [label, opts] of sizes) {
  const ctx = await browser.newContext(opts);
  const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
  const press = (loc) => (opts.hasTouch ? loc.tap() : loc.click());
  const S = { agent: 'greg', outlook_ready: true, is_admin: true, signin: null, mailboxes: [
    { id: 'aaaa0001', kind: 'outlook', address: 'greg@hotmail.example', exclude: ['mybank.example'], health: { synced: '2026-11-27T09:00:00Z', error: null } }] };
  const sent = [];
  await page.route('**/api/v2/mail**', async (route) => {
    const r = route.request(), u = new URL(r.url()), body = r.postData() ? JSON.parse(r.postData()) : null;
    sent.push([r.method(), u.pathname + u.search, body]);
    if (r.method() === 'GET') return route.fulfill({ json: S });
    if (u.pathname.endsWith('/gmail')) {
      S.mailboxes.push({ id: 'cccc0002', kind: 'gmail', address: body.address, exclude: [], health: null });
      return route.fulfill({ json: { connected: true, inbox: 7, id: 'cccc0002' } });
    }
    if (u.pathname.endsWith('/exclude')) {
      const b = S.mailboxes.find((m) => m.id === body.box); b.exclude = [...b.exclude, body.sender];
      return route.fulfill({ json: { exclude: b.exclude, id: b.id } });
    }
    if (r.method() === 'DELETE') { S.mailboxes = S.mailboxes.filter((m) => m.id !== u.searchParams.get('box')); return route.fulfill({ json: { disconnected: true } }); }
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/settings/email`); await page.waitForTimeout(1500);
  ok(await page.locator('.mail-card').count() === 1 && await page.getByRole('button', { name: '+ Add another mailbox' }).count() === 1,
    `${label}: one card, and Add another mailbox`);
  await press(page.getByRole('button', { name: '+ Add another mailbox' })); await page.waitForTimeout(300);
  ok(await page.getByText('Which email is the other mailbox?').count() === 1, `${label}: it asks which kind`);
  await press(page.getByRole('button', { name: 'Back' })); await page.waitForTimeout(400);
  ok(await page.locator('.mail-card').count() === 1, `${label}: Back returns to the mailboxes`);
  await press(page.getByRole('button', { name: '+ Add another mailbox' })); await page.waitForTimeout(300);
  await press(page.getByRole('button', { name: 'Gmail' })); await page.waitForTimeout(300);
  await page.fill('#mail-addr', 'greg@gmail.example'); await page.fill('#mail-pw', 'abcd efgh ijkl mnop');
  await press(page.getByRole('button', { name: 'Connect' })); await page.waitForTimeout(700);
  const cards = page.locator('.mail-card');
  ok(await cards.count() === 2 && await cards.nth(0).getByText('greg@hotmail.example').count() === 1
     && await cards.nth(1).getByText('greg@gmail.example').count() === 1, `${label}: the second mailbox is added, not swapped in`);
  ok((await cards.nth(1).innerText()).includes('Gmail') && (await cards.nth(0).innerText()).includes('Outlook.com'),
    `${label}: Gmail and Outlook side by side`);
  // private senders belong to one mailbox
  await press(cards.nth(1).locator('.mail-private summary')); await page.waitForTimeout(200);
  await cards.nth(1).locator('input[aria-label^="A sender to keep private"]').fill('doctor.example');
  await press(cards.nth(1).getByRole('button', { name: 'Add' })); await page.waitForTimeout(500);
  const ex = sent.find(([m, p]) => p.endsWith('/exclude'));
  ok(ex && ex[2].box === 'cccc0002' && ex[2].sender === 'doctor.example', `${label}: a private sender is kept for that mailbox only`);
  ok(await page.locator('.mail-card').nth(1).locator('.mail-chip', { hasText: 'doctor.example' }).count() === 1
     && await page.locator('.mail-card').nth(0).locator('.mail-chip', { hasText: 'doctor.example' }).count() === 0
     && await page.locator('.mail-card').nth(1).locator('.mail-private[open]').count() === 1, `${label}: its chip shows on its card, and the fold stays open`);
  const wide = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
  ok(wide, `${label}: no sideways scrolling`);
  // (a full-page shot resizes the window, and the desktop layout then draws the page again)
  await page.screenshot({ path: `${process.argv[2]}/mail-two-${label}.png`, fullPage: label === 'phone' });
  // disconnect only one of them
  await press(page.getByRole('button', { name: 'Disconnect greg@hotmail.example' })); await page.waitForTimeout(300);
  ok(await page.locator('.ask-layer').getByText('Disconnect greg@hotmail.example?').count() === 1, `${label}: Disconnect names the mailbox`);
  await press(page.locator('.ask-layer').getByRole('button', { name: 'Disconnect' })); await page.waitForTimeout(600);
  ok(sent.some(([m, p]) => m === 'DELETE' && p.endsWith('?box=aaaa0001')) && await page.locator('.mail-card').count() === 1
     && await page.locator('.mail-card').getByText('greg@gmail.example').count() === 1, `${label}: only that mailbox goes`);
  ok(errs.length === 0, `${label}: no page errors ${errs.join('|')}`);
  await ctx.close();
}
await browser.close();
