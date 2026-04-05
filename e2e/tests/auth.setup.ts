/**
 * Auth setup — runs before all tests to create an authenticated session.
 * Saves storageState to .auth/admin.json so tests skip the login page.
 */
import { test as setup, expect } from "@playwright/test";
import { loginViaUI } from "../lib/helpers";

const ADMIN_USER = process.env.ADMIN_USERNAME || "Greg";
const ADMIN_PASS = process.env.ADMIN_PASSWORD || "";

setup("authenticate as admin", async ({ page }) => {
  if (!ADMIN_PASS) {
    throw new Error("ADMIN_PASSWORD env var is required. Copy .env.example to .env and fill in.");
  }

  await loginViaUI(page, ADMIN_USER, ADMIN_PASS);

  // Verify we're authenticated
  const me = await page.request.get("/auth/me");
  expect(me.ok()).toBeTruthy();
  const user = await me.json();
  expect(user.user).toBeTruthy();
  expect(user.user.role).toBe("admin");

  // Save session for other tests
  await page.context().storageState({ path: ".auth/admin.json" });
});
