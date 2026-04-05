/**
 * Settings tab tests — inference providers, routing, tools.
 *
 * Tag: @regression
 */
import { test, expect } from "@playwright/test";
import { goToTab } from "../lib/helpers";
import { settings } from "../lib/selectors";

test.describe("Settings @regression", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await goToTab(page, "settings");
  });

  // ── Inference sub-tab ───────────────────────────────────────────────────

  test.describe("Inference", () => {
    test("shows cloud providers section with add button", async ({ page }) => {
      await expect(page.locator("text=Cloud Providers").first()).toBeVisible();
      await expect(page.locator(settings.addProviderButton).first()).toBeVisible();
    });

    test("shows local servers section", async ({ page }) => {
      await expect(page.locator("text=Local Servers")).toBeVisible();
      await expect(page.locator(settings.scanButton)).toBeVisible();
    });

    test("add provider form opens on click", async ({ page }) => {
      await page.locator(settings.addProviderButton).first().click();
      await page.waitForTimeout(500);

      // Form should appear with provider type options
      // TODO: verify provider type cards (Anthropic, OpenRouter, Custom) appear
      // These are conditional on x-show, need data-testid for reliable targeting
      await expect(page.locator('text=Cancel')).toBeVisible();
    });

    test("local machine card shows status and actions", async ({ page }) => {
      // Check if any machine is registered
      const machineCard = page.locator("text=enabled").first();
      if (await machineCard.count() === 0) {
        test.skip(true, "No local machines registered");
        return;
      }

      await expect(page.locator(settings.pingButton).first()).toBeVisible();
    });

    test("ping machine returns health status", async ({ page }) => {
      const pingBtn = page.locator(settings.pingButton).first();
      if (await pingBtn.count() === 0) {
        test.skip(true, "No machines to ping");
        return;
      }

      await pingBtn.click();
      await page.waitForTimeout(3000);

      // Status should update — look for "up" or "down" badge
      // TODO: add data-testid="machine-status" for reliable assertion
    });
  });

  // ── Routing sub-tab ─────────────────────────────────────────────────────

  test.describe("Routing", () => {
    test.beforeEach(async ({ page }) => {
      await page.locator(settings.routingTab).click();
      await page.waitForTimeout(1000);
    });

    test("shows routing profiles section", async ({ page }) => {
      await expect(page.locator("text=Routing Profiles")).toBeVisible();
      await expect(page.locator(settings.newProfileButton)).toBeVisible();
    });

    test("default profile exists", async ({ page }) => {
      await expect(page.locator("text=default")).toBeVisible();
      await expect(page.locator("text=fallback")).toBeVisible();
    });

    test("collapsible sections render", async ({ page }) => {
      await expect(page.locator("text=Model Map")).toBeVisible();
      await expect(page.locator("text=Benchmark")).toBeVisible();
    });
  });

  // ── Tools sub-tab ───────────────────────────────────────────────────────

  test.describe("Tools", () => {
    test.beforeEach(async ({ page }) => {
      await page.locator(settings.toolsTab).click();
      await page.waitForTimeout(1000);
    });

    test("shows MCP tool servers section", async ({ page }) => {
      await expect(page.locator(settings.mcpSection)).toBeVisible();
    });

    test("shows services and integrations section", async ({ page }) => {
      await expect(page.locator(settings.servicesSection)).toBeVisible();
    });
  });

  // ── Proposals sub-tab ───────────────────────────────────────────────────

  test.describe("Proposals", () => {
    test("proposals tab loads without error", async ({ page }) => {
      await page.locator(settings.proposalsTab).click();
      await page.waitForTimeout(1000);

      // Should show the proposals panel or empty state
      // TODO: verify proposals list or "No proposals" message
    });
  });
});
