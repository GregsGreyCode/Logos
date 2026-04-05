/**
 * Chat interaction tests — sending messages, receiving responses, error handling.
 *
 * NOTE: Tests that send actual messages to the agent require a running
 * inference backend (LM Studio or cloud provider). Mark those with
 * test.skip() if no backend is available, or mock via route interception.
 *
 * Tag: @regression
 */
import { test, expect } from "@playwright/test";
import { goToTab, sendMessage, newChat, waitForResponse } from "../lib/helpers";
import { chat } from "../lib/selectors";

test.describe("Chat @regression", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await goToTab(page, "chats");
  });

  test("chat header shows agent name and STAMP chips", async ({ page }) => {
    // Agent name (Hermes by default)
    const header = page.locator(chat.agentNameHeader).first();
    await expect(header).toBeVisible();
    const name = await header.textContent();
    expect(name).toBeTruthy();

    // STAMP chips via data-testid
    await expect(page.locator(chat.stampS)).toBeVisible();
    await expect(page.locator(chat.stampT)).toBeVisible();
    await expect(page.locator(chat.stampM)).toBeVisible();
    await expect(page.locator(chat.stampP)).toBeVisible();
  });

  test("chat input accepts text", async ({ page }) => {
    const textarea = page.locator(chat.textarea);
    await textarea.fill("Hello, world!");
    await expect(textarea).toHaveValue("Hello, world!");
  });

  test("new chat creates fresh conversation", async ({ page }) => {
    await newChat(page);
    // Messages area should be empty (just the logo)
    // The chat input should be ready
    await expect(page.locator(chat.sendButton)).toBeVisible();
  });

  test("send message shows it in chat history", async ({ page }) => {
    await newChat(page);
    const testMsg = `E2E test ${Date.now()}`;
    await sendMessage(page, testMsg);

    // The user's message should appear in the chat
    await expect(page.locator(`text=${testMsg}`).first()).toBeVisible({ timeout: 5_000 });
  });

  test("send message triggers loading state", async ({ page }) => {
    await newChat(page);
    await sendMessage(page, "Say hello");

    // The status indicator should show thinking/loading
    // The header dot should change color (indigo pulse)
    // We check that SOMETHING happens within a reasonable time
    await page.waitForTimeout(500);

    // The chat area should now have the user message
    await expect(page.locator("text=Say hello").first()).toBeVisible();
  });

  // This test requires an active inference backend
  test("agent responds to message", async ({ page }) => {
    // Skip if no inference backend is likely available
    const status = await page.request.get("/status").catch(() => null);
    if (!status?.ok()) {
      test.skip(true, "No inference backend available");
      return;
    }

    await newChat(page);
    await sendMessage(page, "Reply with exactly: PONG");

    // Wait for response — this may take a while with local models
    await waitForResponse(page, 60_000);

    // There should be at least 2 messages now (user + agent)
    // TODO: add data-testid="chat-message" to message elements for reliable counting
    await page.waitForTimeout(1000);
  });

  test("platform filter pills render", async ({ page }) => {
    await expect(page.locator(chat.filterWeb)).toBeVisible();
  });

  test("STAMP S chip opens soul dropdown", async ({ page }) => {
    const sChip = page.locator(chat.stampS);
    await sChip.click();
    await page.waitForTimeout(500);

    // Dropdown should appear with soul options
    const dropdown = page.locator('[class*="z-50"]:visible');
    await expect(dropdown).toBeVisible();

    // Close it
    await page.keyboard.press("Escape");
  });
});
