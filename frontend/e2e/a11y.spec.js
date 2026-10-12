// Accessibility: automated WCAG 2.1 A/AA checks (axe-core) on the main
// screens, in English and in Persian (right-to-left). Serious and critical
// violations fail the test; the report names the rule and the elements.
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const fs = require("fs");
const path = require("path");
const { state, loginAs } = require("./helpers");

// Every serious/critical finding is also written to test-results/a11y.jsonl.
const DUMP = path.join(__dirname, "..", "test-results", "a11y.jsonl");



async function audit(page, label) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    // The diagnostic image canvas is pixels, not text.
    .exclude(".vp")
    .analyze();
  const bad = results.violations.filter((v) => ["serious", "critical"].includes(v.impact));
  fs.mkdirSync(path.dirname(DUMP), { recursive: true });
  for (const v of bad) {
    for (const n of v.nodes) {
      fs.appendFileSync(DUMP, JSON.stringify({ page: label, rule: v.id, target: n.target.join(" "),
                                              html: n.html.slice(0, 160), why: n.failureSummary }) + "\n");
    }
  }
  const report = bad.map((v) => `${v.id} (${v.impact}): ${v.help}\n  ` +
    v.nodes.slice(0, 5).map((n) => n.target.join(" ")).join("\n  ")).join("\n");
  expect.soft(bad.map((v) => v.id), `${label}\n${report}`).toEqual([]);
}

for (const lang of ["en", "fa"]) {
  test(`sign-in screens have no serious accessibility violations (${lang})`, async ({ page }) => {
    await page.addInitScript((l) => window.localStorage.setItem("asr.lang", l), lang);
    await page.goto("/login");
    await expect(page.locator("[data-role]").first()).toBeVisible();
    await audit(page, `login role picker (${lang})`);
    await page.locator('[data-role="doctor"]').click();
    await expect(page.locator("#login-username")).toBeVisible();
    await audit(page, `login credentials (${lang})`);
  });

  test(`workspace screens have no serious accessibility violations (${lang})`, async ({ page, request }) => {
    const st = state();
    await loginAs(page, request, "e2e_doctor", st.user_password, lang);
    const pages = [
      ["/dictate", ".recorder"],
      ["/ehr", '[data-testid="ehr-hub"]'],
      ["/ehr?tab=transfers", '[data-testid="ehr-hub"]'],
      [`/ehr/chart?id=${st.local_person}`, ".chart-tabs"],
      ["/pacs", "table, .empty"],
      ["/reports", ".card"],
      ["/settings", ".card"],
    ];
    for (const [url, ready] of pages) {
      await page.goto(url);
      await expect(page.locator(ready).first()).toBeVisible();
      await page.waitForTimeout(400);
      await audit(page, `${url} (${lang})`);
    }
  });
}

test("keyboard: skip link, drawer and dialog focus behave", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  // On a page with no autofocused field, the first Tab reaches the skip link.
  await page.goto("/reports");
  await expect(page.locator(".card").first()).toBeVisible();
  await page.keyboard.press("Tab");
  const skip = page.locator(".skip-link");
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  // The patient search box is focused and ready to type on the EHR page.
  await page.goto("/ehr");
  await expect(page.locator("input[name=q]")).toBeFocused();

  // The register dialog traps focus and gives it back when closed.
  const opener = page.getByRole("button", { name: /register|new patient/i }).first();
  await opener.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  for (let i = 0; i < 15; i += 1) await page.keyboard.press("Tab");
  expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();

  // On a phone-sized screen the closed drawer cannot be tabbed into.
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/ehr");
  expect(await page.locator("#app-sidebar").evaluate((el) => el.inert)).toBe(true);
  await page.getByRole("button", { name: "Open menu" }).click();
  expect(await page.locator("#app-sidebar").evaluate((el) => el.inert)).toBe(false);
  await page.keyboard.press("Escape");
  expect(await page.locator("#app-sidebar").evaluate((el) => el.inert)).toBe(true);
});
