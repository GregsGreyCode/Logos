// Desktop (Greg, 23 Nov): going back and forth between menus. The menu remembers where you were in each section; pressing
// the section you are in goes to its start; an agent's Brain and Routines carry the same ‹ back and Chat | Brain | Routines pill.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const browser = await chromium.launch({ executablePath: process.env.CHROME });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const hash = () => page.evaluate(() => location.hash);
const nav = async (label) => { await page.locator('.nav-links a', { hasText: label }).first().click(); await page.waitForTimeout(900); };
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/chat/greg`); await page.waitForTimeout(2200);
await nav('Brain');
ok((await hash()) === '#/brain', `Brain from a chat opens the Brain start (${await hash()})`);
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/brain/greg`); await page.waitForTimeout(1500);
await nav('Agents');
ok((await hash()) === '#/chat/greg', `Agents from the brain goes back into the chat you left (${await hash()})`);
await nav('Agents');
ok((await hash()) === '#/chat', `pressing Agents again, in that section, goes to its start (${await hash()})`);
await nav('Brain');
ok((await hash()) === '#/brain/greg', `and Brain remembers the brain page you were on (${await hash()})`);
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/settings/look`); await page.waitForTimeout(1500);
await nav('Home'); await nav('Settings');
ok((await hash()) === '#/settings/look', `Settings remembers its section (${await hash()})`);
// the agent's three views, from the brain and from routines
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/brain/greg`); await page.waitForTimeout(1800);
ok(await page.locator('.brain > .agent-bar .agent-menu > summary').isVisible(), 'Brain: the Chat | Brain | Routines pill on a desktop, in the title band');
await page.locator('.brain > .agent-bar .agent-menu > summary').click();
ok((await page.locator('.brain > .agent-bar .agent-menu-list a').allInnerTexts()).map((t) => t.replace(/[↗\s]+$/, '')).join('|') === 'Chat|Brain|Routines|Settings', 'Brain: the pill offers Chat, Brain, Routines, Settings');
await page.locator('.brain > .agent-bar .agent-menu-list a', { hasText: 'Chat' }).click(); await page.waitForTimeout(900);
ok((await hash()) === '#/chat/greg', `Brain › Chat opens the agent's chat (${await hash()})`);
await page.goto(`http://127.0.0.1:${process.env.PORT || 8779}/#/agents/greg`); await page.waitForTimeout(1800);
ok(await page.locator('.agents-page > .agent-bar .agent-menu > summary').isVisible(), 'Routines: the pill on a desktop, in the title band');
// The band is the one agent bar (UI review, 28 Nov): the agent's name and the pill share it.
const pill = await page.locator('.agents-page > .agent-bar .agent-menu > summary').boundingBox(), t = await page.locator('.agents-page > .agent-bar .agent-bar-switch').boundingBox();
ok(pill.y < 60 && t.y < 60, `Routines: the pill and the agent's name share the band (pill y=${Math.round(pill.y)}, name y=${Math.round(t.y)})`);
await browser.close();
