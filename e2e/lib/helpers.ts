/**
 * Reusable helpers for Logos E2E tests.
 * Keep these thin — they wrap common multi-step actions.
 */
import { type Page, expect } from "@playwright/test";
import { login as loginSel, nav, chat, agents } from "./selectors";

// ── Auth ────────────────────────────────────────────────────────────────────

/** Log in via the UI and wait for the main app to load. */
export async function loginViaUI(
  page: Page,
  username: string,
  password: string,
) {
  await page.goto("/login");
  await page.waitForSelector(`${loginSel.usernameInput}:visible`, {
    timeout: 10_000,
  });
  await page.locator(loginSel.usernameInput).fill(username);
  await page.locator(loginSel.passwordInput).fill(password);
  await page.locator(loginSel.passwordInput).press("Enter");
  // Wait for main nav to appear (proves login succeeded)
  await page.waitForSelector(nav.agentsTab, { timeout: 15_000 });
}

/** Log in via API (faster, for storageState setup). */
export async function loginViaAPI(page: Page, identifier: string, password: string) {
  const resp = await page.request.post("/auth/login", {
    data: { identifier, password },
  });
  expect(resp.ok()).toBeTruthy();
  // Navigate to app to pick up cookies
  await page.goto("/");
  await page.waitForSelector(nav.agentsTab, { timeout: 15_000 });
}

// ── Navigation ──────────────────────────────────────────────────────────────

export async function goToTab(page: Page, tab: "agents" | "chats" | "settings" | "admin") {
  const selMap = {
    agents: nav.agentsTab,
    chats: nav.chatsTab,
    settings: nav.settingsTab,
    admin: nav.adminTab,
  };
  await page.locator(selMap[tab]).first().click();
  // Allow data to load
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(1000);
}

// ── Chat ────────────────────────────────────────────────────────────────────

/** Send a message in the active chat and wait for the response to start. */
export async function sendMessage(page: Page, text: string) {
  const textarea = page.locator(chat.textarea);
  await textarea.fill(text);
  await page.locator(chat.sendButton).click();
}

/** Wait for the agent to finish responding (loading indicator disappears). */
export async function waitForResponse(page: Page, timeoutMs = 60_000) {
  // The send button re-enables when response is complete
  await page.locator(chat.sendButton).waitFor({ state: "visible", timeout: timeoutMs });
  // Small buffer for DOM to settle
  await page.waitForTimeout(500);
}

/** Start a new chat session. */
export async function newChat(page: Page) {
  await page.locator(chat.newChatButton).first().click();
  await page.waitForTimeout(500);
}

// ── Agents ──────────────────────────────────────────────────────────────────

/** Create a named agent and return to the agents list. */
export async function createAgent(
  page: Page,
  opts: { name: string; soul?: string; description?: string },
) {
  await goToTab(page, "agents");
  await page.locator(agents.createButton).first().click();
  await page.locator(agents.nameInput).fill(opts.name);
  if (opts.description) {
    await page.locator(agents.descriptionInput).fill(opts.description);
  }
  await page.locator(agents.submitCreate).click();
  await page.waitForTimeout(1500);
}

/** Delete a named agent by name. Handles the confirmation dialog. */
export async function deleteAgent(page: Page, name: string) {
  await goToTab(page, "agents");
  const card = page.locator(agents.agentCard, { hasText: name }).first();
  await card.hover();
  await page.waitForTimeout(300);

  page.on("dialog", (d) => d.accept());
  await card.locator(agents.deleteButton).click();
  await page.waitForTimeout(1000);
}

// ── API helpers ─────────────────────────────────────────────────────────────

/** Get CSRF token from cookies. */
export async function getCsrfToken(page: Page): Promise<string> {
  const cookies = await page.context().cookies();
  return cookies.find((c) => c.name === "csrf_token")?.value ?? "";
}

/** Create agent via API (faster for setup/teardown). */
export async function createAgentAPI(page: Page, name: string) {
  const csrf = await getCsrfToken(page);
  const resp = await page.request.post("/admin/agents", {
    data: { name, soul_slug: "general", description: "E2E test agent" },
    headers: { "X-CSRF-Token": csrf },
  });
  return resp.json();
}

/** Delete agent via API (faster for teardown). */
export async function deleteAgentAPI(page: Page, agentId: string) {
  const csrf = await getCsrfToken(page);
  await page.request.delete(`/admin/agents/${agentId}`, {
    headers: { "X-CSRF-Token": csrf },
  });
}
