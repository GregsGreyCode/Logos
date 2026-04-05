import { defineConfig, devices } from "@playwright/test";
import * as dotenv from "dotenv";
import * as path from "path";

dotenv.config({ path: path.resolve(__dirname, ".env") });

const BASE_URL = process.env.BASE_URL || "http://localhost:8080";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false, // Sequential — Logos is single-instance, shared state
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }]]
    : [["html", { open: "on-failure" }]],
  timeout: 30_000,

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },

  projects: [
    // Auth setup — runs first, saves session state
    {
      name: "auth-setup",
      testMatch: /auth\.setup\.ts/,
    },
    // Main tests — reuse authenticated session
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        storageState: ".auth/admin.json",
      },
      dependencies: ["auth-setup"],
    },
  ],
});
