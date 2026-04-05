/**
 * Auth setup — runs before all tests to create an authenticated session.
 * Handles both fresh installs (setup wizard) and existing installs (login).
 * Saves storageState to .auth/admin.json so tests skip the login page.
 */
import { test as setup, expect } from "@playwright/test";
import { ensureLoggedIn } from "../lib/helpers";

const ADMIN_USER = process.env.ADMIN_USERNAME || "Greg";
const ADMIN_PASS = process.env.ADMIN_PASSWORD || "";
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || "e2e@test.local";

setup("authenticate as admin", async ({ page }) => {
  if (!ADMIN_PASS) {
    throw new Error(
      "ADMIN_PASSWORD env var is required. Copy .env.example to .env and fill in.",
    );
  }

  await ensureLoggedIn(page, ADMIN_USER, ADMIN_PASS, ADMIN_EMAIL);

  // Verify we're authenticated
  const me = await page.request.get("/auth/me");
  expect(me.ok()).toBeTruthy();
  const user = await me.json();
  expect(user.user).toBeTruthy();

  // Save session for other tests
  await page.context().storageState({ path: ".auth/admin.json" });
});
