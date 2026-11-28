import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const ctx = await browser.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
const page = await ctx.newPage(); const errs = []; page.on('pageerror', (e) => errs.push(e.message));
const S = { agent: 'greg', outlook_ready: false, is_admin: true, mailboxes: [], signin: null };
const sent = [];
await page.route('**/api/v2/mail**', async (route) => {
  const r = route.request(), u = new URL(r.url()), body = r.postData() ? JSON.parse(r.postData()) : null;
  sent.push([r.method(), u.pathname, body]);
  if (r.method() === 'GET') return route.fulfill({ json: S });
  if (u.pathname.endsWith('/gmail')) {
    if (body.app_password.replace(/ /g, '') !== 'abcdefghijklmnop') return route.fulfill({ status: 400, json: { error: 'refused', message: 'Gmail did not accept it. Check the address.' } });
    S.mailboxes = [{ id: 'aaaa0001', kind: 'gmail', address: body.address, exclude: [], health: { synced: '2026-11-23T10:00:00', error: null } }];
    return route.fulfill({ json: { connected: true, inbox: 42, id: 'aaaa0001' } });
  }
  if (u.pathname.endsWith('/outlook-app')) { S.outlook_ready = true; return route.fulfill({ json: { outlook_ready: true } }); }
  if (u.pathname.endsWith('/outlook')) { S.signin = { state: 'waiting', code: 'ABCD1234', url: 'https://microsoft.com/devicelogin' }; return route.fulfill({ json: S.signin }); }
  if (u.pathname.endsWith('/exclude')) { const b = S.mailboxes.find((m) => m.id === body.box); b.exclude = [body.sender]; return route.fulfill({ json: { exclude: b.exclude } }); }
  if (r.method() === 'DELETE') { S.mailboxes = S.mailboxes.filter((m) => m.id !== u.searchParams.get('box')); return route.fulfill({ json: { disconnected: true } }); }
  return route.fulfill({ status: 404, json: {} });
});
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/settings/email`); await page.waitForTimeout(1500);
ok(await page.locator('.topbar .topbar-back').count() + await page.locator('.set-row', { hasText: 'Email' }).count() >= 1, 'Settings › Email is its own page');
await page.getByRole('button', { name: 'Gmail' }).tap(); await page.waitForTimeout(300);
ok(await page.locator('a[href="https://myaccount.google.com/apppasswords"]').count() === 1, 'Gmail: links straight to App passwords');
await page.screenshot({ path: process.argv[2] + '/mail-gmail.png' });
await page.fill('#mail-addr', 'greg@gmail.example'); await page.fill('#mail-pw', 'wrong wrong wron');
await page.getByRole('button', { name: 'Connect' }).tap(); await page.waitForTimeout(400);
ok((await page.locator('[role=status]').allInnerTexts()).join(' ').includes('did not accept'), 'a wrong password says why');
await page.fill('#mail-pw', 'abcd efgh ijkl mnop'); await page.getByRole('button', { name: 'Connect' }).tap(); await page.waitForTimeout(600);
ok(await page.locator('.mail-card', { hasText: 'greg@gmail.example' }).count() === 1, 'connected: the address and how it is doing');
await page.screenshot({ path: process.argv[2] + '/mail-connected0.png' });
await page.locator('.mail-private summary').tap(); await page.waitForTimeout(200);
await page.fill('input[aria-label^="A sender to keep private"]', 'mybank.example'); await page.getByRole('button', { name: 'Add', exact: true }).tap(); await page.waitForTimeout(400);
ok(await page.locator('.mail-chip', { hasText: 'mybank.example' }).count() === 1 && await page.locator('.mail-private[open]').count() === 1, 'a sender can be kept private, and the fold stays open');
await page.screenshot({ path: process.argv[2] + '/mail-connected.png' });
await page.getByRole('button', { name: 'Disconnect' }).tap(); await page.waitForTimeout(300);
await page.locator('.ask-layer').getByRole('button', { name: 'Disconnect' }).tap(); await page.waitForTimeout(500);
ok(sent.some(([m, path]) => m === 'DELETE') && await page.getByRole('button', { name: 'Gmail' }).count() === 1, 'Disconnect, after asking');
await page.getByRole('button', { name: 'Outlook.com / Hotmail' }).tap(); await page.waitForTimeout(300);
ok(await page.getByText('One-time setup for this Logos').count() === 1, 'Outlook not set up: the manager gets the walkthrough');
await page.screenshot({ path: process.argv[2] + '/mail-app.png', fullPage: true });
await page.fill('#mail-app', '1a2b3c4d-1111-2222-3333-444455556666'); await page.getByRole('button', { name: 'Save' }).tap(); await page.waitForTimeout(400);
await page.getByRole('button', { name: 'Get a sign-in code' }).tap(); await page.waitForTimeout(800);
ok(await page.locator('.mail-code', { hasText: 'ABCD1234' }).count() === 1, 'the code to type at Microsoft');
await page.screenshot({ path: process.argv[2] + '/mail-code.png' });
S.signin = { state: 'connected' }; S.mailboxes = [{ id: 'bbbb0002', kind: 'outlook', address: 'greg@hotmail.example', exclude: [] }];
await page.waitForTimeout(3800);
ok(await page.locator('.mail-card', { hasText: 'greg@hotmail.example' }).count() === 1, 'it carries on by itself once signed in');
// someone who does not manage Logos
S.mailboxes = []; S.signin = null; S.outlook_ready = false; S.is_admin = false;
await page.reload(); await page.waitForTimeout(1500);
await page.getByRole('button', { name: 'Outlook.com / Hotmail' }).tap(); await page.waitForTimeout(300);
ok(await page.getByText('Outlook is not set up on this Logos yet').count() === 1, 'others are told to ask whoever manages Logos');
ok(errs.length === 0, `no page errors ${errs.join('|')}`);
await browser.close();
