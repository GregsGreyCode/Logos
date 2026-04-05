/**
 * Smoke tests — fast, high-level checks that the app is alive and navigable.
 * Run on every deploy. Should complete in under 60 seconds.
 *
 * Tag: @smoke
 */
import { test, expect } from "@playwright/test";
import { goToTab } from "../lib/helpers";
import { nav, chat, agents, settings, admin } from "../lib/selectors";

test.describe("Smoke @smoke", () => {
  test("main app loads with all nav tabs", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(nav.agentsTab).first()).toBeVisible();
    await expect(page.locator(nav.chatsTab).first()).toBeVisible();
    await expect(page.locator(nav.settingsTab).first()).toBeVisible();
    await expect(page.locator(nav.adminTab).first()).toBeVisible();
  });

  test("Agents tab renders world canvas and create button", async ({ page }) => {
    await page.goto("/");
    await goToTab(page, "agents");

    await expect(page.locator(agents.worldCanvas)).toBeVisible();
    await expect(page.locator(agents.createButton).first()).toBeVisible();
  });

  test("Chats tab renders header, input, and send button", async ({ page }) => {
    await page.goto("/");
    await goToTab(page, "chats");

    await expect(page.locator(chat.agentNameHeader).first()).toBeVisible();
    await expect(page.locator(chat.sendButton)).toBeVisible();
    await expect(page.locator(chat.newChatButton).first()).toBeVisible();
  });

  test("Settings tab renders sub-tabs", async ({ page }) => {
    await page.goto("/");
    await goToTab(page, "settings");

    await expect(page.locator(settings.inferenceTab)).toBeVisible();
    await expect(page.locator(settings.routingTab)).toBeVisible();
    await expect(page.locator(settings.toolsTab)).toBeVisible();
  });

  test("Admin tab renders sub-tabs", async ({ page }) => {
    await page.goto("/");
    await goToTab(page, "admin");

    await expect(page.locator(admin.usersTab)).toBeVisible();
    await expect(page.locator(admin.securityTab)).toBeVisible();
    await expect(page.locator(admin.workflowsTab)).toBeVisible();
    await expect(page.locator(admin.runsTab)).toBeVisible();
    await expect(page.locator(admin.auditTab)).toBeVisible();
    await expect(page.locator(admin.approvalsTab)).toBeVisible();
  });

  test("no console errors on initial load", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") {
        const text = msg.text();
        // Ignore browser extension noise and CDN warnings
        if (text.includes("autofill") || text.includes("favicon") || text.includes("tailwindcss.com")) return;
        errors.push(text);
      }
    });

    await page.goto("/");
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(2000);

    expect(errors).toEqual([]);
  });
});
