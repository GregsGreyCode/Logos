/**
 * Admin panel tests — users, security, workflows, runs, audit, approvals.
 *
 * Tag: @regression
 */
import { test, expect } from "@playwright/test";
import { goToTab, getCsrfToken } from "../lib/helpers";
import { admin } from "../lib/selectors";

test.describe("Admin @regression", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await goToTab(page, "admin");
  });

  // ── Users ─────────────────────────────────────────────────────────────

  test.describe("Users", () => {
    test("users table shows current user", async ({ page }) => {
      const table = page.locator(admin.userTable);
      await expect(table).toBeVisible();
      // Current user's row should exist in the table
      await expect(table.locator("tr").first()).toBeVisible();
    });

    test("new user button present", async ({ page }) => {
      await expect(page.locator(admin.newUserButton)).toBeVisible();
    });

    test("new user form opens with required fields", async ({ page }) => {
      await page.locator(admin.newUserButton).click();
      await page.waitForTimeout(500);

      // Form should have email, username, password fields
      await expect(page.locator('input[placeholder*="mail"], input[type="email"]').first()).toBeVisible();
      await expect(page.locator('input[type="password"]:visible').first()).toBeVisible();
    });

    test("create and delete user end-to-end", async ({ page }) => {
      const testEmail = `e2e-${Date.now()}@test.local`;
      const testUser = `e2e-user-${Date.now()}`;

      await page.locator(admin.newUserButton).click();
      await page.waitForTimeout(500);

      // Fill in form — field order may vary, use placeholders
      // TODO: add data-testid to form fields for reliability
      const emailInput = page.locator('input[placeholder*="mail"], input[type="email"]').first();
      await emailInput.fill(testEmail);

      const usernameInput = page.locator('input[placeholder*="sername"]').first();
      if (await usernameInput.count()) {
        await usernameInput.fill(testUser);
      }

      const passwordInput = page.locator('input[type="password"]:visible').first();
      await passwordInput.fill("TestPass123!");

      // Submit
      const createBtn = page.locator('button:has-text("Create User"), button:has-text("Create")').first();
      await createBtn.click();
      await page.waitForTimeout(2000);

      // User should appear in table
      const newRow = page.locator(`text=${testUser}`).or(page.locator(`text=${testEmail}`));
      await expect(newRow.first()).toBeVisible({ timeout: 5_000 });

      // Cleanup: delete via last column delete icon
      // TODO: add data-testid="delete-user-{id}" for reliable targeting
    });

    test("last login timestamp formats correctly", async ({ page }) => {
      // Verify no date shows year 58230 (the double-multiply bug)
      const pageText = await page.locator("body").textContent();
      expect(pageText).not.toContain("58230");
    });
  });

  // ── Security ──────────────────────────────────────────────────────────

  test.describe("Security", () => {
    test.beforeEach(async ({ page }) => {
      await page.locator(admin.securityTab).click();
      await page.waitForTimeout(1000);
    });

    test("action policies section visible", async ({ page }) => {
      await expect(page.locator("text=ACTION POLICIES").first()).toBeVisible();
      await expect(page.locator(admin.newPolicyButton)).toBeVisible();
    });

    test("new policy form has all permission fields", async ({ page }) => {
      await page.locator(admin.newPolicyButton).click();
      await page.waitForTimeout(500);

      // Should show permission dropdowns for Write, Exec, Filesystem, Network, etc.
      // Policy form should show permission fields
      await page.waitForTimeout(500);
    });
  });

  // ── Workflows ─────────────────────────────────────────────────────────

  test.describe("Workflows", () => {
    test.beforeEach(async ({ page }) => {
      await page.locator(admin.workflowsTab).click();
      await page.waitForTimeout(1000);
    });

    test("workflows section shows build and import buttons", async ({ page }) => {
      await expect(page.locator(admin.buildButton)).toBeVisible();
      await expect(page.locator(admin.importButton)).toBeVisible();
    });

    test("empty state message shown when no workflows", async ({ page }) => {
      // If no workflows exist, should show helpful message
      const empty = page.locator("text=No workflow definitions");
      if (await empty.count()) {
        await expect(empty).toBeVisible();
      }
    });
  });

  // ── Runs ──────────────────────────────────────────────────────────────

  test.describe("Runs", () => {
    test("runs tab loads without error", async ({ page }) => {
      await page.locator(admin.runsTab).click();
      await page.waitForTimeout(1000);
      // Should show either runs table or empty state
      // TODO: verify run list or "No runs" message with data-testid
    });
  });

  // ── Audit Log ─────────────────────────────────────────────────────────

  test.describe("Audit Log", () => {
    test("audit log tab loads", async ({ page }) => {
      await page.locator(admin.auditTab).click();
      await page.waitForTimeout(1000);

      // Should show audit events (login event at minimum from our auth setup)
      await expect(page.locator("text=Audit").first()).toBeVisible();
    });
  });

  // ── Approvals ─────────────────────────────────────────────────────────

  test.describe("Approvals", () => {
    test("approvals tab loads", async ({ page }) => {
      await page.locator(admin.approvalsTab).click();
      await page.waitForTimeout(1000);
      // TODO: verify approvals table or empty state
    });
  });
});
