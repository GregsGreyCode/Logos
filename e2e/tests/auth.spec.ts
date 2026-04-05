/**
 * Authentication tests — login, logout, session handling.
 * These run WITHOUT the pre-authenticated storageState.
 *
 * Tag: @regression
 */
import { test, expect } from "@playwright/test";
import { login as sel } from "../lib/selectors";

// Override: don't use saved auth for these tests
test.use({ storageState: { cookies: [], origins: [] } });

const USER = process.env.ADMIN_USERNAME || "Greg";
const PASS = process.env.ADMIN_PASSWORD || "";

test.describe("Authentication @regression", () => {
  test("login page renders form elements", async ({ page }) => {
    await page.goto("/login");
    await page.waitForSelector(`${sel.usernameInput}:visible`, { timeout: 10_000 });

    await expect(page.locator(sel.usernameInput)).toBeVisible();
    await expect(page.locator(sel.passwordInput)).toBeVisible();
    await expect(page.locator(sel.submitButton)).toBeVisible();
  });

  test("successful login redirects to main app", async ({ page }) => {
    await page.goto("/login");
    await page.waitForSelector(`${sel.usernameInput}:visible`, { timeout: 10_000 });

    await page.locator(sel.usernameInput).fill(USER);
    await page.locator(sel.passwordInput).fill(PASS);
    await page.locator(sel.passwordInput).press("Enter");

    // Should land on main app with nav visible
    await page.waitForSelector('button:has-text("Agents")', { timeout: 15_000 });
    expect(page.url()).not.toContain("/login");
  });

  test("failed login shows error message", async ({ page }) => {
    await page.goto("/login");
    await page.waitForSelector(`${sel.usernameInput}:visible`, { timeout: 10_000 });

    await page.locator(sel.usernameInput).fill("nonexistent");
    await page.locator(sel.passwordInput).fill("wrongpassword");
    await page.locator(sel.passwordInput).press("Enter");

    // Should still be on login page after a brief wait
    await page.waitForTimeout(2000);
    expect(page.url()).toContain("/login");
    // Error message should appear (may be styled differently)
    const hasError = await page.locator(sel.errorMessage).count() > 0
      || await page.locator("text=invalid").count() > 0
      || await page.locator("[class*='red']").count() > 0;
    expect(hasError).toBeTruthy();
  });

  test("empty credentials show error", async ({ page }) => {
    await page.goto("/login");
    await page.waitForSelector(`${sel.usernameInput}:visible`, { timeout: 10_000 });

    await page.locator(sel.passwordInput).press("Enter");

    // Should show validation error or stay on login
    await page.waitForTimeout(1000);
    expect(page.url()).toContain("/login");
  });

  test("unauthenticated access redirects to login", async ({ page }) => {
    await page.goto("/");
    await page.waitForURL("**/login", { timeout: 5_000 });
    expect(page.url()).toContain("/login");
  });

  test("API returns 401 without session", async ({ page }) => {
    const resp = await page.request.get("/auth/me");
    expect(resp.status()).toBe(401);
  });

  test("session persists across page reload", async ({ page }) => {
    // Login
    await page.goto("/login");
    await page.waitForSelector(`${sel.usernameInput}:visible`, { timeout: 10_000 });
    await page.locator(sel.usernameInput).fill(USER);
    await page.locator(sel.passwordInput).fill(PASS);
    await page.locator(sel.passwordInput).press("Enter");
    await page.waitForSelector('button:has-text("Agents")', { timeout: 15_000 });

    // Reload
    await page.reload();
    await page.waitForSelector('button:has-text("Agents")', { timeout: 15_000 });

    // Should still be authenticated
    const me = await page.request.get("/auth/me");
    expect(me.ok()).toBeTruthy();
  });
});
