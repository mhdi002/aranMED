// Browser E2E: every patient-data screen lives inside the single EHR section.
const { test, expect } = require("@playwright/test");
const { state, loginAs } = require("./helpers");

test.describe.configure({ mode: "serial" });

test("one EHR entry in the sidebar opens the hub; each tab shows its screen", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/pacs");
  const nav = page.locator(".sidebar");
  // Patient screens are no longer separate top-level menu items.
  for (const gone of ["Patients", "Transfers", "EMS inbound", "Alerts"]) {
    await expect(nav.getByRole("button", { name: gone, exact: true })).toHaveCount(0);
  }
  await nav.getByRole("button", { name: "Electronic health record" }).click();
  await expect(page).toHaveURL(/\/ehr/);
  const hub = page.getByTestId("ehr-hub");
  const tabs = hub.locator("[data-ehr-tab]");
  await expect(tabs).toHaveText(["Patients", "Transfers", "EMS inbound", "Medication alerts", "Text intake"]);

  await expect(page.getByTestId("patient-results").or(page.locator("input[name=q]"))).toBeVisible();
  await hub.locator('[data-ehr-tab="transfers"]').click();
  await expect(page).toHaveURL(/tab=transfers/);
  await expect(page.getByTestId("transfers")).toContainText("E2E Peer Hospital");
  await hub.locator('[data-ehr-tab="ems"]').click();
  await expect(page.getByTestId("ems-board")).toContainText("MEDIC-21");
  await hub.locator('[data-ehr-tab="alerts"]').click();
  await expect(page.getByRole("button", { name: "Check medications" })).toBeVisible();
  await hub.locator('[data-ehr-tab="intake"]').click();
  await expect(page.locator(".list-row", { hasText: "Maryam Ahmadi" })).toBeVisible();

  // The chart is part of the EHR section too, with a way back to the hub.
  await hub.locator('[data-ehr-tab="patients"]').click();
  await page.locator("input[name=q]").fill("L-1001");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByTestId("patient-results").getByRole("button", { name: "Open chart" }).first().click();
  await expect(page).toHaveURL(/\/ehr\/chart\?id=/);
  await expect(nav.locator(".nav-item.active")).toHaveText("Electronic health record");
  await page.getByTestId("back-to-ehr").click();
  await expect(page).toHaveURL(/\/ehr\?tab=patients/);
});

test("old patient URLs redirect into the EHR section", async ({ page, request }) => {
  const st = state();
  await loginAs(page, request, "e2e_doctor", st.user_password);
  for (const [from, to] of [["/clinical", /\/ehr\?tab=patients/], ["/transfers", /\/ehr\?tab=transfers/],
                            ["/ems", /\/ehr\?tab=ems/], ["/alerts", /\/ehr\?tab=alerts/]]) {
    await page.goto(from);
    await expect(page).toHaveURL(to);
  }
  await page.goto(`/clinical/chart?id=${st.local_person}`);
  await expect(page).toHaveURL(new RegExp(`/ehr/chart\\?id=${st.local_person}`));
  await expect(page.getByTestId("chart-name")).toHaveText("Reza Farahani");
});

test("tabs follow the role: a radiologist sees patients and transfers only", async ({ page, request }) => {
  await loginAs(page, request, "e2e_rad", state().user_password);
  await page.goto("/ehr?tab=ems");
  const tabs = page.getByTestId("ehr-hub").locator("[data-ehr-tab]");
  await expect(tabs).toHaveText(["Patients", "Transfers"]);
  // Asking for a tab the role lacks falls back to the first allowed tab.
  await expect(tabs.first()).toHaveClass(/active/);
  await expect(page.getByTestId("ems-board")).toHaveCount(0);
});

test("Persian: EHR hub is right-to-left with translated title and tabs", async ({ page, request }) => {
  await loginAs(page, request, "e2e_doctor", state().user_password);
  await page.goto("/ehr?tab=transfers");
  await page.locator(".lang-pick button", { hasText: "FA" }).click();
  await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  await expect(page.locator(".topbar h1")).toHaveText("پرونده الکترونیک سلامت");
  await expect(page.locator('[data-ehr-tab="transfers"]')).toHaveText("انتقال‌ها");
});
