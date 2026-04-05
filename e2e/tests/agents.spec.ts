/**
 * Agent management tests — CRUD operations on named agents.
 *
 * Tag: @regression
 */
import { test, expect } from "@playwright/test";
import { goToTab, createAgent, deleteAgent, deleteAgentAPI, getCsrfToken } from "../lib/helpers";
import { agents } from "../lib/selectors";

test.describe("Agents @regression", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await goToTab(page, "agents");
  });

  test("agents page shows world canvas", async ({ page }) => {
    const world = page.locator(agents.worldCanvas);
    await expect(world).toBeVisible();
    const box = await world.boundingBox();
    expect(box).toBeTruthy();
    expect(box!.width).toBeGreaterThan(300);
    expect(box!.height).toBeGreaterThan(200);
  });

  test("create agent form opens and has all fields", async ({ page }) => {
    await page.locator(agents.createButton).first().click();

    await expect(page.locator(agents.nameInput)).toBeVisible();
    await expect(page.locator(agents.descriptionInput)).toBeVisible();
    // Soul and model selects
    const selects = page.locator("select:visible");
    expect(await selects.count()).toBeGreaterThanOrEqual(2);
    // Toolsets expandable
    await expect(page.locator(agents.toolsetsSummary)).toBeVisible();
  });

  test("create agent form validates required name", async ({ page }) => {
    await page.locator(agents.createButton).first().click();

    // Submit button should be disabled without a name
    const submit = page.locator(agents.submitCreate);
    await expect(submit).toBeDisabled();

    // Fill name — button enables
    await page.locator(agents.nameInput).fill("Test");
    await expect(submit).toBeEnabled();
  });

  test("toolsets section expands with checkboxes", async ({ page }) => {
    await page.locator(agents.createButton).first().click();
    await page.locator(agents.toolsetsSummary).click();
    await page.waitForTimeout(500);

    const checkboxes = page.locator(agents.toolsetCheckbox);
    expect(await checkboxes.count()).toBeGreaterThan(0);
  });

  test("create and delete agent end-to-end", async ({ page }) => {
    const agentName = `E2E-Agent-${Date.now()}`;

    // Create
    await page.locator(agents.createButton).first().click();
    await page.locator(agents.nameInput).fill(agentName);
    await page.locator(agents.descriptionInput).fill("Automated test agent");
    await page.locator(agents.submitCreate).click();
    await page.waitForTimeout(2000);

    // Verify it appears
    await expect(page.locator(`text=${agentName}`)).toBeVisible();

    // Delete
    const card = page.locator(agents.agentCard, { hasText: agentName }).first();
    await card.hover();
    await page.waitForTimeout(300);

    page.on("dialog", (d) => d.accept());
    await card.locator(agents.deleteButton).click();
    await page.waitForTimeout(1000);

    // Verify it's gone
    await expect(page.locator(`text=${agentName}`)).not.toBeVisible();
  });

  test("duplicate agent name shows error", async ({ page }) => {
    const agentName = `Dup-Test-${Date.now()}`;

    // Create first agent via API (fast)
    const csrf = await getCsrfToken(page);
    const resp = await page.request.post("/admin/agents", {
      data: { name: agentName, soul_slug: "general" },
      headers: { "X-CSRF-Token": csrf },
    });
    const agent = await resp.json();

    try {
      // Try creating duplicate via UI
      await page.locator(agents.createButton).first().click();
      await page.locator(agents.nameInput).fill(agentName);
      await page.locator(agents.submitCreate).click();
      await page.waitForTimeout(1500);

      // Should show error
      await expect(page.locator("text=already exists")).toBeVisible();
    } finally {
      // Cleanup
      await deleteAgentAPI(page, agent.id);
    }
  });

  test("clicking agent card navigates to chat", async ({ page }) => {
    const agentName = `Chat-Test-${Date.now()}`;
    const csrf = await getCsrfToken(page);
    const resp = await page.request.post("/admin/agents", {
      data: { name: agentName, soul_slug: "general" },
      headers: { "X-CSRF-Token": csrf },
    });
    const agent = await resp.json();

    try {
      // Reload agents
      await goToTab(page, "agents");

      // Click agent card
      const card = page.locator(agents.agentCard, { hasText: agentName }).first();
      await card.click();
      await page.waitForTimeout(2000);

      // Should be on chats tab now
      // TODO: verify chat header shows the agent name once data-testid is added
    } finally {
      await deleteAgentAPI(page, agent.id);
    }
  });
});
