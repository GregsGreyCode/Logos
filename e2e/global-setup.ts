/**
 * Global setup — runs once before all tests.
 * Handles setup wizard if needed, logs in, saves storageState.
 */
import { chromium, type FullConfig } from "@playwright/test";
import * as dotenv from "dotenv";
import * as path from "path";

dotenv.config({ path: path.resolve(__dirname, ".env") });

const BASE_URL = process.env.BASE_URL || "http://localhost:8080";
const USER = process.env.ADMIN_USERNAME || "Greg";
const PASS = process.env.ADMIN_PASSWORD || "";
const EMAIL = process.env.ADMIN_EMAIL || "e2e@test.local";
const LMSTUDIO = process.env.LMSTUDIO_URL || "http://localhost:1234";

export default async function globalSetup(config: FullConfig) {
  if (!PASS) throw new Error("ADMIN_PASSWORD required in .env");

  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL: BASE_URL });
  const page = await context.newPage();

  // Check if setup is needed
  const status = await page.request.get("/api/setup/status");
  const { completed } = await status.json();

  if (!completed) {
    console.log("  Setup wizard detected — completing via API...");
    const resp = await page.request.post("/api/setup/complete", {
      data: {
        endpoint: `${LMSTUDIO}/v1`,
        model: "auto",
        exec_env: "local",
        agent_type: "hermes",
        setup_email: EMAIL,
        setup_username: USER,
        setup_password: PASS,
        setup_display_name: USER,
        additional_users: [],
      },
    });
    if (!resp.ok()) {
      throw new Error(`Setup failed (${resp.status()}): ${await resp.text()}`);
    }
    console.log("  Setup complete.");
  }

  // Login
  await page.goto("/login");
  try {
    // Wait for either login form or app (auto-login after setup)
    const result = await Promise.race([
      page.waitForSelector("#identifier:visible", { timeout: 8_000 }).then(() => "login"),
      page.waitForSelector('[data-testid="nav-agents"]', { timeout: 8_000 }).then(() => "app"),
    ]);

    if (result === "login") {
      await page.locator("#identifier").fill(USER);
      await page.locator("#password").fill(PASS);
      await page.locator("#password").press("Enter");
      await page.waitForSelector('[data-testid="nav-agents"]', { timeout: 15_000 });
    }
  } catch {
    // Might already be on the app
    await page.goto("/");
    await page.waitForSelector('[data-testid="nav-agents"]', { timeout: 15_000 });
  }

  console.log("  Authenticated — saving session.");
  await context.storageState({ path: ".auth/admin.json" });
  await browser.close();
}
